"""Authority policy tests (T-DEV-024). Synthetic only; nothing is executed, no network, no process is touched.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_authority.py
"""
import io
import json
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import authority as au  # noqa: E402
from dev_supervisor.authority import (ALLOW, ASK, DENY, Action, AuthorityDenied, PolicyContext, decide,  # noqa: E402
                                      enforce)

ROOT = "C:/proj/CreatorStudio"
CTX = PolicyContext(roots=(ROOT, "C:/proj/CreatorStudio-worktrees"), repo_root=ROOT,
                    owned_paths=("orchestrator/dev_supervisor/", "orchestrator/tests/test_dev_authority.py"))


def ident(**kw):
    base = dict(pid=111, exe="claude.exe", cmdline="claude --resume X C:/smoke/state", parent_pid=50, session_id="s-smoke",
                owned_marker="C:/smoke/state", owned_parent_pids=(50,), protected_pids=(900,), protected_sessions=("s-real",))
    base.update(kw)
    return base


class AutonomousActions(unittest.TestCase):
    def test_routine_kinds_allowed(self):
        for k in (au.READ, au.RUN_TESTS, au.CREATE_BRANCH, au.CREATE_WORKTREE, au.DISPATCH_WORKER, au.RECOVER_WORKER,
                  au.CREATE_PR, au.UPDATE_RECORD, au.GIT_COMMIT):
            with self.subTest(k):
                self.assertEqual(decide(Action(k, "orchestrator/x.py"), CTX).verdict, ALLOW)

    def test_edit_owned_file_allowed(self):
        self.assertEqual(decide(Action(au.EDIT_FILE, "orchestrator/dev_supervisor/authority.py"), CTX).verdict, ALLOW)
        self.assertEqual(decide(Action(au.EDIT_FILE, ROOT + "/orchestrator/dev_supervisor/x.py"), CTX).verdict, ALLOW)

    def test_edit_unowned_file_denied(self):
        d = decide(Action(au.EDIT_FILE, "orchestrator/creator_state/state.py"), CTX)
        self.assertEqual((d.verdict, d.rule), (DENY, "not-owned"))

    def test_edit_without_ownership_list_is_not_ownership_checked(self):
        ctx = PolicyContext(roots=(ROOT,), repo_root=ROOT)
        self.assertEqual(decide(Action(au.EDIT_FILE, "anything/ok.py"), ctx).verdict, ALLOW)

    def test_push_task_branch_allowed(self):
        self.assertEqual(decide(Action(au.GIT_PUSH, attrs={"branch": "task/t-dev-024"}), CTX).verdict, ALLOW)

    def test_resume_owned_main_allowed_but_not_unowned(self):
        self.assertEqual(decide(Action(au.RESUME_MAIN, attrs={"owned": True}), CTX).verdict, ALLOW)
        self.assertEqual(decide(Action(au.RESUME_MAIN), CTX).verdict, DENY)

    def test_integrate_requires_all_gates_and_review(self):
        ok = {"gates_passed": True, "independent_review": True}
        self.assertEqual(decide(Action(au.INTEGRATE_PR, attrs=ok), CTX).verdict, ALLOW)
        for bad in ({}, {"gates_passed": True}, {"independent_review": True}, dict(ok, admin_bypass=True),
                    {"gates_passed": False, "independent_review": True}):
            with self.subTest(bad):
                self.assertEqual(decide(Action(au.INTEGRATE_PR, attrs=bad), CTX).verdict, DENY)


