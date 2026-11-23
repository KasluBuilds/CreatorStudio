import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import provider_env as pe  # noqa: E402

CFG = "C:/worker-owned-config"


class ProviderEnvTests(unittest.TestCase):
    LEAKED = {"ANTHROPIC_BASE_URL": "http://localhost:20128/v1", "ANTHROPIC_MODEL": "auto/coding",
              "ANTHROPIC_AUTH_TOKEN": "x", "PATH": "p"}

    def test_pro_strips_gateway_and_model_overrides(self):
        out = pe.child_env(self.LEAKED)
        self.assertEqual(out, {"PATH": "p"})

    def test_inherit_keeps_everything(self):
        env = dict(self.LEAKED, **{pe.ENV_PROVIDER: "inherit"})
        self.assertEqual(pe.child_env(env), env)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            pe.child_env({pe.ENV_PROVIDER: "paid"})

    def test_input_not_mutated(self):
        src = dict(self.LEAKED)
        pe.child_env(src)
        self.assertEqual(src, self.LEAKED)

    def test_launchers_pass_clean_env(self):
        import os
        from dev_supervisor import launch
        seen = {}
        old = dict(os.environ)
        os.environ.update(self.LEAKED)
        try:
            t = launch.NewConsoleTerminal(which=lambda n: "C:/bin/claude.exe",
                                          popen=lambda a, **kw: seen.update(kw), os_name="nt")
            t.open(["claude", "--model", "opus"], "C:/x")
        finally:
            os.environ.clear(); os.environ.update(old)
        self.assertNotIn("ANTHROPIC_BASE_URL", seen["env"])
        self.assertNotIn("ANTHROPIC_MODEL", seen["env"])

    PARENT = {"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_SESSION_ID": "sid",
              "CLAUDE_PID": "1", "CLAUDE_CODE_MESSAGING_SOCKET": "s", "CLAUDE_CODE_MESSAGING_TOKEN": "t",
              "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_EXECPATH": "e"}

    def test_pro_strips_parent_session_markers(self):
        out = pe.child_env(dict(self.PARENT, PATH="p", CLAUDE_CODE_MAX_OUTPUT_TOKENS="9"))
        self.assertEqual(out, {"PATH": "p", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "9"})

    def test_inherit_strips_parent_session_markers_only(self):
        env = dict(self.LEAKED, **self.PARENT, **{pe.ENV_PROVIDER: "inherit"})
        self.assertEqual(pe.child_env(env), dict(self.LEAKED, **{pe.ENV_PROVIDER: "inherit"}))

    def test_parent_session_markers_names_only(self):
        self.assertEqual(pe.parent_session_markers({"CLAUDECODE": "1", "PATH": "p"}), ["CLAUDECODE"])
        self.assertEqual(pe.parent_session_markers({"PATH": "p"}), [])

    def test_launchers_strip_parent_markers(self):
        import os
        from dev_supervisor import launch
        seen = {}
        old = dict(os.environ)
        os.environ.update(self.PARENT)
        try:
            t = launch.NewConsoleTerminal(which=lambda n: "C:/bin/claude.exe",
                                          popen=lambda a, **kw: seen.update(kw), os_name="nt")
            t.open(["claude", "--model", "opus"], "C:/x")
        finally:
            os.environ.clear(); os.environ.update(old)
        for name in self.PARENT:
            self.assertNotIn(name, seen["env"])



