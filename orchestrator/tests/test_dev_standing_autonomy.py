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

# Fake key material is assembled at runtime so the CI credential scan never sees a literal key pattern.
FAKE_KEY = "sk" + "-abcdefghijklmnop"
FAKE_ENV_KEY = "sk" + "-should-never-be-copied"

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
    ("Bash", {"command": "git log --oneline | head -5"}, None, "deny"),    # structural rule: one simple command
    ("Bash", {"command": "git diff 2>&1"}, None, "allow"),
    ("Bash", {"command": "git add orchestrator/x.py tests/y.py"}, None, "allow"),
    ("Bash", {"command": 'git commit -m "T-DEV-028: x" -m "Co-Authored-By: a"'}, None, "allow"),
    ("Bash", {"command": "git branch task/new"}, None, "allow"),
    ("Bash", {"command": "git worktree list"}, None, "allow"),
    ("Bash", {"command": "git config --get user.name"}, None, "allow"),
    ("Bash", {"command": "gh pr view 98"}, None, "allow"),
    ("Bash", {"command": "gh api repos/KasluBuilds/CreatorStudio/pulls"}, None, "allow"),
    ("Bash", {"command": "python -B -I -m unittest discover -s tests -p test_dev_x.py"}, None, "allow"),
    ("Bash", {"command": "python -B -m creator_state list"}, None, "allow"),
    ("Bash", {"command": "python -B -m governance wave submit"}, None, "allow"),
    ("Bash", {"command": "sed -n 1,80p orchestrator/x.py"}, None, "allow"),
    ("Bash", {"command": "find . -name x.py"}, None, "allow"),
    ("Bash", {"command": "mkdir -p build/out"}, None, "allow"),
    ("PowerShell", {"command": "ls"}, None, "deny"),                         # workers use Bash
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
    ("mcp__davinci-resolve__get_current_timeline", {}, None, "allow"),             # read by NAME
    ("mcp__davinci-resolve__timeline", {"action": "get_current"}, None, "deny"),   # input-dependent: no worker allow
    ("mcp__davinci-resolve__timeline", {"action": "delete_clips"}, "queue", "queue"),
    ("mcp__davinci-resolve__delete_timeline", {"action": "list"}, "queue", "queue"),  # mutating name wins
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
        s = au.input_summary({"command": "x token=abcdef123456 " + FAKE_KEY + " " + "y" * 1000})
        self.assertNotIn("abcdef123456", s)
        self.assertNotIn(FAKE_KEY, s)
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
    "permissions": {"allow": ["Bash(git status)"], "deny": ["Bash(git push --force)"], "defaultMode": "bypassPermissions",
                    "additionalDirectories": ["C:/"]},
    "env": {"ANTHROPIC_API_KEY": FAKE_ENV_KEY}, "apiKeyHelper": "helper.sh",
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
        self.assertEqual(d["permissions"], {"deny": ["Bash(git push --force)"]})   # allow/defaultMode/dirs dropped
        text = json.dumps(d)
        for gone in ("rtk ", "sk-should", "apiKeyHelper", "awsAuthRefresh", "otelHeadersHelper", "skipDangerous",
                     "bypassPermissions", "additionalDirectories", "Bash(git status)"):
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


# ---------------------------------------------------------------- security review round 1 (B1-B5, N1-N4, N6, N7, N9)
WT = "C:/wt/task"                       # synthetic worker worktree (the worker's only root)
LIVE = au.live_boundary_files()[0]      # the authority.py this hook process runs
LIVE_SP = au.live_boundary_files()[1]
LIVE_ROOT = LIVE.rsplit("/orchestrator/dev_supervisor/", 1)[0]

