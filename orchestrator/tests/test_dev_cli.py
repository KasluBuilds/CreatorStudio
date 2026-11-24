"""Development supervisor CLI, interactive Main launcher and machine-local config (T-DEV-008, wave w03 lane L8).

Offline only: synthetic data in temp directories, a fake terminal, a fake worker launcher, fake checkers, fake
command runner and fake transports. Sockets and real process creation are patched to fail, so nothing touches the
real machine, the network, GitHub, Claude or Resolve.
Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_cli.py -v
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import cli, compaction, dispatch_routing, handoff, launch, replacement, session_policy  # noqa: E402
from dev_supervisor import config as cfgmod  # noqa: E402
from dev_supervisor.lifecycle import LaunchHandle, LaunchSpec, build_claude_argv  # noqa: E402
from dev_supervisor.omniroute import TransportError  # noqa: E402
from dev_supervisor.state_store import StateStore, this_repository_root  # noqa: E402
from dev_supervisor.telemetry import cwd_slug  # noqa: E402

# Credential-shaped strings are assembled at runtime so no literal credential appears in the repository.
FAKE_KEY = "sk-" + "ant-" + "x" * 24
FAKE_GH_TOKEN = "gh" + "o_" + "B" * 30
TEST_MODEL = "claude-test-model"


def _boom(*a, **k):
    raise AssertionError("real network or process access attempted in an offline test")


# ------------------------------------------------------------------ fakes

class FakeHandle:
    def __init__(self, pid):
        self.pid = pid
        self.code = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 1

    def kill(self):
        self.killed = True
        self.code = -9


class FakeTerminal:
    """Records argv; simulates claude starting by writing its Claude Code session file (idle)."""

    def __init__(self, env):
        self.env = env
        self.opened = []
        self.handles = {}
        self.write_session_file = True

    def open(self, argv, cwd, env=None):
        pid = self.env.next_pid()
        self.opened.append({"argv": list(argv), "cwd": cwd, "pid": pid, "env": dict(env or {})})
        h = FakeHandle(pid)
        self.handles[pid] = h
        if self.write_session_file:
            sid = argv[argv.index("--session-id") + 1]
            self.env.write_session(pid, sid, status="idle")
        return h


class FakeWorkerLauncher:
    def __init__(self, env):
        self.env = env
        self.specs = []
        self.procs = {}

    def launch(self, spec):
        pid = self.env.next_pid()
        self.specs.append(spec)
        self.procs[pid] = None
        return LaunchHandle(pid=pid)

    def owns(self, pid):
        return pid in self.procs

    def poll(self, pid):
        return self.procs[pid]

    def terminate(self, pid):
        self.procs[pid] = 0

    def kill(self, pid):
        self.procs[pid] = -9


class FakeCheckers:
    def __init__(self, ok=True):
        self.ok = ok

    def branch(self, name):
        return self.ok, "fake"

    def worktree(self, path, branch):
        return self.ok, "fake"

    def commit(self, sha):
        return self.ok, "fake"

    def pull_request(self, number):
        return self.ok, "fake"


class FakeGhRunner:
    def __init__(self):
        self.comments = []

    def comment(self, issue, body):
        self.comments.append((issue, body))

    def create_issue(self, title, body, labels=()):
        raise AssertionError("not used")

    def close_issue(self, issue, comment=""):
        raise AssertionError("not used")

    def event_ids(self, issue):
        return set()


_DENY = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}'
_ALLOW = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}'


def fake_policy_install(*args, **kw):
    """The real installer with the hook subprocess faked (writes the settings file, no process started). The user
    settings path points nowhere so the test never reads the real ~/.claude/settings.json."""
    def run(cmd, **k):
        stdin, env = k.get("input", ""), k.get("env") or {}
        if "git status" in stdin:
            out = _ALLOW if env.get("CS_AUTHORITY_MODE", "advisory") != "advisory" else ""
        else:
            out = _DENY
        return mock.Mock(returncode=0, stdout=out)
    kw.setdefault("user_settings", os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-such-settings.json"))
    return session_policy.install(*args, run=run, **kw)


GATEWAY_ENV = {"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "gateway-test-token", "PATH": "p"}


class Env:
    """Temp directories plus a Services object built only from fakes."""

    def __init__(self, tc, **cfg_over):
        self.tc = tc
        self.tmp = Path(tempfile.mkdtemp(prefix="l8-cli-"))
        tc.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.state = self.tmp / "state"
        self.sessions = self.tmp / "claude" / "sessions"
        self.projects = self.tmp / "claude" / "projects"
        self.wt = self.tmp / "worktrees"
        for d in (self.repo, self.sessions, self.projects, self.wt):
            d.mkdir(parents=True)
        self.pid = 4000
        self.alive = set()
        self.commands = []
        self.run_results = {}
        data = {"state_dir": str(self.state), "repo_root": str(self.repo), "sessions_dir": str(self.sessions),
                "projects_dir": str(self.projects), "models": {"main": "opus", "worker": "sonnet"},
                "context_windows": {TEST_MODEL: 200000}, "poll_interval_s": 1}
        data.update(cfg_over)
        self.cfg = cfgmod.from_dict(data, source="test")
        self.terminal = FakeTerminal(self)
        self.worker_launcher = FakeWorkerLauncher(self)
        self.checkers = FakeCheckers()
        self.gh = FakeGhRunner()
        self.out = io.StringIO()
        self.transport_calls = []
        self.svc = cli.Services(
            config=self.cfg, terminal=self.terminal, worker_launcher_factory=lambda: self.worker_launcher,
            worktree_checker=lambda wt, br: (True, "ok"), ref_checkers_factory=lambda c, r: self.checkers,
            run=self.run, which=lambda name: "/fake/bin/" + name, omniroute_transport=self.transport,
            resolve_discover=lambda identity: {"verdict": "UNCONFIGURED"}, gh_runner_factory=lambda c: self.gh,
            pid_alive=lambda pid: pid in self.alive, sleep=lambda s: None, out=self.out, env={},
            policy_install=fake_policy_install)

    # -- helpers
    def next_pid(self):
        self.pid += 1
        self.alive.add(self.pid)
        return self.pid

    def run(self, argv, timeout=30.0, cwd=None):
        self.commands.append(list(argv))
        key = " ".join(argv[1:]) if argv[0].startswith("/fake/bin/") else " ".join(argv)
        for k, v in self.run_results.items():
            if k in key:
                return v
        if "--version" in argv:
            return 0, "9.9.9 (fake)\n", ""
        if argv[-2:] == ["auth", "status"]:
            return 0, "", "github.com\n  Logged in to github.com account fake-user (keyring)\n  - Token: " + \
                FAKE_GH_TOKEN[:4] + "****\n"
        if "status" in argv and "--porcelain" in argv:
            return 0, "", ""
        if "worktree" in argv:
            return 0, "worktree /r\nHEAD abc\nbranch refs/heads/main\n", ""
        return 0, "", ""

    def transport(self, method, url, headers, body, timeout):
        self.transport_calls.append((method, url, dict(headers)))
        return 200, json.dumps({"data": [{"id": "gemini/a"}, {"id": "groq/b"}]}).encode()

    def write_session(self, pid, sid, status="idle"):
        now_ms = int(time.time() * 1000)
        (self.sessions / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": sid, "cwd": str(self.repo), "status": status, "statusUpdatedAt": now_ms,
             "startedAt": now_ms}), encoding="utf-8")

    def write_transcript(self, sid, tokens):
        d = self.projects / cwd_slug(str(self.repo))
        d.mkdir(parents=True, exist_ok=True)
        rec = {"type": "assistant", "message": {"role": "assistant", "model": TEST_MODEL, "content": [],
                                                 "usage": {"input_tokens": tokens, "cache_read_input_tokens": 0,
                                                           "cache_creation_input_tokens": 0}}}
        (d / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    def cli(self, *argv):
        self.out.seek(0)
        self.out.truncate()
        code = cli.main(list(argv), services=self.svc)
        return code, self.out.getvalue()

    def store(self):
        return StateStore(self.state)

    def facts(self, **over):
        f = {"objective": "Integrate the supervisor lanes", "decisions": ["Main is interactive"],
             "plan": ["finish L8"], "branches": ["lane/w03-dev-supervisor/L8"],
             "commits": [{"sha": "0123456789abcdef", "branch": "lane/w03-dev-supervisor/L8", "subject": "wip"}],
             "tests": [{"command": "unittest", "result": "OK", "label": "OBSERVED"}],
             "next_actions": ["submit the lane"]}
        f.update(over)
        p = self.tmp / f"facts-{uuid.uuid4().hex[:6]}.json"
        p.write_text(json.dumps(f), encoding="utf-8")
        return str(p)

    def verified_route(self, model="auto/coding", provider="gemini", cost_zero=True):
        """Record one verified zero-cost route in the state dir and give the services a gateway-only environment."""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "verified_routes.json").write_text(json.dumps({"routes": [
            {"model": model, "provider": provider, "evidence": "test:synthetic", "cost_zero": cost_zero}]}),
            encoding="utf-8")
        self.svc.env = dict(GATEWAY_ENV)

    def prompt_file(self):
        p = self.tmp / f"task-{uuid.uuid4().hex[:6]}.txt"
        p.write_text("synthetic task prompt\n", encoding="utf-8")
        return str(p)


class Offline(unittest.TestCase):
    def setUp(self):
        for target, name in ((socket, "socket"), (socket, "create_connection"), (subprocess, "Popen"),
                             (subprocess, "run")):
            p = mock.patch.object(target, name, _boom)
            p.start()
            self.addCleanup(p.stop)


# ------------------------------------------------------------------ config

class ConfigTests(Offline):
    def test_defaults_when_file_missing(self):
        tmp = tempfile.mkdtemp(prefix="l8-cfg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = cfgmod.load(env={cfgmod.ENV_CONFIG: os.path.join(tmp, "absent.json")})
        self.assertFalse(cfg.allow_paid_api)
        self.assertFalse(cfg.dangerously_skip_permissions)
        self.assertEqual(cfg.source, "defaults")
        self.assertEqual(cfg.model("main"), "opus")

    def test_load_from_machine_local_file(self):
        tmp = tempfile.mkdtemp(prefix="l8-cfg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "config.json")
        Path(p).write_text(json.dumps({"github_repo": "owner/repo", "github_issue": 52,
                                       "models": {"main": "fable"}, "thresholds": {"warn": 0.5}}), encoding="utf-8")
        cfg = cfgmod.load(p)
        self.assertEqual(cfg.github_issue, 52)
        self.assertEqual(cfg.model("main"), "fable")
        self.assertEqual(cfg.model("worker"), "sonnet")          # defaults merged
        self.assertEqual(cfg.thresholds["compact"], 0.80)
        self.assertEqual(cfg.source, p)

    def test_explicit_missing_file_is_an_error(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load(os.path.join(tempfile.gettempdir(), f"absent-{uuid.uuid4().hex}.json"))

    def test_credential_key_refused(self):
        with self.assertRaises(cfgmod.ConfigError) as cm:
            cfgmod.from_dict({"api_key": "anything"})
        self.assertNotIn("anything", str(cm.exception))

    def test_credential_value_refused(self):
        with self.assertRaises(cfgmod.ConfigError) as cm:
            cfgmod.from_dict({"github_repo": FAKE_KEY})
        self.assertNotIn(FAKE_KEY, str(cm.exception))

    def test_unknown_key_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({"launcher": "something"})

    def test_bypass_permissions_needs_explicit_dangerous_flag(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({"main_permission_mode": "bypassPermissions"})
        cfg = cfgmod.from_dict({"main_permission_mode": "bypassPermissions", "dangerously_skip_permissions": True})
        self.assertTrue(cfg.dangerously_skip_permissions)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({"worker_permission_mode": "yolo"})

    def test_thresholds_must_be_ordered(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({"thresholds": {"warn": 0.9, "compact": 0.8, "replace": 0.95}})

    def test_config_file_inside_repository_refused(self):
        root = this_repository_root()
        self.assertIsNotNone(root)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load(str(root / "not-a-real-dir" / "config.json"))

    def test_example_has_placeholders_only(self):
        ex = cfgmod.example()
        blob = json.dumps(ex)
        self.assertNotRegex(blob, r"[A-Za-z]:\\\\")             # no Windows absolute path
        self.assertNotIn("/Users/", blob)
        self.assertFalse(ex["allow_paid_api"])
        self.assertFalse(ex["dangerously_skip_permissions"])
        self.assertIn("<owner>/<repository>", blob)
        self.assertEqual(set(ex), set(cfgmod.Config().to_dict()))

    def test_cli_config_example_prints_template(self):
        env = Env(self)
        code, out = env.cli("config", "--example")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), json.loads(json.dumps(cfgmod.example())))


# ------------------------------------------------------------------ launch

class LaunchTests(Offline):
    SID = "12345678-1234-4234-8234-123456789abc"

    def test_main_argv_is_interactive_with_prompt_file(self):
        argv = launch.build_main_argv(launch.MainProfile(model="opus"), self.SID, "PROMPT")
        self.assertNotIn("-p", argv)
        self.assertNotIn("--print", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], self.SID)
        self.assertEqual(argv[argv.index("--append-system-prompt-file") + 1], "PROMPT")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--permission-mode", argv)
        self.assertFalse(argv[-1].startswith("-"))               # the initial instruction

    def test_dangerous_flag_only_when_configured(self):
        cfg = cfgmod.from_dict({"main_permission_mode": "acceptEdits"})
        argv = launch.build_main_argv(launch.MainProfile.from_config(cfg), self.SID, "P")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        cfg = cfgmod.from_dict({"dangerously_skip_permissions": True})
        self.assertIn("--dangerously-skip-permissions",
                      launch.build_main_argv(launch.MainProfile.from_config(cfg), self.SID, "P"))
        with self.assertRaises(launch.LaunchError):
            launch.build_main_argv(launch.MainProfile(model="opus", permission_mode="bypassPermissions"),
                                   self.SID, "P")

    def test_unsafe_inputs_refused(self):
        with self.assertRaises(launch.LaunchError):
            launch.build_main_argv(launch.MainProfile(model="opus; rm"), self.SID, "P")
        with self.assertRaises(launch.LaunchError):
            launch.build_main_argv(launch.MainProfile(model="opus"), "not-a-uuid", "P")
        with self.assertRaises(launch.LaunchError):
            launch.build_main_argv(launch.MainProfile(model="opus", initial_prompt="--resume x"), self.SID, "P")

    def test_main_prompt_file_carries_verified_handoff(self):
        env = Env(self)
        store = env.store()
        h = handoff.build_handoff(session_id=self.SID, role="main", objective="synthetic objective")
        rec = handoff.write_handoff(store, h)
        rep = handoff.verify_handoff(store, rec.handoff_id, FakeCheckers())
        self.assertTrue(rep.safe)
        p = launch.write_main_prompt(store, self.SID, verified=rep.verified)
        self.assertTrue(str(p).startswith(str(env.state)))
        text = p.read_text(encoding="utf-8")
        self.assertIn(rec.content_hash, text)
        self.assertIn(rec.handoff_id, text)
        self.assertIn("does NOT replenish", text)

    def test_new_console_terminal_uses_argument_list_and_new_console(self):
        calls = []

        def popen(argv, **kw):
            calls.append((argv, kw))
            return FakeHandle(77)

        t = launch.NewConsoleTerminal(which=lambda n: "C:/bin/claude.exe", popen=popen, os_name="nt")
        h = t.open(["claude", "--model", "opus", "two words"], cwd="REPO")
        self.assertEqual(h.pid, 77)
        argv, kw = calls[0]
        self.assertEqual(argv[1:], ["--model", "opus", "two words"])
        self.assertFalse(kw["shell"])
        self.assertEqual(kw["cwd"], "REPO")
        self.assertEqual(kw["creationflags"], getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))

    def test_new_console_terminal_refuses_cmd_shim_and_non_windows(self):
        t = launch.NewConsoleTerminal(which=lambda n: "C:/bin/claude.cmd", popen=_boom, os_name="nt")
        with self.assertRaises(launch.LaunchError):
            t.open(["claude"], cwd=".")
        t = launch.NewConsoleTerminal(which=lambda n: "/usr/bin/claude", popen=_boom, os_name="posix")
        with self.assertRaises(launch.LaunchError):
            t.open(["claude"], cwd=".")

    def test_interactive_launcher_turns_headless_spec_into_interactive_main(self):
        env = Env(self)
        store = env.store()
        resume = env.tmp / "resume.txt"
        resume.write_text("read handoff h-000000000000 hash sha256:abc\n", encoding="utf-8")
        ml = launch.InteractiveMainLauncher(store, env.terminal, launch.MainProfile(model="opus"), str(env.repo),
                                            policy_install=fake_policy_install)
        spec = LaunchSpec(argv=build_claude_argv("sonnet", self.SID), cwd=str(env.repo), stdin_path=str(resume),
                          stdout_path="o", stderr_path="e", max_runtime_s=10)
        pid = ml.launch(spec).pid
        opened = env.terminal.opened[-1]
        self.assertNotIn("-p", opened["argv"])
        self.assertEqual(opened["argv"][opened["argv"].index("--model") + 1], "sonnet")
        self.assertEqual(opened["cwd"], str(env.repo))
        pf = opened["argv"][opened["argv"].index("--append-system-prompt-file") + 1]
        self.assertIn("h-000000000000", Path(pf).read_text(encoding="utf-8"))
        self.assertTrue(ml.owns(pid))
        self.assertIsNone(ml.poll(pid))
        ml.terminate(pid)
        self.assertEqual(ml.poll(pid), 1)

    def test_heartbeat_idle_and_retirer(self):
        env = Env(self)
        store = env.store()
        ml = launch.InteractiveMainLauncher(store, env.terminal, launch.MainProfile(model="opus"), str(env.repo),
                                            policy_install=fake_policy_install)
        pid, _argv, _p = ml.open_main(self.SID)
        from dev_supervisor import telemetry
        hb = launch.SessionFileHeartbeat(env.sessions, telemetry.read_sessions, lambda p: p in env.alive)
        self.assertIsNotNone(hb.first_heartbeat(self.SID, pid))
        self.assertIsNone(hb.first_heartbeat(str(uuid.uuid4()), pid))
        sess = launch.find_session(telemetry.read_sessions(env.sessions, pid_alive=lambda p: True), self.SID)
        self.assertIs(launch.idle_evidence(sess).value, True)
        self.assertFalse(launch.idle_evidence(None).available)
        retire = launch.OwnedMainRetirer(ml, lambda sid: pid if sid == self.SID else None)
        self.assertEqual(retire.retire("other")[0], False)
        self.assertEqual(retire.retire(self.SID)[0], True)
        self.assertTrue(env.terminal.handles[pid].terminated)


# ------------------------------------------------------------------ CLI commands

class CliTests(Offline):
    def test_start_without_yes_is_a_dry_run(self):
        env = Env(self)
        code, out = env.cli("start", "--json")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertNotIn("-p", plan["argv"])
        self.assertEqual(plan["cwd"], str(env.repo))
        self.assertEqual(env.terminal.opened, [])
        self.assertFalse(env.state.exists())

    def test_start_launches_main_once_and_releases_lock(self):
        env = Env(self)
        code, out = env.cli("start", "--yes", "--max-ticks", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(env.terminal.opened), 1)
        self.assertEqual(env.terminal.opened[0]["cwd"], str(env.repo))
        main = launch.read_main(env.store())
        self.assertEqual(main["status"], "RUNNING")
        self.assertEqual(cli._authoritative_main(env.store()), main["session_id"])
        self.assertFalse((env.state / "supervisor.lock").exists())

    def test_start_observes_running_main_instead_of_relaunching(self):
        env = Env(self)
        env.cli("start", "--yes", "--max-ticks", "1")
        code, out = env.cli("start", "--yes", "--max-ticks", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(env.terminal.opened), 1)

    def test_start_refused_while_another_loop_holds_the_lock(self):
        env = Env(self)
        env.state.mkdir(parents=True)
        (env.state / "supervisor.lock").write_text(json.dumps({"pid": 999999, "owner": "x"}), encoding="utf-8")
        env.alive.add(999999)
        code, out = env.cli("start", "--yes", "--max-ticks", "1")
        self.assertEqual(code, 1)
        self.assertIn("already running", out)
        self.assertEqual(env.terminal.opened, [])

    def test_loop_ends_when_main_exits(self):
        env = Env(self)
        hook = {"n": 0}

        def sleep(_s):
            hook["n"] += 1
            for h in env.terminal.handles.values():
                h.code = 0
        env.svc.sleep = sleep
        code, out = env.cli("start", "--yes", "--max-ticks", "5")
        self.assertEqual(code, 0)
        self.assertIn("no longer running", out)
        self.assertEqual(launch.read_main(env.store())["status"], "EXITED")

    def test_assign_dry_run_then_queue(self):
        env = Env(self)
        env.verified_route()
        pf = env.prompt_file()
        args = ["assign", "--task", "T-X-1", "--role", "worker", "--branch", "lane/x", "--worktree", str(env.wt),
                "--owner-stream", "claude:x", "--prompt-file", pf, "--model", "auto/coding", "--provider", "gemini"]
        code, out = env.cli(*args)
        self.assertEqual(code, 0, out)
        self.assertEqual(cli.pending_requests(env.store()), [])
        code, out = env.cli(*args, "--yes")
        self.assertEqual(code, 0, out)
        reqs = cli.pending_requests(env.store())
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0][1]["payload"]["model"], "auto/coding")
        self.assertEqual(reqs[0][1]["payload"]["provider"], "gemini")

    def test_assign_refuses_bad_worktree_or_missing_prompt(self):
        env = Env(self)
        env.svc.worktree_checker = lambda wt, br: (False, "worktree is on branch 'main'")
        code, out = env.cli("assign", "--task", "T-X-1", "--role", "worker", "--branch", "lane/x", "--worktree",
                            str(env.wt), "--owner-stream", "s", "--prompt-file", str(env.tmp / "missing.txt"), "--model", "auto/coding",
                            "--provider", "gemini", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("prompt file does not exist", out)
        self.assertIn("worktree check", out)
        self.assertFalse(env.state.exists())

    def _assign_args(self, env, model="auto/coding", provider="gemini"):
        return ["assign", "--task", "T-X-1", "--role", "worker", "--branch", "lane/x", "--worktree", str(env.wt),
                "--owner-stream", "claude:x", "--prompt-file", env.prompt_file(), "--model", model,
                "--provider", provider, "--yes"]

    def test_assign_is_refused_without_a_verified_route_and_queues_nothing(self):
        env = Env(self)
        env.svc.env = dict(GATEWAY_ENV)
        code, out = env.cli(*self._assign_args(env))
        self.assertEqual(code, 1, out)
        self.assertIn("registry unavailable", out)
        self.assertFalse(env.state.exists(), "a refused assignment must create no state")

    def test_assign_is_refused_for_claude_models_and_for_anthropic(self):
        env = Env(self)
        env.verified_route()
        for model, provider, fragment in (("sonnet", "gemini", "Claude model"), ("auto/coding", "anthropic", "anthropic"),
                                          ("auto/coding", "groq", "no verified zero-cost route")):
            with self.subTest(model=model, provider=provider):
                code, out = env.cli(*self._assign_args(env, model, provider))
                self.assertEqual(code, 1, out)
                self.assertIn(fragment, out)
                self.assertEqual(cli.pending_requests(env.store()), [])

    def test_assign_is_refused_when_the_route_is_not_recorded_as_zero_cost(self):
        env = Env(self)
        env.verified_route(cost_zero="yes")
        code, out = env.cli(*self._assign_args(env))
        self.assertEqual(code, 1, out)
        self.assertEqual(cli.pending_requests(env.store()), [])

    def test_assign_is_refused_without_a_gateway_environment(self):
        env = Env(self)
        env.verified_route()
        env.svc.env = {"PATH": "p", "ANTHROPIC_API_KEY": "dummy-not-a-real-key"}
        code, out = env.cli(*self._assign_args(env))
        self.assertEqual(code, 1, out)
        self.assertIn("worker environment is not safe", out)

    def test_assign_requires_explicit_model_and_provider(self):
        env = Env(self)
        env.verified_route()
        base = ["assign", "--task", "T-X-1", "--role", "worker", "--branch", "lane/x", "--worktree", str(env.wt),
                "--owner-stream", "s", "--prompt-file", env.prompt_file(), "--yes"]
        for extra in ([], ["--model", "auto/coding"], ["--provider", "gemini"]):
            with self.subTest(extra=extra), mock.patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as cm:        # argparse refuses a missing required option
                    env.cli(*(base + extra))
                self.assertNotEqual(cm.exception.code, 0)
                self.assertEqual(cli.pending_requests(env.store()), [])

    def test_a_queued_request_that_bypasses_the_cli_is_still_gated_at_dispatch(self):
        """A request file written directly (not through `assign`) with provider anthropic must create no worker."""
        env = Env(self)
        env.verified_route()
        cli.enqueue(env.store(), "assign", {"task_id": "T-X-9", "role": "worker", "branch": "lane/x9",
                                            "worktree": str(env.wt), "owner_stream": "s", "model": "sonnet",
                                            "prompt_file": env.prompt_file(), "provider": "anthropic"})
        code, out = env.cli("start", "--yes", "--max-ticks", "1", "--no-main")
        self.assertEqual(code, 0, out)
        self.assertEqual(env.worker_launcher.specs, [], "no worker launch may happen for a refused request")
        code, out = env.cli("workers", "--json")
        self.assertEqual(json.loads(out)["workers"], [])

    def test_a_direct_request_with_a_valid_pair_but_no_registry_creates_no_worker(self):
        """The loop's own gate (not Supervisor.assign) must refuse: the pair is valid, only the registry is missing."""
        env = Env(self)
        env.svc.env = dict(GATEWAY_ENV)
        cli.enqueue(env.store(), "assign", {"task_id": "T-X-8", "role": "worker", "branch": "lane/x8",
                                            "worktree": str(env.wt), "owner_stream": "s", "model": "auto/coding",
                                            "prompt_file": env.prompt_file(), "provider": "gemini"})
        code, out = env.cli("start", "--yes", "--max-ticks", "1", "--no-main")
        self.assertEqual(code, 0, out)
        self.assertEqual(env.worker_launcher.specs, [])
        code, out = env.cli("workers", "--json")
        self.assertEqual(json.loads(out)["workers"], [])

    def test_a_lost_worker_still_counts_toward_the_concurrency_cap(self):
        """A LOST worker's process may still be alive: it must keep holding the single worker slot."""
        from dev_supervisor.workers import Worker, WorkerRegistry, WorkerStatus
        env = Env(self)
        env.verified_route()
        reg = WorkerRegistry(env.store())
        reg.add(Worker(worker_id=reg.new_id(), role="worker", task_id="T-OLD", branch="lane/old", worktree=str(env.wt / "old"),
                       owner_stream="s", provider="gemini", model="auto/coding", status=WorkerStatus.LOST))
        code, out = env.cli(*self._assign_args(env))
        self.assertEqual(code, 1, out)
        self.assertIn("concurrency cap", out)

    def test_loop_spawns_queued_workers_with_configured_permission_mode(self):
        env = Env(self, worker_permission_mode="acceptEdits")
        env.verified_route()
        env.cli("assign", "--task", "T-X-1", "--role", "worker", "--branch", "lane/x", "--worktree", str(env.wt),
                "--owner-stream", "s", "--prompt-file", env.prompt_file(), "--model", "auto/coding",
                "--provider", "gemini", "--yes")
        code, out = env.cli("start", "--yes", "--max-ticks", "2", "--no-main")
        self.assertEqual(code, 0, out)
        self.assertIn("tick 2", out)                                  # no Main is not a reason to stop here
        self.assertEqual(env.terminal.opened, [])
        spec = env.worker_launcher.specs[0]
        self.assertEqual([spec.argv[0]] + spec.argv[3:6], ["claude", "--setting-sources", "project,local", "-p"])
        self.assertEqual(spec.argv[spec.argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cli.pending_requests(env.store()), [])
        code, out = env.cli("workers", "--json")
        self.assertEqual(json.loads(out)["workers"][0]["status"], "RUNNING")

    def _allocation_registry(self, env, routes=None):
        env.verified_route()
        routes = routes or [
            {"model": "auto/coding", "provider": "gemini", "evidence": "test:a", "cost_zero": True,
             "roles": ["implementation", "research"], "capability": 2},
            {"model": "auto/big", "provider": "gemini", "evidence": "test:b", "cost_zero": True,
             "roles": ["implementation", "architecture", "review"], "capability": 5},
            {"model": "auto/legacy", "provider": "groq", "evidence": "test:c", "cost_zero": True}]
        (env.state / "verified_routes.json").write_text(json.dumps({"routes": routes}), encoding="utf-8")

    def _alloc_args(self, env, *extra, yes=True):
        a = ["assign", "--task", "T-X-1", "--role", "worker", "--branch", "lane/x", "--worktree", str(env.wt),
             "--owner-stream", "claude:x", "--prompt-file", env.prompt_file(), *extra]
        return a + (["--yes"] if yes else [])

    def test_assign_with_an_allocation_role_chooses_the_cheapest_sufficient_route(self):
        env = Env(self)
        self._allocation_registry(env)
        code, out = env.cli(*self._alloc_args(env, "--allocation-role", "implementation"))
        self.assertEqual(code, 0, out)
        (req,) = cli.pending_requests(env.store())
        payload = req[1]["payload"]
        self.assertEqual((payload["provider"], payload["model"]), ("gemini", "auto/coding"))
        self.assertEqual(payload["allocation_role"], "implementation")
        self.assertEqual(payload["allocation"]["capability"], 2)
        code, out = env.cli(*self._alloc_args(env, "--allocation-role", "architecture", yes=False))
        self.assertEqual(code, 0, out)
        self.assertIn("auto/big", out)

    def test_assign_with_an_allocation_role_refuses_a_model_or_provider_that_disagrees(self):
        env = Env(self)
        self._allocation_registry(env)
        for extra in (["--model", "auto/big"], ["--provider", "groq"], ["--model", "auto/legacy", "--provider", "groq"]):
            with self.subTest(extra=extra):
                code, out = env.cli(*self._alloc_args(env, "--allocation-role", "implementation", *extra))
                self.assertEqual(code, 1, out)
                self.assertIn("differs from the allocation", out)
        self.assertEqual(cli.pending_requests(env.store()), [])
        code, out = env.cli(*self._alloc_args(env, "--allocation-role", "implementation", "--model", "auto/coding",
                                              "--provider", "gemini"))
        self.assertEqual(code, 0, out)                                 # an explicit pair that equals the allocation is fine

    def test_assign_with_an_allocation_role_and_no_fitting_route_queues_nothing(self):
        env = Env(self)
        review3 = {"model": "auto/rev", "provider": "gemini", "evidence": "test:r", "cost_zero": True,
                   "roles": ["review"], "capability": 3}
        self._allocation_registry(env, [review3])
        for role, extra in (("review", ["--author-capability", "4"]),         # nothing as capable as the work
                            ("test_writing", []),                              # no route covers the role
                            ("architecture", [])):
            with self.subTest(role=role):
                code, out = env.cli(*self._alloc_args(env, "--allocation-role", role, *extra))
                self.assertEqual(code, 1, out)
                self.assertIn("allocation refused", out)
        self.assertEqual(cli.pending_requests(env.store()), [])
        code, out = env.cli(*self._alloc_args(env, "--allocation-role", "review", "--author-capability", "3"))
        self.assertEqual(code, 0, out)                                         # a route as capable as the work exists

    def test_assign_with_an_allocation_role_and_no_registry_is_refused(self):
        env = Env(self)
        env.svc.env = dict(GATEWAY_ENV)
        code, out = env.cli(*self._alloc_args(env, "--allocation-role", "implementation"))
        self.assertEqual(code, 1, out)
        self.assertIn("registry unavailable", out)
        self.assertFalse(env.state.exists(), "a refused assignment must create no state")

    def test_assign_without_a_role_still_requires_model_and_provider(self):
        env = Env(self)
        self._allocation_registry(env)
        for extra in ([], ["--model", "auto/coding"], ["--provider", "gemini"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as cm:
                env.cli(*self._alloc_args(env, *extra))
            self.assertEqual(cm.exception.code, 2)

    def test_the_loop_records_the_recomputed_allocation_and_ignores_the_one_in_the_request(self):
        from dev_supervisor.workers import WorkerRegistry
        env = Env(self)
        self._allocation_registry(env)
        cli.enqueue(env.store(), "assign", {
            "task_id": "T-X-9", "role": "worker", "branch": "lane/x9", "worktree": str(env.wt), "owner_stream": "s",
            "model": "auto/coding", "provider": "gemini", "prompt_file": env.prompt_file(),
            "allocation_role": "implementation",
            "allocation": {"role": "architecture", "provider": "forged", "model": "forged", "capability": 5,
                           "evidence": "forged", "reason": "forged"}})
        code, out = env.cli("start", "--yes", "--max-ticks", "1", "--no-main")
        self.assertEqual(code, 0, out)
        (w,) = WorkerRegistry(env.store()).list()
        self.assertEqual((w.allocation["role"], w.allocation["provider"], w.allocation["model"], w.allocation["capability"]),
                         ("implementation", "gemini", "auto/coding", 2))
        self.assertNotIn("forged", json.dumps(w.allocation))
        self.assertNotIn(GATEWAY_ENV["ANTHROPIC_AUTH_TOKEN"], json.dumps(w.to_dict()))
        self.assertEqual(env.worker_launcher.specs[0].provider, "gemini")

    def test_the_loop_refuses_a_request_whose_route_differs_from_the_allocation(self):
        env = Env(self)
        self._allocation_registry(env)
        base = {"task_id": "T-X-9", "role": "worker", "branch": "lane/x9", "worktree": str(env.wt), "owner_stream": "s",
                "prompt_file": env.prompt_file()}
        for extra in ({"allocation_role": "implementation", "model": "auto/big", "provider": "gemini"},
                      {"allocation_role": "test_writing", "model": "auto/coding", "provider": "gemini"},
                      {"allocation_role": "no-such-role", "model": "auto/coding", "provider": "gemini"}):
            with self.subTest(extra=extra):
                cli.enqueue(env.store(), "assign", dict(base, **extra))
                code, out = env.cli("start", "--yes", "--max-ticks", "1", "--no-main")
                self.assertEqual(code, 0, out)
        self.assertEqual(env.worker_launcher.specs, [])
        code, out = env.cli("workers", "--json")
        self.assertEqual(json.loads(out)["workers"], [])

    def test_a_request_without_an_allocation_role_records_an_empty_allocation(self):
        from dev_supervisor.workers import WorkerRegistry
        env = Env(self)
        self._allocation_registry(env)
        cli.enqueue(env.store(), "assign", {
            "task_id": "T-X-9", "role": "worker", "branch": "lane/x9", "worktree": str(env.wt), "owner_stream": "s",
            "model": "auto/legacy", "provider": "groq", "prompt_file": env.prompt_file()})
        env.cli("start", "--yes", "--max-ticks", "1", "--no-main")
        (w,) = WorkerRegistry(env.store()).list()
        self.assertEqual(w.allocation, {})

    def test_handoff_dry_run_write_and_verify(self):
        env = Env(self)
        env.cli("start", "--yes", "--max-ticks", "1")
        facts = env.facts()
        code, out = env.cli("handoff", "--input", facts, "--json")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertFalse((env.state / "handoffs").exists())
        code, out = env.cli("handoff", "--input", facts, "--yes", "--json")
        self.assertEqual(code, 0, out)
        res = json.loads(out)
        self.assertEqual(res["status"], handoff.SAFE)
        code, _ = env.cli("handoff", "--verify", res["handoff_id"])
        self.assertEqual(code, 0)

    def test_handoff_refuses_secrets_unknown_keys_and_tampering(self):
        env = Env(self)
        env.cli("start", "--yes", "--max-ticks", "1")
        code, out = env.cli("handoff", "--input", env.facts(next_actions=["use " + FAKE_KEY]), "--yes")
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_KEY, out)
        code, _ = env.cli("handoff", "--input", env.facts(transcript="x"), "--yes")
        self.assertEqual(code, 2)
        code, out = env.cli("handoff", "--input", env.facts(), "--yes", "--json")
        hid = json.loads(out)["handoff_id"]
        jp = env.state / "handoffs" / f"{hid}.json"
        doc = json.loads(jp.read_text(encoding="utf-8"))
        doc["handoff"]["objective"] = "tampered"
        jp.write_text(json.dumps(doc), encoding="utf-8")
        code, out = env.cli("handoff", "--verify", hid)
        self.assertEqual(code, 1)
        self.assertIn("NOT_SAFE", out)

    def test_compact_check_exit_codes(self):
        env = Env(self)
        code, out = env.cli("compact-check")
        self.assertEqual(code, cli.EXIT_PRESSURE_UNAVAILABLE)
        self.assertIn("UNAVAILABLE", out)
        env.cli("start", "--yes", "--max-ticks", "1")
        sid = launch.read_main(env.store())["session_id"]
        env.write_transcript(sid, 50000)
        code, out = env.cli("compact-check")
        self.assertEqual(code, 0, out)
        env.write_transcript(sid, 190000)
        code, out = env.cli("compact-check", "--json")
        self.assertEqual(code, cli.EXIT_ACTION_DUE)
        dec = json.loads(out)
        self.assertEqual(dec["action"], compaction.REPLACE)
        self.assertFalse(dec["allowed"])
        self.assertIn("no_fresh_verified_handoff", dec["blocked_by"])

    def test_replace_dry_run_refusal_and_direct_replacement(self):
        env = Env(self)
        env.cli("start", "--yes", "--max-ticks", "1")
        old = launch.read_main(env.store())["session_id"]
        code, out = env.cli("handoff", "--input", env.facts(), "--yes", "--json")
        hid = json.loads(out)["handoff_id"]
        code, out = env.cli("replace", "--handoff-id", hid)
        self.assertEqual(code, 0)
        self.assertEqual(len(env.terminal.opened), 1)                 # dry run launched nothing
        env.checkers.ok = False
        code, out = env.cli("replace", "--handoff-id", hid, "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(len(env.terminal.opened), 1)
        env.checkers.ok = True
        code, out = env.cli("replace", "--handoff-id", hid, "--yes", "--json")
        self.assertEqual(code, 0, out)
        res = json.loads(out)
        # the old Main was started by a previous process: it is not signalled, the user closes it
        self.assertEqual(res["status"], replacement.ReplacementStatus.REPLACED_OLD_NOT_CLEARED)
        self.assertEqual(len(env.terminal.opened), 2)
        new = launch.read_main(env.store())["session_id"]
        self.assertNotEqual(new, old)
        self.assertEqual(cli._authoritative_main(env.store()), new)

    def test_replace_is_queued_when_a_loop_is_running(self):
        env = Env(self)
        env.cli("start", "--yes", "--max-ticks", "1")
        code, out = env.cli("handoff", "--input", env.facts(), "--yes", "--json")
        hid = json.loads(out)["handoff_id"]
        (env.state / "supervisor.lock").write_text(json.dumps({"pid": 4321, "owner": "x"}), encoding="utf-8")
        env.alive.add(4321)
        code, out = env.cli("replace", "--handoff-id", hid, "--yes", "--json")
        self.assertEqual(code, 0, out)
        self.assertIn("queued", json.loads(out))
        self.assertEqual(len(env.terminal.opened), 1)

    def test_report_dry_run_and_flush(self):
        env = Env(self)
        code, out = env.cli("report")
        self.assertEqual(code, 1)                                     # no issue configured
        env = Env(self, github_issue=52, github_repo="owner/repo")
        env.cli("start", "--yes", "--max-ticks", "1")                 # spools a task_started event
        code, out = env.cli("report", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["pending"], 1)
        self.assertEqual(env.gh.comments, [])
        code, out = env.cli("report", "--yes", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["posted"], 1)
        issue, body = env.gh.comments[0]
        self.assertEqual(issue, 52)
        self.assertNotIn(str(env.tmp), body)

    def test_snapshot_and_status(self):
        env = Env(self)
        env.cli("start", "--yes", "--max-ticks", "1")
        code, out = env.cli("snapshot")
        self.assertEqual(code, 0)
        snap = json.loads(out)
        self.assertEqual(snap["product"], "Orbit")
        self.assertTrue(snap["read_only"])
        code, out = env.cli("status", "--json")
        self.assertEqual(code, 0)
        st = json.loads(out)
        self.assertEqual(st["main"]["status"], "RUNNING")
        self.assertTrue(st["main"]["session_file_found"])

    def test_route_explains_and_keeps_paid_route_off(self):
        env = Env(self)
        code, out = env.cli("route", "--criticality", "critical", "--complexity", "architecture", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["best"], "claude:strongest")
        self.assertEqual(data["model"], "opus")
        self.assertIn("paid_api", [e[0] for e in data["excluded"]])

    def test_status_and_workers_do_not_create_the_state_dir(self):
        env = Env(self)
        self.assertEqual(env.cli("status")[0], 0)
        self.assertEqual(env.cli("workers")[0], 0)
        self.assertFalse(env.state.exists())


class DoctorTests(Offline):
    def test_all_checks_pass_with_fakes_and_no_token_printed(self):
        env = Env(self)
        with mock.patch.dict(os.environ, {"OMNIROUTE_API_KEY": FAKE_KEY}):
            code, out = env.cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("fake-user", out)
        self.assertNotIn(FAKE_GH_TOKEN[:4] + "****", out)
        self.assertNotIn(FAKE_KEY, out)
        self.assertIn("UNCONFIGURED", out)
        self.assertIn("[WARN] resolve", out)
        self.assertTrue(env.transport_calls[0][1].startswith("http://localhost:20128/"))

    def test_failures_exit_non_zero(self):
        env = Env(self)
        env.run_results["status --porcelain"] = (0, " M file.py\n", "")
        code, out = env.cli("doctor", "--json")
        self.assertEqual(code, 1)
        checks = {c["name"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(checks["repo_clean"]["status"], "FAIL")
        self.assertNotIn("file.py", out)

    def test_omniroute_down_and_resolve_mismatch_fail(self):
        env = Env(self)

        def down(*a):
            raise TransportError("connection")
        env.svc.omniroute_transport = down
        env.svc.resolve_discover = lambda identity: {"verdict": "MISMATCH"}
        code, out = env.cli("doctor", "--json")
        self.assertEqual(code, 1)
        checks = {c["name"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(checks["omniroute"]["status"], "FAIL")
        self.assertIn("connection_error", checks["omniroute"]["detail"])
        self.assertEqual(checks["resolve"]["status"], "FAIL")

    def test_missing_claude_and_gh_logged_out(self):
        env = Env(self)
        env.svc.which = lambda name: None if name == "claude" else "/fake/bin/" + name
        env.run_results["auth status"] = (1, "", "You are not logged into any GitHub hosts.")
        code, out = env.cli("doctor", "--json")
        self.assertEqual(code, 1)
        checks = {c["name"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(checks["claude"]["status"], "FAIL")
        self.assertEqual(checks["gh_auth"]["status"], "FAIL")

    def test_state_dir_inside_repository_fails(self):
        env = Env(self)
        env.svc.config.state_dir = str(env.repo / "state")
        code, out = env.cli("doctor", "--json")
        checks = {c["name"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(checks["state_dir"]["status"], "FAIL")
        self.assertEqual(code, 1)

    def test_wave_worktrees_checked(self):
        env = Env(self, wave_id="w-test")
        (env.repo / "waves" / "w-test").mkdir(parents=True)
        (env.repo / "waves" / "w-test" / "wave.json").write_text(json.dumps(
            {"lanes": [{"id": "L1", "branch": "main"}, {"id": "L2", "branch": "lane/w-test/L2"}]}), encoding="utf-8")
        code, out = env.cli("doctor", "--json")
        checks = {c["name"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(checks["worktrees"]["status"], "FAIL")
        self.assertIn("L2", checks["worktrees"]["detail"])

    def test_parent_session_markers_warn_without_values(self):
        env = Env(self)
        env.svc.env = dict(env.svc.env or {}, CLAUDECODE="1", CLAUDE_CODE_MESSAGING_TOKEN="secret-value-xyz")
        code, out = env.cli("doctor", "--json")
        checks = {c["name"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(checks["parent_session"]["status"], "WARN")
        self.assertIn("CLAUDECODE", checks["parent_session"]["detail"])
        self.assertNotIn("secret-value-xyz", out)

    def test_no_parent_session_ok(self):
        env = Env(self)
        env.svc.env = {k: v for k, v in (env.svc.env or {}).items() if k.upper() not in
                       ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")}
        code, out = env.cli("doctor", "--json")
        checks = {c["name"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(checks["parent_session"]["status"], "OK")


# ------------------------------------------------------------------ end-to-end with fakes (mandatory)

class EndToEndTests(Offline):
    def test_start_assign_pressure_handoff_replace_status(self):
        env = Env(self, auto_replace_main=True, github_issue=52, github_repo="owner/repo")
        env.verified_route()
        for task in ("T-E2E-1", "T-E2E-2"):
            wt = env.wt / task
            wt.mkdir()
            code, out = env.cli("assign", "--task", task, "--role", "worker", "--branch", f"lane/{task}",
                                "--worktree", str(wt), "--owner-stream", "claude:e2e", "--model", "auto/coding",
                                "--provider", "gemini", "--prompt-file", env.prompt_file(), "--yes")
            self.assertEqual(code, 0, out)
        steps = []

        def sleep(_s):
            steps.append(len(steps) + 1)
            if len(steps) == 1:
                main = launch.read_main(StateStore(env.state))
                env.write_transcript(main["session_id"], 190000)        # simulated pressure: 95% of the window
                inner_out = io.StringIO()
                saved, env.svc.out = env.svc.out, inner_out
                try:
                    code = cli.main(["handoff", "--input", env.facts(), "--yes"], services=env.svc)
                finally:
                    env.svc.out = saved
                self.assertEqual(code, 0, inner_out.getvalue())
        env.svc.sleep = sleep
        code, out = env.cli("start", "--yes", "--max-ticks", "3")
        self.assertEqual(code, 0, out)

        # concurrency cap 1 (T-DEV-032): the first queued assignment spawns; the second is refused while it is active
        self.assertEqual(len(env.worker_launcher.specs), 1)
        self.assertTrue(all(s.argv[1] == "--settings" and s.argv[3:6] == ["--setting-sources", "project,local", "-p"] for s in env.worker_launcher.specs))
        # Main launched twice: the original and its replacement, both interactive
        self.assertEqual(len(env.terminal.opened), 2)
        first, second = env.terminal.opened
        self.assertTrue(all("-p" not in o["argv"] for o in (first, second)))
        old_sid = first["argv"][first["argv"].index("--session-id") + 1]
        new_sid = second["argv"][second["argv"].index("--session-id") + 1]
        # the replacement prompt carries the verified handoff hash
        pf = Path(second["argv"][second["argv"].index("--append-system-prompt-file") + 1])
        self.assertIn("sha256:", pf.read_text(encoding="utf-8"))
        # the old Main was started by this loop, so it was retired (terminated) after the new one was confirmed
        self.assertTrue(env.terminal.handles[first["pid"]].terminated)
        sessions = replacement.load_sessions(env.store())["sessions"]
        self.assertEqual(sessions[old_sid]["status"], replacement.SessionStatus.RETIRED)
        self.assertTrue(sessions[new_sid]["authoritative"])

        code, out = env.cli("status", "--json")
        self.assertEqual(code, 0)
        st = json.loads(out)
        self.assertEqual(st["main"]["session_id"], new_sid)
        self.assertEqual(st["main"]["replaced_session_id"], old_sid)
        self.assertEqual(st["authoritative_main"], new_sid)
        self.assertEqual(st["workers"], {"RUNNING": 1})   # cap 1: the second assignment was refused
        self.assertEqual(st["queued_requests"], 0)
        self.assertIsNone(st["lock"])
        events, bad = env.store().read_events()
        kinds = [e["kind"] for e in events]
        for k in ("main_launch_begin", "worker_assigned", "handoff_verified", "compaction_decision",
                  "replacement_confirmed", "session_retired"):
            self.assertIn(k, kinds)
        self.assertEqual(bad, 0)
        # GitHub progress events were spooled (not posted) and contain no absolute paths
        code, out = env.cli("report", "--json")
        self.assertGreaterEqual(json.loads(out)["pending"], 3)
        self.assertEqual(env.gh.comments, [])


# ------------------------------------------------------------------ T-DEV-029: supervisor tick latency

class TickLatencyTests(Offline):
    def make(self, **cfg):
        env = Env(self, **cfg)
        store = env.store()
        return env, store, cli.SupervisorLoop(env.svc, store)

    def running_main(self, env, loop, tokens=1000):
        loop.ensure_main()
        main = launch.read_main(loop.store)
        pid = env.terminal.opened[-1]["pid"]
        env.alive.add(pid)
        env.write_session(pid, main["session_id"])
        env.write_transcript(main["session_id"], tokens)
        return main["session_id"]

    def heartbeat(self, store):
        return json.loads((store.directory / cli.HEARTBEAT_FILE).read_text(encoding="utf-8"))

    def test_heartbeat_and_status_carry_duration_and_phases(self):
        env, store, loop = self.make()
        self.running_main(env, loop)
        clock = iter(x * 0.25 for x in range(100))
        env.svc.timer = lambda: next(clock)          # every call advances 250 ms: deterministic phases
        loop.tick()
        hb = self.heartbeat(store)
        self.assertEqual(hb["duration_ms"], 1000.0)
        self.assertEqual(hb["phase_ms"], {"requests": 250.0, "workers": 250.0, "main_and_pressure": 250.0})
        code, out = env.cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["heartbeat"]["duration_ms"], 1000.0)
        code, out = env.cli("status")
        self.assertIn("last_tick_duration_ms=1000.0", out)

    def test_real_timer_gives_a_non_negative_duration(self):
        env, store, loop = self.make()
        loop.tick()
        self.assertGreaterEqual(self.heartbeat(store)["duration_ms"], 0.0)

    def write_hb(self, store, age_s, duration_ms=None, poll=15.0, now=1_000_000.0):
        from dev_supervisor.state_store import utc_iso
        body = {"pid": 1, "tick": 3, "utc": utc_iso(now - age_s), "main_alive": True, "worker_count": 0,
                "poll_interval_s": poll}
        if duration_ms is not None:
            body["duration_ms"] = duration_ms
        (store.directory / cli.HEARTBEAT_FILE).write_text(json.dumps(body), encoding="utf-8")
        return now

    def test_stale_uses_the_larger_of_three_polls_and_three_tick_durations(self):
        env, store, loop = self.make()
        now = self.write_hb(store, age_s=100, duration_ms=None)          # no duration recorded: 3 x poll = 45 s
        self.assertEqual(cli.heartbeat_view(store, now)["state"], "STALE")
        now = self.write_hb(store, age_s=100, duration_ms=68000)         # a 68 s tick: limit 204 s
        view = cli.heartbeat_view(store, now)
        self.assertEqual((view["state"], view["stale_after_s"]), ("FRESH", 204.0))
        now = self.write_hb(store, age_s=205, duration_ms=68000)
        self.assertEqual(cli.heartbeat_view(store, now)["state"], "STALE")
        now = self.write_hb(store, age_s=40, duration_ms=1000)           # fast ticks keep the 3 x poll rule
        self.assertEqual(cli.heartbeat_view(store, now)["state"], "FRESH")
        now = self.write_hb(store, age_s=46, duration_ms=1000)
        self.assertEqual(cli.heartbeat_view(store, now)["state"], "STALE")

    def test_bad_duration_value_is_ignored(self):
        env, store, loop = self.make()
        now = self.write_hb(store, age_s=100, duration_ms="fast")
        self.assertEqual(cli.heartbeat_view(store, now)["state"], "STALE")

    def test_pressure_is_not_recomputed_from_the_whole_transcript_each_tick(self):
        env, store, loop = self.make()
        sid = self.running_main(env, loop, tokens=20000)
        for _ in range(3):
            loop.tick()
        cache = env.svc.usage_cache
        self.assertEqual((cache.full_reads, cache.incremental_reads), (1, 0))
        # the transcript grows: only the appended bytes are read and the new pressure is seen
        d = env.projects / cwd_slug(str(env.repo))
        rec = {"type": "assistant", "message": {"role": "assistant", "model": TEST_MODEL, "content": [],
                                                 "usage": {"input_tokens": 100000, "cache_read_input_tokens": 0,
                                                           "cache_creation_input_tokens": 0}}}
        with open(d / f"{sid}.jsonl", "ab") as f:
            f.write((json.dumps(rec) + "\n").encode())
        _sess, pressure = cli.main_observation(env.svc, sid)
        self.assertEqual(pressure["tokens"].value, 100000)
        self.assertEqual(pressure["ratio"].value, 0.5)
        self.assertEqual((cache.full_reads, cache.incremental_reads), (1, 1))

    def test_handoff_verification_under_pressure_is_not_repeated_every_tick(self):
        """While Main sits at compact/replace pressure with a fresh handoff, each tick used to run the git/gh ref
        checkers. Now the result is reused for HANDOFF_VERIFY_TTL_S; the decision is unchanged."""
        env, store, loop = self.make()
        self.running_main(env, loop, tokens=190000)
        code, out = env.cli("handoff", "--input", env.facts(), "--yes", "--json")
        self.assertEqual(code, 0, out)
        real = cli.handoff.verify_handoff
        with mock.patch.object(cli.handoff, "verify_handoff", wraps=real) as spy:
            first = loop.check_pressure()["decision"]
            second = loop.check_pressure()["decision"]
            self.assertEqual(spy.call_count, 1)
            self.assertEqual((first["action"], first["blocked_by"]), (second["action"], second["blocked_by"]))
            now = env.svc.clock()
            env.svc.clock = lambda: now + cli.HANDOFF_VERIFY_TTL_S + 1      # TTL elapsed: verified again
            loop.check_pressure()
            self.assertEqual(spy.call_count, 2)

    def test_pressure_decision_is_identical_with_and_without_the_cache(self):
        env, store, loop = self.make()
        sid = self.running_main(env, loop, tokens=190000)       # 95% of the window: REPLACE level
        cached = loop.check_pressure()["decision"]
        env.svc.usage_cache = cli.telemetry.UsageTailCache()
        again = loop.check_pressure()["decision"]
        for k in ("action", "level", "ratio", "allowed"):
            self.assertEqual(cached.get(k), again.get(k), k)
        self.assertIn(cached["action"], ("REPLACE", "COMPACT"))


if __name__ == "__main__":
    unittest.main()
