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
                          stdin_path="x", stdout_path="y", stderr_path="z", max_runtime_s=1)

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
                          max_runtime_s=1)

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
        out = {"popen": {}, "launcher": {}, "child_env": {}}
        for f in sorted(PKG.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                fn = n.func
                name = (ast.unparse(fn.value) + "." + fn.attr) if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in ("subprocess.Popen", "Popen"):
                    out["popen"].setdefault(f.name, []).append(n.lineno)
                if name == "ClaudeCliLauncher":
                    out["launcher"].setdefault(f.name, []).append(n.lineno)
                if name in ("provider_env.child_env", "child_env"):
                    out["child_env"].setdefault(f.name, []).append(n.lineno)
        return out

    def test_only_reviewed_modules_call_popen_directly(self):
        self.assertEqual(sorted(self.calls()["popen"]), ["live_autonomy.py", "worker_spawner.py"])

    def test_claude_cli_launcher_is_constructed_in_exactly_one_production_place(self):
        self.assertEqual(sorted(self.calls()["launcher"]), ["dispatch_routing.py"])

    def test_the_claude_login_environment_is_used_only_by_the_main_launch_paths(self):
        """child_env defaults to Claude Pro. Main is meant to run on it; no worker module may use it."""
        self.assertEqual(sorted(self.calls()["child_env"]), ["launch.py", "main_host.py"])

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
                          stderr_path=str(self.state / "e"), max_runtime_s=1, env=env)

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


if __name__ == "__main__":
    unittest.main()
