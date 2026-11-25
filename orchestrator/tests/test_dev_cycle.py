import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import cycle  # noqa: E402
from dev_supervisor import integrate as I  # noqa: E402

BRANCH = "task/t-x-001-thing"


def registry(status="ACTIVE", branch=BRANCH, tid="T-X-001"):
    return json.dumps({"tasks": [{"id": tid, "branch": branch, "status": status}]}).encode()


class Env:
    """Fake git and gh: the registry text, PR list, remote-branch presence and gate results are all injected."""

    def __init__(self, reg=None, prs=None, remote=False, gates=None, pr_list_rc=0, reg_rc=0, diff="abcd1234abcd1234",
                 info_extra=None):
        self.reg, self.prs, self.remote, self.gates = reg, prs, remote, gates or []
        self.pr_list_rc, self.reg_rc, self.diff = pr_list_rc, reg_rc, diff
        self.info_extra = info_extra or {}
        self.calls = []

    def git(self, args):
        self.calls.append(("git", list(args)))
        if args[0] == "show":
            return self.reg_rc, (self.reg if self.reg is not None else b"")
        if args[0] == "rev-parse":
            return (0 if self.remote else 1), b""
        return 1, b""

    def gh(self, args, input=None):
        self.calls.append(("gh", list(args)))
        if args[:2] == ["pr", "list"]:
            return self.pr_list_rc, json.dumps(self.prs if self.prs is not None else [])
        return 1, ""

    def evaluate(self, n, gh, git, base="main"):
        return self.gates, {"diff_sha": self.diff, **self.info_extra}

    def track(self, task="T-X-001", free=4096, **kw):
        return cycle.track(task, self.gh, self.git, evaluate=self.evaluate, free_memory=lambda: free, **kw)


def g(num, name, status="PASS", detail=""):
    return I.Gate(num, name, status, detail)


ALL_PASS = [g(1, "pr_state"), g(2, "checks"), g(3, "merge_state"), g(4, "scope"), g(5, "review"), g(6, "secrets"),
            g(7, "forbidden_files"), g(8, "reserved")]


def with_gates(**overrides):
    out = []
    for gate in ALL_PASS:
        if gate.name in overrides:
            status, detail = overrides[gate.name]
            out.append(g(gate.num, gate.name, status, detail))
        else:
            out.append(gate)
    return out


OPEN7 = [{"number": 7, "state": "OPEN"}]