# Authoritative worker, cwd = its worktree: every command below must get an explicit deny.
WORKER_DENY = [
    # B1: option injection into fetch/pull and diff drivers
    "git fetch --upload-pack='touch /tmp/pwn' /some/repo", "git fetch --upload-pack=calc.exe origin",
    "git pull --upload-pack=calc.exe ../x", "git fetch --upload-p=x origin", "git pull --receive-pack=x origin",
    "git fetch -u origin", "git fetch --exec=x origin", "git fetch --config=x origin", "git fetch ext::calc",
    "git fetch origin +main:main", "git fetch origin main:main", "git fetch /some/repo", "git pull ../x main",
    "git -c core.sshCommand=calc fetch origin", "git diff --ext-diff", "git log --textconv -p",
    "git show --textconv HEAD", "git diff --output=/tmp/x", "git log --output=x", "git diff --no-index C:/x C:/y",
    "git diff --ext", "git log --out=x", "git show HEAD:.env", "git show HEAD:sub/.git-credentials",
    # B2: git global options and cwd escape
    "git -C C:/cs/CreatorStudio commit -m x", "git -C C:/cs/CreatorStudio checkout main",
    "git -C C:/cs/CreatorStudio stash push", "git -C ../.. add foo", "git --git-dir=C:/cs/CreatorStudio/.git status",
    "git --work-tree=C:/x status", "git --no-pager log", "git --exec-path=C:/x status",
    "cd C:/cs/CreatorStudio", "cd C:/cs/CreatorStudio && git commit -m x", "cd .. && git status",
    "cd ../.. ; git status", "pushd C:/elsewhere", "cd ~", "cd", "cd -", "cd $HOME", "cd C:", "cd C:x",
    "git worktree add ../../outside", "git worktree add D:/elsewhere", "git worktree add sub",
    "git worktree remove x", "git status -C C:/x",
    # B3: root pathspecs, abbreviations, commit/branch options
    "git add ./", "git add -- ./", "git add :", "git add :/", "git add .", "git add '*'", 'git add "*.py"',
    "git add *.py", "git add -A", "git add --all", "git add --al", "git add -u", "git add --update", "git add sub/..",
    "git add ../outside.py", "git add C:/cs/CreatorStudio/x.py", "git add ':!x'", "git add ':(top)'", "git add -f x",
    "git add --force x", "git add -p x", "git add",
    "git commit --no-veri -m x", "git commit --ame -m x", "git commit --amend -m x", "git commit --no-verify -m x",
    "git commit -n -m x", "git commit -a -m x", "git commit -am x", "git commit -m x .", "git commit",
    "git commit --fixup HEAD", "git commit -F C:/elsewhere/msg.txt", "git commit --mess x", "git commit -mx",
    "git commit --no-gpg-sign -m x", "git commit -m x --no-edit",
    "git branch --forc main HEAD~5", "git branch -f main", "git branch --force x", "git branch -D x", "git branch -d x",
    "git branch -m a b", "git branch -M a b", "git branch --delete x", "git branch --del x", "git branch -c a b",
    "git branch --set-upstream-to=origin/x", "git branch -u origin/x",
    "git checkout main", "git checkout -- x.py", "git checkout -B x", "git checkout x.py", "git checkout -b x -- y",
    "git switch -C x", "git switch --discard-changes x", "git switch --force x", "git switch --orphan x",
    "git stash", "git stash push", "git stash apply", "git stash pop", "git stash drop",
    "git config user.name x", "git config --get credential.helper", "git config --global --get user.name",
    "git remote add x y", "git remote set-url origin x", "git reset --hard", "git clean -fd", "git rebase main",
    "git merge main", "git push origin task/x", "git tag v1", "git init", "git clone x", "git apply x.patch",
    "git restore x.py", "git rm x.py", "git mv a b", "git cherry-pick abc", "git revert abc", "git am x",
    # B4: python -m subcommand allowlists
    "python -B -m governance wave close w01 --source DIRECT --date 2026-11-20 --approver x",
    "python -B -m governance wave init m.json", "python -B -m governance wave worktrees w01",
    "python -B -m governance wave verify w01", "python -B -m governance score --write",
    "python -B -m governance handoff --write", "python -B -m governance --root C:/x validate",
    "python -B -m governance scope", "python -B -m governance tasks", "python -B -m governance wave submit --yes",
    "python -B -m governance validate --against C:/x", "python -B -m governance",
    "python -B -m dev_supervisor start --yes", "python -B -m dev_supervisor assign --task x --yes",
    "python -B -m dev_supervisor replace --handoff-id x --yes", "python -B -m dev_supervisor report --yes",
    "python -B -m dev_supervisor config", "python -B -m dev_supervisor --config x status",
    "python -B -m dev_supervisor handoff --yes", "python -B -m dev_supervisor.authority approvals list",
    "python -B -m dev_supervisor.session_policy live-probe --yes", "python -B -m dev_supervisor.integrate 1 --yes",
    "python -B -m dev_supervisor snapshot --out C:/elsewhere/x.json", "python -B -m dev_supervisor snapshot --out .git/x",
    "python -B -m creator_state ingest x", "python -B -m creator_state --root C:/x list",
    "python -B -m creator_state incoming-inspect x --ffprobe C:/evil.exe", "python -B -m creator_state shots-detect s --yes",
    "python -B -m creator_state proxy-make s --yes", "python -B -m creator_state log C:/elsewhere/p",
    "python -B -m unittest discover -s C:/elsewhere", "python -B -m unittest C:/elsewhere/t.py",
    "python -m pip install x", "python -c print(1)", "python x.py", "python -X importtime -m unittest",
    # N1: shell reads outside the roots, secrets, outward publishing of local files
    "cat C:/Users/Korisnik/.claude.json", "head ~/.git-credentials", "cat C:/Users/Korisnik/.config/gh/hosts.yml",
    "grep -r . C:/", "find C:/ -name x", "cat ../outside.txt", "cat C:/elsewhere/a.txt", "type env:GH_TOKEN",
    "cat $HOME/x", 'cat "$HOME/x"', "ls C:/", "ls ..", "dir D:/", "wc -l ../x", "sort ../x", "diff a C:/x",
    "rg -n x C:/elsewhere", "sed -n 1p ../x", "tail C:/x", "cat .git-credentials", "cat sub/.claude.json",
    "gh pr comment 1 --body-file C:/Users/Korisnik/.claude.json", "gh pr comment 1 -F ../x.md",
    "gh issue create --title t --body-file C:/x", "gh pr view 1 --jq .body", "gh pr view 1 -q .body",
    "gh pr view 1 --template x", "gh api repos/x -H 'X-HTTP-Method-Override: DELETE'", "gh api repos/x --jq .",
    "gh pr create --web", "gh pr view 1 --web", "gh pr comment 1 --repo other/repo --body x", "gh api graphql",
    "gh pr merge 1", "gh pr close 1", "gh release create v1", "gh repo delete x", "gh api --hostname x repos/y",
    "gh pr view 1 --rep x/y",
    # shell expansion and other closed-world gaps
    "echo $GH_TOKEN", "ls @(1)", "ls (Get-Content x)", "git diff --stat HEAD~1", "cat x{,/../../etc}", "ls *", "ls ./*/../..",
    "cat .*", "grep -r x --include=x -f C:/x .", "find . -fprint C:/x", "find . -newer C:/x", "sed -n 1p -i x",
    "sed -n 1,5p;w/out x", "sed -n s/a/b/pe x", "sort -T C:/x a", "uniq a b", "mkdir .git/hooks", "mkdir ../x",
    "mkdir sub/.git", "rg -z x", "rg --pre=x y", "rg --hostname-bin x y", "head -f x", "tail -f x", "ls --hyperlink",
    "echo x > C:/cs/CreatorStudio/a", "git status --%", "git log --format=%h(%an)",
]

