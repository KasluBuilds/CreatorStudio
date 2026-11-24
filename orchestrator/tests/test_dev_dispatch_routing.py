import ast
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import dispatch_routing as dr  # noqa: E402
from dev_supervisor import memory_guard  # noqa: E402
from dev_supervisor import lifecycle, live_autonomy, provider_env  # noqa: E402
from dev_supervisor.lifecycle import ClaudeCliLauncher, LaunchSpec, LifecycleError, build_claude_argv  # noqa: E402

GATEWAY = {"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "gateway-test-token", "PATH": "p"}
PKG = Path(__file__).resolve().parents[1] / "dev_supervisor"


def write_registry(state, routes):
    Path(state).mkdir(parents=True, exist_ok=True)
    (Path(state) / dr.REGISTRY_FILE).write_text(json.dumps({"routes": routes}), encoding="utf-8")


def route(model="auto/coding", provider="gemini", cost_zero=True, evidence="test:synthetic"):
    return {"model": model, "provider": provider, "evidence": evidence, "cost_zero": cost_zero}


class Base(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(memory_guard, "free_memory_mb", return_value=8192)   # independent of this machine
        patcher.start()
        self.addCleanup(patcher.stop)

    def decide(self, **kw):
        args = dict(provider="gemini", model="auto/coding", active_workers=0, state_dir=self.state,
                    base_env=dict(GATEWAY))
        args.update(kw)
        return dr.decide_worker_dispatch(**args)


class DecideTests(Base):
    def test_allowed_only_with_a_verified_route_and_a_gateway_environment(self):
        write_registry(self.state, [route()])
        d = self.decide()
        self.assertTrue(d.allowed, d.reason)
        self.assertEqual((d.provider, d.route, d.model), ("gemini", "auto/coding", "auto/coding"))

    def test_refused_without_a_registry(self):
        d = self.decide()
        self.assertFalse(d.allowed)
        self.assertIn("registry unavailable", d.reason)
        self.assertIsNone(d.provider)

    def test_refused_for_anthropic_and_claude_models_and_missing_values(self):
        write_registry(self.state, [route()])
        cases = [({"provider": "anthropic"}, "anthropic"), ({"provider": "Anthropic"}, "anthropic"),
                 ({"model": "sonnet"}, "Claude model"), ({"model": "claude-opus-5-5"}, "Claude model"),
                 ({"model": "auto/claude-sonnet"}, "Claude model"), ({"provider": None}, "provider is required"),
                 ({"provider": "  "}, "provider is required"), ({"model": None}, "model is required"),
                 ({"model": ""}, "model is required")]
        for over, fragment in cases:
            with self.subTest(over=over):
                d = self.decide(**over)
                self.assertFalse(d.allowed)
                self.assertIn(fragment, d.reason)

    def test_refused_when_the_pair_is_not_in_the_registry(self):
        write_registry(self.state, [route()])
        for over in ({"provider": "groq"}, {"model": "gemini/other"}):
            with self.subTest(over=over):
                d = self.decide(**over)
                self.assertFalse(d.allowed)
                self.assertIn("no verified zero-cost route", d.reason)

    def test_cost_must_be_recorded_as_exactly_true(self):
        for bad in (False, "yes", 1, None):
            with self.subTest(cost_zero=bad):
                write_registry(self.state, [route(cost_zero=bad)])
                self.assertFalse(self.decide().allowed)

    def test_registry_entries_for_other_providers_are_ignored(self):
        write_registry(self.state, [route(provider="anthropic", model="auto/coding")])
        self.assertFalse(self.decide(provider="anthropic").allowed)
        self.assertFalse(self.decide(provider="gemini").allowed)

    def test_refused_without_a_safe_gateway_environment(self):
        write_registry(self.state, [route()])
        for env in ({"PATH": "p"}, {"PATH": "p", "ANTHROPIC_API_KEY": "sk-x"},
                    dict(GATEWAY, ANTHROPIC_BASE_URL="https://api.example.com")):
            with self.subTest(env=sorted(env)):
                d = self.decide(base_env=env)
                self.assertFalse(d.allowed)
                self.assertIn("worker environment is not safe", d.reason)
                self.assertNotIn("sk-x", d.reason)
                self.assertNotIn("example.com", d.reason)

    def test_concurrency_cap_and_unknown_counts(self):
        write_registry(self.state, [route()])
        self.assertFalse(self.decide(active_workers=dr.MAX_ACTIVE_WORKERS).allowed)
        for bad in (None, -1, True, "0", 1.5):
            with self.subTest(active=bad):
                d = self.decide(active_workers=bad)
                self.assertFalse(d.allowed)

    def test_decision_never_leaks_a_token_or_url(self):
        write_registry(self.state, [route()])
        d = self.decide(base_env=dict(GATEWAY, ANTHROPIC_API_KEY="sk-leak"))
        self.assertNotIn("gateway-test-token", json.dumps(d.to_dict()))
        self.assertNotIn("localhost", json.dumps(d.to_dict()))


class EnvBuilderTests(Base):
    def spec(self, model="auto/coding"):
        return LaunchSpec(argv=build_claude_argv(model, "00000000-0000-4000-8000-000000000001"), cwd=str(self.state),
                          stdin_path="x", stdout_path="y", stderr_path="z", max_runtime_s=1, provider="gemini")

    def test_builds_the_isolated_environment_and_the_worker_config_dir(self):
        write_registry(self.state, [route()])
        env = dr.make_worker_env_builder(self.state, dict(GATEWAY, ANTHROPIC_API_KEY="sk-x",
                                                          CLAUDE_CODE_OAUTH_TOKEN="o"))(self.spec())
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(Path(env["CLAUDE_CONFIG_DIR"]).resolve(), dr.worker_config_dir(self.state).resolve())
        self.assertTrue(dr.worker_config_dir(self.state).is_dir())

    def test_refuses_without_registry_unverified_model_claude_model_or_unsafe_env(self):
        b = dr.make_worker_env_builder(self.state, dict(GATEWAY))
        with self.assertRaises(LifecycleError):
            b(self.spec())                                  # no registry at all
        write_registry(self.state, [route()])
        with self.assertRaises(LifecycleError):
            b(self.spec("gemini/other"))                    # not verified
        with self.assertRaises(LifecycleError):
            b(self.spec("sonnet"))                          # Claude model
        with self.assertRaises(LifecycleError):
            dr.make_worker_env_builder(self.state, {"PATH": "p"})(self.spec())   # no gateway
        no_model = self.spec()
        no_model.argv = ["claude", "-p"]
        with self.assertRaises(LifecycleError):
            b(no_model)

    def test_refusal_messages_hold_no_credentials(self):
        write_registry(self.state, [route()])
        try:
            dr.make_worker_env_builder(self.state, dict(GATEWAY, ANTHROPIC_BASE_URL="https://secret.example.com"))(self.spec())
        except LifecycleError as exc:
            self.assertNotIn("secret.example.com", str(exc))
            self.assertNotIn("gateway-test-token", str(exc))
        else:
            self.fail("an unsafe environment must be refused")


class FakePopen:
    calls = []

    def __init__(self, argv, **kw):
        FakePopen.calls.append((list(argv), kw))
        self.pid = 4242


class LauncherTests(Base):
    def spec(self, model="auto/coding"):
        prompt = self.state / "prompt.md"
        prompt.write_text("x", encoding="utf-8")
        return LaunchSpec(argv=build_claude_argv(model, "00000000-0000-4000-8000-000000000002"), cwd=str(self.state),
                          stdin_path=str(prompt), stdout_path=str(self.state / "o"), stderr_path=str(self.state / "e"),
                          max_runtime_s=1, provider="gemini")

    def launcher(self, env=None):
        la = dr.make_worker_launcher("claude", self.state, env if env is not None else dict(GATEWAY))
        la._which, la._popen = (lambda n: "/fake/bin/claude"), FakePopen
        return la

    def test_rejected_launches_start_no_process(self):
        FakePopen.calls = []
        with self.assertRaises(LifecycleError):
            self.launcher().launch(self.spec())              # no registry
        write_registry(self.state, [route()])
        for model in ("gemini/other", "sonnet"):
            with self.subTest(model=model), self.assertRaises(LifecycleError):
                self.launcher().launch(self.spec(model))
        with self.assertRaises(LifecycleError):
            self.launcher({"PATH": "p"}).launch(self.spec())  # no gateway environment
        self.assertEqual(FakePopen.calls, [], "a rejected launch must create no child process")

    def test_authorised_launch_uses_the_isolated_environment_and_project_only_settings(self):
        FakePopen.calls = []
        write_registry(self.state, [route()])
        self.launcher(dict(GATEWAY, ANTHROPIC_API_KEY="sk-parent")).launch(self.spec())
        argv, kw = FakePopen.calls[0]
        self.assertEqual(argv[1:3], ["--setting-sources", "project"])
        self.assertNotIn("ANTHROPIC_API_KEY", kw["env"])
        self.assertEqual(kw["env"]["ANTHROPIC_BASE_URL"], "http://localhost:20128")

    def test_a_policy_supplied_setting_sources_flag_is_kept_but_never_with_user(self):
        FakePopen.calls = []
        write_registry(self.state, [route()])
        spec = self.spec()
        spec.argv = [spec.argv[0], "--settings", "s.json", "--setting-sources", "project,local"] + spec.argv[1:]
        self.launcher().launch(spec)
        argv, _ = FakePopen.calls[0]
        self.assertEqual(argv.count("--setting-sources"), 1, "the launcher must not add a second flag")
        bad = self.spec()
        bad.argv = [bad.argv[0], "--setting-sources", "user,project"] + bad.argv[1:]
        with self.assertRaises(LifecycleError):
            self.launcher().launch(bad)
        self.assertEqual(len(FakePopen.calls), 1)

    def test_real_subprocess_receives_only_the_authorised_environment(self):
        write_registry(self.state, [route()])
        out = self.state / "child.json"
        script = self.state / "dump.py"
        script.write_text(chr(10).join(["import json, os, sys",
                                        "json.dump({k: v for k, v in os.environ.items() if k.startswith(('ANTHROPIC', 'CLAUDE'))}, open(sys.argv[1], 'w'))",
                                        ""]), encoding="utf-8")
        parent = dict(os.environ)
        parent.update(GATEWAY)
        parent.update({"ANTHROPIC_API_KEY": "sk-parent", "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth",
                       "ANTHROPIC_MODEL": "claude-opus-5-5", "CLAUDE_CONFIG_DIR": "C:/Users/someone/.claude"})
        # The same environment builder the production launcher uses, with a Python stand-in for claude (which would
        # not understand the Claude-only --setting-sources flag the factory adds).
        la = ClaudeCliLauncher(which=lambda n: sys.executable, env_builder=dr.make_worker_env_builder(self.state, parent))
        spec = self.spec()
        spec.argv = [sys.executable, str(script), str(out), "--model", "auto/coding"]
        h = la.launch(spec)
        for _ in range(100):
            if la.poll(h.pid) is not None:
                break
            time.sleep(0.1)
        seen = json.loads(out.read_text(encoding="utf-8"))
        self.assertNotIn("ANTHROPIC_API_KEY", seen)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen)
        self.assertNotIn("ANTHROPIC_MODEL", seen)
        self.assertEqual(Path(seen["CLAUDE_CONFIG_DIR"]).resolve(), dr.worker_config_dir(self.state).resolve())
        self.assertNotIn("sk-parent", json.dumps(seen))
        self.assertIn("ANTHROPIC_AUTH_TOKEN", seen, "only the explicitly authorised gateway credential is passed")


class LaunchPathAudit(unittest.TestCase):
    """Repository-wide audit as a test: a new, unreviewed way to start a claude process fails here."""

    @classmethod
    def calls(cls):
        out = {"popen": {}, "launcher": {}, "child_env": {}, "run_defaults": {}, "imports": {}}
        for f in sorted(PKG.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Call):
                    fn = n.func
                    last = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    full = (ast.unparse(fn.value) + "." + fn.attr) if isinstance(fn, ast.Attribute) else last
                    if last == "Popen":
                        out["popen"].setdefault(f.name, []).append(n.lineno)
                    if last == "ClaudeCliLauncher":
                        out["launcher"].setdefault(f.name, []).append(n.lineno)
                    if full in ("provider_env.child_env", "child_env"):
                        out["child_env"].setdefault(f.name, []).append(n.lineno)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in list(n.args.defaults) + [x for x in n.args.kw_defaults if x is not None]:
                        if ast.unparse(d) in ("subprocess.run", "subprocess.Popen"):
                            out["run_defaults"].setdefault(f.name, set()).add(n.name)
                if isinstance(n, ast.ImportFrom) and n.module == "subprocess":
                    if any(a.name in ("Popen", "run", "call", "check_call", "check_output") for a in n.names):
                        out["imports"].setdefault(f.name, []).append(n.lineno)
                if isinstance(n, ast.Import) and any(a.name == "subprocess" and a.asname for a in n.names):
                    out["imports"].setdefault(f.name, []).append(n.lineno)
        return out

    @classmethod
    def scan_source(cls, text):
        """Flags launch forms in one source text: aliased subprocess, from-imported subprocess functions, subprocess calls
        that carry a claude argv, os.system/startfile/exec/spawn, and aliased imports of the launcher or child_env."""
        hits = []
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.Import) and any(a.name == "subprocess" and a.asname for a in n.names):
                hits.append("aliased subprocess import")
            if isinstance(n, ast.ImportFrom):
                if n.module == "subprocess" and any(a.name in ("Popen", "run", "call", "check_call", "check_output")
                                                    for a in n.names):
                    hits.append("from-imported subprocess function")
                if any(a.name in ("ClaudeCliLauncher", "child_env") and a.asname for a in n.names):
                    hits.append("aliased launcher or child_env import")
            if isinstance(n, ast.Call):
                fn = n.func
                full = (ast.unparse(fn.value) + "." + fn.attr) if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if full in ("os.system", "os.startfile") or full.startswith(("os.exec", "os.spawn")):
                    hits.append(full)
                if full in ("subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output") \
                        and n.args and "claude" in ast.unparse(n.args[0]).lower():
                    hits.append("subprocess call carrying claude")
        return hits

    def test_no_production_module_contains_a_flagged_launch_form(self):
        flagged = {}
        for f in sorted(PKG.glob("*.py")):
            hits = self.scan_source(f.read_text(encoding="utf-8"))
            if hits:
                flagged[f.name] = hits
        self.assertEqual(flagged, {})

    def test_only_reviewed_modules_call_popen_directly(self):
        self.assertEqual(sorted(self.calls()["popen"]), ["live_autonomy.py", "worker_spawner.py"])

    def test_no_module_imports_a_subprocess_function_by_name_or_aliases_subprocess(self):
        """`from subprocess import Popen` or `import subprocess as sp` would hide a launch from the audit."""
        self.assertEqual(self.calls()["imports"], {})

    def test_claude_cli_launcher_is_constructed_in_exactly_one_production_place(self):
        self.assertEqual(sorted(self.calls()["launcher"]), ["dispatch_routing.py"])

    def test_the_claude_login_environment_is_used_only_by_the_main_launch_paths(self):
        """child_env defaults to Claude Pro. Main is meant to run on it; no worker module may use it."""
        self.assertEqual(sorted(self.calls()["child_env"]), ["launch.py", "main_host.py"])

    def test_functions_that_default_to_a_real_process_runner_are_the_reviewed_ones(self):
        """A function defaulting to subprocess.run/Popen can start a process without a visible call. Every one is listed;
        adding another must be a reviewed decision. The two probes that used to start claude default to None now."""
        got = {k: sorted(v) for k, v in self.calls()["run_defaults"].items()}
        self.assertNotIn("live_probe", got.get("session_policy.py", []))
        self.assertNotIn("live_compaction_probe", got.get("context_monitor.py", []))
        self.assertEqual(sorted(got), ["context_monitor.py", "handoff.py", "launch.py", "lifecycle.py", "main_host.py",
                                       "resolve_discovery.py", "session_policy.py"])

    def test_the_launcher_has_no_default_environment(self):
        self.assertIs(ClaudeCliLauncher.__init__.__defaults__[3], lifecycle._no_worker_env)
        with self.assertRaises(LifecycleError):
            lifecycle._no_worker_env(None)

    def test_live_autonomy_never_starts_claude_from_the_callers_environment(self):
        src = (PKG / "live_autonomy.py").read_text(encoding="utf-8")
        for fn in ("launch_request", "start_main"):
            body = src[src.index("def " + fn):]
            body = body[:body.index("\ndef ", 10)] if "\ndef " in body[10:] else body
            self.assertIn("harness_env(lay)", body, fn)
            self.assertNotIn("dict(os.environ)", body, fn)

    def test_the_harness_refuses_a_claude_model_and_an_unsafe_environment(self):
        lay = mock.Mock(root=Path(tempfile.mkdtemp()))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}, clear=True):
            with self.assertRaises(live_autonomy.HarnessRefused):
                live_autonomy.harness_env(lay)


