"""Tests for dev_supervisor.testreq (T-DEV-065): classification, execution, evidence binding and recovery.

Most logic is tested with a fake git and a fake executor (fast). The behaviour that matters most is also tested for real:
a real temporary git repository (with an origin) and real child processes run passing, failing, skipped, zero-test,
timed-out, worktree-modifying and secret-printing commands, and the CLI runs end to end. Process and git start-up are
slow on Windows, so the real repository is built once and copied for each test that needs one.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dev_supervisor import integrate as I  # noqa: E402
from dev_supervisor import testreq as T  # noqa: E402

TID, BRANCH = "T-DEMO-001", "task/demo"
UNIT = "cd orchestrator && python -B -I -m unittest discover -s tests -p {}"
OKTXT = "Ran 1 test in 0.100s\n\nOK\n"
OK_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"
FAIL_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 2)\n"
SKIP_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    @unittest.skip('later')\n    def test_a(self):\n        pass\n"
SLOW_TEST = "import time\nimport unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        time.sleep(60)\n"
WRITES_TEST = ("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
               "        open('../stray_output.txt', 'w').write('x')\n")
ENV_TEST = ("import os\nimport unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
            "        self.assertNotIn('ANTHROPIC_API_KEY', os.environ)\n"
            "        self.assertNotIn('GH_TOKEN', os.environ)\n"
            "        self.assertNotEqual(os.environ.get('CREATORSTUDIO_DEV_SUPERVISOR_DIR'), 'REAL')\n")
LEAK_TEST = ("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
             "        print('gh' + 'p_' + 'A' * 24)\n")


def ok_exec(calls=None, text=OKTXT, code=0):
    def run(argv, cwd, env, timeout, out):
        if calls is not None:
            calls.append(list(argv))
        Path(out).write_text(text, encoding="utf-8")
        return code, False, ""
    return run


class FakeGit:
    """Answers the few git calls testreq makes. ``commit()`` moves the head and changes the diff."""

    def __init__(self, head=None, diff=b"diff one\n", dirty="", branch=BRANCH, registry=None):
        self.n = 0
        self.head, self.diff, self.dirty, self.branch, self.registry = head or self._sha(0), diff, dirty, branch, registry
        self.calls = []

    @staticmethod
    def _sha(n):
        return hashlib.sha1(f"head{n}".encode()).hexdigest()

    def commit(self):
        self.n += 1
        self.head, self.diff = self._sha(self.n), self.diff + b"+more\n"

    def __call__(self, args):
        a = list(args)
        self.calls.append(a)
        if a[:3] == ["--no-optional-locks", "status", "--porcelain=v2"]:
            return 0, (f"# branch.oid {self.head}\n# branch.head {self.branch}\n" + self.dirty).encode()
        if a == ["rev-parse", "HEAD"]:
            return 0, self.head.encode()
        if a == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return 0, self.branch.encode()
        if a[0] == "merge-base":
            return 0, b"m" * 40
        if a[:2] == ["diff", "--binary"]:
            return 0, self.diff
        if a[0] == "show" and a[1].endswith("ACTIVE_TASKS.json") and self.registry is not None:
            return 0, json.dumps(self.registry).encode()
        return 1, b""

    def diff_hash(self):
        return I.diff_hash(self, "origin/main", "HEAD")


class Base(unittest.TestCase):
    """Fake git, fake executor, a temp worktree root with the directories requirements run in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "work"
        (self.root / "orchestrator").mkdir(parents=True)
        (self.root / "dashboard" / "desktop").mkdir(parents=True)
        self.state = self.tmp / "state"
        self.ev = T.EvidenceStore(self.state)
        self.git = FakeGit()

    def run_all(self, reqs, **kw):
        kw.setdefault("executor", ok_exec())
        return T.run_requirements(self.root, TID, BRANCH, reqs, self.git, self.ev, **kw)

    def verdict(self, reqs, record="load", head=None, diff=None, alive=lambda p: False, task=TID):
        head = head or self.git.head
        why = ""
        if record == "load":
            record, why = self.ev.load(task, head)
        return T.check_evidence(T.classify_all(reqs), reqs, record, why, task_id=task, head=head,
                                diff_sha=diff or self.git.diff_hash(), pid_alive=alive)


