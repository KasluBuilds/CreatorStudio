"""Main terminal host and attached launch mode (VS Code two-tab setup). Offline only.

Fake claude processes (handles that write fake Claude Code session files), a fake clock whose ``sleep`` also steps the
host (so supervisor and host cooperate deterministically in one thread), a fake worker launcher and fake checkers.
No real claude, VS Code, network or process is touched; ``subprocess.Popen`` is patched to fail.
Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_main_host.py -v
"""
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dev_supervisor import cli, launch, lifecycle, main_host, provider_env, replacement  # noqa: E402
from dev_supervisor import config as cfgmod  # noqa: E402
from dev_supervisor.launch import LaunchError  # noqa: E402
from dev_supervisor.main_host import MainHost  # noqa: E402
from dev_supervisor.state_store import StoreCorruptError  # noqa: E402
from test_dev_cli import Env, Offline  # noqa: E402

HOST_PID = 9001


def lock_is_free(store):
    """True when no process holds the host's OS lock (the lock FILE may exist: its holder is the OS, not the file)."""
    fd = os.open(str(store.directory / main_host.HOST_LOCK), os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        ok = main_host._os_lock(fd)
        if ok:
            main_host._os_unlock(fd)
        return ok
    finally:
        os.close(fd)


class HostCrash(BaseException):
    """Simulates the host process dying (not caught by the host's ``except Exception``)."""


class FakeChild:
    def __init__(self, env, pid, stubborn=False):
        self.env, self.pid, self.code, self.stubborn = env, pid, None, stubborn
        self.terminated = self.killed = False
        self.raise_on_terminate = None          # e.g. KeyboardInterrupt: Ctrl+C landing while the host stops it

    def poll(self):
        return self.code

    def end(self, code):
        self.code = code
        self.env.alive.discard(self.pid)

    def terminate(self):
        if self.raise_on_terminate is not None:
            exc, self.raise_on_terminate = self.raise_on_terminate, None
            raise exc
        self.terminated = True
        if not self.stubborn:
            self.end(143)

    def kill(self):
        self.killed = True
        if not self.stubborn:
            self.end(-9)


class FakeSpawner:
    """Fake claude. ``script_new`` / ``script_resume`` list the behaviour of upcoming handover / resume starts:
    ok, raise, crash (host dies), silent (no session file), dies (exits at once), newid (session file shows another id)."""

    def __init__(self, env):
        self.env = env
        self.opened, self.handles = [], {}
        self.script_new, self.script_resume = [], []
        self.n_new = 0
        self.fail_preflight = False             # True: LaunchError; "interrupt": Ctrl+C during preflight
        self.stubborn_first = False
        self.slow_open_s = 0.0                  # real seconds a handover start takes (threaded tests)

    def preflight(self, executable):
        if self.fail_preflight == "interrupt":
            self.fail_preflight = False
            raise KeyboardInterrupt()
        if self.fail_preflight:
            raise LaunchError("claude not found on PATH")
        return "/fake/bin/claude"

    def open(self, argv, cwd):
        resume = "--resume" in argv
        sid = argv[argv.index("--resume" if resume else "--session-id") + 1]
        if resume:
            mode = self.script_resume.pop(0) if self.script_resume else "ok"
        else:
            self.n_new += 1
            mode = (self.script_new.pop(0) if self.script_new else "ok") if self.n_new > 1 else "ok"
        self.opened.append({"argv": list(argv), "cwd": cwd, "sid": sid, "mode": mode, "resume": resume})
        if mode == "raise":
            raise OSError("fake start failure")
        if mode == "interrupt":
            raise KeyboardInterrupt()
        if mode == "sigint":
            signal.raise_signal(signal.SIGINT)      # a real SIGINT delivered while the host starts the new Main
        if self.slow_open_s and resume is False and self.n_new > 1:
            time.sleep(self.slow_open_s)
        if mode == "crash":
            raise HostCrash()
        pid = self.env.next_pid()
        h = FakeChild(self.env, pid, stubborn=self.stubborn_first and len(self.handles) == 0)
        self.handles[pid] = h
        if mode == "dies":
            h.end(1)
        elif mode != "silent":
            self.env.write_session(pid, str(uuid.uuid4()) if mode == "newid" else sid)
        return h

    def live(self):
        return [p for p, h in self.handles.items() if h.poll() is None]


class World:
    """One attached-mode machine: env, fake clock, fake spawner, host and supervisor loop."""

    def __init__(self, tc, **cfg):
        defaults = dict(main_launch_mode="attached", main_permission_mode="auto", host_command_timeout_s=60,
                        host_recovery_wait_s=180)
        self.tc = tc
        self.env = Env(tc, **{**defaults, **cfg})
        self.t = 1_800_000_000.0
        self.env.svc.clock, self.env.svc.sleep = self.time, self.sleep
        self.env.alive.update({os.getpid(), HOST_PID})
        self.spawner = FakeSpawner(self.env)
        self.store = self.env.svc.store()
        self.host = None
        self.host_alive = False
        self.host_exit = None
        self.loop = None

    # clock
    def time(self):
        return self.t

    def advance(self, s):
        self.t += s

    def sleep(self, s):
        self.t += s
        self.pump()

    def pump(self):
        if self.host is not None and self.host_alive:
            try:
                code = self.host.tick()
            except HostCrash:
                self.crash_host()
                return
            if code is not None:
                self.host_exit, self.host_alive = code, False

    def crash_host(self):
        self.host_alive = False
        self.env.alive.discard(HOST_PID)
        if self.host is not None:
            self.host.simulate_process_death()      # the OS drops the dead process's lock
        for h in self.spawner.handles.values():
            if h.poll() is None:
                h.end(1)            # closing the tab takes its processes with it

    # parts
    def new_host(self, pid=HOST_PID, **kw):
        self.env.alive.add(pid)
        kw.setdefault("read_sessions", self.env.svc.read_sessions)
        return MainHost(self.store, self.env.cfg, self.spawner, root=str(self.env.repo), clock=self.time,
                        sleep=self.advance, pid_alive=self.env.svc.pid_alive, session_factory=self.env.svc.session_factory,
                        host_pid=pid, out=io.StringIO(), **kw)

    def start_host(self, **kw):
        self.host = self.new_host()
        self.host.start(**kw)
        self.host_alive = True
        return self.host

    def make_loop(self):
        self.loop = cli.SupervisorLoop(self.env.svc, self.store)
        self.tc.addCleanup(self.loop.close)
        return self.loop

    def running(self):
        """Host started, supervisor adopted it. Returns the Main session id."""
        self.start_host()
        self.make_loop()
        main = self.loop.ensure_main()
        self.tc.assertEqual(main["status"], "RUNNING", main)
        return main["session_id"]

    def handoff(self):
        code, out = self.env.cli("handoff", "--input", self.env.facts(), "--yes", "--json")
        self.tc.assertEqual(code, 0, out)
        return json.loads(out)["handoff_id"]

    def main(self):
        return launch.read_main(self.store)

    def sessions(self):
        return replacement.load_sessions(self.store)["sessions"]

    def commands(self):
        d = self.store.directory / main_host.COMMANDS_DIR
        return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []


class Base(Offline):
    def world(self, **cfg):
        return World(self, **cfg)


# ------------------------------------------------------------------ configuration

class ConfigTests(Base):
    def test_default_is_console_and_attached_is_validated(self):
        self.assertEqual(cfgmod.Config().main_launch_mode, "console")
        self.assertEqual(cfgmod.from_dict({"main_launch_mode": "attached"}).main_launch_mode, "attached")
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({"main_launch_mode": "tab"})
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({"host_recovery_wait_s": 0})

    def test_example_documents_the_keys_and_keeps_safe_defaults(self):
        ex = cfgmod.example()
        for k in ("main_launch_mode", "host_heartbeat_stale_s", "host_command_timeout_s", "host_recovery_wait_s"):
            self.assertIn(k, ex)
        self.assertFalse(ex["allow_paid_api"])
        self.assertFalse(ex["dangerously_skip_permissions"])
        self.assertEqual(ex["main_launch_mode"], "console")


# ------------------------------------------------------------------ console mode is preserved

class ConsoleModePreserved(Base):
    def test_console_start_still_opens_the_terminal_and_writes_no_host_state(self):
        env = Env(self)
        code, out = env.cli("start", "--yes", "--max-ticks", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(env.terminal.opened), 1)
        self.assertFalse((env.state / "main_host.json").exists())
        self.assertNotIn("main-host", out.split("Main session")[0])

    def test_console_mode_refuses_to_launch_beside_a_live_host_child(self):
        w = self.world(main_launch_mode="console")
        w.env.alive.add(7777)
        w.store.write(main_host.HOST_DOC, {"host_id": "x", "pid": HOST_PID, "child": {"pid": 7777, "session_id": "s"}})
        loop = w.make_loop()
        with self.assertRaises(LaunchError):
            loop.ensure_main()
        self.assertEqual(w.env.terminal.opened, [])

    def test_main_host_refuses_in_console_mode(self):
        w = self.world(main_launch_mode="console")
        with self.assertRaises(LaunchError):
            w.new_host().start()
        self.assertEqual(w.spawner.opened, [])


# ------------------------------------------------------------------ host start, duplicates, argv

class HostStartTests(Base):
    def test_host_starts_one_interactive_main_with_safe_flags(self):
        w = self.world()
        h = w.start_host()
        self.assertEqual(len(w.spawner.opened), 1)
        argv = w.spawner.opened[0]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("-p", argv)
        self.assertEqual(w.spawner.opened[0]["cwd"], str(w.env.repo))
        doc = main_host.read_host(w.store)
        self.assertEqual(doc["state"], "RUNNING")
        self.assertEqual(doc["child"]["session_id"], argv[argv.index("--session-id") + 1])
        self.assertNotIn("handle", json.dumps(doc))
        self.assertTrue(h._locked)

    def test_refuses_when_main_json_records_a_live_main_the_invisible_console_main(self):
        w = self.world()
        w.env.alive.add(5555)
        launch.write_main(w.store, session_id=str(uuid.uuid4()), status="RUNNING", pid=5555)
        with self.assertRaises(LaunchError) as cm:
            w.new_host().start()
        self.assertIn("duplicate", str(cm.exception))
        self.assertEqual(w.spawner.opened, [])
        self.assertTrue(lock_is_free(w.store))

    def test_ignore_recorded_main_is_an_explicit_override(self):
        w = self.world()
        w.env.alive.add(5555)
        launch.write_main(w.store, session_id=str(uuid.uuid4()), status="RUNNING", pid=5555)
        w.new_host().start(ignore_recorded=True)
        self.assertEqual(len(w.spawner.opened), 1)

    def test_stale_recorded_main_does_not_block(self):
        w = self.world()
        launch.write_main(w.store, session_id=str(uuid.uuid4()), status="RUNNING", pid=5555)   # pid not alive
        w.new_host().start()
        self.assertEqual(len(w.spawner.opened), 1)

    def test_second_host_is_refused_while_the_first_is_alive(self):
        w = self.world()
        w.start_host()
        with self.assertRaises(LaunchError) as cm:
            w.new_host(pid=9002).start()
        self.assertIn("already running", str(cm.exception))
        self.assertEqual(len(w.spawner.opened), 1)

    def test_stale_lock_of_a_dead_host_is_taken_over_and_recorded(self):
        w = self.world()
        (w.store.directory / main_host.HOST_LOCK).write_text(json.dumps({"host_id": "old", "pid": 4242}))
        w.new_host().start()
        self.assertEqual(len(w.spawner.opened), 1)
        events, _ = w.store.read_events()
        self.assertTrue(any(e["kind"] == "main_host_stale_lock_taken_over" for e in events))

    def test_orphaned_child_of_an_earlier_host_blocks_a_new_host(self):
        w = self.world()
        w.env.alive.add(6000)
        w.store.write(main_host.HOST_DOC, {"host_id": "earlier", "pid": 4242, "child": {"pid": 6000, "session_id": "s"}})
        with self.assertRaises(LaunchError) as cm:
            w.new_host().start()
        self.assertIn("earlier host", str(cm.exception))
        self.assertEqual(w.spawner.opened, [])

    def test_preflight_failure_starts_nothing_and_releases_the_lock(self):
        w = self.world()
        w.spawner.fail_preflight = True
        with self.assertRaises(LaunchError):
            w.new_host().start()
        self.assertEqual(w.spawner.opened, [])
        self.assertTrue(lock_is_free(w.store))

    def test_cli_main_host_dry_run_and_run(self):
        w = self.world()
        w.env.svc.main_spawner = w.spawner
        code, out = w.env.cli("main-host")
        self.assertEqual(code, 0, out)
        self.assertEqual(w.spawner.opened, [])
        self.assertIn("duplicate_check", out)
        code, out = w.env.cli("main-host", "--yes", "--max-ticks", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(w.spawner.opened), 1)
        w.spawner.handles[list(w.spawner.handles)[0]].end(0)          # the first run's Main is gone
        w.env.alive.add(5555)
        launch.write_main(w.store, session_id=str(uuid.uuid4()), status="RUNNING", pid=5555)
        w.host = None
        code, out = w.env.cli("main-host", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("duplicate", out)
        self.assertEqual(len(w.spawner.opened), 1)


# ------------------------------------------------------------------ startup ordering and identity

class OrderingAndIdentityTests(Base):
    def test_supervisor_first_never_launches_then_adopts_the_host_main(self):
        w = self.world()
        loop = w.make_loop()
        main = loop.ensure_main()
        self.assertEqual(main["status"], "AWAITING_HOST")
        self.assertEqual(w.env.terminal.opened, [])
        self.assertEqual(w.spawner.opened, [])
        w.start_host()
        t = loop.tick()
        self.assertTrue(t["main_alive"])
        main = w.main()
        self.assertEqual(main["status"], "RUNNING")
        self.assertEqual(main["session_id"], w.spawner.opened[0]["sid"])
        self.assertTrue(w.sessions()[main["session_id"]]["authoritative"])
        self.assertEqual(w.env.terminal.opened, [])

    def test_host_first_then_supervisor_adopts_without_launching(self):
        w = self.world()
        w.start_host()
        main = w.make_loop().ensure_main()
        self.assertEqual(main["status"], "RUNNING")
        self.assertEqual(main["pid"], list(w.spawner.handles)[0])
        self.assertEqual(w.env.terminal.opened, [])
        self.assertEqual(len(w.spawner.opened), 1)

    def test_supervisor_waiting_for_host_does_not_end_the_loop(self):
        w = self.world()
        loop = w.make_loop()
        loop.ensure_main()
        code = loop.run(max_ticks=3)
        self.assertEqual(code, 0)
        self.assertEqual(loop.ticks, 3)
        self.assertEqual(w.main()["status"], "AWAITING_HOST")
        self.assertIn("main_awaiting_host", [e["kind"] for e in w.store.read_events()[0]])

    def test_start_in_attached_mode_tells_the_user_which_command_to_run_in_the_main_tab(self):
        w = self.world()
        code, out = w.env.cli("start", "--yes", "--max-ticks", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("main-host --yes", out)
        self.assertEqual(w.env.terminal.opened, [])
        code, out = w.env.cli("start")
        self.assertIn("main_tab", out)

    def test_session_file_with_another_id_is_an_identity_mismatch_and_not_adopted(self):
        w = self.world()
        w.spawner.script_new = []
        w.start_host()
        pid = list(w.spawner.handles)[0]
        w.env.write_session(pid, str(uuid.uuid4()))          # the process on the host's pid reports another session
        loop = w.make_loop()
        loop.ensure_main()
        main = w.main()
        self.assertEqual(main["status"], "IDENTITY_MISMATCH")
        self.assertNotEqual(main.get("observed_session_id"), main.get("expected_session_id"))
        self.assertNotIn(main.get("observed_session_id"), w.sessions())
        self.assertIn("main_identity_mismatch", [e["kind"] for e in w.store.read_events()[0]])

    def test_no_session_file_yet_means_awaiting_session_not_adopted(self):
        w = self.world()
        w.start_host()
        for f in w.env.sessions.glob("*.json"):
            f.unlink()
        loop = w.make_loop()
        self.assertEqual(loop.attached.reconcile()["state"], "awaiting_session")
        self.assertNotEqual(w.main().get("status"), "RUNNING")

    def test_invisible_old_console_main_is_observed_never_duplicated_or_signalled(self):
        w = self.world()
        w.env.alive.add(5555)
        old = str(uuid.uuid4())
        launch.write_main(w.store, session_id=old, status="RUNNING", pid=5555)
        loop = w.make_loop()
        main = loop.ensure_main()
        self.assertEqual((main["session_id"], main["pid"]), (old, 5555))
        self.assertEqual(w.env.terminal.opened, [])
        self.assertEqual(w.spawner.opened, [])
        with self.assertRaises(LaunchError):
            w.new_host().start()
        self.assertIn(5555, w.env.alive)                      # never signalled


# ------------------------------------------------------------------ host disconnect / restart, supervisor restart

class DisconnectAndRestartTests(Base):
    def test_user_exits_main_host_ends_supervisor_marks_exited_and_loop_ends(self):
        w = self.world()
        sid = w.running()
        w.spawner.handles[list(w.spawner.handles)[0]].end(0)
        w.pump()
        self.assertEqual(w.host_exit, 0)
        self.assertEqual(main_host.read_host(w.store)["state"], "EXITED")
        self.assertTrue(lock_is_free(w.store))      # lock released
        t = w.loop.tick()
        self.assertFalse(t["main_alive"])
        self.assertEqual(w.main()["status"], "EXITED")
        w.advance(60)
        self.assertEqual(w.loop.run(max_ticks=5), 0)           # no live host: the loop ends like console mode
        self.assertEqual(sid, w.main()["session_id"])

    def test_tab_closed_host_restart_starts_one_fresh_main_and_supervisor_adopts_it(self):
        w = self.world()
        old = w.running()
        w.crash_host()                                          # tab closed: host and its Main die
        w.loop.tick()
        self.assertEqual(w.main()["status"], "EXITED")
        w.start_host()                                          # new tab, same command
        w.loop.tick()
        main = w.main()
        self.assertEqual(main["status"], "RUNNING")
        self.assertNotEqual(main["session_id"], old)
        self.assertEqual(len(w.spawner.live()), 1)
        self.assertEqual(w.sessions()[old]["status"], replacement.SessionStatus.RETIRED)
        self.assertTrue(w.sessions()[main["session_id"]]["authoritative"])

    def test_resume_last_resumes_the_recorded_session(self):
        w = self.world()
        old = w.running()
        w.crash_host()
        w.start_host(resume_last=True)
        o = w.spawner.opened[-1]
        self.assertTrue(o["resume"])
        self.assertEqual(o["sid"], old)
        self.assertNotIn("--session-id", o["argv"])
        w.loop.tick()
        self.assertEqual(w.main()["session_id"], old)           # same id observed in the session file

    def test_commands_left_over_from_before_the_host_started_are_expired_not_run(self):
        w = self.world()
        cid = main_host.send_command(w.store, "stop_child", {"pid": 1, "session_id": str(uuid.uuid4())}, 600)
        w.start_host()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "EXPIRED")
        self.assertEqual(len(w.spawner.live()), 1)

    def test_supervisor_restart_after_the_host_finished_a_handover_adopts_the_new_main(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        new = str(uuid.uuid4())
        cid = main_host.send_command(w.store, "handover", {
            "old_session_id": old, "new_session_id": new, "model": "opus", "handoff_id": hid,
            "prompt_file": str(self._prompt(w))}, 60)
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "STARTED")
        w.loop.close()                                          # supervisor "dies" before recording anything
        loop2 = cli.SupervisorLoop(w.env.svc, w.store)
        self.addCleanup(loop2.close)
        loop2.tick()
        main = w.main()
        self.assertEqual((main["status"], main["session_id"]), ("RUNNING", new))
        self.assertEqual(w.sessions()[old]["status"], replacement.SessionStatus.RETIRED)
        self.assertTrue(w.sessions()[new]["authoritative"])
        self.assertEqual(len(w.spawner.live()), 1)

    def _prompt(self, w):
        p = w.store.directory / "handoffs"
        p.mkdir(exist_ok=True)
        f = p / "resume.prompt.txt"
        f.write_text("read the verified handoff\n", encoding="utf-8")
        return f


# ------------------------------------------------------------------ host command validation

class HostCommandTests(Base):
    def _send(self, w, op, payload, ttl=60, supervisor_pid=None):
        return main_host.send_command(w.store, op, payload, ttl, supervisor_pid=supervisor_pid)

    def test_expired_command_is_not_executed(self):
        w = self.world()
        old = w.running()
        cid = self._send(w, "handover", {"old_session_id": old, "new_session_id": str(uuid.uuid4())}, ttl=5)
        w.advance(30)
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "EXPIRED")
        self.assertEqual(len(w.spawner.live()), 1)

    def test_command_from_a_dead_supervisor_is_rejected(self):
        w = self.world()
        old = w.running()
        cid = self._send(w, "handover", {"old_session_id": old, "new_session_id": str(uuid.uuid4())},
                         supervisor_pid=31337)
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "REJECTED")
        self.assertEqual(len(w.spawner.live()), 1)

    def test_handover_for_a_session_the_host_does_not_own_is_rejected_and_touches_nothing(self):
        w = self.world()
        w.running()
        cid = self._send(w, "handover", {"old_session_id": str(uuid.uuid4()), "new_session_id": str(uuid.uuid4())})
        w.pump()
        ack = main_host.read_ack(w.store, cid)
        self.assertEqual(ack["status"], "REJECTED")
        self.assertIn("identity", ack["reason"])
        self.assertEqual(len(w.spawner.opened), 1)

    def test_prompt_file_outside_the_state_directory_is_rejected(self):
        w = self.world()
        old = w.running()
        outside = w.env.tmp / "elsewhere.txt"
        outside.write_text("x")
        cid = self._send(w, "handover", {"old_session_id": old, "new_session_id": str(uuid.uuid4()),
                                         "prompt_file": str(outside)})
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "REJECTED")
        self.assertEqual(len(w.spawner.opened), 1)
        self.assertEqual(len(w.spawner.live()), 1)

    def test_resume_is_refused_while_a_main_runs(self):
        w = self.world()
        old = w.running()
        cid = self._send(w, "resume", {"session_id": old})
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "REJECTED")
        self.assertEqual(len(w.spawner.opened), 1)

    def test_stop_child_needs_the_matching_pid_and_session(self):
        w = self.world()
        old = w.running()
        cid = self._send(w, "stop_child", {"pid": 1, "session_id": old})
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "REJECTED")
        self.assertEqual(len(w.spawner.live()), 1)

    def test_unknown_command_cannot_be_written_or_executed(self):
        w = self.world()
        w.running()
        with self.assertRaises(LaunchError):
            self._send(w, "rm-rf", {})
        d = w.store.directory / main_host.COMMANDS_DIR
        d.mkdir(exist_ok=True)
        (d / "000-x.json").write_text(json.dumps({"id": "000-x", "op": "rm-rf", "payload": {}}))
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, "000-x")["status"], "REJECTED")

    def test_coordination_files_never_hold_credentials(self):
        w = self.world()
        w.running()
        secret = "sk-" + "ant-" + "z" * 30
        main_host.send_command(w.store, "settle", {"session_id": str(uuid.uuid4()), "note": secret}, 60)
        blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in w.store.directory.rglob("*")
                       if p.is_file() and p.suffix in (".json", ".lock"))
        self.assertNotIn(secret, blob)