class StageTests(unittest.TestCase):
    def test_unregistered(self):
        r = Env(reg=registry()).track("T-X-999")
        self.assertEqual(r["stage"], "unregistered")
        self.assertIn("claim", r["next_action"])

    def test_done(self):
        self.assertEqual(Env(reg=registry("COMPLETED")).track()["stage"], "done")
        self.assertEqual(Env(reg=registry("RELEASED")).track()["stage"], "done")

    def test_awaiting_work_reports_free_memory_and_the_floor(self):
        r = Env(reg=registry(), prs=[]).track(free=512)
        self.assertEqual(r["stage"], "awaiting_work")
        self.assertIn("free memory 512 MB", " ".join(e["text"] for e in r["evidence"]))
        self.assertIn("memory guard", r["next_action"])

    def test_branch_pushed(self):
        r = Env(reg=registry(), prs=[], remote=True).track()
        self.assertEqual(r["stage"], "branch_pushed")
        self.assertIn("intake", r["next_action"])

    def test_ready_to_merge(self):
        r = Env(reg=registry(), prs=OPEN7, gates=ALL_PASS).track()
        self.assertEqual((r["stage"], r["pr"]), ("ready_to_merge", 7))
        self.assertEqual(r["next_action"], "python -B -m dev_supervisor.integrate 7 --yes")

    def test_the_tests_check_is_consulted_only_when_the_eight_gates_pass_and_facts_are_given(self):
        calls = []

        def check(info):
            calls.append(info)
            return g(9, "tests", "PASS", "4 command requirement(s) passed")
        facts = {"tests": {"branch": BRANCH}}
        r = Env(reg=registry(), prs=OPEN7, gates=ALL_PASS, info_extra=facts).track(tests_check=check)
        self.assertEqual((r["stage"], len(calls)), ("ready_to_merge", 1))
        self.assertTrue(any("tests check: PASS" in e["text"] for e in r["evidence"]))
        calls.clear()
        Env(reg=registry(), prs=OPEN7, gates=with_gates(scope=("FAIL", "x")), info_extra=facts).track(tests_check=check)
        Env(reg=registry(), prs=OPEN7, gates=ALL_PASS).track(tests_check=check)           # no facts from evaluate
        self.assertEqual(calls, [])

    def test_a_failing_tests_check_is_needs_tests_with_the_command_to_run(self):
        detail = "no test evidence for head 0209709abcde (missing); run: python -B -m dev_supervisor.testreq run --task T-X-001"
        r = Env(reg=registry(), prs=OPEN7, gates=ALL_PASS, info_extra={"tests": {}}).track(
            tests_check=lambda info: g(9, "tests", "FAIL", detail))
        self.assertEqual((r["stage"], r["pr"]), ("needs_tests", 7))
        self.assertIn("testreq run --task T-X-001", r["next_action"])
        self.assertTrue(any(detail in e["text"] for e in r["evidence"]))
        self.assertIn("needs_tests", cycle.STAGES)

    def test_an_unreadable_tests_check_is_unknown_never_ready(self):
        r = Env(reg=registry(), prs=OPEN7, gates=ALL_PASS, info_extra={"tests": {}}).track(
            tests_check=lambda info: g(9, "tests", "UNKNOWN", "cannot read"))
        self.assertEqual(r["stage"], "unknown")

    def test_a_crashing_tests_check_does_not_crash_the_tracker(self):
        def boom(info):
            raise RuntimeError("x")
        r = Env(reg=registry(), prs=OPEN7, gates=ALL_PASS, info_extra={"tests": {}}).track(tests_check=boom)
        self.assertEqual(r["stage"], "unknown")

    def test_in_review_names_the_diff_hash(self):
        gates = with_gates(review=("FAIL", "no dev-review APPROVE for diff abcd1234abcd1234"))
        r = Env(reg=registry(), prs=OPEN7, gates=gates).track()
        self.assertEqual(r["stage"], "in_review")
        self.assertIn("abcd1234abcd1234", r["next_action"])

    def test_review_with_ci_pending_is_still_in_review(self):
        gates = with_gates(review=("FAIL", "no approve"), checks=("FAIL", "validate is pending"),
                           merge_state=("FAIL", "BLOCKED"))
        self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "in_review")

    def test_waiting_ci(self):
        gates = with_gates(checks=("FAIL", "validate is pending"), merge_state=("FAIL", "BLOCKED"))
        self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "waiting_ci")

    def test_failing_ci_or_a_behind_branch_needs_a_fix(self):
        gates = with_gates(checks=("FAIL", "validate FAILURE on head abc"))
        self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "needs_fix")
        gates = with_gates(merge_state=("FAIL", "BEHIND"))
        self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "needs_fix")

    def test_a_failing_non_review_gate_needs_a_fix_even_when_review_is_missing(self):
        gates = with_gates(scope=("FAIL", "outside owned paths"), review=("FAIL", "no approve"))
        r = Env(reg=registry(), prs=OPEN7, gates=gates).track()
        self.assertEqual(r["stage"], "needs_fix")
        self.assertIn("scope", " ".join(e["text"] for e in r["evidence"]))

    def test_an_unreadable_gate_is_unknown_never_a_fix_or_a_merge(self):
        for name in ("secrets", "scope", "pr_state", "reserved", "forbidden_files", "review"):
            with self.subTest(gate=name):
                gates = with_gates(**{name: ("UNKNOWN", "cannot read")})
                self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "unknown")

    def test_every_gate_unreadable_is_unknown(self):
        gates = [g(i, n, "UNKNOWN", "cannot read PR") for i, n in enumerate(cycle.EXPECTED_GATES, 1)]
        self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "unknown")

    def test_ci_side_unknowns_and_uppercase_pending_mean_not_yet(self):
        for overrides in ({"merge_state": ("UNKNOWN", "mergeStateStatus not computed yet")},
                          {"checks": ("UNKNOWN", "no statusCheckRollup")},
                          {"checks": ("FAIL", "validate is PENDING"), "merge_state": ("FAIL", "BLOCKED")},
                          {"checks": ("FAIL", "required check 'validate' not reported on head abc"),
                           "merge_state": ("UNKNOWN", "x")}):
            with self.subTest(overrides=overrides):
                gates = with_gates(**overrides)
                self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "waiting_ci")

    def test_blocked_with_green_ci_or_red_ci_with_blocked_needs_a_fix(self):
        for overrides in ({"merge_state": ("FAIL", "BLOCKED")},
                          {"checks": ("FAIL", "validate FAILURE on head abc"), "merge_state": ("FAIL", "BLOCKED")}):
            with self.subTest(overrides=overrides):
                gates = with_gates(**overrides)
                self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "needs_fix")

    def test_a_short_or_odd_gate_list_is_never_ready(self):
        for gates in ([g(1, "pr_state")], ALL_PASS[:7], ALL_PASS + [g(9, "extra")],
                      [g(i, "pr_state") for i in range(1, 9)], []):
            with self.subTest(n=len(gates)):
                self.assertEqual(Env(reg=registry(), prs=OPEN7, gates=gates).track()["stage"], "unknown")

    def test_closed_prs_and_a_cut_list_are_noted(self):
        r = Env(reg=registry(), prs=[{"number": 3, "state": "CLOSED"}]).track()
        self.assertEqual(r["stage"], "awaiting_work")
        self.assertIn("closed without merge: #3", " ".join(e["text"] for e in r["evidence"]))
        many = [{"number": i, "state": "CLOSED"} for i in range(1, 21)]
        self.assertIn("cut at 20", " ".join(e["text"] for e in Env(reg=registry(), prs=many).track()["evidence"]))

    def test_a_base_ref_that_looks_like_an_option_is_refused(self):
        env = Env(reg=registry())
        r = cycle.track("T-X-001", env.gh, env.git, base="--output=x", evaluate=env.evaluate)
        self.assertEqual(r["stage"], "unknown")
        self.assertEqual(env.calls, [])

    def test_merged_pr_on_an_active_task_awaits_closeout(self):
        r = Env(reg=registry(), prs=[{"number": 5, "state": "MERGED"}]).track()
        self.assertEqual((r["stage"], r["pr"]), ("awaiting_closeout", 5))

    def test_an_open_closeout_pr_is_reported_as_the_open_pull_request(self):
        prs = [{"number": 5, "state": "MERGED"}, {"number": 6, "state": "OPEN"}]
        r = Env(reg=registry(), prs=prs, gates=ALL_PASS).track()
        self.assertEqual((r["stage"], r["pr"]), ("ready_to_merge", 6))