class Classification(unittest.TestCase):
    def kind(self, text):
        return T.classify(text).kind

    def test_the_real_forms_are_commands(self):
        for text in (
            "cd orchestrator && python -B -I -m unittest discover -s tests",
            "cd orchestrator && python -B -I -m unittest discover -s tests -p test_dev_*.py",
            "cd orchestrator && python -B -m governance validate",
            "cd orchestrator && python -B -m governance validate --against origin/main",
            "cd orchestrator && python -B -m governance handoff --check",
            "cd orchestrator && python -B -m governance scope --base origin/main",
            "python -B dashboard/tracker_check.py --against origin/main",
            "python -B -m governance validate from orchestrator/",
            "python -B -I -m unittest discover -s orchestrator/tests -p test_dev_claim.py from the repository root",
            "python -B -I -m unittest discover -s tests -p 'test_dev_cli.py' from orchestrator/",
            "focused: python -B -m unittest discover -s tests -p test_dev_dispatch_routing.py from orchestrator/",
            "python -B -m unittest tests.test_dev_lifecycle tests.test_dev_cli from orchestrator/",
            'cd dashboard/desktop && node --test "tests/*.test.js"',
        ):
            r = T.classify(text)
            self.assertEqual(r.kind, "command", (text, r.reason))

    def test_directory_and_argv_are_derived_not_shelled(self):
        r = T.classify("cd orchestrator && python -B -I -m unittest discover -s tests -p test_x.py")
        self.assertEqual((r.cwd, r.argv, r.counts),
                         ("orchestrator", ("python", "-B", "-I", "-m", "unittest", "discover", "-s", "tests", "-p", "test_x.py"), True))
        r = T.classify("python -B -m governance validate from orchestrator/")
        self.assertEqual((r.cwd, r.counts), ("orchestrator", False))
        self.assertEqual(T.classify('cd dashboard/desktop && node --test "tests/*.test.js"').cwd, "dashboard/desktop")
        self.assertEqual(T.classify("python -B dashboard/tracker_check.py --against origin/main").cwd, ".")

    def test_gate_covered_prose(self):
        for text in ("independent review of the exact diff", "integrate --preflight passes on the branch",
                     "full per-file validation on the exact head"):
            self.assertEqual(self.kind(text), "gate", text)
        self.assertEqual(self.kind("Independent review of the exact diff"), "manual")   # exact wording only

    def test_prose_and_unsafe_text_is_manual_never_command(self):
        for text in ("full suite before merge", "affected suites: test_dev_review, test_dev_cli",
                     "a test against a real temporary git repository",
                     "cd orchestrator && python -B -I -m unittest discover -s tests && rm -rf x",
                     "cd orchestrator && python -B -m governance validate; echo done",
                     "cd orchestrator && python -B -m governance validate | tee out.txt",
                     "cd orchestrator && python -B -m governance validate > out.txt",
                     "python -B -m governance score --json",
                     "python -m unittest discover -s tests",                      # no -B
                     "python -B -I -m unittest discover -s ../other",
                     "python -B -I -m unittest discover -s tests -p ../x.py",
                     "python -B -I -m unittest discover -s tests -p 'a b.py'",
                     "python -B -c print(1)",
                     "python -B -m pip install x",
                     "rm -rf /",
                     "node --test tests/*.test.js extra",
                     "node script.js",
                     "cd orchestrator && cd .. && python -B -m governance validate",
                     "cd elsewhere && python -B -m governance validate",
                     "python -B -I -m unittest discover -s tests 'unbalanced",
                     "python -B -m unittest tests.test_a; rm x",
                     "python -B -m unittest os.path"):
            r = T.classify(text)
            self.assertEqual(r.kind, "manual", text)
            self.assertTrue(r.reason, text)

    def test_empty_and_malformed_requirements(self):
        for bad in ("", "   ", None, 7, ["x"], {"a": 1}):
            self.assertEqual(T.classify(bad).kind, "manual", repr(bad))
        for bad in (None, [], "python -B -m governance validate", {}):
            with self.assertRaises(T.TestReqError):
                T.classify_all(bad)

    def test_every_real_registry_requirement_classifies_and_no_command_has_shell_syntax(self):
        reg = Path(__file__).resolve().parents[2] / "ACTIVE_TASKS.json"
        if not reg.exists():
            self.skipTest("registry not present")
        kinds = set()
        for task in json.loads(reg.read_text(encoding="utf-8"))["tasks"]:
            for r in T.classify_all(task["test_requirements"]):
                kinds.add(r.kind)
                if r.kind == "command":
                    self.assertTrue(r.argv[0] in ("python", "node") and not any(c in " ".join(r.argv) for c in "&|;<>`$"), r.text)
        self.assertIn("command", kinds)

    def test_requirement_hash_changes_with_text_order_and_length(self):
        a = ["x", "y"]
        self.assertEqual(T.reqs_hash(a), T.reqs_hash(list(a)))
        for other in (["y", "x"], ["x"], ["x", "y2"], ["x", "y", "z"]):
            self.assertNotEqual(T.reqs_hash(a), T.reqs_hash(other))

    def test_parse_counts(self):
        self.assertEqual(T.parse_counts("....\nRan 4 tests in 0.1s\n\nOK\n"), (4, 0))
        self.assertEqual(T.parse_counts("Ran 1 test in 0.0s\n\nOK (skipped=1)\n"), (1, 1))
        self.assertEqual(T.parse_counts("Ran 0 tests in 0.0s\n\nNO TESTS RAN\n"), (0, 0))
        self.assertEqual(T.parse_counts("# tests 5\n# pass 4\n# skip 1\n"), (5, 1))
        self.assertEqual(T.parse_counts("no summary at all"), (None, None))