class ReviewRound2Tests(Base):
    """Findings from the independent security review: probes, spec.env, model flags, harness entry points."""

    def test_live_probes_refuse_the_real_runner_and_start_nothing(self):
        from dev_supervisor import context_monitor, session_policy
        with self.assertRaises(session_policy.PolicyInstallError):
            session_policy.live_probe()
        with self.assertRaises(RuntimeError):
            context_monitor.live_compaction_probe()
        buf = __import__("io").StringIO()
        with mock.patch("sys.stdout", buf):
            self.assertEqual(session_policy.main(["live-probe", "--yes"]), 2)
        self.assertIn("refused", buf.getvalue())

    def test_live_probes_still_accept_an_injected_fake_runner(self):
        from dev_supervisor import context_monitor
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")
        res = context_monitor.live_compaction_probe(run=fake_run, turns=1, projects_dir=str(self.state))
        self.assertEqual(len(calls), 1)
        self.assertIn("passed", res)

    def spec_with_env(self, env):
        prompt = self.state / "prompt.md"
        prompt.write_text("x", encoding="utf-8")
        return LaunchSpec(argv=build_claude_argv("auto/coding", "00000000-0000-4000-8000-000000000003"),
                          cwd=str(self.state), stdin_path=str(prompt), stdout_path=str(self.state / "o"),
                          stderr_path=str(self.state / "e"), max_runtime_s=1, env=env, provider="gemini")

    def test_spec_env_may_only_add_authority_variables_and_never_override(self):
        write_registry(self.state, [route()])
        FakePopen.calls = []
        for bad in ({"ANTHROPIC_BASE_URL": "https://api.example.com"}, {"ANTHROPIC_AUTH_TOKEN": "x"},
                    {"ANTHROPIC_MODEL": "claude-opus-5-5"}, {"CLAUDE_CONFIG_DIR": "C:/Users/x/.claude"},
                    {"CLAUDE_CODE_USE_BEDROCK": "1"}, {"claude_config_dir": "C:/x"}, {"PATH": "evil"}):
            with self.subTest(env=sorted(bad)):
                la = dr.make_worker_launcher("claude", self.state, dict(GATEWAY))
                la._which, la._popen = (lambda n: "/fake/bin/claude"), FakePopen
                with self.assertRaises(LifecycleError):
                    la.launch(self.spec_with_env(bad))
        la = dr.make_worker_launcher("claude", self.state, dict(GATEWAY))
        la._which, la._popen = (lambda n: "/fake/bin/claude"), FakePopen
        la.launch(self.spec_with_env({"CS_AUTHORITY_REPO_ROOT": "X"}))
        self.assertEqual(len(FakePopen.calls), 1, "only the authorised launch started a process")
        self.assertEqual(FakePopen.calls[0][1]["env"]["CS_AUTHORITY_REPO_ROOT"], "X")

    def test_a_cs_authority_key_cannot_override_a_builder_value_in_another_case(self):
        FakePopen.calls = []
        la = ClaudeCliLauncher(which=lambda n: "/fake/bin/claude", popen=FakePopen,
                               env_builder=lambda spec: {"PATH": "p", "CS_AUTHORITY_X": "builder"})
        with self.assertRaises(LifecycleError):
            la.launch(self.spec_with_env({"cs_authority_x": "spec"}))
        self.assertEqual(FakePopen.calls, [])

    def test_the_provider_and_model_pair_must_be_verified_at_launch(self):
        """A restarted or recovered old record cannot ride on another provider's verified route."""
        write_registry(self.state, [route(model="auto/coding", provider="gemini")])
        FakePopen.calls = []
        for provider in ("anthropic", "Anthropic", "groq", "", "  "):
            with self.subTest(provider=provider):
                spec = self.spec_with_env({})
                spec.provider = provider
                la = dr.make_worker_launcher("claude", self.state, dict(GATEWAY))
                la._which, la._popen = (lambda n: "/fake/bin/claude"), FakePopen
                with self.assertRaises(LifecycleError):
                    la.launch(spec)
        self.assertEqual(FakePopen.calls, [], "no process for an unverified provider and model pair")
        la = dr.make_worker_launcher("claude", self.state, dict(GATEWAY))
        la._which, la._popen = (lambda n: "/fake/bin/claude"), FakePopen
        la.launch(self.spec_with_env({}))                          # the verified pair does launch
        self.assertEqual(len(FakePopen.calls), 1)

    def test_a_spec_without_a_provider_is_refused(self):
        write_registry(self.state, [route()])
        spec = self.spec_with_env({})
        spec.provider = ""
        with self.assertRaises(LifecycleError):
            dr.make_worker_env_builder(self.state, dict(GATEWAY))(spec)

    def test_more_credential_and_login_variables_are_refused_in_any_spelling(self):
        FakePopen.calls = []
        for name in ("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "claude_code_oauth_token_file_descriptor", "OPENAI_API_KEY",
                     "x_api_key", "CLAUDE_CODE_USE_VERTEX"):
            with self.subTest(name=name):
                la = ClaudeCliLauncher(which=lambda n: "/fake/bin/claude", popen=FakePopen,
                                       env_builder=lambda spec, n=name: {"PATH": "p", n: "x"})
                with self.assertRaises(LifecycleError):
                    la.launch(self.spec_with_env({}))
        self.assertEqual(FakePopen.calls, [])

    def test_probes_refuse_when_no_runner_is_injected_even_if_subprocess_run_is_patched(self):
        from dev_supervisor import context_monitor, session_policy
        with mock.patch.object(session_policy.subprocess, "run", lambda *a, **k: self.fail("a process was started")):
            with self.assertRaises(session_policy.PolicyInstallError):
                session_policy.live_probe()
            with self.assertRaises(RuntimeError):
                context_monitor.live_compaction_probe()

    def test_exactly_one_model_flag_is_required(self):
        write_registry(self.state, [route()])
        b = dr.make_worker_env_builder(self.state, dict(GATEWAY))
        base = self.spec_with_env({})
        for argv in (base.argv + ["--model", "sonnet"], base.argv + ["--model=sonnet"],
                     [a for a in base.argv if a not in ("--model", "auto/coding")]):
            with self.subTest(argv=argv[-3:]):
                spec = self.spec_with_env({})
                spec.argv = argv
                with self.assertRaises(LifecycleError):
                    b(spec)

    def test_harness_launch_functions_refuse_claude_models_before_starting_anything(self):
        lay = mock.Mock(root=Path(tempfile.mkdtemp()))
        with mock.patch.object(live_autonomy.subprocess, "Popen") as popen, \
                mock.patch.dict(os.environ, dict(GATEWAY), clear=True):
            with self.assertRaises(live_autonomy.HarnessRefused):
                live_autonomy.launch_request(lay, {"worker_id": "w1", "task": "T", "kind": "worker"}, "claude", "haiku", {}, 1)
            with self.assertRaises(live_autonomy.HarnessRefused):
                live_autonomy.start_main(lay, {}, "claude", "claude-opus-5-5", {}, 1, False)
        popen.assert_not_called()

    def test_run_live_refuses_a_claude_model(self):
        import argparse
        lay = mock.Mock(root=Path(tempfile.mkdtemp()))
        buf = __import__("io").StringIO()
        with mock.patch("sys.stderr", buf):
            rc = live_autonomy.run_live(lay, argparse.Namespace(model="haiku", claude="claude"))
        self.assertEqual(rc, 2)
        self.assertIn("never runs a Claude model", buf.getvalue())

    def test_project_settings_check_covers_login_and_model_routing_keys(self):
        from dev_supervisor import session_policy
        for key in ("gcpAuthRefresh", "forceLoginMethod", "forceLoginOrgUUID", "modelOverrides", "env", "apiKeyHelper"):
            self.assertIn(key, session_policy.FORBIDDEN_PROJECT_KEYS)