# Authoritative worker, cwd = its worktree: routine work must get an explicit allow.
WORKER_ALLOW = [
    "git status", "git status --short", "git status --porcelain=v1", "git diff", "git diff --stat origin/main",
    "git diff --cached", "git diff -- orchestrator/x.py", "git diff origin/main...HEAD --name-only", "git diff --stat",
    "git log --oneline -5", "git log --format=%H -n 3", "git log --oneline origin/main..HEAD",
    "git show HEAD:orchestrator/x.py", "git show --stat HEAD", "git add orchestrator/x.py tests/y.py",
    "git add -- orchestrator/x.py", "git add orchestrator", 'git commit -m "T-DEV-028: x" -m "Co-Authored-By: a"',
    "git commit -F msg.txt", "git commit --message=x", "git fetch", "git fetch origin", "git fetch --prune origin main",
    "git pull --ff-only origin task/x", "git branch", "git branch task/new", "git branch -a", "git branch --show-current",
    "git checkout -b task/new", "git checkout -b task/new origin/main", "git switch task/x",
    "git switch -c task/y origin/main", "git rev-parse --abbrev-ref HEAD", "git rev-parse --show-toplevel",
    "git merge-base HEAD origin/main", "git remote", "git remote -v", "git remote get-url origin",
    "git config --get user.email", "git stash list", "git stash show", "git worktree list", "git ls-files orchestrator",
    "gh pr view 98", "gh pr view 98 --json title,body", "gh pr list --state open", "gh pr checks 98",
    "gh pr diff 98 --name-only", "gh pr create --title t --body b --base main", "gh pr comment 98 --body-file notes.md",
    "gh pr comment 98 --body x", "gh issue view 44 --comments", "gh issue list --label x",
    "gh api repos/KasluBuilds/CreatorStudio/pulls", "gh api -X GET repos/x/y",
    'python -B -m unittest discover -s tests -p "test_dev_x.py"', "python -B -I -m unittest discover -s tests -p test_dev_x.py",
    "python -B -m unittest discover -s tests -p test_x.py",
    "python -B -m unittest tests.test_dev_authority -v", "python -B -m governance validate",
    "python -B -m governance validate --against origin/main", "python -B -m governance leases",
    "python -B -m governance handoff", "python -B -m governance handoff --check", "python -B -m governance wave submit",
    "python -B -m governance wave status", "python -B -m governance wave status w03 --json",
    "python -B -m dev_supervisor status --json", "python -B -m dev_supervisor workers", "python -B -m dev_supervisor doctor",
    "python -B -m dev_supervisor report", "python -B -m dev_supervisor compact-check",
    "python -B -m dev_supervisor snapshot --out snap.json", "python -B -m dev_supervisor route --criticality high",
    "python -B -m creator_state list", "python -B -m creator_state log demo -n 30", "python -B -m creator_state check demo",
    "python -B -m creator_state verify-sources demo --deep", "python -B -m creator_state --help",
    "ls", "ls -la", "ls orchestrator", "pwd", "cat orchestrator/x.py", "head -n 20 x.py", "tail -5 x.py",
    "wc -l x.py", 'grep -rn "def x" orchestrator', "grep -n TODO x.py", "rg -n pattern orchestrator",
    'find orchestrator -name "x.py"', "find . -type f -name x.py", "sort x.txt", "uniq x.txt", "diff a.txt b.txt",
    "echo hi", "mkdir -p build/out", "test -f x.py", "true", "printf x", "sed -n 1,80p x.py", "sed -n '1,5p' x.py",
    "rtk git status", "rtk git log -5", "rtk gh pr view 5", "rtk ls orchestrator", "rtk grep -n x x.py",
    "rtk find . -name x.py", "rtk wc -l x.py", "rtk diff a.txt b.txt", "git diff 2>&1", "git status --short 2>&1",
]


