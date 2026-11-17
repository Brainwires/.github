# pick-runners

Our self-hosted pools are the first choice, never a bottleneck: a job goes to our pool only while
that pool has a free slot for it, and to the GitHub-hosted equivalent at once otherwise.

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
      # also: free, blocked, saturated (see action.yml)
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

For each pool the jobs use, free slots = `healthy_slots − max(busy, running + queued)` from the
org variable `DENEXT_POOL_HEALTH` (the fleet watchdog publishes it about every 2 min), where
`queued` is the larger of the fleet's org-wide count and this repository's other runs' queued jobs
(fresher). `busy` and `running + queued` overlap (a booting runner's job is both), so they are never
added. Then, in the order given: pinned jobs (`!`) take our pool regardless; every other job takes
our pool while it (and, for biscuits' three Linux labels, their shared capacity) has a slot, else
its GitHub image.

- `ok: false`, `paused: true` or a label the value does not list: 0 free.
- GitHub fixes a run's `vars` when the run (attempt) starts, so a picker that runs later (a leg's
  picker after its build, 30 min in) reads an old value however often the fleet publishes. A value
  that was at most 10 min old when the run started and is at most 90 min old now is used for the
  pools' states (ok, paused, slots, `mem_gb`; an unhealthy pool stays blocked), with the jobs counted
  by the picker itself (the org's busy runners with `runner-token`, else the published load as well);
  its `github` queue is ignored. One extra API call (the run's `run_started_at`), only then.
- The value missing, unparsable, already stale when the run started, or older than 90 min: at most
  1 job per pool (fail open toward speed).
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
- macOS: at most `macos-cap` (default 3) jobs per run go to GitHub (its ~5 org-wide macOS slots);
  the rest wait for our pool.
- Re-runs: a pool whose job the previous attempt cancelled before any runner took it gets 0 free;
  a GitHub macOS job that went the same way sets the macOS cap to 0.
- GitHub saturated: when the health's optional `github.<family>` (`macos`, `windows`, `ubuntu`:
  the org's jobs queued on GitHub's images of that family, `{"queued": n, "oldest_at": epoch}`)
  shows a job queued for at least 3 min, a job that would overflow to an image of that family
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
- The pool variables (`RUNNER_WINDOWS_X64`, …): unset → the GitHub image; a GitHub image
  (`windows-latest`) → that image (a job's own `@image` wins); our label (`["self-hosted","denext-windows-x64"]`) → our pool
  with overflow.

Pools: `linux-x64`, `linux-x64-2204`, `linux-x64-docker` (biscuits, shared capacity),
`windows-x64`, `windows-x64-fast` (long jobs: only the fast Windows host, label
`denext-windows-x64-fast`; it shares `windows-x64`'s slots, and when that host is busy the job
goes to GitHub, never to the slow host), `windows-arm64`, `macos-arm64`, `macos-x64`, `linux-arm64` (Linux arm64
containers on the Mac mini, variable `RUNNER_LINUX_ARM64`, label `denext-linux-arm64`; overflow `ubuntu-24.04-arm`, so a job
that needs GitHub's 22.04 ARM image on overflow writes `key=linux-arm64@ubuntu-22.04-arm`), `linux-arm64-desktop` (the Pi; pinned
jobs only). See `pick_runners.py` for the full rules; tests: `python3 -I pick_runners_test.py`.

Dry run (no API call), e.g. against the live value:

```sh
PICK_DRY_RUN=true PICK_JOBS="a=windows-x64 b=windows-x64" \
PICK_VARS="$(jq -n --arg h "$(gh variable get DENEXT_POOL_HEALTH --org Brainwires)" \
  '{DENEXT_POOL_HEALTH: $h, RUNNER_WINDOWS_X64: "[\"self-hosted\",\"denext-windows-x64\"]"}')" \
python3 -I actions/pick-runners/pick_runners.py
```