class WorkerTestStageTests(unittest.TestCase):
    """T-DEV-074: the Supervisor's test state, before a pull request exists."""
    HEAD = "d" * 40

    def entry(self, state, detail="d", **kw):
        return {"task_id": "T-X-001", "state": state, "head": self.HEAD, "detail": detail, "updated": "2026-11-25T10:00:00+00:00", **kw}

    def track(self, entry, remote=False, prs=None, reg=None):
        return Env(reg=reg or registry(), prs=[] if prs is None else prs, remote=remote).track(worker_tests=lambda t: entry)

    def test_running_and_pending_are_worker_testing(self):
        for st in ("RUNNING", "PENDING"):
            r = self.track(self.entry(st, "runner pid 5 started"))
            self.assertEqual(r["stage"], "worker_testing", st)
            self.assertIn("Supervisor", r["next_action"])
            self.assertIn(f"{st} for head {self.HEAD[:12]}: runner pid 5 started", " ".join(e["text"] for e in r["evidence"]))

    def test_failed_and_blocked_are_worker_tests_failed_with_the_cause(self):
        for st in ("FAILED", "BLOCKED"):
            r = self.track(self.entry(st, "requirement 2 failed: exit code 1"), remote=True)
            self.assertEqual(r["stage"], "worker_tests_failed", st)
            self.assertIn("requirement 2 failed", " ".join(e["text"] for e in r["evidence"]))
            self.assertIn("testreq show --task T-X-001", r["next_action"])

    def test_passed_keeps_the_stage_and_names_intake(self):
        r = self.track(self.entry("PASSED"))
        self.assertEqual(r["stage"], "awaiting_work")
        self.assertIn("task tests passed", r["next_action"])
        self.assertIn("intake <worker-id>", r["next_action"])
        r = self.track(self.entry("PASSED"), remote=True)
        self.assertEqual(r["stage"], "branch_pushed")
        self.assertIn("the task tests passed for head " + self.HEAD[:12], r["next_action"])

    def test_nothing_changes_without_an_entry_or_with_a_pull_request_or_a_junk_entry(self):
        self.assertEqual(self.track(None)["stage"], "awaiting_work")
        self.assertNotIn("intake <worker-id>", self.track(None)["next_action"])
        self.assertEqual(self.track({"state": "ODD"})["stage"], "awaiting_work")
        self.assertEqual(self.track("junk")["stage"], "awaiting_work")
        r = Env(reg=registry(), prs=OPEN7, gates=ALL_PASS).track(worker_tests=lambda t: self.entry("FAILED"))
        self.assertEqual(r["stage"], "ready_to_merge")
        r = Env(reg=registry(), prs=[{"number": 7, "state": "MERGED"}]).track(worker_tests=lambda t: self.entry("FAILED"))
        self.assertEqual(r["stage"], "awaiting_closeout")
        self.assertEqual(Env(reg=registry("COMPLETED")).track(worker_tests=lambda t: self.entry("FAILED"))["stage"], "done")

    def test_a_reader_that_raises_changes_nothing(self):
        def boom(t):
            raise RuntimeError("x")
        self.assertEqual(Env(reg=registry(), prs=[]).track(worker_tests=boom)["stage"], "awaiting_work")

    def test_reader_picks_the_newest_entry_for_the_task_and_survives_a_missing_directory(self):
        import tempfile
        from dev_supervisor.state_store import StateStore
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cycle.worker_tests_reader(str(Path(d) / "none"))("T-X-001"))
            store = StateStore(Path(d))
            self.assertIsNone(cycle.worker_tests_reader(d)("T-X-001"))
            store.write("worker_tests", {"entries": {"w1": self.entry("FAILED", updated="2026-11-25T09:00:00+00:00"),
                                                      "w2": self.entry("PASSED", updated="2026-11-25T10:00:00+00:00"),
                                                      "w3": {**self.entry("RUNNING"), "task_id": "T-OTHER-001"}}})
            self.assertEqual(cycle.worker_tests_reader(d)("T-X-001")["state"], "PASSED")
            self.assertIsNone(cycle.worker_tests_reader(d)("T-NONE-001"))

    def test_cli_uses_an_injected_reader_and_the_json_output_has_the_new_stage(self):
        env = Env(reg=registry(), prs=[])
        out = io.StringIO()
        cycle.main(["T-X-001", "--json"], out=out, gh=env.gh, git=env.git, worker_tests=lambda t: self.entry("RUNNING"))
        self.assertEqual(json.loads(out.getvalue())["stage"], "worker_testing")
        self.assertIn("worker_testing", cycle.STAGES)
        self.assertIn("worker_tests_failed", cycle.STAGES)