class ProcessIdentity(unittest.TestCase):
    def test_full_identity_allows_restart_and_terminate(self):
        for k in (au.RESTART_PROCESS, au.TERMINATE_PROCESS):
            self.assertEqual(decide(Action(k, attrs=ident()), CTX).verdict, ALLOW)

    def test_pid_alone_rejected(self):
        d = decide(Action(au.TERMINATE_PROCESS, attrs={"pid": 111}), CTX)
        self.assertEqual((d.verdict, d.rule), (DENY, "process-identity"))

    def test_each_missing_field_rejected(self):
        for f in au.IDENTITY_FIELDS:
            with self.subTest(f):
                a = ident()
                a.pop(f)
                self.assertEqual(decide(Action(au.TERMINATE_PROCESS, attrs=a), CTX).rule, "process-identity")

    def test_wrong_marker_or_parent_rejected(self):
        self.assertEqual(decide(Action(au.TERMINATE_PROCESS, attrs=ident(cmdline="claude --model opus")), CTX).rule, "process-not-owned")
        self.assertEqual(decide(Action(au.TERMINATE_PROCESS, attrs=ident(parent_pid=7)), CTX).rule, "process-not-owned")

    def test_protected_pid_and_session_rejected_even_with_marker(self):
        self.assertEqual(decide(Action(au.TERMINATE_PROCESS, attrs=ident(pid=900, owned_parent_pids=(50,))), CTX).rule, "protected-process")
        self.assertEqual(decide(Action(au.TERMINATE_PROCESS, attrs=ident(session_id="s-real")), CTX).rule, "protected-process")


class ProtectedActions(unittest.TestCase):
    def test_unconditional_denials_survive_every_grant(self):
        ctx = PolicyContext(roots=(ROOT,), repo_root=ROOT, allow_paid_api=True, grants=frozenset(au.PROTECTED_KINDS))
        for k in (au.PURCHASE, au.SECRET_DISCLOSURE, au.ORIGINAL_MUTATION, au.SECURITY_BYPASS, au.FORCE_PUSH,
                  au.HISTORY_REWRITE, au.STAGE_ALL, au.OUTSIDE_ENVIRONMENT):
            with self.subTest(k):
                self.assertEqual(decide(Action(k), ctx).verdict, DENY)

    def test_paid_api_denied_by_default_and_needs_grant_even_when_enabled(self):
        self.assertEqual(decide(Action(au.PAID_API), CTX).rule, "paid-api-off")
        on = PolicyContext(roots=(ROOT,), repo_root=ROOT, allow_paid_api=True)
        self.assertEqual(decide(Action(au.PAID_API), on).verdict, ASK)
        self.assertEqual(decide(Action(au.PAID_API), PolicyContext(allow_paid_api=True, grants=frozenset({au.PAID_API}))).verdict, ALLOW)

    def test_resolve_mutation(self):
        self.assertEqual(decide(Action(au.RESOLVE_MUTATION), CTX).verdict, ASK)                       # production by default
        self.assertEqual(decide(Action(au.RESOLVE_MUTATION, attrs={"production": False}), CTX).verdict, ASK)  # not marked fixture
        self.assertEqual(decide(Action(au.RESOLVE_MUTATION, attrs={"production": False, "fixture": True}), CTX).verdict, ALLOW)
        self.assertEqual(decide(Action(au.RESOLVE_MUTATION), PolicyContext(grants=frozenset({au.RESOLVE_MUTATION}))).verdict, ALLOW)

    def test_irrecoverable_delete(self):
        self.assertEqual(decide(Action(au.IRRECOVERABLE_DELETE, "x/y"), CTX).verdict, ASK)
        self.assertEqual(decide(Action(au.IRRECOVERABLE_DELETE, "orchestrator/tmp/a", {"recoverable": True}), CTX).verdict, ALLOW)
        self.assertEqual(decide(Action(au.IRRECOVERABLE_DELETE, "Incoming/p/a.mov", {"recoverable": True}), CTX).rule, "originals-immutable")
        self.assertEqual(decide(Action(au.IRRECOVERABLE_DELETE, "C:/Windows/x", {"recoverable": True}), CTX).rule, "outside-environment")

    def test_force_push_and_push_to_main(self):
        self.assertEqual(decide(Action(au.GIT_PUSH, attrs={"branch": "task/x", "force": True}), CTX).rule, "force-push")
        for b in ("main", "master", "refs/heads/main"):
            self.assertEqual(decide(Action(au.GIT_PUSH, attrs={"branch": b}), CTX).rule, "push-main")

    def test_originals_never_mutated(self):
        for k in (au.UPDATE_RECORD, au.GIT_COMMIT, au.EDIT_FILE):
            self.assertEqual(decide(Action(k, "Incoming/proj/clip.mp4"), CTX).rule, "originals-immutable")

    def test_secret_files_not_written_or_read(self):
        self.assertEqual(decide(Action(au.EDIT_FILE, ".env"), CTX).rule, "secret-file-mutation")
        self.assertEqual(decide(Action(au.GIT_COMMIT, "config/credentials.json"), CTX).rule, "secret-file-mutation")
        self.assertEqual(decide(Action(au.READ, ".env"), CTX).rule, "no-secret-disclosure")

    def test_launchers_need_authorization(self):
        for f in ("pro.ps1", "backup.ps1", ".mcp.json", "tools/davinci/x.py"):
            self.assertEqual(decide(Action(au.EDIT_FILE, f), PolicyContext(roots=(ROOT,), repo_root=ROOT)).verdict, ASK)

    def test_grants_file_not_writable(self):
        self.assertEqual(decide(Action(au.EDIT_FILE, "state/authority_grants.json"), CTX).rule, "grants-file")

    def test_outside_environment_denied(self):
        self.assertEqual(decide(Action(au.EDIT_FILE, "C:/Users/other/file.txt"), CTX).rule, "outside-environment")
        self.assertEqual(decide(Action(au.EDIT_FILE, "../elsewhere/x.py"), CTX).rule, "outside-environment")

    def test_unknown_kind_default_deny(self):
        self.assertEqual(decide(Action("launch_missiles"), CTX).rule, "default-deny")
        self.assertEqual(decide(Action(""), CTX).rule, "default-deny")

    def test_enforce_raises_and_returns(self):
        self.assertTrue(enforce(Action(au.READ, "a.txt"), CTX).allowed)
        with self.assertRaises(AuthorityDenied) as cm:
            enforce(Action(au.PAID_API), CTX)
        self.assertEqual(cm.exception.decision.rule, "paid-api-off")