class ReviewRound1Shell(Base):
    def wenv(self, **extra):
        return self.env("authoritative", root=WT, roots=[WT], **extra)

    def test_worker_deny_table(self):
        for cmd in WORKER_DENY:
            with self.subTest(cmd=cmd):
                got, why = self.hook("Bash", {"command": cmd}, env=self.wenv(), cwd=WT)
                self.assertEqual(got, "deny", why)

    def test_worker_allow_table(self):
        for cmd in WORKER_ALLOW:
            with self.subTest(cmd=cmd):
                got, why = self.hook("Bash", {"command": cmd}, env=self.wenv(), cwd=WT)
                self.assertEqual(got, "allow", why)

    def test_powershell_tool_is_never_allowed_for_a_worker(self):
        for cmd in ("ls -Recurse orchestrator", "dir -Force", "cat x.py -Raw", "git status", "ls | sort {calc}",
                    "ls | sort {.\\x.cmd}", "ls | ls {calc}"):
            with self.subTest(cmd=cmd):
                got, why = self.hook("PowerShell", {"command": cmd}, env=self.wenv(), cwd=WT)
                self.assertEqual(got, "deny", why)
                self.assertIn("not on the worker routine allowlist", why)

    def test_cwd_outside_roots_denies_every_shell_command(self):
        for cwd in (ROOT, "C:/wt", "D:/x", ""):
            for cmd in ("git status", "ls", "git commit -m x", "pwd"):
                with self.subTest(cwd=cwd, cmd=cmd):
                    got, why = self.hook("Bash", {"command": cmd}, env=self.wenv(), cwd=cwd or "C:/")
                    self.assertEqual(got, "deny", why)
        got, _ = self.hook("Bash", {"command": "git status"}, env=self.wenv(), cwd=WT + "/orchestrator")
        self.assertEqual(got, "allow")

    def test_no_cd_at_all(self):
        ctx = au.PolicyContext(roots=(WT,), repo_root=WT)
        for cmd in ("cd orchestrator", "cd orchestrator && cat x.py", "pushd orchestrator", "popd", "cd"):
            with self.subTest(cmd=cmd):
                self.assertFalse(au.routine_shell(cmd, ctx, WT)[0])

    def test_advisory_deny_list_catches_abbreviations_and_root_pathspecs(self):
        for cmd in ("git commit --no-veri -m x", "git commit --no-gpg -m x", "git commit -nm x", "git add ./",
                    "git add :", "git add :/", "git add ':!x'", "git add sub/..", "git add --al", "git add -A",
                    "git push --forc origin x", "git push --mirr origin", "git push --del origin x", "git push -fu origin x",
                    "git push --al origin"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.hook("Bash", {"command": cmd}, "advisory")[0], "deny")
        for cmd in ("git add orchestrator/x.py", "git commit -m x", "git push origin task/x",
                    "git add orchestrator/dev_supervisor/authority.py"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.hook("Bash", {"command": cmd}, "advisory")[0])