# ------------------------------------------------------------------ replacement

class ReplacementSuccessTests(Base):
    def test_replacement_in_the_same_tab_with_observed_evidence(self):
        w = self.world(auto_replace_main=True)
        old = w.running()
        old_pid = w.main()["pid"]
        hid = w.handoff()
        res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.REPLACED, res)
        self.assertTrue(res["old_session_cleared"])
        new = res["new_session_id"]
        self.assertNotEqual(new, old)
        self.assertEqual(len(w.spawner.opened), 2)
        self.assertTrue(w.spawner.handles[old_pid].terminated)         # ended by the HOST, which owns it
        self.assertEqual(w.env.terminal.opened, [])                    # the supervisor opened nothing
        self.assertEqual(len(w.spawner.live()), 1)                     # exactly one Main
        main = w.main()
        self.assertEqual((main["session_id"], main["status"], main["replaced_session_id"]), (new, "RUNNING", old))
        self.assertEqual(w.sessions()[old]["status"], replacement.SessionStatus.RETIRED)
        self.assertTrue(w.sessions()[new]["authoritative"])
        o = w.spawner.opened[1]
        pf = Path(o["argv"][o["argv"].index("--append-system-prompt-file") + 1])
        self.assertIn("sha256:", pf.read_text(encoding="utf-8"))       # the verified handoff hash reached the new Main
        self.assertEqual(o["argv"][o["argv"].index("--permission-mode") + 1], "auto")
        self.assertNotIn("--dangerously-skip-permissions", o["argv"])
        self.assertIsNone(main_host.read_host(w.store)["recover_session_id"])          # settled
        kinds = [e["kind"] for e in w.store.read_events()[0]]
        for k in ("replacement_confirmed", "session_retired", "main_host_child_ended"):
            self.assertIn(k, kinds)

    def test_a_second_handover_for_the_already_replaced_session_is_rejected(self):
        w = self.world()
        old = w.running()
        res = w.loop.replace_main(w.handoff())
        self.assertEqual(res["status"], replacement.ReplacementStatus.REPLACED)
        cid = main_host.send_command(w.store, "handover", {"old_session_id": old, "new_session_id": str(uuid.uuid4())}, 60)
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "REJECTED")
        self.assertEqual(len(w.spawner.live()), 1)

    def test_automatic_replacement_via_the_queue_uses_the_host(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        cli.enqueue(w.store, "replace", {"handoff_id": hid})
        t = w.loop.tick()
        self.assertTrue(t["requests"][0]["ok"], t)
        self.assertNotEqual(w.main()["session_id"], old)
        self.assertEqual(len(w.spawner.live()), 1)


class ReplacementFailureTests(Base):
    """Failure at every transition. The invariant: never a false success, never two Mains, and the user ends with a
    usable Main (or an explicit NEEDS_ATTENTION)."""

    def _fail(self, w, res):
        self.assertNotIn(res["status"], (replacement.ReplacementStatus.REPLACED,
                                         replacement.ReplacementStatus.REPLACED_OLD_NOT_CLEARED), res)

    def test_unsafe_or_tampered_handoff_sends_the_host_nothing(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        jp = w.store.directory / "handoffs" / f"{hid}.json"
        doc = json.loads(jp.read_text(encoding="utf-8"))
        doc["handoff"]["objective"] = "tampered"
        jp.write_text(json.dumps(doc), encoding="utf-8")
        before = w.commands()
        res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.REFUSED_HANDOFF_NOT_SAFE)
        self.assertEqual(w.commands(), before)
        self.assertEqual(len(w.spawner.opened), 1)
        self.assertEqual(w.main()["session_id"], old)

    def test_checker_failure_refuses_and_keeps_the_old_main(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.env.checkers.ok = False
        res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.REFUSED_HANDOFF_NOT_SAFE)
        self.assertEqual((len(w.spawner.live()), w.main()["session_id"]), (1, old))

    def test_no_live_host_old_main_untouched(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.host_alive = False                      # host stops heartbeating (hung), process id still listed
        w.advance(120)
        res = w.loop.replace_main(hid)
        self._fail(w, res)
        self.assertEqual(res["status"], replacement.ReplacementStatus.ROLLED_BACK)
        self.assertEqual(res["recovery"]["outcome"], "OLD_MAIN_INTACT")
        self.assertEqual(w.main()["session_id"], old)
        self.assertEqual(len(w.spawner.opened), 1)
        self.assertFalse(any(h.terminated for h in w.spawner.handles.values()))

    def test_host_preflight_rejection_stops_nothing(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.spawner.fail_preflight = True
        res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.ROLLED_BACK)
        self.assertEqual(res["recovery"]["outcome"], "OLD_MAIN_INTACT")
        self.assertFalse(any(h.terminated for h in w.spawner.handles.values()))
        self.assertEqual((len(w.spawner.live()), w.main()["session_id"]), (1, old))
        self.assertTrue(w.sessions()[old]["authoritative"])

    def test_supervisor_and_host_disagree_about_the_main_identity(self):
        w = self.world()
        w.running()
        hid = w.handoff()
        other = str(uuid.uuid4())
        launch.write_main(w.store, session_id=other, status="RUNNING")
        replacement.register_session(w.store, other, launch.MAIN_ROLE)
        with self.assertRaises(replacement.UnverifiedHandoffError):      # handoff belongs to another session
            w.loop.replace_main(hid)
        self.assertEqual(len(w.spawner.opened), 1)
        self.assertFalse(any(h.terminated for h in w.spawner.handles.values()))

    def test_old_main_that_cannot_be_stopped_is_left_running(self):
        w = self.world()
        w.spawner.stubborn_first = True
        old = w.running()
        hid = w.handoff()
        res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.ROLLED_BACK)
        self.assertEqual(res["recovery"]["outcome"], "OLD_MAIN_INTACT")
        self.assertEqual(len(w.spawner.opened), 1)                    # no second Main was started beside it
        self.assertEqual(w.main()["session_id"], old)
        self.assertEqual(main_host.read_host(w.store)["child"]["session_id"], old)

    def test_new_main_cannot_start_old_session_is_resumed_in_the_tab(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.spawner.script_new = ["raise", "raise"]
        res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.ROLLED_BACK, res)
        self.assertEqual(res["recovery"]["outcome"], "RESUMED_SAME_ID")
        o = w.spawner.opened[-1]
        self.assertTrue(o["resume"])
        self.assertEqual(o["sid"], old)
        self.assertNotIn("--session-id", o["argv"])
        self.assertEqual(len(w.spawner.live()), 1)
        main = w.main()
        self.assertEqual((main["session_id"], main["status"]), (old, "RUNNING"))
        self.assertEqual(main["pid"], o and [p for p, h in w.spawner.handles.items() if h.poll() is None][0])
        self.assertTrue(w.sessions()[old]["authoritative"])
        self.assertIn("not verified", res["recovery"]["note"])

    def test_new_main_never_sends_a_heartbeat_is_stopped_then_old_resumed(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.spawner.script_new = ["silent", "silent"]
        res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.ROLLED_BACK, res)
        self.assertEqual(res["recovery"]["outcome"], "RESUMED_SAME_ID")
        self.assertEqual([a["reason"] for a in res["attempts"]], ["heartbeat_timeout"] * 2)
        stopped = [h for h in w.spawner.handles.values() if h.terminated]
        self.assertGreaterEqual(len(stopped), 3)           # the old Main (handover) and both silent Mains (stop_child)
        self.assertEqual(len(w.spawner.live()), 1)
        self.assertEqual(w.main()["session_id"], old)

    def test_new_main_exits_at_once_then_the_retry_succeeds_and_old_is_not_resurrected(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.spawner.script_new = ["dies", "ok"]
        res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.REPLACED, res)
        self.assertEqual(len(res["attempts"]), 2)
        self.assertFalse(res["attempts"][0]["ok"])
        self.assertEqual([o["resume"] for o in w.spawner.opened], [False, False, False])
        self.assertEqual(len(w.spawner.live()), 1)
        self.assertNotEqual(w.main()["session_id"], old)

    def test_host_dies_mid_handover_no_false_success_and_needs_attention(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.spawner.script_new = ["crash", "ok"]
        res = w.loop.replace_main(hid)
        self._fail(w, res)
        self.assertEqual(res["recovery"]["outcome"], "NEEDS_ATTENTION")
        self.assertEqual(w.main()["status"], "NEEDS_ATTENTION")
        self.assertFalse(w.sessions()[old]["authoritative"])
        self.assertEqual(w.spawner.live(), [])

    def test_resume_refused_after_failed_handover_is_needs_attention(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.spawner.script_new = ["raise", "raise"]
        w.spawner.script_resume = ["raise"]
        res = w.loop.replace_main(hid)
        self.assertEqual(res["recovery"]["outcome"], "NEEDS_ATTENTION")
        self.assertEqual(w.main()["status"], "NEEDS_ATTENTION")
        self.assertEqual(w.spawner.live(), [])

    def test_resume_under_a_new_id_records_the_observed_identity_only(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        w.spawner.script_new = ["raise", "raise"]
        w.spawner.script_resume = ["newid"]
        res = w.loop.replace_main(hid)
        self.assertEqual(res["recovery"]["outcome"], "RESUMED_NEW_ID")
        observed = res["recovery"]["session_id"]
        self.assertNotEqual(observed, old)
        self.assertEqual(w.main()["session_id"], observed)
        self.assertEqual(w.sessions()[old]["status"], replacement.SessionStatus.RETIRED)
        self.assertTrue(w.sessions()[observed]["authoritative"])

    def test_resumed_main_that_never_shows_a_session_is_not_confirmed(self):
        w = self.world()
        w.running()
        hid = w.handoff()
        w.spawner.script_new = ["raise", "raise"]
        w.spawner.script_resume = ["silent"]
        res = w.loop.replace_main(hid)
        self.assertEqual(res["recovery"]["outcome"], "RESUME_UNCONFIRMED")
        self.assertEqual(w.main()["status"], "NEEDS_ATTENTION")

    def test_supervisor_gone_host_resumes_the_old_session_by_itself(self):
        w = self.world()
        old = w.running()
        w.spawner.script_new = ["raise"]
        new = str(uuid.uuid4())
        p = w.store.directory / "handoffs"
        p.mkdir(exist_ok=True)
        (p / "r.txt").write_text("x\n", encoding="utf-8")
        sup = 4321
        w.env.alive.add(sup)
        cid = main_host.send_command(w.store, "handover", {"old_session_id": old, "new_session_id": new, "model": "opus",
                                                         "prompt_file": str(p / "r.txt")}, 60, supervisor_pid=sup)
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "FAILED")
        self.assertEqual(main_host.read_host(w.store)["state"], "NO_CHILD")
        w.env.alive.discard(sup)                                       # the supervisor dies
        w.advance(6)
        w.pump()
        self.assertTrue(w.spawner.opened[-1]["resume"])
        self.assertEqual(w.spawner.opened[-1]["sid"], old)
        self.assertEqual(len(w.spawner.live()), 1)
        self.assertEqual(main_host.read_host(w.store)["state"], "RUNNING")

    def test_host_recovery_window_expires_without_any_supervisor_action(self):
        w = self.world()
        old = w.running()
        w.spawner.script_new = ["dies"]
        p = w.store.directory / "handoffs"
        p.mkdir(exist_ok=True)
        (p / "r.txt").write_text("x\n", encoding="utf-8")
        main_host.send_command(w.store, "handover", {"old_session_id": old, "new_session_id": str(uuid.uuid4()),
                                                     "model": "opus", "prompt_file": str(p / "r.txt")}, 60)
        w.pump()
        w.pump()                                                       # sees the new child die
        self.assertEqual(main_host.read_host(w.store)["state"], "NO_CHILD")
        w.advance(100)
        w.pump()
        self.assertEqual(main_host.read_host(w.store)["state"], "NO_CHILD")      # still within the window
        w.advance(100)
        w.pump()
        self.assertEqual(main_host.read_host(w.store)["state"], "RUNNING")
        self.assertTrue(w.spawner.opened[-1]["resume"])

    def test_host_self_recovery_failure_ends_the_host_with_an_error(self):
        w = self.world()
        old = w.running()
        w.spawner.script_new = ["raise"]
        w.spawner.script_resume = ["raise"]
        p = w.store.directory / "handoffs"
        p.mkdir(exist_ok=True)
        (p / "r.txt").write_text("x\n", encoding="utf-8")
        main_host.send_command(w.store, "handover", {"old_session_id": old, "new_session_id": str(uuid.uuid4()),
                                                     "model": "opus", "prompt_file": str(p / "r.txt")}, 60)
        w.pump()
        w.advance(200)
        w.pump()
        self.assertEqual(w.host_exit, 1)
        self.assertEqual(main_host.read_host(w.store)["state"], "NEEDS_ATTENTION")
        self.assertEqual([o["resume"] for o in w.spawner.opened], [False, False, True])   # one resume, never a loop


# ------------------------------------------------------------------ spawner and status

class SpawnerAndStatusTests(Base):
    def test_foreground_spawner_inherits_the_terminal_and_the_provider_environment(self):
        calls = []

        def popen(argv, **kw):
            calls.append((argv, kw))
            return mock.Mock(pid=1)
        sp = main_host.ForegroundSpawner(which=lambda n: "C:/bin/claude.exe", popen=popen, os_name="nt")
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://gateway.invalid", "CLAUDECODE": "1"}):
            sp.open(["claude", "--model", "opus"], "C:/repo")
            expected = provider_env.child_env()
        argv, kw = calls[0]
        self.assertEqual(argv, ["C:/bin/claude.exe", "--model", "opus"])
        self.assertFalse(kw["shell"])
        for k in ("creationflags", "stdin", "stdout", "stderr"):
            self.assertNotIn(k, kw)                         # no new console, no redirection: the tab is the terminal
        self.assertEqual(kw["cwd"], "C:/repo")
        self.assertEqual(kw["env"], expected)
        self.assertNotIn("ANTHROPIC_BASE_URL", kw["env"])

    def test_foreground_spawner_refuses_cmd_shims_and_missing_claude(self):
        with self.assertRaises(LaunchError):
            main_host.ForegroundSpawner(which=lambda n: "C:/bin/claude.cmd", popen=None, os_name="nt").open(["claude"], "x")
        with self.assertRaises(LaunchError):
            main_host.ForegroundSpawner(which=lambda n: None, popen=None, os_name="nt").preflight("claude")

    def test_status_shows_the_host_in_attached_mode_only(self):
        w = self.world()
        w.running()
        code, out = w.env.cli("status", "--json")
        data = json.loads(out)
        self.assertEqual(data["main_launch_mode"], "attached")
        self.assertTrue(data["main_host"]["heartbeat_fresh"])
        self.assertEqual(data["main_host"]["state"], "RUNNING")
        env = Env(self)
        code, out = env.cli("status", "--json")
        self.assertIsNone(json.loads(out)["main_host"])

    def test_build_main_argv_resume_keeps_every_safeguard(self):
        prof = launch.MainProfile(model="opus", permission_mode="auto")
        sid = str(uuid.uuid4())
        argv = launch.build_main_argv(prof, sid, "p.txt", resume=True)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(argv[-1], "auto")
        with self.assertRaises(LaunchError):
            launch.build_main_argv(launch.MainProfile(model="opus", permission_mode="bypassPermissions"), sid, "p", resume=True)
        self.assertEqual(launch.build_main_argv(prof, sid, "p.txt")[-1].startswith("Read the appended"), True)


# ====================================================================== safety-fix pass (review findings B1, I2-I8)

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]


def _spec(new_sid):
    return lifecycle.LaunchSpec(argv=["claude", "--model", "opus", "--session-id", new_sid], cwd=".", stdin_path=None,
                                stdout_path="", stderr_path="", max_runtime_s=1.0)


def _session_file(w, pid, cwd, name=None, sid=None):
    """A live Claude Code session file (machine-wide, outside every state directory)."""
    w.env.alive.add(pid)
    now = int(time.time() * 1000)
    doc = {"pid": pid, "sessionId": sid or str(uuid.uuid4()), "cwd": cwd, "status": "idle", "statusUpdatedAt": now,
           "startedAt": now}
    if name is not None:
        doc["name"] = name
    (w.env.sessions / f"{pid}.json").write_text(json.dumps(doc), encoding="utf-8")


# ------------------------------------------------------------------ I4: the lock

class LockTests(Base):
    def test_concurrent_acquisition_has_exactly_one_winner(self):
        w = self.world()
        for rnd in range(5):
            hosts = [w.new_host(pid=9100 + i) for i in range(8)]
            barrier, results, guard = threading.Barrier(len(hosts)), [], threading.Lock()

            def go(h):
                barrier.wait()
                try:
                    h.acquire()
                    outcome = "won"
                except LaunchError:
                    outcome = "refused"
                with guard:
                    results.append(outcome)

            threads = [threading.Thread(target=go, args=(h,)) for h in hosts]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertEqual(sorted(results), ["refused"] * 7 + ["won"], (rnd, results))
            for h in hosts:
                h.release()
            self.assertTrue(lock_is_free(w.store))

    def test_concurrent_takeover_of_a_stale_lock_has_exactly_one_winner(self):
        w = self.world()
        (w.store.directory / main_host.HOST_LOCK).write_text(json.dumps({"host_id": "dead", "pid": 4242}))
        hosts = [w.new_host(pid=9100 + i) for i in range(8)]
        barrier, results, guard = threading.Barrier(len(hosts)), [], threading.Lock()

        def go(h):
            barrier.wait()
            try:
                h.acquire()
                outcome = "won"
            except LaunchError:
                outcome = "refused"
            with guard:
                results.append(outcome)

        threads = [threading.Thread(target=go, args=(h,)) for h in hosts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(results), ["refused"] * 7 + ["won"], results)
        events, _ = w.store.read_events()
        self.assertEqual(sum(e["kind"] == "main_host_stale_lock_taken_over" for e in events), 1)
        for h in hosts:
            h.release()

    def test_a_lock_owner_record_with_a_live_looking_pid_does_not_wedge_the_lock(self):
        w = self.world()          # pid reuse: the recorded owner pid is alive, but nobody HOLDS the lock
        (w.store.directory / main_host.HOST_LOCK).write_text(json.dumps({"host_id": "old", "pid": os.getpid()}))
        h = w.new_host(pid=9100)
        h.acquire()
        self.assertTrue(h._locked)
        h.release()

    def test_orderly_release_leaves_no_owner_record_and_a_dead_host_leaves_one(self):
        w = self.world()
        h = w.new_host()
        h.acquire()
        h.release()
        self.assertEqual((w.store.directory / main_host.HOST_LOCK).read_bytes(), b"")
        h2 = w.new_host(pid=9100)
        h2.acquire()
        h2.simulate_process_death()                         # the OS drops the lock, the owner record stays
        h3 = w.new_host(pid=9101)
        h3.acquire()                                        # no takeover race, no manual cleanup
        events, _ = w.store.read_events()
        self.assertTrue(any(e["kind"] == "main_host_stale_lock_taken_over" for e in events))
        h3.release()


class RealProcessTests(unittest.TestCase):
    """Real Windows/POSIX subprocesses: Python sleepers, never claude. Not Offline: Popen is the real one."""

    def test_the_lock_is_released_by_the_death_of_a_real_process(self):
        tmp = Path(tempfile.mkdtemp(prefix="l8-lock-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(tmp), True))
        lock = tmp / main_host.HOST_LOCK
        code = ("import os,sys,time; sys.path.insert(0, sys.argv[1]); from dev_supervisor import main_host; "
                "fd=os.open(sys.argv[2], os.O_CREAT|os.O_RDWR|getattr(os,'O_BINARY',0)); "
                "assert main_host._os_lock(fd); print('locked', flush=True); time.sleep(120)")
        p = subprocess.Popen([sys.executable, "-B", "-c", code, str(ORCHESTRATOR_DIR), str(lock)],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait(10), p.stdout.close()))
        self.assertEqual(p.stdout.readline().strip(), "locked")
        store = types.SimpleNamespace(directory=tmp)
        self.assertFalse(lock_is_free(store))               # a live process holds it
        p.kill()
        p.wait(10)
        self.assertTrue(lock_is_free(store))                # ... and the OS released it when that process died

    def test_the_host_stops_a_real_child_and_records_the_exit(self):
        env = Env(self, main_launch_mode="attached")
        store = env.svc.store()
        host = MainHost(store, env.cfg, main_host.ForegroundSpawner(which=lambda exe: sys.executable),
                        root=str(env.repo), pid_alive=lifecycle.pid_alive, out=io.StringIO())
        sid = str(uuid.uuid4())
        host._spawn([sys.executable, "-B", "-c", "import time; time.sleep(120)"], sid, "start")
        handle = host.child["handle"]
        self.addCleanup(lambda: handle.poll() is None and handle.kill())
        pid = host.child["pid"]
        self.assertTrue(lifecycle.pid_alive(pid))
        self.assertTrue(host._stop_child("test"))
        self.assertFalse(lifecycle.pid_alive(pid))
        self.assertIsNone(host.child)
        self.assertIsNotNone(host.ended[sid]["exit_code"])
        self.assertEqual(host.ended[sid]["pid"], pid)


# ------------------------------------------------------------------ I2: write-ahead and interruptions

class InterruptTests(Base):
    def _setup(self):
        w = self.world()
        old = w.running()
        new = str(uuid.uuid4())
        cid = main_host.send_command(w.store, "handover", {"old_session_id": old, "new_session_id": new,
                                                           "model": "opus"}, 60)
        return w, old, new, cid

    def _old_pid(self, w):
        return w.main()["pid"]

    def _no_unrecoverable_state(self, w, old, cid):
        """The invariant: never a stopped child without durable recovery information, never two Mains, and the
        command is not left half-open."""
        host = main_host.read_host(w.store)
        child = host.get("child")
        running = child is not None and w.env.svc.pid_alive(child["pid"])
        self.assertTrue(running or host.get("recover_session_id") == old, host)
        self.assertLessEqual(len(w.spawner.live()), 1)
        self.assertIn(main_host.read_ack(w.store, cid)["status"], main_host.TERMINAL_ACKS)
        return host

    def _then_the_host_recovers_the_old_session(self, w, old):
        w.advance(200)
        w.host.tick()
        self.assertEqual(len(w.spawner.live()), 1)
        self.assertTrue(w.spawner.opened[-1]["resume"])
        self.assertEqual(w.spawner.opened[-1]["sid"], old)

    def test_the_recovery_record_is_durable_before_the_old_child_is_touched(self):
        w, old, new, cid = self._setup()
        seen = {}
        child = w.spawner.handles[self._old_pid(w)]
        real_terminate = child.terminate

        def spying_terminate():
            doc = main_host.read_host(w.store)         # what is on DISK at the moment the old Main is touched
            seen.update(recover=doc.get("recover_session_id"), stage=(doc.get("transition") or {}).get("stage"))
            real_terminate()

        child.terminate = spying_terminate
        w.host.tick()
        self.assertEqual(seen, {"recover": old, "stage": "STOPPING"})
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "STARTED")

    def test_ctrl_c_during_preflight_changes_nothing(self):
        w, old, new, cid = self._setup()
        w.spawner.fail_preflight = "interrupt"
        w.host.tick()
        host = self._no_unrecoverable_state(w, old, cid)
        self.assertEqual(host["child"]["session_id"], old)
        self.assertFalse(w.spawner.handles[self._old_pid(w)].terminated)
        self.assertIsNone(host["recover_session_id"])
        self.assertEqual(len(w.spawner.opened), 1)

    def test_ctrl_c_while_saving_the_recovery_record_leaves_the_old_main_running(self):
        w, old, new, cid = self._setup()
        real, calls = w.host.publish, []

        def boom(force=False):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt()
            return real(force)

        with mock.patch.object(w.host, "publish", boom):
            w.host.tick()
        host = self._no_unrecoverable_state(w, old, cid)
        self.assertEqual(host["child"]["session_id"], old)
        self.assertFalse(w.spawner.handles[self._old_pid(w)].terminated)
        self.assertIsNone(host["recover_session_id"])

    def test_ctrl_c_while_stopping_the_old_main_undoes_the_write_ahead(self):
        w, old, new, cid = self._setup()
        w.spawner.handles[self._old_pid(w)].raise_on_terminate = KeyboardInterrupt()
        w.host.tick()
        host = self._no_unrecoverable_state(w, old, cid)
        self.assertEqual(host["child"]["session_id"], old)
        self.assertIsNone(host["recover_session_id"])
        self.assertEqual(len(w.spawner.opened), 1)

    def test_ctrl_c_after_the_old_main_stopped_before_the_new_one_started(self):
        w, old, new, cid = self._setup()
        real, calls = w.host.publish, []

        def boom(force=False):
            calls.append(1)
            if len(calls) == 2:                          # the publish that follows the stop
                raise KeyboardInterrupt()
            return real(force)

        with mock.patch.object(w.host, "publish", boom):
            w.host.tick()
        host = self._no_unrecoverable_state(w, old, cid)
        self.assertIsNone(host["child"])
        self.assertEqual(host["recover_session_id"], old)
        self.assertEqual(host["state"], "NO_CHILD")
        self._then_the_host_recovers_the_old_session(w, old)

    def test_ctrl_c_while_starting_the_new_main(self):
        w, old, new, cid = self._setup()
        w.spawner.script_new = ["interrupt"]
        w.host.tick()
        host = self._no_unrecoverable_state(w, old, cid)
        self.assertIsNone(host["child"])
        self.assertEqual(host["recover_session_id"], old)
        self._then_the_host_recovers_the_old_session(w, old)

    def test_ctrl_c_after_the_new_main_started_but_before_the_answer(self):
        w, old, new, cid = self._setup()
        real = main_host.write_ack

        def flaky(store, c, status, **kw):
            if status == "STARTED":
                raise KeyboardInterrupt()
            return real(store, c, status, **kw)

        with mock.patch.object(main_host, "write_ack", flaky):
            w.host.tick()
        host = self._no_unrecoverable_state(w, old, cid)
        self.assertEqual(host["child"]["session_id"], new)      # exactly one Main: the new one
        self.assertEqual(len(w.spawner.live()), 1)

    def test_a_real_sigint_is_held_off_during_the_handover(self):
        self.assertIs(threading.current_thread(), threading.main_thread())
        w, old, new, cid = self._setup()
        w.spawner.script_new = ["sigint"]
        before = signal.getsignal(signal.SIGINT)
        w.host.tick()                                    # would raise KeyboardInterrupt without the shield
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "STARTED")
        self.assertEqual(w.host.sigint_held, 1)
        self.assertIs(signal.getsignal(signal.SIGINT), before)      # the previous handler is restored
        self.assertEqual(len(w.spawner.live()), 1)


# ------------------------------------------------------------------ I3: transient and corrupt coordination state

class ReadFailureTests(Base):
    def test_transient_read_errors_are_retried(self):
        w = self.world()
        w.start_host()
        real, n = w.store.read, {"c": 0}

        def flaky(name, default=None):
            if name == main_host.HOST_DOC and n["c"] < 2:
                n["c"] += 1
                raise PermissionError(13, "sharing violation")
            return real(name, default)

        with mock.patch.object(w.store, "read", flaky):
            doc, ok = main_host.read_host_ex(w.store, sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(doc["state"], "RUNNING")

    def test_a_persistent_read_error_means_unreadable_not_no_host(self):
        w = self.world()
        w.start_host()
        with mock.patch.object(w.store, "read", side_effect=PermissionError(13, "denied")):
            doc, ok = main_host.read_host_ex(w.store, sleep=lambda s: None)
            ch = main_host.HostChannel(w.store, w.env.cfg, clock=w.time, sleep=lambda s: None,
                                       pid_alive=w.env.svc.pid_alive)
            self.assertFalse(ch.fresh())
            self.assertFalse(ch.read_ok)
        self.assertEqual((doc, ok), ({}, False))

    def test_the_supervisor_loop_survives_a_transient_coordination_error(self):
        w = self.world()
        w.running()
        with mock.patch.object(w.loop.attached, "reconcile", side_effect=PermissionError(13, "sharing violation")):
            t = w.loop.tick()                               # does not raise
        self.assertIn("tick", t)
        self.assertEqual(w.loop.attached.reconcile()["state"], "adopted")
        events, _ = w.store.read_events()
        self.assertTrue(any(e["kind"] == "main_reconcile_error" for e in events))

    def test_a_corrupt_host_record_is_survived_and_healed_by_the_next_heartbeat(self):
        w = self.world()
        w.running()
        w.store.path(main_host.HOST_DOC).write_bytes(b"\x00 not a document")
        bak = w.store.backup_path(main_host.HOST_DOC)
        if bak.exists():
            bak.unlink()
        with self.assertRaises(StoreCorruptError):
            w.store.read(main_host.HOST_DOC)
        w.loop.tick()                                       # the supervisor does not die
        self.assertEqual(main_host.read_host_ex(w.store, sleep=lambda s: None), ({}, False))
        w.host.publish(force=True)                          # the host heartbeat rewrites it
        self.assertEqual(main_host.read_host_ex(w.store, sleep=lambda s: None)[1], True)
        self.assertEqual(main_host.read_host(w.store)["state"], "RUNNING")

    def test_the_host_survives_failing_ticks_and_never_abandons_its_child(self):
        w = self.world()
        host = w.new_host()
        real, n = host.tick, {"c": 0}

        def flaky():
            n["c"] += 1
            if n["c"] <= 5:
                raise OSError("sharing violation")
            return real()

        host.tick = flaky
        self.assertEqual(host.run(max_ticks=2), 0)
        self.assertEqual(len(w.spawner.live()), 1)           # the interactive child is still running
        events, _ = w.store.read_events()
        self.assertEqual(sum(e["kind"] == "main_host_tick_error" for e in events), 5)

    def test_a_host_with_no_child_gives_up_after_repeated_failures(self):
        w = self.world()
        host = w.new_host()

        def bad():
            host.child = None
            raise OSError("disk gone")

        host.tick = bad
        self.assertEqual(host.run(), 1)
        self.assertEqual(host.exit_code, 1)
        self.assertTrue(lock_is_free(w.store))
        events, _ = w.store.read_events()
        self.assertEqual(sum(e["kind"] == "main_host_tick_error" for e in events), main_host.FATAL_AFTER_ERRORS)

    def test_an_abnormal_host_exit_ends_its_child_instead_of_stranding_it(self):
        w = self.world()
        host = w.new_host()

        def die():
            raise SystemExit(3)

        host.tick = die
        with self.assertRaises(SystemExit):
            host.run()
        self.assertTrue(all(h.terminated for h in w.spawner.handles.values()))
        self.assertEqual(w.spawner.live(), [])
        self.assertTrue(lock_is_free(w.store))

    def test_a_corrupt_command_file_is_rejected_only_after_repeated_looks(self):
        w = self.world()
        w.start_host()
        d = w.store.directory / main_host.COMMANDS_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / "000000000000001-bad00000.json").write_text("{not json", encoding="utf-8")
        cid = "000000000000001-bad00000"
        for _ in range(main_host.BAD_COMMAND_READS - 1):
            w.host.tick()
            self.assertIsNone(main_host.read_ack(w.store, cid))
        w.host.tick()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "REJECTED")
        self.assertEqual(len(w.spawner.live()), 1)

    def test_a_command_that_cannot_be_read_right_now_is_not_lost(self):
        w = self.world()
        w.start_host()
        cid = main_host.send_command(w.store, "stop_child", {"pid": 1, "session_id": str(uuid.uuid4())}, 600)
        real = main_host._read_json_ex

        def locked(path):
            if path.name == f"{cid}.json":
                return None, "transient"
            return real(path)

        with mock.patch.object(main_host, "_read_json_ex", locked):
            for _ in range(6):
                w.host.tick()
        self.assertIsNone(main_host.read_ack(w.store, cid))          # skipped, not rejected, not lost
        w.host.tick()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "REJECTED")      # processed once readable