def verdict(cmd, tool="Bash", ctx=CTX):
    return au.evaluate_tool_call(tool, {"command": cmd}, ctx)


class CommandClassifier(unittest.TestCase):
    def test_plain_routine_all_allow(self):
        for c in ("git status", "git add orchestrator/dev_supervisor/authority.py", "git commit -m x",
                  "git push -u origin task/t-dev-024-authority-policy", "python -B -I -m unittest discover -s tests",
                  "gh pr create --base main --head task/x", "git worktree add ../w -b task/x origin/main",
                  "rtk git push origin lane/w1/L1", "git push origin HEAD:task/x"):
            with self.subTest(c):
                self.assertEqual(verdict(c).verdict, ALLOW, verdict(c))

    def test_force_push_variants(self):
        for c in ("git push --force", "git push -f origin task/x", "git push origin +task/x", "git push --force-with-lease origin x",
                  "git push origin --delete task/x", "rtk bypass git push -f origin x", "git status && git push --force"):
            with self.subTest(c):
                self.assertEqual(verdict(c).rule, "force-push")

    def test_push_to_main(self):
        for c in ("git push origin main", "git push origin HEAD:main", "git push origin HEAD:refs/heads/main"):
            with self.subTest(c):
                self.assertEqual(verdict(c).rule, "push-main")

    def test_stage_all(self):
        for c in ("git add .", "git add -A", "git add --all", "git add -a"):
            self.assertEqual(verdict(c).rule, "stage-explicit-files")

    def test_security_bypass(self):
        for c in ("git commit --no-verify -m x", "git push --no-verify origin x", "gh pr merge 3 --admin",
                  "git config core.hooksPath /dev/null", "git commit --no-gpg-sign -m x"):
            with self.subTest(c):
                self.assertEqual(verdict(c).rule, "security-bypass")

    def test_history_rewrite(self):
        self.assertEqual(verdict("git filter-branch --all").rule, "history-rewrite")
        self.assertEqual(verdict("git filter-repo --path x").rule, "history-rewrite")

    def test_incoming_protected(self):
        for c in ("rm -rf Incoming/proj", "Remove-Item -Recurse Incoming\\proj\\a.mov", "del C:\\p\\Incoming\\a.mov",
                  "mv Incoming/a.mov /tmp/a.mov", "Move-Item Incoming\\proj\\a.mov D:\\x"):
            with self.subTest(c):
                self.assertEqual(verdict(c).rule, "originals-immutable")

    def test_delete_outside_recovery_asks(self):
        self.assertEqual(verdict("rm -rf build").verdict, ASK)
        self.assertEqual(verdict("git clean -fdx").verdict, ASK)

    def test_kill_by_pid_or_name_denied(self):
        for c in ("taskkill /PID 123 /F", "taskkill /IM claude.exe", "Stop-Process -Id 123", "Stop-Process -Name claude",
                  "kill -9 123", "pkill claude", "killall python"):
            with self.subTest(c):
                self.assertEqual(verdict(c, ctx=CTX).rule, "process-identity")

    def test_secret_reads(self):
        for c in ("cat .env", "type C:\\Users\\x\\.credentials.json", "Get-Content pro.ps1", "printenv", "Get-ChildItem Env:",
                  "echo $ANTHROPIC_API_KEY", "Write-Output $env:OPENAI_API_KEY"):
            with self.subTest(c):
                self.assertEqual(verdict(c).rule, "no-secret-disclosure")

    def test_paid_api_direct_http(self):
        for c in ("curl https://api.anthropic.com/v1/messages", "Invoke-RestMethod https://api.openai.com/v1/x"):
            self.assertEqual(verdict(c).rule, "paid-api-off")

    def test_chained_commands_worst_wins(self):
        self.assertEqual(verdict("git status; git push --force").verdict, DENY)
        self.assertEqual(verdict("git status | cat; echo hi").verdict, ALLOW)

    def test_powershell_tool_is_classified_too(self):
        self.assertEqual(verdict("git push -f", tool="PowerShell").verdict, DENY)

    def test_empty_or_unknown_commands_allowed(self):
        for c in ("", "   ", "frobnicate --now"):
            self.assertEqual(verdict(c).verdict, ALLOW)