class Execution(Base):
    """Result handling with a fake executor: every outcome is a distinct status and none of them is a silent pass."""

    def test_success_records_the_command_times_exit_and_output(self):
        calls = []
        rec = self.run_all([UNIT.format("test_a.py"), "independent review of the exact diff"], executor=ok_exec(calls))
        x = rec["results"][0]
        self.assertEqual((rec["state"], rec["verdict"], rec["attempt"]), ("complete", "passed", 1))
        self.assertEqual((x["status"], x["exit_code"], x["tests_run"], x["cwd"], x["reason"]), ("passed", 0, 1, "orchestrator", ""))
        self.assertEqual(x["argv"], ["python", "-B", "-I", "-m", "unittest", "discover", "-s", "tests", "-p", "test_a.py"])
        self.assertEqual(calls[0][0], sys.executable)
        self.assertTrue(x["started"] and x["finished"] and x["duration_s"] >= 0)
        self.assertIn("Ran 1 test", x["output_tail"])
        self.assertEqual(rec["results"][1]["status"], "skipped")           # decided by the review gate, never "passed" here
        self.assertEqual((rec["head"], rec["diff_sha256"]), (self.git.head, self.git.diff_hash()))

    def test_failed_command_keeps_exit_code_failing_tests_and_tail(self):
        text = "FAIL: test_a (x.T.test_a)\nAssertionError: 1 != 2\n\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n"
        rec = self.run_all([UNIT.format("test_a.py")], executor=ok_exec(text=text, code=1))
        x = rec["results"][0]
        self.assertEqual((x["status"], x["exit_code"], rec["verdict"]), ("failed", 1, "failed"))
        self.assertIn("exit code 1", x["reason"])
        self.assertIn("FAIL: test_a", x["reason"])
        self.assertIn("AssertionError", x["output_tail"])
        status, detail = self.verdict([UNIT.format("test_a.py")])
        self.assertEqual(status, "FAIL")
        self.assertIn("requirement 1 failed", detail)
        self.assertIn(f"testreq show --task {TID}", detail)

    def test_exit_zero_with_zero_tests_or_no_summary_is_a_failure(self):
        for text in ("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n", "silence\n"):
            x = self.run_all([UNIT.format("test_a.py")], executor=ok_exec(text=text))["results"][0]
            self.assertEqual(x["status"], "failed", text)
            self.assertIn("ran no tests", x["reason"])

    def test_all_tests_skipped_is_a_failure(self):
        x = self.run_all([UNIT.format("test_a.py")], executor=ok_exec(text="Ran 2 tests in 0s\n\nOK (skipped=2)\n"))["results"][0]
        self.assertEqual((x["status"], x["tests_run"], x["tests_skipped"]), ("failed", 2, 2))
        self.assertIn("skipped", x["reason"])
        x = self.run_all([UNIT.format("test_a.py")], executor=ok_exec(text="Ran 2 tests in 0s\n\nOK (skipped=1)\n"))["results"][0]
        self.assertEqual(x["status"], "passed")

    def test_commands_that_are_not_unittest_need_no_test_count(self):
        x = self.run_all(["python -B -m governance validate from orchestrator/"], executor=ok_exec(text="OK: valid\n"))["results"][0]
        self.assertEqual((x["status"], x["tests_run"]), ("passed", None))

    def test_timeout_is_a_failure_without_an_exit_code(self):
        x = self.run_all([UNIT.format("test_a.py")], timeout=7, executor=lambda *a: (None, True, ""))["results"][0]
        self.assertEqual((x["status"], x["exit_code"], x["reason"]), ("failed", None, "timed out after 7 s"))

    def test_missing_runner_is_blocked_and_nothing_starts(self):
        calls = []
        rec = self.run_all(['cd dashboard/desktop && node --test "tests/*.test.js"'], which=lambda name: None, executor=ok_exec(calls))
        self.assertEqual((rec["results"][0]["status"], calls), ("blocked", []))
        self.assertIn("runner not found: node", rec["results"][0]["reason"])
        self.assertEqual(rec["verdict"], "failed")

    def test_a_runner_that_cannot_start_is_blocked(self):
        rec = self.run_all([UNIT.format("test_a.py")], executor=lambda *a: (None, False, "cannot start: OSError"))
        self.assertEqual((rec["results"][0]["status"], rec["results"][0]["reason"]), ("blocked", "cannot start: OSError"))

    def test_missing_directory_is_blocked(self):
        shutil.rmtree(self.root / "dashboard")
        calls = []
        rec = self.run_all(['cd dashboard/desktop && node --test "tests/*.test.js"'], which=lambda n: "node", executor=ok_exec(calls))
        self.assertEqual((rec["results"][0]["status"], calls), ("blocked", []))
        self.assertIn("directory not found", rec["results"][0]["reason"])

    def test_dirty_worktree_blocks_every_command(self):
        self.git.dirty = "? scratch.txt\n"
        calls = []
        rec = self.run_all([UNIT.format("test_a.py"), "python -B -m governance validate from orchestrator/"], executor=ok_exec(calls))
        self.assertEqual(([x["status"] for x in rec["results"]], calls), (["blocked", "blocked"], []))
        self.assertIn("uncommitted", rec["results"][0]["reason"])
        self.assertEqual(self.verdict([UNIT.format("test_a.py")])[0], "FAIL")

    def test_unreadable_git_state_raises_before_anything_runs(self):
        for broken in (lambda a: (1, b""), lambda a: (0, b"# branch.oid (initial)\n")):
            calls = []
            with self.assertRaises(T.TestReqError):
                T.run_requirements(self.root, TID, BRANCH, [UNIT.format("t.py")], broken, self.ev, executor=ok_exec(calls))
            self.assertEqual(calls, [])

    def test_a_command_that_changes_the_worktree_fails_and_stops_the_run(self):
        def run(argv, cwd, env, timeout, out):
            self.git.dirty = "? stray_output.txt\n"
            return ok_exec()(argv, cwd, env, timeout, out)
        rec = self.run_all([UNIT.format("test_a.py"), UNIT.format("test_b.py")], executor=run)
        self.assertEqual([x["status"] for x in rec["results"]], ["failed", "not_run"])
        self.assertIn("modified the worktree", rec["results"][0]["reason"])
        self.assertIn("stray_output.txt", rec["results"][0]["reason"])
        self.assertIn("modified the worktree", rec["results"][1]["reason"])
        self.assertEqual(rec["verdict"], "failed")

    def test_head_moving_during_the_run_fails_the_requirement(self):
        def run(argv, cwd, env, timeout, out):
            self.git.commit()
            return ok_exec()(argv, cwd, env, timeout, out)
        x = self.run_all([UNIT.format("test_a.py")], executor=run)["results"][0]
        self.assertEqual(x["status"], "failed")
        self.assertIn("head commit changed", x["reason"])

    def test_child_environment_has_no_credentials_and_a_private_state_directory(self):
        seen = {}

        def run(argv, cwd, env, timeout, out):
            seen.update(env=dict(env), existed=os.path.isdir(env["CREATORSTUDIO_DEV_SUPERVISOR_DIR"]))
            return ok_exec()(argv, cwd, env, timeout, out)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "GH_TOKEN": "x", "CREATORSTUDIO_DEV_SUPERVISOR_DIR": "REAL"}):
            self.run_all([UNIT.format("test_a.py")], executor=run)
        env = seen["env"]
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertIn("PATH", {k.upper() for k in env})
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn(env["CREATORSTUDIO_DEV_SUPERVISOR_DIR"], ("REAL", str(self.state)))
        self.assertTrue(seen["existed"])
        self.assertFalse(os.path.exists(env["CREATORSTUDIO_DEV_SUPERVISOR_DIR"]))     # removed after the run

    def test_credentials_in_output_are_not_stored(self):
        secret = "gh" + "p_" + "A" * 24
        self.run_all([UNIT.format("test_a.py")], executor=ok_exec(text=f"token {secret}\n{OKTXT}"))
        stored = (self.ev.directory / f"{T.evidence_name(TID, self.git.head)}.json").read_text(encoding="utf-8")
        self.assertNotIn("A" * 24, stored)
        self.assertIn("[REDACTED]", stored)

    def test_an_unclassifiable_requirement_is_blocked_and_fails_the_verdict(self):
        reqs = [UNIT.format("test_a.py"), "full suite before merge"]
        rec = self.run_all(reqs)
        self.assertEqual([x["status"] for x in rec["results"]], ["passed", "blocked"])
        self.assertEqual(rec["verdict"], "failed")
        status, detail = self.verdict(reqs)
        self.assertEqual(status, "FAIL")
        self.assertIn("not machine-executable", detail)

    def test_gate_only_requirements_need_no_run(self):
        reqs = ["independent review of the exact diff", "integrate --preflight passes on the branch"]
        status, detail = self.verdict(reqs, record=None)
        self.assertEqual(status, "PASS")
        self.assertIn("no executable requirement", detail)


