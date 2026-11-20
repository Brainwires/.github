#!/usr/bin/env python3
"""Tests for pick_runners.py:  python3 -I actions/pick-runners/pick_runners_test.py"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pick_runners as pr  # noqa: E402

NOW = 1_800_000_000
WIN = '["self-hosted","denext-windows-x64"]'
LINUX = '["self-hosted","denext-linux-x64"]'
DOCKER = '["self-hosted","denext-linux-x64-docker"]'
MAC = '["self-hosted","denext-macos-arm64"]'
MACX = '["self-hosted","denext-macos-x64"]'


def entry(slots, busy=0, ok=True, paused=False, queued=None, healthy=None):
    e = {"ok": ok, "slots": slots, "busy": busy, "paused": paused,
         "healthy_slots": slots if healthy is None else healthy}
    if queued is not None:
        e["queued"] = queued
    return e


def health(pools, age=60):
    return json.dumps({"v": 1, "checked_at": NOW - age, "pools": pools})


# The suites below that predate the expected wait pin every threshold to 0 (the old rule: a free slot
# or GitHub), so they keep testing the slot counting, the caps and the switches on their own;
# ExpectedWaitTest and BurstTest run with the real defaults (max_wait=None).
OLD_RULE = "linux=0 windows=0 macos=0"


def env(jobs, health_value=None, max_wait=OLD_RULE, **variables):
    v = dict(variables)
    if health_value is not None:
        v["DENEXT_POOL_HEALTH"] = health_value
    out = {} if max_wait is None else {"PICK_MAX_WAIT": max_wait}
    return out | {
        "PICK_JOBS": jobs,
        "PICK_VARS": json.dumps(v),
        "GITHUB_REPOSITORY": "Brainwires/denext",
        "GITHUB_RUN_ID": "100",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_EVENT_NAME": "push",
    }


class FakeApi:
    """GET answers by path prefix; records every call."""

    def __init__(self, runs=(), jobs=None, runners=(), attempt_jobs=(), fail=False, run=None):
        self.runs, self.jobs, self.runners = list(runs), jobs or {}, list(runners)
        self.attempt_jobs, self.fail, self.calls = list(attempt_jobs), fail, []
        self.run = run  # this run's own object (GET /actions/runs/<id>), or None: not answered

    def __call__(self, path):
        self.calls.append(path)
        if self.fail:
            raise OSError("HTTP 502")
        p = path.split("?")[0]
        if "/attempts/" in p:
            return {"jobs": self.attempt_jobs}
        if p.endswith("/actions/runs"):
            status = "queued" if "status=queued" in path else "in_progress"
            return {"workflow_runs": [r for r in self.runs if r["status"] == status]}
        if p.endswith("/jobs"):
            rid = p.split("/runs/")[1].split("/")[0]
            return {"jobs": self.jobs.get(rid, [])}
        if p.endswith("/actions/runners"):
            return {"runners": self.runners}
        if "/actions/runs/" in p and p.rsplit("/", 1)[1].isdigit() and self.run is not None:
            return self.run
        raise AssertionError(path)


def run(e, get=None, runner_get=None, now=NOW):
    warnings = []
    plan, rows, free = pr.pick(e, get, runner_get, warnings.append, now)
    return plan, rows, free, warnings


def ours(v):
    return json.loads(v)


class ParseTest(unittest.TestCase):
    def test_variable(self):
        self.assertEqual(pr.parse_variable(""), ("off", None, None))
        self.assertEqual(pr.parse_variable("windows-latest"), ("image", "windows-latest", None))
        self.assertEqual(pr.parse_variable('["macos-latest"]'), ("image", "macos-latest", None))
        self.assertEqual(pr.parse_variable(WIN), ("ours", ["self-hosted", "denext-windows-x64"], "denext-windows-x64"))
        self.assertEqual(pr.parse_variable("denext-linux-x64"), ("ours", "denext-linux-x64", "denext-linux-x64"))
        self.assertEqual(pr.parse_variable("[not json"), ("off", None, None))
        self.assertEqual(pr.parse_variable('["self-hosted"]'), ("off", None, None))

    def test_jobs(self):
        jobs, bad = pr.parse_jobs("a=linux-x64 b=windows-x64@windows-2025 c=linux-arm64-desktop! d=~a e=nope f")
        self.assertEqual([j["key"] for j in jobs], ["a", "b", "c", "d"])
        self.assertEqual(jobs[1]["image"], "windows-2025")
        self.assertTrue(jobs[2]["pinned"])
        self.assertEqual(jobs[3]["follow"], "a")
        self.assertEqual(bad, ["e=nope", "f"])


class FreeSlotTest(unittest.TestCase):
    def test_free_slot_goes_to_our_pool(self):
        h = health({"denext-windows-x64": entry(2, busy=1)})
        plan, _, free, _ = run(env("a=windows-x64 b=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(free, {"denext-windows-x64": 1})
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest"})

    def test_full_pool_overflows_everything(self):
        # Desktop CI run 43736740374: two Windows jobs queued 16+ min on a full pool.
        h = health({"denext-windows-x64": entry(2, busy=2)})
        plan, rows, _, _ = run(env("a=windows-x64 b=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": "windows-latest", "b": "windows-latest"})
        self.assertIn("denext-windows-x64 2/2 slots taken", rows[0][3])
        self.assertIn("exceeds it", rows[0][3])

    def test_queued_counts_against_free(self):
        h = health({"denext-windows-x64": entry(2, busy=0, queued=1)})
        plan, _, free, _ = run(env("a=windows-x64 b=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(free, {"denext-windows-x64": 1})
        self.assertEqual(plan["b"], "windows-latest")

    def test_booting_runner_is_not_counted_twice(self):
        # Two queued jobs whose launchers are booting: busy 2 AND queued 2 are
        # the same two slots (the old pickers summed them and saw 4 taken).
        h = health({"denext-macos-arm64": entry(4, busy=2, queued=2)})
        _, _, free, _ = run(env("a=macos-arm64", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual(free, {"denext-macos-arm64": 2})

    def test_other_runs_queued_jobs_in_this_repo(self):
        # The fleet has not published yet; this repository's other run queued 1.
        h = health({"denext-windows-x64": entry(2, busy=0)})
        api = FakeApi(runs=[{"id": 7, "status": "queued"}],
                      jobs={"7": [{"status": "queued", "labels": ["self-hosted", "denext-windows-x64"]},
                                  {"status": "completed", "labels": ["self-hosted", "denext-windows-x64"]}]})
        plan, _, free, _ = run(env("a=windows-x64 b=windows-x64", h, RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertEqual(free, {"denext-windows-x64": 1})
        self.assertEqual(plan["b"], "windows-latest")

    def test_own_run_is_not_counted(self):
        h = health({"denext-windows-x64": entry(1)})
        api = FakeApi(runs=[{"id": 100, "status": "in_progress"}],
                      jobs={"100": [{"status": "queued", "labels": ["denext-windows-x64"]}]})
        plan, _, _, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertEqual(plan["a"], ours(WIN))

    def test_busy_runners_org_wide(self):
        h = health({"denext-windows-x64": entry(2, busy=0)})
        runners = [{"status": "online", "busy": True, "labels": [{"name": "self-hosted"}, {"name": "denext-windows-x64"}]},
                   {"status": "online", "busy": True, "labels": [{"name": "denext-windows-x64"}]}]
        plan, _, free, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN),
                               get=FakeApi(), runner_get=FakeApi(runners=runners))
        self.assertEqual(free, {"denext-windows-x64": 0})
        self.assertEqual(plan["a"], "windows-latest")

    def test_api_failure_counts_fleet_numbers_only(self):
        h = health({"denext-windows-x64": entry(2, busy=1)})
        plan, _, free, warnings = run(env("a=windows-x64 b=windows-x64", h, RUNNER_WINDOWS_X64=WIN),
                                      get=FakeApi(fail=True))
        self.assertEqual(free, {"denext-windows-x64": 1})
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest"})
        self.assertTrue(any("counting" in w for w in warnings))


class LinuxArm64Test(unittest.TestCase):
    ARM = '["self-hosted","denext-linux-arm64"]'

    def test_free_slot_then_each_jobs_own_overflow_image(self):
        h = health({"denext-linux-arm64": entry(1)})
        plan, rows, _, _ = run(env("a=linux-arm64 b=linux-arm64@ubuntu-22.04-arm c=linux-arm64", h, RUNNER_LINUX_ARM64=self.ARM))
        self.assertEqual(plan, {"a": ours(self.ARM), "b": "ubuntu-22.04-arm", "c": "ubuntu-24.04-arm"})

    def test_unhealthy_overflows(self):
        h = health({"denext-linux-arm64": entry(1, ok=False)})
        plan, _, _, _ = run(env("a=linux-arm64@ubuntu-22.04-arm", h, RUNNER_LINUX_ARM64=self.ARM))
        self.assertEqual(plan, {"a": "ubuntu-22.04-arm"})

    def test_variable_unset_is_github(self):
        h = health({"denext-linux-arm64": entry(1)})
        plan, _, _, _ = run(env("a=linux-arm64 b=linux-arm64@ubuntu-22.04-arm", h))
        self.assertEqual(plan, {"a": "ubuntu-24.04-arm", "b": "ubuntu-22.04-arm"})


class MiniHostTest(unittest.TestCase):
    """The Mac host's pools share one RAM budget (16 GB): macOS and Windows arm64 VMs 8 GB each,
    a Linux arm64 job container 6. A slot that looks free is no room when the memory is spent."""

    ARM = '["self-hosted","denext-linux-arm64"]'
    WINARM = '["self-hosted","denext-windows-arm64"]'
    VARS = {"RUNNER_LINUX_ARM64": ARM, "RUNNER_MACOS_ARM": MAC, "RUNNER_WINDOWS_ARM": WINARM}

    def pools(self, mac=0, linux=0, win=0, **more):
        return {"denext-macos-arm64": entry(2, busy=mac), "denext-linux-arm64": entry(1, busy=linux),
                "denext-windows-arm64": entry(2, busy=win), **more}

    def test_two_macos_vms_leave_no_room_for_a_linux_arm64_job(self):
        # Brainwires/deno 2026-11-18/19: the build waited 10-36 min on its "free" slot.
        plan, rows, free, _ = run(env("build=linux-arm64@ubuntu-22.04-arm", health(self.pools(mac=2)), **self.VARS))
        self.assertEqual(free["denext-linux-arm64"], 1)
        self.assertEqual(plan, {"build": "ubuntu-22.04-arm"})
        self.assertIn("the mini-host pools 16/16 GB taken", rows[0][3])

    def test_one_macos_vm_leaves_room_for_one_more_job(self):
        h = health(self.pools(mac=1))
        plan, _, _, _ = run(env("build=linux-arm64@ubuntu-22.04-arm", h, **self.VARS))
        self.assertEqual(plan, {"build": ours(self.ARM)})
        # 8 + 8 fills it: the Linux job after a second macOS job takes its image.
        plan, _, _, _ = run(env("lint=macos-arm64 build=linux-arm64@ubuntu-22.04-arm", h, **self.VARS))
        self.assertEqual(plan, {"lint": ours(MAC), "build": "ubuntu-22.04-arm"})
        # 8 + 6 leaves 2 GB: no second VM.
        plan, _, _, _ = run(env("build=linux-arm64 lint=macos-arm64", h, **self.VARS))
        self.assertEqual(plan, {"build": ours(self.ARM), "lint": "macos-latest"})

    def test_an_idle_host_takes_two_vms_or_a_vm_and_a_container(self):
        h = health(self.pools())
        plan, _, _, _ = run(env("a=macos-arm64 b=macos-arm64 c=linux-arm64", h, **self.VARS))
        self.assertEqual(plan, {"a": ours(MAC), "b": ours(MAC), "c": "ubuntu-24.04-arm"})
        plan, _, _, _ = run(env("c=linux-arm64 a=macos-arm64 b=macos-arm64", h, **self.VARS))
        self.assertEqual(plan, {"c": ours(self.ARM), "a": ours(MAC), "b": "macos-latest"})

    def test_queued_jobs_of_the_other_pools_count(self):
        # one VM booting for one of two queued macOS jobs: both VMs' memory is promised
        # (busy and queued overlap, as for a label's slots: the larger of the two counts)
        h = health(self.pools(**{"denext-macos-arm64": entry(2, busy=1, queued=2)}))
        plan, _, _, _ = run(env("build=linux-arm64", h, **self.VARS))
        self.assertEqual(plan, {"build": "ubuntu-24.04-arm"})

    def test_a_disabled_pools_queue_takes_no_memory(self):
        off = dict(entry(2, ok=False, queued=3), reason="disabled by config")
        h = health(self.pools(mac=1, **{"denext-windows-arm64": off}))
        plan, _, _, _ = run(env("build=linux-arm64", h, **self.VARS))
        self.assertEqual(plan, {"build": ours(self.ARM)})

    def test_a_macos_job_waits_for_the_memory_while_github_macos_is_off(self):
        h = json.dumps({"v": 1, "checked_at": NOW - 60, "github_off": ["macos"],
                        "pools": self.pools(mac=1, linux=1)})
        plan, rows, _, _ = run(env("lint=macos-arm64", h, **self.VARS))
        self.assertEqual(plan, {"lint": ours(MAC)})
        self.assertEqual(rows[0][2], "ours (waits)")

    def test_the_budget_variable(self):
        h = health(self.pools(mac=2))
        plan, _, _, _ = run(env("build=linux-arm64", h, RUNNER_MINI_MEM_GB="22", **self.VARS))
        self.assertEqual(plan, {"build": ours(self.ARM)})

    def test_an_aged_value_counts_the_busy_runners_live(self):
        # idle when the run started; two macOS VMs run now
        h = health(self.pools(), age=20 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 15 * 60)})
        vm = {"status": "online", "busy": True, "labels": [{"name": "denext-macos-arm64"}]}
        plan, _, _, _ = run(env("build=linux-arm64@ubuntu-22.04-arm", h, **self.VARS), get=api,
                            runner_get=FakeApi(runners=[vm, vm]))
        self.assertEqual(plan, {"build": "ubuntu-22.04-arm"})
        plan, _, _, _ = run(env("build=linux-arm64@ubuntu-22.04-arm", h, **self.VARS), get=api,
                            runner_get=FakeApi(runners=[vm]))
        self.assertEqual(plan, {"build": ours(self.ARM)})

    def test_stale_health_sends_one_job_to_the_host(self):
        plan, _, _, _ = run(env("a=macos-arm64 c=linux-arm64 b=macos-arm64", None, **self.VARS))
        self.assertEqual(plan, {"a": ours(MAC), "c": "ubuntu-24.04-arm", "b": "macos-latest"})

    def test_a_pinned_job_takes_its_memory_first(self):
        h = health(self.pools(mac=1))
        plan, _, _, _ = run(env("build=linux-arm64 gui=macos-arm64!", h, **self.VARS))
        self.assertEqual(plan, {"build": "ubuntu-24.04-arm", "gui": ours(MAC)})


class HealthTest(unittest.TestCase):
    def test_not_ok_is_zero(self):
        h = health({"denext-windows-x64": dict(entry(2, ok=False), reason="guest unreachable")})
        plan, rows, _, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan["a"], "windows-latest")
        self.assertIn("unhealthy: guest unreachable", rows[0][3])

    def test_paused_is_zero(self):
        h = health({"denext-windows-x64": entry(2, paused=True)})
        plan, _, _, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan["a"], "windows-latest")

    def test_unlisted_label_is_zero(self):
        h = health({"denext-linux-x64": entry(3)})
        plan, _, _, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan["a"], "windows-latest")

    def test_healthy_slots_cap(self):
        # win-x64 + win-x64-brain share the label; one host down.
        h = health({"denext-windows-x64": entry(3, busy=1, healthy=1)})
        plan, _, free, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(free["denext-windows-x64"], 0)
        self.assertEqual(plan["a"], "windows-latest")

    def test_stale_health_allows_one_job_per_pool(self):
        h = health({"denext-windows-x64": entry(9)}, age=11 * 60)
        plan, _, _, warnings = run(env("a=windows-x64 b=windows-x64 c=macos-arm64 d=macos-arm64", h,
                                       RUNNER_WINDOWS_X64=WIN, RUNNER_MACOS_ARM=MAC))
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest", "c": ours(MAC), "d": "macos-latest"})
        self.assertTrue(any("older than 10 min" in w for w in warnings))

    def test_stale_health_minus_what_this_repo_holds(self):
        api = FakeApi(runs=[{"id": 7, "status": "in_progress"}],
                      jobs={"7": [{"status": "in_progress", "labels": ["denext-windows-x64"]}]})
        plan, _, _, _ = run(env("a=windows-x64", "", RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertEqual(plan["a"], "windows-latest")

    def test_unreadable_health(self):
        plan, _, _, _ = run(env("a=windows-x64 b=windows-x64", "{nope", RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest"})

    def test_health_from_the_future_is_stale(self):
        h = json.dumps({"v": 1, "checked_at": NOW + 3600, "pools": {"denext-windows-x64": entry(9)}})
        self.assertIsNone(pr.fresh_health(h, NOW))


class BatchTest(unittest.TestCase):
    def test_batch_fills_free_slots_then_github_in_priority_order(self):
        h = health({"denext-macos-arm64": entry(2, busy=0)})
        plan, _, _, _ = run(env("a=macos-arm64 b=macos-arm64 c=macos-arm64 d=macos-arm64", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual(plan, {"a": ours(MAC), "b": ours(MAC), "c": "macos-latest", "d": "macos-latest"})

    def test_linux_host_labels_share_one_capacity(self):
        h = health({"denext-linux-x64": entry(3, busy=1), "denext-linux-x64-docker": entry(1, busy=1),
                    "denext-linux-x64-2204": entry(3, busy=0)})
        plan, _, _, _ = run(env("a=linux-x64-2204 b=linux-x64 c=linux-x64", h, RUNNER_LINUX_X64=LINUX,
                                RUNNER_LINUX_X64_DOCKER=DOCKER,
                                RUNNER_LINUX_X64_2204='["self-hosted","denext-linux-x64-2204"]'))
        # 3 shared slots, 2 busy: one job on our pools.
        self.assertEqual(plan["a"], ["self-hosted", "denext-linux-x64-2204"])
        self.assertEqual(plan["b"], "ubuntu-latest")
        self.assertEqual(plan["c"], "ubuntu-latest")

    def test_shared_slots_variable(self):
        h = health({"denext-linux-x64": entry(3), "denext-linux-x64-docker": entry(1)})
        plan, _, _, _ = run(env("a=linux-x64 b=linux-x64 c=linux-x64", h, RUNNER_LINUX_X64=LINUX,
                                RUNNER_LINUX_X64_SHARED_SLOTS="1"))
        self.assertEqual(list(plan.values()).count("ubuntu-latest"), 2)

    def test_docker_pool_falls_back_to_x64_variable(self):
        h = health({"denext-linux-x64": entry(3)})
        plan, _, _, _ = run(env("a=linux-x64-docker", h, RUNNER_LINUX_X64=LINUX))
        self.assertEqual(plan["a"], ours(LINUX))

    def test_follow(self):
        h = health({"denext-windows-x64": entry(1)})
        plan, _, _, _ = run(env("a=windows-x64 b=windows-x64 c=~a d=~b", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest", "c": ours(WIN), "d": "windows-latest"})

    def test_pinned_takes_its_slot_first(self):
        h = health({"denext-windows-x64": entry(1)})
        plan, rows, _, _ = run(env("a=windows-x64 b=windows-x64!", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": "windows-latest", "b": ours(WIN)})
        self.assertEqual(rows[1][2], "ours (pinned)")

    def test_pinned_ignores_variable_and_health(self):
        h = health({"denext-windows-arm64": entry(2, ok=False)})
        plan, _, _, _ = run(env("a=windows-arm64! b=linux-arm64-desktop!", h, RUNNER_WINDOWS_ARM="windows-11-arm"))
        self.assertEqual(plan, {"a": ["self-hosted", "denext-windows-arm64"],
                                "b": ["self-hosted", "denext-linux-arm64-desktop"]})

    def test_image_override(self):
        h = health({"denext-windows-x64": entry(1, busy=1)})
        plan, _, _, _ = run(env("a=windows-x64@windows-2025", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan["a"], "windows-2025")


class FastHostTest(unittest.TestCase):
    """Long Windows jobs ask for the fast host's own label; the second Windows host never takes them."""

    def h(self, win_busy, fast_busy, brain_ok=True):
        return health({"denext-windows-x64": entry(2, busy=win_busy + (1 if brain_ok and win_busy > fast_busy else 0),
                                                   healthy=2 if brain_ok else 1),
                       "denext-windows-x64-fast": entry(1, busy=fast_busy)})

    def test_long_job_on_fast_host_when_free(self):
        plan, _, _, _ = run(env("t=windows-x64-fast", self.h(0, 0), RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan["t"], ["self-hosted", "denext-windows-x64-fast"])

    def test_long_job_overflows_when_only_brain_is_free(self):
        h = health({"denext-windows-x64": entry(2, busy=1), "denext-windows-x64-fast": entry(1, busy=1)})
        plan, rows, _, _ = run(env("t=windows-x64-fast s=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"t": "windows-latest", "s": ours(WIN)})  # the short job takes the second host

    def test_long_job_uses_up_the_shared_label_slot(self):
        h = health({"denext-windows-x64": entry(2, busy=1), "denext-windows-x64-fast": entry(1, busy=0)})
        plan, _, _, _ = run(env("t=windows-x64-fast s=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"t": ["self-hosted", "denext-windows-x64-fast"], "s": "windows-latest"})

    def test_short_job_first_leaves_no_fast_slot(self):
        h = health({"denext-windows-x64": entry(2, busy=1), "denext-windows-x64-fast": entry(1, busy=0)})
        plan, _, _, _ = run(env("s=windows-x64 t=windows-x64-fast", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"s": ours(WIN), "t": "windows-latest"})

    def test_fast_label_not_published_yet(self):
        plan, rows, _, _ = run(env("t=windows-x64-fast", health({"denext-windows-x64": entry(2)}),
                                   RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan["t"], "windows-latest")
        self.assertIn("does not list denext-windows-x64-fast", rows[0][3])

    def test_switched_by_the_windows_variable(self):
        plan, _, _, _ = run(env("t=windows-x64-fast@windows-2022", self.h(0, 0), RUNNER_WINDOWS_X64="windows-latest"))
        self.assertEqual(plan["t"], "windows-2022")


class MacosCapTest(unittest.TestCase):
    def test_cap_sends_the_rest_to_our_pool(self):
        h = health({"denext-macos-arm64": entry(2, busy=2)})
        jobs = " ".join(f"j{i}=macos-arm64" for i in range(5))
        e = env(jobs, h, RUNNER_MACOS_ARM=MAC)
        e["PICK_MACOS_CAP"] = "3"
        plan, rows, _, _ = run(e)
        self.assertEqual(list(plan.values()), ["macos-latest"] * 3 + [ours(MAC)] * 2)
        self.assertEqual(rows[3][2], "ours (waits)")

    def test_cap_counts_both_mac_pools(self):
        h = health({"denext-macos-arm64": entry(1, busy=1), "denext-macos-x64": entry(1, busy=1)})
        e = env("a=macos-arm64 b=macos-x64 c=macos-x64", h, RUNNER_MACOS_ARM=MAC, RUNNER_MACOS_X64=MACX)
        e["PICK_MACOS_CAP"] = "2"
        plan, _, _, _ = run(e)
        self.assertEqual(plan, {"a": "macos-latest", "b": "macos-15-intel", "c": ours(MACX)})

    def test_cap_does_not_hold_a_pool_that_is_off(self):
        h = health({"denext-macos-arm64": entry(1, busy=1)})
        e = env("a=macos-arm64 b=macos-arm64", h, RUNNER_MACOS_ARM="macos-latest")
        e["PICK_MACOS_CAP"] = "0"
        plan, _, _, _ = run(e)
        self.assertEqual(plan, {"a": "macos-latest", "b": "macos-latest"})

    def test_cap_never_waits_on_a_stalled_pool(self):
        # denext.24 run 43756144777 attempt 2: mac-x64 stalled in attempt 1, cap spent.
        h = health({"denext-macos-x64": entry(1, busy=1)})
        e = env("a=macos-x64 b=macos-x64", h, RUNNER_MACOS_X64=MACX)
        e.update(PICK_MACOS_CAP="0", GITHUB_RUN_ATTEMPT="2")
        api = FakeApi(attempt_jobs=[{"conclusion": "cancelled", "runner_name": None,
                                     "labels": ["self-hosted", "denext-macos-x64"]}])
        plan, _, _, _ = run(e, get=api)
        self.assertEqual(plan, {"a": "macos-15-intel", "b": "macos-15-intel"})

    def test_cap_never_waits_on_an_unhealthy_pool(self):
        h = health({"denext-macos-x64": entry(1, ok=False)})
        e = env("a=macos-x64", h, RUNNER_MACOS_X64=MACX)
        e["PICK_MACOS_CAP"] = "0"
        self.assertEqual(run(e)[0]["a"], "macos-15-intel")

    def test_busy_single_slot_pool_with_queued_jobs_from_another_run(self):
        # The coordinator's repro: mac-x64 busy 1/1, the tests run's jobs queued on it, two x86
        # jobs and the arm legs on images (RUNNER_MACOS_ARM=macos-latest): both x86 go to GitHub.
        h = health({"denext-macos-x64": entry(1, busy=1)})
        api = FakeApi(runs=[{"id": 7, "status": "in_progress"}],
                      jobs={"7": [{"status": "queued", "labels": ["self-hosted", "denext-macos-x64"]},
                                  {"status": "in_progress", "labels": ["self-hosted", "denext-macos-x64"]}]})
        e = env("a1=macos-arm64 a2=macos-arm64 a3=macos-arm64 x1=macos-x64 x2=macos-x64", h,
                RUNNER_MACOS_ARM="macos-latest", RUNNER_MACOS_X64=MACX)
        plan, _, free, _ = run(e, get=api)
        self.assertEqual(free["denext-macos-x64"], 0)
        self.assertEqual([plan["x1"], plan["x2"]], ["macos-15-intel", "macos-15-intel"])

    def test_rerun_after_github_macos_starved(self):
        h = health({"denext-macos-arm64": entry(1, busy=1)})
        e = env("a=macos-arm64", h, RUNNER_MACOS_ARM=MAC)
        e["GITHUB_RUN_ATTEMPT"] = "2"
        api = FakeApi(attempt_jobs=[{"conclusion": "cancelled", "runner_name": None, "labels": ["macos-15"]}])
        plan, _, _, warnings = run(e, get=api)
        self.assertEqual(plan["a"], ours(MAC))
        self.assertTrue(any("wait for our pool" in w for w in warnings))


def iso(t):
    return __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime(t))


class AgedHealthTest(unittest.TestCase):
    """GitHub fixes `vars` when the run starts: a picker 30 min into the run reads a 30-min-old value."""

    def test_aged_value_keeps_the_pool_states_and_slots(self):
        # fresh (6 min) when the run started 25 min ago; 31 min old now: two free slots, not one
        h = health({"denext-macos-arm64": entry(2)}, age=31 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 25 * 60), "created_at": iso(NOW - 25 * 60)})
        plan, _, free, warnings = run(env("a=macos-arm64 b=macos-arm64 c=macos-arm64", h, RUNNER_MACOS_ARM=MAC),
                                      get=api)
        self.assertEqual(free["denext-macos-arm64"], 2)
        self.assertEqual(plan, {"a": ours(MAC), "b": ours(MAC), "c": "macos-latest"})
        self.assertTrue(any("31 min old" in w and "25 min later" in w for w in warnings), warnings)
        self.assertFalse(any("older than 10 min" in w for w in warnings))

    def test_aged_value_still_blocks_an_unhealthy_pool(self):
        # the stale path would send one job to it; the aged value knows it was down
        h = health({"denext-windows-x64": dict(entry(2, ok=False), reason="guest unreachable")}, age=30 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 28 * 60)})
        plan, rows, _, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertEqual(plan["a"], "windows-latest")
        self.assertIn("guest unreachable", rows[0][3])

    def test_aged_value_counts_the_published_load_without_a_runner_token(self):
        h = health({"denext-macos-arm64": entry(2, busy=2)}, age=20 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 15 * 60)})
        plan, _, free, _ = run(env("a=macos-arm64", h, RUNNER_MACOS_ARM=MAC), get=api)
        self.assertEqual(free["denext-macos-arm64"], 0)
        self.assertEqual(plan["a"], "macos-latest")

    def test_aged_value_with_the_runner_token_counts_busy_runners_live(self):
        h = health({"denext-macos-arm64": entry(2, busy=2, queued=3)}, age=20 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 15 * 60)})
        runners = FakeApi(runners=[{"status": "online", "busy": True, "labels": [{"name": "denext-macos-arm64"}]}])
        e = env("a=macos-arm64 b=macos-arm64", h, RUNNER_MACOS_ARM=MAC)
        plan, _, free, _ = run(e, get=api, runner_get=runners)
        self.assertEqual(free["denext-macos-arm64"], 1)  # 2 slots, 1 busy now (the published 2 + 3 queued are gone)
        self.assertEqual(plan, {"a": ours(MAC), "b": "macos-latest"})

    def test_a_value_already_stale_when_the_run_started_is_stale(self):
        # the watchdog was not publishing: 40 min old, the run started 20 min ago
        h = health({"denext-windows-x64": entry(9)}, age=40 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 20 * 60)})
        plan, _, _, warnings = run(env("a=windows-x64 b=windows-x64", h, RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest"})
        self.assertTrue(any("older than 10 min" in w for w in warnings))

    def test_too_old_or_unknown_start_is_stale(self):
        h = health({"denext-windows-x64": entry(9)}, age=pr.AGED_MAX_AGE + 1)
        self.assertIsNone(pr.aged_health(h, NOW, NOW - pr.AGED_MAX_AGE))
        h = health({"denext-windows-x64": entry(9)}, age=30 * 60)
        self.assertIsNone(pr.aged_health(h, NOW, None))
        plan, _, _, _ = run(env("a=windows-x64 b=windows-x64", h, RUNNER_WINDOWS_X64=WIN), get=FakeApi())
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest"})  # the run lookup failed: stale path

    def test_aged_value_ignores_its_github_queue(self):
        v = json.loads(health({"denext-macos-arm64": entry(1, busy=1)}, age=20 * 60))
        v["github"] = {"macos": {"queued": 5, "oldest_at": NOW - 30 * 60}}
        api = FakeApi(run={"run_started_at": iso(NOW - 15 * 60)})
        plan, _, _, _ = run(env("a=macos-arm64", json.dumps(v), RUNNER_MACOS_ARM=MAC), get=api)
        self.assertEqual(plan["a"], "macos-latest")  # no "waits": that queue is 20 min old

    def test_the_deno_run_of_2026_11_17(self):
        # value checked 02:42:12Z, run started 02:48:33Z, the macos-15 leg's picker at 03:15:01Z
        h = json.dumps({"v": 1, "checked_at": 1794883332, "pools": {"denext-macos-arm64": entry(2)}})
        self.assertIsNone(pr.fresh_health(h, 1794885301))
        self.assertIsNotNone(pr.aged_health(h, 1794885301, 1794883713))


class RerunTest(unittest.TestCase):
    def test_rerun_after_our_pool_stalled(self):
        h = health({"denext-windows-x64": entry(2)})
        e = env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN)
        e["GITHUB_RUN_ATTEMPT"] = "2"
        api = FakeApi(attempt_jobs=[{"conclusion": "cancelled", "runner_name": "",
                                     "labels": ["self-hosted", "denext-windows-x64"]}])
        plan, _, _, _ = run(e, get=api)
        self.assertEqual(plan["a"], "windows-latest")

    def test_first_attempt_makes_no_attempt_call(self):
        api = FakeApi()
        run(env("a=windows-x64", health({"denext-windows-x64": entry(1)}), RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertFalse(any("/attempts/" in c for c in api.calls))


class SettleTest(unittest.TestCase):
    """The same-second race (Brainwires/deno runs 43774199406 tests / 43774202217 runtime)."""

    T0 = "2026-11-15T20:00:00Z"
    T_PLUS = "2026-11-15T20:00:04Z"

    def setup(self, sibling_jobs, sibling_created=None, sibling_id=99):
        self.api = FakeApi(
            runs=[{"id": sibling_id, "status": "in_progress", "created_at": sibling_created or self.T0},
                  {"id": 100, "status": "queued", "created_at": self.T_PLUS}],
            jobs={str(sibling_id): sibling_jobs})
        self.clock_t = 0.0
        self.sleeps = []
        self.on_sleep = lambda: None

    def sleep(self, s):
        self.sleeps.append(s)
        self.clock_t += s
        self.on_sleep()

    def go(self, jobs="d=macos-x64", **extra):
        h = health({"denext-macos-x64": entry(1, busy=0)})
        e = env(jobs, h, RUNNER_MACOS_X64=MACX)
        e.update(extra)
        warnings = []
        plan, _, _ = pr.pick(e, self.api, None, warnings.append, NOW, sleep=self.sleep,
                             clock=lambda: self.clock_t)
        return plan, warnings

    def test_waits_for_the_earlier_sibling_picker_then_counts_its_job(self):
        picker = {"name": "macos-15-intel / pick build runners", "status": "in_progress", "labels": ["ubuntu-24.04"]}
        self.setup([picker])

        def picked():  # the tests run's picker finishes and its build job queues on mac-x64
            picker["status"] = "completed"
            self.api.jobs["99"].append({"name": "macos-15-intel / build debug", "status": "queued",
                                        "labels": ["self-hosted", "denext-macos-x64"]})
        self.on_sleep = picked
        plan, warnings = self.go()
        self.assertEqual(plan["d"], "macos-15-intel")  # the slot is the sibling's
        self.assertEqual(sum(self.sleeps), 20)  # 5 s picking, then 15 s quiet
        self.assertTrue(any("waited" in w for w in warnings))

    def test_the_runtime_run_between_its_two_pickers(self):
        # Gap 1: the fork runtime run's `pick runners` is done, `guard` runs (setup is not
        # listed yet: the API lists a job once its needs are done), then `setup: pick matrix
        # runners` picks and its matrix job queues on mac-x64. The quiet period bridges guard.
        self.setup([{"name": "pick runners", "status": "completed", "labels": ["ubuntu-latest"]},
                    {"name": "guard", "status": "in_progress", "labels": ["ubuntu-24.04"]}])
        step = {"n": 0}

        def progress():
            step["n"] += 1
            jobs = self.api.jobs["99"]
            if step["n"] == 2:  # 10 s in: guard done, setup picking
                jobs[1]["status"] = "completed"
                jobs.append({"name": "setup: pick matrix runners", "status": "in_progress",
                             "labels": ["ubuntu-24.04"]})
            if step["n"] == 4:  # 20 s in: setup done, the planned job exists
                jobs[2]["status"] = "completed"
                jobs.append({"name": "denort bin / x86_64-apple-darwin", "status": "queued",
                             "labels": ["self-hosted", "denext-macos-x64"]})
        self.on_sleep = progress
        plan, _ = self.go()
        self.assertEqual(plan["d"], "macos-15-intel")
        self.assertEqual(sum(self.sleeps), 35)  # 20 s until setup ends, then 15 s quiet

    def test_picker_names(self):
        for name in ("pick runners", "macos-15 / pick build runners", "setup: pick matrix runners"):
            self.assertTrue(pr.PICKER_JOB.search(name), name)
        for name in ("setup", "build debug", "pick runners / test", "pickle runners"):
            self.assertFalse(pr.PICKER_JOB.search(name), name)

    def test_a_sibling_with_no_jobs_yet_is_waited_for(self):
        self.setup([])

        def started():
            self.api.jobs["99"] = [{"name": "pick runners", "status": "completed", "labels": ["ubuntu-latest"]},
                                   {"name": "build", "status": "queued", "labels": ["self-hosted", "denext-macos-x64"]}]
        self.on_sleep = started
        plan, _ = self.go()
        self.assertEqual(plan["d"], "macos-15-intel")

    def test_a_sibling_that_already_picked_costs_only_the_quiet_period(self):
        self.setup([{"name": "pick runners", "status": "completed", "labels": ["ubuntu-latest"]},
                    {"name": "build", "status": "in_progress", "labels": ["self-hosted", "denext-macos-x64"]}])
        plan, _ = self.go()
        self.assertEqual(sum(self.sleeps), 15)
        self.assertEqual(plan["d"], "macos-15-intel")

    def test_a_later_run_never_makes_us_wait(self):
        # the sibling has the HIGHER id: it yields to us (no mutual waiting)
        self.setup([{"name": "pick runners", "status": "in_progress", "labels": ["ubuntu-latest"]}], sibling_id=101)
        plan, _ = self.go()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(plan["d"], ["self-hosted", "denext-macos-x64"])

    def test_an_old_run_is_not_a_sibling(self):
        self.setup([{"name": "pick runners", "status": "in_progress", "labels": ["ubuntu-latest"]}],
                   sibling_created="2026-11-15T19:50:00Z")
        self.go()
        self.assertEqual(self.sleeps, [])

    def test_a_picker_blocked_on_its_needs_is_not_waited_for(self):
        self.setup([{"name": "leg / pick runners", "status": "waiting", "labels": []},
                    {"name": "leg / build", "status": "in_progress", "labels": ["ubuntu-latest"]}])
        self.go()
        self.assertEqual(sum(self.sleeps), 15)  # the quiet period only

    def test_gives_up_after_the_limit(self):
        self.setup([{"name": "pick runners", "status": "in_progress", "labels": ["ubuntu-latest"]}])
        plan, warnings = self.go(PICK_SETTLE="20")
        self.assertEqual(sum(self.sleeps), 20)  # 4 looks x 5 s, then the limit
        self.assertTrue(any("still picking after 20s" in w for w in warnings))
        self.assertEqual(plan["d"], ["self-hosted", "denext-macos-x64"])

    def test_settle_off(self):
        self.setup([{"name": "pick runners", "status": "in_progress", "labels": ["ubuntu-latest"]}])
        self.go(PICK_SETTLE="0")
        self.assertEqual(self.sleeps, [])

    def test_no_api_no_settle(self):
        h = health({"denext-macos-x64": entry(1)})
        slept = []
        pr.pick(env("d=macos-x64", h, RUNNER_MACOS_X64=MACX), None, None, lambda m: None, NOW,
                sleep=slept.append, clock=lambda: 0)
        self.assertEqual(slept, [])


class VariableTest(unittest.TestCase):
    def test_variable_naming_an_image_sends_jobs_there(self):
        # The coordinator's temporary RUNNER_WINDOWS_X64=windows-latest.
        h = health({"denext-windows-x64": entry(2)})
        plan, rows, _, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64="windows-latest"))
        self.assertEqual(plan["a"], "windows-latest")
        self.assertIn("names a GitHub image", rows[0][3])

    def test_variable_naming_an_image_keeps_the_jobs_own_image(self):
        h = health({"denext-windows-x64": entry(2)})
        plan, _, _, _ = run(env("a=windows-x64@windows-2022", h, RUNNER_WINDOWS_X64="windows-latest"))
        self.assertEqual(plan["a"], "windows-2022")

    def test_unset_variable(self):
        plan, _, _, _ = run(env("a=linux-x64-2204", health({})))
        self.assertEqual(plan["a"], "ubuntu-22.04")

    def test_restoring_the_variable_restores_our_pool(self):
        h = health({"denext-windows-x64": entry(2)})
        plan, _, _, _ = run(env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan["a"], ours(WIN))


class ForkTest(unittest.TestCase):
    def _event(self, head):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"pull_request": {"head": {"repo": {"full_name": head}}}}, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_fork_pr_never_runs_on_ours(self):
        h = health({"denext-windows-x64": entry(2)})
        e = env("a=windows-x64 b=linux-arm64-desktop!", h, RUNNER_WINDOWS_X64=WIN)
        e.update(GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=self._event("someone/denext"))
        api = FakeApi()
        plan, _, _, _ = run(e, get=api)
        self.assertEqual(plan, {"a": "windows-latest", "b": "ubuntu-24.04-arm"})
        self.assertEqual(api.calls, [])

    def test_same_repo_pr_may(self):
        h = health_pr({"denext-windows-x64": entry(2)}, ["denext-windows-x64"])
        e = env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN)
        e.update(GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=self._event("Brainwires/denext"))
        plan, _, _, _ = run(e)
        self.assertEqual(plan["a"], ours(WIN))

    def test_pull_request_target_is_a_fork(self):
        e = env("a=windows-x64", health({"denext-windows-x64": entry(2)}), RUNNER_WINDOWS_X64=WIN)
        e["GITHUB_EVENT_NAME"] = "pull_request_target"
        self.assertEqual(run(e)[0]["a"], "windows-latest")


