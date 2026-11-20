#!/usr/bin/env python3
"""Pick each job's runner for one workflow run: our self-hosted pools first,
GitHub's hosted image only as the backup.

A job goes to our pool unless its EXPECTED WAIT there exceeds its family's
threshold (PICK_MAX_WAIT; Linux 10 min, Windows 15, macOS 20). The wait comes
from the pool's slots, what is busy and queued, the jobs this same run has
already placed on it, and a per-label job duration (POOL_JOB_MINUTES); see
`expected_wait` and `allocate`. So a burst of jobs that start in the same second
spreads over our slots, and only the ones that would wait too long overflow. An
unhealthy, paused, unlisted or stalled pool sends its jobs to GitHub; with stale
health the old rule holds (a free slot, else GitHub). A job that lands on GitHub
although its healthy pool's wait was within the threshold is a TRIPWIRE: an
error annotation, the `tripwire` output and a line in the step summary.

The org's workflows run this through the composite action next to it
(Brainwires/.github/actions/pick-runners). One picker job per workflow run
decides for the run's jobs, prints a `plan` ({job key: runs-on}), and every job
reads `fromJSON(needs.<picker>.outputs.plan).<key>`.

Inputs (environment; the action maps its inputs onto these):

- PICK_JOBS: the jobs, in priority order (whitespace or newline separated).
  The first ones are measured first (each placement lengthens the queue for the next). Each entry is one of

      key=pool            our pool unless its expected wait exceeds the threshold, else its image
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
  ignores them too, and an aged one (`aged_health`) keeps `mem_gb` only.
  Two more pool-entry fields drive the expected wait while the health is
  fresh: `live` ([[start, eta], ...] epochs, one pair per busy slot of the
  label, eta = when the slot is expected to free) and `job_s` (the label's
  median job duration in seconds); see `live_wait`. Without them, or with an
  aged or stale value, the wait comes from the table (POOL_JOB_MINUTES).
  The top-level `pull_request_labels` (the runs-on labels whose pools the
  fleet scheduler hands a same-repo pull_request) decides where a same-repo
  pull request's jobs may go: a pool whose label it leaves out sends them to
  GitHub, and so does every pool while it is missing (see `pr_labels`).
- PICK_MAX_WAIT (optional): the per-family thresholds in minutes, any subset of
  `linux=10 windows=15 macos=20` (DEFAULT_MAX_WAIT); 0 restores the free-slot-only rule.
  A family that is off (`github_off`) never goes to GitHub whatever the wait.
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
waits for our busy pool instead of joining GitHub's queue>`, `tripwire=<JSON list
of {key, pool, label, wait, threshold, repo, run_id, attempt, numbers} for each job
sent to GitHub while its healthy pool's expected wait was within the threshold (or
by the picker-error fallback)>`. A decision table (each job's pool, expected wait,
threshold and verdict), one `pick-runners decision <JSON>` line per job, and an
`::error title=pick-runners tripwire::` annotation per tripwire go to stderr (and
the table to the step summary, through the action).

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
# ["self-hosted","denext-windows-x64"]), its jobs take our pool unless the wait is too long.
POOLS = {
    "linux-x64": ("RUNNER_LINUX_X64", "denext-linux-x64", "ubuntu-latest", "linux-host", False),
    "linux-x64-2204": ("RUNNER_LINUX_X64_2204", "denext-linux-x64-2204", "ubuntu-22.04", "linux-host", False),
    "linux-x64-docker": ("RUNNER_LINUX_X64_DOCKER", "denext-linux-x64-docker", "ubuntu-latest", "linux-host", False),
    "windows-x64": ("RUNNER_WINDOWS_X64", "denext-windows-x64", "windows-latest", None, False),
    # The FAST Windows x64 host only (win-x64, a Windows VM): for long jobs. The label is an
    # extra label win-x64's runners carry beside denext-windows-x64; the second Windows host (win-x64-brain, an
    # i3 2C/4T) serves only the shared label and runs a 65-72 min job in 80-95 min. Switched by
    # RUNNER_WINDOWS_X64; its slot is one of windows-x64's (SUBPOOL_OF).
    "windows-x64-fast": ("RUNNER_WINDOWS_X64", "denext-windows-x64-fast", "windows-latest", None, False),
    "windows-arm64": ("RUNNER_WINDOWS_ARM", "denext-windows-arm64", "windows-11-arm", "mini-host", False),
    "macos-arm64": ("RUNNER_MACOS_ARM", "denext-macos-arm64", "macos-latest", "mini-host", True),
    "macos-x64": ("RUNNER_MACOS_X64", "denext-macos-x64", "macos-15-intel", None, True),
    # Linux arm64 job containers on the Mac host (Docker Desktop; Ubuntu 22.04 image, so a job that needs
    # GitHub's 22.04 ARM image on overflow names it: `key=linux-arm64@ubuntu-22.04-arm`).
    "linux-arm64": ("RUNNER_LINUX_ARM64", "denext-linux-arm64", "ubuntu-24.04-arm", "mini-host", False),
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

# A shared group's capacity variable (the Linux host's three labels are one 8-core
# host's job containers).
GROUP_SLOTS_VAR = {"linux-host": "RUNNER_LINUX_X64_SHARED_SLOTS", "mini-host": "RUNNER_MINI_MEM_GB"}

# A group measured in memory, not slots. The Mac host's three pools (macOS VMs, Windows arm64 VMs,
# Linux arm64 job containers) each have their own slots but share one RAM budget (the fleet's
# RAM_BUDGET_GB, 16): a job takes its pool's GB of it. Two macOS VMs fill it, and a Linux arm64 job
# then waits for one of them to end however free its own slot looks (Brainwires/deno, 2026-11-18/19:
# `ubuntu-22.04-arm / build debug` waited 10, 27 and 36 min on a "free" slot with GitHub's arm64
# image idle). name -> GB per job; the capacity is the group's variable, else GROUP_MEM_GB.
GROUP_WEIGHT = {"mini-host": {"macos-arm64": 8, "windows-arm64": 8, "linux-arm64": 6}}
GROUP_MEM_GB = {"mini-host": 16}

# Typical minutes one job holds a slot of the pool, for the expected wait (see `expected_wait`). These are
# deliberately conservative per-label estimates from the fleet's 7-day history (2026-11-19): linux-x64's
# p50 is 26 s and p90 under 5 min, but a denext gate's coverage / integration jobs run 10-20 min;
# windows-x64's p50 is 19 min; the fast Windows label takes only the long jobs (a 45-75 min build);
# the Mac host's VMs and arm64 containers run 15-40 min. A wrong guess only moves where the threshold
# bites: the threshold, not this table, is the knob (PICK_MAX_WAIT).
POOL_JOB_MINUTES = {
    "linux-x64": 10,
    "linux-x64-2204": 10,
    "linux-x64-docker": 10,
    "windows-x64": 20,
    "windows-x64-fast": 60,
    "windows-arm64": 30,
    "macos-arm64": 20,
    "macos-x64": 30,
    "linux-arm64": 30,
    "linux-arm64-desktop": 15,
}

# The longest expected wait (minutes) a job accepts on our pool before it goes to GitHub's image, per
# family (PICK_MAX_WAIT overrides: `linux=10 windows=15 macos=20`; 0 is the old rule, a free slot or
# GitHub). macOS is the longest: GitHub runs about 5 macOS jobs for the whole org, and its macOS queue
# saturates first. A family ops turned off (`github_off`) never goes to GitHub whatever the wait.
DEFAULT_MAX_WAIT = {"linux": 10, "windows": 15, "macos": 20}
FAMILY_NAME = {"ubuntu": "linux", "windows": "windows", "macos": "macos"}


def group_weight(pool_name):
    """What one job of the pool takes of its group: its GB in a memory group, else 1 slot."""
    return GROUP_WEIGHT.get(POOLS[pool_name][3], {}).get(pool_name, 1)


def _stale_group_free(group):
    """A group's room while the health is stale: one job (its largest job in a memory group)."""
    return max(GROUP_WEIGHT.get(group, {}).values(), default=STALE_FREE)

