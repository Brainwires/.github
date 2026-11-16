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

  A non-follower entry may also state the job's memory need, `+<N>g` (N 1-999,
  in GB), after the optional `@image` and before the optional `!`:
  `build=linux-arm64@ubuntu-22.04-arm+16g`, `t=macos-arm64+12g!`. A job that
  needs more than its pool holds (the health entry's `mem_gb`, else
  POOL_MEM_GB) takes its image whatever is free; a pinned one stays pinned
  and gets a warning (it may not fit). See `mem_capacity`.

  `pool` is a name of POOLS below.
- PICK_VARS: the workflow's `toJSON(vars)`. The pool variables (RUNNER_*) say
  whether a pool is in use and under which labels; DENEXT_POOL_HEALTH is the
  fleet's live state (see `free_slots`). Two optional fields of it are read
  when present (absent or malformed, the picker decides as without them):
  a pool entry's `mem_gb` (the most memory one job on that label can use,
  swap it may use included; see `mem_capacity`), and the top-level
  `github: {"macos"|"windows"|"ubuntu": {"queued": n, "oldest_at": epoch}}`
  (the org's jobs queued on GitHub's images of that family; see
  `github_saturated`). Both are as fresh as `checked_at`: stale health
  ignores them too.
- PICK_MACOS_CAP: the most macOS jobs this run sends to GitHub (default 3).
  GitHub runs about 5 macOS jobs at once for the whole org; past the cap a
  macOS job waits for our pool instead (when the pool is in use).
- PICK_RUNNER_TOKEN (optional): a token that may list the org's self-hosted
  runners (RUNNER_STATUS_TOKEN). With it the running jobs are counted org-wide
  in one call.
- PICK_SETTLE (optional): the most seconds to wait for earlier sibling runs'
  pickers (default 90; 0 turns it off). See `settle`.
- PICK_DRY_RUN: `true` prints the decisions and makes no API call.
- GITHUB_TOKEN (`actions: read`), GITHUB_REPOSITORY, GITHUB_RUN_ID,
  GITHUB_RUN_ATTEMPT, GITHUB_API_URL, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH.

Output (stdout, `$GITHUB_OUTPUT` lines): `plan=<JSON>`, `hosted=<JSON list of
the keys on GitHub>`, `free=<JSON {label: free slots before this run}>`,
`blocked=<JSON list of the labels that take no job at all: unhealthy, paused,
unlisted or stalled in the previous attempt>` (a planner must never make a job
wait on one of those, macOS cap or not), `saturated=<JSON list of the GitHub
image families (macos, windows, ubuntu) that were saturated: a job of theirs
waits for our busy pool instead of joining GitHub's queue>`. A
decision table goes to stderr (and the step summary, through the action).