def health_pr(pools, labels, age=60):
    """A health value with the top-level `pull_request_labels` (the pools that take a same-repo PR)."""
    return json.dumps({"v": 1, "checked_at": NOW - age, "pools": pools, "pull_request_labels": labels})


class SameRepoPullRequestTest(unittest.TestCase):
    """A same-repo pull_request goes only to pools the scheduler hands one (`pull_request_labels`); the
    rest take GitHub, so a job never queues on a pool that refuses it (2026-11-19: 8 h on Windows)."""

    POOLS = {"denext-windows-x64": entry(2), "denext-windows-x64-fast": entry(1), "denext-linux-x64": entry(2),
             "denext-windows-arm64": entry(1)}
    VARS = {"RUNNER_WINDOWS_X64": WIN, "RUNNER_LINUX_X64": LINUX}

    def pr_env(self, jobs, h, head="Brainwires/denext", **variables):
        e = env(jobs, h, **(self.VARS | variables))
        e.update(GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=ForkTest._event(self, head))
        return e

    def test_a_pool_that_takes_no_pull_request_sends_its_jobs_to_github(self):
        h = health_pr(self.POOLS, ["denext-linux-x64", "DENEXT-LINUX-X64-DOCKER"])
        plan, rows, _, warnings = run(self.pr_env("w=windows-x64 l=linux-x64", h))
        self.assertEqual(plan, {"w": "windows-latest", "l": ours(LINUX)})
        self.assertEqual(rows[0][2], "GitHub")
        self.assertIn("denext-windows-x64 does not take pull requests", rows[0][3])
        self.assertEqual(pr.LAST["blocked"], ["denext-windows-x64"])
        self.assertEqual(pr.LAST["tripwire"], [])
        self.assertTrue(any("denext-windows-x64" in w and "GitHub image" in w for w in warnings))

    def test_listed_pools_take_it_case_insensitively_aliases_included(self):
        h = health_pr(self.POOLS, ["Denext-Windows-X64", "denext-windows-x64-fast"])
        plan, _, _, warnings = run(self.pr_env("t=windows-x64-fast s=windows-x64", h))
        self.assertEqual(plan, {"t": ["self-hosted", "denext-windows-x64-fast"], "s": ours(WIN)})
        self.assertEqual(pr.LAST["blocked"], [])
        self.assertFalse(any("pull request" in w for w in warnings))

    def test_without_the_list_every_pool_counts_as_refusing(self):
        for h in (health(self.POOLS),
                  health_pr(self.POOLS, "denext-windows-x64"),
                  health_pr(self.POOLS, ["denext-windows-x64", 3]),
                  health_pr(self.POOLS, ["denext-windows-x64"], age=pr.GITHUB_OFF_MAX_AGE + 1)):
            plan, rows, _, warnings = run(self.pr_env("w=windows-x64 l=linux-x64", h))
            self.assertEqual(plan, {"w": "windows-latest", "l": "ubuntu-latest"}, h)
            self.assertTrue(all("no pull_request_labels" in r[3] for r in rows), rows)
            self.assertTrue(warnings)

    def test_the_list_holds_in_a_value_frozen_at_run_start(self):
        h = health_pr(self.POOLS, ["denext-windows-x64"], age=40 * 60)
        self.assertEqual(pr.pr_labels(h, NOW), {"denext-windows-x64"})
        self.assertIsNone(pr.pr_labels(None, NOW))
        self.assertIsNone(pr.pr_labels("nope", NOW))
        self.assertEqual(pr.pr_labels(health_pr({}, []), NOW), set())

    def test_a_pinned_job_on_a_refusing_pool_takes_its_image(self):
        h = health_pr(self.POOLS, ["denext-linux-x64"])
        plan, rows, _, _ = run(self.pr_env("a=windows-arm64! b=linux-x64!", h))
        self.assertEqual(plan, {"a": "windows-11-arm", "b": ours(LINUX)})
        self.assertIn("pinned", rows[0][3])
        self.assertEqual(pr.LAST["blocked"], ["denext-windows-arm64"])

    def test_the_routing_rules_never_hold_a_job_on_a_refusing_pool(self):
        h = json.dumps({"v": 1, "checked_at": NOW - 60, "pools": self.POOLS, "pull_request_labels": [],
                        "routing": {"v": 1, "classes": [{"labels": ["denext-windows-x64"], "github": False,
                                                         "order": ["win-x64"]}]}})
        plan, rows, _, _ = run(self.pr_env("w=windows-x64", h))
        self.assertEqual(plan["w"], "windows-latest")
        self.assertIn("does not take pull requests", rows[0][3])

    def test_other_events_ignore_the_list(self):
        h = health_pr(self.POOLS, [])
        for event in ("push", "workflow_dispatch"):
            e = env("w=windows-x64", h, **self.VARS)
            e["GITHUB_EVENT_NAME"] = event
            self.assertEqual(run(e)[0]["w"], ours(WIN), event)

    def test_a_forks_pull_request_keeps_its_own_rule(self):
        h = health_pr(self.POOLS, ["denext-windows-x64"])
        plan, rows, _, _ = run(self.pr_env("w=windows-x64", h, head="someone/denext"))
        self.assertEqual(plan["w"], "windows-latest")
        self.assertIn("fork", rows[0][3])

    def test_the_error_fallback_keeps_a_pinned_job_off_a_refusing_pool(self):
        orig = pr.free_slots
        pr.free_slots = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            h = health_pr(self.POOLS, ["denext-linux-x64"])
            plan, _, _, _ = run(self.pr_env("a=windows-x64 b=windows-arm64! c=linux-x64!", h,
                                            RUNNER_WINDOWS_ARM='["self-hosted","denext-windows-arm64"]'))
        finally:
            pr.free_slots = orig
        self.assertEqual(plan, {"a": "windows-latest", "b": "windows-11-arm", "c": ours(LINUX)})
        self.assertEqual(pr.LAST["tripwire"], [])