class WorkerEnvTests(unittest.TestCase):
    GATEWAY = {"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "tok-secret-value",
               "PATH": "p"}

    def test_routes_through_gateway_and_keeps_token(self):
        out = pe.worker_env(CFG, self.GATEWAY)
        self.assertEqual(out["ANTHROPIC_BASE_URL"], "http://localhost:20128")
        self.assertEqual(out["ANTHROPIC_AUTH_TOKEN"], "tok-secret-value")
        self.assertEqual(out["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")

    def test_refuses_when_gateway_missing_naming_variables_only(self):
        with self.assertRaises(pe.WorkerEnvError) as cm:
            pe.worker_env(CFG, {"PATH": "p"})
        msg = str(cm.exception)
        self.assertIn("ANTHROPIC_BASE_URL", msg)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", msg)

    def test_refuses_empty_token(self):
        with self.assertRaises(pe.WorkerEnvError):
            pe.worker_env(CFG, dict(self.GATEWAY, ANTHROPIC_AUTH_TOKEN=""))

    def test_refuses_non_local_gateway_without_echoing_it(self):
        with self.assertRaises(pe.WorkerEnvError) as cm:
            pe.worker_env(CFG, dict(self.GATEWAY, ANTHROPIC_BASE_URL="https://api.example.com"))
        self.assertNotIn("example.com", str(cm.exception))

    def test_strips_direct_key_and_parent_session_markers(self):
        out = pe.worker_env(CFG, dict(self.GATEWAY, ANTHROPIC_API_KEY="k", CLAUDECODE="1",
                                 CLAUDE_CODE_SESSION_ID="s", CS_AUTHORITY_X="y"))
        for name in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CS_AUTHORITY_X"):
            self.assertNotIn(name, out)

    def test_never_returns_claude_pro_environment(self):
        # With the gateway configured the worker keeps it; without it the call refuses instead of dropping to Pro.
        self.assertIn("ANTHROPIC_BASE_URL", pe.worker_env(CFG, self.GATEWAY))
        with self.assertRaises(pe.WorkerEnvError):
            pe.worker_env(CFG, {"ANTHROPIC_MODEL": "sonnet", "PATH": "p"})

    def test_input_not_mutated(self):
        src = dict(self.GATEWAY)
        pe.worker_env(CFG, src)
        self.assertEqual(src, self.GATEWAY)


class WorkerEnvHardeningTests(unittest.TestCase):
    GATEWAY = {"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "tok-secret-value",
               "PATH": "p"}

    def _refused(self, env):
        with self.assertRaises(pe.WorkerEnvError) as cm:
            pe.worker_env(CFG, env)
        return str(cm.exception)

    def test_host_edge_cases(self):
        for url in ("http://localhost@evil.test", "http://localhost.evil.test:20128", "https://api.example.com",
                    "http://[::1]:20128/x", "http://LOCALHOST:20128"):
            with self.subTest(url=url):
                if url in ("http://[::1]:20128/x", "http://LOCALHOST:20128"):
                    self.assertIn("ANTHROPIC_BASE_URL", pe.worker_env(CFG, dict(self.GATEWAY, ANTHROPIC_BASE_URL=url)))
                else:
                    self._refused(dict(self.GATEWAY, ANTHROPIC_BASE_URL=url))

    def test_malformed_url_does_not_echo_userinfo(self):
        msg = self._refused(dict(self.GATEWAY, ANTHROPIC_BASE_URL="http://user:pw-secret@h＃[bad"))
        self.assertNotIn("pw-secret", msg)
        self.assertNotIn("user:", msg)

    def test_whitespace_only_token_is_missing(self):
        msg = self._refused(dict(self.GATEWAY, ANTHROPIC_AUTH_TOKEN="   "))
        self.assertIn("ANTHROPIC_AUTH_TOKEN", msg)

    def test_provider_switches_and_oauth_token_are_stripped(self):
        out = pe.worker_env(CFG, dict(self.GATEWAY, CLAUDE_CODE_USE_BEDROCK="1", CLAUDE_CODE_USE_VERTEX="1",
                                 CLAUDE_CODE_OAUTH_TOKEN="oauth-x", ANTHROPIC_UNIX_SOCKET="/tmp/s"))
        for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_OAUTH_TOKEN",
                     "ANTHROPIC_UNIX_SOCKET"):
            self.assertNotIn(name, out)

    def test_gateway_flags_cannot_be_overridden_by_parent(self):
        out = pe.worker_env(CFG, dict(self.GATEWAY, CLAUDE_CODE_DISABLE_REASONING_EFFORT="0"))
        self.assertEqual(out["CLAUDE_CODE_DISABLE_REASONING_EFFORT"], "1")