class Binding(Base):
    REQS = [UNIT.format("test_a.py")]

    def setUp(self):
        super().setUp()
        self.rec = self.run_all(self.REQS)

    def test_same_head_diff_and_requirements_pass_and_survive_a_restart(self):
        self.assertEqual(self.verdict(self.REQS)[0], "PASS")
        self.ev = T.EvidenceStore(self.state)                      # a new process over the same directory
        status, detail = self.verdict(self.REQS)
        self.assertEqual(status, "PASS", detail)
        self.assertIn(self.git.head[:12], detail)

    def test_a_new_commit_makes_the_evidence_stale(self):
        old = self.rec
        self.git.commit()
        status, detail = self.verdict(self.REQS)                   # the record file for the new head does not exist
        self.assertEqual(status, "FAIL")
        self.assertIn("no test evidence", detail)
        status, detail = self.verdict(self.REQS, record=old)       # handed over explicitly it is for another head
        self.assertEqual(status, "FAIL")
        self.assertIn("different head commit", detail)

    def test_a_different_diff_is_not_accepted(self):
        status, detail = self.verdict(self.REQS, diff="0" * 16)
        self.assertEqual(status, "FAIL")
        self.assertIn("different diff", detail)

    def test_changed_requirement_text_is_not_accepted(self):
        for texts in ([UNIT.format("test_b.py")], [UNIT.format("test_a.py"), "independent review of the exact diff"]):
            status, detail = self.verdict(texts)
            self.assertEqual(status, "FAIL", texts)
            self.assertIn("different requirement list", detail)

    def test_another_task_is_not_accepted(self):
        status, detail = self.verdict(self.REQS, record=self.rec, task="T-OTHER-002")
        self.assertEqual(status, "FAIL")
        self.assertIn("different task", detail)

    def test_an_edited_record_fails_its_checksum(self):
        name = T.evidence_name(TID, self.git.head)
        doc = self.ev.store.read(name)
        doc["attempt"] = 99                                        # any edit, with the old checksum kept
        self.ev.store.write(name, doc)
        status, detail = self.verdict(self.REQS)
        self.assertEqual(status, "FAIL")
        self.assertIn("damaged", detail)

    def test_a_failed_result_cannot_be_hidden_by_the_summary_field(self):
        rec = self.run_all(self.REQS, executor=ok_exec(code=1, text="FAILED\n"))
        self.assertEqual(rec["verdict"], "failed")
        forged = json.loads(json.dumps(rec))
        forged["verdict"] = "passed"
        forged = self.ev.save(forged)                              # a forger who also recomputes the checksum
        self.assertEqual(self.verdict(self.REQS, record=forged)[0], "FAIL")   # the per-requirement status decides

    def test_missing_and_unreadable_records_fail_with_the_command_to_run(self):
        status, detail = self.verdict(self.REQS, head="f" * 40)
        self.assertEqual(status, "FAIL")
        self.assertIn(f"python -B -m dev_supervisor.testreq run --task {TID}", detail)
        name = T.evidence_name(TID, self.git.head)
        (self.ev.directory / f"{name}.json").write_text("{not json", encoding="utf-8")
        for bak in self.ev.directory.glob(f"{name}.json.bak"):
            bak.unlink()
        rec, why = self.ev.load(TID, self.git.head)
        self.assertIsNone(rec)
        self.assertTrue(why.startswith("unreadable"))
        self.assertEqual(self.verdict(self.REQS)[0], "FAIL")

    def test_a_damaged_record_falls_back_to_its_sealed_backup_which_is_never_better_than_the_truth(self):
        self.run_all(self.REQS)                                    # every save keeps the previous version as the backup
        name = T.evidence_name(TID, self.git.head)
        (self.ev.directory / f"{name}.json").write_text("{not json", encoding="utf-8")
        rec, why = self.ev.load(TID, self.git.head)
        self.assertEqual(why, "")                                  # recovered by the state store
        self.assertTrue(T._sealed(rec))                            # and still carrying a valid checksum
        status, detail = self.verdict(self.REQS, record=rec)
        self.assertEqual(rec["state"], "running")                  # the backup is the version before the last write
        self.assertEqual(status, "FAIL")
        self.assertNotIn("damaged", detail)