class ReviewRound1Boundary(Base):
    """B5: the live authority code, the hook script and the user settings are the authority boundary."""

    def benv(self, mode, **extra):
        return self.env(mode, root=LIVE_ROOT, roots=[LIVE_ROOT],
                        CS_AUTHORITY_BOUNDARY_PATHS=os.pathsep.join(["C:/cs/user/settings-user.json"]), **extra)

    def test_writes_to_boundary_files_are_authority_change_in_both_modes(self):
        targets = [LIVE, LIVE_SP, LIVE.upper(), LIVE.replace("/", "\\"), "C:/cs/user/settings-user.json",
                   LIVE_ROOT + "/.claude/settings.json", LIVE_ROOT + "/.claude/settings.local.json",
                   LIVE_ROOT + "/orchestrator/dev_supervisor/../dev_supervisor/authority.py"]
        for mode in ("advisory", "authoritative"):
            for tool in ("Write", "Edit", "MultiEdit"):
                for t in targets:
                    with self.subTest(mode=mode, tool=tool, t=t):
                        got, why = self.hook(tool, {"file_path": t, "content": "x"}, env=self.benv(mode), cwd=LIVE_ROOT)
                        self.assertEqual(got, "deny")
                        self.assertIn("authority-boundary", why)
                        self.assertNotIn("queued", why)

    def test_shell_commands_naming_boundary_files_are_denied_in_both_modes(self):
        rel = "orchestrator/dev_supervisor/authority.py"
        for mode in ("advisory", "authoritative"):
            for cmd in (f"sed -i s/a/b/ {LIVE}", f"cat {LIVE}", f"sed -n 1,5p {rel}", f"git add {rel}",
                        f"cp x {LIVE_SP}", "cat C:/cs/user/settings-user.json", f"echo x >> {LIVE.replace('/', chr(92))}"):
                with self.subTest(mode=mode, cmd=cmd):
                    got, why = self.hook("Bash", {"command": cmd}, env=self.benv(mode), cwd=LIVE_ROOT)
                    self.assertEqual(got, "deny")
                    self.assertIn("authority-boundary", why)

    def test_reading_boundary_files_with_read_tool_stays_allowed(self):
        self.assertIsNone(self.hook("Read", {"file_path": LIVE}, env=self.benv("advisory"), cwd=LIVE_ROOT)[0])
        self.assertEqual(self.hook("Read", {"file_path": LIVE}, env=self.benv("authoritative"), cwd=LIVE_ROOT)[0],
                         "allow")

    def test_install_puts_hook_script_and_user_settings_into_the_boundary_env(self):
        user = self.tmp / "u.json"
        inst = sp.install(self.tmp / "state", SID, "worker", WT, run=_fake_run, user_settings=user)
        paths = inst.env[au.BOUNDARY_ENV].split(os.pathsep)
        self.assertIn(sp._posix(os.path.abspath(au.__file__)), paths)
        self.assertIn(sp._posix(os.path.abspath(sp.__file__)), paths)
        self.assertIn(sp._posix(str(user)), paths)