# ------------------------------------------------------------------ B1 / I5: duplicate prevention

class GuardTests(Base):
    def test_a_live_recorded_pid_blocks_the_host_whatever_the_status(self):
        for status in ("NEEDS_ATTENTION", "IDENTITY_MISMATCH", "AWAITING_HOST", "EXITED", "STARTING", "RUNNING"):
            with self.subTest(status=status):
                w = self.world()
                w.env.alive.add(5555)
                launch.write_main(w.store, session_id=str(uuid.uuid4()), status=status, pid=5555)
                with self.assertRaises(LaunchError) as cm:
                    w.new_host().start()
                self.assertIn("duplicate", str(cm.exception))
                self.assertEqual(w.spawner.opened, [])
                self.assertTrue(lock_is_free(w.store))
                self.assertEqual(launch.read_main(w.store)["pid"], 5555)          # the record was not touched

    def test_awaiting_host_never_erases_a_live_pid(self):
        w = self.world()
        w.env.alive.add(5555)
        sid = str(uuid.uuid4())
        launch.write_main(w.store, session_id=sid, status="NEEDS_ATTENTION", pid=5555)
        w.make_loop()
        main = w.loop.ensure_main()
        self.assertEqual((main["pid"], main["status"]), (5555, "NEEDS_ATTENTION"))
        self.assertEqual(launch.read_main(w.store)["pid"], 5555)
        events, _ = w.store.read_events()
        self.assertTrue(any(e["kind"] == "main_awaiting_host_kept_live_pid" for e in events))

    def test_a_live_main_session_in_this_repository_blocks_even_when_it_is_recorded_nowhere(self):
        w = self.world()                    # empty state directory: main.json knows nothing (a different state dir)
        _session_file(w, 7001, str(w.env.repo), launch.SESSION_NAME)
        with self.assertRaises(LaunchError) as cm:
            w.new_host().start()
        self.assertIn("already runs in this repository", str(cm.exception))
        self.assertEqual(w.spawner.opened, [])
        self.assertTrue(lock_is_free(w.store))

    def test_the_same_name_in_a_throwaway_repository_does_not_block(self):
        w = self.world()
        _session_file(w, 7002, str(w.env.tmp / "throwaway-repo"), launch.SESSION_NAME)
        w.new_host().start()
        self.assertEqual(len(w.spawner.opened), 1)

    def test_the_users_own_unnamed_or_differently_named_session_does_not_block(self):
        w = self.world()
        _session_file(w, 7003, str(w.env.repo))
        _session_file(w, 7004, str(w.env.repo), "my-own-session")
        w.new_host().start()
        self.assertEqual(len(w.spawner.opened), 1)

    def test_a_dead_session_file_does_not_block(self):
        w = self.world()
        _session_file(w, 7005, str(w.env.repo), launch.SESSION_NAME)
        w.env.alive.discard(7005)
        w.new_host().start()
        self.assertEqual(len(w.spawner.opened), 1)

    def test_the_explicit_override_also_bypasses_the_session_scan(self):
        w = self.world()
        _session_file(w, 7006, str(w.env.repo), launch.SESSION_NAME)
        w.new_host().start(ignore_recorded=True)
        self.assertEqual(len(w.spawner.opened), 1)

    def test_unreadable_records_fail_closed(self):
        w = self.world()
        with mock.patch.object(w.store, "read", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(LaunchError) as cm:
                w.new_host().start()
        self.assertIn("could not be read", str(cm.exception))
        self.assertEqual(w.spawner.opened, [])

    def test_dry_run_prints_the_isolation_facts_and_the_initial_prompt_override_is_validated(self):
        w = self.world(main_initial_prompt="Reply with OK and stop.")
        w.env.svc.main_spawner = w.spawner
        code, out = w.env.cli("main-host")
        self.assertEqual(code, 0, out)
        for key in ("isolation", "repo_root", "state_directory", "model", "initial_prompt", "Reply with OK and stop."):
            self.assertIn(key, out)
        w.start_host()
        self.assertEqual(w.spawner.opened[0]["argv"][-1], "Reply with OK and stop.")
        for bad in ("", "  ", "line\nbreak", "-dash", "x" * 301, 5):
            with self.assertRaises(cfgmod.ConfigError, msg=repr(bad)):
                cfgmod.from_dict({"main_initial_prompt": bad})
        self.assertEqual(cfgmod.Config().main_initial_prompt, None)

    def test_the_host_record_names_the_provider_label_only(self):
        w = self.world()
        w.start_host()
        doc = main_host.read_host(w.store)
        self.assertIn(doc["provider"], ("pro", "inherit", "invalid"))
        self.assertNotIn("TOKEN", json.dumps(doc).upper())


# ------------------------------------------------------------------ I6: adoption never retires a live Main

class AdoptionTests(Base):
    def test_adoption_never_marks_a_still_live_main_as_retired(self):
        w = self.world()
        x = str(uuid.uuid4())
        w.env.alive.add(5555)
        launch.write_main(w.store, session_id=x, status="RUNNING", pid=5555)
        replacement.register_session(w.store, x, launch.MAIN_ROLE)
        w.start_host(ignore_recorded=True)          # the explicit override: the invisible Main is still alive
        w.make_loop()
        r = w.loop.attached.reconcile()
        self.assertEqual(r["state"], "adopted")
        rec = w.sessions()[x]
        self.assertEqual(rec["status"], replacement.SessionStatus.RETIRE_FAILED)
        self.assertFalse(rec["authoritative"])
        kinds = [e["kind"] for e in w.store.read_events()[0]]
        self.assertIn("main_adopted_while_other_main_alive", kinds)
        self.assertIn("session_retire_unconfirmed", kinds)
        self.assertTrue(w.env.svc.pid_alive(5555))                       # nothing was signalled

    def test_adoption_retires_the_old_record_only_with_host_recorded_evidence(self):
        w = self.world()
        old = w.running()
        res = w.loop.replace_main(w.handoff())
        self.assertEqual(res["status"], replacement.ReplacementStatus.REPLACED)
        self.assertIn(old, main_host.read_host(w.store)["ended_children"])
        self.assertEqual(w.sessions()[old]["status"], replacement.SessionStatus.RETIRED)


# ------------------------------------------------------------------ I7: settle

class SettleTests(Base):
    def test_a_lost_settle_acknowledgement_is_reported_and_retried(self):
        w = self.world()
        w.running()
        hid = w.handoff()
        with mock.patch.object(MainHost, "_cmd_settle", lambda self, cid, p: None):         # the host never answers
            res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.REPLACED)
        self.assertEqual(res["settle"], "UNCONFIRMED")
        self.assertEqual(w.loop.attached.pending_settle, res["new_session_id"])
        self.assertIsNotNone(main_host.read_host(w.store)["recover_session_id"])
        w.loop.tick()                                       # the retry reaches the real handler
        self.assertIsNone(w.loop.attached.pending_settle)
        self.assertIsNone(main_host.read_host(w.store)["recover_session_id"])

    def test_the_user_quitting_the_new_main_inside_the_window_never_resurrects_the_old_session(self):
        w = self.world()
        w.running()
        hid = w.handoff()
        with mock.patch.object(MainHost, "_cmd_settle", lambda self, cid, p: None):         # settle was lost
            res = w.loop.replace_main(hid)
        self.assertEqual(res["status"], replacement.ReplacementStatus.REPLACED)
        new_pid = w.main()["pid"]
        self.assertTrue(w.host.child["confirmed"])          # the host SAW the new Main running
        w.spawner.handles[new_pid].end(0)                   # the user exits it on purpose
        w.advance(5)
        w.pump()
        self.assertEqual(w.host_exit, 0)
        w.advance(400)
        w.pump()
        self.assertEqual([o["resume"] for o in w.spawner.opened], [False, False])       # no resume was ever started
        self.assertEqual(w.spawner.live(), [])

    def test_a_settle_that_arrives_after_an_unobserved_exit_still_prevents_resurrection(self):
        w = self.world()
        w.running()
        hid = w.handoff()
        with mock.patch.object(MainHost, "_observe_confirmation", lambda self: None),                 mock.patch.object(MainHost, "_cmd_settle", lambda self, cid, p: None):
            res = w.loop.replace_main(hid)
            self.assertEqual(res["settle"], "UNCONFIRMED")
            w.spawner.handles[w.main()["pid"]].end(0)       # the user quits before the host ever noticed it running
            w.advance(3)
            w.pump()
            self.assertEqual(main_host.read_host(w.store)["state"], "NO_CHILD")      # inside the recovery window
        w.loop.attached.retry_settle()                      # the late settle reaches the real handler
        self.assertIsNone(main_host.read_host(w.store)["recover_session_id"])      # recovery information dropped
        w.loop.attached.retry_settle()                      # the host has ended meanwhile: nothing left to settle
        self.assertIsNone(w.loop.attached.pending_settle)
        w.advance(400)
        w.pump()
        self.assertEqual([o["resume"] for o in w.spawner.opened], [False, False])       # nothing was resurrected
        self.assertEqual(w.spawner.live(), [])