class Recovery(Base):
    REQS = [UNIT.format("test_one.py"), UNIT.format("test_two.py")]

    class Crash(BaseException):
        pass

    def crash_on_second(self, calls):
        def run(argv, cwd, env, timeout, out):
            calls.append(argv)
            if len(calls) == 2:
                raise self.Crash()
            return ok_exec()(argv, cwd, env, timeout, out)
        return run

    def test_interrupt_records_the_state_and_blocks_integration(self):
        calls = []

        def run(argv, cwd, env, timeout, out):
            calls.append(argv)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return ok_exec()(argv, cwd, env, timeout, out)
        with self.assertRaises(KeyboardInterrupt):
            self.run_all(self.REQS + [UNIT.format("test_three.py")], executor=run)
        rec, _ = self.ev.load(TID, self.git.head)
        self.assertEqual((rec["state"], rec["verdict"]), ("interrupted", "failed"))
        self.assertEqual([x["status"] for x in rec["results"]], ["passed", "not_run", "not_run"])
        self.assertIn("interrupted", rec["results"][1]["reason"])
        self.assertIn("interrupted", rec["results"][2]["reason"])
        status, detail = self.verdict(self.REQS + [UNIT.format("test_three.py")])
        self.assertEqual(status, "FAIL")
        self.assertIn("did not finish", detail)

    def test_a_crash_leaves_a_running_record_that_a_restart_recognises(self):
        with self.assertRaises(self.Crash):
            self.run_all(self.REQS, executor=self.crash_on_second([]), pid=4242)
        rec, _ = self.ev.load(TID, self.git.head)
        self.assertEqual((rec["state"], rec["pid"]), ("running", 4242))
        status, detail = self.verdict(self.REQS, alive=lambda p: False)
        self.assertEqual((status, "interrupted" in detail, "--resume" in detail), ("FAIL", True, True))
        status, detail = self.verdict(self.REQS, alive=lambda p: True)
        self.assertEqual((status, "still in progress" in detail), ("FAIL", True))

    def test_a_second_run_is_refused_while_the_first_process_is_alive(self):
        with self.assertRaises(self.Crash):
            self.run_all(self.REQS, executor=self.crash_on_second([]), pid=4242)
        with self.assertRaises(T.TestReqError) as cm:
            self.run_all(self.REQS, pid=7, pid_alive=lambda p: True)
        self.assertIn("in progress", str(cm.exception))
        rec = self.run_all(self.REQS, pid=7, pid_alive=lambda p: False)       # the crashed owner is gone: a new run takes over
        self.assertEqual(rec["verdict"], "passed")

    def test_resume_reuses_what_passed_for_the_same_head_and_runs_the_rest(self):
        with self.assertRaises(self.Crash):
            self.run_all(self.REQS, executor=self.crash_on_second([]))
        ran = []
        rec = self.run_all(self.REQS, executor=ok_exec(ran), resume=True, pid_alive=lambda p: False)
        self.assertEqual(len(ran), 1)
        self.assertTrue(ran[0][-1].endswith("test_two.py"))
        self.assertEqual([x["status"] for x in rec["results"]], ["passed", "passed"])
        self.assertEqual(rec["results"][0].get("reused"), True)
        self.assertEqual((rec["attempt"], rec["verdict"]), (2, "passed"))
        self.assertEqual(self.verdict(self.REQS)[0], "PASS")

    def test_without_resume_every_requirement_runs_again(self):
        self.run_all(self.REQS)
        ran = []
        rec = self.run_all(self.REQS, executor=ok_exec(ran))
        self.assertEqual((len(ran), rec["attempt"], rec["verdict"]), (2, 2, "passed"))
        self.assertNotIn("reused", rec["results"][0])

    def test_resume_never_reuses_results_from_another_head_or_changed_requirements(self):
        self.run_all(self.REQS)
        self.git.commit()
        ran = []
        rec = self.run_all(self.REQS, executor=ok_exec(ran), resume=True)
        self.assertEqual((len(ran), rec["attempt"]), (2, 1))
        ran.clear()
        self.run_all([self.REQS[0], UNIT.format("test_changed.py")], executor=ok_exec(ran), resume=True)
        self.assertEqual(len(ran), 2)

    def test_resume_does_not_reuse_a_failed_result(self):
        self.run_all(self.REQS, executor=ok_exec(code=1, text="FAILED\n"))
        ran = []
        rec = self.run_all(self.REQS, executor=ok_exec(ran), resume=True)
        self.assertEqual((len(ran), rec["verdict"]), (2, "passed"))


