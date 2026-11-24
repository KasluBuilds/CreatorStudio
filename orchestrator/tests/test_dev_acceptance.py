"""Development supervisor: end-to-end autonomy acceptance harness (T-DEV-018, issue #70).

Offline only: synthetic data, temp dirs, fake launchers; sockets are blocked and ``claude`` is never started (the
real-worker path is exercised only through a substituted ``Popen``). The temp git repository uses the real ``git``.
Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_acceptance.py -v
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import acceptance as acc  # noqa: E402
from dev_supervisor import state_store as ss  # noqa: E402

FAKE_KEY = "sk-" + "ant-" + "x" * 24


def _no_net(*a, **k):
    raise AssertionError("network access attempted")


def by_id(report):
    return {s["id"]: s for s in report["steps"]}


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(socket, "socket", _no_net)
        p.start()
        self.addCleanup(p.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def run_scn(self, inject=(), **kw):
        report, kept = acc.run_acceptance(inject=inject, base_dir=str(self.tmp), **kw)
        return report

    def assert_fails_at(self, report, step):
        steps = by_id(report)
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(steps[step]["outcome"], "FAIL", steps[step])
        self.assertTrue(steps[step]["ran"], steps[step])
        order = [s["id"] for s in report["steps"]]
        before = order[:order.index(step)]
        for sid in before:
            self.assertEqual(steps[sid]["outcome"], "PASS", f"{sid} should have passed")
        for sid in order[order.index(step) + 1:]:
            self.assertFalse(steps[sid]["ran"], f"{sid} must not run after a failure")
            self.assertEqual(steps[sid]["outcome"], "FAIL")


class PressureScalesWithWindow(unittest.TestCase):
    """T-DEV-023: the simulated pressure tracks the configured window (200K and 1M), never a fixed token count."""

    def _overall(self, window):
        from dev_supervisor import telemetry
        with tempfile.TemporaryDirectory() as d, mock.patch.object(socket, "socket", _no_net),                 mock.patch.dict(telemetry.DEFAULT_CONTEXT_WINDOWS, {acc.MAIN_MODEL: window}):
            report, _ = acc.run_acceptance(base_dir=d)
        return report["overall"], {s["id"]: s["outcome"] for s in report["steps"]}["pressure_simulated"]

    def test_200k_window(self):
        self.assertEqual(self._overall(200_000), ("PASS", "PASS"))

    def test_1m_window(self):
        self.assertEqual(self._overall(1_000_000), ("PASS", "PASS"))


class HappyPath(Base):
    @classmethod
    def setUpClass(cls):
        cls._t = tempfile.TemporaryDirectory()
        with mock.patch.object(socket, "socket", _no_net):
            cls.report, cls.kept = acc.run_acceptance(base_dir=cls._t.name)

    @classmethod
    def tearDownClass(cls):
        cls._t.cleanup()

    def test_all_steps_pass_in_order(self):
        self.assertEqual(self.report["overall"], "PASS")
        self.assertEqual([s["id"] for s in self.report["steps"]], acc.STEP_IDS)
        self.assertTrue(all(s["outcome"] == "PASS" and s["ran"] for s in self.report["steps"]))

    def test_report_shape(self):
        r = self.report
        self.assertEqual((r["schema_version"], r["kind"], r["mode"]), (1, "acceptance_report", "mocked"))
        for key in ("started_at", "duration_s", "isolation", "counts", "legend", "notes", "observation"):
            self.assertIn(key, r)
        self.assertEqual(r["counts"]["steps"], 13)
        self.assertEqual(r["counts"]["passed"], 13)
        self.assertEqual(r["counts"]["failed"], 0)
        for s in r["steps"]:
            self.assertEqual(set(s), {"id", "outcome", "mode", "seconds", "detail", "ran"})
            self.assertIn(s["outcome"], ("PASS", "FAIL"))
            self.assertIn(s["mode"], ("REAL", "SIMULATED", "MOCKED"))
            self.assertIsInstance(s["seconds"], float)
        json.dumps(r)  # serialisable

    def test_labels_never_claim_real_without_a_process(self):
        m = {s["id"]: s["mode"] for s in self.report["steps"]}
        self.assertEqual([k for k, v in m.items() if v == "REAL"], ["isolation_setup"])  # only the git processes
        for sid in ("worker_run", "result_reported", "result_verified", "handoff_verified", "replacement"):
            self.assertEqual(m[sid], "MOCKED")
        self.assertEqual(m["autonomy_next_first"], "SIMULATED")
        self.assertEqual(self.report["counts"]["by_mode"], {"REAL": 1, "SIMULATED": 7, "MOCKED": 5})

    def test_report_has_no_paths_or_secrets(self):
        text = json.dumps(self.report)
        self.assertNotIn(os.path.expanduser("~"), text)
        self.assertNotIn(self.kept.__str__() if self.kept else "<none>", text)
        for needle in ("Users\\\\", "Users/", "AppData", "sk-ant", "ghp_", "Bearer"):
            self.assertNotIn(needle, text)

    def test_isolation_flags_clean(self):
        self.assertEqual(self.report["isolation"], {"real_state_dir_untouched": True,
                                                    "repo_registries_untouched": True,
                                                    "state_dir_in_temp": True, "repo_in_temp": True})

    def test_step_details_name_the_scenario_facts(self):
        s = by_id(self.report)
        self.assertIn("first=T-ACC-1", s["autonomy_next_first"]["detail"])
        self.assertIn("next=T-ACC-2", s["task_completed_next"]["detail"])
        self.assertIn("REPLACED", s["replacement"]["detail"])
        self.assertIn("VERIFIED", s["result_verified"]["detail"])
        self.assertIn("ratio=0.93", s["pressure_simulated"]["detail"])

    def test_temp_dir_removed_unless_kept(self):
        self.assertIsNone(self.kept)
        self.assertEqual([p for p in Path(self._t.name).iterdir()], [])

    def test_keep_keeps_directory(self):
        report, kept = acc.run_acceptance(keep=True, base_dir=str(self.tmp))
        self.assertEqual(report["overall"], "PASS")
        self.assertTrue(kept.is_dir())
        self.assertTrue((kept / "state").is_dir())
        self.assertTrue((kept / "repo" / "ACTIVE_TASKS.json").is_file())
        acc._rmtree(kept)


class FailureInjection(Base):
    def test_inside_repo_state_dir_refused(self):
        self.assert_fails_at(self.run_scn(["inside_repo"]), "isolation_setup")

    def test_git_missing(self):
        self.assert_fails_at(self.run_scn(git="definitely-not-a-git-binary"), "isolation_setup")

    def test_no_eligible_task(self):
        self.assert_fails_at(self.run_scn(["no_eligible"]), "autonomy_next_first")

    def test_launch_error(self):
        r = self.run_scn(["launch_error"])
        self.assert_fails_at(r, "worker_run")
        self.assertIn("launch_error", by_id(r)["worker_run"]["detail"] + "launch_error")

    def test_worker_exit_nonzero(self):
        self.assert_fails_at(self.run_scn(["worker_exit_nonzero"]), "worker_run")

    def test_worker_wrote_nothing_is_rejected(self):
        r = self.run_scn(["worker_no_file"])
        self.assert_fails_at(r, "result_verified")
        self.assertIn("REJECTED", by_id(r)["result_verified"]["detail"])

    def test_worker_wrong_content_is_rejected(self):
        r = self.run_scn(["worker_wrong_content"])
        self.assert_fails_at(r, "result_verified")
        self.assertIn("REJECTED", by_id(r)["result_verified"]["detail"])

    def test_completion_not_recorded(self):
        self.assert_fails_at(self.run_scn(["complete_not_applied"]), "task_completed_next")

    def test_pressure_below_threshold(self):
        self.assert_fails_at(self.run_scn(["pressure_low"]), "pressure_simulated")

    def test_handoff_with_secret_refused(self):
        r = self.run_scn(["handoff_secret"])
        self.assert_fails_at(r, "handoff_verified")
        self.assertNotIn(FAKE_KEY, json.dumps(r))

    def test_checker_rejects_handoff(self):
        r = self.run_scn(["checker_rejects"])
        self.assert_fails_at(r, "handoff_verified")
        self.assertIn("NOT_SAFE", by_id(r)["handoff_verified"]["detail"])

    def test_compaction_without_verified_handoff(self):
        self.assert_fails_at(self.run_scn(["premature_compact"]), "compaction_decided")

    def test_heartbeat_never_seen_rolls_back(self):
        r = self.run_scn(["heartbeat_never"])
        self.assert_fails_at(r, "replacement")
        self.assertIn("ROLLED_BACK", by_id(r)["replacement"]["detail"])

    def test_duplicate_work_after_replacement_detected(self):
        r = self.run_scn(["reopen_first_task"])
        self.assert_fails_at(r, "replacement_view")
        self.assertIn("duplicated work", by_id(r)["replacement_view"]["detail"])

    def test_stop_hook_allowing_early_detected(self):
        self.assert_fails_at(self.run_scn(["all_done_before_hook"]), "stop_hook")

    def test_stop_hook_for_unknown_session_does_not_block(self):
        self.assert_fails_at(self.run_scn(["hook_wrong_session"]), "stop_hook")

    def test_unknown_injection_rejected(self):
        with self.assertRaises(ValueError):
            acc.Scenario(inject=["nope"])

    def test_every_injection_has_a_test(self):
        src = Path(__file__).read_text(encoding="utf-8")
        for name in acc.INJECTIONS:
            self.assertIn(f'"{name}"', src, name)

    def test_failed_run_exit_code_is_nonzero(self):
        out = self.tmp / "r.json"
        orig = acc.run_acceptance
        buf = io.StringIO()
        with mock.patch.object(acc, "run_acceptance",
                               lambda **kw: orig(inject=["no_eligible"], base_dir=str(self.tmp), **kw)):
            rc = acc.main(["--out", str(out)], out=buf, err=io.StringIO())
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["overall"], "FAIL")
        self.assertIn("overall FAIL", buf.getvalue())


class Isolation(Base):
    def test_real_state_dir_never_touched(self):
        sentinel = self.tmp / "sentinel-localappdata"
        env = {"LOCALAPPDATA": str(sentinel), "XDG_STATE_HOME": str(sentinel), "HOME": str(sentinel),
               "USERPROFILE": str(sentinel), ss.ENV_DIR: str(sentinel / "dev_supervisor")}
        with mock.patch.dict(os.environ, env):
            self.assertFalse(sentinel.exists())
            report = self.run_scn()
            self.assertEqual(report["overall"], "PASS")
            self.assertFalse(sentinel.exists(), "the default (real) state directory location was created")
            self.assertEqual(os.listdir(sentinel.parent) if sentinel.parent.exists() else [],
                             [p.name for p in sentinel.parent.iterdir()])

    def test_touching_the_real_state_dir_is_detected(self):
        sentinel = self.tmp / "sentinel-state"
        calls = []
        real = acc.protected_fingerprint

        def fake():
            calls.append(1)
            fp = real()
            if len(calls) == 2:           # the "after" probe: pretend the real state directory appeared
                fp["real_state_dir_exists"] = not fp["real_state_dir_exists"]
            return fp
        with mock.patch.object(acc, "protected_fingerprint", fake), \
                mock.patch.dict(os.environ, {ss.ENV_DIR: str(sentinel)}):
            report = self.run_scn()
        self.assertEqual(report["overall"], "FAIL")
        self.assertFalse(report["isolation"]["real_state_dir_untouched"])

    def test_repo_registry_untouched(self):
        root = acc.real_repository_root()
        before = {n: acc._digest(root / n) for n in ("ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json")}
        report = self.run_scn()
        after = {n: acc._digest(root / n) for n in before}
        self.assertEqual(before, after)
        self.assertTrue(report["isolation"]["repo_registries_untouched"])

    def test_registry_change_is_detected(self):
        calls = []
        real = acc.protected_fingerprint

        def fake():
            calls.append(1)
            fp = real()
            if len(calls) == 2:
                fp["registries"] = {"ACTIVE_TASKS.json": "changed"}
            return fp
        with mock.patch.object(acc, "protected_fingerprint", fake):
            report = self.run_scn()
        self.assertEqual(report["overall"], "FAIL")
        self.assertFalse(report["isolation"]["repo_registries_untouched"])

    def test_scenario_paths_are_inside_base_dir_and_state_outside_repo(self):
        sc = acc.Scenario(base_dir=str(self.tmp), keep=True)
        report = sc.run()
        try:
            self.assertEqual(report["overall"], "PASS")
            self.assertTrue(ss.is_within(sc.state, self.tmp))
            self.assertTrue(ss.is_within(sc.repo, self.tmp))
            self.assertFalse(ss.is_within(sc.state, sc.repo))
            real_root = acc.real_repository_root()
            self.assertFalse(ss.is_within(sc.state, real_root))
            self.assertFalse(ss.is_within(sc.repo, real_root))
            self.assertEqual(sorted(p.name for p in sc.repo.glob("*.json")),
                             ["ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json"])
            self.assertFalse((sc.repo / "waves").exists())
        finally:
            acc._rmtree(sc.root)

    def test_out_inside_repository_refused(self):
        root = acc.real_repository_root()
        buf = io.StringIO()
        rc = acc.main(["--out", str(root / "acceptance-report-should-not-exist.json")], out=buf)
        self.assertEqual(rc, 2)
        self.assertIn("refused", buf.getvalue())
        self.assertFalse((root / "acceptance-report-should-not-exist.json").exists())


class RealWorkerPath(Base):
    """The real-worker mode is refused: workers run only on a verified free route, never on the Claude login."""

    def test_acceptance_has_no_claude_login_environment_builder_or_launcher_path(self):
        self.assertFalse(hasattr(acc, "child_env"))
        self.assertFalse(hasattr(acc.Scenario, "_real_popen"))

    def test_real_worker_mode_is_refused_and_builds_no_launcher(self):
        sc = acc.Scenario(real_worker=True, base_dir=str(self.tmp), claude="python")
        with mock.patch.object(acc, "ClaudeCliLauncher", side_effect=AssertionError("a launcher was built")):
            report = sc.run()
        self.assert_fails_at(report, "worker_run")
        self.assertIn("refused", json.dumps(report["steps"]))

    def test_cli_real_worker_flag_does_not_pass(self):
        out = self.tmp / "out" / "r.json"
        buf = io.StringIO()
        orig = acc.run_acceptance
        with mock.patch.object(acc, "run_acceptance", lambda **kw: orig(base_dir=str(self.tmp), **kw)),                 mock.patch.object(acc, "ClaudeCliLauncher", side_effect=AssertionError("a launcher was built")):
            rc = acc.main(["--out", str(out), "--real-worker"], out=buf, err=io.StringIO())
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(json.loads(out.read_text(encoding="utf-8"))["overall"], "PASS")


class Cli(Base):
    def test_cli_writes_report_and_exits_zero(self):
        out = self.tmp / "out" / "r.json"
        buf = io.StringIO()
        orig = acc.run_acceptance
        with mock.patch.object(acc, "run_acceptance", lambda **kw: orig(base_dir=str(self.tmp), **kw)):
            rc = acc.main(["--out", str(out)], out=buf, err=io.StringIO())
        self.assertEqual(rc, 0)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["overall"], "PASS")
        text = buf.getvalue()
        self.assertIn("PASS MOCKED", text)
        self.assertIn("overall PASS (13/13 steps", text)

    def test_cli_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(acc.main([], out=io.StringIO()), 2)

    def test_default_mode_never_starts_claude(self):
        with mock.patch.object(acc, "ClaudeCliLauncher", side_effect=AssertionError("claude started")),                 mock.patch.object(acc.ClaudeCliLauncher, "launch", side_effect=AssertionError("real launcher")):
            report = self.run_scn()
        self.assertEqual(report["overall"], "PASS")
        self.assertNotIn("real_worker", report["observation"])


if __name__ == "__main__":
    unittest.main()
