"""Session policy installer tests (T-DEV-025). Offline; never starts claude. The hook command itself IS executed
(a local python subprocess), which is the point: installation is proven, not assumed.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_session_policy.py
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import session_policy as sp  # noqa: E402

SID = str(uuid.uuid4())
DENY = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}\n'


ALLOW_OUT = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}\n'


def good_run(cmd, **kw):
    """Fast stand-in for a correct hook: deny the force push and garbage; pass git status (silence in advisory mode,
    an explicit allow in authoritative mode, T-DEV-028)."""
    stdin, env = kw.get("input", ""), kw.get("env") or {}
    if "git status" in stdin:
        out = ALLOW_OUT if env.get("CS_AUTHORITY_MODE", "advisory") != "advisory" else ""
    else:
        out = DENY
    return SimpleNamespace(returncode=0, stdout=out)


class Install(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.state = Path(self.td.name) / "state"
        self.repo = str(Path(self.td.name) / "repo")

    def test_real_hook_command_is_verified_and_file_written(self):
        inst = sp.install(self.state, SID, "main", self.repo)
        self.assertTrue(inst.verified)
        p = Path(inst.settings_path)
        self.assertEqual(p, sp.settings_path(self.state, "main", SID))
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["hooks"]["PreToolUse"][0]["hooks"][0]["command"], inst.hook_command)
        self.assertEqual(d["hooks"]["PreToolUse"][0]["matcher"], ".*")
        self.assertEqual(inst.env["CS_AUTHORITY_REPO_ROOT"], self.repo.replace("\\", "/"))

    def test_path_is_deterministic_so_resume_reuses_it(self):
        a = sp.install(self.state, SID, "worker", self.repo, run=good_run)
        b = sp.install(self.state, SID, "worker", self.repo, run=good_run)
        self.assertEqual(a.settings_path, b.settings_path)
        self.assertNotEqual(a.settings_path, sp.install(self.state, str(uuid.uuid4()), "worker", self.repo, run=good_run).settings_path)

    def test_argv_gets_settings_for_fresh_and_resume(self):
        inst = sp.install(self.state, SID, "main", self.repo, run=good_run)
        fresh = sp.argv_with_policy(["claude", "--model", "haiku", "--session-id", SID], inst)
        resume = sp.argv_with_policy(["claude", "--model", "haiku", "--resume", SID], inst)
        for a in (fresh, resume):
            self.assertEqual(a[:3], ["claude", "--settings", inst.settings_path])
            self.assertTrue(sp.check_resume(inst, a))
        self.assertFalse(sp.check_resume(inst, ["claude", "--resume", SID]))

    def test_unverified_install_cannot_be_launched(self):
        inst = sp.install(self.state, SID, "main", self.repo, verify=False)
        with self.assertRaises(sp.PolicyInstallError):
            sp.argv_with_policy(["claude"], inst)

    def test_existing_settings_flag_and_missing_file_refused(self):
        inst = sp.install(self.state, SID, "main", self.repo, run=good_run)
        with self.assertRaises(sp.PolicyInstallError):
            sp.argv_with_policy(["claude", "--settings", "x.json"], inst)
        Path(inst.settings_path).unlink()
        with self.assertRaises(sp.PolicyInstallError):
            sp.argv_with_policy(["claude"], inst)

    def test_bad_role_and_session_id(self):
        with self.assertRaises(sp.PolicyInstallError):
            sp.install(self.state, SID, "admin", self.repo, verify=False)
        with self.assertRaises(sp.PolicyInstallError):
            sp.install(self.state, "not-a-uuid", "main", self.repo, verify=False)

    def test_env_has_no_grants_unless_passed_and_owned_paths_json(self):
        env = sp.install(self.state, SID, "worker", self.repo, owned_paths=["a/", "b.py"], run=good_run).env
        self.assertNotIn("CS_AUTHORITY_GRANTS", env)
        self.assertEqual(json.loads(env["CS_AUTHORITY_OWNED_PATHS"]), ["a/", "b.py"])
        env = sp.install(self.state, SID, "worker", self.repo, grants=["resolve_mutation"], run=good_run).env
        self.assertEqual(env["CS_AUTHORITY_GRANTS"], "resolve_mutation")

    def test_installed_env_makes_the_real_hook_enforce_ownership(self):
        inst = sp.install(self.state, SID, "worker", self.repo, owned_paths=["src/"])
        payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "other/x.py"}, "cwd": self.repo})
        out = sp._run_hook(inst.hook_command, inst.env, payload, subprocess.run)[1]
        self.assertIn("not-owned", out)


class VerificationFailsLoudly(unittest.TestCase):
    @staticmethod
    def fake(outputs):
        it = iter(outputs)

        def run(cmd, **kw):
            rc, out = next(it)
            return SimpleNamespace(returncode=rc, stdout=out)
        return run

    def test_each_failure_mode_raises(self):
        cases = {
            "hook does not deny force push": [(0, ""), (0, ""), (0, DENY)],
            "hook crashes": [(1, ""), (0, ""), (0, DENY)],
            "hook blocks routine": [(0, DENY), (0, DENY), (0, DENY)],
            "hook fails open on garbage": [(0, DENY), (0, ""), (0, "")],
        }
        for name, outs in cases.items():
            with self.subTest(name), self.assertRaises(sp.PolicyInstallError):
                sp.verify_hook("cmd", {}, "r", self.fake(outs))

    def test_all_three_probes_pass(self):
        sp.verify_hook("cmd", {}, "r", self.fake([(0, DENY), (0, ""), (0, DENY)]))

    def test_deny_text_that_is_not_the_json_decision_does_not_pass(self):
        fakes = ['WARN "permissionDecision": "deny"\n', '{"permissionDecision": "deny"}\n',
                 '{"hookSpecificOutput": {"permissionDecision": "deny"}}\n',
                 '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}\n']
        for bad in fakes:
            with self.subTest(bad), self.assertRaises(sp.PolicyInstallError):
                sp.verify_hook("cmd", {}, "r", self.fake([(0, bad), (0, ""), (0, bad)]))

    def test_unsafe_hook_paths_are_refused_and_safe_ones_always_quoted(self):
        for bad in ("C:/a;b/python.exe", "C:/a$b/python.exe", "C:/it's/python.exe", "C:/a&b/p.exe", "C:/a`b/p.exe", "C:/a%b/p.exe"):
            with self.subTest(bad), self.assertRaises(sp.PolicyInstallError):
                sp.hook_command(python=bad)
        cmd = sp.hook_command(python="C:/Program Files (x86)/Python/python.exe", script="C:/x/authority.py")
        self.assertEqual(cmd, '"C:/Program Files (x86)/Python/python.exe" -I -B "C:/x/authority.py" hook')

    def test_forged_verified_flag_and_tampered_settings_are_refused(self):
        import dataclasses
        with tempfile.TemporaryDirectory() as td:
            state, repo = Path(td) / "state", str(Path(td) / "repo")
            inst = sp.install(state, SID, "main", repo, verify=False)
            forged = dataclasses.replace(inst, verified=True)
            with self.assertRaises(sp.PolicyInstallError):
                sp.argv_with_policy(["claude"], forged)
            good = sp.install(state, SID, "main", repo, run=good_run)
            Path(good.settings_path).write_text("{}", encoding="utf-8")
            with self.assertRaises(sp.PolicyInstallError):
                sp.argv_with_policy(["claude"], good)

    def test_unrunnable_command_raises(self):
        def boom(*a, **k):
            raise OSError("no such file")
        with self.assertRaises(sp.PolicyInstallError):
            sp.verify_hook("cmd", {}, "r", boom)

    def test_broken_python_path_fails_install(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaises(sp.PolicyInstallError):
            sp.install(Path(td) / "s", SID, "main", td, python=str(Path(td) / "no-such-python.exe"))


def line(**kw):
    return json.dumps(kw)


def use(i, cmd):
    return line(type="assistant", message={"content": [{"type": "tool_use", "id": i, "input": {"command": cmd}}]})


def res(i, err, text):
    return line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": i, "is_error": err, "content": text}]})


class ProbeTranscript(unittest.TestCase):
    def test_blocked_force_push_and_ok_status(self):
        lines = [use("1", "git status --short"), res("1", False, ""),
                 use("2", "git push --force origin task/x"),
                 res("2", True, "PreToolUse hook error: authority policy [force-push]: x"), "garbage line"]
        r = sp.parse_probe_transcript(lines)
        self.assertEqual((r["status_ran"], r["force_push_blocked"], r["force_push_ran"]), (True, True, False))

    def test_short_force_flag_is_detected(self):
        r = sp.parse_probe_transcript([use("2", "git push -f origin x"), res("2", False, "ok")])
        self.assertTrue(r["force_push_ran"])

    def test_unblocked_force_push_is_detected(self):
        r = sp.parse_probe_transcript([use("2", "git push --force origin x"), res("2", False, "ok")])
        self.assertEqual((r["force_push_blocked"], r["force_push_ran"]), (False, True))

    def test_other_error_is_not_counted_as_policy_block(self):
        r = sp.parse_probe_transcript([use("2", "git push --force o x"), res("2", True, "fatal: no remote")])
        self.assertFalse(r["force_push_blocked"])

    def test_cli_dry_run_starts_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(sp.main(["live-probe"]), 0)
        self.assertIn("dry run", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
