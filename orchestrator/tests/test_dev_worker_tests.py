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
        def spawn(task_id, worktree, state_dir, *, resume=False, timeout=1800.0, base="origin/main"):
            self.next_pid += 1
            p = FakeProc(self.next_pid)
            on_disk = (self.store.read(W.DOC) or {}).get("entries", {}).get(next(
                (w.worker_id for w in self.registry.workers.values() if w.task_id == task_id), ""), {})
            self.spawned.append({"task": task_id, "worktree": worktree, "state_dir": state_dir, "resume": resume, "pid": p.pid,
                                 "base": base, "on_disk": (on_disk.get("state"), on_disk.get("runner_pid"), bool(on_disk.get("launched_at")))})
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
        self.assertEqual([(s["task"], s["worktree"], s["state_dir"], s["resume"], s["base"]) for s in self.spawned],
                         [(TID, str(self.work), str(self.state), False, "origin/main")])
        self.assertEqual(self.spawned[0]["on_disk"], ("RUNNING", None, True), "the launch is on disk before the process starts")
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


class Review(Env):
    """Findings of the independent review of the first head."""

    def test_a_moved_head_does_not_free_the_slot_while_the_old_runner_lives(self):
        p = self.launch()
        self.alive.add(p.pid)
        self.git.commit()                                       # someone commits in the worktree under a live runner
        for _ in range(3):
            self.scanner.scan()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.entry()["state"], "RUNNING")

    def test_a_blocked_look_at_the_worktree_does_not_free_the_slot_for_another_worker(self):
        work2 = self.tmp / "work2"
        (work2 / "orchestrator").mkdir(parents=True)
        other = dict(self.task, id="T-DEMO-002", branch="task/demo2")
        self.git.registry = {"tasks": [self.task, other]}
        self.gits = getattr(self, "gits", {})
        p = self.launch()
        self.git.dirty = "1 .M N... x\n"
        self.git.commit()
        self.gits[str(work2)] = FakeGit(registry=self.git.registry, branch="task/demo2")
        self.add_worker("w2", task="T-DEMO-002", branch="task/demo2", worktree=work2)
        self.scanner.scan()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.entry("w2")["state"], "PENDING")

    def test_attempts_belong_to_the_task_and_head_not_to_the_worker(self):
        work2 = self.tmp / "work2"
        (work2 / "orchestrator").mkdir(parents=True)
        self.gits[str(work2)] = self.git                           # a continuation worker on the same branch and head
        p = self.launch()
        for wid in ("w1", "w2"):
            if wid == "w2":
                self.add_worker("w2", worktree=work2, status=WorkerStatus.EXITED)
            for _ in range(5):
                for proc in self.procs:
                    proc.code = -9
                self.scanner.scan()
        self.assertEqual(len(self.spawned), W.MAX_ATTEMPTS)
        self.assertEqual(len(self.events("worker_tests_started")), W.MAX_ATTEMPTS)

    def test_a_new_head_gets_a_fresh_attempt_budget(self):
        p = self.launch()
        p.code = -9
        self.scanner.scan()
        self.git.commit()
        self.procs[-1].code = -9
        self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["head"], e["attempts"], self.spawned[-1]["resume"]), (sha(1), 1, False))

    def test_changed_requirements_are_evaluated_again_after_a_verdict(self):
        p = self.launch()
        self.write_evidence()
        p.code = 0
        self.scanner.scan()
        self.assertEqual(self.entry()["state"], "PASSED")
        self.task["test_requirements"] = [REQ.replace("test_demo", "test_other"), GATE]
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], len(self.spawned)), ("RUNNING", 2))

    def test_one_workers_exception_does_not_stop_the_others_and_is_recorded(self):
        class Boom(FakeGit):
            def __call__(self, args):
                raise RuntimeError("secret detail that must not be stored")
        boom_dir, ok_dir = self.tmp / "boom", self.tmp / "ok"
        for d in (boom_dir, ok_dir):
            (d / "orchestrator").mkdir(parents=True)
        self.gits[str(boom_dir)] = Boom()
        self.git.registry["tasks"].append(dict(self.task, id="T-DEMO-009", branch="task/nine"))
        self.add_worker("w0", task="T-DEMO-009", branch="task/nine", worktree=boom_dir)
        self.add_worker("w1", worktree=ok_dir)
        self.scanner.scan()
        self.assertEqual((self.entry("w0")["state"], self.entry("w0")["detail"]), ("BLOCKED", "the evaluation failed (RuntimeError)"))
        self.assertEqual(self.entry("w1")["state"], "RUNNING")
        self.assertNotIn("secret detail", json.dumps(self.store.read(W.DOC)))

    def test_a_damaged_worktree_field_is_a_block_not_an_exception(self):
        w = self.add_worker("w0")
        w.worktree = None
        self.scanner.scan()
        self.assertEqual(self.entry("w0")["state"], "BLOCKED")

    def test_no_candidate_means_no_git_command_and_only_the_enabled_stamp_is_written(self):
        self.git.calls.clear()
        self.add_worker(status=WorkerStatus.FAILED)
        writes = []
        real = self.store.write
        self.store.write = lambda name, data: (writes.append(name), real(name, data))[1]
        self.scanner.scan()
        self.assertEqual((self.git.calls, writes), ([], []))

    def test_an_unchanged_state_is_neither_rewritten_nor_announced(self):
        self.git.dirty = "1 .M N... x\n"
        self.add_worker()
        self.scanner.scan()
        first = self.store.read(W.DOC)["entries"]["w1"]["updated"]
        writes = []
        real = self.store.write
        self.store.write = lambda name, data: (writes.append(name), real(name, data))[1]
        self.now += 60
        for _ in range(3):
            self.scanner.scan()
        self.assertEqual((writes, self.store.read(W.DOC)["entries"]["w1"]["updated"], len(self.events())), ([], first, 1))

    def test_the_base_ref_is_passed_to_the_runner(self):
        sc = self.make_scanner(base="origin/develop")
        self.git.registry = {"tasks": [self.task]}
        self.add_worker()
        sc.scan()
        self.assertEqual(self.spawned[0]["base"], "origin/develop")