class ToolCalls(unittest.TestCase):
    def test_write_and_edit(self):
        e = lambda tool, p: au.evaluate_tool_call(tool, {"file_path": p}, CTX)
        self.assertEqual(e("Write", "orchestrator/dev_supervisor/new.py").verdict, ALLOW)
        self.assertEqual(e("Edit", "Incoming/a/b.mov").rule, "originals-immutable")
        self.assertEqual(e("Write", "orchestrator/other/x.py").rule, "not-owned")
        self.assertEqual(e("Write", "pro.ps1").verdict, ASK)
        self.assertEqual(au.evaluate_tool_call("Read", {"file_path": ".env"}, CTX).rule, "no-secret-disclosure")
        self.assertEqual(au.evaluate_tool_call("Read", {"file_path": "README.md"}, CTX).verdict, ALLOW)

    def test_resolve_mcp(self):
        t = "mcp__davinci-resolve__timeline"
        self.assertEqual(au.evaluate_tool_call(t, {"action": "get_name"}, CTX).verdict, ALLOW)
        self.assertEqual(au.evaluate_tool_call(t, {"action": "list_items"}, CTX).verdict, ALLOW)
        self.assertEqual(au.evaluate_tool_call(t, {"action": "delete_clips"}, CTX).verdict, ASK)
        self.assertEqual(au.evaluate_tool_call(t, {}, CTX).verdict, ASK)    # no verb: treated as a mutation

    def test_other_tools_not_guarded(self):
        self.assertEqual(au.evaluate_tool_call("Grep", {"pattern": "x"}, CTX).verdict, ALLOW)