class FailureTests(unittest.TestCase):
    def test_nothing_is_guessed_when_state_cannot_be_read(self):
        cases = [Env(reg=None, reg_rc=1), Env(reg=b"{not json"), Env(reg=b"{}"), Env(reg=registry(), pr_list_rc=1),
                 Env(reg=registry(status="BLOCKED")), Env(reg=registry(branch=""))]
        for env in cases:
            with self.subTest(env=env.reg):
                r = env.track()
                self.assertEqual(r["stage"], "unknown", r)
                self.assertTrue(r["evidence"])

    def test_garbage_pr_list_is_unknown(self):
        env = Env(reg=registry())
        env.gh = lambda args, input=None: (0, "not json")
        self.assertEqual(env.track()["stage"], "unknown")

    def test_an_evaluator_that_raises_does_not_crash_the_tracker(self):
        env = Env(reg=registry(), prs=OPEN7)

        def boom(*a, **k):
            raise RuntimeError("x")
        r = cycle.track("T-X-001", env.gh, env.git, evaluate=boom, free_memory=lambda: 1)
        self.assertEqual(r["stage"], "unknown")

    def test_the_tracker_only_reads(self):
        env = Env(reg=registry(), prs=OPEN7, gates=ALL_PASS)
        env.track()
        for kind, args in env.calls:
            if kind == "gh":
                self.assertEqual(args[:2], ["pr", "list"])
            else:
                self.assertIn(args[0], ("show", "rev-parse"))


class CliTests(unittest.TestCase):
    def test_text_and_json_output_and_exit_codes(self):
        env = Env(reg=registry(), prs=[], remote=True)
        out = io.StringIO()
        self.assertEqual(cycle.main(["T-X-001"], out=out, gh=env.gh, git=env.git), 0)
        self.assertIn("T-X-001: branch_pushed", out.getvalue())
        out = io.StringIO()
        cycle.main(["T-X-001", "--json"], out=out, gh=env.gh, git=env.git)
        self.assertEqual(json.loads(out.getvalue())["stage"], "branch_pushed")
        bad = Env(reg=None, reg_rc=1)
        self.assertEqual(cycle.main(["T-X-001"], out=io.StringIO(), gh=bad.gh, git=bad.git), 3)
        self.assertEqual(cycle.main([], out=io.StringIO()), 2)


if __name__ == "__main__":
    unittest.main()