This never fails a run: any error sends every job but the pinned ones to its
image (toward speed), with a warning.
"""

import calendar
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
    # The FAST Windows x64 host only (win-x64, the MacBook VM): for long jobs. The label is an
    # extra label win-x64's runners carry beside denext-windows-x64; brain (win-x64-brain, an
    # i3 2C/4T) serves only the shared label and runs a 65-72 min job in 80-95 min. Switched by
    # RUNNER_WINDOWS_X64; its slot is one of windows-x64's (SUBPOOL_OF).
    "windows-x64-fast": ("RUNNER_WINDOWS_X64", "denext-windows-x64-fast", "windows-latest", None, False),
    "windows-arm64": ("RUNNER_WINDOWS_ARM", "denext-windows-arm64", "windows-11-arm", None, False),
    "macos-arm64": ("RUNNER_MACOS_ARM", "denext-macos-arm64", "macos-latest", None, True),
    "macos-x64": ("RUNNER_MACOS_X64", "denext-macos-x64", "macos-15-intel", None, True),
    # Linux arm64 job containers on the Mac mini (Docker Desktop; Ubuntu 22.04 image, so a job that needs
    # GitHub's 22.04 ARM image on overflow names it: `key=linux-arm64@ubuntu-22.04-arm`).
    "linux-arm64": ("RUNNER_LINUX_ARM64", "denext-linux-arm64", "ubuntu-24.04-arm", None, False),
    # The Pi (GNOME session on arm64): no GitHub equivalent; pinned jobs only.
    "linux-arm64-desktop": ("RUNNER_LINUX_ARM64_DESKTOP", "denext-linux-arm64-desktop", "ubuntu-24.04-arm", None, False),
}

# A pool whose variable is empty falls back to another pool's variable (the
# docker pool to RUNNER_LINUX_X64, as the workflows did before).
VAR_FALLBACK = {"linux-x64-docker": "RUNNER_LINUX_X64"}

# A pool whose runners are some of another pool's: its label is extra on those runners, so a
# slot it takes is also one of the other's (and vice versa through the fleet's busy count).
SUBPOOL_OF = {"windows-x64-fast": "windows-x64"}

# The most memory (GB, swap a job may use included) one job on a pool's label can use, while
# DENEXT_POOL_HEALTH gives no `mem_gb` for it (stale, or not published). linux-arm64's job
# container holds 6 GB + 2 GB swap: a deno debug build OOMed there (2026-11-16).
POOL_MEM_GB = {
    "linux-x64": 7,
    "linux-x64-2204": 7,
    "linux-x64-docker": 7,
    "windows-x64": 8,
    "windows-x64-fast": 8,
    "windows-arm64": 8,
    "macos-arm64": 8,
    "macos-x64": 16,
    "linux-arm64": 8,
    "linux-arm64-desktop": 10,
}

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
GITHUB_FAMILIES = ("macos", "windows", "ubuntu")
WAIT_DEPTH = 2  # jobs of one run that may wait for one busy pool of ours (2026-11-16: 6 macOS jobs queued behind 2 slots while GitHub emptied); past it they overflow
GITHUB_SATURATED_AFTER = 180  # seconds a family's oldest queued GitHub job waits before it is saturated


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
        self.parent_label = None
        if name in SUBPOOL_OF:
            # the variable is only the switch: the runs-on is always the fixed sub-label
            self.parent_label = Pool(SUBPOOL_OF[name], variables).label
            self.label = label
            if self.mode == "ours":
                self.runs_on = ["self-hosted", label]
        self.image = image
        self.group = group
        self.macos = macos
        # Pinned jobs need our hardware whatever the variable says.
        self.pinned_runs_on = self.runs_on if self.mode == "ours" else ["self-hosted", label]


# ---- the jobs ----------------------------------------------------------------

JOB = re.compile(
    r"^(?P<key>[A-Za-z0-9_.-]+)=(?:~(?P<follow>[A-Za-z0-9_.-]+)|"
    r"(?P<pool>[a-z0-9-]+)(?:@(?P<image>[A-Za-z0-9._-]+))?"
    r"(?:\+(?P<mem>[1-9][0-9]{0,2})g)?(?P<pin>!)?)$"
)


def parse_jobs(text):
    """PICK_JOBS as a list of dicts: key, pool, image, mem (GB or None),
    pinned, follow."""
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
                "mem": int(m.group("mem")) if m.group("mem") else None,
                "pinned": bool(m.group("pin")),
                "follow": m.group("follow"),
            }
        )
    return jobs, errors


# ---- the fleet's state -------------------------------------------------------


def _fresh(value, now):
    """DENEXT_POOL_HEALTH as a dict when `value` parses and is at most
    HEALTH_MAX_AGE old, else None (missing, unparsable or stale)."""
    try:
        health = json.loads(value or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(health, dict):
        return None
    at = health.get("checked_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    if now - at > HEALTH_MAX_AGE or at - now > 300:
        return None
    return health


def fresh_health(value, now):
    """DENEXT_POOL_HEALTH's `pools` map when `value` parses and is at most
    HEALTH_MAX_AGE old, else None (missing, unparsable or stale)."""
    health = _fresh(value, now)
    pools = health.get("pools") if health else None
    return pools if isinstance(pools, dict) else None


def github_saturated(value, now):
    """{family: seconds its oldest queued job has waited} for the GitHub image
    families (GITHUB_FAMILIES) that are saturated right now: the fresh health's
    `github.<family>` has `queued` >= 1 and its `oldest_at` at least
    GITHUB_SATURATED_AFTER seconds ago.

    Why (laufey run of 2026-11-16): GitHub's macOS arm64 capacity was degraded;
    a job sent to `macos-15` because our mac-arm pool was busy sat in GitHub's
    queue until GitHub cancelled it after 15 min ("not acquired by Runner of type
    hosted"), twice, while our own pool freed minutes later. When GitHub already
    has a job of the family waiting that long, a busy pool of ours is the faster
    queue. Stale health, no `github` key, a family it does not list, or a
    malformed entry (no count, no `oldest_at`): not saturated (as before the
    field existed)."""
    health = _fresh(value, now)
    github = health.get("github") if health else None
    if not isinstance(github, dict):
        return {}
    out = {}
    for family in GITHUB_FAMILIES:
        e = github.get(family)
        if not isinstance(e, dict):
            continue
        queued, oldest = _n(e.get("queued")), _n(e.get("oldest_at"))
        if not queued or not oldest:
            continue
        if now - oldest >= GITHUB_SATURATED_AFTER:
            out[family] = int(now - oldest)
    return out


def image_family(image):
    """The GitHub image family of a runs-on image (`macos-15` -> macos), or None
    for anything that is not one of GITHUB_FAMILIES."""
    if not isinstance(image, str):
        return None
    family = image.split("-", 1)[0]
    return family if family in GITHUB_FAMILIES else None


def mem_capacity(pool, health_pools):
    """The most memory (GB) one job on `pool` can use: its fresh health entry's
    `mem_gb` (a positive number) when the fleet publishes one, else the static
    POOL_MEM_GB default. The fleet's number wins because it is measured (a
    container's limit plus the swap it may use), the default only a floor that
    keeps a stale value from sending an over-sized job to a pool it cannot fit
    (the linux-arm64 OOM of 2026-11-16)."""
    entry = (health_pools or {}).get(pool.label)
    gb = _n(entry.get("mem_gb")) if isinstance(entry, dict) else None
    return gb if gb else POOL_MEM_GB[pool.name]


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


def allocate(jobs, pools, free, groups, macos_cap, fork_pr, notes=None, saturated=None, mem=None):
    """({key: runs-on}, [decision rows]). Pinned jobs first (they take their
    slots whatever is free), then the rest in priority order: a job that needs
    more memory than its pool holds takes its image; else our pool while it and
    its group have a free slot, else the image; a macOS job past the macOS cap,
    or a job whose image family is saturated on GitHub (`saturated`, from
    `github_saturated`), waits for our pool instead, but only for a pool that is
    merely busy (no `notes` entry). `mem` is {pool name: GB} (`mem_capacity`;
    POOL_MEM_GB for a pool it leaves out). Followers copy their target."""
    free = dict(free)
    groups = dict(groups)
    notes, saturated = notes or {}, saturated or {}
    plan, rows = {}, {}

    def mem_of(p):
        return (mem or {}).get(p.name) or POOL_MEM_GB[p.name]

    def take(p):
        free[p.label] = free.get(p.label, 0) - 1
        if p.parent_label in free:
            free[p.parent_label] -= 1
        for other in pools.values():  # a sub-pool's slots are its parent's
            if other.parent_label == p.label and other.label in free:
                free[other.label] = min(free[other.label], free[p.label])
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
        why = "needs our hardware"
        if job["mem"] and job["mem"] > mem_of(p):
            why += f"; needs {job['mem']} GB, {p.label} holds {mem_of(p)} (may not fit)"
        rows[job["key"]] = (p.name, "ours (pinned)", why)
    hosted_macos = 0
    waits = {}  # label -> jobs of this run that wait for it (at most WAIT_DEPTH)
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
        elif job["mem"] and job["mem"] > mem_of(p):
            # our pool cannot hold the job (the linux-arm64 OOM): GitHub's image whatever is free
            if p.macos:
                hosted_macos += 1
            plan[key], rows[key] = image, (p.name, "GitHub", f"needs {job['mem']} GB, {p.label} holds {mem_of(p)}")
        elif free.get(p.label, 0) > 0 and (not p.group or groups.get(p.group, 0) > 0) and (
            p.parent_label not in free or free[p.parent_label] > 0
        ):
            take(p)
            plan[key], rows[key] = p.runs_on, (p.name, "ours", f"free slot on {p.label}")
        elif p.macos and hosted_macos >= macos_cap and not notes.get(p.label) and waits.get(p.label, 0) < WAIT_DEPTH:
            waits[p.label] = waits.get(p.label, 0) + 1
            # GitHub's macOS share is spent: wait for our pool, but only for a pool that is
            # merely busy (never one that is unhealthy, paused, unlisted or stalled).
            plan[key], rows[key] = p.runs_on, (
                p.name,
                "ours (waits)",
                f"no free slot, but this run already sent {hosted_macos} macOS job(s) to GitHub (cap {macos_cap})",
            )
        elif image_family(image) in saturated and not notes.get(p.label) and waits.get(p.label, 0) < WAIT_DEPTH:
            waits[p.label] = waits.get(p.label, 0) + 1
            # GitHub has had a job of this family queued for minutes: our busy pool frees sooner.
            # Not counted against the macOS cap (the job does not go to GitHub).
            family = image_family(image)
            busy = "no free slot" if free.get(p.label, 0) <= 0 else f"the {p.group} pools are full"
            plan[key], rows[key] = p.runs_on, (
                p.name,
                "ours (waits)",
                f"{busy}, but GitHub's {family} images have had a job queued for"
                f" {saturated[family] // 60} min (saturated)",
            )
        else:
            if p.macos:
                hosted_macos += 1
            why = notes.get(p.label) or (
                f"no free slot on {p.label}" if free.get(p.label, 0) <= 0 else f"the {p.group} pools are full"
            )
            if waits.get(p.label, 0) >= WAIT_DEPTH:
                why += f" ({WAIT_DEPTH} job(s) of this run already wait for it)"
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


SETTLE_MAX = 90  # seconds, at most (PICK_SETTLE)
SETTLE_WINDOW = 120  # seconds: an earlier run created this long before ours is a sibling
SETTLE_STEP = 5  # seconds between looks
SETTLE_QUIET = 15  # seconds with no sibling picking before this run counts
# A picker job: `pick … runners`, `<leg> / pick … runners` (a reusable workflow's), or
# `<job>: pick … runners` (a job that also picks, such as the fork runtime run's
# `setup: pick matrix runners`, whose step hands the action's free slots to plan_matrix).
PICKER_JOB = re.compile(r"(^| / |: )pick [^/]*runners?$")


def _epoch(stamp):
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def settle(get, repo, run_id, sleep, clock, max_wait=SETTLE_MAX, warn=lambda m: None):
    """Wait for this repository's EARLIER sibling runs to finish picking, so their
    picked jobs exist (and are counted) before this run decides. Returns the
    seconds waited.

    The same-second race (Brainwires/deno run 43774202217): two workflows
    dispatched seconds apart both count the pools before either has queued its
    jobs, and both send a job to the same single slot. A sibling is an in-flight
    run of this repository created at most SETTLE_WINDOW seconds before this one,
    with a LOWER run id (so two pickers never wait for each other: the later run
    yields). While a sibling has no jobs yet (still starting) or has a picker job
    (PICKER_JOB) queued or running, look again every SETTLE_STEP seconds; count
    only after SETTLE_QUIET seconds in which no sibling was picking, for at most
    `max_wait` in all. The quiet period covers a sibling between two pickers
    (the fork runtime run: `pick runners`, then `guard`, about 7 s, then `setup:
    pick matrix runners`, whose picked jobs exist the moment it ends): the jobs
    API lists a job only once its `needs` are done, so the next picker cannot be
    seen before it starts. A picker much further down a sibling's graph (a test
    leg's, after an hour-long build) picks from the numbers of its own time.
    Costs 2 calls plus one per sibling per look, only when a sibling exists."""
    if max_wait <= 0:
        return 0
    start = clock()
    quiet_since = None  # when the siblings were last seen not picking
    any_sibling = False
    while True:
        runs, seen = [], set()
        for status in ("queued", "in_progress"):
            for r in list_all(get, f"/repos/{repo}/actions/runs?status={status}", "workflow_runs"):
                if str(r.get("id")) not in seen:
                    seen.add(str(r.get("id")))
                    runs.append(r)
        me = next((r for r in runs if str(r.get("id")) == str(run_id)), None)
        mine = _epoch(me.get("created_at")) if me else None
        if mine is None:
            break
        pending = []
        for r in runs:
            try:
                earlier = int(r.get("id")) < int(run_id)
            except (TypeError, ValueError):
                continue
            created = _epoch(r.get("created_at"))
            if not earlier or created is None or not 0 <= mine - created <= SETTLE_WINDOW:
                continue
            any_sibling = True
            jobs = list_all(get, f"/repos/{repo}/actions/runs/{r['id']}/jobs?filter=latest", "jobs")
            if not jobs or any(
                PICKER_JOB.search(j.get("name") or "") and j.get("status") in ("queued", "in_progress")
                for j in jobs
            ):
                pending.append(str(r["id"]))
        if not any_sibling:
            break
        if pending:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = clock()
        if quiet_since is not None and clock() - quiet_since >= SETTLE_QUIET:
            break
        if clock() - start >= max_wait:
            if pending:
                warn(f"pick-runners: earlier run(s) {', '.join(pending)} still picking after {max_wait}s; deciding anyway")
            break
        sleep(SETTLE_STEP)
    return round(clock() - start)


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


LAST = {"blocked": [], "saturated": {}}  # side output of the latest pick() (kept out of its return value)


def pick(env, get=None, runner_get=None, warn=lambda m: None, now=None, sleep=time.sleep,
         clock=time.monotonic):
    """(plan, rows, free) for the environment `env` (see the module docstring)."""
    LAST["blocked"], LAST["saturated"] = [], {}
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
    health = fresh_health(variables.get("DENEXT_POOL_HEALTH"), now)
    saturated = github_saturated(variables.get("DENEXT_POOL_HEALTH"), now)
    LAST["saturated"] = saturated
    mem = {name: mem_capacity(p, health) for name, p in pools.items()}
    for job in jobs:
        if job["pinned"] and job["mem"] and not fork_pr and job["mem"] > mem[job["pool"]]:
            warn(f"pick-runners: {job['key']} needs {job['mem']} GB but is pinned to"
                 f" {pools[job['pool']].label}, which holds {mem[job['pool']]}: it may not fit")
    if fork_pr or not in_use or pinned_only:
        plan, rows = allocate(jobs, pools, {}, {}, macos_cap, fork_pr, mem=mem)
        return plan, rows, {}
    try:
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
                limit = int(env.get("PICK_SETTLE") or SETTLE_MAX)
            except ValueError:
                limit = SETTLE_MAX
            try:
                waited = settle(get, repo, run_id, sleep, clock, limit, warn)
                if waited:
                    warn(f"pick-runners: waited {waited}s for an earlier run of this repository to pick first")
            except Exception as e:  # advisory: count now
                warn(f"pick-runners: the sibling check failed ({e}); counting now")
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
        plan, rows = allocate(jobs, pools, free, groups, macos_cap, False, notes, saturated, mem)
        LAST["blocked"] = sorted(l for l, n in notes.items() if n)
        return plan, rows, free
    except Exception as e:  # never fail the run
        warn(f"pick-runners: {e}; every job but the pinned ones takes its GitHub image")
        plan = fallback_plan(jobs, pools, False)
        return plan, [(k, "?", "GitHub", "picker error", v) for k, v in plan.items()], {}


def table(rows, free, saturated=None):
    lines = ["| job | pool | decision | why | runs-on |", "| --- | --- | --- | --- | --- |"]
    for key, pool, decision, why, runs_on in rows:
        lines.append(f"| {key} | {pool} | {decision} | {why} | `{_dumps(runs_on)}` |")
    if free:
        lines.append("")
        lines.append("Free slots before this run: " + ", ".join(f"{l} {n}" for l, n in sorted(free.items())))
    if saturated:
        lines.append("")
        lines.append("GitHub saturated: " + ", ".join(
            f"{f} (queued {s // 60} min)" for f, s in sorted(saturated.items())))
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
    print(f"blocked={_dumps(LAST['blocked'])}")
    print(f"saturated={_dumps(sorted(LAST['saturated']))}")
    print(("DRY RUN (no API call)\n" if dry else "") + table(rows, free, LAST["saturated"]), file=sys.stderr)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary and not dry:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### Runner picks\n\n" + table(rows, free, LAST["saturated"]) + "\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