class WorkerEnvRound2Tests(unittest.TestCase):
    GATEWAY = {"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "tok-secret-value",
               "PATH": "p"}

    def _refused(self, url):
        with self.assertRaises(pe.WorkerEnvError) as cm:
            pe.worker_env(CFG, dict(self.GATEWAY, ANTHROPIC_BASE_URL=url))
        return str(cm.exception)

    def test_backslash_userinfo_bypass_refused(self):
        msg = self._refused("http://evil.test" + chr(92) + "@localhost:20128")
        self.assertNotIn("evil.test", msg)

    def test_bad_port_and_scheme_refused(self):
        for url in ("http://localhost:abc", "http://localhost:99999", "//localhost:20128", "ftp://localhost:20128",
                    "http://localhost bad:20128"):
            with self.subTest(url=url):
                self._refused(url)

    def test_whitespace_only_base_url_is_missing(self):
        msg = self._refused("   ")
        self.assertIn("ANTHROPIC_BASE_URL", msg)

    def test_missing_token_alone_named(self):
        env = {k: v for k, v in self.GATEWAY.items() if k != "ANTHROPIC_AUTH_TOKEN"}
        with self.assertRaises(pe.WorkerEnvError) as cm:
            pe.worker_env(CFG, env)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", str(cm.exception))
        self.assertNotIn("ANTHROPIC_BASE_URL", str(cm.exception))

    def test_proxies_stripped(self):
        out = pe.worker_env(CFG, dict(self.GATEWAY, HTTPS_PROXY="http://p", NO_PROXY="x", ALL_PROXY="http://q"))
        for name in ("HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"):
            self.assertNotIn(name, out)

    def test_lowercase_flag_duplicate_removed(self):
        out = pe.worker_env(CFG, dict(self.GATEWAY, claude_code_disable_reasoning_effort="0"))
        self.assertNotIn("claude_code_disable_reasoning_effort", out)
        self.assertEqual(out["CLAUDE_CODE_DISABLE_REASONING_EFFORT"], "1")

    def test_child_receives_stripped_values(self):
        out = pe.worker_env(CFG, dict(self.GATEWAY, ANTHROPIC_AUTH_TOKEN="  tok  "))
        self.assertEqual(out["ANTHROPIC_AUTH_TOKEN"], "tok")


class WorkerEnvRound3Tests(unittest.TestCase):
    GATEWAY = {"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "tok-secret-value",
               "PATH": "p"}

    def test_lowercase_duplicate_of_required_vars_removed(self):
        out = pe.worker_env(CFG, dict(self.GATEWAY, anthropic_base_url="https://api.example.com",
                                 anthropic_auth_token="other"))
        self.assertNotIn("anthropic_base_url", out)
        self.assertNotIn("anthropic_auth_token", out)
        self.assertEqual(out["ANTHROPIC_BASE_URL"], "http://localhost:20128")

    def test_empty_port_refused(self):
        with self.assertRaises(pe.WorkerEnvError):
            pe.worker_env(CFG, dict(self.GATEWAY, ANTHROPIC_BASE_URL="http://localhost:"))

    def test_refusal_names_the_right_check(self):
        with self.assertRaises(pe.WorkerEnvError) as cm:
            pe.worker_env(CFG, dict(self.GATEWAY, ANTHROPIC_BASE_URL="https://api.example.com"))
        self.assertIn("must be a plain local", str(cm.exception))
        with self.assertRaises(pe.WorkerEnvError) as cm2:
            pe.worker_env(CFG, {"PATH": "p"})
        self.assertIn("missing variable(s)", str(cm2.exception))


class WorkerEnvRound4Tests(unittest.TestCase):
    GATEWAY = {"ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "tok-secret-value",
               "PATH": "p"}

    def test_config_dir_is_worker_owned(self):
        out = pe.worker_env(CFG, dict(self.GATEWAY, CLAUDE_CONFIG_DIR="C:/Users/x/.claude"))
        self.assertEqual(out["CLAUDE_CONFIG_DIR"], CFG)

    def test_refuses_without_worker_config_dir(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad), self.assertRaises(pe.WorkerEnvError):
                pe.worker_env(bad, self.GATEWAY)

    def test_worker_args_exclude_user_settings(self):
        args = pe.worker_claude_args()
        self.assertEqual(args[0], "--setting-sources")
        self.assertEqual(args[1], "project")


if __name__ == "__main__":
    unittest.main()