class ReviewRound1Tools(Base):
    def test_read_tool_gaps(self):
        cases = [
            ("Grep", {"pattern": "x", "glob": ".env*"}, "deny", "deny"),
            ("Grep", {"pattern": "x", "glob": "**/.env"}, "deny", "deny"),
            ("Grep", {"pattern": "x", "glob": "*.pem"}, "deny", "deny"),
            ("Grep", {"pattern": "x", "glob": "{*.py,.env}"}, "deny", "deny"),
            ("Grep", {"pattern": "x", "glob": ".git-credentials"}, "deny", "deny"),
            ("Grep", {"pattern": "x", "glob": "C:/elsewhere/**"}, "deny", "deny"),
            ("Grep", {"pattern": "x", "glob": "../**/*.py"}, "deny", "deny"),
            ("Grep", {"pattern": "x", "glob": "*.py"}, None, "allow"),
            ("Grep", {"pattern": "x", "glob": "*.json"}, None, "allow"),
            ("Glob", {"pattern": "C:/Users/Korisnik/**/*.json"}, "deny", "deny"),
            ("Glob", {"pattern": "C:/*"}, "deny", "deny"),
            ("Glob", {"pattern": "/**/x"}, "deny", "deny"),
            ("Glob", {"pattern": "../**/x"}, "deny", "deny"),
            ("Glob", {"pattern": "~/**"}, "deny", "deny"),
            ("Glob", {"pattern": "D:**"}, "deny", "deny"),
            ("Glob", {"pattern": ROOT + "/**/*.py"}, None, "allow"),
            ("Glob", {"pattern": "**/*.py"}, None, "allow"),
            ("Read", {}, "deny", "deny"),
            ("Read", {"file_path": ""}, "deny", "deny"),
            ("NotebookRead", {}, "deny", "deny"),
            ("Read", {"file_path": ROOT + "/.git-credentials"}, "deny", "deny"),
            ("Read", {"file_path": "C:/Users/x/.claude.json"}, "deny", "deny"),
            ("Read", {"file_path": ROOT + "/gh/hosts.yml"}, "deny", "deny"),
            # N3: drive-relative and provider paths
            ("Write", {"file_path": "C:x.py", "content": "x"}, "deny", "deny"),
            ("Read", {"file_path": "C:x.py"}, "deny", "deny"),
            ("Write", {"file_path": "env:X", "content": "x"}, "deny", "deny"),
            ("Write", {"file_path": "~/x.py", "content": "x"}, "deny", "deny"),
            # .git internals are never written (a crafted git config executes commands)
            ("Write", {"file_path": ROOT + "/.git/config", "content": "x"}, "deny", "deny"),
            ("Write", {"file_path": ROOT + "/sub/.git/config", "content": "x"}, "deny", "deny"),
            ("Edit", {"file_path": ROOT + "/.GIT/hooks/pre-commit"}, "deny", "deny"),
            ("Write", {"file_path": ROOT + "/.gitignore", "content": "x"}, None, "allow"),
        ]
        for tool, tin, adv, auth in cases:
            for mode, want in (("advisory", adv), ("authoritative", auth)):
                with self.subTest(tool=tool, tin=tin, mode=mode):
                    got, why = self.hook(tool, tin, mode)
                    self.assertEqual(got, want, why)

    def test_resolve_is_classified_by_name(self):
        self.assertTrue(au.resolve_name_is_read("mcp__davinci-resolve__list_timelines"))
        self.assertFalse(au.resolve_name_is_read("mcp__davinci-resolve__delete_timeline"))
        self.assertFalse(au.resolve_input_dependent("mcp__davinci-resolve__delete_timeline", {"action": "list"}))
        self.assertTrue(au.resolve_input_dependent("mcp__davinci-resolve__timeline", {"action": "list"}))
        self.assertEqual(au.classify_tool_call("mcp__davinci-resolve__delete_timeline", {"action": "list"})[0].kind,
                         au.RESOLVE_MUTATION)


class ReviewRound1Settings(Base):
    def project(self, name, data):
        d = self.tmp / "proj" / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return str(self.tmp / "proj")

    def test_install_refuses_project_settings_with_hooks_or_disable(self):
        for name, data in (("settings.json", {"hooks": {"PreToolUse": []}, "x": 1}),
                           ("settings.json", {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": []}]}}),
                           ("settings.local.json", {"disableAllHooks": True}),
                           ("settings.local.json", {"disableAllHooks": False}),
                           ("settings.json", "{not json"), ("settings.local.json", "[1]")):
            with self.subTest(name=name, data=data):
                td = tempfile.TemporaryDirectory()
                self.addCleanup(td.cleanup)
                self.tmp = Path(td.name)
                proj = self.project(name, data)
                with self.assertRaises(sp.PolicyInstallError):
                    sp.install(self.tmp / "state", SID, "worker", proj, run=_fake_run, user_settings=self.tmp / "n.json")

    def test_benign_project_settings_are_accepted_and_rechecked_at_launch(self):
        proj = self.project("settings.local.json", {"enableAllProjectMcpServers": True})
        inst = sp.install(self.tmp / "state", SID, "main", proj, run=_fake_run, user_settings=self.tmp / "n.json")
        self.assertEqual(inst.project_dir, proj)
        sp.argv_with_policy(["claude"], inst)
        self.project("settings.json", {"disableAllHooks": True})       # appears after install
        with self.assertRaises(sp.PolicyInstallError):
            sp.argv_with_policy(["claude"], inst)

    def test_rtk_detected_by_executable_name(self):
        for cmd in ("rtk hook claude", "rtk.exe hook claude", '"C:/tools/rtk.exe" hook claude', "RTK hook claude",
                    "C:\\bin\\rtk hook claude", "'/usr/bin/rtk' hook claude", "  Rtk.EXE hook"):
            with self.subTest(cmd=cmd):
                self.assertTrue(sp._is_rtk({"type": "command", "command": cmd}))
        for cmd in ("rtkx hook", "node rtk.js hook", "python -m rtk hook", "powershell -File guard.ps1", ""):
            with self.subTest(cmd=cmd):
                self.assertFalse(sp._is_rtk({"type": "command", "command": cmd}))
        hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": '"C:/x/rtk.exe" hook claude'},
                                                             {"type": "command", "command": "keep.ps1"}]}]}
        self.assertEqual(sp._strip_rtk(hooks)["PreToolUse"][0]["hooks"], [{"type": "command", "command": "keep.ps1"}])



