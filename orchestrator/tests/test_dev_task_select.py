"""Tests for dev_supervisor.task_select (T-DEV-075): verdicts for PLANNED tasks, global reasons, recording, refusals.

Synthetic registry, state directory and route registry in a temporary directory; memory and git are injected.
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dev_supervisor import task_select as ts  # noqa: E402
from dev_supervisor.state_store import StateStore  # noqa: E402

CMD = "cd orchestrator && python -B -I -m unittest discover -s tests -p test_x.py"
GATE = "independent review of the exact diff"
MAIN_SID = "11111111-1111-4111-8111-111111111111"


def task(tid, status="PLANNED", deps=(), paths=("a/",), **kw):
    t = {"id": tid, "status": status, "depends_on": list(deps), "owned_paths": list(paths), "owner": "claude:x",
         "branch": f"task/{tid.lower()}", "worktree": f"../wt/{tid.lower()}", "acceptance_criteria": ["it works"],
         "test_requirements": [CMD, GATE]}
    t.update(kw)
    return t


def no_git(args):
    return 1, ""


class Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.state = self.tmp / "repo", self.tmp / "state"
        self.repo.mkdir()
        self.state.mkdir()
        self.store = StateStore(self.state)
        self.mem = 4096
        self.set_routes([self.route("gemini/impl", 2, ["implementation"])])
        self.set_main(True)
        self.write([task("T-1")])

    @staticmethod
    def route(model, cap, roles):
        return {"model": model, "provider": "gemini", "evidence": "PR #1 test round trip", "cost_zero": True,
                "roles": roles, "capability": cap}

    def set_routes(self, routes):
        (self.state / "verified_routes.json").write_text(json.dumps({"routes": routes}), encoding="utf-8")

    def set_main(self, on):
        sessions = {MAIN_SID: {"session_id": MAIN_SID, "role": "main", "authoritative": True, "status": "ACTIVE"}} if on else {}
        self.store.write("sessions", {"sessions": sessions, "failures": {}})

    def write(self, tasks, shared=None):
        doc = {"schema_version": 1, "shared_files": shared or {"ACTIVE_TASKS.json": "shared"}, "tasks": tasks}
        (self.repo / "ACTIVE_TASKS.json").write_text(json.dumps(doc), encoding="utf-8")

    def sel(self, **kw):
        kw.setdefault("floor", 1024)
        return ts.select(self.repo, self.store, self.state, no_git, lambda: self.mem, **kw)

    def verdict(self, rec, tid):
        return next(v for v in rec["verdicts"] if v["task_id"] == tid)

    def cli(self, *args, **kw):
        out = io.StringIO()
        env = {"CREATORSTUDIO_DEV_SUPERVISOR_CONFIG": str(self.tmp / "none.json")}
        rc = ts.main(list(args) + ["--repo", str(self.repo), "--state-dir", str(self.state)], out, git=no_git,
                     free_memory=lambda: self.mem, env=env, **kw)
        return rc, out.getvalue()


class Verdicts(Env):
    def test_a_complete_planned_task_is_selected(self):
        rec = self.sel()
        self.assertEqual((rec["selected"], rec["candidates"]), ("T-1", 1))
        self.assertEqual(self.verdict(rec, "T-1"), {"task_id": "T-1", "selected": True, "reasons": []})
        self.assertTrue(any("worktree add -b task/t-1" in s for s in rec["next_steps"]))

    def test_only_planned_tasks_are_candidates(self):
        self.write([task(f"T-{i}", st, paths=(f"p{i}/",)) for i, st in enumerate(
            ["ACTIVE", "UNDER_REVIEW", "COMPLETED", "RELEASED", "BLOCKED"])])
        rec = self.sel()
        self.assertEqual((rec["selected"], rec["candidates"], rec["verdicts"]), (None, 0, []))

    def test_the_first_task_in_registry_order_without_a_reason_is_selected(self):
        self.write([task("T-1", paths=[]), task("T-2", paths=("b/",)), task("T-3", paths=("c/",))])
        rec = self.sel()
        self.assertEqual(rec["selected"], "T-2")
        self.assertEqual([v["selected"] for v in rec["verdicts"]], [False, True, False])
        self.assertEqual(self.verdict(rec, "T-3")["reasons"], [])        # fine, but not first: not selected

    def reasons_of(self, **defect):
        self.write([task("T-1", **defect)])
        v = self.verdict(self.sel(), "T-1")
        self.assertFalse(v["selected"])
        return " | ".join(v["reasons"])

    def test_each_task_defect_is_named(self):
        self.assertIn("owned_paths is empty", self.reasons_of(owned_paths=[]))
        self.assertIn("acceptance_criteria is empty", self.reasons_of(acceptance_criteria=[]))
        self.assertIn("not machine-executable", self.reasons_of(test_requirements=[CMD, "run the whole suite"]))
        self.assertIn("no registered task/ branch", self.reasons_of(branch="feature/x"))
        self.assertIn("no registered task/ branch", self.reasons_of(branch=None))
        self.assertIn("no registered worktree", self.reasons_of(worktree=""))
        self.assertIn("no command test requirement", self.reasons_of(test_requirements=[GATE]))
        self.assertIn("test_requirements", self.reasons_of(test_requirements=[]))

    def test_dependency_and_path_and_worker_reasons_come_from_next(self):
        self.write([task("T-1", deps=["T-9"]), task("T-2", deps=["T-3"], paths=("b/",)), task("T-3", "ACTIVE", paths=("c/",)),
                    task("T-4", paths=("c/sub/",))])
        rec = self.sel()
        self.assertIn("unknown task T-9", self.verdict(rec, "T-1")["reasons"][0])
        self.assertIn("waits for T-3 (ACTIVE)", self.verdict(rec, "T-2")["reasons"][0])
        self.assertIn("held by T-3", self.verdict(rec, "T-4")["reasons"][0])
        self.assertIsNone(rec["selected"])

    def test_a_running_worker_rejects_the_task(self):
        from dev_supervisor.workers import Worker, WorkerRegistry, WorkerStatus
        WorkerRegistry(self.store).add(Worker(worker_id="w1", role="worker", task_id="T-1", branch="task/t-1", worktree=str(self.tmp / "w"),
                                              owner_stream="s", status=WorkerStatus.RUNNING))
        self.assertIn("worker is already running", " ".join(self.verdict(self.sel(), "T-1")["reasons"]))

    def test_shared_files_do_not_block(self):
        self.write([task("T-1", "ACTIVE", paths=("ACTIVE_TASKS.json", "x/")), task("T-2", paths=("ACTIVE_TASKS.json", "y/"))])
        self.assertEqual(self.sel()["selected"], "T-2")

    def test_a_malformed_record_does_not_raise(self):
        self.write([task("T-1", owned_paths="a/", acceptance_criteria=None, test_requirements="x", depends_on=None)])
        self.assertFalse(self.verdict(self.sel(), "T-1")["selected"])


class GlobalReasons(Env):
    def all_rejected_with(self, text):
        rec = self.sel()
        self.assertIsNone(rec["selected"])
        self.assertTrue(any(text in r for r in rec["global_reasons"]), rec["global_reasons"])
        self.assertTrue(all(any(text in r for r in v["reasons"]) for v in rec["verdicts"]))

    def test_memory_below_the_floor_or_unreadable(self):
        self.mem = 900
        self.all_rejected_with("memory guard")
        self.mem = None
        self.all_rejected_with("could not be read")

    def test_no_implementation_route(self):
        self.set_routes([])
        self.all_rejected_with("no implementation route")
        self.set_routes([self.route("gemini/rev", 3, ["review"])])
        self.all_rejected_with("no implementation route")
        (self.state / "verified_routes.json").unlink()
        self.all_rejected_with("no implementation route")

    def test_no_independent_review_path(self):
        self.set_main(False)
        self.all_rejected_with("no independent review path")

    def test_an_authoritative_main_is_a_review_path(self):
        self.assertEqual(self.sel()["selected"], "T-1")

    def test_a_capability_3_review_route_that_is_not_the_author_is_a_review_path(self):
        self.set_main(False)
        self.set_routes([self.route("gemini/impl", 2, ["implementation"]), self.route("gemini/rev", 3, ["review"])])
        self.assertEqual(self.sel()["selected"], "T-1")

    def test_a_review_route_that_is_the_authors_own_is_not_independent(self):
        self.set_main(False)
        self.set_routes([self.route("gemini/impl", 3, ["implementation", "review"])])
        self.all_rejected_with("no independent review path")

    def test_a_low_capability_review_route_is_not_enough(self):
        self.set_main(False)
        self.set_routes([self.route("gemini/impl", 2, ["implementation"]), self.route("gemini/rev", 2, ["review"])])
        self.all_rejected_with("no independent review path")

    def test_no_state_directory_at_all(self):
        rec = ts.select(self.repo, None, None, no_git, lambda: self.mem, 1024)
        self.assertIsNone(rec["selected"])
        self.assertTrue(any("state directory is unknown" in r for r in rec["global_reasons"]))


class Recording(Env):
    def events(self):
        evs, _ = self.store.read_events()
        return [e for e in evs if e["kind"].startswith("task_select")]

    def test_nothing_is_written_without_record(self):
        before = sorted(p.name for p in self.state.iterdir())
        rc, out = self.cli()
        self.assertEqual(rc, 0)
        self.assertIn("selected: T-1", out)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), before)
        self.assertEqual(self.events(), [])

    def test_record_writes_one_event_and_the_document_and_not_a_second_time(self):
        self.write([task("T-1", paths=[]), task("T-2", paths=("b/",))])
        rc, out = self.cli("--record")
        self.assertEqual(rc, 0)
        self.assertIn("recorded", out)
        ev = self.events()
        self.assertEqual([e["kind"] for e in ev], ["task_selected"])
        self.assertEqual(ev[0]["data"]["task_id"], "T-2")
        self.assertEqual(ev[0]["data"]["rejected"][0]["task_id"], "T-1")
        doc = self.store.read("selection")
        self.assertEqual((doc["selected"], doc["candidates"]), ("T-2", 2))
        rc, out = self.cli("--record")
        self.assertIn("not recorded again", out)
        self.assertEqual(len(self.events()), 1)

    def test_a_changed_situation_is_recorded_again(self):
        self.cli("--record")
        self.mem = 100
        rc, out = self.cli("--record")
        self.assertEqual(rc, 1)
        self.assertEqual([e["kind"] for e in self.events()], ["task_selected", "task_selection_none"])
        self.assertIn("memory guard", json.dumps(self.events()[1]["data"]))

    def test_exit_codes_and_json(self):
        rc, out = self.cli("--json")
        self.assertEqual((rc, json.loads(out)["selected"]), (0, "T-1"))
        self.write([])
        rc, out = self.cli("--json")
        self.assertEqual((rc, json.loads(out)["selected"]), (1, None))

    def test_unreadable_registry_is_a_refusal(self):
        (self.repo / "ACTIVE_TASKS.json").write_text("{bad", encoding="utf-8")
        rc, out = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("refused", out)
        (self.repo / "ACTIVE_TASKS.json").unlink()
        self.assertEqual(self.cli()[0], 2)

    def test_record_without_a_state_directory_is_a_refusal(self):
        import shutil
        shutil.rmtree(self.state)
        rc, out = self.cli("--record")
        self.assertEqual(rc, 2)

    def test_the_command_changes_nothing_else(self):
        reg = (self.repo / "ACTIVE_TASKS.json").read_bytes()
        calls = []
        ts.select(self.repo, self.store, self.state, lambda a: (calls.append(list(a)), (1, ""))[1], lambda: self.mem, 1024)
        self.assertEqual((self.repo / "ACTIVE_TASKS.json").read_bytes(), reg)
        self.assertTrue(all(c[0] in ("rev-parse", "merge-base", "ls-tree") for c in calls))
        self.assertEqual([p.name for p in self.repo.iterdir()], ["ACTIVE_TASKS.json"])

    def test_the_source_has_no_write_side_commands(self):
        src = Path(ts.__file__).read_text(encoding="utf-8")
        for word in ("subprocess", "Popen", "os.system", "gh pr", "git commit", "git push"):
            self.assertNotIn(word, src.split('"""', 2)[2], word)


if __name__ == "__main__":
    unittest.main()