class GateVerdict(Base):
    def tc(self, reqs, **kw):
        reg = {"tasks": [{"id": TID, "branch": BRANCH, "status": "ACTIVE", "test_requirements": reqs}]}
        d = {"branch": BRANCH, "wave": False, "reg_only": False, "registry": reg}
        d.update(kw)
        return d

    def verdict_of(self, tc, head="a" * 40, diff="d" * 16):
        return T.gate_verdict(tc, head, diff, self.ev)

    def record(self, reqs, **kw):
        rec = {"schema": 1, "task": TID, "branch": BRANCH, "head": "a" * 40, "diff_sha256": "d" * 16,
               "reqs_sha256": T.reqs_hash(reqs), "state": "complete", "pid": 1, "attempt": 1, "started": "s", "finished": "f",
               "verdict": "passed", "results": [{"index": i, "requirement": t, "kind": T.classify(t).kind,
                                                 "status": "passed" if T.classify(t).kind == "command" else "skipped",
                                                 "finished": "f"} for i, t in enumerate(reqs)]}
        rec.update(kw)
        return rec

    def test_not_applicable_cases_are_explicit_passes(self):
        status, detail = self.verdict_of(self.tc([], wave=True))
        self.assertEqual((status, "wave" in detail), ("PASS", True))
        status, detail = self.verdict_of(self.tc([], reg_only=True))
        self.assertEqual((status, "registry-only" in detail), ("PASS", True))

    def test_unreadable_facts_are_unknown_not_pass(self):
        self.assertEqual(self.verdict_of(None)[0], "UNKNOWN")
        self.assertEqual(self.verdict_of(self.tc(["x"], registry=None))[0], "UNKNOWN")
        self.assertEqual(T.gate_verdict(self.tc(["x"]), "", "d" * 16, self.ev)[0], "UNKNOWN")
        self.assertEqual(T.gate_verdict(self.tc(["x"]), "a" * 40, "", self.ev)[0], "UNKNOWN")

    def test_missing_malformed_and_ambiguous_requirements_fail(self):
        for reqs in (None, [], "python -B -m governance validate", {"a": 1}):
            status, detail = self.verdict_of(self.tc(reqs))
            self.assertEqual(status, "FAIL", reqs)
            self.assertIn("missing or empty", detail)
        status, detail = self.verdict_of(self.tc(["make sure it works"]))
        self.assertEqual(status, "FAIL")
        self.assertIn("not machine-executable", detail)

    def test_no_or_ambiguous_task_for_the_branch_fails(self):
        other = self.tc(["python -B -m governance validate from orchestrator/"], branch="task/other")
        other["branch"] = BRANCH
        self.assertEqual(self.verdict_of(other)[0], "FAIL")
        two = {"tasks": [{"id": "T-A-001", "branch": BRANCH, "status": "ACTIVE", "test_requirements": ["x"]},
                         {"id": "T-B-002", "branch": BRANCH, "status": "ACTIVE", "test_requirements": ["x"]}]}
        self.assertEqual(self.verdict_of(self.tc([], registry=two))[0], "FAIL")
        done = {"tasks": [{"id": "T-A-001", "branch": BRANCH, "status": "COMPLETED", "test_requirements": ["x"]}]}
        self.assertEqual(self.verdict_of(self.tc([], registry=done))[0], "FAIL")

    def test_no_evidence_fails_and_matching_evidence_passes(self):
        reqs = ["python -B -m governance validate from orchestrator/", "independent review of the exact diff"]
        status, detail = self.verdict_of(self.tc(reqs))
        self.assertEqual(status, "FAIL")
        self.assertIn("no test evidence", detail)
        self.ev.save(self.record(reqs))
        status, detail = self.verdict_of(self.tc(reqs))
        self.assertEqual(status, "PASS", detail)
        self.assertEqual(self.verdict_of(self.tc(reqs), head="b" * 40)[0], "FAIL")
        self.assertEqual(self.verdict_of(self.tc(reqs), diff="e" * 16)[0], "FAIL")

    def test_every_status_other_than_passed_fails_and_is_named(self):
        reqs = ["python -B -m governance validate from orchestrator/"]
        for bad in ("failed", "skipped", "blocked", "not_run"):
            rec = self.record(reqs)
            rec["results"][0]["status"] = bad
            self.ev.save(rec)
            status, detail = self.verdict_of(self.tc(reqs))
            self.assertEqual(status, "FAIL", bad)
            self.assertIn(bad, detail)

    def test_a_result_for_other_requirement_text_or_a_missing_result_does_not_count(self):
        reqs = ["python -B -m governance validate from orchestrator/", "python -B -m governance handoff --check from orchestrator/"]
        rec = self.record(reqs)
        rec["results"][0]["requirement"] = "something else"
        self.ev.save(rec)
        self.assertEqual(self.verdict_of(self.tc(reqs))[0], "FAIL")
        rec = self.record(reqs)
        rec["results"].pop()
        self.ev.save(rec)
        status, detail = self.verdict_of(self.tc(reqs))
        self.assertEqual(status, "FAIL")
        self.assertIn("no result", detail)

    def test_an_unfinished_record_fails_whatever_its_results_say(self):
        reqs = ["python -B -m governance validate from orchestrator/"]
        for state in ("running", "interrupted", "weird"):
            self.ev.save(self.record(reqs, state=state))
            self.assertEqual(self.verdict_of(self.tc(reqs))[0], "FAIL", state)

    def test_a_record_for_another_schema_fails(self):
        reqs = ["python -B -m governance validate from orchestrator/"]
        self.ev.save(self.record(reqs, schema=2))
        status, detail = self.verdict_of(self.tc(reqs))
        self.assertEqual(status, "FAIL")
        self.assertIn("different format", detail)