class ReviewRound3Tests(Base):
    def spec_for(self, model, provider="gemini"):
        prompt = self.state / "prompt.md"
        prompt.write_text("x", encoding="utf-8")
        return LaunchSpec(argv=build_claude_argv(model, "00000000-0000-4000-8000-000000000004"), cwd=str(self.state),
                          stdin_path=str(prompt), stdout_path=str(self.state / "o"), stderr_path=str(self.state / "e"),
                          max_runtime_s=1, provider=provider)

    def test_a_claude_named_route_in_the_registry_is_still_refused_at_launch(self):
        """The loader does not reject Claude names, so the launch gate must (a mutation check removes it)."""
        write_registry(self.state, [route(model="gemini/claude-x", provider="gemini")])
        with self.assertRaises(LifecycleError):
            dr.make_worker_env_builder(self.state, dict(GATEWAY))(self.spec_for("gemini/claude-x"))
        self.assertFalse(self.decide(model="gemini/claude-x").allowed)

    def test_credential_style_variables_never_reach_a_worker_and_gate_and_launch_agree(self):
        write_registry(self.state, [route()])
        env = dict(GATEWAY, GEMINI_API_KEY="g-key", OPENAI_API_KEY="o-key", CLAUDE_CODE_OAUTH_REFRESH_TOKEN="r",
                   CLAUDE_CODE_SOMETHING_NEW="x", SOME_OAUTH_TOKEN="t", HARMLESS="keep")
        d = self.decide(base_env=env)
        self.assertTrue(d.allowed, d.reason)
        built = dr.make_worker_env_builder(self.state, env)(self.spec_for("auto/coding"))
        for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_SOMETHING_NEW",
                     "SOME_OAUTH_TOKEN"):
            self.assertNotIn(name, built)
        self.assertEqual(built["HARMLESS"], "keep")
        self.assertIn("ANTHROPIC_AUTH_TOKEN", built)
        self.assertEqual(built["CLAUDE_CODE_DISABLE_REASONING_EFFORT"], "1")

    def test_harness_needs_a_verified_registry_that_lists_the_model(self):
        reg = self.state / "routes.json"
        write_registry(self.state, [route()])
        (self.state / dr.REGISTRY_FILE).replace(reg)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(live_autonomy.HarnessRefused):
                live_autonomy.refuse_claude_model("auto/coding")            # no registry configured
        with mock.patch.dict(os.environ, {live_autonomy.ENV_ROUTES: str(self.state / "missing.json")}, clear=True):
            with self.assertRaises(live_autonomy.HarnessRefused):
                live_autonomy.refuse_claude_model("auto/coding")
        with mock.patch.dict(os.environ, {live_autonomy.ENV_ROUTES: str(reg)}, clear=True):
            live_autonomy.refuse_claude_model("auto/coding")                 # listed: allowed
            for bad in ("auto/other", "sonnet", "haiku", "auto/claude-sonnet"):
                with self.subTest(model=bad), self.assertRaises(live_autonomy.HarnessRefused):
                    live_autonomy.refuse_claude_model(bad)

    def test_harness_registry_entry_with_non_true_cost_is_not_listed(self):
        reg = self.state / "routes.json"
        reg.write_text(json.dumps({"routes": [route(cost_zero="yes")]}), encoding="utf-8")
        with mock.patch.dict(os.environ, {live_autonomy.ENV_ROUTES: str(reg)}, clear=True):
            with self.assertRaises(live_autonomy.HarnessRefused):
                live_autonomy.refuse_claude_model("auto/coding")

    def test_audit_catches_more_launch_forms(self):
        """The audit itself, run on sources that contain forms the earlier version missed."""
        sources = {
            "a.py": "import subprocess as sp\nsp.Popen(['x'])\n",
            "b.py": "from subprocess import run\nrun(['x'])\n",
            "c.py": "import subprocess\nsubprocess.run(['claude', '-p'])\n",
            "d.py": "import os\nos.system('claude')\nos.startfile('x')\n",
            "e.py": "from .lifecycle import ClaudeCliLauncher as L\nL('claude')\n",
        }
        found = {name: LaunchPathAudit.scan_source(src) for name, src in sources.items()}
        for name, hits in found.items():
            with self.subTest(source=name):
                self.assertTrue(hits, "the audit must flag %s" % name)


if __name__ == "__main__":
    unittest.main()