class FailureTest(unittest.TestCase):
    def test_bad_vars(self):
        e = env("a=windows-x64")
        e["PICK_VARS"] = "nope"
        plan, _, _, warnings = run(e)
        self.assertEqual(plan["a"], "windows-latest")
        self.assertTrue(warnings)

    def test_internal_error_falls_back_to_images(self):
        orig = pr.free_slots
        pr.free_slots = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            plan, _, _, warnings = run(env("a=windows-x64 b=windows-arm64!", health({}),
                                           RUNNER_WINDOWS_X64=WIN))
        finally:
            pr.free_slots = orig
        self.assertEqual(plan, {"a": "windows-latest", "b": ["self-hosted", "denext-windows-arm64"]})
        self.assertTrue(any("boom" in w for w in warnings))


class CliTest(unittest.TestCase):
    def test_dry_run_prints_outputs_and_table(self):
        e = dict(os.environ)
        e.update(env("a=windows-x64 b=windows-x64", json.dumps(
            {"v": 1, "checked_at": int(__import__("time").time()), "pools": {"denext-windows-x64": entry(2, busy=1)}}),
            RUNNER_WINDOWS_X64=WIN))
        e["PICK_DRY_RUN"] = "true"
        e.pop("GITHUB_EVENT_PATH", None)
        out = subprocess.run([sys.executable, "-I", pr.__file__], env=e, capture_output=True, text=True, check=True)
        lines = dict(l.split("=", 1) for l in out.stdout.splitlines())
        self.assertEqual(json.loads(lines["plan"]), {"a": ours(WIN), "b": "windows-latest"})
        self.assertEqual(json.loads(lines["hosted"]), ["b"])
        self.assertIn("DRY RUN", out.stderr)
        self.assertEqual(json.loads(lines["blocked"]), [])
        self.assertIn("| b | windows-x64 | GitHub |", out.stderr)