class Cli(Base):
    """The command line over a fake git (the real git version is in RealRepository)."""

    def setUp(self):
        super().setUp()
        reqs = [UNIT.format("test_a.py"), "independent review of the exact diff"]
        self.git = FakeGit(registry={"tasks": [{"id": TID, "branch": BRANCH, "status": "ACTIVE", "test_requirements": reqs}]})

    def go(self, *args, executor=None):
        out = io.StringIO()
        code = T.main([*args, "--repo", str(self.root), "--state-dir", str(self.state)], git=self.git, out=out,
                      **({"executor": executor} if executor else {}))
        return code, out.getvalue()

    def test_plan_run_show_check_and_staleness(self):
        code, text = self.go("plan")
        self.assertEqual(code, 0)
        self.assertIn("COMMAND in orchestrator", text)
        self.assertIn("GATE (review)", text)
        self.assertEqual(self.go("check")[0], 1)                          # nothing run yet
        code, text = self.go("run", executor=ok_exec())
        self.assertEqual(code, 0, text)
        self.assertIn("PASSED", text)
        code, text = self.go("check")
        self.assertEqual((code, "tests check: PASS" in text), (0, True))
        self.assertEqual(self.go("show")[0], 0)
        self.git.commit()
        code, text = self.go("check")
        self.assertEqual((code, "tests check: FAIL" in text), (1, True))   # a new commit invalidates the evidence
        self.assertEqual(self.go("show")[0], 1)

    def test_failed_run_exits_nonzero_and_show_prints_the_output_tail(self):
        code, text = self.go("run", executor=ok_exec(code=1, text="AssertionError: nope\nFAILED\n"))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", text)
        code, text = self.go("show")
        self.assertIn("output tail of requirement 1", text)
        self.assertIn("AssertionError: nope", text)

    def test_manual_requirement_makes_plan_and_run_fail(self):
        self.git.registry["tasks"][0]["test_requirements"] = ["full suite before merge"]
        code, text = self.go("plan")
        self.assertEqual((code, "MANUAL" in text), (1, True))
        self.assertEqual(self.go("run", executor=ok_exec())[0], 1)

    def test_unknown_task_unreadable_registry_and_empty_requirements_are_refused(self):
        code, text = self.go("run", "--task", "T-NOPE-999")
        self.assertEqual((code, "refused" in text), (2, True))
        broken = FakeGit()
        self.assertEqual(T.main(["plan", "--repo", str(self.root)], git=broken, out=io.StringIO()), 2)
        self.git.registry["tasks"][0]["test_requirements"] = []
        self.assertEqual(self.go("plan")[0], 2)