class SecondReview(Env):
    """Findings of the second independent review."""

    def test_a_dead_runners_pid_is_cleared_so_a_reused_pid_cannot_jam_the_slot(self):
        p = self.launch()
        pid = p.pid
        self.git.commit()
        self.git.dirty = "1 .M N... x\n"                          # moved head, dirty worktree: BLOCKED while the runner lives
        self.alive.add(pid)
        self.scanner.scan()
        p.code = 0                                                 # the runner ends
        self.alive.discard(pid)
        self.scanner.scan()
        self.assertEqual(self.entry()["state"], "BLOCKED")
        self.assertIsNone(self.entry()["runner_pid"])
        self.alive.add(pid)                                        # the OS hands the pid to something else
        self.git.dirty = ""
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], len(self.spawned)), ("RUNNING", 2))
        other = dict(self.task, id="T-DEMO-002", branch="task/demo2")
        self.git.registry = {"tasks": [self.task, other]}

    def test_a_pid_reused_after_the_runner_finished_does_not_hide_its_complete_record(self):
        p = self.launch()
        self.write_evidence()
        rec = self.ev_record()
        T.EvidenceStore(self.state).save(dict(rec, pid=p.pid))     # the record names this runner's pid
        again = self.make_scanner()
        self.alive.add(p.pid)                                      # ...and the pid now belongs to another process
        again.scan()
        self.assertEqual(self.entry()["state"], "PASSED")

    def ev_record(self):
        return T.EvidenceStore(self.state).load(TID, self.git.head)[0]

    def test_a_path_that_looks_like_a_credential_does_not_cause_a_rewrite_or_event_on_every_pass(self):
        odd = self.tmp / "task-governance-foundation-with-a-long-name"
        (odd / "orchestrator").mkdir(parents=True)
        self.gits[str(odd)] = self.git
        self.git.dirty = "1 .M N... x\n"
        self.add_worker(worktree=odd)
        self.scanner.scan()
        writes = []
        real = self.store.write
        self.store.write = lambda name, data: (writes.append(name), real(name, data))[1]
        for _ in range(3):
            self.now += 5
            self.scanner.scan()
        self.assertEqual((writes, len(self.events("worker_tests_blocked"))), ([], 1))

    def test_a_moved_base_makes_a_decided_head_be_evaluated_again(self):
        base = {"sha": "b" * 40}
        orig = self.git.__class__.__call__

        def with_base(git, args):
            return (0, base["sha"].encode()) if list(args) == ["rev-parse", "origin/main"] else orig(git, args)
        self.git.__class__.__call__ = with_base
        self.addCleanup(setattr, self.git.__class__, "__call__", orig)
        p = self.launch()
        self.write_evidence()
        p.code = 0
        self.scanner.scan()
        self.assertEqual(self.entry()["state"], "PASSED")
        self.scanner.scan()
        self.assertEqual(len(self.spawned), 1)
        base["sha"] = "c" * 40
        self.scanner.scan()
        self.assertEqual(self.entry()["base_sha"], "c" * 40)       # evaluated again against the new base

    def test_a_failed_spawn_does_not_use_up_an_attempt(self):
        calls = []

        def refuse(*a, **k):
            calls.append(1)
            raise OSError("x")
        sc = self.make_scanner(spawn=refuse)
        self.add_worker()
        for _ in range(5):
            sc.scan()
        self.assertEqual(len(calls), 1, "no retry storm within the retry interval")
        for _ in range(W.MAX_ATTEMPTS + 1):
            self.now += W.SPAWN_RETRY_S + 1
            sc.scan()
        self.assertEqual(len(calls), W.MAX_ATTEMPTS + 2)
        self.assertIn("could not be started", self.entry()["detail"])
        self.assertEqual(self.store.read(W.DOC)["attempts"][f"{TID}:{sha(0)}"]["n"], 0)

    def test_a_damaged_entry_in_the_document_is_ignored(self):
        doc = self.store.read(W.DOC)
        doc["entries"]["junk"] = 5
        self.store.write(W.DOC, doc)
        self.add_worker()
        self.scanner.scan()
        self.assertEqual(self.entry()["state"], "RUNNING")

    def test_a_skipped_command_in_a_record_is_never_a_pass(self):
        rec = self.write_evidence()
        rec = json.loads(json.dumps(rec))
        for r in rec["results"]:
            if r["kind"] == "command":
                r["status"] = "skipped"
        T.EvidenceStore(self.state).save(rec)
        self.add_worker()
        self.scanner.scan()
        self.assertNotEqual(self.entry()["state"], "PASSED")
        self.assertEqual(self.entry()["state"], "FAILED")


