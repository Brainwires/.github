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


def env(jobs, health_value=None, **variables):
    v = dict(variables)
    if health_value is not None:
        v["DENEXT_POOL_HEALTH"] = health_value
    return {
        "PICK_JOBS": jobs,
        "PICK_VARS": json.dumps(v),
        "GITHUB_REPOSITORY": "Brainwires/denext",
        "GITHUB_RUN_ID": "100",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_EVENT_NAME": "push",
    }


class FakeApi:
    """GET answers by path prefix; records every call."""

    def __init__(self, runs=(), jobs=None, runners=(), attempt_jobs=(), fail=False):
        self.runs, self.jobs, self.runners = list(runs), jobs or {}, list(runners)
        self.attempt_jobs, self.fail, self.calls = list(attempt_jobs), fail, []

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
        self.assertIn("no free slot", rows[0][3])

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

    def test_biscuits_share_one_capacity(self):
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
    """Long Windows jobs ask for the fast host's own label; brain never takes them."""

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
        self.assertEqual(plan, {"t": "windows-latest", "s": ours(WIN)})  # the short job takes brain

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
        self.assertEqual(self.sleeps, [5, 5])
        self.assertTrue(any("waited" in w for w in warnings))

    def test_a_sibling_with_no_jobs_yet_is_waited_for(self):
        self.setup([])

        def started():
            self.api.jobs["99"] = [{"name": "pick runners", "status": "completed", "labels": ["ubuntu-latest"]},
                                   {"name": "build", "status": "queued", "labels": ["self-hosted", "denext-macos-x64"]}]
        self.on_sleep = started
        plan, _ = self.go()
        self.assertEqual(plan["d"], "macos-15-intel")

    def test_no_wait_when_the_sibling_already_picked(self):
        self.setup([{"name": "pick runners", "status": "completed", "labels": ["ubuntu-latest"]},
                    {"name": "build", "status": "in_progress", "labels": ["self-hosted", "denext-macos-x64"]}])
        plan, _ = self.go()
        self.assertEqual(self.sleeps, [])
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
        self.assertEqual(self.sleeps, [])

    def test_gives_up_after_the_limit(self):
        self.setup([{"name": "pick runners", "status": "in_progress", "labels": ["ubuntu-latest"]}])
        plan, warnings = self.go(PICK_SETTLE="20")
        self.assertEqual(sum(self.sleeps), 25)  # 4 looks x 5 s, then 5 s for the jobs
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
        h = health({"denext-windows-x64": entry(2)})
        e = env("a=windows-x64", h, RUNNER_WINDOWS_X64=WIN)
        e.update(GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=self._event("Brainwires/denext"))
        plan, _, _, _ = run(e)
        self.assertEqual(plan["a"], ours(WIN))

    def test_pull_request_target_is_a_fork(self):
        e = env("a=windows-x64", health({"denext-windows-x64": entry(2)}), RUNNER_WINDOWS_X64=WIN)
        e["GITHUB_EVENT_NAME"] = "pull_request_target"
        self.assertEqual(run(e)[0]["a"], "windows-latest")


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


if __name__ == "__main__":
    unittest.main()