class RealRepository(unittest.TestCase):
    """Real git, real child processes. The repository is built once and copied per test."""

    template = None

    @staticmethod
    def sh(cwd, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false", *args],
                       cwd=cwd, check=True, capture_output=True)

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(cls._tmp.cleanup)
        base = Path(cls._tmp.name)
        cls.template = base / "template"
        origin, work = base / "origin.git", cls.template
        work.mkdir()
        cls.sh(base, "init", "-q", "--bare", str(origin))
        cls.sh(work, "init", "-q", "-b", "main")
        (work / "orchestrator" / "tests").mkdir(parents=True)
        (work / "orchestrator" / "tests" / "test_base.py").write_text(OK_TEST, encoding="utf-8")
        (work / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        cls.sh(work, "add", "-A")
        cls.sh(work, "commit", "-q", "-m", "base")
        cls.sh(work, "remote", "add", "origin", str(origin))
        cls.sh(work, "push", "-q", "-u", "origin", "main")
        cls.sh(work, "checkout", "-q", "-b", BRANCH)
        cls.counter = 0

    def setUp(self):
        self._tmp2 = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp2.cleanup)
        self.root = Path(self._tmp2.name) / "work"
        shutil.copytree(self.template, self.root)
        self.state = Path(self._tmp2.name) / "state"
        self.ev = T.EvidenceStore(self.state)
        self.git = I.real_git_runner(self.root)

    def commit(self, files, message="work"):
        for name, text in files.items():
            (self.root / "orchestrator" / "tests" / name).write_text(text, encoding="utf-8")
        self.sh(self.root, "add", "-A")
        self.sh(self.root, "commit", "-q", "--allow-empty", "-m", message)

    def run_all(self, reqs, **kw):
        return T.run_requirements(self.root, TID, BRANCH, reqs, self.git, self.ev, **kw)

    def test_real_commands_have_distinct_outcomes_and_a_clean_environment(self):
        self.commit({"test_ok.py": OK_TEST, "test_bad.py": FAIL_TEST, "test_skip.py": SKIP_TEST, "test_env.py": ENV_TEST,
                     "test_leak.py": LEAK_TEST})
        reqs = [UNIT.format(n) for n in ("test_ok.py", "test_bad.py", "test_nothing_here.py", "test_skip.py", "test_env.py", "test_leak.py")]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk" + "-" + "x" * 20, "GH_TOKEN": "x",
                                          "CREATORSTUDIO_DEV_SUPERVISOR_DIR": "REAL"}):
            rec = self.run_all(reqs)
        st = {x["index"]: x for x in rec["results"]}
        self.assertEqual([st[i]["status"] for i in range(6)], ["passed", "failed", "failed", "failed", "passed", "passed"],
                         [st[i].get("reason") for i in range(6)])
        self.assertEqual((st[0]["exit_code"], st[0]["tests_run"]), (0, 1))
        self.assertEqual(st[1]["exit_code"], 1)
        self.assertIn("AssertionError", st[1]["output_tail"])
        self.assertIn("FAIL: test_a", st[1]["reason"])
        self.assertIn("ran no tests", st[2]["reason"])         # a pattern that matches nothing never counts as a pass
        self.assertIn("skipped", st[3]["reason"])
        self.assertEqual(rec["verdict"], "failed")
        stored = (self.ev.directory / f"{T.evidence_name(TID, rec['head'])}.json").read_text(encoding="utf-8")
        self.assertNotIn("A" * 24, stored)                     # the printed credential-shaped text is redacted
        self.assertEqual(self.run_all(reqs[:1], resume=False)["verdict"], "passed")
        # the repeated run left the worktree untouched
        out = subprocess.run(["git", "status", "--porcelain"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "")

    def test_timeout_really_kills_the_child(self):
        self.commit({"test_slow.py": SLOW_TEST})
        t0 = time.time()
        x = self.run_all([UNIT.format("test_slow.py")], timeout=6)["results"][0]
        self.assertLess(time.time() - t0, 40)
        self.assertEqual((x["status"], x["exit_code"]), ("failed", None))
        self.assertIn("timed out after 6 s", x["reason"])

    def test_dirty_and_modified_worktrees_are_detected_by_real_git(self):
        self.commit({"test_ok.py": OK_TEST, "test_writes.py": WRITES_TEST})
        (self.root / "scratch.txt").write_text("uncommitted", encoding="utf-8")
        rec = self.run_all([UNIT.format("test_ok.py")], executor=lambda *a: self.fail("must not start"))
        self.assertEqual(rec["results"][0]["status"], "blocked")
        (self.root / "scratch.txt").unlink()
        rec = self.run_all([UNIT.format("test_writes.py"), UNIT.format("test_ok.py")])
        self.assertEqual([x["status"] for x in rec["results"]], ["failed", "not_run"])
        self.assertIn("stray_output.txt", rec["results"][0]["reason"])

    def test_cli_end_to_end_and_a_new_commit_invalidates_the_evidence(self):
        reqs = [UNIT.format("test_ok.py"), "independent review of the exact diff"]
        reg = {"tasks": [{"id": TID, "branch": BRANCH, "status": "ACTIVE", "test_requirements": reqs}]}
        (self.root / "ACTIVE_TASKS.json").write_text(json.dumps(reg), encoding="utf-8")
        self.sh(self.root, "add", "ACTIVE_TASKS.json")
        self.sh(self.root, "commit", "-q", "-m", "registry")
        self.sh(self.root, "push", "-q", "origin", f"{BRANCH}:main")        # the registry on origin/main is the source
        self.commit({"test_ok.py": OK_TEST})

        def go(*a):
            out = io.StringIO()
            return T.main([*a, "--repo", str(self.root), "--state-dir", str(self.state)], out=out), out.getvalue()
        code, text = go("run")
        self.assertEqual(code, 0, text)
        self.assertEqual(go("check")[0], 0)
        self.commit({"test_more.py": OK_TEST}, "another commit")
        code, text = go("check")
        self.assertEqual((code, "tests check: FAIL" in text), (1, True))
        # called from a subdirectory (as Main does from orchestrator/), the root is still the worktree top level
        out = io.StringIO()
        code = T.main(["run", "--repo", str(self.root / "orchestrator"), "--state-dir", str(self.state)], out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(go("check")[0], 0)
        out = io.StringIO()
        self.assertEqual(T.main(["plan", "--repo", str(self.root.parent)], out=out), 2)      # not a worktree
        self.assertIn("not inside a git worktree", out.getvalue())


if __name__ == "__main__":
    unittest.main()
