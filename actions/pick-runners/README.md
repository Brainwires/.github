# pick-runners

Our self-hosted pools first, GitHub-hosted runners only as the backup: a job goes to our pool unless
its expected wait there exceeds its family's threshold (Linux 10 min, Windows 15, macOS 20).

```yaml
jobs:
  runners:
    runs-on: ubuntu-latest # GitHub's image: the pick must not wait in the queue it shortens
    timeout-minutes: 5
    permissions:
      actions: read # the repository's queued / running jobs
    outputs:
      plan: ${{ steps.pick.outputs.plan }}
      hosted: ${{ steps.pick.outputs.hosted }}
      # also: free, blocked, saturated, tripwire (see action.yml)
    steps:
      - id: pick
        uses: Brainwires/.github/actions/pick-runners@<full commit sha>
        with:
          jobs: build=linux-x64 test=windows-x64 mac=macos-arm64 gui=linux-arm64-desktop!
          vars: ${{ toJSON(vars) }}

  build:
    needs: runners
    if: ${{ !cancelled() }}
    runs-on: ${{ fromJSON(needs.runners.outputs.plan || '{}').build || 'ubuntu-latest' }}
```

Each job's fallback after `||` is its GitHub image: if the picker job fails, the run still goes.

## The rule

For each pool the jobs use, taken slots = `max(busy, running + queued)` out of `healthy_slots`, from
the org variable `DENEXT_POOL_HEALTH` (the fleet watchdog publishes it about every 2 min), where
`queued` is the larger of the fleet's org-wide count and this repository's other runs' queued jobs
(fresher). `busy` and `running + queued` overlap (a booting runner's job is both), so they are never
added. Then, in the order given: pinned jobs (`!`) take our pool regardless; every other job takes
our pool unless its **expected wait** there exceeds its family's threshold, else its GitHub image.

**Expected wait.** With `cap` slots of which `taken` are held or queued (this run's own earlier
placements included: each job placed joins the queue the next is measured against), a job that
finds no room starts in wave `ceil((taken + 1 − cap) / cap)`, and waits `(waves − 0.5) × minutes`:
the first wave frees after half a job on average, since the picker cannot see how far the running
jobs are. The binding constraint is the worst of the job's label, its parent label (the fast
Windows label's slot is one of `denext-windows-x64`'s) and its host group (the Linux host's three
shared slots; the Mac host's 16 GB, a job weighing its pool's GB). `minutes` is a per-label estimate
of how long one job holds a slot (`POOL_JOB_MINUTES`, deliberately conservative, from the fleet's
7-day history): linux-x64 / -2204 / -docker 10, windows-x64 20, windows-x64-fast 60 (long jobs only),
windows-arm64 30, macos-arm64 20, macos-x64 30, linux-arm64 30, linux-arm64-desktop 15; a host group
uses its slowest pool's. Example (the 4.20.1 gate, 12 Linux jobs, the host's 3 slots idle): 3 start,
3 wait ~5 min, the other 6 would wait 15+ min and take `ubuntu-latest`.

**Live ETAs.** When the health value is fresh (at most 10 min old) and a label's entry carries
`live`, the label's wait comes from the fleet's per-slot ETAs instead of the table:

- `live`: `[[start_epoch, eta_epoch], ...]`, one pair per busy slot of the label (merged across the
  pools that share it), sorted by eta; `eta` is when the slot is expected to free (its start plus the
  job's own median duration by name, else the label's). `[]` when no slot is busy.
- `job_s`: the label's rolling median job duration in seconds (launch to slot free), for the jobs
  queued ahead that have no ETA yet. Optional.

