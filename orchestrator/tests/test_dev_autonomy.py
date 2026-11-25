"""Autonomy loop tests (T-DEV-013). Synthetic data only; temp dirs; fake git runner and fake clock; no network.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_autonomy.py -v
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import autonomy as au
from dev_supervisor.state_store import StateStore
from dev_supervisor.workers import ResultRecord, Worker, WorkerRegistry, WorkerStatus

MAIN_SID = "11111111-1111-4111-8111-111111111111"


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def task(tid, status="PLANNED", deps=(), paths=(), owner="claude:repository-coordinator"):
    return {"id": tid, "status": status, "depends_on": list(deps), "owned_paths": list(paths), "owner": owner}


class FakeGit:
    """Fake read-only git: waves -> {wave_id: {"merged": bool, "files": [paths]}}; missing wave = no remote branch."""

    def __init__(self, waves=None):
        self.waves = waves or {}
        self.calls = []

    def __call__(self, args):
        a = list(args)
        self.calls.append(a)
        if a[0] == "rev-parse":
            wid = a[-1].split("origin/wave/")[1]
            return (0 if wid in self.waves else 1), ""
        if a[0] == "merge-base":
            wid = a[2].split("origin/wave/")[1]
            return (0 if self.waves[wid].get("merged") else 1), ""
        if a[0] == "ls-tree":
            wid = a[-1].split("/")[1]
            return 0, "\n".join(self.waves[wid].get("files", [])) + "\n"
        return 1, ""


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.state = self.tmp / "state"
        self.clock = Clock()
        self.env = {"CREATORSTUDIO_DEV_SUPERVISOR_CONFIG": str(self.tmp / "none.json")}
        self.store = None

    def mkstore(self):
        self.state.mkdir(exist_ok=True)
        self.store = StateStore(self.state, clock=self.clock)
        return self.store

    def write_registry(self, tasks, shared=None):
        doc = {"schema_version": 1, "shared_files": shared or {"ACTIVE_TASKS.json": "shared"}, "tasks": tasks}
        (self.repo / "ACTIVE_TASKS.json").write_text(json.dumps(doc), encoding="utf-8")

    def write_tracker(self, tasks):
        (self.repo / "DEVELOPMENT_TRACKER.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")

    def write_wave(self, wid, lanes, extra=None):
        d = self.repo / "waves" / wid
        d.mkdir(parents=True, exist_ok=True)
        doc = {"wave_id": wid, "lanes": [{"id": l, "task_id": "T-" + l} for l in lanes], **(extra or {})}
        (d / "wave.json").write_text(json.dumps(doc),
                                     encoding="utf-8")

    def items(self, git=None):
        return au.compute_items(self.repo, self.store, git or FakeGit())

    def kinds(self, res):
        return [(i["kind"], i["id"]) for i in res["items"]]

    def run_cli(self, argv, stdin="", git=None, sleep=None):
        out = io.StringIO()
        full = list(argv)
        if argv[0] != "main-settings":
            full += ["--repo", str(self.repo), "--state-dir", str(self.state)]
        rc = au.main(full, out=out, stdin=io.StringIO(stdin), env=self.env, git=git or FakeGit(), clock=self.clock,
                     sleep=sleep or self.clock.sleep)
        return rc, out.getvalue()

    def add_worker(self, wid, task_id, status, **kw):
        reg = WorkerRegistry(self.store)
        reg.add(Worker(worker_id=wid, role="dev", task_id=task_id, branch="b-" + wid, worktree=str(self.tmp / wid),
                       owner_stream="s", status=status, **kw))
        return reg

    def set_main(self, sid=MAIN_SID):
        self.store.write("sessions", {"sessions": {sid: {"session_id": sid, "role": "main", "authoritative": True,
                                                         "status": "ACTIVE"}}, "failures": {}})


class TestFingerprint(unittest.TestCase):
    def test_stable_and_order_independent(self):
        self.assertEqual(au.fingerprint(["b", "a"]), au.fingerprint(["a", "b", "a"]))

    def test_changes_with_content(self):
        self.assertNotEqual(au.fingerprint(["a"]), au.fingerprint(["a", "b"]))

    def test_empty_is_empty_string(self):
        self.assertEqual(au.fingerprint([]), "")

    def test_overlap_rules(self):
        self.assertTrue(au.overlaps("a/b", "a/b/c.py"))
        self.assertTrue(au.overlaps("A/B/", "a/b/c.py"))
        self.assertFalse(au.overlaps("a/bc", "a/b/c.py"))


def full_task(tid, **over):
    t = task(tid, paths=["x/"])
    t.update({"acceptance_criteria": ["it works"],
              "test_requirements": ["cd orchestrator && python -B -I -m unittest discover -s tests -p test_x.py",
                                    "independent review of the exact diff"]})
    t.update(over)
    return t


class TestDispatchReadiness(Base):
    """T-DEV-069: a start_task item says whether the record is complete enough to hand to a worker."""

    def item(self, rec):
        self.write_registry([rec])
        res = self.items()
        self.assertEqual(self.kinds(res), [("start_task", f"start_task:{rec['id']}")])    # kinds and ids are unchanged
        return res["items"][0]

    def test_a_complete_record_is_ready(self):
        i = self.item(full_task("T-1"))
        self.assertEqual((i["dispatch_ready"], i["dispatch_problems"]), (True, []))

    def test_each_missing_piece_is_named(self):
        cases = {
            "no owned paths": (full_task("T-1", owned_paths=[]), "owned_paths is empty"),
            "blank owned path": (full_task("T-1", owned_paths=["  "]), "owned_paths is empty"),
            "no acceptance criteria": (full_task("T-1", acceptance_criteria=[]), "acceptance_criteria is empty"),
            "acceptance criteria missing": ({k: v for k, v in full_task("T-1").items() if k != "acceptance_criteria"}, "acceptance_criteria is empty"),
            "no test requirements": (full_task("T-1", test_requirements=[]), "missing or empty"),
            "test requirements missing": ({k: v for k, v in full_task("T-1").items() if k != "test_requirements"}, "missing or empty"),
            "test requirements not a list": (full_task("T-1", test_requirements="python -B -m governance validate"), "missing or empty"),
            "prose requirement": (full_task("T-1", test_requirements=["full suite before merge"]), "test_requirements[0] is not machine-executable"),
            "shell chain": (full_task("T-1", test_requirements=["cd orchestrator && python -B -m governance validate && rm x"]),
                            "test_requirements[0] is not machine-executable"),
        }
        for name, (rec, expect) in cases.items():
            with self.subTest(case=name):
                i = self.item(rec)
                self.assertFalse(i["dispatch_ready"])
                self.assertTrue(any(expect in p for p in i["dispatch_problems"]), i["dispatch_problems"])

    def test_every_cause_is_reported_and_only_the_manual_requirements(self):
        rec = full_task("T-1", owned_paths=[], acceptance_criteria=[],
                        test_requirements=["cd orchestrator && python -B -m governance validate", "make it good", "also this"])
        probs = self.item(rec)["dispatch_problems"]
        self.assertEqual(len(probs), 4)
        self.assertIn("test_requirements[1]", probs[2])
        self.assertIn("test_requirements[2]", probs[3])

    def test_the_bare_records_of_older_tests_are_not_ready_but_still_offered(self):
        i = self.item(task("T-1", paths=["x/"]))
        self.assertFalse(i["dispatch_ready"])
        self.assertEqual(i["reason"], "T-1 is PLANNED, its dependencies are done and no owned path is held")   # reason unchanged

    def test_blocked_tasks_are_still_blocked_and_get_no_verdict(self):
        self.write_registry([task("T-0", "ACTIVE", paths=["y/"]), full_task("T-1", depends_on=["T-0"])])
        res = self.items()
        self.assertEqual(res["items"], [])
        self.assertEqual([b["task_id"] for b in res["blocked"]], ["T-1"])

    def test_the_text_output_shows_the_verdict(self):
        self.write_registry([full_task("T-1"), full_task("T-2", owned_paths=["z/"], test_requirements=["do it"])])
        rc, out = self.run_cli(["next"])
        self.assertEqual(rc, 0)
        self.assertIn("dispatch: READY", out)
        self.assertIn("dispatch: NOT READY - test_requirements[0] is not machine-executable", out)

    def test_the_json_output_carries_the_fields_and_reading_writes_nothing(self):
        self.write_registry([full_task("T-1")])
        before = sorted(p.name for p in self.repo.rglob("*"))
        rc, out = self.run_cli(["next", "--json"])
        item = json.loads(out)["items"][0]
        self.assertEqual((rc, item["dispatch_ready"], item["dispatch_problems"]), (0, True, []))
        self.assertEqual(sorted(p.name for p in self.repo.rglob("*")), before)


class TestTaskEligibility(Base):
    def test_planned_task_without_deps_is_eligible(self):
        self.write_registry([task("T-1", paths=["x/"])])
        res = self.items()
        self.assertEqual(self.kinds(res), [("start_task", "start_task:T-1")])
        self.assertEqual(res["idle_reason"], None)

    def test_completed_and_released_dependencies_allow(self):
        self.write_registry([task("T-0", "COMPLETED"), task("T-9", "RELEASED"),
                             task("T-1", deps=["T-0", "T-9"], paths=["x/"])])
        self.assertEqual(self.kinds(self.items()), [("start_task", "start_task:T-1")])

    def test_unfinished_dependency_blocks_with_reason(self):
        self.write_registry([task("T-0", "ACTIVE", paths=["y/"]), task("T-1", deps=["T-0"], paths=["x/"])])
        res = self.items()
        self.assertEqual(res["items"], [])
        self.assertIn("waits for T-0 (ACTIVE)", res["blocked"][0]["blocker"])

    def test_unknown_dependency_blocks(self):
        self.write_registry([task("T-1", deps=["T-404"])])
        self.assertIn("unknown task T-404", self.items()["blocked"][0]["blocker"])

    def test_owned_path_overlap_with_active_blocks(self):
        self.write_registry([task("T-0", "ACTIVE", paths=["pkg/"]), task("T-1", paths=["pkg/mod.py"])])
        self.assertIn("held by T-0", self.items()["blocked"][0]["blocker"])

    def test_overlap_with_under_review_blocks(self):
        self.write_registry([task("T-0", "UNDER_REVIEW", paths=["pkg/"]), task("T-1", paths=["pkg/mod.py"])])
        self.assertEqual(self.items()["items"], [])

    def test_no_overlap_with_completed_task(self):
        self.write_registry([task("T-0", "COMPLETED", paths=["pkg/"]), task("T-1", paths=["pkg/mod.py"])])
        self.assertEqual(len(self.items()["items"]), 1)

    def test_shared_files_do_not_count_as_overlap(self):
        self.write_registry([task("T-0", "ACTIVE", paths=["ACTIVE_TASKS.json", "a/"]),
                             task("T-1", paths=["ACTIVE_TASKS.json", "b/"])])
        self.assertEqual(self.kinds(self.items()), [("start_task", "start_task:T-1")])

    def test_planned_tasks_do_not_block_each_other(self):
        self.write_registry([task("T-1", paths=["p/"]), task("T-2", paths=["p/"])])
        self.assertEqual(len(self.items()["items"]), 2)

    def test_running_worker_blocks_start(self):
        self.mkstore()
        self.write_registry([task("T-1", paths=["x/"])])
        self.add_worker("w1", "T-1", WorkerStatus.RUNNING)
        res = self.items()
        self.assertEqual(res["items"], [])
        self.assertIn("worker is already running", res["blocked"][0]["blocker"])

    def test_missing_registry_is_warning_not_crash(self):
        res = self.items()
        self.assertEqual(res["items"], [])
        self.assertTrue(any("ACTIVE_TASKS.json" in w for w in res["warnings"]))

    def test_corrupt_registry_is_warning(self):
        (self.repo / "ACTIVE_TASKS.json").write_text("{not json", encoding="utf-8")
        self.assertTrue(self.items()["warnings"])

    def test_completed_task_never_offered(self):
        self.write_registry([task("T-1", "COMPLETED")])
        self.assertEqual(self.items()["items"], [])


class TestTrackerSteps(Base):
    def steps(self, *statuses):
        return [{"id": f"S-{i}", "status": s} for i, s in enumerate(statuses)]

    def test_ready_for_review_step_for_active_task(self):
        self.write_registry([task("T-1", "ACTIVE")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("COMPLETED", "READY_FOR_REVIEW", "PLANNED")}])
        self.assertEqual(self.kinds(self.items()), [("review_step", "review_step:T-1:S-1")])

    def test_next_planned_step_is_start_step(self):
        self.write_registry([task("T-1", "ACTIVE")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("COMPLETED", "PLANNED")}])
        res = self.items()
        self.assertEqual(self.kinds(res), [("start_step", "start_step:T-1:S-1")])
        self.assertIn("authorization", res["items"][0]["reason"])

    def test_in_progress_step_not_actionable_and_explains_idle(self):
        self.write_registry([task("T-1", "ACTIVE")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("IN_PROGRESS", "PLANNED")}])
        res = self.items()
        self.assertEqual(res["items"], [])
        self.assertIn("1 step IN_PROGRESS", res["idle_reason"])

    def test_completed_registry_task_steps_ignored(self):
        self.write_registry([task("T-1", "COMPLETED")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("READY_FOR_REVIEW")}])
        self.assertEqual(self.items()["items"], [])

    def test_all_steps_completed_nothing(self):
        self.write_registry([task("T-1", "ACTIVE")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("COMPLETED", "COMPLETED")}])
        self.assertEqual(self.items()["items"], [])

    def test_start_step_blocked_by_running_worker(self):
        self.mkstore()
        self.write_registry([task("T-1", "ACTIVE")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("PLANNED")}])
        self.add_worker("w1", "T-1", WorkerStatus.RUNNING)
        self.assertEqual(self.items()["items"], [])

    def test_missing_tracker_warns_but_keeps_tasks(self):
        self.write_registry([task("T-1", paths=["x/"])])
        res = self.items()
        self.assertEqual(len(res["items"]), 1)
        self.assertTrue(any("TRACKER" in w for w in res["warnings"]))


class TestWorkers(Base):
    def setUp(self):
        super().setUp()
        self.mkstore()
        self.write_registry([])

    def test_reported_result_is_review_item_and_first(self):
        self.write_registry([task("T-2", paths=["q/"])])
        reg = self.add_worker("w1", "T-1", WorkerStatus.EXITED)
        reg.results["r1"] = ResultRecord(result_id="r1", worker_id="w1", task_id="T-1", outcome="DONE")
        reg.save()
        res = self.items()
        self.assertEqual([i["kind"] for i in res["items"]], ["review_worker_result", "start_task"])
        self.assertEqual(res["items"][0]["evidence"], "REPORTED")

    def test_verified_and_rejected_results_not_listed(self):
        reg = self.add_worker("w1", "T-1", WorkerStatus.EXITED)
        reg.results["r1"] = ResultRecord(result_id="r1", worker_id="w1", task_id="T-1", outcome="DONE",
                                         status="VERIFIED")
        reg.results["r2"] = ResultRecord(result_id="r2", worker_id="w1", task_id="T-1", outcome="DONE",
                                         status="REJECTED")
        reg.save()
        self.assertEqual(self.items()["items"], [])

    def test_failed_worker_retry_eligible(self):
        self.add_worker("w1", "T-1", WorkerStatus.FAILED, restart_count=1, failures=["exit 1"],
                        next_restart_not_before="2026-01-01T00:00:00+00:00")
        i = self.items()["items"][0]
        self.assertEqual(i["kind"], "worker_failed")
        self.assertTrue(i["retry_eligible"])
        self.assertIn("retry eligible (restart 1/3)", i["reason"])

    def test_failed_worker_retry_blocked_by_budget(self):
        self.add_worker("w1", "T-1", WorkerStatus.FAILED, restart_count=3, failures=["a", "b", "c"])
        i = self.items()["items"][0]
        self.assertFalse(i["retry_eligible"])
        self.assertIn("restart budget exhausted", i["reason"])

    def test_failed_worker_retry_blocked_by_identical_failures(self):
        self.add_worker("w1", "T-1", WorkerStatus.FAILED, restart_count=1, failures=["x", "x", "x"])
        i = self.items()["items"][0]
        self.assertFalse(i["retry_eligible"])
        self.assertIn("identical failures", i["reason"])

    def test_lost_worker_listed(self):
        self.add_worker("w1", "T-1", WorkerStatus.LOST)
        self.assertEqual(self.items()["items"][0]["kind"], "worker_failed")

    def test_blocked_worker_goes_to_blocked_list_only(self):
        self.add_worker("w1", "T-1", WorkerStatus.BLOCKED)
        res = self.items()
        self.assertEqual(res["items"], [])
        self.assertIn("needs a human", res["blocked"][0]["blocker"])

    def test_failed_worker_superseded_by_live_worker(self):
        self.add_worker("w1", "T-1", WorkerStatus.FAILED, assigned_at="2026-01-01T00:00:00+00:00")
        self.add_worker("w2", "T-1", WorkerStatus.RUNNING, assigned_at="2026-01-02T00:00:00+00:00")
        self.assertEqual(self.items()["items"], [])

    def test_failed_worker_superseded_by_newer_worker(self):
        self.add_worker("w1", "T-1", WorkerStatus.FAILED, assigned_at="2026-01-01T00:00:00+00:00")
        self.add_worker("w2", "T-1", WorkerStatus.EXITED, assigned_at="2026-01-02T00:00:00+00:00")
        self.assertEqual(self.items()["items"], [])

    def _failed_for(self, status, wstatus=WorkerStatus.FAILED):
        shutil.rmtree(self.state, ignore_errors=True)    # fresh worker registry for each sub-case
        self.mkstore()
        self.write_registry([task("T-1", status, paths=["a/"])])
        self.add_worker("w1", "T-1", wstatus, restart_count=0)
        return self.items()

    def test_failed_worker_of_finished_task_is_suppressed_not_offered(self):
        for tstatus in ("COMPLETED", "RELEASED"):
            for wstatus in (WorkerStatus.FAILED, WorkerStatus.LOST):
                with self.subTest(task=tstatus, worker=wstatus):
                    res = self._failed_for(tstatus, wstatus)
                    self.assertEqual(res["items"], [])
                    self.assertEqual(res["blocked"], [])
                    self.assertEqual([(x["task_id"], x["worker_id"]) for x in res["suppressed"]], [("T-1", "w1")])
                    self.assertIn(f"task T-1 is {tstatus}", res["suppressed"][0]["reason"])
                    self.assertEqual(res["fingerprint"], "")
                    self.assertNotIn("retry", json.dumps(res["items"]))

    def test_failed_worker_of_open_task_is_still_offered(self):
        for tstatus in ("PLANNED", "ACTIVE", "UNDER_REVIEW"):
            with self.subTest(task=tstatus):
                res = self._failed_for(tstatus)
                i = [x for x in res["items"] if x["kind"] == "worker_failed"]
                self.assertEqual([x["id"] for x in i], ["worker_failed:w1:FAILED:0"])
                self.assertTrue(i[0]["retry_eligible"])
                self.assertIn("retry eligible (restart 0/3)", i[0]["reason"])
                self.assertEqual(res["suppressed"], [])

    def test_failed_worker_of_blocked_task_is_blocked_not_restartable(self):
        res = self._failed_for("BLOCKED")
        self.assertEqual([i for i in res["items"] if i["kind"] == "worker_failed"], [])
        self.assertEqual(res["blocked"][0]["id"], "blocked:worker:failed:w1")
        self.assertIn("BLOCKED", res["blocked"][0]["blocker"])
        self.assertNotIn("no eligible task", res["idle_reason"])

    def test_failed_worker_of_unknown_task_or_unreadable_registry_keeps_old_behaviour(self):
        self.add_worker("w1", "T-9", WorkerStatus.FAILED)
        self.assertEqual(self.items()["items"][0]["kind"], "worker_failed")        # task not in the registry
        (self.repo / "ACTIVE_TASKS.json").write_text("{not json", encoding="utf-8")
        res = self.items()
        self.assertEqual(res["items"][0]["kind"], "worker_failed")
        self.assertTrue(any("ACTIVE_TASKS.json unreadable" in w for w in res["warnings"]))
        (self.repo / "ACTIVE_TASKS.json").unlink()
        self.assertEqual(self.items()["items"][0]["kind"], "worker_failed")

    def test_suppression_hides_nothing_from_the_worker_registry(self):
        self._failed_for("COMPLETED")
        reg = WorkerRegistry(self.store)
        self.assertEqual(reg.workers["w1"].status, WorkerStatus.FAILED)

    def test_text_output_lists_suppressed_failure_as_info(self):
        self._failed_for("COMPLETED")
        rc, out = self.run_cli(["next"])
        self.assertEqual(rc, 0)
        self.assertIn("info (suppressed) T-1: worker w1 is FAILED but task T-1 is COMPLETED", out)
        self.assertNotIn("worker_failed", out)

    def test_dispatch_candidate_is_not_hidden_by_a_suppressed_failure(self):
        self.write_registry([task("T-1", "COMPLETED", paths=["a/"]), task("T-2", paths=["b/"])])
        self.add_worker("w1", "T-1", WorkerStatus.FAILED)
        self.assertEqual(self.kinds(self.items()), [("start_task", "start_task:T-2")])

    def test_idle_reason_waiting_for_running_workers(self):
        self.add_worker("w1", "T-1", WorkerStatus.RUNNING)
        self.add_worker("w2", "T-2", WorkerStatus.RUNNING)
        self.assertEqual(self.items()["idle_reason"], "waiting for 2 running workers")

    def test_idle_reason_names_blocker(self):
        self.write_registry([task("T-X", deps=["T-Y"], paths=["a/"]), task("T-Y", "ACTIVE", paths=["b/"])])
        self.assertIn("no eligible task: T-X waits for T-Y", self.items()["idle_reason"])

    def test_idle_reason_when_nothing_registered(self):
        self.assertIn("nothing is planned", self.items()["idle_reason"])

    def test_missing_state_dir_is_not_created(self):
        self.store = StateStore(self.tmp / "absent", clock=self.clock, create=False)
        self.write_registry([task("T-1", paths=["x/"])])
        self.assertEqual(len(self.items()["items"]), 1)
        self.assertFalse((self.tmp / "absent").exists())


class TestWaves(Base):
    def test_submitted_unmerged_wave_is_verify_item(self):
        self.write_registry([])
        self.write_wave("w9", ["L1", "L2"])
        git = FakeGit({"w9": {"merged": False,
                              "files": ["waves/w9/lanes/L1/report.json", "waves/w9/lanes/L1/BRIEF.md"]}})
        res = self.items(git)
        self.assertEqual(self.kinds(res), [("verify_wave", "verify_wave:w9:L1")])
        self.assertIn("1 of 2 lanes", res["items"][0]["reason"])

    def test_merged_wave_skipped(self):
        self.write_wave("w9", ["L1"])
        git = FakeGit({"w9": {"merged": True, "files": ["waves/w9/lanes/L1/report.json"]}})
        self.assertEqual(self.items(git)["items"], [])

    def test_wave_without_remote_branch_skipped(self):
        self.write_wave("w9", ["L1"])
        self.assertEqual(self.items(FakeGit())["items"], [])

    def test_wave_without_reports_skipped(self):
        self.write_wave("w9", ["L1"])
        self.assertEqual(self.items(FakeGit({"w9": {"merged": False, "files": []}}))["items"], [])

    def test_report_for_unknown_lane_ignored(self):
        self.write_wave("w9", ["L1"])
        git = FakeGit({"w9": {"merged": False, "files": ["waves/w9/lanes/L7/report.json"]}})
        self.assertEqual(self.items(git)["items"], [])

    def test_new_lane_changes_fingerprint(self):
        self.write_wave("w9", ["L1", "L2"])
        a = self.items(FakeGit({"w9": {"files": ["waves/w9/lanes/L1/report.json"]}}))["fingerprint"]
        b = self.items(FakeGit({"w9": {"files": ["waves/w9/lanes/L1/report.json",
                                                  "waves/w9/lanes/L2/report.json"]}}))["fingerprint"]
        self.assertNotEqual(a, b)

    def test_git_is_only_called_with_read_only_plumbing(self):
        self.write_wave("w9", ["L1"])
        git = FakeGit({"w9": {"files": ["waves/w9/lanes/L1/report.json"]}})
        self.items(git)
        self.assertEqual({c[0] for c in git.calls}, {"rev-parse", "merge-base", "ls-tree"})

    def test_unreadable_manifest_warns(self):
        d = self.repo / "waves" / "bad"
        d.mkdir(parents=True)
        (d / "wave.json").write_text("nope", encoding="utf-8")
        self.assertTrue(any("bad/wave.json" in w for w in self.items()["warnings"]))

    def test_git_failure_never_raises(self):
        self.write_wave("w9", ["L1"])

        def boom(args):
            raise OSError("boom")
        res = au.compute_items(self.repo, None, boom)
        self.assertTrue(res["warnings"])


class TestOwnerAndClosedWaves(Base):
    def steps(self, st):
        return [{"id": "S-0", "status": st}]

    def test_other_owner_review_step_not_actionable_and_listed_separately(self):
        self.write_registry([task("T-1", "ACTIVE", owner="claude:someone-else")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("READY_FOR_REVIEW")}])
        res = self.items()
        self.assertEqual(res["items"], [])
        self.assertEqual(res["fingerprint"], "")
        self.assertEqual(res["others_waiting_review"][0]["task_id"], "T-1")
        self.assertEqual(res["counts"]["others_waiting"], 1)

    def test_other_owner_step_does_not_change_fingerprint(self):
        self.write_registry([task("T-2", paths=["z/"])])
        before = self.items()["fingerprint"]
        self.write_registry([task("T-2", paths=["z/"]), task("T-1", "ACTIVE", owner="claude:other")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("READY_FOR_REVIEW")}])
        self.assertEqual(self.items()["fingerprint"], before)

    def test_other_owner_planned_step_not_actionable(self):
        self.write_registry([task("T-1", "ACTIVE", owner="claude:other")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("PLANNED")}])
        self.assertEqual(self.items()["items"], [])

    def test_owner_flag_changes_who_is_actionable(self):
        self.write_registry([task("T-1", "ACTIVE", owner="claude:other")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("READY_FOR_REVIEW")}])
        res = au.compute_items(self.repo, None, FakeGit(), owner="claude:other")
        self.assertEqual(self.kinds(res), [("review_step", "review_step:T-1:S-0")])
        rc, out = self.run_cli(["next", "--json", "--owner", "claude:other"])
        self.assertEqual(json.loads(out)["counts"]["items"], 1)

    def test_stop_hook_never_blocks_for_other_owner_step(self):
        self.mkstore()
        self.set_main()
        self.write_registry([task("T-1", "ACTIVE", owner="claude:other")])
        self.write_tracker([{"task_id": "T-1", "steps": self.steps("READY_FOR_REVIEW")}])
        rc, out = self.run_cli(["stop-hook"], stdin=json.dumps({"session_id": MAIN_SID}))
        self.assertEqual((rc, out), (0, ""))

    def test_wave_with_completed_tasks_skipped_despite_non_ancestor_branch(self):
        self.write_registry([task("T-L1", "COMPLETED"), task("T-L2", "RELEASED")])
        self.write_wave("w9", ["L1", "L2"])
        git = FakeGit({"w9": {"merged": False, "files": ["waves/w9/lanes/L1/report.json"]}})
        self.assertEqual(self.items(git)["items"], [])

    def test_wave_with_only_some_tasks_completed_still_listed(self):
        self.write_registry([task("T-L1", "COMPLETED"), task("T-L2", "ACTIVE")])
        self.write_wave("w9", ["L1", "L2"])
        git = FakeGit({"w9": {"merged": False, "files": ["waves/w9/lanes/L1/report.json"]}})
        self.assertEqual(len(self.items(git)["items"]), 1)

    def test_manifest_closed_state_skips_wave(self):
        self.write_registry([])
        self.write_wave("w9", ["L1"], {"status": "CLOSED"})
        git = FakeGit({"w9": {"merged": False, "files": ["waves/w9/lanes/L1/report.json"]}})
        self.assertEqual(self.items(git)["items"], [])


class TestOrdering(Base):
    def test_priority_order(self):
        self.mkstore()
        self.write_registry([task("T-1", "ACTIVE"), task("T-2", paths=["z/"])])
        self.write_tracker([{"task_id": "T-1", "steps": [{"id": "S-0", "status": "READY_FOR_REVIEW"}]}])
        self.write_wave("w9", ["L1"])
        reg = self.add_worker("w1", "T-5", WorkerStatus.FAILED)
        reg.results["r1"] = ResultRecord(result_id="r1", worker_id="w1", task_id="T-5", outcome="DONE")
        reg.save()
        git = FakeGit({"w9": {"files": ["waves/w9/lanes/L1/report.json"]}})
        kinds = [i["kind"] for i in self.items(git)["items"]]
        self.assertEqual(kinds, ["review_worker_result", "worker_failed", "review_step", "verify_wave", "start_task"])

    def test_every_item_has_reason_and_inspect(self):
        self.write_registry([task("T-1", paths=["x/"])])
        for i in self.items()["items"]:
            self.assertTrue(i["reason"] and i["inspect"] and isinstance(i["priority"], int))


class TestNextCli(Base):
    def test_json_output_shape(self):
        self.write_registry([task("T-1", paths=["x/"])])
        rc, out = self.run_cli(["next", "--json"])
        d = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(d["counts"]["items"], 1)
        self.assertIn("fingerprint", d)

    def test_text_output_idle(self):
        self.write_registry([])
        rc, out = self.run_cli(["next"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("idle:"))

    def test_next_does_not_create_state_dir(self):
        self.write_registry([task("T-1", paths=["x/"])])
        self.run_cli(["next", "--json"])
        self.assertFalse(self.state.exists())

    def test_next_does_not_change_existing_state_files(self):
        self.mkstore()
        self.set_main()
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        self.write_registry([task("T-1", paths=["x/"])])
        self.run_cli(["next", "--json"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir()})

    def test_bad_config_exits_2(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{", encoding="utf-8")
        out = io.StringIO()
        rc = au.main(["next", "--config", str(bad), "--repo", str(self.repo)], out=out, env=self.env)
        self.assertEqual(rc, 2)


def ms(mid, state="NOT_STARTED", deps=(), questions=(), blockers=(), phase="Phase 9", weight=3):
    return {"id": mid, "title": "Title of " + mid, "phase": phase, "weight": weight, "depends_on": list(deps),
            "implementation": {"state": state, "summary": "s"}, "open_questions": list(questions),
            "blockers": list(blockers)}


class TestMilestoneProposals(Base):
    """T-DEV-039: read-only proposals from PROJECT_STATUS.json when nothing is registered."""

    def write_status(self, milestones):
        (self.repo / "PROJECT_STATUS.json").write_text(json.dumps({"milestones": milestones}), encoding="utf-8")

    def props(self, res=None):
        return [p["milestone_id"] for p in (res or self.items())["proposals"]]

    def test_an_eligible_milestone_is_proposed_and_the_idle_reason_names_it(self):
        self.write_registry([])
        self.write_status([ms("M-A", "IMPLEMENTED"), ms("M-B", deps=["M-A"])])
        res = self.items()
        self.assertEqual(self.props(res), ["M-B"])
        self.assertEqual(res["proposals"][0]["evidence"], "PROPOSED")
        self.assertFalse(res["proposals"][0]["needs_decision"])
        self.assertEqual(res["items"], [])
        self.assertIn("1 milestone(s) could be proposed", res["idle_reason"])
        self.assertIn("M-B", res["idle_reason"])
        self.assertEqual(res["counts"]["proposals"], 1)

    def test_ineligible_milestones_are_not_proposed(self):
        self.write_registry([])
        self.write_status([
            ms("M-DONE", "IMPLEMENTED"), ms("M-WIP", "IN_PROGRESS"), ms("M-BLOCKED", "BLOCKED"),
            ms("M-UNMET", deps=["M-WIP"]), ms("M-UNKNOWN", deps=["M-NOPE"]), ms("M-OK", deps=["M-DONE"])])
        self.assertEqual(self.props(), ["M-OK"])

    def test_a_malformed_dependency_list_is_ineligible_not_an_error(self):
        self.write_registry([])
        bad = ms("M-BAD")
        bad["depends_on"] = "M-A"
        self.write_status([bad, ms("M-GOOD")])
        self.assertEqual(self.props(), ["M-GOOD"])

    def test_an_open_task_that_mentions_the_milestone_suppresses_it_but_a_done_task_does_not(self):
        t_open = task("T-OPEN", "ACTIVE")
        t_open["title"] = "Work for M-X"
        t_done = task("T-DONE", "COMPLETED")
        t_done["title"] = "Finished M-Y"
        self.write_registry([t_open, t_done])
        self.write_status([ms("M-X"), ms("M-Y")])
        self.assertEqual(self.props(), ["M-Y"])

    def test_a_longer_id_in_a_task_does_not_suppress_a_shorter_milestone(self):
        t = task("T-OPEN", "ACTIVE")
        t["title"] = "Work for M-INT-020"
        self.write_registry([t])
        self.write_status([ms("M-INT-02")])
        self.assertEqual(self.props(), ["M-INT-02"])

    def test_an_id_embedded_in_a_longer_id_before_it_does_not_suppress_either(self):
        t = task("T-OPEN", "ACTIVE")
        t["title"] = "Work for T-M-X"
        self.write_registry([t])
        self.write_status([ms("M-X")])
        self.assertEqual(self.props(), ["M-X"])

    def test_an_unreadable_or_malformed_registry_proposes_nothing_and_warns(self):
        self.write_status([ms("M-A")])
        wrong = ("{", "[]", "null", "{}", '{"tasks": null}', '{"tasks": 5}', '{"tasks": "abc"}',
                 '{"tasks": {"T-1": {"status": "ACTIVE", "title": "M-A"}}}', '{"tasks": [1, "M-A"]}',
                 '{"tasks": [{"id": "T-1", "status": "ACTIVE", "title": "M-A"}, "junk"]}')
        for text in (None,) + wrong:
            with self.subTest(registry=text):
                if text is None:
                    (self.repo / "ACTIVE_TASKS.json").unlink(missing_ok=True)
                else:
                    (self.repo / "ACTIVE_TASKS.json").write_text(text, encoding="utf-8")
                res = self.items()
                self.assertEqual(res["proposals"], [], "a registry that cannot be checked must propose nothing")
                self.assertTrue(any("ACTIVE_TASKS.json" in w and "proposals" in w for w in res["warnings"]),
                                res["warnings"])
        (self.repo / "ACTIVE_TASKS.json").write_text('{"tasks": []}', encoding="utf-8")
        self.assertEqual(self.props(), ["M-A"])                              # an empty list is a valid registry

    def test_malformed_question_or_blocker_fields_lean_towards_needs_decision(self):
        self.write_registry([])
        a, b, c = ms("M-A"), ms("M-B"), ms("M-C")
        a["open_questions"] = "Q open"
        b["blockers"] = {"x": 1}
        c["open_questions"] = None            # absent or null means none
        self.write_status([a, b, c])
        res = self.items()
        self.assertEqual({p["milestone_id"]: p["needs_decision"] for p in res["proposals"]},
                         {"M-A": True, "M-B": True, "M-C": False})
        self.assertEqual(self.props(res)[0], "M-C")

    def test_milestones_with_questions_or_blockers_are_flagged_and_listed_after_ready_ones(self):
        self.write_registry([])
        self.write_status([ms("M-Q", questions=["Q1 is open"]), ms("M-R"), ms("M-B", blockers=["no engine"])])
        res = self.items()
        self.assertEqual(self.props(res), ["M-R", "M-Q", "M-B"])
        self.assertEqual([p["needs_decision"] for p in res["proposals"]], [False, True, True])
        self.assertEqual(res["proposals"][1]["open_questions"], ["Q1 is open"])
        self.assertIn("(1 without an open decision: M-R)", res["idle_reason"])

    def test_a_missing_status_file_is_silent_and_leaves_the_idle_reason_unchanged(self):
        self.write_registry([])
        res = self.items()
        self.assertEqual(res["proposals"], [])
        self.assertEqual([w for w in res["warnings"] if "PROJECT_STATUS" in w or "proposals" in w], [])
        self.assertEqual(res["idle_reason"], "no registered work: nothing is planned or waiting")

    def test_malformed_status_files_warn_and_propose_nothing(self):
        self.write_registry([])
        for text in ("{", "[]", '{"milestones": {"a": 1}}', '{"milestones": 5}', "null"):
            with self.subTest(text=text):
                (self.repo / "PROJECT_STATUS.json").write_text(text, encoding="utf-8")
                res = self.items()
                self.assertEqual(res["proposals"], [])
                self.assertTrue(any("PROJECT_STATUS.json" in w for w in res["warnings"]), res["warnings"])

    def test_junk_entries_inside_the_milestone_list_are_skipped(self):
        self.write_registry([])
        self.write_status(["junk", 5, None, {"id": 3}, {"title": "no id"}, ms("M-OK")])
        self.assertEqual(self.props(), ["M-OK"])

    def test_proposals_do_not_change_items_or_the_fingerprint(self):
        self.write_registry([task("T-1", paths=["x/"])])
        without = self.items()
        self.write_status([ms("M-A")])
        with_status = self.items()
        self.assertEqual(without["fingerprint"], with_status["fingerprint"])
        self.assertEqual(self.kinds(without), self.kinds(with_status))
        self.assertEqual(self.props(with_status), ["M-A"])
        self.assertNotIn("could be proposed", with_status["idle_reason"] or "")

    def test_text_output_lists_proposals_only_when_nothing_else_is_actionable(self):
        self.write_registry([])
        self.write_status([ms("M-A", questions=["Q9 open"])])
        rc, out = self.run_cli(["next"])
        self.assertEqual(rc, 0)
        self.assertIn("PROPOSED M-A [Phase 9] Title of M-A (needs a decision: Q9 open)", out)
        self.write_registry([task("T-1", paths=["x/"])])
        rc, out = self.run_cli(["next"])
        self.assertNotIn("PROPOSED", out)
        rc, out = self.run_cli(["next", "--json"])
        self.assertEqual(json.loads(out)["proposals"][0]["milestone_id"], "M-A")

    def test_next_with_proposals_writes_nothing_and_creates_no_state(self):
        self.write_registry([])
        self.write_status([ms("M-A")])
        before = {p.name: p.read_bytes() for p in self.repo.iterdir()}
        self.run_cli(["next"])
        self.run_cli(["next", "--json"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.repo.iterdir()})
        self.assertFalse(self.state.exists())


class TestStopHook(Base):
    def setUp(self):
        super().setUp()
        self.mkstore()
        self.set_main()
        self.write_registry([task("T-1", paths=["x/"])])

    def hook(self, payload=None, extra=()):
        payload = {"session_id": MAIN_SID, "stop_hook_active": False} if payload is None else payload
        return self.run_cli(["stop-hook", *extra], stdin=json.dumps(payload))

    def test_blocks_with_one_json_object(self):
        rc, out = self.hook()
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["decision"], "block")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("start_task:T-1", d["reason"])
        self.assertNotIn("\n", d["reason"])

    def test_stop_hook_does_not_block_on_failure_of_finished_task(self):
        self.write_registry([task("T-1", "COMPLETED", paths=["x/"])])
        self.add_worker("w1", "T-1", WorkerStatus.FAILED)
        self.assertEqual(self.hook(), (0, ""))

    def test_other_session_allowed_silently(self):
        self.assertEqual(self.hook({"session_id": "other", "stop_hook_active": False}), (0, ""))

    def test_missing_session_id_allowed(self):
        self.assertEqual(self.hook({"stop_hook_active": False}), (0, ""))

    def test_no_items_allows_and_resets(self):
        self.hook()
        self.write_registry([])
        self.assertEqual(self.hook(), (0, ""))
        self.assertEqual(self.store.read("autonomy")["consecutive_blocks"], 0)

    def test_active_with_unchanged_fingerprint_allows(self):
        self.hook()
        self.assertEqual(self.hook({"session_id": MAIN_SID, "stop_hook_active": True}), (0, ""))

    def test_active_with_changed_fingerprint_blocks_again(self):
        self.hook()
        self.write_registry([task("T-1", paths=["x/"]), task("T-2", paths=["y/"])])
        rc, out = self.hook({"session_id": MAIN_SID, "stop_hook_active": True})
        self.assertIn("2 actionable", json.loads(out)["reason"])

    def test_consecutive_blocks_are_bounded(self):
        results = [self.hook()[1] for _ in range(5)]
        self.assertEqual([bool(r) for r in results], [True, True, True, False, False])

    def test_max_blocks_option(self):
        self.assertTrue(self.hook(extra=["--max-blocks", "1"])[1])
        self.assertEqual(self.hook(extra=["--max-blocks", "1"]), (0, ""))

    def test_max_blocks_zero_never_blocks(self):
        self.assertEqual(self.hook(extra=["--max-blocks", "0"]), (0, ""))

    def test_max_blocks_from_environment(self):
        self.env[au.ENV_MAX_BLOCKS] = "1"
        self.assertTrue(self.hook()[1])
        self.assertEqual(self.hook(), (0, ""))

    def test_counter_resets_when_fingerprint_changes(self):
        for _ in range(3):
            self.hook()
        self.write_registry([task("T-2", paths=["y/"])])
        self.assertTrue(self.hook()[1])
        self.assertEqual(self.store.read("autonomy")["consecutive_blocks"], 1)

    def test_fails_open_on_invalid_stdin(self):
        self.assertEqual(self.run_cli(["stop-hook"], stdin="not json"), (0, ""))

    def test_fails_open_on_empty_stdin(self):
        self.assertEqual(self.run_cli(["stop-hook"], stdin=""), (0, ""))

    def test_fails_open_and_logs_on_internal_error(self):
        with mock.patch.object(au, "compute_items", side_effect=RuntimeError("boom")):
            self.assertEqual(self.hook(), (0, ""))
        events, _ = self.store.read_events()
        self.assertEqual(events[-1]["kind"], "autonomy_hook_error")
        self.assertEqual(events[-1]["data"]["error"], "RuntimeError")

    def test_fails_open_on_bad_config(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{", encoding="utf-8")
        out = io.StringIO()
        rc = au.main(["stop-hook", "--config", str(bad)], out=out, stdin=io.StringIO("{}"), env=self.env)
        self.assertEqual((rc, out.getvalue()), (0, ""))

    def test_no_state_dir_allows_without_creating(self):
        shutil.rmtree(self.state)
        self.assertEqual(self.hook(), (0, ""))
        self.assertFalse(self.state.exists())

    def test_main_json_fallback_when_no_sessions_doc(self):
        (self.state / "sessions.json").unlink()
        self.store.write("main", {"session_id": MAIN_SID, "status": "RUNNING"})
        self.assertTrue(self.hook()[1])

    def test_retired_main_is_not_authoritative(self):
        self.store.write("sessions", {"sessions": {MAIN_SID: {"role": "main", "authoritative": True,
                                                              "status": "RETIRED"}}, "failures": {}})
        self.assertEqual(self.hook(), (0, ""))

    def test_reason_has_no_secret_and_is_bounded(self):
        self.write_registry([task("T-1 " + "sk" + "-" + "a" * 30 + "x" * 400, paths=["x/"])])
        reason = json.loads(self.hook()[1])["reason"]
        self.assertNotIn("sk" + "-" + "a" * 12, reason)
        self.assertLess(len(reason), 520)


class TestAmbiguousMain(Base):
    STALE = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        super().setUp()
        self.mkstore()
        self.write_registry([task("T-1", paths=["x/"])])

    def sessions(self, *sids):
        self.store.write("sessions", {"sessions": {s: {"session_id": s, "role": "main", "authoritative": True,
                                                       "status": "ACTIVE"} for s in sids}, "failures": {}})

    def hook(self, sid):
        return self.run_cli(["stop-hook"], stdin=json.dumps({"session_id": sid}))

    def test_main_json_running_picks_current_over_stale_first_entry(self):
        self.sessions(self.STALE, MAIN_SID)
        self.store.write("main", {"session_id": MAIN_SID, "status": "RUNNING"})
        self.assertEqual(au.authoritative_main(self.store), MAIN_SID)
        self.assertTrue(self.hook(MAIN_SID)[1])
        self.assertEqual(self.hook(self.STALE), (0, ""))

    def test_single_candidate_used_even_without_main_json(self):
        self.sessions(MAIN_SID)
        self.assertEqual(au.authoritative_main(self.store), MAIN_SID)

    def test_single_candidate_used_when_main_json_not_running(self):
        self.sessions(MAIN_SID)
        self.store.write("main", {"session_id": self.STALE, "status": "RUNNING"})
        self.assertEqual(au.authoritative_main(self.store), MAIN_SID)

    def test_several_without_match_is_none_and_hook_fails_open(self):
        self.sessions(self.STALE, MAIN_SID)
        self.store.write("main", {"session_id": "other", "status": "STOPPED"})
        self.assertIsNone(au.authoritative_main(self.store))
        self.assertEqual(self.hook(MAIN_SID), (0, ""))
        self.assertEqual(self.hook(self.STALE), (0, ""))

    def test_next_warns_about_multiple_even_when_resolved(self):
        self.sessions(self.STALE, MAIN_SID)
        self.store.write("main", {"session_id": MAIN_SID, "status": "RUNNING"})
        d = json.loads(self.run_cli(["next", "--json"])[1])
        w = [x for x in d["warnings"] if x.startswith("multiple authoritative Main sessions: ")]
        self.assertEqual(len(w), 1)
        self.assertIn(self.STALE[:8], w[0])
        self.assertIn(MAIN_SID[:8], w[0])
        self.assertEqual(d["counts"]["warnings"], len(d["warnings"]))

    def test_no_warning_for_single_main(self):
        self.sessions(MAIN_SID)
        d = json.loads(self.run_cli(["next", "--json"])[1])
        self.assertFalse([w for w in d["warnings"] if "authoritative" in w])

    def test_warning_does_not_change_fingerprint(self):
        self.sessions(MAIN_SID)
        a = self.items()["fingerprint"]
        self.sessions(self.STALE, MAIN_SID)
        self.assertEqual(self.items()["fingerprint"], a)


class TestWait(Base):
    def setUp(self):
        super().setUp()
        self.mkstore()
        self.write_registry([])
        self.cursor = self.tmp / "cursor.json"

    def wait(self, timeout=10, sleep=None, poll=1):
        return self.run_cli(["wait", "--timeout", str(timeout), "--poll", str(poll), "--cursor", str(self.cursor)],
                            sleep=sleep)

    def test_timeout_exit_3(self):
        rc, out = self.wait(timeout=5)
        self.assertEqual(rc, 3)
        self.assertTrue(json.loads(out)["timeout"])
        self.assertGreaterEqual(self.clock.t - 1_700_000_000.0, 5)

    def test_timeout_is_bounded_by_poll_steps(self):
        calls = []
        self.wait(timeout=10, poll=3, sleep=lambda s: (calls.append(s), self.clock.sleep(s)))
        self.assertLessEqual(len(calls), 5)

    def test_fingerprint_change_exit_0(self):
        def sleep(s):
            self.write_registry([task("T-1", paths=["x/"])])
            self.clock.sleep(s)
        rc, out = self.wait(sleep=sleep)
        d = json.loads(out)
        self.assertEqual((rc, d["reason"]), (0, "fingerprint"))
        self.assertEqual(d["added"], ["start_task:T-1"])

    def test_new_worker_event_exit_0(self):
        def sleep(s):
            self.store.append_event("worker_failed", worker_id="w1")
            self.clock.sleep(s)
        rc, out = self.wait(sleep=sleep)
        d = json.loads(out)
        self.assertEqual((rc, d["reason"], d["new_events"][0]["kind"]), (0, "events", "worker_failed"))

    def test_main_and_replacement_events_count(self):
        for kind in ("main_exited", "replacement_confirmed"):
            if self.cursor.exists():
                self.cursor.unlink()

            def sleep(s, k=kind):
                self.store.append_event(k)
                self.clock.sleep(s)
            self.assertEqual(self.wait(sleep=sleep)[0], 0)

    def test_unrelated_event_does_not_wake(self):
        def sleep(s):
            self.store.append_event("handoff_written")
            self.clock.sleep(s)
        self.assertEqual(self.wait(timeout=3, sleep=sleep)[0], 3)

    def test_events_before_cursor_do_not_wake(self):
        self.store.append_event("worker_failed")
        self.assertEqual(self.wait(timeout=2)[0], 3)  # first call baselines at the current end of the log
        self.assertEqual(self.wait(timeout=2)[0], 3)  # stored cursor already covers it

    def test_event_after_stored_cursor_wakes_next_call(self):
        self.wait(timeout=1)
        self.store.append_event("worker_started")
        self.assertEqual(self.wait(timeout=1)[0], 0)

    def test_cursor_persists_fingerprint(self):
        self.write_registry([task("T-1", paths=["x/"])])
        self.assertEqual(self.wait(timeout=1)[0], 3)
        saved = json.loads(self.cursor.read_text(encoding="utf-8"))
        self.assertEqual(saved["ids"], ["start_task:T-1"])
        self.assertEqual(self.wait(timeout=1)[0], 3)

    def test_corrupt_cursor_tolerated(self):
        self.cursor.write_text("{{{", encoding="utf-8")
        self.assertEqual(self.wait(timeout=1)[0], 3)

    def test_missing_state_dir_tolerated(self):
        shutil.rmtree(self.state)
        rc, _ = self.run_cli(["wait", "--timeout", "1", "--poll", "1"])
        self.assertEqual(rc, 3)
        self.assertFalse(self.state.exists())

    def test_corrupt_event_line_tolerated(self):
        self.store.append_event("worker_failed")
        with open(self.state / "events.jsonl", "ab") as fh:
            fh.write(b"{torn")
        self.assertEqual(self.wait(timeout=1)[0], 3)


class TestMainSettings(Base):
    def test_shape_and_only_stop_hook(self):
        out = self.tmp / "s.json"
        rc, _text = self.run_cli(["main-settings", "--out", str(out), "--python", "py", "--orchestrator",
                                  str(self.tmp / "orch")])
        self.assertEqual(rc, 0)
        d = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(list(d), ["hooks"])
        self.assertEqual(list(d["hooks"]), ["Stop"])
        hook = d["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(hook["type"], "command")
        self.assertIn("autonomy.py stop-hook", hook["command"])
        self.assertTrue(hook["command"].startswith("py -B "))

    def test_paths_with_spaces_are_quoted_with_forward_slashes(self):
        cmd = au.build_hook_command("C:\\Program Files\\Python\\python.exe",
                                    "C:\\My Repo\\orch\\dev_supervisor\\autonomy.py", 2)
        self.assertEqual(cmd, '"C:/Program Files/Python/python.exe" -B "C:/My Repo/orch/dev_supervisor/autonomy.py" '
                              "stop-hook --max-blocks 2")

    def test_refuses_dot_claude_target(self):
        target = self.tmp / ".claude" / "settings.json"
        rc, _text = self.run_cli(["main-settings", "--out", str(target)])
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_generated_command_runs_as_script(self):
        script = Path(au.__file__).resolve()
        p = subprocess.run([sys.executable, "-B", str(script), "stop-hook", "--repo", str(self.repo),
                            "--state-dir", str(self.state)], input=json.dumps({"session_id": "x"}),
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "CREATORSTUDIO_DEV_SUPERVISOR_CONFIG": str(self.tmp / "none.json")})
        self.assertEqual((p.returncode, p.stdout), (0, ""))


class TestNoNetwork(Base):
    def test_sockets_blocked_during_next(self):
        self.write_registry([task("T-1", paths=["x/"])])
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")):
            self.assertEqual(self.run_cli(["next", "--json"])[0], 0)


if __name__ == "__main__":
    unittest.main()