# ---------------------------------------------------------------- security review round 2 (B1-B3, N1-N3, N5)
class ReviewRound2(Base):
    """The worker shell rule is STRUCTURAL: one simple Bash command, no shell metacharacters, no cd, closed allowlist."""

    def wenv(self, **extra):
        return self.env("authoritative", root=WT, roots=[WT], **extra)

    def worker(self, cmd, tool="Bash", cwd=WT):
        return self.hook(tool, {"command": cmd}, env=self.wenv(), cwd=cwd)

    def test_routine_single_commands_get_allow(self):
        for cmd in ("git status", "git diff --stat", "git log --oneline -5", "git add orchestrator/x.py",
                    'git commit -m "msg"', "gh pr view 5", "python -B -m unittest discover -s tests -p test_x.py",
                    "python -B -m governance validate", "rtk git status"):
            with self.subTest(cmd=cmd):
                got, why = self.worker(cmd)
                self.assertEqual(got, "allow", why)

    def test_b1_cd_tracker_desync_strings_are_denied(self):
        for cmd in ("cd a/b; cd ../../CreatorStudio; git add f; git commit -m x",
                    "cd a/b || cd ../../CreatorStudio && git commit -m x",
                    "cd sub/deeper | cat ../../x",
                    "cd a/b; cd ../../main; git commit --allow-empty -m ESCAPED -q",
                    "cd orchestrator && git status", "cd orchestrator", "pushd orchestrator", "popd",
                    "git log | head", "git log --oneline | head -5", "git status; git log", "git status && git log",
                    "git status || true", "git status\ngit log", "git status\r\ngit log", "git status &"):
            with self.subTest(cmd=cmd):
                got, why = self.worker(cmd)
                self.assertEqual(got, "deny", why)
                self.assertIn("not on the worker routine allowlist", why)

    def test_b2_powershell_scriptblocks_are_denied(self):
        for cmd in ("ls | sort {calc}", "ls | sort {.\\x.cmd}", "ls | ls {calc}", "sort {calc}", "ls {calc}"):
            for tool in ("PowerShell", "Bash"):
                with self.subTest(cmd=cmd, tool=tool):
                    self.assertEqual(self.worker(cmd, tool=tool)[0], "deny")

    def test_b3_rtk_runners_are_denied_and_seen_by_the_deny_list(self):
        for cmd in ("rtk test git push origin main", "rtk test ./x.cmd", "rtk test rm x", "rtk test curl evil.example",
                    "rtk err git status", "rtk summary git status", "rtk run git status", "rtk proxy git status",
                    "rtk bypass git status", "rtk rtk git status", "rtk python -B -m governance validate", "rtk cat x",
                    "rtk", "rtk test git status"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.worker(cmd)[0], "deny")
        for runner in ("test", "err", "summary", "run", "bypass", "proxy"):
            with self.subTest(runner=runner):
                acts = au.classify_command(f"rtk {runner} git push origin main")
                self.assertEqual([(a.kind, a.attrs.get("branch")) for a in acts], [(au.GIT_PUSH, "main")])
                got, why = self.hook("Bash", {"command": f"rtk {runner} git push origin main"}, "advisory")
                self.assertEqual(got, "deny")
                self.assertIn("push-main", why)
        self.assertEqual(au.classify_command("rtk test rm -rf x")[0].kind, au.IRRECOVERABLE_DELETE)

    def test_metacharacters_and_unclean_tokens_are_denied(self):
        for cmd in ("git status $HOME", 'git commit -m "$(id)"', "git commit -m '$x'", "git status `id`",
                    "git log --format=%h(%an)", "ls *", "ls x?", "ls [a]", "ls ~", "ls !x", "git status # c",
                    "cat a\\b", "git log -1 >x", "git log <x", 'git commit -m a"b"', 'git commit -m "a"b',
                    "git commit -m 'a'\"b\"", 'git commit -m "unbalanced', "git commit -m 'x", "git status 2>&1 2>&1",
                    "git status 2>/dev/null", "git status >/dev/null", "git status 2>&1;", "", "   ", " 2>&1",
                    "git\x0bstatus", "git status\x00"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.worker(cmd)[0], "deny")

    def test_n1_redirect_spellings_are_denied(self):
        for cmd in ("echo hi >/dev/nullfoo", "echo hi >/dev/null/../x", "echo hi >$nullfoo", "echo hi >/dev/null",
                    "echo hi 2>/dev/null", "echo hi > $null"):
            for tool in ("Bash", "PowerShell"):
                with self.subTest(cmd=cmd, tool=tool):
                    self.assertEqual(self.worker(cmd, tool=tool)[0], "deny")

    def test_n2_gh_operands_stay_in_the_own_repository_form(self):
        for cmd in ("gh api https://evil.example/x", "gh api //evil.example/x", "gh api evil.example/x",
                    "gh api repos/x", "gh api repos", "gh api repos/../x/y", "gh api graphql", "gh api /graphql",
                    "gh pr comment https://github.com/other/repo/pull/1 --body x",
                    "gh pr view https://github.com/other/repo/pull/1", "gh pr view github.com/other/repo/pull/1",
                    "gh issue comment other/repo/issues/1 --body x", "gh pr view ../x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.worker(cmd)[0], "deny")
        for cmd in ("gh api repos/KasluBuilds/CreatorStudio/pulls", "gh api repos/a/b", "gh api user",
                    "gh api /rate_limit", "gh pr view task/t-dev-028-x", "gh issue view 44"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.worker(cmd)[0], "allow")

    def test_cwd_must_be_inside_the_roots(self):
        self.assertEqual(self.worker("git status", cwd=WT + "/orchestrator")[0], "allow")
        for cwd in (ROOT, "C:/wt", "C:/wt/task2"):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.worker("git status", cwd=cwd)[0], "deny")