# ------------------------------------------------------------------ I8: late acknowledgements

class LateAckTests(Base):
    def test_a_handover_that_outlives_the_supervisor_wait_is_adopted_not_reported_as_failed(self):
        env = Env(self, main_launch_mode="attached", host_command_timeout_s=0.4)
        env.svc.clock, env.svc.sleep = time.time, time.sleep
        env.alive.update({os.getpid(), HOST_PID})
        spawner = FakeSpawner(env)
        spawner.slow_open_s = 1.0                           # the host needs longer than the supervisor waits
        store = env.svc.store()
        host = MainHost(store, env.cfg, spawner, root=str(env.repo), pid_alive=env.svc.pid_alive, host_pid=HOST_PID,
                        out=io.StringIO(), poll_s=0.02, read_sessions=env.svc.read_sessions)
        host.start()
        old = host.child["session_id"]
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    host.tick()
                except Exception:       # noqa: BLE001
                    pass
                time.sleep(0.02)

        th = threading.Thread(target=loop, daemon=True)
        th.start()
        self.addCleanup(th.join, 10)
        self.addCleanup(stop.set)
        att = main_host.AttachedMain(store, env.cfg, read_sessions=env.svc.read_sessions, pid_alive=env.svc.pid_alive,
                                     clock=time.time, sleep=time.sleep)
        att.launcher.begin(old, "h1")
        new = str(uuid.uuid4())
        with mock.patch.object(main_host, "HANDOVER_GRACE_S", 10.0):
            handle = att.launcher.launch(_spec(new))
        self.assertEqual(len(att.launcher.cancelled), 1)          # the supervisor DID ask the host to cancel ...
        self.assertEqual(handle.pid, host.child["pid"])           # ... the host was already past the point of no return
        self.assertEqual(host.child["session_id"], new)
        self.assertEqual(len(spawner.live()), 1)

    def test_a_cancel_is_honoured_before_the_old_main_is_touched(self):
        w = self.world()
        old = w.running()
        w.host_alive = False                                 # the host is busy and does not tick
        att = w.loop.attached
        att.launcher.begin(old, "h1")
        with mock.patch.object(main_host, "HANDOVER_GRACE_S", 5.0):
            with self.assertRaises(LaunchError) as cm:
                att.launcher.launch(_spec(str(uuid.uuid4())))
        self.assertIn("UNANSWERED", str(cm.exception))
        cid = att.launcher.cancelled[0]
        w.host_alive = True
        w.pump()
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "CANCELLED")
        self.assertEqual(len(w.spawner.opened), 1)
        self.assertFalse(any(h.terminated for h in w.spawner.handles.values()))
        self.assertEqual(main_host.read_host(w.store)["child"]["session_id"], old)

    def test_the_reported_result_agrees_with_the_authoritative_main(self):
        w = self.world()
        old = w.running()
        hid = w.handoff()
        new = str(uuid.uuid4())
        cid = main_host.send_command(w.store, "handover", {"old_session_id": old, "new_session_id": new,
                                                           "model": "opus"}, 60)
        w.pump()                                             # the host completes the handover on its own
        self.assertEqual(main_host.read_ack(w.store, cid)["status"], "STARTED")
        rolled = replacement.ReplacementResult(status=replacement.ReplacementStatus.ROLLED_BACK, old_session_id=old,
                                               authoritative_session_id=old, handoff_id=hid)
        with mock.patch.object(cli.replacement, "replace_session", return_value=rolled):
            res = w.loop.replace_main(hid)                   # the supervisor had given up and says ROLLED_BACK
        self.assertEqual(res["status"], "REPLACED_LATE_HANDOVER")
        self.assertEqual(res["authoritative_session_id"], new)
        self.assertEqual(res["recovery"]["outcome"], "HOST_HAS_OTHER_MAIN")
        self.assertEqual((w.main()["session_id"], w.main()["status"]), (new, "RUNNING"))
        self.assertEqual(w.sessions()[old]["status"], replacement.SessionStatus.RETIRED)
        self.assertEqual(len(w.spawner.live()), 1)

    def test_an_unreadable_host_record_after_a_failed_replacement_is_not_declared_a_failure_of_the_old_main(self):
        w = self.world()
        old = w.running()
        with mock.patch.object(w.store, "read", side_effect=PermissionError(13, "denied")):
            with mock.patch.object(w.loop.attached, "sleep", lambda s: None):
                out = w.loop.attached.recover_after_failed_replace(old)
        self.assertEqual(out["outcome"], "HOST_STATE_UNREADABLE")
        self.assertNotEqual(w.sessions()[old]["status"], replacement.SessionStatus.FAILED)


