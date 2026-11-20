"""T-DEV-028: permanent standing autonomy (issue #44).

Closed-world tool classification, advisory/authoritative hook modes, worker confinement, pending approvals with a
human-only approve path, RTK-safe derived settings and --setting-sources on every supervised launch.

Offline and synthetic: no claude, no network. One test executes the real hook command in a local python subprocess
(as test_dev_session_policy already does) to prove the authoritative install verifies.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_standing_autonomy.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_dev_loop_repair as lr  # noqa: E402
from dev_supervisor import authority as au  # noqa: E402
from dev_supervisor import launch, session_policy as sp  # noqa: E402

# Synthetic paths that are NOT under the temp directory (the policy always allows the temp directory).
ROOT = "C:/cs/CreatorStudio"
WORKTREE = "C:/cs/CreatorStudio-worktrees/t-x"
OUTSIDE = "C:/elsewhere"
SID = str(uuid.uuid4())


class _TTY(io.StringIO):
    def isatty(self):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        self.approvals = str(self.tmp / "state" / "approvals").replace("\\", "/")

    def env(self, mode=None, root=ROOT, roots=None, **extra):
        e = {"CS_AUTHORITY_ROOTS": os.pathsep.join(roots or [root]), "CS_AUTHORITY_REPO_ROOT": root,
             "CS_AUTHORITY_APPROVALS_DIR": self.approvals, "CS_AUTHORITY_ROLE": "worker" if mode == "authoritative" else "main"}
        if mode is not None:
            e["CS_AUTHORITY_MODE"] = mode
        e.update(extra)
        return e

    def hook(self, tool, tin, mode=None, cwd=ROOT, env=None, session="s-1"):
        out = io.StringIO()
        payload = {"tool_name": tool, "tool_input": tin, "cwd": cwd, "session_id": session}
        self.assertEqual(au.run_hook(io.StringIO(json.dumps(payload)), out, env or self.env(mode)), 0)
        text = out.getvalue()
        if not text:
            return None, ""
        d = json.loads(text)["hookSpecificOutput"]
        self.assertEqual(d["hookEventName"], "PreToolUse")
        return d["permissionDecision"], d["permissionDecisionReason"]


# (tool, input, advisory decision, authoritative decision); None = the hook prints nothing.
# "queue" = deny with a pending approval record written.
CASES = [
    # ---- shell: deny-list unchanged, routine allowlist in authoritative mode
    ("Bash", {"command": "git status --short"}, None, "allow"),
    ("Bash", {"command": "rtk git log --oneline -5"}, None, "allow"),
    ("Bash", {"command": "git log --oneline | head -5"}, None, "allow"),
    ("Bash", {"command": "git diff 2>&1"}, None, "allow"),
    ("Bash", {"command": "git add orchestrator/x.py tests/y.py"}, None, "allow"),
    ("Bash", {"command": 'git commit -m "T-DEV-028: x" -m "Co-Authored-By: a"'}, None, "allow"),
    ("Bash", {"command": "git branch task/new"}, None, "allow"),
    ("Bash", {"command": "git worktree list"}, None, "allow"),
    ("Bash", {"command": "git config --get user.name"}, None, "allow"),
    ("Bash", {"command": "gh pr view 98"}, None, "allow"),
    ("Bash", {"command": "gh api repos/KasluBuilds/CreatorStudio/pulls"}, None, "allow"),
    ("Bash", {"command": "python -B -I -m unittest discover -s tests -p test_dev_*.py"}, None, "allow"),
    ("Bash", {"command": "python -B -m creator_state list"}, None, "allow"),
    ("Bash", {"command": "python -B -m governance wave submit"}, None, "allow"),
    ("Bash", {"command": "sed -n 1,80p orchestrator/x.py"}, None, "allow"),
    ("Bash", {"command": "find . -name x.py"}, None, "allow"),
    ("Bash", {"command": "mkdir -p build/out"}, None, "allow"),
    ("PowerShell", {"command": "ls; pwd"}, None, "allow"),
    ("Bash", {"command": "git push origin task/x"}, None, "deny"),           # Main pushes; workers do not
    ("Bash", {"command": "git branch -D old"}, None, "deny"),
    ("Bash", {"command": "git checkout -- ."}, None, "deny"),
    ("Bash", {"command": "git commit -am x"}, None, "deny"),
    ("Bash", {"command": "git -c core.hooksPath=/dev/null status"}, "deny", "deny"),
    ("Bash", {"command": "git stash"}, None, "deny"),
    ("Bash", {"command": "git config user.name x"}, None, "deny"),
    ("Bash", {"command": "gh pr merge 98"}, None, "deny"),
    ("Bash", {"command": "gh api -X POST repos/x/issues"}, None, "deny"),
    ("Bash", {"command": "gh api repos/x/issues -f title=t"}, None, "deny"),
    ("Bash", {"command": "python -c \"print(1)\""}, None, "deny"),
    ("Bash", {"command": "python -B -m creator_state ingest x"}, None, "deny"),
    ("Bash", {"command": "python -B -m dev_supervisor assign --task x --yes"}, None, "deny"),
    ("Bash", {"command": "curl https://example.com"}, None, "deny"),
    ("Bash", {"command": "echo hi > out.txt"}, None, "deny"),
    ("Bash", {"command": "git status $(rm -rf x)"}, None, "deny"),
    ("Bash", {"command": "git status & rm x"}, None, "deny"),
    ("Bash", {"command": "find . -name x -delete"}, None, "deny"),
    ("Bash", {"command": "sed -i s/a/b/ x.py"}, None, "deny"),
    ("Bash", {"command": "sed -n 1w out x.py"}, None, "deny"),
    ("Bash", {"command": "sort -o out x"}, None, "deny"),
    ("Bash", {"command": "rg --pre ./evil x"}, None, "deny"),
    ("Bash", {"command": "grep KEY .env"}, None, "deny"),
    ("Bash", {"command": "mkdir C:/elsewhere/x"}, None, "deny"),
    ("Bash", {"command": "rtk curl https://example.com"}, None, "deny"),
    ("Bash", {"command": "git push --force origin task/x"}, "deny", "deny"),
    ("Bash", {"command": "git add ."}, "deny", "deny"),
    ("Bash", {"command": "git commit --no-verify -m x"}, "deny", "deny"),
    ("Bash", {"command": "rm -rf Incoming/x"}, "deny", "deny"),
    ("Bash", {"command": "cat pro.ps1"}, "deny", "deny"),
    ("Bash", {"command": "curl https://api.anthropic.com/v1/messages"}, "deny", "deny"),
    ("Bash", {"command": "rm -rf build"}, "queue", "queue"),                  # no recovery path: REQUIRE_AUTHORIZATION
    # ---- file read: path gate; Glob/Grep/LS without a path use cwd
    ("Read", {"file_path": ROOT + "/a.py"}, None, "allow"),
    ("Read", {"file_path": OUTSIDE + "/a.py"}, "deny", "deny"),
    ("Read", {"file_path": ROOT + "/.env"}, "deny", "deny"),
    ("Read", {"file_path": ROOT + "/pro.ps1"}, "deny", "deny"),
    ("NotebookRead", {"notebook_path": ROOT + "/n.ipynb"}, None, "allow"),
    ("LS", {"path": ROOT}, None, "allow"),
    ("Glob", {"pattern": "**/*.py"}, None, "allow"),
    ("Glob", {"pattern": "*.py", "path": OUTSIDE}, "deny", "deny"),
    ("Grep", {"pattern": "x"}, None, "allow"),
    ("Grep", {"pattern": "x", "path": OUTSIDE + "/src"}, "deny", "deny"),
    # ---- file write: EDIT_FILE plus the authority boundary
    ("Write", {"file_path": ROOT + "/a.py", "content": "x"}, None, "allow"),
    ("Edit", {"file_path": ROOT + "/a.py", "old_string": "a", "new_string": "b"}, None, "allow"),
    ("MultiEdit", {"file_path": ROOT + "/a.py", "edits": []}, None, "allow"),
    ("NotebookEdit", {"notebook_path": ROOT + "/n.ipynb"}, None, "allow"),
    ("Write", {"file_path": OUTSIDE + "/a.py"}, "deny", "deny"),
    ("Write", {"file_path": ROOT + "/authority_grants.json"}, "deny", "deny"),
    ("Write", {"file_path": ROOT + "/.claude/settings.json"}, "deny", "deny"),
    ("Edit", {"file_path": ROOT + "/.claude/settings.local.json"}, "deny", "deny"),
    ("Edit", {"file_path": ROOT + "/pro.ps1"}, "queue", "queue"),
    ("Edit", {"file_path": ROOT + "/.mcp.json"}, "queue", "queue"),
    # ---- internal/session tools
    *[(t, {}, None, "allow") for t in au.INTERNAL_TOOLS],
    # ---- subagents
    ("Agent", {"prompt": "x"}, None, "allow"),
    ("Task", {"prompt": "x"}, None, "allow"),
    # ---- web read
    ("WebFetch", {"url": "https://docs.python.org/3/"}, None, "allow"),
    ("WebSearch", {"query": "python"}, None, "allow"),
    ("WebFetch", {"url": "https://api.anthropic.com/v1/messages"}, "deny", "deny"),
    ("WebFetch", {"url": "https://api.openai.com/v1/x"}, "deny", "deny"),
    # ---- publishing: protected, pure reads allowed
    ("Artifact", {"action": "create"}, "queue", "queue"),
    ("Artifact", {"action": "read"}, None, "allow"),
    ("Artifact", {"action": "list"}, None, "allow"),
    ("ArtifactData", {"action": "get"}, None, "allow"),
    ("ArtifactData", {"action": "query"}, None, "allow"),
    ("ArtifactData", {"action": "set"}, "queue", "queue"),
    ("ArtifactComments", {"action": "list"}, "queue", "queue"),
    ("mcp__claude-artifacts__publish", {}, "queue", "queue"),
    ("mcp__claude_ai_Claude_Docs__batch", {}, "queue", "queue"),
    ("mcp__claude_ai_Claude_Docs__update", {}, "queue", "queue"),
    ("mcp__claude_ai_Claude_Docs__read", {}, None, "allow"),
    ("mcp__claude_ai_Claude_Docs__query", {}, None, "allow"),
    ("mcp__claude_ai_Claude_Docs__guide", {}, None, "allow"),
    ("mcp__claude_ai_Claude_Docs__export", {}, None, "allow"),
    ("DesignSync", {}, "queue", "queue"),
    ("SendToProject", {}, "queue", "queue"),
    ("SendToProjectContext", {}, "queue", "queue"),
    ("PushNotification", {"message": "x"}, "queue", "queue"),
    ("RemoteTrigger", {}, "queue", "queue"),
    ("CronCreate", {}, "queue", "queue"),
    ("CronDelete", {}, "queue", "queue"),
    # ---- DaVinci Resolve MCP: read verbs allowed, everything else a production mutation
    ("mcp__davinci-resolve__timeline", {"action": "get_current"}, None, "allow"),
    ("mcp__davinci-resolve__timeline", {"action": "delete_clips"}, "queue", "queue"),
    # ---- IDE
    ("mcp__ide__getDiagnostics", {}, None, "allow"),
    ("mcp__ide__executeCode", {"code": "1"}, "queue", "queue"),
    ("mcp__ide__somethingElse", {}, "deny", "deny"),
    # ---- unknown / malformed: default-deny
    ("FooTool", {}, "deny", "deny"),
    ("EnterWorktree", {}, "deny", "deny"),
    ("mcp__other-server__do", {}, "deny", "deny"),
    ("mcp__bad", {}, "deny", "deny"),
    ("bad name!", {}, "deny", "deny"),
    ("", {}, "deny", "deny"),
]


class ClosedWorldTable(Base):
    def test_every_case_in_both_modes(self):
        for tool, tin, adv, auth in CASES:
            for mode, want in (("advisory", adv), ("authoritative", auth)):
                with self.subTest(tool=tool, input=tin, mode=mode):
                    got, why = self.hook(tool, tin, mode)
                    if want == "queue":
                        self.assertEqual(got, "deny")
                        self.assertIn("queued as approval", why)
                        self.assertIn("continue with unrelated work", why)
                    else:
                        self.assertEqual(got, want, why)
                        if want == "deny":
                            self.assertNotIn("queued as approval", why)

    def test_unset_mode_is_advisory(self):
        self.assertEqual(self.hook("Bash", {"command": "curl https://example.com"}, None)[0], None)
        self.assertEqual(self.hook("FooTool", {}, None)[0], "deny")

    def test_unrecognised_mode_is_treated_as_authoritative(self):
        got, why = self.hook("Bash", {"command": "curl https://example.com"}, "permissive")
        self.assertEqual(got, "deny")
        self.assertIn("not on the worker routine allowlist", why)

    def test_unknown_tool_kinds_and_tables(self):
        for k in (au.SPAWN_SUBAGENT, au.WEB_READ):
            self.assertIn(k, au.AUTONOMOUS_KINDS)
        for k in (au.PUBLISH, au.IDE_EXECUTE, au.UNKNOWN_TOOL, au.AUTHORITY_CHANGE):
            self.assertIn(k, au.PROTECTED_KINDS)
        self.assertTrue(set(au.UNCONDITIONAL_KINDS) <= set(au.PROTECTED_KINDS))
        self.assertFalse(set(au.AUTONOMOUS_KINDS) & set(au.PROTECTED_KINDS))
        self.assertEqual(au.classify_tool_call("Nope", {})[0].kind, au.UNKNOWN_TOOL)

    def test_grants_cannot_lift_unknown_tool_or_authority_boundary(self):
        env = self.env("advisory", CS_AUTHORITY_GRANTS="unknown_tool,authority_change,publish")
        self.assertEqual(self.hook("FooTool", {}, env=env)[0], "deny")
        self.assertEqual(self.hook("Write", {"file_path": ROOT + "/.claude/settings.json"}, env=env)[0], "deny")
        self.assertEqual(self.hook("Artifact", {"action": "create"}, env=env)[0], None)   # publish is grantable

    def test_deny_list_semantics_unchanged_for_shell(self):
        ctx = au.PolicyContext(roots=(ROOT,), repo_root=ROOT)
        for cmd, rule in (("git push --force", "force-push"), ("git add -A", "stage-explicit-files"),
                          ("git push origin main", "push-main"), ("rm -rf Incoming/a", "originals-immutable")):
            with self.subTest(cmd):
                self.assertEqual(au.evaluate_tool_call("Bash", {"command": cmd}, ctx).rule, rule)

    def test_shell_mentions_of_the_authority_boundary_are_denied(self):
        for cmd in (f"cat {self.approvals}/pending/0123456789abcdef.json",
                    "python -B -m dev_supervisor.authority approvals approve 0123456789abcdef",
                    "echo {} > authority_grants.json", "cat ~/.claude/settings.json",
                    f"ls {self.approvals.rsplit('/', 1)[0]}/policy"):
            for mode in ("advisory", "authoritative"):
                with self.subTest(cmd=cmd, mode=mode):
                    got, why = self.hook("Bash", {"command": cmd}, mode)
                    self.assertEqual(got, "deny")
                    self.assertIn("authority-boundary", why)

    def test_write_into_approvals_or_policy_dir_is_denied(self):
        for p in (self.approvals + "/pending/0123456789abcdef.json",
                  self.approvals.rsplit("/", 1)[0] + "/policy/main-x.settings.json"):
            for mode in ("advisory", "authoritative"):
                with self.subTest(p=p, mode=mode):
                    got, why = self.hook("Write", {"file_path": p, "content": "{}"}, mode)
                    self.assertEqual(got, "deny")
                    self.assertIn("authority-boundary", why)

    def test_read_of_approvals_is_allowed(self):
        self.assertEqual(self.hook("Read", {"file_path": self.approvals + "/pending/x.json"}, "advisory")[0], None)


class PendingApprovals(Base):
    def pending(self):
        d = Path(self.approvals) / "pending"
        return sorted(d.glob("*.json")) if d.exists() else []

    def test_record_fields(self):
        tin = {"command": "rm -rf build"}
        got, why = self.hook("Bash", tin, "advisory", session="sess-42")
        self.assertEqual(got, "deny")
        files = self.pending()
        self.assertEqual(len(files), 1)
        rec = json.loads(files[0].read_text(encoding="utf-8"))
        h = au.call_hash("Bash", tin)
        self.assertEqual(rec["id"], h[:16])
        self.assertEqual(rec["call_hash"], h)
        self.assertIn(rec["id"], why)
        self.assertEqual((rec["session_id"], rec["role"], rec["tool"], rec["status"]), ("sess-42", "main", "Bash", "PENDING"))
        self.assertEqual(rec["rule"], "irrecoverable-delete")
        self.assertTrue(rec["reason"])
        self.assertRegex(rec["created_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("rm -rf build", rec["input_summary"])

    def test_dedupe_same_call_and_distinct_calls(self):
        tin = {"command": "rm -rf build"}
        self.hook("Bash", tin, "advisory")
        first = self.pending()[0].read_text(encoding="utf-8")
        _, why = self.hook("Bash", dict(reversed(list(tin.items()))), "authoritative")   # same canonical input
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0].read_text(encoding="utf-8"), first)
        self.hook("Bash", {"command": "rm -rf build2"}, "advisory")
        self.assertEqual(len(self.pending()), 2)
        rec, created = au.queue_approval(self.approvals, "Bash", tin, au.Decision(au.ASK, "r", "x"))
        self.assertFalse(created)

    def test_atomic_write_leaves_no_partial_file(self):
        with mock.patch.object(au.os, "replace", side_effect=OSError("disk full")):
            got, why = self.hook("Edit", {"file_path": ROOT + "/pro.ps1"}, "advisory")
        self.assertEqual(got, "deny")
        self.assertIn("could not be queued", why)
        d = Path(self.approvals) / "pending"
        self.assertEqual(list(d.iterdir()) if d.exists() else [], [])    # no record, no temp file left behind
        self.hook("Edit", {"file_path": ROOT + "/pro.ps1"}, "advisory")
        self.assertEqual([p.suffix for p in d.iterdir()], [".json"])

    def test_unwritable_queue_never_crashes_the_hook(self):
        blocker = self.tmp / "file-not-dir"
        blocker.write_text("x", encoding="utf-8")
        env = self.env("advisory", CS_AUTHORITY_APPROVALS_DIR=str(blocker))
        got, why = self.hook("Edit", {"file_path": ROOT + "/pro.ps1"}, env=env)
        self.assertEqual(got, "deny")
        self.assertIn("could not be queued", why)
        env = self.env("authoritative")
        del env["CS_AUTHORITY_APPROVALS_DIR"]
        self.assertEqual(self.hook("Edit", {"file_path": ROOT + "/pro.ps1"}, env=env)[0], "deny")

    def test_unknown_and_denied_calls_are_not_queued(self):
        self.hook("FooTool", {}, "advisory")
        self.hook("Bash", {"command": "git push --force"}, "advisory")
        self.assertEqual(self.pending(), [])

    def test_summary_is_redacted_and_truncated(self):
        s = au.input_summary({"command": "x token=abcdef123456 sk-abcdefghijklmnop " + "y" * 1000})
        self.assertNotIn("abcdef123456", s)
        self.assertNotIn("sk-abcdefghijklmnop", s)
        self.assertTrue(s.endswith("...(truncated)"))
        self.assertLess(len(s), 330)


class ApproveCli(Base):
    def queue(self, tin=None):
        tin = tin or {"file_path": ROOT + "/pro.ps1"}
        self.hook("Edit", tin, "advisory")
        return au.approval_id(au.call_hash("Edit", tin)), tin

    def run_cli(self, argv, stdin=None, env=None, answer="APPROVE"):
        out = io.StringIO()
        rc = au.main(argv, stdin=stdin or io.StringIO(""), out=out, env=env if env is not None else {},
                     ask=lambda prompt: answer)
        return rc, out.getvalue()

    def status(self, aid):
        return au.read_approval(self.approvals, aid)["status"]

    def test_list_is_read_only(self):
        aid, _ = self.queue()
        p = Path(self.approvals) / "pending" / f"{aid}.json"
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        rc, out = self.run_cli(["approvals", "--dir", self.approvals, "list"])
        self.assertEqual(rc, 0)
        self.assertIn(aid, out)
        self.assertIn("PENDING", out)
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)

    def test_refused_without_tty(self):
        aid, _ = self.queue()
        rc, out = self.run_cli(["approvals", "--dir", self.approvals, "approve", aid], stdin=io.StringIO(""))
        self.assertEqual(rc, 2)
        self.assertIn("not an interactive terminal", out)
        self.assertEqual(self.status(aid), "PENDING")

    def test_refused_inside_a_hooked_session(self):
        aid, _ = self.queue()
        for mode in ("advisory", "authoritative", ""):
            with self.subTest(mode=mode):
                rc, out = self.run_cli(["approvals", "--dir", self.approvals, "approve", aid], stdin=_TTY(""),
                                       env={"CS_AUTHORITY_MODE": mode})
                self.assertEqual(rc, 2)
                self.assertIn("hooked session", out)
        self.assertEqual(self.status(aid), "PENDING")

    def test_refused_without_typed_confirmation_and_for_unknown_ids(self):
        aid, _ = self.queue()
        rc, out = self.run_cli(["approvals", "--dir", self.approvals, "approve", aid], stdin=_TTY(""), answer="yes")
        self.assertEqual((rc, self.status(aid)), (2, "PENDING"))
        rc, out = self.run_cli(["approvals", "--dir", self.approvals, "approve", "ffffffffffffffff"], stdin=_TTY(""))
        self.assertEqual(rc, 2)
        rc, out = self.run_cli(["approvals", "--dir", self.approvals, "approve", "../../x"], stdin=_TTY(""))
        self.assertEqual(rc, 2)

    def test_human_approval_records_approved_once(self):
        aid, _ = self.queue()
        rc, out = self.run_cli(["approvals", "--dir", self.approvals, "approve", aid], stdin=_TTY(""))
        self.assertEqual(rc, 0, out)
        rec = au.read_approval(self.approvals, aid)
        self.assertEqual(rec["status"], "APPROVED")
        self.assertRegex(rec["approved_utc"], r"Z$")
        rc, out = self.run_cli(["approvals", "--dir", self.approvals, "approve", aid], stdin=_TTY(""))
        self.assertEqual(rc, 2)          # not PENDING any more

    def test_approved_call_asks_only_in_advisory_mode(self):
        aid, tin = self.queue()
        self.assertEqual(self.run_cli(["approvals", "--dir", self.approvals, "approve", aid], stdin=_TTY(""))[0], 0)
        got, why = self.hook("Edit", tin, "advisory")
        self.assertEqual(got, "ask")
        self.assertIn(aid, why)
        got, why = self.hook("Edit", tin, "authoritative")
        self.assertEqual(got, "deny")
        # a different call is not covered by the approval
        got, _ = self.hook("Edit", {"file_path": ROOT + "/pro.ps1", "old_string": "a"}, "advisory")
        self.assertEqual(got, "deny")
        # a forged APPROVED record whose hash does not match the call is ignored
        other = {"file_path": ROOT + "/backup.ps1"}
        oid = au.approval_id(au.call_hash("Edit", other))
        p = Path(self.approvals) / "pending" / f"{oid}.json"
        p.write_text(json.dumps({"id": oid, "call_hash": "0" * 64, "status": "APPROVED"}), encoding="utf-8")
        self.assertEqual(self.hook("Edit", other, "advisory")[0], "deny")

    def test_main_cannot_reach_the_approve_path(self):
        aid, _ = self.queue()
        got, _ = self.hook("Bash", {"command": f"python -B -m dev_supervisor.authority approvals approve {aid}"}, "advisory")
        self.assertEqual(got, "deny")
        got, _ = self.hook("Write", {"file_path": f"{self.approvals}/pending/{aid}.json", "content": "{}"}, "advisory")
        self.assertEqual(got, "deny")
        self.assertEqual(self.status(aid), "PENDING")


USER_SETTINGS = {
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "powershell -File guard.ps1"},
                                                     {"type": "command", "command": "rtk hook claude"}]},
                       {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}],
        "PostToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": "rtk hook claude --post"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "node notify.js"}]}],
    },
    "autoMode": {"enabled": True}, "modelSettings": {"x": 1}, "model": "opus", "statusLine": {"type": "command"},
    "permissions": {"allow": ["Bash(git status)"]},
    "env": {"ANTHROPIC_API_KEY": "sk-should-never-be-copied"}, "apiKeyHelper": "helper.sh",
    "awsAuthRefresh": "aws.sh", "otelHeadersHelper": "otel.sh", "skipDangerousModePermissionPrompt": True,
}


class DerivedSettings(Base):
    def write_user(self, data):
        p = self.tmp / "user-settings.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return p

    def test_copies_only_allowed_keys_and_drops_rtk(self):
        d = sp.build_settings("HOOK", sp.load_user_settings(self.write_user(USER_SETTINGS)))
        self.assertEqual(set(d), {"hooks", "autoMode", "modelSettings", "model", "statusLine", "permissions"})
        self.assertEqual(d["autoMode"], {"enabled": True})
        text = json.dumps(d)
        for gone in ("rtk ", "sk-should", "apiKeyHelper", "awsAuthRefresh", "otelHeadersHelper", "skipDangerous"):
            self.assertNotIn(gone, text)
        pre = d["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], {"matcher": ".*", "hooks": [{"type": "command", "command": "HOOK"}]})
        self.assertEqual(pre[1]["hooks"], [{"type": "command", "command": "powershell -File guard.ps1"}])
        self.assertEqual(len(pre), 2)                     # the rtk-only group is gone
        self.assertNotIn("PostToolUse", d["hooks"])       # an event left without hooks is dropped
        self.assertEqual(d["hooks"]["Stop"], USER_SETTINGS["hooks"]["Stop"])

    def test_missing_or_unreadable_user_settings_give_only_the_authority_hook(self):
        only = {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": "HOOK"}]}]}}
        for src in (self.tmp / "missing.json", self.write_user("{not json"), self.write_user("[1, 2]")):
            with self.subTest(src=src.name):
                self.assertEqual(sp.build_settings("HOOK", sp.load_user_settings(src)), only)

    def test_install_writes_derived_file_and_detects_tampering(self):
        user = self.write_user(USER_SETTINGS)
        inst = sp.install(self.tmp / "state", SID, "main", ROOT, run=_fake_run, user_settings=user)
        on_disk = json.loads(Path(inst.settings_path).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["autoMode"], {"enabled": True})
        self.assertNotIn("env", on_disk)
        self.assertEqual(on_disk["hooks"]["PreToolUse"][0]["hooks"][0]["command"], inst.hook_command)
        argv = sp.argv_with_policy(["claude", "-p"], inst)
        self.assertEqual(argv, ["claude", "--settings", inst.settings_path, "--setting-sources", "project,local", "-p"])
        on_disk["env"] = {"X": "1"}
        Path(inst.settings_path).write_text(json.dumps(on_disk), encoding="utf-8")
        with self.assertRaises(sp.PolicyInstallError):
            sp.argv_with_policy(["claude"], inst)

    def test_existing_setting_sources_flag_is_refused(self):
        inst = sp.install(self.tmp / "state", SID, "worker", ROOT, run=_fake_run, user_settings=self.tmp / "none.json")
        with self.assertRaises(sp.PolicyInstallError):
            sp.argv_with_policy(["claude", "--setting-sources", "user"], inst)

    def test_matcher_is_every_tool(self):
        self.assertEqual(sp.MATCHER, ".*")
        self.assertEqual(sp.build_settings("H")["hooks"]["PreToolUse"][0]["matcher"], ".*")
        self.assertEqual(au.hook_settings()["hooks"]["PreToolUse"][0]["matcher"], ".*")

    def test_env_keys_per_role(self):
        state = self.tmp / "state"
        for role, mode in (("main", "advisory"), ("worker", "authoritative"), ("reviewer", "authoritative")):
            with self.subTest(role=role):
                env = sp.install(state, SID, role, ROOT, run=_fake_run, user_settings=self.tmp / "none.json").env
                self.assertEqual(env["CS_AUTHORITY_MODE"], mode)
                self.assertEqual(env["CS_AUTHORITY_ROLE"], role)
                self.assertEqual(env["CS_AUTHORITY_APPROVALS_DIR"], str(state / "approvals").replace("\\", "/"))

    def test_verification_expects_explicit_allow_in_authoritative_mode(self):
        silent = SimpleNamespace(returncode=0, stdout="")
        deny = SimpleNamespace(returncode=0, stdout=json.dumps(au._out("deny", "x")))
        outs = iter([deny, silent, deny])
        with self.assertRaises(sp.PolicyInstallError):
            sp.verify_hook("cmd", {"CS_AUTHORITY_MODE": "authoritative"}, "r", lambda *a, **k: next(outs))

    def test_real_hook_verifies_in_authoritative_mode(self):
        inst = sp.install(self.tmp / "state", SID, "worker", str(self.tmp / "wt"), user_settings=self.tmp / "none.json")
        self.assertTrue(inst.verified)
        self.assertEqual(inst.env["CS_AUTHORITY_MODE"], "authoritative")


def _fake_run(cmd, **kw):
    stdin, env = kw.get("input", ""), kw.get("env") or {}
    if "git status" in stdin:
        out = json.dumps(au._out("allow", "x")) if env.get("CS_AUTHORITY_MODE") == "authoritative" else ""
    else:
        out = json.dumps(au._out("deny", "x"))
    return SimpleNamespace(returncode=0, stdout=out)


class WorkerConfinement(Base):
    def test_worker_write_to_main_checkout_is_denied(self):
        env = self.env("authoritative", root=WORKTREE, roots=[WORKTREE])
        got, why = self.hook("Write", {"file_path": ROOT + "/orchestrator/x.py", "content": "x"}, env=env, cwd=WORKTREE)
        self.assertEqual(got, "deny")
        self.assertIn("outside-environment", why)
        got, _ = self.hook("Write", {"file_path": WORKTREE + "/orchestrator/x.py", "content": "x"}, env=env, cwd=WORKTREE)
        self.assertEqual(got, "allow")
        got, _ = self.hook("Read", {"file_path": ROOT + "/CLAUDE.md"}, env=env, cwd=WORKTREE)
        self.assertEqual(got, "deny")

    def test_main_keeps_its_roots(self):
        env = self.env("advisory", root=ROOT, roots=[ROOT, "C:/cs/CreatorStudio-worktrees"])
        self.assertIsNone(self.hook("Write", {"file_path": WORKTREE + "/x.py"}, env=env)[0])
        self.assertIsNone(self.hook("Write", {"file_path": ROOT + "/x.py"}, env=env)[0])


class LaunchWiring(lr.Loop):
    def test_worker_env_is_confined_authoritative_and_argv_has_setting_sources(self):
        env, store, loop = self.make()
        w = self.assign(env, loop)
        spec = env.worker_launcher.specs[-1]
        wt = str(env.wt / "w").replace("\\", "/")
        self.assertEqual(spec.env["CS_AUTHORITY_REPO_ROOT"], wt)
        self.assertEqual(spec.env["CS_AUTHORITY_ROOTS"], wt)
        self.assertNotEqual(spec.env["CS_AUTHORITY_REPO_ROOT"], str(env.repo).replace("\\", "/"))
        self.assertEqual(spec.env["CS_AUTHORITY_MODE"], "authoritative")
        self.assertEqual(spec.env["CS_AUTHORITY_APPROVALS_DIR"], str(store.directory / "approvals").replace("\\", "/"))
        self.assertEqual(spec.argv[3:5], ["--setting-sources", "project,local"])
        # restart re-installs the policy with the same keys
        from dev_supervisor.workers import WorkerStatus
        w.status = WorkerStatus.STOPPED
        w.next_restart_not_before = None
        loop.sup.registry.update(w)
        loop.sup.restart(w.worker_id)
        spec2 = env.worker_launcher.specs[-1]
        self.assertIsNot(spec2, spec)
        for k in ("CS_AUTHORITY_MODE", "CS_AUTHORITY_APPROVALS_DIR", "CS_AUTHORITY_REPO_ROOT", "CS_AUTHORITY_ROLE"):
            self.assertEqual(spec2.env[k], spec.env[k])
        self.assertEqual(spec2.argv[3:5], ["--setting-sources", "project,local"])

    def test_main_env_is_advisory_and_argv_has_setting_sources(self):
        env, store, loop = self.make()
        loop.ensure_main()
        opened = env.terminal.opened[-1]
        self.assertEqual(opened["argv"][1], "--settings")
        self.assertEqual(opened["argv"][3:5], ["--setting-sources", "project,local"])
        self.assertEqual(opened["env"]["CS_AUTHORITY_MODE"], "advisory")
        self.assertEqual(opened["env"]["CS_AUTHORITY_ROLE"], "main")
        self.assertEqual(opened["env"]["CS_AUTHORITY_APPROVALS_DIR"], str(store.directory / "approvals").replace("\\", "/"))

    def test_replacement_launch_keeps_the_new_keys(self):
        from dev_supervisor import lifecycle
        env, store, loop = self.make()
        sid = str(uuid.uuid4())
        (env.tmp / "resume.txt").write_text("resume", encoding="utf-8")
        spec = lifecycle.LaunchSpec(argv=["claude", "--model", "opus", "--session-id", sid], cwd=str(env.repo),
                                    stdin_path=str(env.tmp / "resume.txt"), stdout_path="o", stderr_path="e",
                                    max_runtime_s=1)
        loop.main_launcher.launch(spec)
        opened = env.terminal.opened[-1]
        self.assertEqual(opened["argv"][3:5], ["--setting-sources", "project,local"])
        self.assertEqual(opened["env"]["CS_AUTHORITY_MODE"], "advisory")
        self.assertIn("CS_AUTHORITY_APPROVALS_DIR", opened["env"])

    def test_main_prompt_has_standing_authority(self):
        env, store, loop = self.make()
        p = launch.write_main_prompt(store, str(uuid.uuid4()))
        text = p.read_text(encoding="utf-8")
        self.assertIn("STANDING AUTHORITY", text)
        self.assertIn("Never ask Lukas for routine operations", text)
        self.assertIn("pending approvals", text)
        self.assertIn("If a gate fails, stop and report", text)
        for word in ("paid API", "launchers/tools/.mcp.json", "publishing", "merge ONLY when every gate passes"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