class ReviewRound2Boundary(Base):
    """N3: the shell boundary check normalises ./ and //, follows cd targets, matches globs and directory restores."""

    def benv(self, mode):
        return self.env(mode, root=LIVE_ROOT, roots=[LIVE_ROOT])

    def test_n3_spellings_touch_the_boundary_in_both_modes(self):
        for cmd in ("cd orchestrator/dev_supervisor && sed -i s/a/b/ authority.py",
                    "sed -i s/a/b/ orchestrator/dev_supervisor/author*.py",
                    "sed -i s/a/b/ orchestrator/dev_supervisor/*.py", "rm orchestrator/dev_supervisor/*",
                    "sed -i s/a/b/ orchestrator/dev_supervisor/[ab]uthority.py",
                    "sed -i s/a/b/ orchestrator/dev_supervisor/session_pol?cy.py",
                    "sed -i s/a/b/ orchestrator/**/authority.py",
                    "cp x orchestrator/dev_supervisor/{authority,x}.py",
                    "cp x orchestrator/dev_supervisor/./authority.py", "cp x orchestrator//dev_supervisor/authority.py",
                    "cp x ./orchestrator/./dev_supervisor//session_policy.py",
                    "git checkout HEAD~1 -- orchestrator/dev_supervisor/", "git checkout HEAD~1 -- orchestrator/dev_supervisor",
                    "git restore --source HEAD~1 orchestrator/dev_supervisor/", "git checkout -- orchestrator",
                    "git restore ."):
            for mode in ("advisory", "authoritative"):
                with self.subTest(cmd=cmd, mode=mode):
                    got, why = self.hook("Bash", {"command": cmd}, env=self.benv(mode), cwd=LIVE_ROOT)
                    self.assertEqual(got, "deny")
                    self.assertIn("authority-boundary", why)

    def test_unrelated_commands_are_not_boundary_touches(self):
        ctx = au.PolicyContext(roots=(LIVE_ROOT,), repo_root=LIVE_ROOT)
        for cmd in ("ls *", "ls orchestrator/*.py", "git checkout main", "git checkout -b task/x",
                    "cat orchestrator/tests/test_dev_authority.py", "git add orchestrator/dev_supervisor/x.py",
                    "git restore orchestrator/tests/x.py", "sed -n 1p orchestrator/dev_supervisor/*.md"):
            with self.subTest(cmd=cmd):
                self.assertFalse(au.touches_boundary_command(cmd, ctx, LIVE_ROOT))

if __name__ == "__main__":
    unittest.main()
