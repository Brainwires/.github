#!/usr/bin/env python3
"""Pick each job's runner for one workflow run: our self-hosted pools first,
GitHub's hosted image for whatever our pools cannot start right now.

Our runners are there to make CI faster (more cores and RAM per machine), never
to make it wait. So a job goes to our pool only while that pool has a free
slot for it; every other job goes to the GitHub-hosted equivalent at once.

The org's workflows run this through the composite action next to it
(Brainwires/.github/actions/pick-runners). One picker job per workflow run
decides for the run's jobs, prints a `plan` ({job key: runs-on}), and every job
reads `fromJSON(needs.<picker>.outputs.plan).<key>`.

Inputs (environment; the action maps its inputs onto these):

- PICK_JOBS: the jobs, in priority order (whitespace or newline separated).
  The first ones take our free slots. Each entry is one of

      key=pool            our pool while it has a free slot, else its image
      key=pool@image      the same with another GitHub image for the overflow
      key=pool!           pinned: always our pool (the job needs our hardware:
                          a GUI session, the Pi, Windows arm64, ...). It still
                          takes a slot, so the jobs after it overflow sooner.
      key=~other          whatever `other` got (a job that `needs` the other
                          one starts in its slot when it ends)

  `pool` is a name of POOLS below.
- PICK_VARS: the workflow's `toJSON(vars)`. The pool variables (RUNNER_*) say
  whether a pool is in use and under which labels; DENEXT_POOL_HEALTH is the
  fleet's live state (see `free_slots`).
- PICK_MACOS_CAP: the most macOS jobs this run sends to GitHub (default 3).
  GitHub runs about 5 macOS jobs at once for the whole org; past the cap a
  macOS job waits for our pool instead (when the pool is in use).
- PICK_RUNNER_TOKEN (optional): a token that may list the org's self-hosted
  runners (RUNNER_STATUS_TOKEN). With it the running jobs are counted org-wide
  in one call.
- PICK_DRY_RUN: `true` prints the decisions and makes no API call.
- GITHUB_TOKEN (`actions: read`), GITHUB_REPOSITORY, GITHUB_RUN_ID,
  GITHUB_RUN_ATTEMPT, GITHUB_API_URL, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH.

Output (stdout, `$GITHUB_OUTPUT` lines): `plan=<JSON>`, `hosted=<JSON list of
the keys on GitHub>`, `free=<JSON {label: free slots before this run}>`. A
decision table goes to stderr (and the step summary, through the action).

This never fails a run: any error sends every job but the pinned ones to its
image (toward speed), with a warning.
"""

import json
import os
import re
import sys
import time
import urllib.request

# ---- the pools ---------------------------------------------------------------

# name: (variable, label, GitHub image of the overflow, shared capacity group, macOS)
# The variable switches the pool on: unset or empty, its jobs take the image
# (pinned jobs excepted); naming a GitHub image (`windows-latest`), its jobs take
# that image; naming our label (a bare label, or a JSON array such as
# ["self-hosted","denext-windows-x64"]), its jobs take our pool while it has room.
POOLS = {
    "linux-x64": ("RUNNER_LINUX_X64", "denext-linux-x64", "ubuntu-latest", "biscuits", False),
    "linux-x64-2204": ("RUNNER_LINUX_X64_2204", "denext-linux-x64-2204", "ubuntu-22.04", "biscuits", False),
    "linux-x64-docker": ("RUNNER_LINUX_X64_DOCKER", "denext-linux-x64-docker", "ubuntu-latest", "biscuits", False),
    "windows-x64": ("RUNNER_WINDOWS_X64", "denext-windows-x64", "windows-latest", None, False),
    "windows-arm64": ("RUNNER_WINDOWS_ARM", "denext-windows-arm64", "windows-11-arm", None, False),
    "macos-arm64": ("RUNNER_MACOS_ARM", "denext-macos-arm64", "macos-latest", None, True),
    "macos-x64": ("RUNNER_MACOS_X64", "denext-macos-x64", "macos-15-intel", None, True),
    # The Pi (GNOME session on arm64): no GitHub equivalent; pinned jobs only.
    "linux-arm64-desktop": ("RUNNER_LINUX_ARM64_DESKTOP", "denext-linux-arm64-desktop", "ubuntu-24.04-arm", None, False),
}

