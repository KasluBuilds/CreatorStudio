"""Tests for dev_supervisor.worker_tests (T-DEV-073): Supervisor-triggered task tests for finished workers.

Everything is deterministic: a fake git, a fake runner process, a fake clock and a real state store and evidence store in a
temporary directory. Evidence records are produced by the real ``testreq.run_requirements`` with a fake executor, so the
sealed, head- and diff-bound records the scanner reads are the real ones.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dev_supervisor import testreq as T  # noqa: E402
from dev_supervisor import worker_tests as W  # noqa: E402
from dev_supervisor.state_store import StateStore, utc_iso  # noqa: E402
from dev_supervisor.workers import Worker, WorkerRegistry, WorkerStatus  # noqa: E402

TID, BRANCH = "T-DEMO-001", "task/demo"
REQ = "cd orchestrator && python -B -I -m unittest discover -s tests -p test_demo.py"
GATE = "independent review of the exact diff"
T0 = 1_700_000_000.0
OK_TXT = "Ran 3 tests in 0.1s\n\nOK\n"


def sha(n):
    return hashlib.sha1(f"head{n}".encode()).hexdigest()


class FakeGit:
    def __init__(self, registry=None, branch=BRANCH, dirty="", ahead=1):
        self.n, self.branch, self.dirty, self.ahead, self.registry = 0, branch, dirty, ahead, registry
        self.fail_show = False
        self.calls = []

    @property
    def head(self):
        return sha(self.n)

    def commit(self):
        self.n += 1

    def __call__(self, args):
        a = list(args)
        self.calls.append(a)
        if a[:3] == ["--no-optional-locks", "status", "--porcelain=v2"]:
            return 0, (f"# branch.oid {self.head}\n# branch.head {self.branch}\n" + self.dirty).encode()
        if a == ["rev-parse", "HEAD"]:
            return 0, self.head.encode()
        if a == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return 0, self.branch.encode()
        if a[:2] == ["rev-list", "--count"]:
            return 0, str(self.ahead).encode()
        if a[0] == "merge-base":
            return 0, b"m" * 40
        if a[:2] == ["diff", "--binary"]:
            return 0, f"diff of {self.head}\n".encode()
        if a[0] == "show" and a[1].endswith("ACTIVE_TASKS.json"):
            return (1, b"") if self.fail_show else (0, json.dumps(self.registry).encode())
        return 1, b""


class FakeProc:
    def __init__(self, pid):
        self.pid, self.code = pid, None

    def poll(self):
        return self.code


def exec_with(text=OK_TXT, code=0):
    def run(argv, cwd, env, timeout, out):
        Path(out).write_text(text, encoding="utf-8")
        return code, False, ""
    return run


class Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.now = T0
        self.clock = lambda: self.now
        self.store = StateStore(self.state, clock=self.clock)
        self.registry = WorkerRegistry(self.store)
        self.work = self.tmp / "work"
        (self.work / "orchestrator").mkdir(parents=True)
        self.task = {"id": TID, "status": "ACTIVE", "branch": BRANCH, "test_requirements": [REQ, GATE]}
        self.git = FakeGit(registry={"tasks": [self.task]})
        self.gits = {}
        self.spawned, self.procs, self.alive, self.free = [], [], set(), 4096
        self.next_pid = 5000
        self.scanner = self.make_scanner()
        self.scanner.scan()                                   # first pass fixes enabled_since at T0
        self.now = T0 + 100

    def make_scanner(self, **kw):
        def spawn(task_id, worktree, state_dir, *, resume=False, timeout=1800.0):
            self.next_pid += 1
            p = FakeProc(self.next_pid)
            self.spawned.append({"task": task_id, "worktree": worktree, "state_dir": state_dir, "resume": resume, "pid": p.pid})
            self.procs.append(p)
            return p
        args = dict(git_for=lambda path: self.gits.get(str(path), self.git), spawn=spawn, pid_alive=lambda pid: pid in self.alive,
                    free_memory=lambda: self.free, floor=1024, clock=self.clock)
        args.update(kw)
        return W.WorkerTestScanner(self.store, self.registry, self.tmp / "repo", **args)

    def add_worker(self, wid="w1", task=TID, branch=BRANCH, status=WorkerStatus.EXITED, exit_code=0, role="worker",
                   ended=T0 + 50, worktree=None, **kw):
        w = Worker(worker_id=wid, role=role, task_id=task, branch=branch, worktree=str(worktree or self.work),
                   owner_stream="s", status=status, exit_code=exit_code, ended_at=utc_iso(ended),
                   assigned_at=utc_iso(T0 + 1 + len(self.registry.workers)), **kw)
        self.registry.add(w)
        return w

    def entry(self, wid="w1"):
        return (self.store.read(W.DOC) or {}).get("entries", {}).get(wid)

    def events(self, kind=None):
        evs, _ = self.store.read_events()
        return [e for e in evs if e["kind"].startswith("worker_tests_") and (kind is None or e["kind"] == kind)]

    def write_evidence(self, text=OK_TXT, code=0, resume=False):
        ev = T.EvidenceStore(self.state, clock=self.clock)
        return T.run_requirements(self.work, TID, BRANCH, [REQ, GATE], self.git, ev, executor=exec_with(text, code),
                                  resume=resume, pid=999999, clock=self.clock)

    def launch(self):
        self.add_worker()
        ch = self.scanner.scan()
        self.assertEqual(self.entry()["state"], "RUNNING", ch)
        return self.procs[-1]


class Triggering(Env):
    def test_a_successful_worker_starts_one_runner_with_a_fixed_command(self):
        self.add_worker()
        ch = self.scanner.scan()
        self.assertEqual([(s["task"], s["worktree"], s["state_dir"], s["resume"]) for s in self.spawned],
                         [(TID, str(self.work), str(self.state), False)])
        e = self.entry()
        self.assertEqual((e["state"], e["head"], e["attempts"], e["runner_pid"]), ("RUNNING", sha(0), 1, self.spawned[0]["pid"]))
        self.assertEqual([c["state"] for c in ch][-1], "RUNNING")
        self.assertEqual(len(self.events("worker_tests_started")), 1)

    def test_a_repeated_tick_starts_no_second_runner_and_no_second_event(self):
        self.add_worker()
        self.scanner.scan()
        for _ in range(3):
            self.assertEqual(self.scanner.scan(), [])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(len(self.events()), 1)

    def test_workers_that_must_not_be_tested(self):
        cases = {
            "failed": dict(status=WorkerStatus.FAILED), "stopped": dict(status=WorkerStatus.STOPPED),
            "lost": dict(status=WorkerStatus.LOST),
            "nonzero": dict(exit_code=1), "reviewer": dict(role="reviewer"),
            "other branch": dict(branch="review/demo"),
            "before enabling": dict(ended=T0 - 5),
            "too old": dict(ended=T0 + 50),
            "running": dict(status=WorkerStatus.RUNNING, exit_code=None),
        }
        for i, (name, kw) in enumerate(cases.items()):
            with self.subTest(name):
                if name == "too old":
                    self.now = T0 + 50 + W.MAX_AGE_S + 1
                self.add_worker(wid=f"w{i}", **kw)
                self.scanner.scan()
                self.assertIsNone(self.entry(f"w{i}"))
        self.assertEqual(self.spawned, [])

    def test_a_finished_task_is_not_tested(self):
        for st in ("COMPLETED", "RELEASED", "PLANNED"):
            self.task["status"] = st
            self.git.registry = {"tasks": [self.task]}
            self.add_worker(wid="w" + st)
            self.scanner.scan()
        self.assertEqual((self.spawned, self.events()), ([], []))

    def test_an_unreadable_registry_does_nothing(self):
        self.add_worker()
        self.git.fail_show = True
        self.assertEqual(self.scanner.scan(), [])
        self.assertEqual(self.spawned, [])

    def test_feature_switch(self):
        self.assertTrue(W.enabled({}))
        self.assertTrue(W.enabled({W.ENV_SWITCH: "1"}))
        for off in ("0", "off", "False", " NO "):
            self.assertFalse(W.enabled({W.ENV_SWITCH: off}))


class Preconditions(Env):
    def blocked(self, why):
        self.add_worker()
        self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["state"], self.spawned), ("BLOCKED", []))
        self.assertIn(why, e["detail"])
        self.assertEqual(len(self.events("worker_tests_blocked")), 1)
        self.scanner.scan()
        self.assertEqual(len(self.events("worker_tests_blocked")), 1, "an unchanged reason is not repeated")

    def test_missing_task(self):
        self.git.registry = {"tasks": []}
        self.blocked("is not on origin/main")

    def test_branch_registered_to_another_name(self):
        self.task["branch"] = "task/other"
        self.blocked("registered branch")

    def test_no_requirements(self):
        self.task["test_requirements"] = []
        self.blocked("test_requirements")

    def test_manual_requirement(self):
        self.task["test_requirements"] = [REQ, "run the whole suite before merge"]
        self.blocked("not machine-executable")

    def test_dirty_worktree(self):
        self.git.dirty = "1 .M N... 100644 100644 100644 a b orchestrator/x.py\n"
        self.blocked("uncommitted changes")

    def test_wrong_branch_checked_out(self):
        self.git.branch = "task/somewhere-else"
        self.blocked("not on the registered branch")

    def test_no_commit_ahead_of_base(self):
        self.git.ahead = 0
        self.blocked("no commit ahead")

    def test_missing_worktree_directory(self):
        self.add_worker(worktree=self.tmp / "gone")
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], self.spawned), ("BLOCKED", []))

    def test_a_cleared_cause_lets_a_later_pass_proceed(self):
        self.git.dirty = "1 .M N... x\n"
        self.add_worker()
        self.scanner.scan()
        self.assertEqual(self.entry()["state"], "BLOCKED")
        self.git.dirty = ""
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], len(self.spawned)), ("RUNNING", 1))


class Verdicts(Env):
    def finish(self, text=OK_TXT, code=0):
        p = self.launch()
        self.write_evidence(text, code)
        p.code = 0
        self.scanner.scan()
        return self.entry()

    def test_a_passing_record_gives_passed_once(self):
        e = self.finish()
        self.assertEqual(e["state"], "PASSED")
        self.assertEqual(len(self.events("worker_tests_passed")), 1)
        self.scanner.scan()
        self.scanner.scan()
        self.assertEqual((len(self.spawned), len(self.events())), (1, 2))      # started, passed

    def test_a_failing_test_gives_failed_with_an_actionable_detail(self):
        e = self.finish("FAIL: test_x (t.T)\nRan 3 tests in 0.1s\n\nFAILED (failures=1)\n", 1)
        self.assertEqual(e["state"], "FAILED")
        self.assertIn("failed", e["detail"])
        self.assertIn(f"testreq show --task {TID}", e["detail"])
        self.assertEqual(len(self.events("worker_tests_failed")), 1)
        self.scanner.scan()
        self.assertEqual(len(self.spawned), 1, "a failed head is not run again")

    def test_skipped_tests_are_never_a_pass(self):
        e = self.finish("Ran 2 tests in 0.1s\n\nOK (skipped=2)\n")
        self.assertEqual(e["state"], "FAILED")
        self.assertIn("skipped", e["detail"].lower())

    def test_an_existing_passing_record_for_this_exact_head_is_used_without_a_run(self):
        self.write_evidence()
        self.add_worker()
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], self.spawned), ("PASSED", []))

    def test_a_record_for_another_head_is_never_used(self):
        self.write_evidence()
        self.git.commit()
        self.add_worker()
        self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["state"], e["head"], len(self.spawned)), ("RUNNING", sha(1), 1))

    def test_a_record_for_another_diff_is_never_used(self):
        rec = self.write_evidence()
        rec = dict(rec, diff_sha256="0" * 16)
        T.EvidenceStore(self.state).save(rec)                 # re-sealed, but bound to another diff
        self.add_worker()
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], len(self.spawned)), ("RUNNING", 1))

    def test_a_changed_requirement_list_invalidates_the_record(self):
        self.write_evidence()
        self.task["test_requirements"] = [REQ.replace("test_demo", "test_other"), GATE]
        self.add_worker()
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], len(self.spawned)), ("RUNNING", 1))

    def test_a_damaged_record_is_never_used(self):
        self.write_evidence()
        path = next((self.state / "test_evidence").glob("tev_*.json"))
        path.write_text("{ damaged", encoding="utf-8")
        for bak in (self.state / "test_evidence").glob("*.bak"):
            bak.unlink()
        self.add_worker()
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], len(self.spawned)), ("RUNNING", 1))

    def test_a_new_head_after_a_decision_is_evaluated_again(self):
        e = self.finish("Ran 3 tests in 0.1s\n\nFAILED (failures=1)\n", 1)
        self.assertEqual(e["state"], "FAILED")
        self.git.commit()
        self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["state"], e["head"], e["attempts"], len(self.spawned)), ("RUNNING", sha(1), 1, 2))


class Recovery(Env):
    def test_a_live_runner_is_waited_for_after_a_supervisor_restart(self):
        p = self.launch()
        self.alive.add(p.pid)
        again = self.make_scanner()                                   # a new process: no handles
        self.assertEqual(again.scan(), [])
        self.assertEqual(len(self.spawned), 1)
        self.write_evidence()
        self.alive.discard(p.pid)
        again.scan()
        self.assertEqual(self.entry()["state"], "PASSED")

    def test_a_dead_runner_without_a_complete_record_is_relaunched_with_resume(self):
        p = self.launch()
        p.code = -9                                                    # the runner died; nothing was recorded
        self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["state"], e["attempts"], self.spawned[-1]["resume"], len(self.spawned)), ("RUNNING", 2, True, 2))
        self.assertEqual(len(self.events("worker_tests_interrupted")), 1)

    def test_after_a_restart_a_dead_runner_is_relaunched(self):
        self.launch()
        again = self.make_scanner()                                    # pid not alive, no handle
        again.scan()
        self.assertEqual((self.entry()["attempts"], len(self.spawned)), (2, 2))

    def test_an_interrupted_record_is_not_a_pass_and_is_relaunched(self):
        p = self.launch()
        ev = T.EvidenceStore(self.state, clock=self.clock)
        try:
            T.run_requirements(self.work, TID, BRANCH, [REQ, GATE], self.git, ev, pid=999999,
                               executor=lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        except KeyboardInterrupt:
            pass
        p.code = 1
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], len(self.spawned), self.spawned[-1]["resume"]), ("RUNNING", 2, True))

    def test_three_interrupted_attempts_end_blocked(self):
        for i in range(W.MAX_ATTEMPTS):
            p = self.launch() if i == 0 else self.procs[-1]
            p.code = -9
            self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["state"], len(self.spawned)), ("BLOCKED", W.MAX_ATTEMPTS))
        self.assertIn("run it by hand", e["detail"])
        self.scanner.scan()
        self.assertEqual(len(self.spawned), W.MAX_ATTEMPTS)

    def test_a_crash_between_the_launch_record_and_the_pid_does_not_start_a_second_runner(self):
        self.add_worker()
        self.scanner.scan()
        doc = self.store.read(W.DOC)
        doc["entries"]["w1"]["runner_pid"] = None                      # the pid was never saved
        self.store.write(W.DOC, doc)
        self.now += W.ADOPT_GRACE_S - 1
        self.make_scanner().scan()
        self.assertEqual(len(self.spawned), 1)
        self.now += 5                                                  # grace over, nothing alive, no record
        self.make_scanner().scan()
        self.assertEqual((len(self.spawned), self.entry()["attempts"]), (2, 2))

    def test_a_live_run_found_in_the_evidence_store_is_adopted_not_duplicated(self):
        self.add_worker()
        ev = T.EvidenceStore(self.state, clock=self.clock)
        done = self.write_evidence()
        running = dict(done, state="running", finished=None, pid=777, verdict="not_run")
        ev.save(running)
        self.alive.add(777)
        self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["state"], e["runner_pid"], self.spawned), ("RUNNING", 777, []))

    def test_the_state_survives_a_restart(self):
        self.launch()
        first = self.store.read(W.DOC)
        again = self.make_scanner()
        again.scan()
        second = self.store.read(W.DOC)
        self.assertEqual(first["enabled_since"], second["enabled_since"])
        self.assertEqual(second["entries"]["w1"]["head"], sha(0))


class Limits(Env):
    def test_one_runner_at_a_time(self):
        other = dict(self.task, id="T-DEMO-002", branch="task/demo2")
        self.git.registry = {"tasks": [self.task, other]}
        work2 = self.tmp / "work2"
        (work2 / "orchestrator").mkdir(parents=True)
        self.gits[str(work2)] = FakeGit(registry=self.git.registry, branch="task/demo2")
        self.add_worker("w1")
        self.add_worker("w2", task="T-DEMO-002", branch="task/demo2", worktree=work2)
        self.scanner.scan()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.entry("w1")["state"], "RUNNING")
        self.assertNotEqual(self.entry("w2")["state"], "RUNNING")

    def test_pending_work_starts_when_the_slot_is_free(self):
        self.test_one_runner_at_a_time()
        self.procs[0].code = 0
        self.write_evidence()
        self.scanner.scan()
        self.assertEqual((self.entry("w1")["state"], self.entry("w2")["state"], len(self.spawned)), ("PASSED", "RUNNING", 2))

    def test_no_launch_below_the_memory_floor_and_no_attempt_is_used(self):
        self.free = 512
        self.add_worker()
        self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["state"], self.spawned, self.events()), ("PENDING", [], []))
        self.assertIn("512 MB free", e["detail"])
        self.free = None
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], self.spawned), ("PENDING", []))
        self.free = 4096
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], self.entry()["attempts"]), ("RUNNING", 1))

    def test_a_spawn_failure_is_recorded_and_not_raised(self):
        def boom(*a, **k):
            raise OSError("no such file")
        sc = self.make_scanner(spawn=boom)
        self.add_worker()
        sc.scan()
        e = self.entry()
        self.assertEqual(e["state"], "BLOCKED")
        self.assertIn("could not be started", e["detail"])
        self.assertNotIn("no such file", json.dumps(e))

    def test_the_state_document_holds_no_output_or_environment(self):
        self.launch()
        self.write_evidence("Ran 1 test in 0s\nFAILED\nsecret output line\n", 1)
        self.procs[-1].code = 0
        self.scanner.scan()
        raw = json.dumps(self.store.read(W.DOC))
        self.assertNotIn("secret output line", raw)
        self.assertNotIn("output_tail", raw)
        self.assertNotIn("env", json.loads(raw)["entries"]["w1"])


if __name__ == "__main__":
    unittest.main()