Each free slot frees at 0, each busy slot with an ETA at `max(eta − now, 60 s)` (a job past its ETA
ends soon, not now), each busy slot beyond `len(live)` at half a job. Every job ahead (queued on the
label, and this run's own earlier placements) takes the earliest free time and holds the slot one job
(`job_s`, else the table's minutes); the job's wait is the earliest time left. The same applies to the
parent label. The host groups (the Linux host's shared slots, the Mac host's memory) keep the table.
An aged or stale value, or a label without `live` (an older publisher, or a malformed list), uses the
table rule above, unchanged. The decision's `why` says which (`live, ~N min/job` or
`table, ~N min/job`) and the `pick-runners decision` line carries `"source": "live" | "table"`.

**Thresholds** (`max-wait`: any subset of `linux=10 windows=15 macos=20`, the defaults; `0` restores
the old free-slot-or-GitHub rule). A family ops turned off (`github_off`) never goes to GitHub.

**Each decision is logged**: the table's `why` names the pool, the binding constraint, the expected
wait, the threshold and the verdict (`ours`, `ours (waits)`, `GitHub`), and one
`pick-runners decision {...}` JSON line per job carries the pool's slots / busy / queued at pick time.

**Tripwire.** A job placed on GitHub while its pool was healthy (in use, ok, not paused) with an
expected wait within the threshold, or by the picker-error fallback, is an
`::error title=pick-runners tripwire::` annotation naming the run, the job, the pool's slots / busy /
queued and the wait, a `TRIPWIRE` line in the step summary, and an entry of the `tripwire` output.
The rule never does that; the check is independent of it, so a regression is loud.

**Label equivalence is not assumed.** `denext-linux-x64` and `denext-linux-x64-2204` are the SAME
three job containers on one host with another image (Ubuntu 24.04 with Chrome and the Android
platform, vs 22.04 for jobs pinned to the 22.04 glibc floor), and `denext-linux-x64-docker` is one of
those three with a Docker daemon. An idle `-2204` label is therefore no free capacity for a
`linux-x64` job (the shared group bounds both), and a job never moves to another label's image.

- `ok: false`, `paused: true` or a label the value does not list: 0 free.
- GitHub fixes a run's `vars` when the run (attempt) starts, so a picker that runs later (a leg's
  picker after its build, 30 min in) reads an old value however often the fleet publishes. A value
  that was at most 10 min old when the run started and is at most 90 min old now is used for the
  pools' states (ok, paused, slots, `mem_gb`; an unhealthy pool stays blocked), with the jobs counted
  by the picker itself (the org's busy runners with `runner-token`, else the published load as well);
  its `github` queue is ignored. One extra API call (the run's `run_started_at`), only then.
- The value missing, unparsable, already stale when the run started, or older than 90 min: at most
  1 job per pool (fail open toward speed: the load, so the wait, cannot be seen).
- Same-second siblings: workflows dispatched together (a release gate, a fork train's tests and
  runtime runs) would each count the pools before the other has queued its jobs. A run waits
  (polling every 5 s, at most `settle`, default 90 s) while an EARLIER run of the same repository,
  created up to 2 min before it, is still starting or still has a picker job queued or running
  (`pick … runners`, `<leg> / pick … runners`, or `<job>: pick … runners` for a job that also
  picks); it counts only after 15 s with no sibling picking (which bridges a sibling between two
  pickers: the jobs API lists a job only once its `needs` are done). The later run always yields, so two pickers never wait
  for each other. Across repositories the fleet's `queued` count (published every ~1-5 min) is
  the only signal.
- Fork pull requests (and any `pull_request_*` but `pull_request` itself): GitHub images only,
  pinned jobs included, no API call.
- macOS: of the jobs past the threshold, at most `macos-cap` (default 3) per run go to GitHub (its
  ~5 org-wide macOS slots); the rest wait for our pool.
- Re-runs: a pool whose job the previous attempt cancelled before any runner took it gets 0 free;
  a GitHub macOS job that went the same way sets the macOS cap to 0.
- GitHub saturated: when the health's optional `github.<family>` (`macos`, `windows`, `ubuntu`:
  the org's jobs queued on GitHub's images of that family, `{"queued": n, "oldest_at": epoch}`)
  shows a job queued for at least 3 min, a job past the threshold that would overflow to an image of that family
  waits for our pool instead (`ours (waits)`), but only when the pool is merely busy (never an
  unhealthy, paused, unlisted or stalled one). It does not count against the macOS cap. Stale
  health or no `github` key: ignored. The step summary lists the saturated families, and the
  `saturated` output is them as a JSON array.
- Memory: an entry may state its job's memory need after the image and before `!`:
  `build=linux-arm64@ubuntu-22.04-arm+16g`, `t=macos-arm64+12g!` (1-999 GB). A job that needs more
  than its pool holds takes its image whatever is free (`needs 16 GB, denext-linux-arm64 holds 8`);
  a pinned one stays pinned with a warning. A pool holds its health entry's optional `mem_gb` (the
  most one job on the label can use, swap included) when the health is fresh, else a default:
  linux-x64 / -2204 / -docker 7, windows-x64 / -fast / windows-arm64 8, macos-arm64 8, macos-x64 16,
  linux-arm64 8, linux-arm64-desktop 10.
- The Mac host's memory: `macos-arm64`, `windows-arm64` and `linux-arm64` have their own slots but
  share one RAM budget (16 GB; the variable `RUNNER_MINI_MEM_GB` overrides it). A job takes 8, 8 and
  6 GB of it, counted like slots (the busy runners, the running and the queued jobs of the three
  labels; a pool that is not ok or paused counts nothing), and the host's memory is one of the
  wait's constraints: two macOS VMs running, a Linux arm64 job's wait is half the host's slowest job
  (~15 min, past Linux's 10: GitHub's arm64 image), a macOS job's the same (within macOS's 20: it
  waits). Stale health: one job for the whole host.
- The pool variables (`RUNNER_WINDOWS_X64`, …): unset → the GitHub image; a GitHub image
  (`windows-latest`) → that image (a job's own `@image` wins); our label (`["self-hosted","denext-windows-x64"]`) → our pool
  with overflow.

Pools: `linux-x64`, `linux-x64-2204`, `linux-x64-docker` (one Linux host, shared capacity),
`windows-x64`, `windows-x64-fast` (long jobs: only the fast Windows host, label
`denext-windows-x64-fast`; it shares `windows-x64`'s slots, and when that host's wait is past the
threshold the job goes to GitHub, never to the slow host), `windows-arm64`, `macos-arm64`, `macos-x64`, `linux-arm64` (Linux arm64
containers on the Mac host, variable `RUNNER_LINUX_ARM64`, label `denext-linux-arm64`; overflow `ubuntu-24.04-arm`, so a job
that needs GitHub's 22.04 ARM image on overflow writes `key=linux-arm64@ubuntu-22.04-arm`), `linux-arm64-desktop` (the Pi; pinned
jobs only). See `pick_runners.py` for the full rules; tests: `python3 -I pick_runners_test.py`.

Dry run (no API call), e.g. against the live value:

```sh
PICK_DRY_RUN=true PICK_JOBS="a=windows-x64 b=windows-x64" \
PICK_VARS="$(jq -n --arg h "$(gh variable get DENEXT_POOL_HEALTH --org Brainwires)" \
  '{DENEXT_POOL_HEALTH: $h, RUNNER_WINDOWS_X64: "[\"self-hosted\",\"denext-windows-x64\"]"}')" \
python3 -I actions/pick-runners/pick_runners.py
```