# A pool whose variable is empty falls back to another pool's variable (the
# docker pool to RUNNER_LINUX_X64, as the workflows did before).
VAR_FALLBACK = {"linux-x64-docker": "RUNNER_LINUX_X64"}

# A shared group's capacity variable (biscuits' three labels are one 8-core
# host's job containers).
GROUP_SLOTS_VAR = {"biscuits": "RUNNER_LINUX_X64_SHARED_SLOTS"}

HEALTH_MAX_AGE = 10 * 60  # seconds: an older DENEXT_POOL_HEALTH is stale
STALE_FREE = 1  # slots per pool (and per shared group) while the health is stale
DEFAULT_MACOS_CAP = 3
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
TIMEOUT = 15  # seconds per request
MAX_PAGES = 5
MAX_RUNS = 40  # this repository's in-flight runs whose jobs are counted
GITHUB_IMAGE = re.compile(r"^(ubuntu|windows|macos)-[0-9a-z.-]+$")


def _dumps(value):
    return json.dumps(value, separators=(",", ":"))


def parse_variable(value):
    """A pool variable's value as ('ours', runs-on, label), ('image', runs-on,
    None) or ('off', None, None)."""
    v = (value or "").strip()
    if not v:
        return ("off", None, None)
    if v.startswith("["):
        try:
            labels = json.loads(v)
        except ValueError:
            return ("off", None, None)
        if not isinstance(labels, list) or not labels or not all(
            isinstance(l, str) and l for l in labels
        ):
            return ("off", None, None)
        label = next((l for l in labels if l != "self-hosted"), None)
        if label is None:
            return ("off", None, None)
        if len(labels) == 1 and GITHUB_IMAGE.match(label):
            return ("image", label, None)
        return ("ours", labels, label)
    if GITHUB_IMAGE.match(v):
        return ("image", v, None)
    return ("ours", v, v)


class Pool:
    def __init__(self, name, variables):
        var, label, image, group, macos = POOLS[name]
        raw = (variables.get(var) or "").strip()
        if not raw and name in VAR_FALLBACK:
            # the x64 pool's label then stands in for the docker pool's
            raw = (variables.get(VAR_FALLBACK[name]) or "").strip()
        self.name = name
        self.mode, self.runs_on, var_label = parse_variable(raw)
        self.label = var_label or label
        self.image = image
        self.group = group
        self.macos = macos
        # Pinned jobs need our hardware whatever the variable says.
        self.pinned_runs_on = self.runs_on if self.mode == "ours" else ["self-hosted", label]


# ---- the jobs ----------------------------------------------------------------

JOB = re.compile(
    r"^(?P<key>[A-Za-z0-9_.-]+)=(?:~(?P<follow>[A-Za-z0-9_.-]+)|"
    r"(?P<pool>[a-z0-9-]+)(?:@(?P<image>[A-Za-z0-9._-]+))?(?P<pin>!)?)$"
)


def parse_jobs(text):
    """PICK_JOBS as a list of dicts: key, pool, image, pinned, follow."""
    jobs, errors = [], []
    for entry in (text or "").split():
        m = JOB.match(entry)
        if not m or (m.group("pool") and m.group("pool") not in POOLS):
            errors.append(entry)
            continue
        jobs.append(
            {
                "key": m.group("key"),
                "pool": m.group("pool"),
                "image": m.group("image"),
                "pinned": bool(m.group("pin")),
                "follow": m.group("follow"),
            }
        )
    return jobs, errors


# ---- the fleet's state -------------------------------------------------------