class AuthoritativeMainTests(unittest.TestCase):
    """A dead Main must never be reported as authoritative, whatever its record says (review of PR #118)."""

    def _store(self, tmp):
        from dev_supervisor.state_store import StateStore
        return StateStore(tmp, repo_roots=[], clock=lambda: 1.0, pid_alive=lambda pid: False, create=True)

    def test_dead_main_in_awaiting_host_is_not_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            sid = str(uuid.uuid4())
            replacement.register_session(store, sid, "main")          # ACTIVE and authoritative, process gone
            launch.write_main(store, session_id=sid, status="AWAITING_HOST", pid=None)
            self.assertEqual(replacement.load_sessions(store)["sessions"][sid]["status"], "ACTIVE")
            self.assertIsNone(cli._authoritative_main(store))

    def test_dead_main_marked_exited_is_not_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            sid = str(uuid.uuid4())
            replacement.register_session(store, sid, "main")
            launch.write_main(store, session_id=sid, status="EXITED", pid=None)
            self.assertIsNone(cli._authoritative_main(store))

    def test_running_main_is_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            sid = str(uuid.uuid4())
            replacement.register_session(store, sid, "main")
            launch.write_main(store, session_id=sid, status="RUNNING", pid=None)
            self.assertEqual(cli._authoritative_main(store), sid)


if __name__ == "__main__":
    unittest.main()
