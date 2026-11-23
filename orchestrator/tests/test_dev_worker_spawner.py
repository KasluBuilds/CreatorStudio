import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import provider_env, worker_spawner as ws  # noqa: E402

GOOD = {"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "tok-dummy-value", "PATH": "p"}
ROUTE = ws.VerifiedRoute(model="auto/coding", provider="gemini", evidence="test:verified-run", cost_zero=True)


class SpawnerRefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "wt"
        self.work.mkdir()
        self.cfg = str(self.tmp / "worker-cfg")

    def _build(self, **over):
        kw = dict(route=ROUTE, worktree=self.work, config_dir=self.cfg, prompt="do it",
                  base_env=GOOD, verified=[ROUTE])
        kw.update(over)
        return ws.build_worker_command(**kw)

    def test_accepts_verified_free_route(self):
        argv, env, cwd = self._build()
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[1:3], ["--setting-sources", "project"])
        self.assertEqual(argv[-2:], ["--model", "auto/coding"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(Path(self.cfg).resolve()))
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://localhost:20128")

    def test_refuses_unverified_route(self):
        other = ws.VerifiedRoute(model="gemini/other", provider="gemini", evidence="x", cost_zero=True)
        with self.assertRaises(ws.SpawnRefused) as cm:
            self._build(route=other)
        self.assertIn("verified registry", str(cm.exception))

    def test_refuses_route_with_nonzero_cost(self):
        paid = ws.VerifiedRoute(model="auto/coding", provider="gemini", evidence="x", cost_zero=False)
        with self.assertRaises(ws.SpawnRefused):
            self._build(route=paid)

    def test_refuses_blank_or_missing_config_dir(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad), self.assertRaises(ws.SpawnRefused):
                self._build(config_dir=bad)

    def test_refuses_default_claude_config_dir_and_its_parents_and_children(self):
        default = Path.home() / ".claude"
        for bad in (str(default), str(default / "sub"), str(default.parent)):
            with self.subTest(bad=bad), self.assertRaises(ws.SpawnRefused) as cm:
                self._build(config_dir=bad)
            self.assertIn("default", str(cm.exception))

    def test_refuses_when_gateway_missing_and_names_variable_only(self):
        with self.assertRaises(ws.SpawnRefused) as cm:
            self._build(base_env={"PATH": "p"})
        self.assertIn("ANTHROPIC_BASE_URL", str(cm.exception))
        self.assertNotIn("tok-dummy", str(cm.exception))

    def test_refuses_non_local_gateway(self):
        with self.assertRaises(ws.SpawnRefused):
            self._build(base_env=dict(GOOD, ANTHROPIC_BASE_URL="https://api.example.com"))

    def test_refuses_empty_prompt_and_missing_worktree(self):
        with self.assertRaises(ws.SpawnRefused):
            self._build(prompt="   ")
        with self.assertRaises(ws.SpawnRefused):
            self._build(worktree=self.tmp / "missing")

    def test_direct_api_key_is_never_passed(self):
        argv, env, _ = self._build(base_env=dict(GOOD, ANTHROPIC_API_KEY="sk-x", CLAUDE_CODE_OAUTH_TOKEN="o"))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_registry_loading_skips_malformed_entries(self):
        reg = self.tmp / "routes.json"
        reg.write_text(json.dumps({"routes": [
            {"model": "auto/coding", "provider": "gemini", "evidence": "e", "cost_zero": True},
            {"model": "", "provider": "gemini", "evidence": "e", "cost_zero": True},
            {"model": "x", "provider": "anthropic", "evidence": "e", "cost_zero": True},
            {"model": "y", "provider": "gemini", "evidence": "", "cost_zero": True},
            {"model": "z", "provider": "gemini", "evidence": "e", "cost_zero": "yes"},
        ]}), encoding="utf-8")
        loaded = ws.load_verified_routes(reg)
        self.assertEqual([r.model for r in loaded], ["auto/coding"])

    def test_registry_missing_is_refused(self):
        with self.assertRaises(ws.SpawnRefused):
            ws.load_verified_routes(self.tmp / "nope.json")


class SpawnerRealProcessTests(unittest.TestCase):
    """Starts a real child process. The child is a Python stand-in for claude that records what it received."""

    def test_child_receives_isolated_environment_and_arguments(self):
        tmp = Path(tempfile.mkdtemp())
        work = tmp / "wt"
        work.mkdir()
        cfg = str(tmp / "cfg")
        stub = tmp / "fake_claude.py"
        stub.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
            "  'config': os.environ.get('CLAUDE_CONFIG_DIR'), 'base': os.environ.get('ANTHROPIC_BASE_URL'),\n"
            "  'has_api_key': 'ANTHROPIC_API_KEY' in os.environ, 'has_pro_oauth': 'CLAUDE_CODE_OAUTH_TOKEN' in os.environ,\n"
            "  'has_token': 'ANTHROPIC_AUTH_TOKEN' in os.environ}))\n",
            encoding="utf-8")
        route = ws.VerifiedRoute(model="auto/coding", provider="gemini", evidence="stub", cost_zero=True)
        base = dict(os.environ)
        base.update({"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "tok-dummy-value",
                     "ANTHROPIC_API_KEY": "sk-should-not-pass", "CLAUDE_CODE_OAUTH_TOKEN": "pro-should-not-pass"})
        argv, env, cwd = ws._command(route, work, cfg, "reply ok", [route], base, sys.executable, [str(stub)])
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err.decode(errors="replace")[-300:])
        seen = json.loads(out.decode().strip().splitlines()[-1])
        self.assertEqual(seen["argv"][:2], ["--setting-sources", "project"])
        self.assertEqual(seen["argv"][-2:], ["--model", "auto/coding"])
        self.assertEqual(Path(seen["cwd"]).resolve(), work.resolve())
        self.assertEqual(Path(seen["config"]).resolve(), Path(cfg).resolve())
        self.assertEqual(seen["base"], "http://localhost:20128")
        self.assertFalse(seen["has_api_key"])
        self.assertFalse(seen["has_pro_oauth"])
        self.assertTrue(seen["has_token"])
        # the token value itself must not be echoed anywhere the child reports
        self.assertNotIn("tok-dummy-value", out.decode())

    def test_refuses_when_verified_list_omitted(self):
        with self.assertRaises(ws.SpawnRefused):
            tmp = Path(tempfile.mkdtemp())
            ws.build_worker_command(ROUTE, tmp, str(tmp / "cfg"), "x", None, GOOD)

    def test_refuses_anthropic_route_even_if_listed_as_verified(self):
        pro = ws.VerifiedRoute(model="sonnet", provider="anthropic", evidence="x", cost_zero=True)
        with self.assertRaises(ws.SpawnRefused):
            ws.build_worker_command(pro, Path(tempfile.mkdtemp()), str(Path(tempfile.mkdtemp()) / "c"), "x",
                                    [pro], GOOD)

    def test_refusal_starts_no_process(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(ws.subprocess, "Popen") as popen:
            with self.assertRaises(ws.SpawnRefused):
                ws.spawn_worker(ROUTE, tmp, None, "x", [ROUTE], tmp / "log.txt", base_env=GOOD)
        popen.assert_not_called()

class SpawnerRound3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "wt"
        self.work.mkdir()
        self.cfg = str(self.tmp / "cfg")

    def _refused(self, route, verified=None):
        with self.assertRaises(ws.SpawnRefused):
            ws.build_worker_command(route, self.work, self.cfg, "x", verified if verified is not None else [route], GOOD)

    def test_claude_models_refused_under_any_provider_prefix(self):
        for model in ("sonnet", "opus", "haiku", "claude-sonnet-5-5", "auto/claude-sonnet", "gemini/claude-haiku"):
            with self.subTest(model=model):
                r = ws.VerifiedRoute(model=model, provider="gemini", evidence="e", cost_zero=True)
                self._refused(r)

    def test_non_boolean_cost_zero_refused(self):
        r = ws.VerifiedRoute(model="auto/coding", provider="gemini", evidence="e", cost_zero="false")
        self._refused(r, verified=[r])

    def test_loader_skips_null_model_and_non_dict_entries(self):
        reg = self.tmp / "routes.json"
        reg.write_text(json.dumps({"routes": [
            {"model": None, "provider": "gemini", "evidence": "e", "cost_zero": True},
            "not-a-dict",
            {"model": "auto/coding", "provider": "gemini", "evidence": "e", "cost_zero": True},
        ]}), encoding="utf-8")
        self.assertEqual([r.model for r in ws.load_verified_routes(reg)], ["auto/coding"])

    def test_loader_refuses_non_object_registry(self):
        reg = self.tmp / "routes.json"
        reg.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(ws.load_verified_routes(reg), [])


if __name__ == "__main__":
    unittest.main()