def fresh_health(value, now):
    """DENEXT_POOL_HEALTH's `pools` map when `value` parses and is at most
    HEALTH_MAX_AGE old, else None (missing, unparsable or stale)."""
    try:
        health = json.loads(value or "")
    except ValueError:
        return None
    if not isinstance(health, dict):
        return None
    at = health.get("checked_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    if now - at > HEALTH_MAX_AGE or at - now > 300:
        return None
    pools = health.get("pools")
    return pools if isinstance(pools, dict) else None


def _n(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


class Counts:
    """What the API saw: per label, this repository's other runs' running and
    queued jobs, and (with the runner token) the org's busy runners."""

    def __init__(self, running=None, queued=None, busy_runners=None):
        self.running = running or {}
        self.queued = queued or {}
        self.busy_runners = busy_runners  # None: not counted


def taken_slots(label, entry, counts):
    """How many of the label's slots are taken or promised.

    `busy` (the fleet's live launchers, a booting one included) and the jobs
    that want the label (running + queued) overlap: a queued job whose runner
    is still booting is in both. So the slots taken are the larger of the two,
    never their sum (the sum is what made the old pickers send jobs to GitHub
    while our pool had room). The queued jobs are the fleet's org-wide count
    (`queued`, from the scheduler's queue view) or this repository's, whichever
    is larger (this repository's is fresher: a workflow dispatched with this one
    has queued its jobs since the fleet last published)."""
    busy = _n(entry.get("busy")) or 0
    running = counts.running.get(label, 0)
    if counts.busy_runners is not None:
        running = max(running, counts.busy_runners.get(label, 0))
    queued = max(_n(entry.get("queued")) or 0, counts.queued.get(label, 0))
    return max(busy, running + queued)


def pool_note(label, pools, stalled):
    """Why a label takes no job whatever its slots, or None."""
    if label in stalled:
        return f"{label} stalled in the previous attempt"
    if pools is None:
        return None
    entry = pools.get(label)
    if not isinstance(entry, dict):
        return f"DENEXT_POOL_HEALTH does not list {label}"
    if entry.get("ok") is not True:
        return f"{label} is unhealthy: {entry.get('reason') or 'not ok'}"[:120]
    if entry.get("paused") is True:
        return f"{label} is paused"
    return None


def pool_free(label, pools, counts, stalled):
    """The free slots of one label: 0 when the fleet marks it not ok or paused,
    does not list it, or a previous attempt's job sat on it until cancelled;
    else its healthy slots minus the taken ones."""
    if label in stalled:
        return 0
    entry = pools.get(label)
    if not isinstance(entry, dict) or entry.get("ok") is not True or entry.get("paused") is True:
        return 0
    cap = _n(entry.get("healthy_slots"))
    if cap is None:
        cap = _n(entry.get("slots")) or 0
    return max(0, cap - taken_slots(label, entry, counts))


def free_slots(pools_in_use, health_pools, variables, counts, stalled):
    """({label: free}, {group: free}) for the pools the jobs use.

    Fresh health: per label as `pool_free`; a shared group's capacity is its
    slots variable, else the largest healthy cap among its labels, minus the
    slots its labels take together. Stale or missing health: STALE_FREE per
    label and per group, minus what this repository's runs already hold there
    (fail open toward speed: one job may wait on a pool we cannot see, the rest
    go to GitHub)."""
    free, groups = {}, {}
    if health_pools is None:
        for p in pools_in_use:
            held = counts.running.get(p.label, 0) + counts.queued.get(p.label, 0)
            free[p.label] = max(0, STALE_FREE - held) if p.label not in stalled else 0
            if p.group:
                g = groups.get(p.group, STALE_FREE)
                groups[p.group] = max(0, g - held)
        return free, groups
    for p in pools_in_use:
        free[p.label] = pool_free(p.label, health_pools, counts, stalled)
    group_labels = {}
    for name, (_, label, _, group, _) in POOLS.items():
        if group:
            group_labels.setdefault(group, []).append(label)
    for group in {p.group for p in pools_in_use if p.group}:
        labels = group_labels[group]
        try:
            cap = int(variables.get(GROUP_SLOTS_VAR.get(group, ""), "") or "x")
        except ValueError:
            cap = None
        if cap is None:
            caps = []
            for l in labels:
                e = health_pools.get(l)
                if isinstance(e, dict) and e.get("ok") is True and e.get("paused") is not True:
                    c = _n(e.get("healthy_slots"))
                    caps.append(c if c is not None else (_n(e.get("slots")) or 0))
            cap = max(caps, default=0)
        busy = sum(_n((health_pools.get(l) or {}).get("busy")) or 0 for l in labels)
        want = 0
        for l in labels:
            e = health_pools.get(l) or {}
            running = counts.running.get(l, 0)
            if counts.busy_runners is not None:
                running = max(running, counts.busy_runners.get(l, 0))
            want += running + max(_n(e.get("queued")) or 0, counts.queued.get(l, 0))
        groups[group] = max(0, cap - max(busy, want))
    return free, groups


# ---- the decision ------------------------------------------------------------


def allocate(jobs, pools, free, groups, macos_cap, fork_pr, notes=None):
    """({key: runs-on}, [decision rows]). Pinned jobs first (they take their
    slots whatever is free), then the rest in priority order: our pool while it
    and its group have a free slot, else the image; a macOS job past the macOS
    cap waits for our pool instead. Followers copy their target."""
    free = dict(free)
    groups = dict(groups)
    plan, rows = {}, {}

    def take(p):
        free[p.label] = free.get(p.label, 0) - 1
        if p.group:
            groups[p.group] = groups.get(p.group, 0) - 1

    for job in jobs:
        if job["follow"] or not job["pinned"]:
            continue
        p = pools[job["pool"]]
        if fork_pr:
            plan[job["key"]] = job["image"] or p.image
            rows[job["key"]] = (p.name, "GitHub", "a fork's pull request never runs on our runners")
            continue
        take(p)
        plan[job["key"]] = p.pinned_runs_on
        rows[job["key"]] = (p.name, "ours (pinned)", "needs our hardware")
    hosted_macos = 0
    for job in jobs:
        if job["follow"] or job["pinned"]:
            continue
        p = pools[job["pool"]]
        image = job["image"] or p.image
        key = job["key"]
        if fork_pr:
            plan[key], rows[key] = image, (p.name, "GitHub", "a fork's pull request never runs on our runners")
        elif p.mode == "off":
            plan[key], rows[key] = image, (p.name, "GitHub", f"{POOLS[p.name][0]} is unset")
        elif p.mode == "image":
            # the job's own @image wins (a leg keeps the image its build ran on)
            plan[key], rows[key] = job["image"] or p.runs_on, (p.name, "GitHub", f"{POOLS[p.name][0]} names a GitHub image")
        elif free.get(p.label, 0) > 0 and (not p.group or groups.get(p.group, 0) > 0):
            take(p)
            plan[key], rows[key] = p.runs_on, (p.name, "ours", f"free slot on {p.label}")
        elif p.macos and hosted_macos >= macos_cap:
            plan[key], rows[key] = p.runs_on, (
                p.name,
                "ours (waits)",
                f"no free slot, but this run already sent {hosted_macos} macOS job(s) to GitHub (cap {macos_cap})",
            )
        else:
            if p.macos:
                hosted_macos += 1
            why = (notes or {}).get(p.label) or (
                f"no free slot on {p.label}" if free.get(p.label, 0) <= 0 else f"the {p.group} pools are full"
            )
            plan[key], rows[key] = image, (p.name, "GitHub", why)
    for job in jobs:
        if job["follow"]:
            target = job["follow"]
            plan[job["key"]] = plan.get(target, "ubuntu-latest")
            rows[job["key"]] = ("~" + target, "same as " + target, "starts in its slot")
    ordered = [j["key"] for j in jobs]
    return {k: plan[k] for k in ordered}, [(k, *rows[k], plan[k]) for k in ordered]


def fallback_plan(jobs, pools, fork_pr):
    """Every job on its image, the pinned ones on our pool (any error)."""
    plan = {}
    for job in jobs:
        if job["follow"]:
            continue
        p = pools[job["pool"]]
        plan[job["key"]] = (
            p.pinned_runs_on if job["pinned"] and not fork_pr else (job["image"] or p.image)
        )
    for job in jobs:
        if job["follow"]:
            plan[job["key"]] = plan.get(job["follow"], "ubuntu-latest")
    return plan


# ---- the API -----------------------------------------------------------------


def api(base, token):
    def get(path):
        req = urllib.request.Request(
            base + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pick-runners",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.load(res)

    return get


def list_all(get, path, key):
    items = []
    for page in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in path else "?"
        body = get(f"{path}{sep}per_page=100&page={page}")
        batch = body.get(key) if isinstance(body, dict) else None
        if not isinstance(batch, list):
            raise ValueError(f'no "{key}" in the answer to {path.split("?")[0]}')
        items += batch
        if len(batch) < 100:
            break
    return items


def count_jobs(get, repo, run_id, labels):
    """(running, queued) per label of `labels`: the active jobs of this
    repository's other queued / in-progress runs (this run's jobs wait on the
    pick). 2 calls plus one per in-flight run (at most MAX_RUNS)."""
    want = set(labels)
    running, queued = {}, {}
    seen = set()
    runs = []
    for status in ("queued", "in_progress"):
        runs += list_all(get, f"/repos/{repo}/actions/runs?status={status}", "workflow_runs")
    for run in runs:
        rid = str(run.get("id"))
        if rid == str(run_id) or rid in seen or len(seen) >= MAX_RUNS:
            continue
        seen.add(rid)
        for job in list_all(get, f"/repos/{repo}/actions/runs/{rid}/jobs?filter=latest", "jobs"):
            status = job.get("status")
            if status not in ACTIVE:
                continue
            label = next((l for l in job.get("labels") or [] if l in want), None)
            if label:
                bucket = running if status == "in_progress" else queued
                bucket[label] = bucket.get(label, 0) + 1
    return running, queued


def busy_runners(get, org, labels):
    """The org's online, busy self-hosted runners per label (one call per 100)."""
    want = set(labels)
    out = {}
    for r in list_all(get, f"/orgs/{org}/actions/runners", "runners"):
        if r.get("status") == "online" and r.get("busy"):
            for l in r.get("labels") or []:
                if l.get("name") in want:
                    out[l["name"]] = out.get(l["name"], 0) + 1
                    break
    return out


def previous_attempt(get, repo, run_id, attempt, labels):
    """(our labels whose job the previous attempt cancelled before any runner
    took it, whether a GitHub macOS job of it went the same way). On a re-run
    the first means our pool was stuck (overflow everything from it); the second
    that GitHub had no macOS capacity (macOS jobs wait for our pool)."""
    if attempt <= 1:
        return set(), False
    stuck, macos = set(), False
    for j in list_all(get, f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt - 1}/jobs", "jobs"):
        if j.get("conclusion") != "cancelled" or j.get("runner_name"):
            continue
        jl = set(j.get("labels") or [])
        stuck |= jl & set(labels)
        if any(l.startswith("macos-") for l in jl):
            macos = True
    return stuck, macos


# ---- glue --------------------------------------------------------------------


def is_fork_pr(env):
    """A pull_request from another repository, or any pull_request_* event other
    than pull_request (pull_request_target runs the base's workflow for
    untrusted code): never on our runners."""
    name = env.get("GITHUB_EVENT_NAME", "")
    if not name.startswith("pull_request"):
        return False
    if name != "pull_request":
        return True
    try:
        with open(env.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as f:
            event = json.load(f)
        head = event["pull_request"]["head"]["repo"]["full_name"]
    except (OSError, ValueError, KeyError, TypeError):
        return True  # cannot tell: treat it as a fork's
    return head != env.get("GITHUB_REPOSITORY")


def pick(env, get=None, runner_get=None, warn=lambda m: None, now=None):
    """(plan, rows, free) for the environment `env` (see the module docstring)."""
    now = time.time() if now is None else now
    jobs, bad = parse_jobs(env.get("PICK_JOBS"))
    for entry in bad:
        warn(f"pick-runners: ignored the malformed or unknown job entry {entry!r}")
    try:
        variables = json.loads(env.get("PICK_VARS") or "{}")
        if not isinstance(variables, dict):
            raise ValueError("not an object")
    except ValueError:
        warn("pick-runners: PICK_VARS is not toJSON(vars); every pool is off")
        variables = {}
    pools = {name: Pool(name, variables) for name in {j["pool"] for j in jobs if j["pool"]}}
    fork_pr = is_fork_pr(env)
    try:
        macos_cap = max(0, int(env.get("PICK_MACOS_CAP") or DEFAULT_MACOS_CAP))
    except ValueError:
        macos_cap = DEFAULT_MACOS_CAP
    in_use = [p for p in pools.values() if p.mode == "ours"]
    pinned_only = all(j["pinned"] or j["follow"] for j in jobs)
    if fork_pr or not in_use or pinned_only:
        plan, rows = allocate(jobs, pools, {}, {}, macos_cap, fork_pr)
        return plan, rows, {}
    try:
        health = fresh_health(variables.get("DENEXT_POOL_HEALTH"), now)
        if health is None:
            warn("pick-runners: DENEXT_POOL_HEALTH is missing, unparsable or older than 10 min:"
                 f" at most {STALE_FREE} job per pool goes to our runners")
        groups_in_use = {p.group for p in in_use if p.group}
        labels = sorted({p.label for p in in_use} |
                        {POOLS[n][1] for n in POOLS if POOLS[n][3] in groups_in_use})
        counts, stalled = Counts(), set()
        repo, run_id = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
        if get is not None:
            try:
                attempt = int(env.get("GITHUB_RUN_ATTEMPT") or "1")
            except ValueError:
                attempt = 1
            try:
                stalled, macos_starved = previous_attempt(get, repo, run_id, attempt, labels)
                if stalled:
                    warn(f"pick-runners: the previous attempt's job(s) on {', '.join(sorted(stalled))}"
                         " were cancelled before a runner took them: GitHub takes those pools' jobs")
                if macos_starved:
                    warn("pick-runners: the previous attempt's GitHub macOS job was cancelled before a"
                         " runner took it: macOS jobs wait for our pool this time")
                    macos_cap = 0
            except Exception as e:  # advisory
                warn(f"pick-runners: previous-attempt check failed ({e}); ignored")
            try:
                running, queued = count_jobs(get, repo, run_id, labels)
                counts = Counts(running, queued)
            except Exception as e:
                warn(f"pick-runners: counting this repository's jobs failed ({e}); the fleet's numbers alone count")
        if runner_get is not None:
            try:
                counts.busy_runners = busy_runners(runner_get, repo.split("/")[0], labels)
            except Exception as e:
                warn(f"pick-runners: listing the org's runners failed ({e}); ignored")
        free, groups = free_slots(in_use, health, variables, counts, stalled)
        notes = {p.label: pool_note(p.label, health, stalled) for p in in_use}
        plan, rows = allocate(jobs, pools, free, groups, macos_cap, False, notes)
        return plan, rows, free
    except Exception as e:  # never fail the run
        warn(f"pick-runners: {e}; every job but the pinned ones takes its GitHub image")
        plan = fallback_plan(jobs, pools, False)
        return plan, [(k, "?", "GitHub", "picker error", v) for k, v in plan.items()], {}


def table(rows, free):
    lines = ["| job | pool | decision | why | runs-on |", "| --- | --- | --- | --- | --- |"]
    for key, pool, decision, why, runs_on in rows:
        lines.append(f"| {key} | {pool} | {decision} | {why} | `{_dumps(runs_on)}` |")
    if free:
        lines.append("")
        lines.append("Free slots before this run: " + ", ".join(f"{l} {n}" for l, n in sorted(free.items())))
    return "\n".join(lines)


def main(argv):
    env = dict(os.environ)
    dry = env.get("PICK_DRY_RUN") == "true" or "--dry-run" in argv
    warn = lambda m: print(f"::warning::{m}", file=sys.stderr)
    get = runner_get = None
    if not dry:
        base = env.get("GITHUB_API_URL") or "https://api.github.com"
        if env.get("GITHUB_TOKEN"):
            get = api(base, env["GITHUB_TOKEN"])
        if env.get("PICK_RUNNER_TOKEN"):
            runner_get = api(base, env["PICK_RUNNER_TOKEN"])
    try:
        plan, rows, free = pick(env, get, runner_get, warn)
    except Exception as e:  # pick() catches its own; this is the last guard
        warn(f"pick-runners: {e}")
        plan, rows, free = {}, [], {}
    hosted = [k for k, v in plan.items() if isinstance(v, str) and GITHUB_IMAGE.match(v)]
    print(f"plan={_dumps(plan)}")
    print(f"hosted={_dumps(hosted)}")
    print(f"free={_dumps(free)}")
    print(("DRY RUN (no API call)\n" if dry else "") + table(rows, free), file=sys.stderr)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary and not dry:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### Runner picks\n\n" + table(rows, free) + "\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