HEALTH_MAX_AGE = 10 * 60  # seconds: an older DENEXT_POOL_HEALTH is stale
# GitHub fixes a run's `vars` (so DENEXT_POOL_HEALTH) when the run (attempt) starts, not when a job
# starts: a picker behind a 25-min build reads a 30-min-old value however often the fleet publishes
# (Brainwires/deno run 44005663132, 2026-11-17: four pickers from 02:48 to 03:28 all read the value
# checked at 02:42). Such a value, fresh when the run started and at most this old, is "aged": see
# `aged_health`.
AGED_MAX_AGE = 90 * 60
STALE_FREE = 1  # slots per pool (and per shared group) while the health is stale
DEFAULT_MACOS_CAP = 3
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
TIMEOUT = 15  # seconds per request
MAX_PAGES = 5
MAX_RUNS = 40  # this repository's in-flight runs whose jobs are counted
GITHUB_IMAGE = re.compile(r"^(ubuntu|windows|macos)-[0-9a-z.-]+$")
GITHUB_FAMILIES = ("macos", "windows", "ubuntu")
GITHUB_OFF_MAX_AGE = 6 * 3600  # seconds the github_off switch in a frozen health value still holds
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


def aged_health(value, now, started):
    """DENEXT_POOL_HEALTH's `pools` map when it is no longer fresh only because
    GitHub froze it when this run (attempt) started at `started` (epoch): it was
    at most HEALTH_MAX_AGE old then, and is at most AGED_MAX_AGE old now. Else
    None (`started` unknown, already stale when the run began, too old, or
    unparsable). The picker then trusts its pool STATES (ok, paused, slots,
    healthy_slots, mem_gb: an unhealthy pool stays blocked) and counts the jobs
    itself; its `github` queue is ignored (stale, as before)."""
    if started is None:
        return None
    try:
        health = json.loads(value or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(health, dict):
        return None
    at = health.get("checked_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    if now - at > AGED_MAX_AGE or at - now > 300 or started - at > HEALTH_MAX_AGE:
        return None
    pools = health.get("pools")
    return pools if isinstance(pools, dict) else None


def run_started(get, repo, run_id):
    """When this run's current attempt started (epoch), from the run itself."""
    run = get(f"/repos/{repo}/actions/runs/{run_id}")
    if not isinstance(run, dict):
        return None
    return _epoch(run.get("run_started_at")) or _epoch(run.get("created_at"))


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


def github_off(value, now):
    """The GitHub image families ops turned off (the fresh health's `github_off`, set from the watchdog's
    `github-off` switch): a job of one never goes to GitHub, it waits for our pool however long and however
    many wait (2026-11-17, "use only our runners": GitHub's macOS arm64 capacity dropped queued jobs all day
    while WAIT_DEPTH still overflowed the third job of a run). Stale or missing health: none off."""
    # Not `_fresh`: GitHub freezes the variable when the run starts, so a picker later in a long run
    # (a test stage after a 40-min build) sees it 15-60 min old, and the ops switch must still hold
    # (2026-11-17: 33 jobs of a denext.26 gate went to GitHub with the switch on). The switch expires
    # on its own in the watchdog; a value older than GITHUB_OFF_MAX_AGE is ignored.
    try:
        health = json.loads(value or "")
    except (TypeError, ValueError):
        return set()
    if not isinstance(health, dict):
        return set()
    at = health.get("checked_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or now - at > GITHUB_OFF_MAX_AGE:
        return set()
    off = health.get("github_off")
    if not isinstance(off, list):
        return set()
    return {f for f in off if f in GITHUB_FAMILIES}


def _glob(pattern, value):
    """`*` matches anything; the rest literally, case-insensitive (the routing rules' repo / workflow globs)."""
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), value, re.IGNORECASE) is not None


def routing_no_github(value, now, repo, workflow):
    """The runs-on labels whose jobs may NOT go to GitHub-hosted runners, from the fleet's routing rules
    (the health's `routing`: {v: 1, classes: [{labels?, repo?, workflow?, github, order}]}, published by
    the watchdog from routing.json; the user edits it in Fleet). Classes are checked in order and the
    FIRST one that fits a label (and this run's repository and workflow file) decides: a class without
    "github" in its order keeps its jobs on our pools however long they wait, exactly like the ops
    switch. Classes that match on a job's name are not published (a picker cannot know it).

    {label: "<our pools in order>"} for the labels held back. Like `github_off` it holds in a frozen
    value up to GITHUB_OFF_MAX_AGE; no `routing`, a newer version or anything malformed: nothing held."""
    try:
        health = json.loads(value or "")
    except (TypeError, ValueError):
        return {}
    if not isinstance(health, dict):
        return {}
    at = health.get("checked_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or now - at > GITHUB_OFF_MAX_AGE:
        return {}
    routing = health.get("routing")
    if not isinstance(routing, dict) or routing.get("v") != 1 or not isinstance(routing.get("classes"), list):
        return {}
    path = workflow or ""
    out, seen = {}, set()
    for c in routing["classes"]:
        if not isinstance(c, dict) or not isinstance(c.get("github"), bool):
            continue
        r, w, labels = c.get("repo"), c.get("workflow"), c.get("labels")
        if isinstance(r, str) and not _glob(r, repo or ""):
            continue
        if isinstance(w, str) and not (_glob(w, path) or _glob(w, path.rsplit("/", 1)[-1])):
            continue
        if not isinstance(labels, list):
            continue  # a class for every label of a repository / workflow is not expressible per label here
        for label in labels:
            if not isinstance(label, str) or label.lower() in seen:
                continue
            seen.add(label.lower())  # the first class that fits decides
            if not c["github"]:
                order = c.get("order") if isinstance(c.get("order"), list) else []
                out[label.lower()] = " > ".join(str(o) for o in order[:8])
    return out


def pr_labels(value, now):
    """The runs-on labels (lower case) whose pools take a same-repo pull_request, from the health's
    `pull_request_labels` (published by the watchdog from the scheduler's policy: `widened_labels`,
    and `pull_request` among its `events`). The scheduler refuses a pull_request job on any other
    pool, so it would queue there until GitHub drops it (2026-11-19: a same-repo pull request's job
    sat 8 h on denext-windows-x64, "not in widened_labels: its sandbox hook predates same-repo pull
    requests"). The policy changes rarely, so like `github_off` it holds in a value frozen at run
    start up to GITHUB_OFF_MAX_AGE. None (unknown: every pool counts as refusing) when the value is
    missing, too old, or has no well-formed list."""
    try:
        health = json.loads(value or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(health, dict):
        return None
    at = health.get("checked_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or now - at > GITHUB_OFF_MAX_AGE:
        return None
    labels = health.get("pull_request_labels")
    if not isinstance(labels, list) or not all(isinstance(l, str) for l in labels):
        return None
    return {l.lower() for l in labels}


def workflow_path(env):
    """This run's workflow file (".github/workflows/ci.yml") from GITHUB_WORKFLOW_REF
    ("owner/repo/.github/workflows/ci.yml@refs/heads/x"), or ""."""
    ref = (env.get("GITHUB_WORKFLOW_REF") or "").split("@", 1)[0]
    parts = ref.split("/", 2)
    return parts[2] if len(parts) == 3 else ""


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


def label_load(label, pools, counts, stalled):
    """(capacity, taken) of one label: capacity 0 when the fleet marks it not ok or paused, does not
    list it, or a previous attempt's job sat on it until cancelled; else its healthy slots and the
    slots taken or promised (`taken_slots`, which may exceed the capacity: the jobs queued past it)."""
    if label in stalled:
        return 0, 0
    entry = pools.get(label)
    if not isinstance(entry, dict) or entry.get("ok") is not True or entry.get("paused") is True:
        return 0, 0
    cap = _n(entry.get("healthy_slots"))
    if cap is None:
        cap = _n(entry.get("slots")) or 0
    return cap, taken_slots(label, entry, counts)


def pool_free(label, pools, counts, stalled):
    """The free slots of one label (`label_load`'s capacity minus its taken slots, at least 0)."""
    cap, taken = label_load(label, pools, counts, stalled)
    return max(0, cap - taken)


def pool_load(pools_in_use, health_pools, variables, counts, stalled):
    """({label: (capacity, taken)}, {group: (capacity, taken)}) for the pools the jobs use, or
    (None, None) while the health is stale or missing (the load cannot be seen).

    Per label as `label_load`. A shared group's capacity is its variable (slots, or GB for a memory
    group), else the largest healthy cap among its labels (GROUP_MEM_GB for a memory group); its
    taken units are the larger of what its labels' busy runners hold and what their running and
    queued jobs want (weighted by GB in a memory group; a pool that is not ok or paused counts
    nothing there). `taken` is never clipped at the capacity: the queue past it is the wait."""
    if health_pools is None:
        return None, None
    labels_out, groups = {}, {}
    for p in pools_in_use:
        labels_out[p.label] = label_load(p.label, health_pools, counts, stalled)
        if p.parent_label and p.parent_label not in labels_out:
            labels_out[p.parent_label] = label_load(p.parent_label, health_pools, counts, stalled)
    group_labels, weight = {}, {}
    for name, (_, label, _, group, _) in POOLS.items():
        if group:
            group_labels.setdefault(group, []).append(label)
            weight[label] = group_weight(name)
    for group in {p.group for p in pools_in_use if p.group}:
        labels = group_labels[group]
        try:
            cap = int(variables.get(GROUP_SLOTS_VAR.get(group, ""), "") or "x")
        except ValueError:
            cap = None
        if group in GROUP_WEIGHT:
            # A memory group: its GB, minus what the labels that can run a job hold or are
            # promised (a pool that is not ok or paused runs none: its queue takes no memory).
            if cap is None:
                cap = GROUP_MEM_GB[group]
            busy = want = 0
            for l in labels:
                e = health_pools.get(l)
                if not isinstance(e, dict) or e.get("ok") is not True or e.get("paused") is True:
                    continue
                busy += (_n(e.get("busy")) or 0) * weight[l]
                running = counts.running.get(l, 0)
                if counts.busy_runners is not None:
                    running = max(running, counts.busy_runners.get(l, 0))
                want += (running + max(_n(e.get("queued")) or 0, counts.queued.get(l, 0))) * weight[l]
            groups[group] = (cap, max(busy, want))
            continue
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
        groups[group] = (cap, max(busy, want))
    return labels_out, groups


def free_slots(pools_in_use, health_pools, variables, counts, stalled):
    """({label: free}, {group: free}) for the pools the jobs use.

    Fresh health: `pool_load`'s capacity minus taken, at least 0. Stale or missing health:
    STALE_FREE per label and per group, minus what this repository's runs already hold there
    (fail open toward speed: one job may wait on a pool we cannot see, the rest go to GitHub)."""
    free, groups = {}, {}
    if health_pools is None:
        for p in pools_in_use:
            held = counts.running.get(p.label, 0) + counts.queued.get(p.label, 0)
            free[p.label] = max(0, STALE_FREE - held) if p.label not in stalled else 0
            if p.group:
                g = groups.get(p.group, _stale_group_free(p.group))
                groups[p.group] = max(0, g - held * group_weight(p.name))
        return free, groups
    labels, group_load = pool_load(pools_in_use, health_pools, variables, counts, stalled)
    for p in pools_in_use:
        cap, taken = labels[p.label]
        free[p.label] = max(0, cap - taken)
    for g, (cap, taken) in group_load.items():
        groups[g] = max(0, cap - taken)
    return free, groups


def expected_wait(cap, taken, need, minutes):
    """Minutes a job needing `need` units (1 slot, or its GB in a memory group) waits for a pool of
    `cap` units of which `taken` are held or promised, when one job holds a slot `minutes` long.

    Room now: 0. Else the jobs ahead drain in waves of `cap` units: the job starts in wave
    ceil((taken + need - cap) / cap), and the first wave frees after half a job on average (we do not
    know how far the running jobs are), so the wait is (waves - 0.5) * minutes: one slot of three,
    all three busy and none queued, waits ~half a job; with three more queued ahead, a job and a
    half. No capacity at all, or a job larger than the whole pool: infinite (never placed on it)."""
    if cap <= 0 or need > cap:
        return float("inf")
    if cap - taken >= need:
        return 0.0
    waves = -(-(taken + need - cap) // cap)
    return (waves - 0.5) * minutes


OVERRUN_FLOOR = 60  # seconds: a job past its ETA is assumed to end soon, not now


def live_entries(health_pools):
    """{label: (busy, [eta, ...] ascending, job_s or None)} for the labels whose FRESH health entry
    carries a well-formed `live` list ([[start_epoch, eta_epoch], ...], one pair per busy slot; `[]`
    when none is busy). `job_s` (the label's median job duration, seconds) is kept when it is a
    positive number. A label without `live` (an older publisher) or with a malformed one is left out:
    its wait comes from the table. Callers pass only a fresh value (never an aged or stale one: the
    ETAs are as old as `checked_at`)."""
    out = {}
    for label, e in (health_pools or {}).items():
        if not isinstance(e, dict) or not isinstance(e.get("live"), list):
            continue
        etas = []
        for pair in e["live"]:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in pair)):
                etas = None
                break
            etas.append(float(pair[1]))
        if etas is None:
            continue
        job_s = e.get("job_s")
        job_s = float(job_s) if not isinstance(job_s, bool) and isinstance(job_s, (int, float)) and job_s > 0 else None
        out[label] = (_n(e.get("busy")) or 0, sorted(etas), job_s)
    return out


def live_wait(cap, taken, busy, etas, job_s, minutes, now, need=1):
    """Minutes one more job waits for a label of `cap` slots of which `taken` are held or promised
    (busy, running, queued, and this picker's earlier placements), from the fleet's live per-slot ETAs.

    The slots' free times (seconds from `now`): a free slot at 0; a busy slot with an ETA at
    max(eta - now, OVERRUN_FLOOR) (a job past its ETA ends soon, not now); a busy slot the fleet gave
    no ETA (`busy` > len(etas)) at half a job. A job is `job_s` seconds when the label has a median,
    else the table's `minutes`. The jobs ahead (`taken` beyond the busy slots: queued, and placed by
    this picker) each take the earliest free slot and hold it a job long; this job's wait is the
    earliest free time left. Busy slots past `cap` (a slot no longer healthy finishing its job) are
    the latest ETAs and are dropped: that slot takes no new job. No capacity, or `need` above it:
    infinite."""
    if cap <= 0 or need > cap:
        return float("inf")
    per_job = job_s if job_s else minutes * 60
    busy_n = max(busy, len(etas))
    times = sorted([max(eta - now, OVERRUN_FLOOR) for eta in etas] + [per_job / 2] * (busy_n - len(etas)))
    times = ([0.0] * max(0, cap - busy_n) + times)[:cap]
    for _ in range(max(0, taken - busy_n)):
        t = min(times)
        times.remove(t)
        times.append(t + per_job)
    return min(times) / 60


def max_wait_thresholds(text):
    """PICK_MAX_WAIT (`linux=10 windows=15 macos=20`, any subset, minutes >= 0) over DEFAULT_MAX_WAIT.
    Returns ({family name: minutes}, [the entries it ignored])."""
    out, bad = dict(DEFAULT_MAX_WAIT), []
    for item in (text or "").replace(",", " ").split():
        k, _, v = item.partition("=")
        try:
            n = float(v)
        except ValueError:
            n = -1
        if k in out and n >= 0:
            out[k] = n
        else:
            bad.append(item)
    return out, bad


def _fmt_min(m):
    return "inf" if m == float("inf") else f"{m:.0f}" if m == int(m) else f"{m:.1f}"


# ---- the decision ------------------------------------------------------------


def allocate(jobs, pools, free, groups, macos_cap, fork_pr, notes=None, saturated=None, mem=None, off=None,
             load=None, max_wait=None, records=None, live=None, now=None, no_github=None, pr_refused=None):
    """({key: runs-on}, [decision rows]). Pinned jobs first (they take their slots whatever is
    free), then the rest in priority order:

    - a job that needs more memory than its pool holds takes its image;
    - with the pools' load known (`load`, from `pool_load`: fresh or aged health), a job goes to our
      pool unless its EXPECTED WAIT there (`job_wait`) exceeds its family's threshold (`max_wait`,
      {linux|windows|macos: minutes}; DEFAULT_MAX_WAIT). Every job placed (free slot or waiting)
      joins the queue the next job is measured against, so a burst of one run spreads over our
      slots and only the jobs past the threshold overflow. An unhealthy, paused, unlisted or stalled
      pool (`notes`) waits forever: GitHub;
    - without the load (stale health), our pool only while it and its group have a free slot
      (`free` / `groups`), else the image (fail open toward speed, as before);
    - a same-repo pull request's job whose pool does not take pull requests (`pr_refused`, {lower-case
      label: why}; see `pr_labels`) takes its image, pinned or not, routing rules or not: the
      scheduler would never start it on our pool;
    - a job whose label the routing rules keep off GitHub (`no_github`, {label: our pools in
      order}; `routing_no_github`) always takes our pool: no threshold, no depth limit, even when
      the pool is unhealthy or the job may not fit (the user said "not at all");
    - past the threshold (or with no free slot), a job of a family ops turned off (`off`) waits
      for our pool anyway; a macOS job past the macOS cap, or a job whose image family is
      saturated on GitHub (`saturated`), waits too, but only for a pool that is merely busy (no
      `notes` entry), at most WAIT_DEPTH per pool.

    `mem` is {pool name: GB} (`mem_capacity`; POOL_MEM_GB for a pool it leaves out). Followers copy
    their target. Each row's `why` names the pool, the expected wait and the threshold. `records`, a
    dict when given, gets {key: {pool, label, wait (minutes, None when not measured), source ("live":
    the fleet's per-slot ETAs, "table": POOL_JOB_MINUTES; None when not measured), threshold, eligible
    (our pool in use, healthy, the job fits: GitHub only past the threshold)}} for every unpinned job
    (the tripwire's input; see `tripwires`).

    `live` ({label: (busy, etas, job_s)}, `live_entries` of a FRESH health value) and `now` (epoch)
    measure a label's (and a parent label's) wait from the live ETAs (`live_wait`); a label it leaves
    out, and every host group, use the table (`expected_wait`)."""
    free = dict(free)
    groups = dict(groups)
    notes, saturated = notes or {}, saturated or {}
    pr_refused = pr_refused or {}
    thresholds = dict(DEFAULT_MAX_WAIT, **(max_wait or {}))
    label_load_, group_load_ = ({}, {}) if load is None else ({k: list(v) for k, v in load[0].items()},
                                                             {k: list(v) for k, v in load[1].items()})
    plan, rows = {}, {}

    def mem_of(p):
        return (mem or {}).get(p.name) or POOL_MEM_GB[p.name]

    def group_minutes(group):
        return max(POOL_JOB_MINUTES[n] for n in POOLS if POOLS[n][3] == group)

    live_ = live or {}

    def label_wait(label, minutes):
        """(minutes, description, source, minutes per job) of one more job on one label."""
        cap, taken = label_load_.get(label, (0, 0))
        desc = f"{label} {taken}/{cap} slots taken"
        if label in live_ and now is not None:
            busy, etas, job_s = live_[label]
            per = job_s / 60 if job_s else minutes
            return live_wait(cap, taken, busy, etas, job_s, minutes, now), desc, "live", per
        return expected_wait(cap, taken, 1, minutes), desc, "table", minutes

    def job_wait(p):
        """(minutes, what binds it: a short description, "live" | "table", minutes per job) for one
        more job on `p`."""
        minutes = POOL_JOB_MINUTES[p.name]
        worst = label_wait(p.label, minutes)
        if p.parent_label:
            # the table path keeps measuring the parent with this pool's minutes, as before
            w = label_wait(p.parent_label, minutes)
            if w[0] > worst[0]:
                worst = w
        if p.group and p.group in group_load_:
            cap, taken = group_load_[p.group]
            unit = "GB" if p.group in GROUP_WEIGHT else "slots"
            gm = group_minutes(p.group)
            w = expected_wait(cap, taken, group_weight(p.name), gm)
            if w > worst[0]:
                worst = (w, f"the {p.group} pools {taken}/{cap} {unit} taken", "table", gm)
        return worst

    def take(p):
        free[p.label] = free.get(p.label, 0) - 1
        if p.parent_label in free:
            free[p.parent_label] -= 1
        for other in pools.values():  # a sub-pool's slots are its parent's
            if other.parent_label == p.label and other.label in free:
                free[other.label] = min(free[other.label], free[p.label])
        if p.group:
            groups[p.group] = groups.get(p.group, 0) - group_weight(p.name)
        for l in (p.label, p.parent_label):
            if l in label_load_:
                label_load_[l][1] += 1
        if p.group in group_load_:
            group_load_[p.group][1] += group_weight(p.name)

    for job in jobs:
        if job["follow"] or not job["pinned"]:
            continue
        p = pools[job["pool"]]
        if fork_pr:
            plan[job["key"]] = job["image"] or p.image
            rows[job["key"]] = (p.name, "GitHub", "a fork's pull request never runs on our runners")
            continue
        if p.label.lower() in pr_refused:
            plan[job["key"]] = job["image"] or p.image
            rows[job["key"]] = (p.name, "GitHub", pr_refused[p.label.lower()] + " (pinned: may not fit the image)")
            continue
        take(p)
        plan[job["key"]] = p.pinned_runs_on
        why = "needs our hardware"
        if job["mem"] and job["mem"] > mem_of(p):
            why += f"; needs {job['mem']} GB, {p.label} holds {mem_of(p)} (may not fit)"
        rows[job["key"]] = (p.name, "ours (pinned)", why)
    hosted_macos = 0
    waits = {}  # label -> jobs of this run that wait for it past the threshold (at most WAIT_DEPTH)
    for job in jobs:
        if job["follow"] or job["pinned"]:
            continue
        p = pools[job["pool"]]
        image = job["image"] or p.image
        key = job["key"]
        family = image_family(image)
        fname = FAMILY_NAME.get(family or image_family(p.image), "linux")
        limit = thresholds.get(fname, 0)
        if fork_pr:
            plan[key], rows[key] = image, (p.name, "GitHub", "a fork's pull request never runs on our runners")
            continue
        if p.mode == "off":
            plan[key], rows[key] = image, (p.name, "GitHub", f"{POOLS[p.name][0]} is unset")
            continue
        if p.mode == "image":
            # the job's own @image wins (a leg keeps the image its build ran on)
            plan[key], rows[key] = job["image"] or p.runs_on, (p.name, "GitHub", f"{POOLS[p.name][0]} names a GitHub image")
            continue
        if p.label.lower() in pr_refused:
            # the scheduler refuses a pull_request job on this pool: it would never start there
            plan[key], rows[key] = image, (p.name, "GitHub", pr_refused[p.label.lower()])
            continue
        if p.label.lower() in (no_github or {}):
            # the routing rules: this class never goes to GitHub, whatever the wait
            take(p)
            order = (no_github or {})[p.label.lower()]
            why = f"routing rules: GitHub is not allowed for {p.label}" + (f" (our pools, in order: {order})" if order else "")
            if notes.get(p.label):
                why += f"; {notes[p.label]}"
            if job["mem"] and job["mem"] > mem_of(p):
                why += f"; needs {job['mem']} GB, {p.label} holds {mem_of(p)} (may not fit)"
            plan[key], rows[key] = p.runs_on, (p.name, "ours (routing)", why)
            continue
        if job["mem"] and job["mem"] > mem_of(p):
            # our pool cannot hold the job (the linux-arm64 OOM): GitHub's image whatever is free
            if p.macos:
                hosted_macos += 1
            plan[key], rows[key] = image, (p.name, "GitHub", f"needs {job['mem']} GB, {p.label} holds {mem_of(p)}")
            continue
        if records is not None:
            records[key] = {"pool": p.name, "label": p.label, "wait": None, "source": None, "threshold": limit,
                            "eligible": load is not None and not notes.get(p.label)}
        if load is not None:
            if notes.get(p.label):
                wait, binding, source, per = float("inf"), notes[p.label], None, POOL_JOB_MINUTES[p.name]
            else:
                wait, binding, source, per = job_wait(p)
            if records is not None and wait != float("inf"):
                records[key]["wait"] = round(wait, 1)
                records[key]["source"] = source
            room = wait == 0
            within = wait <= limit
            how = f"{source}, " if source else ""
            stat = (f"expected wait {_fmt_min(round(wait, 1))} min ({binding}; {how}~{_fmt_min(round(per, 1))} min/job),"
                    f" threshold {_fmt_min(limit)} min")
        else:
            room = free.get(p.label, 0) > 0 and (not p.group or groups.get(p.group, 0) >= group_weight(p.name)) and (
                p.parent_label not in free or free[p.parent_label] > 0)
            within = room
            stat = "health stale: free slot only" + ("" if room else ", none free")
        if room:
            take(p)
            plan[key], rows[key] = p.runs_on, (p.name, "ours", f"free slot on {p.label}; {stat}")
        elif within:
            take(p)
            plan[key], rows[key] = p.runs_on, (p.name, "ours (waits)", f"{stat}: within it, ours")
        elif family in (off or set()):
            # ops turned GitHub's family off: wait for our pool, no depth limit, never GitHub
            take(p)
            plan[key], rows[key] = p.runs_on, (p.name, "ours (waits)", f"GitHub {family} is off (ops switch); {stat}")
        elif p.macos and hosted_macos >= macos_cap and not notes.get(p.label) and waits.get(p.label, 0) < WAIT_DEPTH:
            waits[p.label] = waits.get(p.label, 0) + 1
            take(p)
            # GitHub's macOS share is spent: wait for our pool, but only for a pool that is
            # merely busy (never one that is unhealthy, paused, unlisted or stalled).
            plan[key], rows[key] = p.runs_on, (
                p.name,
                "ours (waits)",
                f"{stat}, but this run already sent {hosted_macos} macOS job(s) to GitHub (cap {macos_cap})",
            )
        elif family in saturated and not notes.get(p.label) and waits.get(p.label, 0) < WAIT_DEPTH:
            waits[p.label] = waits.get(p.label, 0) + 1
            take(p)
            # GitHub has had a job of this family queued for minutes: our busy pool frees sooner.
            # Not counted against the macOS cap (the job does not go to GitHub).
            plan[key], rows[key] = p.runs_on, (
                p.name,
                "ours (waits)",
                f"{stat}, but GitHub's {family} images have had a job queued for"
                f" {saturated[family] // 60} min (saturated)",
            )
        else:
            if p.macos:
                hosted_macos += 1
            if notes.get(p.label):
                why = notes[p.label]
            elif load is not None:
                why = f"{stat}: exceeds it, GitHub"
            else:
                why = (f"no free slot on {p.label}" if free.get(p.label, 0) <= 0 else f"the {p.group} pools are full") + f" ({stat})"
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


def tripwires(plan, records):
    """The jobs placed on a GitHub image although our pool for them was healthy and their expected
    wait there was within the threshold: [{key, pool, label, wait, threshold}]. The decision rules
    never do that (a job within the threshold always takes our pool); this is the independent check
    that they still don't, so a placement like the 4.20.1 gate's (coverage shards on ubuntu-latest
    beside an idle pool) is an error in the log, never silent."""
    out = []
    for key, r in (records or {}).items():
        runs_on = plan.get(key)
        if not (isinstance(runs_on, str) and GITHUB_IMAGE.match(runs_on)):
            continue
        if r.get("eligible") and r.get("wait") is not None and r["wait"] <= r["threshold"]:
            out.append({"key": key, "pool": r["pool"], "label": r["label"], "wait": r["wait"],
                        "threshold": r["threshold"]})
    return out


def fallback_plan(jobs, pools, fork_pr, pr_refused=None):
    """Every job on its image, the pinned ones on our pool (any error) unless that pool refuses
    this run's pull request (`pr_refused`)."""
    plan = {}
    for job in jobs:
        if job["follow"]:
            continue
        p = pools[job["pool"]]
        ours_ok = not fork_pr and p.label.lower() not in (pr_refused or {})
        plan[job["key"]] = (
            p.pinned_runs_on if job["pinned"] and ours_ok else (job["image"] or p.image)
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


def same_repo_pr_refused(env, pools, value, now, fork_pr):
    """{lower-case label: why} of the pools that must not get this run's jobs because it is a same-repo
    pull_request and the fleet does not hand those pools one (`pr_labels`); {} for any other event (a
    fork's pull request has its own rule)."""
    if fork_pr or env.get("GITHUB_EVENT_NAME") != "pull_request":
        return {}
    accepted = pr_labels(value, now)
    out = {}
    for p in pools.values():
        label = p.label.lower()
        if accepted is None:
            out[label] = ("a same-repo pull request: DENEXT_POOL_HEALTH does not say which pools take one"
                          " (no pull_request_labels)")
        elif label not in accepted:
            out[label] = f"a same-repo pull request: {p.label} does not take pull requests (not in pull_request_labels)"
    return out


# Side output of the latest pick() (kept out of its return value): `decisions` {key: record} (see
# `allocate`'s `records`, with the pool's numbers at pick time) and `tripwire` (`tripwires`, each with
# the run and those numbers, plus any job the picker-error fallback sent to GitHub from a pool in use).
LAST = {"blocked": [], "saturated": {}, "decisions": {}, "tripwire": []}


def pool_numbers(label, health, counts):
    """The pool's state at pick time, for the decision log and the tripwire: the fleet's slots,
    healthy_slots, busy and queued for `label` (None when not published) and this picker's own
    counts (this repository's running / queued jobs, the org's busy runners)."""
    e = (health or {}).get(label)
    e = e if isinstance(e, dict) else {}
    out = {k: _n(e.get(k)) for k in ("slots", "healthy_slots", "busy", "queued")}
    out["ok"] = e.get("ok") is True and e.get("paused") is not True
    if counts is not None:
        out["repo_running"] = counts.running.get(label, 0)
        out["repo_queued"] = counts.queued.get(label, 0)
        if counts.busy_runners is not None:
            out["busy_runners"] = counts.busy_runners.get(label, 0)
    return out


def pick(env, get=None, runner_get=None, warn=lambda m: None, now=None, sleep=time.sleep,
         clock=time.monotonic):
    """(plan, rows, free) for the environment `env` (see the module docstring)."""
    LAST["blocked"], LAST["saturated"], LAST["decisions"], LAST["tripwire"] = [], {}, {}, []
    run_ref = {"repo": env.get("GITHUB_REPOSITORY", ""), "run_id": env.get("GITHUB_RUN_ID", ""),
               "attempt": env.get("GITHUB_RUN_ATTEMPT", "1")}
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
    max_wait, bad_wait = max_wait_thresholds(env.get("PICK_MAX_WAIT"))
    for entry in bad_wait:
        warn(f"pick-runners: ignored the malformed PICK_MAX_WAIT entry {entry!r}")
    pr_refused = same_repo_pr_refused(env, pools, variables.get("DENEXT_POOL_HEALTH"), now, fork_pr)
    refused_labels = sorted({p.label for p in pools.values() if p.label.lower() in pr_refused})
    if any(j["pool"] and pools[j["pool"]].label.lower() in pr_refused
           and (j["pinned"] or pools[j["pool"]].mode == "ours") for j in jobs):
        warn(f"pick-runners: {next(iter(pr_refused.values()))}: the jobs for {', '.join(refused_labels)}"
             " take their GitHub image")
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
        plan, rows = allocate(jobs, pools, {}, {}, macos_cap, fork_pr, mem=mem, pr_refused=pr_refused)
        LAST["blocked"] = refused_labels
        return plan, rows, {}
    try:
        repo, run_id = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
        aged = False
        if health is None and get is not None:
            try:
                started = run_started(get, repo, run_id)
            except Exception:  # advisory: the stale path below
                started = None
            health = aged_health(variables.get("DENEXT_POOL_HEALTH"), now, started)
            if health is not None:
                aged = True
                mem = {name: mem_capacity(p, health) for name, p in pools.items()}
                at = json.loads(variables["DENEXT_POOL_HEALTH"])["checked_at"]
                warn(f"pick-runners: DENEXT_POOL_HEALTH is {int(now - at) // 60} min old (GitHub fixes vars"
                     f" when the run starts; this picker runs {int(now - started) // 60} min later):"
                     " using its pool states, with the jobs counted here")
        if health is None:
            warn("pick-runners: DENEXT_POOL_HEALTH is missing, unparsable or older than 10 min:"
                 f" at most {STALE_FREE} job per pool goes to our runners")
        groups_in_use = {p.group for p in in_use if p.group}
        labels = sorted({p.label for p in in_use} |
                        {POOLS[n][1] for n in POOLS if POOLS[n][3] in groups_in_use})
        counts, stalled = Counts(), set()
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
        if aged and counts.busy_runners is not None:
            # The org's busy runners are counted live: the published busy / queued are as old as the value.
            health = {l: ({k: v for k, v in e.items() if k not in ("busy", "queued")} if isinstance(e, dict) else e)
                      for l, e in health.items()}
        free, groups = free_slots(in_use, health, variables, counts, stalled)
        load = pool_load(in_use, health, variables, counts, stalled)
        notes = {p.label: pool_note(p.label, health, stalled) for p in in_use}
        records = {}
        # The live per-slot ETAs count only in a FRESH value: an aged one's are as old as the run.
        live = live_entries(health) if health is not None and not aged else None
        plan, rows = allocate(jobs, pools, free, groups, macos_cap, False, notes, saturated, mem,
                              github_off(variables.get("DENEXT_POOL_HEALTH"), now),
                              None if load[0] is None else load, max_wait, records, live, now,
                              routing_no_github(variables.get("DENEXT_POOL_HEALTH"), now, repo, workflow_path(env)),
                              pr_refused)
        LAST["blocked"] = sorted({l for l, n in notes.items() if n} | set(refused_labels))
        for key, r in records.items():
            r["runs_on"] = plan[key]
            r["numbers"] = pool_numbers(r["label"], health, counts)
        LAST["decisions"] = records
        LAST["tripwire"] = [dict(t, **run_ref, numbers=records[t["key"]]["numbers"])
                            for t in tripwires(plan, records)]
        return plan, rows, free
    except Exception as e:  # never fail the run
        warn(f"pick-runners: {e}; every job but the pinned ones takes its GitHub image")
        plan = fallback_plan(jobs, pools, False, pr_refused)
        # Loud, not silent: each job this sends to GitHub from a pool in use is a tripwire (the wait
        # was never measured, so the pool may well have been idle).
        LAST["tripwire"] = [
            dict(run_ref, key=j["key"], pool=j["pool"], label=pools[j["pool"]].label, wait=None,
                 threshold=max_wait.get(FAMILY_NAME.get(image_family(pools[j["pool"]].image), "linux")),
                 reason=f"picker error: {e}"[:200], numbers=pool_numbers(pools[j["pool"]].label, health, None))
            for j in jobs
            if not j["follow"] and not j["pinned"] and pools[j["pool"]].mode == "ours"
            and pools[j["pool"]].label.lower() not in pr_refused
            and isinstance(plan.get(j["key"]), str) and GITHUB_IMAGE.match(plan[j["key"]])
        ]
        return plan, [(k, "?", "GitHub", "picker error", v) for k, v in plan.items()], {}


def table(rows, free, saturated=None, tripwire=None):
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
    if tripwire:
        lines.append("")
        lines.append("**TRIPWIRE: on GitHub although our pool was healthy within the threshold:** " + ", ".join(
            f"{t['key']} ({t['label']}, wait {'unmeasured' if t.get('wait') is None else t['wait']} min)"
            for t in tripwire))
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
    print(f"tripwire={_dumps(LAST['tripwire'])}")
    # One machine-readable line per job (a log scraper or the watchdog greps `pick-runners decision `).
    for key, r in LAST["decisions"].items():
        print(f"pick-runners decision {_dumps(dict(r, key=key))}", file=sys.stderr)
    for t in LAST["tripwire"]:
        n = {k: ("?" if v is None else v) for k, v in (t.get("numbers") or {}).items()}
        wait = "unmeasured" if t.get("wait") is None else f"{t['wait']} min"
        print(f"::error title=pick-runners tripwire::{t['repo']} run {t['run_id']} (attempt {t['attempt']}) job"
              f" {t['key']} went to GitHub while {t['label']} was healthy with expected wait {wait}"
              f" (threshold {t['threshold']} min; slots {n.get('healthy_slots', '?')}, busy"
              f" {n.get('busy')}, queued {n.get('queued')})" + (f": {t['reason']}" if t.get("reason") else ""),
              file=sys.stderr)
    print(("DRY RUN (no API call)\n" if dry else "") + table(rows, free, LAST["saturated"], LAST["tripwire"]), file=sys.stderr)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary and not dry:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### Runner picks\n\n" + table(rows, free, LAST["saturated"], LAST["tripwire"]) + "\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
