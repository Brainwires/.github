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
- The value missing, unparsable or older than 10 min: at most 1 job per pool (fail open toward speed).
- Fork pull requests (and any `pull_request_*` but `pull_request` itself): GitHub images only,
  pinned jobs included, no API call.
- macOS: at most `macos-cap` (default 3) jobs per run go to GitHub (its ~5 org-wide macOS slots);
  the rest wait for our pool.
- Re-runs: a pool whose job the previous attempt cancelled before any runner took it gets 0 free;
  a GitHub macOS job that went the same way sets the macOS cap to 0.
- The pool variables (`RUNNER_WINDOWS_X64`, …): unset → the GitHub image; a GitHub image
  (`windows-latest`) → that image (a job's own `@image` wins); our label (`["self-hosted","denext-windows-x64"]`) → our pool
  with overflow.

Pools: `linux-x64`, `linux-x64-2204`, `linux-x64-docker` (biscuits, shared capacity),
`windows-x64`, `windows-arm64`, `macos-arm64`, `macos-x64`, `linux-arm64-desktop` (the Pi; pinned
jobs only). See `pick_runners.py` for the full rules; tests: `python3 -I pick_runners_test.py`.

Dry run (no API call), e.g. against the live value:

```sh
PICK_DRY_RUN=true PICK_JOBS="a=windows-x64 b=windows-x64" \
PICK_VARS="$(jq -n --arg h "$(gh variable get DENEXT_POOL_HEALTH --org Brainwires)" \
  '{DENEXT_POOL_HEALTH: $h, RUNNER_WINDOWS_X64: "[\"self-hosted\",\"denext-windows-x64\"]"}')" \
python3 -I actions/pick-runners/pick_runners.py
```