class ThirdReview(Env):
    """Findings of the third independent review."""

    def test_gate_only_requirements_are_never_a_pass_without_a_record(self):
        self.task["test_requirements"] = [GATE]
        self.add_worker()
        self.scanner.scan()
        e = self.entry()
        self.assertEqual((e["state"], self.spawned), ("BLOCKED", []))
        self.assertIn("no command test requirement", e["detail"])

    def test_a_manual_rerun_that_passes_replaces_a_recorded_failure(self):
        p = self.launch()
        self.write_evidence("Ran 3 tests in 0.1s\n\nFAILED (failures=1)\n", 1)
        p.code = 0
        self.scanner.scan()
        self.assertEqual(self.entry()["state"], "FAILED")
        self.write_evidence()                                       # the operator re-runs by hand; it passes now
        self.scanner.scan()
        self.assertEqual((self.entry()["state"], len(self.spawned)), ("PASSED", 1))

    def test_a_reused_pid_cannot_hold_the_slot_forever(self):
        p = self.launch()
        self.alive.add(p.pid)                                       # the runner died; the OS gave its pid to a long-lived process
        p.code = None
        self.now += 6 * 3600 + 1000
        self.add_worker("w2", task="T-DEMO-002", branch="task/demo2", worktree=self.tmp / "work2", ended=self.now - 10)
        (self.tmp / "work2" / "orchestrator").mkdir(parents=True)
        self.git.registry = {"tasks": [self.task, dict(self.task, id="T-DEMO-002", branch="task/demo2")]}
        self.gits[str(self.tmp / "work2")] = FakeGit(registry=self.git.registry, branch="task/demo2")
        fresh = self.make_scanner()                                  # no handle: only the pid check and the age budget
        fresh.scan()
        self.assertEqual(len(self.spawned), 2)                       # w1 is relaunched (its pid is not trusted); nothing is stuck
        self.assertEqual((self.entry("w1")["state"], self.entry("w1")["attempts"]), ("RUNNING", 2))
        self.assertEqual(self.entry("w2")["state"], "PENDING")

    def test_a_dead_runners_pid_is_cleared_even_when_the_worker_is_no_longer_evaluated(self):
        p = self.launch()
        p.code = 0
        self.task["status"] = "COMPLETED"                           # the task finished: the entry is skipped from now on
        self.git.registry = {"tasks": [self.task]}
        self.scanner.scan()
        self.assertIsNone(self.entry()["runner_pid"])

    def test_the_pid_is_on_disk_before_any_other_work_of_the_pass(self):
        self.add_worker("w1")
        seen = []
        real = self.store.write

        def spy(name, data):
            if name == W.DOC:
                seen.append({k: v.get("runner_pid") for k, v in data["entries"].items()})
            return real(name, data)
        self.store.write = spy
        self.scanner.scan()
        self.assertIn({"w1": self.spawned[0]["pid"]}, seen)

    def test_an_orphan_runner_found_after_the_grace_period_still_holds_the_slot(self):
        self.add_worker("w1")
        self.scanner.scan()
        doc = self.store.read(W.DOC)
        doc["entries"]["w1"]["runner_pid"] = None                   # the pid never reached the disk
        self.store.write(W.DOC, doc)
        done = self.write_evidence()
        T.EvidenceStore(self.state).save(dict(done, state="running", finished=None, pid=4242, verdict="not_run"))
        self.alive.add(4242)
        self.now += W.ADOPT_GRACE_S + 5
        work2 = self.tmp / "work2"
        (work2 / "orchestrator").mkdir(parents=True)
        self.git.registry = {"tasks": [self.task, dict(self.task, id="T-DEMO-002", branch="task/demo2")]}
        self.gits[str(work2)] = FakeGit(registry=self.git.registry, branch="task/demo2")
        self.add_worker("w2", task="T-DEMO-002", branch="task/demo2", worktree=work2, ended=self.now - 5)
        self.make_scanner().scan()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.entry("w2")["state"], "PENDING")

    def test_attempt_counters_are_pruned_by_age_never_by_name(self):
        doc = self.store.read(W.DOC)
        doc["attempts"] = {f"T-OLD-{i:03d}:{'a' * 40}": {"n": 3, "t": utc_stamp(i)} for i in range(W.MAX_ATTEMPT_KEYS)}
        doc["attempts"][f"T-AAA-001:{sha(0)}"] = {"n": 2, "t": utc_stamp(10_000)}      # sorts first, but is the newest
        self.store.write(W.DOC, doc)
        self.add_worker()
        self.scanner.scan()
        att = self.store.read(W.DOC)["attempts"]
        self.assertIn(f"T-AAA-001:{sha(0)}", att)
        self.assertNotIn(f"T-OLD-000:{'a' * 40}", att)             # the oldest went
        self.assertLessEqual(len(att), W.MAX_ATTEMPT_KEYS)
        self.assertEqual(att[f"{TID}:{sha(0)}"]["n"], 1)

    def test_old_decided_entries_and_entries_of_unknown_workers_are_dropped(self):
        doc = self.store.read(W.DOC)
        doc["entries"]["ghost"] = {"worker_id": "ghost", "task_id": TID, "state": "PASSED", "updated": utc_iso(self.now)}
        self.store.write(W.DOC, doc)
        self.scanner.scan()
        self.assertNotIn("ghost", self.store.read(W.DOC)["entries"])

    def test_a_changed_field_alone_does_not_append_an_event(self):
        self.git.dirty = "1 .M N... x\n"
        self.add_worker()
        self.scanner.scan()
        n = len(self.events())
        self.git.commit()                                           # new head, same reason: fields change, state and detail do not
        self.scanner.scan()
        self.assertEqual(len(self.events()), n)


def utc_stamp(i):
    return utc_iso(T0 + i)


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
        self.assertIn("below the 1024 MB floor", e["detail"])
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
