import unittest

from dev_supervisor import provider_env as pe


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


if __name__ == "__main__":
    unittest.main()