def health_gh(pools, github, age=60):
    return json.dumps({"v": 1, "checked_at": NOW - age, "pools": pools, "github": github})


class SaturationTest(unittest.TestCase):
    """GitHub's macOS arm64 capacity degraded (laufey, 2026-11-16): a job sent to macos-15
    because our pool was busy queued until GitHub cancelled it; our pool freed minutes later."""

    def test_saturated_family_sends_a_busy_pool_job_to_our_pool(self):
        h = health_gh({"denext-macos-arm64": entry(1, busy=1)}, {"macos": {"queued": 2, "oldest_at": NOW - 420}})
        plan, rows, _, _ = run(env("a=macos-arm64@macos-15", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual(plan["a"], ours(MAC))
        self.assertEqual(rows[0][2], "ours (waits)")
        self.assertIn("GitHub's macos images have had a job queued for 7 min (saturated)", rows[0][3])
        self.assertEqual(pr.LAST["saturated"], {"macos": 420})

    def test_saturated_waits_are_bounded_per_pool(self):
        # 2026-11-16: every macOS job of a run waited for a 2-slot pool, 6 deep, while GitHub emptied.
        h = health_gh({"denext-macos-arm64": entry(2, busy=2)}, {"macos": {"queued": 4, "oldest_at": NOW - 600}})
        plan, rows, _, _ = run(env("a=macos-arm64@macos-15 b=macos-arm64@macos-15 c=macos-arm64@macos-15"
                                   " d=macos-arm64@macos-15", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual([plan[k] for k in "abcd"], [ours(MAC), ours(MAC), "macos-15", "macos-15"])
        self.assertEqual([r[2] for r in rows], ["ours (waits)", "ours (waits)", "GitHub", "GitHub"])
        self.assertIn("already wait for it", rows[2][3])

    def test_github_off_keeps_every_job_on_our_pool(self):
        # 2026-11-17 "use only our runners": the ops switch turns a family off; no depth limit.
        h = json.dumps({"v": 1, "checked_at": NOW - 60, "pools": {"denext-macos-arm64": entry(2, busy=2)},
                        "github": {}, "github_off": ["macos"]})
        plan, rows, _, _ = run(env("a=macos-arm64@macos-15 b=macos-arm64@macos-15 c=macos-arm64@macos-15"
                                   " d=macos-arm64@macos-15 e=macos-arm64@macos-15", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual([plan[k] for k in "abcde"], [ours(MAC)] * 5)
        self.assertTrue(all(r[2] == "ours (waits)" and "GitHub macos is off" in r[3] for r in rows))

    def test_github_off_holds_in_a_frozen_health_value(self):
        # GitHub freezes the variable at run start: a late picker sees it 17 min old (2026-11-17).
        h = json.dumps({"v": 1, "checked_at": NOW - 17 * 60, "pools": {"denext-macos-arm64": entry(1, busy=1)},
                        "github": {}, "github_off": ["macos", "windows"]})
        self.assertEqual(pr.github_off(h, NOW), {"macos", "windows"})

    def test_github_off_expires_after_its_max_age(self):
        h = json.dumps({"v": 1, "checked_at": NOW - pr.GITHUB_OFF_MAX_AGE - 1, "pools": {},
                        "github_off": ["macos"]})
        self.assertEqual(pr.github_off(h, NOW), set())

    def test_github_off_applies_with_aged_pool_health(self):
        h = json.dumps({"v": 1, "checked_at": NOW - 17 * 60,
                        "pools": {"denext-windows-x64": entry(1, busy=1)}, "github": {}, "github_off": ["windows"]})
        plan, rows, _, _ = run(env("a=windows-x64@windows-2022 b=windows-x64@windows-2022", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual([plan[k] for k in "ab"], [ours(WIN)] * 2)

    def test_github_off_ignores_unknown_families(self):
        h = json.dumps({"v": 1, "checked_at": NOW - 60, "pools": {}, "github_off": ["macos", "nope", 3]})
        self.assertEqual(pr.github_off(h, NOW), {"macos"})

    # ---- the fleet's routing rules (the health's `routing`): which classes may use GitHub at all
    ROUTING = {"v": 1, "classes": [
        {"labels": ["denext-windows-x64-fast"], "github": True, "order": ["win-x64-brain", "win-x64"]},
        {"labels": ["denext-windows-x64"], "repo": "Brainwires/deno", "workflow": "ci.yml", "github": False,
         "order": ["win-x64-brain", "win-x64"]},
        {"labels": ["denext-windows-x64"], "github": True, "order": ["win-x64-brain", "win-x64"]},
        {"labels": ["denext-macos-arm64"], "github": False, "order": ["mac-arm"]},
    ]}

    def routed(self, pools, age=60, routing=None):
        return json.dumps({"v": 1, "checked_at": NOW - age, "pools": pools, "github": {},
                           "routing": self.ROUTING if routing is None else routing})

    def test_routing_a_class_without_github_never_leaves_our_pools(self):
        h = self.routed({"denext-macos-arm64": entry(2, busy=2)})
        plan, rows, _, _ = run(env("a=macos-arm64@macos-15 b=macos-arm64@macos-15 c=macos-arm64@macos-15"
                                   " d=macos-arm64@macos-15 e=macos-arm64@macos-15", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual([plan[k] for k in "abcde"], [ours(MAC)] * 5)
        self.assertTrue(all(r[2] == "ours (routing)" and "GitHub is not allowed for denext-macos-arm64" in r[3]
                            and "mac-arm" in r[3] for r in rows))
        self.assertEqual(pr.LAST["tripwire"], [])

    def test_routing_holds_even_on_an_unhealthy_pool_and_says_so(self):
        h = self.routed({"denext-macos-arm64": dict(entry(1), ok=False, reason="NOT CLEAN")})
        plan, rows, _, _ = run(env("a=macos-arm64", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual(plan["a"], ours(MAC))
        self.assertIn("GitHub is not allowed", rows[0][3])

    def test_routing_a_class_with_github_overflows_as_before(self):
        h = self.routed({"denext-windows-x64": entry(1, busy=1)})
        plan, rows, _, _ = run(env("a=windows-x64@windows-2022 b=windows-x64@windows-2022", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual([plan[k] for k in "ab"], ["windows-2022"] * 2)
        self.assertEqual(pr.routing_no_github(h, NOW, "Brainwires/denext", ".github/workflows/ci.yml"),
                         {"denext-macos-arm64": "mac-arm"})

    def test_routing_the_first_class_that_fits_the_repository_and_workflow_decides(self):
        h = self.routed({})
        held = pr.routing_no_github(h, NOW, "Brainwires/deno", ".github/workflows/ci.yml")
        self.assertEqual(held, {"denext-windows-x64": "win-x64-brain > win-x64", "denext-macos-arm64": "mac-arm"})
        # another workflow of that repository, or another repository: the later class (GitHub allowed)
        self.assertNotIn("denext-windows-x64", pr.routing_no_github(h, NOW, "Brainwires/deno", ".github/workflows/x.yml"))
        self.assertNotIn("denext-windows-x64", pr.routing_no_github(h, NOW, "Brainwires/denext", ".github/workflows/ci.yml"))
        # globs
        g = self.routed({}, routing={"v": 1, "classes": [
            {"labels": ["denext-linux-x64"], "repo": "Brainwires/*", "workflow": "ci*.yml", "github": False, "order": []}]})
        self.assertIn("denext-linux-x64", pr.routing_no_github(g, NOW, "brainwires/Deno", ".github/workflows/ci.generate.yml"))
        self.assertEqual(pr.routing_no_github(g, NOW, "other/repo", ".github/workflows/ci.yml"), {})
        self.assertEqual(pr.workflow_path({"GITHUB_WORKFLOW_REF": "Brainwires/deno/.github/workflows/ci.yml@refs/heads/x"}),
                         ".github/workflows/ci.yml")
        self.assertEqual(pr.workflow_path({}), "")

    def test_routing_holds_in_a_frozen_value_and_expires(self):
        self.assertIn("denext-macos-arm64", pr.routing_no_github(self.routed({}, age=17 * 60), NOW, "a/b", ""))
        self.assertEqual(pr.routing_no_github(self.routed({}, age=pr.GITHUB_OFF_MAX_AGE + 1), NOW, "a/b", ""), {})

    def test_routing_missing_or_malformed_changes_nothing(self):
        for routing in ({"v": 2, "classes": self.ROUTING["classes"]}, {"v": 1, "classes": "x"}, "nope", {"v": 1},
                        {"v": 1, "classes": [3, {"labels": "x", "github": False}, {"labels": ["denext-macos-arm64"], "github": "no"},
                                             {"github": False, "order": []}]}):
            self.assertEqual(pr.routing_no_github(self.routed({}, routing=routing), NOW, "a/b", ""), {}, routing)
        h = json.dumps({"v": 1, "checked_at": NOW - 60, "pools": {"denext-macos-arm64": entry(1, busy=1)}, "github": {}})
        self.assertEqual(pr.routing_no_github(h, NOW, "a/b", ""), {})
        plan, _, _, _ = run(env("a=macos-arm64@macos-15", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual(plan["a"], "macos-15")

    def test_routing_does_not_send_a_forks_pull_request_to_our_runners(self):
        e = env("a=macos-arm64@macos-15", self.routed({"denext-macos-arm64": entry(2)}), RUNNER_MACOS_ARM=MAC)
        e.update(GITHUB_EVENT_NAME="pull_request_target")
        plan, rows, _, _ = run(e)
        self.assertEqual(plan["a"], "macos-15")

    def test_below_the_threshold_is_not_saturated(self):
        h = health_gh({"denext-macos-arm64": entry(1, busy=1)}, {"macos": {"queued": 3, "oldest_at": NOW - 179}})
        plan, _, _, _ = run(env("a=macos-arm64@macos-15", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual(plan["a"], "macos-15")
        self.assertEqual(pr.LAST["saturated"], {})

    def test_stale_health_ignores_github(self):
        h = health_gh({"denext-macos-arm64": entry(1, busy=1)},
                      {"macos": {"queued": 2, "oldest_at": NOW - 3600}}, age=11 * 60)
        api = FakeApi(runs=[{"id": 7, "status": "in_progress"}],
                      jobs={"7": [{"status": "in_progress", "labels": ["denext-macos-arm64"]}]})
        plan, _, _, _ = run(env("a=macos-arm64", h, RUNNER_MACOS_ARM=MAC), get=api)
        self.assertEqual(plan["a"], "macos-latest")
        self.assertEqual(pr.github_saturated(h, NOW), {})

    def test_never_strands_a_job_on_an_unhealthy_pool(self):
        sat = {"macos": {"queued": 2, "oldest_at": NOW - 900}}
        for pools in ({"denext-macos-arm64": dict(entry(1), ok=False)},
                      {"denext-macos-arm64": entry(1, busy=1, paused=True)},
                      {}):
            plan, rows, _, _ = run(env("a=macos-arm64", health_gh(pools, sat), RUNNER_MACOS_ARM=MAC))
            self.assertEqual(plan["a"], "macos-latest", pools)
            self.assertEqual(rows[0][2], "GitHub")

    def test_never_strands_a_job_on_a_stalled_pool(self):
        h = health_gh({"denext-macos-arm64": entry(1, busy=1)}, {"macos": {"queued": 1, "oldest_at": NOW - 900}})
        e = env("a=macos-arm64", h, RUNNER_MACOS_ARM=MAC)
        e["GITHUB_RUN_ATTEMPT"] = "2"
        api = FakeApi(attempt_jobs=[{"conclusion": "cancelled", "runner_name": None,
                                     "labels": ["self-hosted", "denext-macos-arm64"]}])
        self.assertEqual(run(e, get=api)[0]["a"], "macos-latest")

    def test_windows_and_ubuntu_families(self):
        h = health_gh({"denext-windows-x64": entry(1, busy=1), "denext-linux-arm64": entry(1, busy=1),
                       "denext-linux-x64": entry(1, busy=1)},
                      {"windows": {"queued": 1, "oldest_at": NOW - 240},
                       "ubuntu": {"queued": 4, "oldest_at": NOW - 600},
                       "macos": {"queued": 0, "oldest_at": None}})
        plan, rows, _, _ = run(env("w=windows-x64 u=linux-arm64@ubuntu-22.04-arm l=linux-x64", h,
                                   RUNNER_WINDOWS_X64=WIN, RUNNER_LINUX_ARM64=LinuxArm64Test.ARM,
                                   RUNNER_LINUX_X64=LINUX))
        self.assertEqual(plan, {"w": ours(WIN), "u": ours(LinuxArm64Test.ARM), "l": ours(LINUX)})
        self.assertIn("windows images", rows[0][3])
        self.assertIn("ubuntu images", rows[1][3])
        self.assertEqual(sorted(pr.LAST["saturated"]), ["ubuntu", "windows"])

    def test_only_the_jobs_own_family_counts(self):
        h = health_gh({"denext-windows-x64": entry(1, busy=1)}, {"macos": {"queued": 1, "oldest_at": NOW - 900}})
        plan, _, _, _ = run(env("w=windows-x64", h, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan["w"], "windows-latest")

    def test_no_github_key_is_unchanged(self):
        h = health({"denext-macos-arm64": entry(1, busy=1)})
        plan, rows, _, _ = run(env("a=macos-arm64", h, RUNNER_MACOS_ARM=MAC))
        self.assertEqual(plan["a"], "macos-latest")
        self.assertEqual(pr.LAST["saturated"], {})

    def test_malformed_github_is_unknown(self):
        for gh in ("nope", {"macos": "x"}, {"macos": {"queued": "2", "oldest_at": NOW - 900}},
                   {"macos": {"queued": 2, "oldest_at": None}}, {"macos": {"queued": True, "oldest_at": NOW - 900}}):
            self.assertEqual(pr.github_saturated(health_gh({}, gh), NOW), {}, gh)

    def test_saturation_does_not_count_against_the_macos_cap(self):
        h = health_gh({"denext-macos-arm64": entry(1, busy=1), "denext-macos-x64": entry(1, busy=1)},
                      {"macos": {"queued": 1, "oldest_at": NOW - 900}})
        e = env("a=macos-arm64 b=macos-x64+32g", h, RUNNER_MACOS_ARM=MAC, RUNNER_MACOS_X64=MACX)
        e["PICK_MACOS_CAP"] = "1"
        plan, rows, _, _ = run(e)
        # a waits on ours (saturated, cap untouched); b cannot fit ours: GitHub, the one hosted macOS job
        self.assertEqual(plan, {"a": ours(MAC), "b": "macos-15-intel"})
        self.assertIn("saturated", rows[0][3])


class MemoryTest(unittest.TestCase):
    ARM = '["self-hosted","denext-linux-arm64"]'

    def test_grammar(self):
        jobs, bad = pr.parse_jobs("a=linux-arm64+16g b=linux-arm64@ubuntu-22.04-arm+999g c=macos-arm64+12g! "
                                  "d=linux-x64 e=linux-x64@ubuntu-24.04! f=~a")
        self.assertEqual([(j["key"], j["image"], j["mem"], j["pinned"]) for j in jobs],
                         [("a", None, 16, False), ("b", "ubuntu-22.04-arm", 999, False), ("c", None, 12, True),
                          ("d", None, None, False), ("e", "ubuntu-24.04", None, True), ("f", None, None, False)])
        self.assertEqual(bad, [])

    def test_malformed_suffix(self):
        entries = ["a=linux-arm64+0g", "b=linux-arm64+16", "c=linux-arm64+1000g", "d=linux-arm64+16G",
                   "e=linux-arm64!+16g", "f=linux-arm64+016g", "g=~a+4g", "h=linux-arm64+g"]
        jobs, bad = pr.parse_jobs(" ".join(entries))
        self.assertEqual(jobs, [])
        self.assertEqual(bad, entries)
        _, _, _, warnings = run(env("b=linux-arm64+16", health({}), RUNNER_LINUX_ARM64=self.ARM))
        self.assertTrue(any("malformed or unknown job entry 'b=linux-arm64+16'" in w for w in warnings))

    def test_over_the_health_mem_goes_to_github_with_a_free_slot(self):
        h = health({"denext-linux-arm64": dict(entry(2), mem_gb=8)})
        plan, rows, _, _ = run(env("b=linux-arm64@ubuntu-22.04-arm+16g c=linux-arm64", h, RUNNER_LINUX_ARM64=self.ARM))
        self.assertEqual(plan, {"b": "ubuntu-22.04-arm", "c": ours(self.ARM)})
        self.assertEqual(rows[0][2:4], ("GitHub", "needs 16 GB, denext-linux-arm64 holds 8"))

    def test_over_capacity_ignores_saturation(self):
        h = health_gh({"denext-linux-arm64": dict(entry(1, busy=1), mem_gb=8)},
                      {"ubuntu": {"queued": 3, "oldest_at": NOW - 900}})
        plan, rows, _, _ = run(env("b=linux-arm64+16g", h, RUNNER_LINUX_ARM64=self.ARM))
        self.assertEqual(plan["b"], "ubuntu-24.04-arm")
        self.assertIn("needs 16 GB", rows[0][3])

    def test_under_the_health_mem_is_ours(self):
        h = health({"denext-linux-arm64": dict(entry(1), mem_gb=24)})
        plan, _, _, _ = run(env("b=linux-arm64+16g", h, RUNNER_LINUX_ARM64=self.ARM))
        self.assertEqual(plan["b"], ours(self.ARM))

    def test_stale_health_uses_the_static_default(self):
        h = health({"denext-linux-arm64": dict(entry(1), mem_gb=24)}, age=11 * 60)
        plan, rows, _, _ = run(env("b=linux-arm64+16g c=linux-arm64+8g", h, RUNNER_LINUX_ARM64=self.ARM))
        self.assertEqual(plan, {"b": "ubuntu-24.04-arm", "c": ours(self.ARM)})
        self.assertEqual(rows[0][3], "needs 16 GB, denext-linux-arm64 holds 8")
        self.assertEqual(pr.mem_capacity(pr.Pool("macos-x64", {}), None), 16)

    def test_pinned_over_capacity_stays_pinned_and_warns(self):
        h = health({"denext-linux-arm64-desktop": dict(entry(1), mem_gb=4)})
        plan, rows, _, warnings = run(env("g=linux-arm64-desktop+6g!", h))
        self.assertEqual(plan["g"], ["self-hosted", "denext-linux-arm64-desktop"])
        self.assertEqual(rows[0][2], "ours (pinned)")
        self.assertIn("holds 4 (may not fit)", rows[0][3])
        self.assertTrue(any("g needs 6 GB but is pinned to denext-linux-arm64-desktop, which holds 4" in w
                            for w in warnings))


class SaturatedOutputTest(unittest.TestCase):
    def test_saturated_output_line_and_summary(self):
        e = dict(os.environ)
        t = int(__import__("time").time())
        e.update(env("a=macos-arm64@macos-15 b=linux-arm64+16g", json.dumps(
            {"v": 1, "checked_at": t, "pools": {"denext-macos-arm64": entry(1, busy=1),
                                                "denext-linux-arm64": dict(entry(1), mem_gb=8)},
             "github": {"macos": {"queued": 1, "oldest_at": t - 7 * 60 - 5}}}),
            RUNNER_MACOS_ARM=MAC, RUNNER_LINUX_ARM64=LinuxArm64Test.ARM))
        e["PICK_DRY_RUN"] = "true"
        e.pop("GITHUB_EVENT_PATH", None)
        out = subprocess.run([sys.executable, "-I", pr.__file__], env=e, capture_output=True, text=True, check=True)
        lines = dict(l.split("=", 1) for l in out.stdout.splitlines())
        self.assertEqual(json.loads(lines["saturated"]), ["macos"])
        self.assertEqual(json.loads(lines["plan"]), {"a": ours(MAC), "b": "ubuntu-24.04-arm"})
        self.assertIn("GitHub saturated: macos (queued 7 min)", out.stderr)
        self.assertIn("| a | macos-arm64 | ours (waits) |", out.stderr)

    def test_no_saturation_prints_an_empty_list(self):
        e = dict(os.environ)
        e.update(env("a=windows-x64", "", RUNNER_WINDOWS_X64=WIN))
        e["PICK_DRY_RUN"] = "true"
        out = subprocess.run([sys.executable, "-I", pr.__file__], env=e, capture_output=True, text=True, check=True)
        lines = dict(l.split("=", 1) for l in out.stdout.splitlines())
        self.assertEqual(json.loads(lines["saturated"]), [])
        self.assertNotIn("GitHub saturated", out.stderr)


LINUX2204 = '["self-hosted","denext-linux-x64-2204"]'
WINARM = '["self-hosted","denext-windows-arm64"]'
ARM = '["self-hosted","denext-linux-arm64"]'


def records_of(e, **kw):
    """pick() with the real thresholds; (plan, rows, LAST['decisions'], LAST['tripwire'])."""
    plan, rows, _, warnings = run(e, **kw)
    return plan, rows, pr.LAST["decisions"], pr.LAST["tripwire"]


def assert_ours_first(test, plan, decisions):
    """The rule itself: a job goes to GitHub only when its measured wait exceeds its threshold (or it
    was not measurable: an unhealthy pool), and every job within the threshold is on our pool."""
    for key, r in decisions.items():
        hosted = isinstance(plan[key], str) and pr.GITHUB_IMAGE.match(plan[key])
        if hosted:
            test.assertTrue(r["wait"] is None or r["wait"] > r["threshold"], (key, r))
        if r["eligible"] and r["wait"] is not None and r["wait"] <= r["threshold"]:
            test.assertFalse(hosted, (key, r))
    # (a job past its threshold may still be ours: the macOS cap, saturation or the github-off switch)


class ExpectedWaitTest(unittest.TestCase):
    """Ours first: a job goes to GitHub only when its expected wait on our pool exceeds its family's
    threshold (Linux 10 min, Windows 15, macOS 20)."""

    def test_the_wait_formula(self):
        inf = float("inf")
        self.assertEqual(pr.expected_wait(3, 0, 1, 10), 0)
        self.assertEqual(pr.expected_wait(3, 2, 1, 10), 0)
        self.assertEqual(pr.expected_wait(3, 3, 1, 10), 5)  # all busy: half a job
        self.assertEqual(pr.expected_wait(3, 5, 1, 10), 5)  # two queued ahead: still the first wave
        self.assertEqual(pr.expected_wait(3, 6, 1, 10), 15)  # three ahead: the second wave
        self.assertEqual(pr.expected_wait(16, 16, 6, 30), 15)  # memory: two VMs hold the host
        self.assertEqual(pr.expected_wait(0, 0, 1, 10), inf)
        self.assertEqual(pr.expected_wait(4, 0, 8, 10), inf)

    def test_thresholds_parse(self):
        got, bad = pr.max_wait_thresholds("linux=5, macos=0 windows=x nope=3 junk")
        self.assertEqual(got, {"linux": 5, "windows": 15, "macos": 0})
        self.assertEqual(bad, ["windows=x", "nope=3", "junk"])
        self.assertEqual(pr.max_wait_thresholds("")[0], pr.DEFAULT_MAX_WAIT)
        _, _, _, warnings = run(env("a=windows-x64", health({"denext-windows-x64": entry(1)}), max_wait="windows=-1",
                                    RUNNER_WINDOWS_X64=WIN))
        self.assertTrue(any("PICK_MAX_WAIT" in w for w in warnings))

    def test_the_4_20_1_gate(self):
        # Brainwires/denext run 44537985240: 12 Linux jobs at once; the Linux host's three shared slots
        # idle. The old rule sent the 9 past the free slots to ubuntu-latest at once. Now 3 start, 3
        # more wait ~5 min for the first to end, and only the jobs past the 10-min threshold overflow.
        keys = ["coverage_1"] + [f"coverage_{i}" for i in range(2, 7)] + [
            "backend_integration", "check", "integration", "next_compat", "bundle_budget", "cargo_audit"]
        jobs = "coverage_1=linux-x64-docker " + " ".join(f"{k}=linux-x64" for k in keys[1:]) + (
            " windows_tests=windows-x64-fast windows_resolve=windows-x64 macos_tests=macos-arm64")
        h = health({"denext-linux-x64": entry(3), "denext-linux-x64-2204": entry(3), "denext-linux-x64-docker": entry(1),
                    "denext-windows-x64": entry(2), "denext-windows-x64-fast": entry(1), "denext-macos-arm64": entry(2)})
        plan, rows, decisions, trip = records_of(env(jobs, h, max_wait=None, RUNNER_LINUX_X64=LINUX, RUNNER_LINUX_X64_DOCKER=DOCKER,
                                                     RUNNER_LINUX_X64_SHARED_SLOTS="3", RUNNER_WINDOWS_X64=WIN,
                                                     RUNNER_MACOS_ARM=MAC))
        ours_linux = [k for k in keys if plan[k] != "ubuntu-latest"]
        self.assertEqual(ours_linux, keys[:6])
        self.assertEqual([r[2] for r in rows[:6]], ["ours"] * 3 + ["ours (waits)"] * 3)
        self.assertIn("expected wait 5 min", rows[3][3])
        self.assertIn("threshold 10 min", rows[3][3])
        self.assertIn("expected wait 15 min", rows[6][3])
        self.assertIn("exceeds it", rows[6][3])
        self.assertEqual(plan["windows_tests"], ["self-hosted", "denext-windows-x64-fast"])
        self.assertEqual(plan["windows_resolve"], ours(WIN))
        self.assertEqual(plan["macos_tests"], ours(MAC))
        self.assertEqual(trip, [])
        assert_ours_first(self, plan, decisions)

    def test_a_busy_pool_within_the_threshold_waits(self):
        h = health({"denext-windows-x64": entry(2, busy=2)})
        plan, rows, decisions, _ = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertEqual(rows[0][2], "ours (waits)")
        self.assertIn("denext-windows-x64 2/2 slots taken", rows[0][3])
        self.assertEqual(decisions["a"]["wait"], 10)
        self.assertEqual(decisions["a"]["threshold"], 15)

    def test_a_long_queue_overflows(self):
        # busy and queued overlap (a booting runner's job is both): 4 queued on 2 busy slots is 2 ahead
        h = health({"denext-windows-x64": entry(2, busy=2, queued=4)})
        plan, rows, _, _ = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": "windows-latest"})
        self.assertIn("expected wait 30 min", rows[0][3])

    def test_the_threshold_is_configurable(self):
        h = health({"denext-windows-x64": entry(2, busy=2, queued=4)})
        plan, _, _, _ = records_of(env("a=windows-x64", h, max_wait="windows=30", RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": ours(WIN)})

    def test_unhealthy_paused_unlisted_go_to_github(self):
        for pools in ({"denext-windows-x64": dict(entry(2, ok=False), reason="guest unreachable")},
                      {"denext-windows-x64": entry(2, paused=True)}, {}):
            plan, rows, decisions, trip = records_of(env("a=windows-x64", health(pools), max_wait=None,
                                                         RUNNER_WINDOWS_X64=WIN))
            self.assertEqual(plan, {"a": "windows-latest"}, pools)
            self.assertFalse(decisions["a"]["eligible"])
            self.assertEqual(trip, [])

    def test_a_long_job_on_the_busy_fast_host_goes_to_github_not_the_slow_host(self):
        h = health({"denext-windows-x64": entry(2, busy=1), "denext-windows-x64-fast": entry(1, busy=1)})
        plan, rows, _, _ = records_of(env("t=windows-x64-fast", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"t": "windows-latest"})  # ~60-min jobs: half of one is past 15 min
        self.assertIn("denext-windows-x64-fast 1/1 slots taken", rows[0][3])

    def test_the_mac_host_memory_counts_in_the_wait(self):
        h = health({"denext-macos-arm64": entry(2, busy=2), "denext-linux-arm64": entry(1), "denext-windows-arm64": entry(2)})
        e = env("build=linux-arm64@ubuntu-22.04-arm lint=macos-arm64", h, max_wait=None, RUNNER_LINUX_ARM64=ARM,
                RUNNER_MACOS_ARM=MAC, RUNNER_WINDOWS_ARM=WINARM)
        plan, rows, _, _ = records_of(e)
        # two VMs hold the host's 16 GB: ~15 min for the Linux job (past 10), within macOS's 20
        self.assertEqual(plan, {"build": "ubuntu-22.04-arm", "lint": ours(MAC)})
        self.assertIn("the mini-host pools 16/16 GB taken", rows[0][3])

    def test_label_equivalence_is_not_assumed(self):
        # linux-x64 and linux-x64-2204 are the same three containers (another image): an idle 2204
        # label is no free capacity, and a linux-x64 job never runs on the 22.04 image.
        h = health({"denext-linux-x64": entry(3, busy=3), "denext-linux-x64-2204": entry(3)})
        plan, rows, _, _ = records_of(env("a=linux-x64", h, max_wait=None, RUNNER_LINUX_X64=LINUX,
                                          RUNNER_LINUX_X64_2204=LINUX2204))
        self.assertEqual(plan, {"a": ours(LINUX)})
        self.assertEqual(rows[0][2], "ours (waits)")

    def test_stale_health_keeps_the_free_slot_rule(self):
        h = health({"denext-windows-x64": entry(9)}, age=11 * 60)
        plan, rows, decisions, trip = records_of(env("a=windows-x64 b=windows-x64", h, max_wait=None,
                                                     RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest"})
        self.assertIn("health stale", rows[1][3])
        self.assertIsNone(decisions["b"]["wait"])
        self.assertEqual(trip, [])

    def test_an_aged_value_measures_the_wait(self):
        h = health({"denext-windows-x64": entry(2, busy=2)}, age=20 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 15 * 60)})
        plan, rows, _, _ = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertIn("expected wait 10 min", rows[0][3])


class BurstTest(unittest.TestCase):
    VARS = {"RUNNER_LINUX_X64": LINUX, "RUNNER_LINUX_X64_DOCKER": DOCKER, "RUNNER_LINUX_X64_SHARED_SLOTS": "3",
            "RUNNER_WINDOWS_X64": WIN, "RUNNER_MACOS_ARM": MAC}

    def jobs(self, n=30):
        pools = ["linux-x64", "windows-x64", "macos-arm64", "linux-x64-docker", "linux-x64"]
        return " ".join(f"j{i}={pools[i % len(pools)]}" for i in range(n))

    def test_a_burst_of_30_against_pools_that_free_soon(self):
        # every pool full right now but nothing queued: the first jobs of each wait ~half a job
        h = health({"denext-linux-x64": entry(3, busy=3), "denext-linux-x64-docker": entry(1, busy=0),
                    "denext-windows-x64": entry(2, busy=2), "denext-macos-arm64": entry(2, busy=2)})
        plan, rows, decisions, trip = records_of(env(self.jobs(), h, max_wait=None, **self.VARS))
        assert_ours_first(self, plan, decisions)
        self.assertEqual(trip, [])
        hosted = {k for k, v in plan.items() if isinstance(v, str)}
        # Linux: 3 shared slots busy -> the next 3 jobs wait 5 min, the rest 15+ (past 10)
        linux = [k for k, r in decisions.items() if r["pool"].startswith("linux")]
        self.assertEqual(len([k for k in linux if k not in hosted]), 3)
        # Windows 2 busy: two wait 10 min (within 15), the third 30
        win = [k for k, r in decisions.items() if r["pool"] == "windows-x64"]
        self.assertEqual([k in hosted for k in win], [False, False] + [True] * (len(win) - 2))
        # macOS 2 VMs busy: two wait (10 min on the label, 15 on the host's memory; within 20)
        mac = [k for k, r in decisions.items() if r["pool"] == "macos-arm64"]
        self.assertEqual([k in hosted for k in mac][:2], [False, False])
        for key, _, decision, why, _ in rows:
            self.assertRegex(why, r"expected wait [0-9.inf]+ min .*threshold [0-9.]+ min", key)

    def test_a_burst_of_30_on_idle_pools_spreads_before_overflowing(self):
        h = health({"denext-linux-x64": entry(3), "denext-linux-x64-docker": entry(1),
                    "denext-windows-x64": entry(2), "denext-macos-arm64": entry(2)})
        plan, _, decisions, trip = records_of(env(self.jobs(), h, max_wait=None, **self.VARS))
        assert_ours_first(self, plan, decisions)
        self.assertEqual(trip, [])
        win = [k for k, r in decisions.items() if r["pool"] == "windows-x64"]
        # 2 free, 2 more wait 10 min, then 30: four on our pool
        self.assertEqual(sum(1 for k in win if not isinstance(plan[k], str)), 4)
        linux = [k for k, r in decisions.items() if r["pool"].startswith("linux")]
        self.assertEqual(sum(1 for k in linux if not isinstance(plan[k], str)), 6)

    def test_a_burst_of_30_with_github_off_never_goes_to_github(self):
        h = json.dumps({"v": 1, "checked_at": NOW - 60, "github_off": ["macos", "ubuntu", "windows"],
                        "pools": {"denext-linux-x64": entry(3, busy=3), "denext-linux-x64-docker": entry(1, busy=1),
                                  "denext-windows-x64": entry(2, busy=2), "denext-macos-arm64": entry(2, busy=2)}})
        plan, _, _, trip = records_of(env(self.jobs(), h, max_wait=None, **self.VARS))
        self.assertFalse([k for k, v in plan.items() if isinstance(v, str)])
        self.assertEqual(trip, [])

    def test_macos_past_its_threshold_still_obeys_the_cap_and_the_switch(self):
        h = health({"denext-macos-arm64": entry(2, busy=2, queued=4)})
        e = env(" ".join(f"m{i}=macos-arm64" for i in range(5)), h, max_wait=None, RUNNER_MACOS_ARM=MAC)
        e["PICK_MACOS_CAP"] = "2"
        plan, _, _, _ = records_of(e)
        self.assertEqual(list(plan.values()), ["macos-latest"] * 2 + [ours(MAC)] * 2 + ["macos-latest"])
        off = json.dumps({"v": 1, "checked_at": NOW - 60, "github_off": ["macos"],
                          "pools": {"denext-macos-arm64": entry(2, busy=2, queued=40)}})
        plan, _, _, _ = records_of(env(" ".join(f"m{i}=macos-arm64" for i in range(5)), off, max_wait=None,
                                       RUNNER_MACOS_ARM=MAC))
        self.assertEqual(list(plan.values()), [ours(MAC)] * 5)


def live_entry(slots, busy=0, live=None, job_s=None, queued=None):
    e = entry(slots, busy=busy, queued=queued)
    if live is not None:
        e["live"] = live
    if job_s is not None:
        e["job_s"] = job_s
    return e


class LiveWaitTest(unittest.TestCase):
    """The expected wait from the fleet's live per-slot ETAs (`live`, `job_s`), the table only as fallback."""

    def test_free_slots_wait_nothing(self):
        self.assertEqual(pr.live_wait(3, 1, 1, [NOW + 600], 900, 10, NOW), 0)
        self.assertEqual(pr.live_wait(3, 0, 0, [], None, 10, NOW), 0)

    def test_all_busy_waits_for_the_earliest_eta(self):
        self.assertEqual(pr.live_wait(2, 2, 2, [NOW + 180, NOW + 1200], 900, 20, NOW), 3)

    def test_a_job_past_its_eta_ends_soon_not_now(self):
        self.assertEqual(pr.live_wait(1, 1, 1, [NOW - 300], 900, 20, NOW), 1)  # OVERRUN_FLOOR
        self.assertEqual(pr.live_wait(2, 2, 2, [NOW - 900, NOW + 30], 900, 20, NOW), 1)

    def test_busy_slots_without_an_eta_are_half_a_job(self):
        # 2 busy, one ETA (20 min): the other frees after job_s / 2 = 7.5 min
        self.assertEqual(pr.live_wait(2, 2, 2, [NOW + 1200], 900, 20, NOW), 7.5)
        # no job_s: the table's minutes / 2
        self.assertEqual(pr.live_wait(2, 2, 2, [NOW + 1200], None, 20, NOW), 10)

    def test_jobs_ahead_drain_with_job_s(self):
        # 2 slots free at 2 and 4 min; 3 queued ahead, 10 min each: 2->12, 4->14, 12->22; ours at 14
        self.assertEqual(pr.live_wait(2, 5, 2, [NOW + 120, NOW + 240], 600, 30, NOW), 14)
        # one free slot and one ahead: the free slot goes to the job ahead, ours waits for the busy one
        self.assertEqual(pr.live_wait(2, 2, 1, [NOW + 300], 600, 30, NOW), 5)
        # no job_s: the jobs ahead take the table's minutes
        self.assertEqual(pr.live_wait(1, 2, 1, [NOW + 120], None, 10, NOW), 12)

    def test_no_capacity_is_infinite(self):
        self.assertEqual(pr.live_wait(0, 0, 0, [], 600, 10, NOW), float("inf"))
        self.assertEqual(pr.live_wait(2, 0, 0, [], 600, 10, NOW, need=3), float("inf"))

    def test_busy_past_the_capacity_keeps_the_earliest(self):
        self.assertEqual(pr.live_wait(1, 2, 2, [NOW + 120, NOW + 900], 600, 10, NOW), 2)

    def test_malformed_live_falls_back_to_the_table(self):
        got = pr.live_entries({"a": {"live": [[1, 2], [3]]}, "b": {"live": "x"}, "c": {"busy": 1},
                               "d": {"busy": 1, "live": [[1, NOW + 9], [0, NOW + 3]], "job_s": True}})
        self.assertEqual(got, {"d": (1, [NOW + 3, NOW + 9], None)})

    def test_the_pick_uses_the_live_etas_when_fresh(self):
        # windows-x64, both slots busy: the table says 10 min (half a 20-min job); the ETAs say 2
        h = health({"denext-windows-x64": live_entry(2, busy=2, live=[[NOW - 600, NOW + 120], [NOW - 60, NOW + 1500]],
                                                     job_s=1200)})
        plan, rows, decisions, trip = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertEqual(decisions["a"]["wait"], 2)
        self.assertEqual(decisions["a"]["source"], "live")
        self.assertIn("expected wait 2 min (denext-windows-x64 2/2 slots taken; live, ~20 min/job)", rows[0][3])
        self.assertEqual(trip, [])

    def test_the_pick_overflows_on_late_etas(self):
        # the table would wait 10 min (within 15); the live ETAs say 25 min: GitHub
        h = health({"denext-windows-x64": live_entry(2, busy=2, live=[[NOW, NOW + 1500], [NOW, NOW + 1600]],
                                                     job_s=1200)})
        plan, rows, decisions, trip = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": "windows-latest"})
        self.assertEqual(decisions["a"]["wait"], 25)
        self.assertIn("live", rows[0][3])
        self.assertEqual(trip, [])

    def test_this_runs_own_placements_queue_behind_the_etas(self):
        # 1 free slot, 1 busy freeing in 3 min, job_s 5 min: a free, b 3 min, c 5 (the free slot again), d 8
        h = health({"denext-windows-x64": live_entry(2, busy=1, live=[[NOW, NOW + 180]], job_s=300)})
        plan, _, decisions, _ = records_of(env("a=windows-x64 b=windows-x64 c=windows-x64 d=windows-x64", h,
                                               max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual([decisions[k]["wait"] for k in "abcd"], [0, 3, 5, 8])

    def test_the_parent_label_is_measured_live_too(self):
        h = health({"denext-windows-x64": live_entry(2, busy=2, live=[[NOW, NOW + 1800], [NOW, NOW + 2400]], job_s=1200),
                    "denext-windows-x64-fast": live_entry(1, busy=0, live=[], job_s=3600)})
        plan, rows, decisions, _ = records_of(env("t=windows-x64-fast", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"t": "windows-latest"})
        self.assertEqual(decisions["t"]["wait"], 30)
        self.assertIn("denext-windows-x64 2/2 slots taken; live", rows[0][3])

    def test_no_live_field_uses_the_table(self):
        h = health({"denext-windows-x64": live_entry(2, busy=2, job_s=60)})
        plan, rows, decisions, _ = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(decisions["a"]["wait"], 10)
        self.assertEqual(decisions["a"]["source"], "table")
        self.assertIn("table, ~20 min/job", rows[0][3])

    def test_an_aged_value_uses_the_table(self):
        h = health({"denext-windows-x64": live_entry(2, busy=2, live=[[NOW, NOW + 60], [NOW, NOW + 60]], job_s=60)},
                   age=20 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 15 * 60)})
        plan, rows, decisions, _ = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertEqual(decisions["a"]["wait"], 10)
        self.assertEqual(decisions["a"]["source"], "table")

    def test_a_stale_value_uses_the_free_slot_rule(self):
        h = health({"denext-windows-x64": live_entry(2, busy=2, live=[[NOW, NOW + 60], [NOW, NOW + 60]], job_s=60)},
                   age=11 * 60)
        plan, rows, decisions, _ = records_of(env("a=windows-x64 b=windows-x64", h, max_wait=None,
                                                  RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": ours(WIN), "b": "windows-latest"})
        self.assertIsNone(decisions["b"]["wait"])
        self.assertIsNone(decisions["b"]["source"])
        self.assertIn("health stale", rows[1][3])

    def test_the_host_group_keeps_the_table(self):
        # the label is live (a free slot), the Mac host's memory (two VMs) binds on the table
        h = health({"denext-macos-arm64": live_entry(2, busy=2, live=[[NOW, NOW + 60], [NOW, NOW + 60]], job_s=600),
                    "denext-linux-arm64": live_entry(1, live=[], job_s=600), "denext-windows-arm64": entry(2)})
        e = env("build=linux-arm64", h, max_wait=None, RUNNER_LINUX_ARM64=ARM, RUNNER_MACOS_ARM=MAC,
                RUNNER_WINDOWS_ARM=WINARM)
        _, rows, decisions, _ = records_of(e)
        self.assertEqual(decisions["build"]["source"], "table")
        self.assertIn("the mini-host pools 16/16 GB taken; table", rows[0][3])


class TripwireTest(unittest.TestCase):
    def test_flags_a_github_placement_within_the_threshold(self):
        records = {"a": {"pool": "linux-x64", "label": "denext-linux-x64", "wait": 5.0, "threshold": 10, "eligible": True},
                   "b": {"pool": "linux-x64", "label": "denext-linux-x64", "wait": 15.0, "threshold": 10, "eligible": True},
                   "c": {"pool": "linux-x64", "label": "denext-linux-x64", "wait": 0.0, "threshold": 10, "eligible": False},
                   "d": {"pool": "linux-x64", "label": "denext-linux-x64", "wait": 0.0, "threshold": 10, "eligible": True}}
        plan = {"a": "ubuntu-latest", "b": "ubuntu-latest", "c": "ubuntu-latest", "d": ours(LINUX)}
        self.assertEqual([t["key"] for t in pr.tripwires(plan, records)], ["a"])

    def test_a_correct_pick_has_none_and_logs_each_decision(self):
        h = health({"denext-windows-x64": entry(1, busy=1, queued=3)})
        plan, _, decisions, trip = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        self.assertEqual(plan, {"a": "windows-latest"})
        self.assertEqual(trip, [])
        self.assertEqual(decisions["a"]["numbers"]["busy"], 1)
        self.assertEqual(decisions["a"]["numbers"]["queued"], 3)
        self.assertEqual(decisions["a"]["runs_on"], "windows-latest")

    def test_a_rule_that_regresses_trips_it(self):
        # Simulate the old rule's bug: a decision that sends an in-threshold job to GitHub.
        orig = pr.allocate

        def buggy(*a, **kw):
            plan, rows = orig(*a, **kw)
            return {k: "windows-latest" for k in plan}, rows
        pr.allocate = buggy
        try:
            h = health({"denext-windows-x64": entry(2, busy=1, queued=0)})
            _, _, _, trip = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN))
        finally:
            pr.allocate = orig
        self.assertEqual(len(trip), 1)
        t = trip[0]
        self.assertEqual((t["key"], t["label"], t["wait"], t["threshold"]), ("a", "denext-windows-x64", 0.0, 15))
        self.assertEqual((t["repo"], t["run_id"]), ("Brainwires/denext", "100"))
        self.assertEqual((t["numbers"]["slots"], t["numbers"]["busy"], t["numbers"]["queued"]), (2, 1, 0))

    def test_the_picker_error_fallback_trips_it(self):
        orig = pr.free_slots
        pr.free_slots = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            plan, _, _, trip = records_of(env("a=windows-x64 b=windows-arm64! c=linux-x64", health({}),
                                              max_wait=None, RUNNER_WINDOWS_X64=WIN))
        finally:
            pr.free_slots = orig
        self.assertEqual([t["key"] for t in trip], ["a"])  # c's pool is not in use; b is pinned
        self.assertIsNone(trip[0]["wait"])
        self.assertIn("boom", trip[0]["reason"])

    def test_the_cli_prints_an_error_annotation_and_the_output(self):
        import contextlib
        import io
        from unittest import mock
        trip = [{"key": "a", "pool": "windows-x64", "label": "denext-windows-x64", "wait": 0.0, "threshold": 15,
                 "repo": "Brainwires/denext", "run_id": "100", "attempt": "1",
                 "numbers": {"slots": 2, "healthy_slots": 2, "busy": 1, "queued": 0}}]
        out, err = io.StringIO(), io.StringIO()
        e = env("a=windows-x64", health({"denext-windows-x64": entry(2, busy=1)}), max_wait=None,
                RUNNER_WINDOWS_X64=WIN, PICK_DRY_RUN="true")
        e["PICK_DRY_RUN"] = "true"
        with mock.patch.dict(os.environ, e, clear=False), mock.patch.object(pr, "tripwires", lambda *a: trip), \
                mock.patch.object(pr.time, "time", lambda: NOW), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            pr.main([])
        lines = dict(l.split("=", 1) for l in out.getvalue().splitlines())
        self.assertEqual(json.loads(lines["tripwire"])[0]["key"], "a")
        self.assertIn("::error title=pick-runners tripwire::Brainwires/denext run 100 (attempt 1) job a went to GitHub"
                      " while denext-windows-x64 was healthy with expected wait 0.0 min (threshold 15 min; slots 2,"
                      " busy 1, queued ?)", err.getvalue())
        self.assertIn("TRIPWIRE", err.getvalue())
        self.assertIn('pick-runners decision {"pool":"windows-x64"', err.getvalue())

    def test_the_cli_prints_an_empty_tripwire(self):
        e = dict(os.environ)
        e.update(env("a=windows-x64", "", RUNNER_WINDOWS_X64=WIN))
        e["PICK_DRY_RUN"] = "true"
        out = subprocess.run([sys.executable, "-I", pr.__file__], env=e, capture_output=True, text=True, check=True)
        lines = dict(l.split("=", 1) for l in out.stdout.splitlines())
        self.assertEqual(json.loads(lines["tripwire"]), [])
        self.assertNotIn("tripwire::", out.stderr)


# ---- the spill bound: a job whose start estimate on its class's pools exceeds spillAfterMinutes ----

SPILL_ROUTING = {"v": 1, "classes": [
    {"id": "windows-x64", "labels": ["denext-windows-x64"], "github": True, "order": ["win-x64-brain", "win-x64"]},
    {"id": "macos-x64", "labels": ["denext-macos-x64"], "github": True, "order": ["mac-x64"]},
    {"id": "macos-arm64", "labels": ["denext-macos-arm64"], "github": True, "order": ["mac-arm"]},
], "pools": {"win-x64": {"onlyWhenIdle": ["mac-x64"]}}}


def host(label, slots=1, busy=0, ok=True, paused=False, **extra):
    return dict({"label": label, "ok": ok, "slots": slots, "busy": busy, "paused": paused}, **extra)


def spill_health(win_live, mac_live, mac_queued=0, win_job_s=1200, mac_job_s=1800, brain=None, win=None,
                 routing=None, age=60, macx=None, extra_pools=None, extra_hosts=None):
    """The Windows label (brain + the VM, 1 slot each) and the Intel Mac the VM yields to. `win_live` /
    `mac_live`: [[start, eta], ...] of the labels' busy slots (None: no `live`)."""
    win_busy = len(win_live or [])
    pools = {"denext-windows-x64": live_entry(2, busy=win_busy, live=win_live, job_s=win_job_s),
             "denext-macos-x64": live_entry(1, busy=len(mac_live or []), live=mac_live, job_s=mac_job_s,
                                            queued=mac_queued)}
    pools.update(extra_pools or {})
    hosts = {"win-x64-brain": brain or host("denext-windows-x64", busy=min(win_busy, 1)),
             "win-x64": win or host("denext-windows-x64", busy=max(0, win_busy - 1)),
             "mac-x64": macx or host("denext-macos-x64", busy=len(mac_live or []))}
    hosts.update(extra_hosts or {})
    return json.dumps({"v": 1, "checked_at": NOW - age, "pools": pools, "hosts": hosts,
                       "routing": SPILL_ROUTING if routing is None else routing})


class SpillTest(unittest.TestCase):
    """Jobs never wait unboundedly on our pools: past the class's spillAfterMinutes, GitHub at pick time."""

    def pick(self, h, jobs="a=windows-x64", max_wait=None, **kw):
        variables = {"RUNNER_WINDOWS_X64": WIN, "RUNNER_MACOS_X64": MACX, "RUNNER_MACOS_ARM": MAC}
        variables.update(kw)
        return records_of(env(jobs, h, max_wait=max_wait, **variables))

    def test_the_87_minute_wait_spills(self):
        # 2026-11-20: brain busy with a long job (ETA 70 min), the VM free but held by onlyWhenIdle while
        # the Intel Mac runs a job (ETA 45 min). The merged label shows a free slot: expected wait 0.
        h = spill_health([[NOW - 600, NOW + 70 * 60]], [[NOW - 600, NOW + 45 * 60]])
        plan, rows, decisions, trip = self.pick(h, "a=windows-x64 b=windows-x64")
        self.assertEqual(plan, {"a": "windows-latest", "b": "windows-latest"})
        self.assertEqual(decisions["a"]["wait"], 0)  # what the expected wait alone saw
        self.assertEqual(decisions["a"]["spill"]["estimate"], 45)
        self.assertTrue(decisions["a"]["spill"]["over"])
        self.assertEqual(decisions["a"]["spill"]["held"], ["win-x64"])
        self.assertEqual(decisions["a"]["spill"]["class"], "windows-x64")
        self.assertTrue(rows[0][3].startswith("spill: "), rows[0][3])
        self.assertIn("start estimate 45 min on win-x64-brain > win-x64", rows[0][3])
        self.assertIn("win-x64 held until mac-x64 idle (onlyWhenIdle", rows[0][3])
        self.assertIn("spill bound 20 min", rows[0][3])
        self.assertEqual(decisions["b"]["spill"]["estimate"], 45)  # `a` went to GitHub: it took no slot
        self.assertEqual(trip, [])  # a spill is not a tripwire, although the expected wait read 0

    def test_the_mac_idle_releases_the_hold(self):
        h = spill_health([[NOW - 600, NOW + 70 * 60]], [])
        plan, rows, decisions, _ = self.pick(h)
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertEqual(decisions["a"]["spill"]["estimate"], 0)
        self.assertFalse(decisions["a"]["spill"]["over"])
        self.assertEqual(rows[0][2], "ours")

    def test_a_hold_that_ends_soon_keeps_ours(self):
        h = spill_health([[NOW - 600, NOW + 70 * 60]], [[NOW - 600, NOW + 5 * 60]])
        plan, rows, decisions, _ = self.pick(h)
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertEqual(decisions["a"]["spill"]["estimate"], 5)
        self.assertIn("start estimate 5 min", rows[0][3])
        self.assertIn("spill bound 20 min", rows[0][3])

    def test_the_held_pools_queue_drains_first(self):
        # the Mac: 1 busy (10 min) and 1 queued (30 min): the VM is held 40 min; brain frees at 70
        h = spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 600]], mac_queued=1)
        _, _, decisions, _ = self.pick(h)
        self.assertEqual(decisions["a"]["spill"]["estimate"], 40)
        # a paused Mac's queue does not count: only its running job holds the VM
        h = spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 600]], mac_queued=1,
                         macx=host("denext-macos-x64", busy=1, paused=True))
        _, _, decisions, _ = self.pick(h)
        self.assertEqual(decisions["a"]["spill"]["estimate"], 10)

    def test_the_class_bound_is_read_and_defaults_to_20(self):
        h = spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]])
        routing = json.loads(json.dumps(SPILL_ROUTING))
        routing["classes"][0]["spillAfterMinutes"] = 60
        plan, _, decisions, _ = self.pick(spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]], routing=routing))
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertEqual(decisions["a"]["spill"]["after"], 60)
        for bad in (True, -1, 2000, "20"):
            routing["classes"][0]["spillAfterMinutes"] = bad
            _, _, decisions, _ = self.pick(spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]], routing=routing))
            self.assertEqual(decisions["a"]["spill"]["after"], 20, bad)
        routing["classes"][0]["spillAfterMinutes"] = 0  # never
        plan, rows, decisions, _ = self.pick(spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]], routing=routing))
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertFalse(decisions["a"]["spill"]["over"])
        self.assertIn("spillAfterMinutes is 0", decisions["a"]["spill"]["why"])
        self.assertEqual(pr.spill_context(h, NOW, "a/b", "").rules["denext-windows-x64"]["after"], 20)

    def test_unknown_estimates_keep_ours(self):
        long_brain, long_mac = [[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]]
        cases = {
            "no live ETAs for denext-windows-x64": spill_health(None, long_mac, brain=host("denext-windows-x64", busy=1)),
            "whose busy slots have no ETA": spill_health(long_brain, None, macx=host("denext-macos-x64", busy=1)),
            "whose 1 queued job(s) have no duration": spill_health(long_brain, long_mac, mac_queued=1, mac_job_s=None),
            "the health does not list win-x64": json.dumps(dict(json.loads(spill_health(long_brain, long_mac)),
                                                                hosts={"win-x64-brain": host("denext-windows-x64", busy=1),
                                                                       "mac-x64": host("denext-macos-x64", busy=1)})),
            "held by a gate (disk)": spill_health(long_brain, long_mac, win=host("denext-windows-x64", state="blocked",
                                                                                code="disk")),
        }
        for why, h in cases.items():
            plan, rows, decisions, _ = self.pick(h)
            self.assertEqual(plan, {"a": ours(WIN)}, why)
            self.assertIsNone(decisions["a"]["spill"]["estimate"], why)
            self.assertIn(why, decisions["a"]["spill"]["why"])
            self.assertIn("start estimate unknown", rows[0][3])

    def test_the_schedulers_intel_first_state_is_the_rule(self):
        # pool-state's blocked/intel-first is the onlyWhenIdle hold: estimated from the rule
        h = spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]],
                         win=host("denext-windows-x64", state="blocked", code="intel-first"))
        plan, _, decisions, _ = self.pick(h)
        self.assertEqual(plan, {"a": "windows-latest"})
        self.assertEqual(decisions["a"]["spill"]["estimate"], 45)
        # without the published rule the end is unknown: ours
        routing = dict(SPILL_ROUTING, pools={})
        h = spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]], routing=routing,
                         win=host("denext-windows-x64", state="blocked", code="intel-first"))
        plan, _, decisions, _ = self.pick(h)
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertIsNone(decisions["a"]["spill"]["estimate"])

    def test_paused_disabled_and_down_hosts_start_nothing(self):
        for win in (host("denext-windows-x64", paused=True), host("denext-windows-x64", ok=False),
                    host("denext-windows-x64", state="down", code="host-unreachable"),
                    host("denext-windows-x64", state="paused", code="disabled")):
            h = spill_health([[NOW, NOW + 30 * 60]], [], win=win)
            plan, rows, decisions, trip = self.pick(h)
            self.assertEqual(plan, {"a": "windows-latest"}, win)
            self.assertEqual(decisions["a"]["spill"]["estimate"], 30)
            self.assertEqual(decisions["a"]["spill"]["held"], ["win-x64"])
            self.assertIn("win-x64 cannot start jobs", rows[0][3])
            self.assertEqual(trip, [])
        # every host of the class out: no pool can start it (JSON has no infinity: estimate null, over)
        h = spill_health([], [], win=host("denext-windows-x64", paused=True),
                         brain=host("denext-windows-x64", state="down"))
        plan, _, decisions, _ = self.pick(h)
        self.assertEqual(plan, {"a": "windows-latest"})
        self.assertIsNone(decisions["a"]["spill"]["estimate"])
        self.assertTrue(decisions["a"]["spill"]["over"])

    def test_this_runs_own_jobs_queue_into_the_estimate(self):
        # brain frees in 10 min, the VM is free, 20-min jobs; threshold out of the way (windows=60):
        # a 0 (the VM), b 10 (brain), c 20 (the VM again), d 30: past 20, GitHub
        h = spill_health([[NOW, NOW + 600]], [])
        plan, _, decisions, _ = self.pick(h, "a=windows-x64 b=windows-x64 c=windows-x64 d=windows-x64",
                                          max_wait="windows=60")
        self.assertEqual([decisions[k]["spill"]["estimate"] for k in "abcd"], [0, 10, 20, 30])
        self.assertEqual([plan[k] for k in "abcd"], [ours(WIN)] * 3 + ["windows-latest"])

    def test_aged_and_stale_values_never_spill(self):
        h = spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]], age=20 * 60)
        api = FakeApi(run={"run_started_at": iso(NOW - 15 * 60)})
        plan, _, decisions, _ = records_of(env("a=windows-x64", h, max_wait=None, RUNNER_WINDOWS_X64=WIN), get=api)
        self.assertIsNone(decisions["a"]["spill"])
        self.assertEqual(plan, {"a": ours(WIN)})
        self.assertIsNone(pr.spill_context(spill_health([], [], age=11 * 60), NOW, "a/b", ""))
        self.assertIsNone(pr.spill_context(health({}), NOW, "a/b", ""))  # no routing, no hosts

    def test_no_class_with_github_never_spills(self):
        # a class without GitHub keeps its routing hold; a label no class fits has no bound
        routing = {"v": 1, "classes": [{"labels": ["denext-windows-x64"], "github": False, "order": ["win-x64-brain"]}],
                   "pools": SPILL_ROUTING["pools"]}
        h = spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]], routing=routing)
        plan, rows, decisions, _ = self.pick(h)
        self.assertEqual((plan, rows[0][2]), ({"a": ours(WIN)}, "ours (routing)"))
        routing = {"v": 1, "classes": [{"labels": ["denext-macos-x64"], "github": True, "order": ["mac-x64"]}]}
        plan, _, decisions, _ = self.pick(spill_health([[NOW, NOW + 70 * 60]], [], routing=routing))
        self.assertIsNone(decisions["a"]["spill"])
        # a class for another repository does not apply
        routing = dict(SPILL_ROUTING, classes=[dict(SPILL_ROUTING["classes"][0], repo="Brainwires/deno")])
        self.assertEqual(pr.spill_context(spill_health([], [], routing=routing), NOW, "Brainwires/denext", "").rules, {})

    def test_the_github_off_switch_and_saturation_still_hold(self):
        base = json.loads(spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]]))
        plan, rows, _, _ = self.pick(json.dumps(dict(base, github_off=["windows"])))
        self.assertEqual((plan, rows[0][2]), ({"a": ours(WIN)}, "ours (waits)"))
        self.assertIn("GitHub windows is off", rows[0][3])
        sat = dict(base, github={"windows": {"queued": 4, "oldest_at": NOW - 600}})
        plan, rows, _, _ = self.pick(json.dumps(sat))
        self.assertEqual((plan, rows[0][2]), ({"a": ours(WIN)}, "ours (waits)"))
        self.assertIn("saturated", rows[0][3])

    def test_the_macos_cap_still_holds(self):
        # the Intel Mac busy 45 min (one slot): its own class spills, but past the run's cap it waits
        h = spill_health([], [[NOW, NOW + 45 * 60]])
        e = env("m=macos-x64 n=macos-x64", h, max_wait="macos=60", RUNNER_MACOS_X64=MACX, RUNNER_WINDOWS_X64=WIN)
        e["PICK_MACOS_CAP"] = "1"
        plan, rows, decisions, _ = records_of(e)
        self.assertEqual(plan["m"], "macos-15-intel")
        self.assertTrue(decisions["m"]["spill"]["over"])
        self.assertEqual((plan["n"], rows[1][2]), (ours(MACX), "ours (waits)"))
        self.assertIn("cap 1", rows[1][3])

    def test_the_cli_logs_the_spill(self):
        e = dict(os.environ)
        e.update(env("a=windows-x64", spill_health([[NOW, NOW + 70 * 60]], [[NOW, NOW + 45 * 60]]), max_wait=None,
                     RUNNER_WINDOWS_X64=WIN))
        e["PICK_DRY_RUN"] = "true"
        # the script reads the clock: re-date the value to now
        v = json.loads(json.loads(e["PICK_VARS"])["DENEXT_POOL_HEALTH"])
        shift = int(__import__("time").time()) - NOW
        v["checked_at"] += shift
        for p in v["pools"].values():
            p["live"] = [[s + shift, t + shift] for s, t in p.get("live", [])]
        e["PICK_VARS"] = json.dumps({"RUNNER_WINDOWS_X64": WIN, "DENEXT_POOL_HEALTH": json.dumps(v)})
        out = subprocess.run([sys.executable, "-I", pr.__file__], env=e, capture_output=True, text=True, check=True)
        lines = dict(l.split("=", 1) for l in out.stdout.splitlines())
        self.assertEqual(json.loads(lines["plan"]), {"a": "windows-latest"})
        self.assertEqual(json.loads(lines["tripwire"]), [])
        self.assertIn('"spill":{"class":"windows-x64","after":20,"estimate":45', out.stderr)
        self.assertIn("Spilled to GitHub (start estimate past the class's spill bound): a (45 min > 20)", out.stderr)

    def test_hold_release_unit(self):
        ctx = pr.spill_context(spill_health([], [[NOW, NOW - 300]]), NOW, "a/b", "")
        self.assertEqual(pr.hold_release("win-x64", ctx, NOW)[0], pr.OVERRUN_FLOOR)  # past its ETA: ends soon
        self.assertEqual(pr.hold_release("win-x64-brain", ctx, NOW), (0.0, ""))  # no rule


if __name__ == "__main__":
    unittest.main()
