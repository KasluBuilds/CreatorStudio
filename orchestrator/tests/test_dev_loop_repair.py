"""T-DEV-027: authority hook wired into every launch (R1), supervisor heartbeat (R2), dead Main loses authority (R3).

Offline: no claude, no network, no real hook subprocess (the installer runs with a faked ``run``).
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_dev_cli as base  # noqa: E402
from dev_supervisor import cli, launch, lifecycle, provider_env, replacement, session_policy  # noqa: E402
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
                            owner_stream="s", model="auto/coding", provider="gemini", prompt_file=str(prompt))
        return loop.sup.spawn(w.worker_id)


class WorkerHook(Loop):
    def test_worker_argv_has_settings_and_env_merged(self):
        env, store, loop = self.make()
        w = self.assign(env, loop)
        self.assertEqual(w.status, WorkerStatus.RUNNING)
        spec = env.worker_launcher.specs[-1]
        self.assertEqual(spec.argv[1], "--settings")
        self.assertEqual(spec.argv[2], str(session_policy.settings_path(store.directory, "worker", w.session_id)))
        # T-DEV-028: a worker's repo root is its own worktree, never the main checkout
        self.assertEqual(spec.env["CS_AUTHORITY_REPO_ROOT"], str(env.wt / "w").replace("\\", "/"))
        self.assertEqual(spec.env["CS_AUTHORITY_ROOTS"], str(env.wt / "w").replace("\\", "/"))

    def test_worker_restart_keeps_settings_and_env(self):
        env, store, loop = self.make()
        w = self.assign(env, loop)
        w.status = WorkerStatus.STOPPED
        w.next_restart_not_before = None
        loop.sup.registry.update(w)
        w2 = loop.sup.restart(w.worker_id)
        self.assertEqual(w2.status, WorkerStatus.RUNNING)
        spec = env.worker_launcher.specs[-1]
        self.assertEqual(len(env.worker_launcher.specs), 2)
        self.assertEqual(spec.argv[1], "--settings")
        self.assertIn(w2.session_id, spec.argv[2])
        self.assertEqual(spec.env["CS_AUTHORITY_ROOTS"], str(env.wt / "w").replace("\\", "/"))

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
        ClaudeCliLauncher(which=lambda n: "claude", popen=popen,
                          env_builder=lambda sp: {"PATH": "p"}).launch(spec)
        self.assertEqual(seen["env"]["CS_AUTHORITY_REPO_ROOT"], "X")


class MainHook(Loop):
    def test_main_argv_carries_settings_and_env_reaches_terminal(self):
        env, store, loop = self.make()
        loop.ensure_main()
        opened = env.terminal.opened[-1]
        self.assertEqual(opened["argv"][1], "--settings")
        self.assertTrue(Path(opened["argv"][2]).name.startswith("main-"))
        self.assertEqual(opened["env"]["CS_AUTHORITY_REPO_ROOT"], str(env.repo.resolve()).replace("\\", "/"))
        repo = env.repo.resolve()
        expected = [str(repo), str(repo.parent / "CreatorStudio-worktrees"), str(store.directory),
                    str(Path.home() / ".claude" / "projects" / launch.claude_project_slug(repo))]
        self.assertEqual(opened["env"]["CS_AUTHORITY_ROOTS"].split(os.pathsep),
                         [p.replace("\\", "/") for p in expected])

    def test_main_roots_exact_and_slug(self):
        self.assertEqual(launch.claude_project_slug(r"C:\Users\Korisnik\CreatorStudio"), "C--Users-Korisnik-CreatorStudio")
        roots = launch.main_roots(Path("/r/CreatorStudio"), "/r/state", home=Path("/h"))
        self.assertEqual(roots, [str(Path("/r/CreatorStudio")), str(Path("/r/CreatorStudio-worktrees")),
                                 str(Path("/r/state")), str(Path("/h/.claude/projects/-r-CreatorStudio"))])

    def test_replacement_path_has_settings_and_env(self):
        env, store, loop = self.make()
        sid = str(uuid.uuid4())
        spec = lifecycle.LaunchSpec(argv=["claude", "--model", "opus", "--session-id", sid], cwd=str(env.repo),
                                    stdin_path=str(env.tmp / "resume.txt"), stdout_path="o", stderr_path="e",
                                    max_runtime_s=1)
        (env.tmp / "resume.txt").write_text("resume", encoding="utf-8")
        loop.main_launcher.launch(spec)
        opened = env.terminal.opened[-1]
        self.assertEqual(opened["argv"][1], "--settings")
        self.assertIn(sid, opened["argv"][2])
        self.assertIn("CS_AUTHORITY_REPO_ROOT", opened["env"])
        self.assertEqual(len(opened["env"]["CS_AUTHORITY_ROOTS"].split(os.pathsep)), 4)

    def test_parent_authority_env_not_inherited(self):
        poisoned = {"CS_AUTHORITY_GRANTS": "resolve_mutation,paid_api", "CS_AUTHORITY_OWNED_PATHS": "[\"x\"]",
                    "cs_authority_other": "1", "PATH": "p"}
        child = provider_env.child_env(poisoned)
        self.assertEqual([k for k in child if k.upper().startswith("CS_AUTHORITY_")], [])
        self.assertEqual(child["PATH"], "p")
        seen = {}
        t = launch.NewConsoleTerminal(which=lambda n: "C:/bin/claude.exe",
                                      popen=lambda argv, **kw: seen.update(kw) or object(), os_name="nt")
        with mock.patch.dict(os.environ, poisoned):
            t.open(["claude"], "C:/x", env={"CS_AUTHORITY_REPO_ROOT": "R"})
            self.assertEqual({k for k in seen["env"] if k.upper().startswith("CS_AUTHORITY_")}, {"CS_AUTHORITY_REPO_ROOT"})
            captured = {}

            def run(cmd, **kw):
                captured.update(kw["env"])
                raise OSError("stop")
            with self.assertRaises(PolicyInstallError):
                session_policy.verify_hook("x", {"CS_AUTHORITY_REPO_ROOT": "R"}, "R", run)
        self.assertNotIn("CS_AUTHORITY_GRANTS", captured)
        self.assertNotIn("CS_AUTHORITY_OWNED_PATHS", captured)

    def test_install_wraps_oserror(self):
        env, store, loop = self.make()
        with mock.patch.object(session_policy, "_atomic_write", side_effect=OSError("disk")):
            with self.assertRaises(PolicyInstallError):
                session_policy.install(store.directory, str(uuid.uuid4()), "main", str(env.repo))
        with mock.patch.object(session_policy.json, "loads", side_effect=ValueError("bad")):
            with self.assertRaises(PolicyInstallError):
                session_policy.install(store.directory, str(uuid.uuid4()), "main", str(env.repo), verify=False)

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


class HeartbeatFailure(Loop):
    def test_failed_heartbeat_write_does_not_raise(self):
        env, store, loop = self.make()
        loop.ensure_main()
        real = store.atomic_write_bytes

        def flaky(path, data, *a, **k):
            if Path(path).name == "supervisor_heartbeat.json":
                raise PermissionError("held open")
            return real(path, data, *a, **k)
        with mock.patch.object(store, "atomic_write_bytes", flaky):
            loop.tick()
        self.assertEqual(loop.ticks, 1)
        events = (store.directory / "events.jsonl")
        if events.exists():
            self.assertIn("heartbeat_write_failed", events.read_text(encoding="utf-8"))


class ExitedMain(Loop):
    def test_dead_main_at_start_is_demoted_before_new_main(self):
        env, store, loop = self.make()
        old = str(uuid.uuid4())
        launch.write_main(store, session_id=old, status="RUNNING", pid=99999)
        replacement.register_session(store, old, "main")
        main = loop.ensure_main()
        recs = replacement.load_sessions(store)["sessions"]
        self.assertFalse(recs[old]["authoritative"])
        self.assertEqual(recs[old]["status"], replacement.SessionStatus.EXITED)
        self.assertEqual(cli._authoritative_main(store), main["session_id"])

    def test_register_main_demotes_other_authoritative_main(self):
        env, store, loop = self.make()
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        replacement.register_session(store, a, "main")
        replacement.register_session(store, b, "main")
        recs = replacement.load_sessions(store)["sessions"]
        self.assertFalse(recs[a]["authoritative"])
        self.assertTrue(recs[b]["authoritative"])


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
