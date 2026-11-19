"""T-DEV-027: authority hook wired into every launch (R1), supervisor heartbeat (R2), dead Main loses authority (R3).

Offline: no claude, no network, no real hook subprocess (the installer runs with a faked ``run``).
"""
from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_dev_cli as base  # noqa: E402
from dev_supervisor import cli, launch, replacement, session_policy  # noqa: E402
from dev_supervisor.session_policy import PolicyInstallError  # noqa: E402
from dev_supervisor.workers import WorkerStatus  # noqa: E402


def failing_install(*a, **k):
    raise PolicyInstallError("hook did not deny the force-push probe")


class Loop(base.Offline):
    def make(self, **cfg):
        env = base.Env(self, **cfg)
        store = env.store()
        return env, store, cli.SupervisorLoop(env.svc, store)

    def assign(self, env, loop, name="w"):
        prompt = env.tmp / f"{name}.txt"
        prompt.write_text("do it\n", encoding="utf-8")
        wt = env.wt / name
        wt.mkdir(exist_ok=True)
        w = loop.sup.assign(task_id=f"T-{name}", role="impl", branch=f"task/{name}", worktree=str(wt),
                            owner_stream="s", model="sonnet", prompt_file=str(prompt))
        return loop.sup.spawn(w.worker_id)


class WorkerHook(Loop):
    def test_worker_argv_has_settings_and_env_merged(self):
        env, store, loop = self.make()
        w = self.assign(env, loop)
        self.assertEqual(w.status, WorkerStatus.RUNNING)
        spec = env.worker_launcher.specs[-1]
        self.assertEqual(spec.argv[1], "--settings")
        self.assertEqual(spec.argv[2], str(session_policy.settings_path(store.directory, "worker", w.session_id)))
        self.assertEqual(spec.env["CS_AUTHORITY_REPO_ROOT"], str(env.repo).replace("\\", "/"))
        self.assertIn(str(env.wt / "w").replace("\\", "/"), spec.env["CS_AUTHORITY_ROOTS"])

    def test_install_failure_blocks_launch(self):
        env, store, loop = self.make()
        loop.sup.policy_install = failing_install
        w = self.assign(env, loop)
        self.assertEqual(env.worker_launcher.specs, [])
        self.assertEqual(w.status, WorkerStatus.FAILED)
        self.assertEqual(w.failures[-1], "policy_install_failed")
        self.assertIsNone(w.pid)

    def test_claude_launcher_merges_spec_env_over_child_env(self):
        seen = {}

        class P:
            pid = 7

        def popen(argv, **kw):
            seen.update(kw)
            return P()

        from dev_supervisor.lifecycle import ClaudeCliLauncher, LaunchSpec
        env = base.Env(self)
        (env.tmp / "in.txt").write_text("x", encoding="utf-8")
        spec = LaunchSpec(argv=["claude"], cwd=str(env.tmp), stdin_path=str(env.tmp / "in.txt"),
                          stdout_path=str(env.tmp / "o"), stderr_path=str(env.tmp / "e"), max_runtime_s=1,
                          env={"CS_AUTHORITY_REPO_ROOT": "X"})
        ClaudeCliLauncher(which=lambda n: "claude", popen=popen).launch(spec)
        self.assertEqual(seen["env"]["CS_AUTHORITY_REPO_ROOT"], "X")


class MainHook(Loop):
    def test_main_argv_carries_settings_and_env_reaches_terminal(self):
        env, store, loop = self.make()
        loop.ensure_main()
        opened = env.terminal.opened[-1]
        self.assertEqual(opened["argv"][1], "--settings")
        self.assertTrue(Path(opened["argv"][2]).name.startswith("main-"))
        self.assertEqual(opened["env"]["CS_AUTHORITY_REPO_ROOT"], str(env.repo.resolve()).replace("\\", "/"))
        roots = opened["env"]["CS_AUTHORITY_ROOTS"]
        self.assertIn("CreatorStudio-worktrees", roots)

    def test_main_refused_when_install_fails(self):
        env, store, loop = self.make()
        ml = launch.InteractiveMainLauncher(store, env.terminal, launch.MainProfile(model="opus"), str(env.repo),
                                            policy_install=failing_install)
        with self.assertRaises(launch.LaunchError):
            ml.open_main(str(uuid.uuid4()))
        self.assertEqual(env.terminal.opened, [])

    def test_new_console_terminal_merges_env(self):
        seen = {}
        t = launch.NewConsoleTerminal(which=lambda n: "C:/bin/claude.exe",
                                      popen=lambda argv, **kw: seen.update(kw) or object(), os_name="nt")
        t.open(["claude"], "C:/x", env={"CS_AUTHORITY_REPO_ROOT": "R"})
        self.assertEqual(seen["env"]["CS_AUTHORITY_REPO_ROOT"], "R")


class Heartbeat(Loop):
    def test_tick_writes_heartbeat_and_status_labels_it(self):
        env, store, loop = self.make()
        loop.ensure_main()
        env.alive.add(env.terminal.opened[-1]["pid"])
        loop.tick()
        hb = json.loads((store.directory / "supervisor_heartbeat.json").read_text(encoding="utf-8"))
        for k in ("pid", "tick", "utc", "main_alive", "main_session_id", "worker_count", "poll_interval_s"):
            self.assertIn(k, hb)
        self.assertEqual(hb["tick"], 1)
        view = cli.heartbeat_view(store, env.svc.clock())
        self.assertEqual(view["state"], "FRESH")
        self.assertEqual(cli.heartbeat_view(store, env.svc.clock() + 10 * 3600)["state"], "STALE")

    def test_cmd_status_shows_missing_then_stale(self):
        env, store, loop = self.make()
        self.assertEqual(cli.heartbeat_view(store, 0.0)["state"], "MISSING")
        args = cli.build_parser().parse_args(["status"])
        cli.cmd_status(env.svc, args)
        self.assertIn("heartbeat: MISSING", env.out.getvalue())
        loop.tick()
        env.svc.clock = lambda: __import__("time").time() + 10 * 3600
        env.out.truncate(0)
        env.out.seek(0)
        cli.cmd_status(env.svc, args)
        self.assertIn("heartbeat: STALE", env.out.getvalue())


class ExitedMain(Loop):
    def test_exited_main_loses_authority(self):
        env, store, loop = self.make()
        loop.ensure_main()
        sid = launch.read_main(store)["session_id"]
        self.assertEqual(cli._authoritative_main(store), sid)
        loop.tick()                      # pid not in env.alive and not owned-alive -> treated as exited
        env.terminal.handles[env.terminal.opened[-1]["pid"]].code = 1
        loop.tick()
        rec = replacement.load_sessions(store)["sessions"][sid]
        self.assertEqual(rec["status"], replacement.SessionStatus.EXITED)
        self.assertFalse(rec["authoritative"])
        self.assertIsNone(cli._authoritative_main(store))

    def test_new_main_record_has_no_stale_fields(self):
        env, store, loop = self.make()
        launch.write_main(store, session_id=str(uuid.uuid4()), status="EXITED", pid=1, handoff_id="h-1",
                          replaced_session_id="old", ended_at="2020-01-01T00:00:00Z", failure="Boom")
        main = loop.ensure_main()
        doc = launch.read_main(store)
        for k in ("handoff_id", "replaced_session_id", "ended_at", "failure"):
            self.assertNotIn(k, doc)
        self.assertEqual(doc["status"], "RUNNING")
        self.assertEqual(doc["session_id"], main["session_id"])


if __name__ == "__main__":
    unittest.main()