class Hook(unittest.TestCase):
    def run_hook(self, payload, env=None):
        out = io.StringIO()
        rc = au.run_hook(io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)), out,
                         env or {"CS_AUTHORITY_ROOTS": ROOT, "CS_AUTHORITY_REPO_ROOT": ROOT})
        return rc, out.getvalue()

    def test_allowed_prints_nothing(self):
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": ROOT})
        self.assertEqual((rc, out), (0, ""))

    def test_denied_prints_deny_decision(self):
        rc, out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git push --force"}, "cwd": ROOT})
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual((rc, d["hookEventName"], d["permissionDecision"]), (0, "PreToolUse", "deny"))
        self.assertIn("force-push", d["permissionDecisionReason"])

    def test_ask_for_launcher_edit(self):
        _, out = self.run_hook({"tool_name": "Edit", "tool_input": {"file_path": ROOT + "/pro.ps1"}, "cwd": ROOT})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_malformed_input_fails_closed(self):
        for bad in ("not json", "[]", json.dumps({"tool_name": "Bash", "tool_input": "x"})):
            with self.subTest(bad):
                _, out = self.run_hook(bad)
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_owned_paths_from_env_and_bad_env_fails_closed(self):
        env = {"CS_AUTHORITY_ROOTS": ROOT, "CS_AUTHORITY_REPO_ROOT": ROOT, "CS_AUTHORITY_OWNED_PATHS": json.dumps(["a/"])}
        _, out = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": "b/x.py"}, "cwd": ROOT}, env)
        self.assertIn("not-owned", out)
        _, ok = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": "a/x.py"}, "cwd": ROOT}, env)
        self.assertEqual(ok, "")
        env["CS_AUTHORITY_OWNED_PATHS"] = "{broken"
        _, out = self.run_hook({"tool_name": "Write", "tool_input": {"file_path": "a/x.py"}, "cwd": ROOT}, env)
        self.assertIn("not-owned", out)

    def test_env_grants_cannot_lift_unconditional_denials(self):
        env = {"CS_AUTHORITY_ROOTS": ROOT, "CS_AUTHORITY_REPO_ROOT": ROOT,
               "CS_AUTHORITY_GRANTS": "force_push,security_bypass,paid_api,original_mutation,irrecoverable_delete"}
        _, out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git push -f"}, "cwd": ROOT}, env)
        self.assertIn("force-push", out)
        _, out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf Incoming/x"}, "cwd": ROOT}, env)
        self.assertIn("originals-immutable", out)
        # an explicit human grant does lift a grantable ASK (irrecoverable delete outside Incoming)
        _, out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}, "cwd": ROOT}, env)
        self.assertEqual(out, "")

    def test_paid_api_stays_off_through_hook_even_with_grant(self):
        env = {"CS_AUTHORITY_ROOTS": ROOT, "CS_AUTHORITY_REPO_ROOT": ROOT, "CS_AUTHORITY_GRANTS": "paid_api"}
        _, out = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "curl https://api.openai.com/v1/x"}, "cwd": ROOT}, env)
        self.assertIn("paid-api-off", out)


class CliAndSettings(unittest.TestCase):
    def test_check_exit_codes(self):
        out = io.StringIO()
        self.assertEqual(au.main(["check", "--kind", "read", "--target", "a.txt"], out=out), 0)
        self.assertTrue(out.getvalue().startswith("ALLOW"))
        out = io.StringIO()
        self.assertEqual(au.main(["check", "--kind", "paid_api"], out=out), 2)
        self.assertIn("paid-api-off", out.getvalue())
        self.assertEqual(au.main(["check", "--kind", "nonsense"], out=io.StringIO()), 2)

    def test_settings_fragment_shape_and_hook_is_not_installed(self):
        frag = au.hook_settings()
        entry = frag["hooks"]["PreToolUse"][0]
        self.assertIn("Bash", entry["matcher"])
        self.assertTrue(entry["hooks"][0]["command"].endswith(" hook"))
        out = io.StringIO()
        self.assertEqual(au.main(["settings"], out=out), 0)
        self.assertEqual(json.loads(out.getvalue()), frag)

    def test_no_network_or_subprocess_used(self):
        src = (Path(au.__file__)).read_text(encoding="utf8")
        for banned in ("import subprocess", "import socket", "urllib", "requests", "os.system", "os.kill"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
