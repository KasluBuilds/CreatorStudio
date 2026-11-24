"""Development supervisor: state store, worker registry and lifecycle (T-DEV-002).

Offline only: synthetic data, temp dirs, a fake launcher and a fake clock; sockets are blocked and no real process is
started. Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_lifecycle.py -v
"""
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import state_store as ss  # noqa: E402
from dev_supervisor.lifecycle import (BackoffActive, ClaudeCliLauncher, GitWorktreeChecker, LaunchHandle, LaunchSpec,  # noqa: E402
                                      LifecycleError, NotOwnedError, Supervisor, SupervisorPolicy, WorkerBlocked,
                                      WorktreeCheckFailed, build_claude_argv)
from dev_supervisor.state_store import (LockHeldError, SchemaError, StateStore, StoreCorruptError,  # noqa: E402
                                        StoreError, UnsafeStoreLocation, scrub)
from dev_supervisor.workers import (DuplicateWorkerError, ResultStatus, Worker, WorkerRegistry,  # noqa: E402
                                    WorkerStatus)

T0 = 1_800_000_000.0
# Credential-shaped strings are assembled at runtime so no literal credential appears in the repository.
FAKE_KEY = "sk-" + "ant-" + "x" * 24
FAKE_GH = "gh" + "p_" + "A" * 30


def _no_net(*a, **k):
    raise AssertionError("network access attempted")


class FakeClock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeLauncher:
    """Records launches; processes run until a test sets their exit code."""

    def __init__(self, exit_on_terminate=True, fail_with=None):
        self.next_pid = 1000
        self.procs = {}
        self.launches = []
        self.terminated, self.killed = [], []
        self.exit_on_terminate = exit_on_terminate
        self.fail_with = fail_with
        self.on_launch = None

    def launch(self, spec):
        if self.on_launch:
            self.on_launch(spec)
        if self.fail_with:
            raise self.fail_with
        self.next_pid += 1
        self.procs[self.next_pid] = None
        self.launches.append(spec)
        return LaunchHandle(pid=self.next_pid)

    def owns(self, pid):
        return pid in self.procs

    def poll(self, pid):
        return self.procs[pid]

    def terminate(self, pid):
        self.terminated.append(pid)
        if self.exit_on_terminate and self.procs[pid] is None:
            self.procs[pid] = 0

    def kill(self, pid):
        self.killed.append(pid)
        if self.procs[pid] is None:
            self.procs[pid] = -9

    def exit(self, pid, code):
        self.procs[pid] = code


def ok_checker(worktree, branch):
    return True, "ok"


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(socket, "socket", _no_net)
        p.start()
        self.addCleanup(p.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.prompt = self.tmp / "prompt.md"
        self.prompt.write_text("synthetic task prompt", encoding="utf-8")
        self.clock = FakeClock()
        self.alive = set()

    def store(self, **kw):
        kw.setdefault("clock", self.clock)
        kw.setdefault("sleep", lambda s: None)
        return StateStore(self.state_dir, **kw)

    def sup(self, launcher=None, policy=None, verifier=None, store=None, checker=ok_checker, **kw):
        self.launcher = launcher or FakeLauncher()
        sessions = iter(str(uuid.UUID(int=i + 1)) for i in range(10_000))
        s = Supervisor(store or self.store(), self.launcher, worktree_checker=checker, clock=self.clock,
                       sleep=lambda s: self.clock.advance(s), policy=policy or SupervisorPolicy(),
                       verifier=verifier, pid_probe=lambda pid: pid in self.alive,
                       session_factory=lambda: next(sessions), activity_probe=kw.pop("activity_probe", lambda w: None),
                       **kw)
        self.addCleanup(s.close)
        return s

    def assign(self, s, task="T-SYN-001", wt=None, branch="lane/syn/L1"):
        return s.assign(task_id=task, role="lane", branch=branch, worktree=str(wt or self.wt),
                        owner_stream="claude:synthetic", model="auto/coding", provider="gemini",
                        prompt_file=str(self.prompt))

    def running(self, s, **kw):
        w = self.assign(s, **kw)
        return s.spawn(w.worker_id)


# ====================================================================== state store

class TestLocation(Base):
    def test_default_directory_windows_uses_localappdata(self):
        d = ss.default_directory(env={"LOCALAPPDATA": "C:\\LAD"}, os_name="nt")
        self.assertEqual(d, Path("C:\\LAD") / "CreatorStudio" / "dev_supervisor")

    def test_default_directory_posix_uses_xdg_state(self):
        d = ss.default_directory(env={"XDG_STATE_HOME": "/xs"}, os_name="posix")
        self.assertEqual(d, Path("/xs") / "creatorstudio" / "dev_supervisor")

    def test_env_override(self):
        d = ss.default_directory(env={ss.ENV_DIR: str(self.tmp / "o")}, os_name="nt")
        self.assertEqual(d, self.tmp / "o")

    def test_default_directory_is_outside_this_repository(self):
        root = ss.this_repository_root()
        self.assertIsNotNone(root)
        self.assertFalse(ss.is_within(ss.default_directory(), root))

    def test_refuses_directory_inside_this_repository(self):
        inside = Path(__file__).resolve().parents[1] / "not_created_state_dir"
        with self.assertRaises(UnsafeStoreLocation):
            StateStore(inside, create=False)
        self.assertFalse(inside.exists())

    def test_refuses_directory_inside_any_git_tree(self):
        repo = self.tmp / "otherrepo"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(UnsafeStoreLocation):
            StateStore(repo / "a" / "b")

    def test_refuses_worktree_git_file(self):
        repo = self.tmp / "worktree"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
        with self.assertRaises(UnsafeStoreLocation):
            StateStore(repo / "state")

    def test_refuses_explicit_repo_root(self):
        with self.assertRaises(UnsafeStoreLocation):
            StateStore(self.tmp / "r" / "s", repo_roots=[self.tmp / "r"])

    def test_invalid_document_name(self):
        st = self.store()
        for bad in ("../x", "A", "a b", "", "x" * 80):
            with self.assertRaises(StoreError):
                st.path(bad)


class TestDocuments(Base):
    def test_roundtrip_and_envelope(self):
        st = self.store()
        st.write("things", {"a": 1, "b": ["x"]})
        self.assertEqual(st.read("things"), {"a": 1, "b": ["x"]})
        env = json.loads(st.path("things").read_text(encoding="utf-8"))
        self.assertEqual(env["schema_version"], ss.SCHEMA_VERSION)
        self.assertTrue(env["checksum"].startswith("sha256:"))
        self.assertEqual(env["kind"], "things")

    def test_read_missing_returns_default(self):
        self.assertEqual(self.store().read("nothing", default={"d": 1}), {"d": 1})

    def test_previous_version_becomes_backup(self):
        st = self.store()
        st.write("doc", {"v": 1})
        self.assertFalse(st.backup_path("doc").exists())
        st.write("doc", {"v": 2})
        self.assertEqual(ss.decode_envelope(st.backup_path("doc").read_bytes(), "doc"), {"v": 1})
        self.assertEqual(st.read("doc"), {"v": 2})

    def test_truncated_file_recovered_from_backup(self):
        st = self.store()
        st.write("doc", {"v": 1})
        st.write("doc", {"v": 2})
        raw = st.path("doc").read_bytes()
        st.path("doc").write_bytes(raw[: len(raw) // 2])  # torn write
        self.assertEqual(st.read("doc"), {"v": 1})
        self.assertEqual(st.recoveries[0]["document"], "doc")
        quarantined = list(self.state_dir.glob("doc.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)  # the damaged file is kept, never deleted
        self.assertEqual(st.read("doc"), {"v": 1})  # restored file is valid now
        kinds = [e["kind"] for e in st.read_events()[0]]
        self.assertIn("store_recovered_from_backup", kinds)

    def test_checksum_mismatch_detected(self):
        st = self.store()
        st.write("doc", {"v": 1})
        st.write("doc", {"v": 2})
        env = json.loads(st.path("doc").read_text(encoding="utf-8"))
        env["data"]["v"] = 99  # hand edit without updating the checksum
        st.path("doc").write_text(json.dumps(env), encoding="utf-8")
        self.assertEqual(st.read("doc"), {"v": 1})

    def test_both_damaged_refuses(self):
        st = self.store()
        st.write("doc", {"v": 1})
        st.write("doc", {"v": 2})
        st.path("doc").write_bytes(b"{")
        st.backup_path("doc").write_bytes(b"garbage")
        with self.assertRaises(StoreCorruptError):
            st.read("doc")

    def test_damaged_without_backup_refuses(self):
        st = self.store()
        st.write("doc", {"v": 1})
        st.path("doc").write_bytes(b"\x00\x00")
        with self.assertRaises(StoreCorruptError):
            st.read("doc")

    def test_missing_main_restored_from_backup(self):
        st = self.store()
        st.write("doc", {"v": 1})
        st.write("doc", {"v": 2})
        st.path("doc").unlink()
        self.assertEqual(st.read("doc"), {"v": 1})
        self.assertTrue(st.path("doc").exists())

    def test_newer_schema_is_refused_not_recovered(self):
        st = self.store()
        st.write("doc", {"v": 1})
        env = json.loads(st.path("doc").read_text(encoding="utf-8"))
        env["schema_version"] = ss.SCHEMA_VERSION + 1
        st.path("doc").write_text(json.dumps(env), encoding="utf-8")
        with self.assertRaises(SchemaError):
            st.read("doc")

    def test_damaged_main_never_overwrites_good_backup(self):
        st = self.store()
        st.write("doc", {"v": 1})
        st.write("doc", {"v": 2})
        st.path("doc").write_bytes(b"{broken")
        st.write("doc", {"v": 3})
        self.assertEqual(ss.decode_envelope(st.backup_path("doc").read_bytes(), "doc"), {"v": 1})
        self.assertEqual(st.read("doc"), {"v": 3})

    def test_crash_during_replace_leaves_original_and_no_temp(self):
        st = self.store()
        st.write("doc", {"v": 1})
        with mock.patch.object(ss.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                st.write("doc", {"v": 2})
        self.assertEqual(st.read("doc"), {"v": 1})
        self.assertEqual([p for p in os.listdir(self.state_dir) if p.endswith(".tmp")], [])

    def test_stale_temp_from_hard_crash_is_removed_on_open(self):
        st = self.store()
        st.write("doc", {"v": 1})
        (self.state_dir / ".doc.json.4242.deadbeef.tmp").write_bytes(b'{"half":')
        st2 = self.store()
        self.assertEqual(st2.stale_temps_removed, 1)
        self.assertEqual(st2.read("doc"), {"v": 1})

    def test_replace_retries_on_sharing_violation(self):
        sleeps = []
        st = self.store(sleep=sleeps.append)
        real = os.replace
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "in use")
            return real(a, b)

        with mock.patch.object(ss.os, "replace", side_effect=flaky):
            st.write("doc", {"v": 1})
        self.assertEqual(st.read("doc"), {"v": 1})
        self.assertEqual(len(sleeps), 2)

    def test_replace_retries_are_bounded(self):
        st = self.store(replace_retries=3)
        with mock.patch.object(ss.os, "replace", side_effect=PermissionError(13, "in use")) as m:
            with self.assertRaises(PermissionError):
                st.write("doc", {"v": 1})
        self.assertEqual(m.call_count, 3)
        self.assertEqual([p for p in os.listdir(self.state_dir) if p.endswith(".tmp")], [])

    def test_long_path_prefix(self):
        long = "C:\\" + "\\".join(["d" * 50] * 6)
        self.assertTrue(ss.fs_path(long, os_name="nt").startswith("\\\\?\\C:\\"))
        self.assertTrue(ss.fs_path("\\\\srv\\share\\" + "e" * 260, os_name="nt").startswith("\\\\?\\UNC\\srv"))
        self.assertEqual(ss.fs_path("C:\\short", os_name="nt"), "C:\\short")
        self.assertEqual(ss.fs_path("/" + "p" * 300, os_name="posix"), "/" + "p" * 300)


class TestScrub(Base):
    def test_secret_keys_redacted(self):
        out = scrub({"api_key": "abc", "nested": {"auth_token": "t", "password": "p"}, "Authorization": "x"})
        self.assertEqual(out["api_key"], ss.REDACTED)
        self.assertEqual(out["nested"], {"auth_token": ss.REDACTED, "password": ss.REDACTED})
        self.assertEqual(out["Authorization"], ss.REDACTED)

    def test_secret_values_redacted(self):
        out = scrub({"note": f"key {FAKE_KEY} and {FAKE_GH}", "list": ["Bearer " + "z" * 30]})
        self.assertNotIn(FAKE_KEY, json.dumps(out))
        self.assertNotIn(FAKE_GH, json.dumps(out))
        self.assertNotIn("z" * 30, json.dumps(out))

    def test_ordinary_fields_untouched(self):
        d = {"input_tokens": 5, "max_tokens": 9, "session_id": "s", "token_count": 3, "model": "m"}
        self.assertEqual(scrub(d), d)

    def test_secrets_never_reach_disk(self):
        st = self.store()
        st.write("doc", {"note": FAKE_KEY, "token": "abc"})
        st.append_event("synthetic", detail=FAKE_GH)
        blob = b"".join(p.read_bytes() for p in self.state_dir.iterdir() if p.is_file())
        self.assertNotIn(FAKE_KEY.encode(), blob)
        self.assertNotIn(FAKE_GH.encode(), blob)
        self.assertTrue(ss.contains_secret({"x": FAKE_KEY}))
        self.assertFalse(ss.contains_secret({"x": "plain"}))


class TestEventLog(Base):
    def test_append_and_read_in_order(self):
        st = self.store()
        for i in range(3):
            st.append_event("e", i=i)
        evs, bad = st.read_events()
        self.assertEqual([e["data"]["i"] for e in evs], [0, 1, 2])
        self.assertEqual(bad, 0)

    def test_torn_last_line_skipped_and_next_append_clean(self):
        st = self.store()
        st.append_event("e", i=1)
        with open(self.state_dir / ss.EVENT_LOG, "ab") as fh:
            fh.write(b'{"kind": "e", "da')  # crash mid-append
        evs, bad = st.read_events()
        self.assertEqual((len(evs), bad), (1, 1))
        st.append_event("e", i=2)
        evs, bad = st.read_events()
        self.assertEqual([e["data"]["i"] for e in evs], [1, 2])
        self.assertEqual(bad, 1)

    def test_log_is_append_only(self):
        st = self.store()
        st.append_event("a")
        first = (self.state_dir / ss.EVENT_LOG).read_bytes()
        st.append_event("b")
        self.assertTrue((self.state_dir / ss.EVENT_LOG).read_bytes().startswith(first))


class TestLock(Base):
    def test_second_supervisor_refused_while_first_alive(self):
        st = self.store(pid_alive=lambda pid: True)
        st.acquire_lock()
        lock = self.state_dir / ss.LOCK_FILE
        data = json.loads(lock.read_text(encoding="utf-8"))
        data["pid"] = os.getpid() + 100000  # pretend another live process holds it
        lock.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(LockHeldError):
            self.store(pid_alive=lambda pid: True).acquire_lock()

    def test_stale_lock_removed(self):
        lock = self.state_dir
        lock.mkdir()
        (lock / ss.LOCK_FILE).write_text(json.dumps({"pid": 999999}), encoding="utf-8")
        st = self.store(pid_alive=lambda pid: False)
        info = st.acquire_lock()
        self.assertEqual(info["pid"], os.getpid())
        st.release_lock()
        self.assertFalse((lock / ss.LOCK_FILE).exists())


# ====================================================================== workers

class TestWorkers(Base):
    def w(self, wid="w-1", task="T-1", wt=None, status=WorkerStatus.ASSIGNED):
        return Worker(worker_id=wid, role="lane", task_id=task, branch="b", worktree=str(wt or self.wt),
                      owner_stream="s", model="m", status=status)

    def test_record_roundtrip_ignores_unknown_fields(self):
        d = self.w().to_dict()
        d["future_field"] = 1
        self.assertEqual(Worker.from_dict(d).to_dict(), self.w().to_dict())

    def test_unknown_status_rejected(self):
        d = self.w().to_dict()
        d["status"] = "DANCING"
        with self.assertRaises(StoreError):
            Worker.from_dict(d)

    def test_record_has_required_fields(self):
        names = set(self.w().to_dict())
        for f in ("worker_id", "role", "task_id", "branch", "worktree", "owner_stream", "provider", "model", "status",
                  "pid", "session_id", "started_at", "last_heartbeat", "restart_count", "last_result_ref"):
            self.assertIn(f, names)

    def test_registry_survives_restart(self):
        reg = WorkerRegistry(self.store())
        reg.add(self.w())
        reg2 = WorkerRegistry(self.store())
        self.assertEqual(reg2.get("w-1").task_id, "T-1")

    def test_duplicate_task_refused(self):
        reg = WorkerRegistry(self.store())
        reg.add(self.w())
        other = self.tmp / "wt2"
        with self.assertRaises(DuplicateWorkerError):
            reg.add(self.w("w-2", task="T-1", wt=other))

    def test_duplicate_worktree_refused_case_and_separator_insensitive_on_windows(self):
        reg = WorkerRegistry(self.store())
        reg.add(self.w())
        alias = str(self.wt) + os.sep
        if os.name == "nt":
            alias = alias.upper().replace("\\", "/")
        with self.assertRaises(DuplicateWorkerError):
            reg.add(self.w("w-2", task="T-2", wt=alias))

    def test_finished_worker_does_not_block(self):
        reg = WorkerRegistry(self.store())
        reg.add(self.w(status=WorkerStatus.EXITED))
        reg.add(self.w("w-2"))
        self.assertEqual(len(reg.list()), 2)


# ====================================================================== lifecycle: assign / spawn

class TestAssignSpawn(Base):
    def test_assign_requires_worktree_and_branch(self):
        s = self.sup(checker=lambda wt, br: (False, "branch missing"))
        with self.assertRaises(WorktreeCheckFailed):
            self.assign(s)
        self.assertEqual(s.registry.list(), [])

    def test_assign_requires_explicit_model(self):
        s = self.sup()
        with self.assertRaises(LifecycleError):
            s.assign(task_id="T", role="lane", branch="b", worktree=str(self.wt), owner_stream="o", model="",
                     prompt_file=str(self.prompt), provider="gemini")

    def test_spawn_runs_with_argument_list(self):
        s = self.sup()
        w = self.running(s)
        self.assertEqual(w.status, WorkerStatus.RUNNING)
        spec = self.launcher.launches[0]
        self.assertIsInstance(spec.argv, list)
        self.assertEqual(spec.argv[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        self.assertIn("--model", spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--session-id") + 1], w.session_id)
        self.assertEqual(spec.stdin_path, os.path.abspath(str(self.prompt)))
        self.assertNotIn("synthetic task prompt", " ".join(spec.argv))
        self.assertEqual(spec.cwd, os.path.abspath(str(self.wt)))
        self.assertEqual(spec.max_runtime_s, s.policy.max_runtime_s)
        self.assertEqual(w.pid, 1001)
        self.assertEqual(w.started_at, w.last_heartbeat)

    def test_starting_is_persisted_before_launch(self):
        seen = {}
        launcher = FakeLauncher()
        s = self.sup(launcher=launcher)
        w = self.assign(s)

        def peek(spec):
            reg = WorkerRegistry(StateStore(self.state_dir, clock=self.clock))
            seen["status"] = reg.get(w.worker_id).status
        launcher.on_launch = peek
        s.spawn(w.worker_id)
        self.assertEqual(seen["status"], WorkerStatus.STARTING)

    def test_launch_error_is_recorded_failure(self):
        s = self.sup(launcher=FakeLauncher(fail_with=FileNotFoundError("no claude")))
        w = self.running(s)
        self.assertEqual(w.status, WorkerStatus.FAILED)
        self.assertEqual(w.failures, ["launch_error:FileNotFoundError"])

    def test_spawn_requires_prompt_file(self):
        s = self.sup()
        w = self.assign(s)
        self.prompt.unlink()
        with self.assertRaises(LifecycleError):
            s.spawn(w.worker_id)

    def test_spawn_only_from_assigned(self):
        s = self.sup()
        w = self.running(s)
        with self.assertRaises(LifecycleError):
            s.spawn(w.worker_id)

    def test_duplicate_assignment_refused_until_finished(self):
        s = self.sup()
        w = self.running(s)
        with self.assertRaises(DuplicateWorkerError):
            self.assign(s)
        with self.assertRaises(DuplicateWorkerError):
            self.assign(s, task="T-OTHER")  # same worktree
        self.launcher.exit(w.pid, 0)
        s.monitor()
        self.assertEqual(self.assign(s).status, WorkerStatus.ASSIGNED)


class TestArgv(Base):
    def test_flags(self):
        sid = str(uuid.UUID(int=7))
        argv = build_claude_argv("claude-opus-test", sid, permission_mode="acceptEdits", max_budget_usd=2)
        self.assertEqual(argv, ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model",
                                "claude-opus-test", "--session-id", sid, "--permission-mode", "acceptEdits",
                                "--max-budget-usd", "2.00"])

    def test_refuses_metacharacters_and_bad_ids(self):
        sid = str(uuid.UUID(int=7))
        for model in ("x&calc", "a b", "m|n", "", "-m", "m%PATH%", 'm"'):
            with self.assertRaises(LifecycleError):
                build_claude_argv(model, sid)
        with self.assertRaises(LifecycleError):
            build_claude_argv("m", "not-a-uuid")
        with self.assertRaises(LifecycleError):
            build_claude_argv("m", sid, permission_mode="yolo")
        with self.assertRaises(LifecycleError):
            build_claude_argv("m", sid, max_budget_usd=-1)


class FakePopen:
    calls = []

    def __init__(self, argv, **kw):
        FakePopen.calls.append((argv, kw))
        self.pid = 4242
        self.code = None
        self.signals = []

    def poll(self):
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.signals.append("term")

    def kill(self):
        self.signals.append("kill")
        self.code = -9


class TestClaudeCliLauncher(Base):
    def test_launch_uses_list_no_shell_and_prompt_on_stdin(self):
        FakePopen.calls = []
        la = ClaudeCliLauncher(which=lambda n: "/fake/bin/claude", popen=FakePopen,
                               env_builder=lambda spec: dict({"PATH": "p", "CLAUDE_CONFIG_DIR": "C:/worker-cfg"}))
        sid = str(uuid.UUID(int=3))
        spec = LaunchSpec(argv=build_claude_argv("m1", sid), cwd=str(self.wt), stdin_path=str(self.prompt),
                          stdout_path=str(self.tmp / "o.jsonl"), stderr_path=str(self.tmp / "e.log"),
                          max_runtime_s=10)
        h = la.launch(spec)
        argv, kw = FakePopen.calls[0]
        self.assertEqual(argv[0], "/fake/bin/claude")
        self.assertEqual(argv[1:], spec.argv[1:])
        self.assertIs(kw["shell"], False)
        self.assertEqual(kw["cwd"], str(self.wt))
        self.assertTrue(kw["stdin"].name.endswith("prompt.md"))
        self.assertTrue(la.owns(h.pid))
        self.assertIsNone(la.poll(h.pid))
        la.terminate(h.pid)
        la.kill(h.pid)
        self.assertEqual(la.poll(h.pid), -9)

    def test_missing_executable(self):
        la = ClaudeCliLauncher(which=lambda n: None, popen=FakePopen)
        with self.assertRaises(FileNotFoundError):
            la.resolve_executable()

    def test_foreign_pid_never_signalled(self):
        la = ClaudeCliLauncher(which=lambda n: None, popen=FakePopen)
        for fn in (la.poll, la.terminate, la.kill):
            with self.assertRaises(NotOwnedError):
                fn(1234)


class TestWorkerLaunchSafety(Base):
    """Workers never start on the supervisor's own (Claude Pro) environment, and a refused launch starts nothing."""

    def _spec(self):
        (self.tmp / "in.txt").write_text("x", encoding="utf-8")
        return LaunchSpec(argv=build_claude_argv("auto/coding", str(uuid.UUID(int=7))), cwd=str(self.wt),
                          stdin_path=str(self.tmp / "in.txt"), stdout_path=str(self.tmp / "o.jsonl"),
                          stderr_path=str(self.tmp / "e.log"), max_runtime_s=10)

    def test_default_launcher_has_no_environment_and_refuses(self):
        FakePopen.calls = []
        la = ClaudeCliLauncher(which=lambda n: "/fake/bin/claude", popen=FakePopen)
        with self.assertRaises(LifecycleError) as cm:
            la.launch(self._spec())
        self.assertIn("fail closed", str(cm.exception))
        self.assertEqual(FakePopen.calls, [], "a refused launch must start no process")

    def test_environment_with_a_direct_credential_is_refused(self):
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "anthropic_api_key"):
            with self.subTest(name=name):
                FakePopen.calls = []
                la = ClaudeCliLauncher(which=lambda n: "/fake/bin/claude", popen=FakePopen,
                                       env_builder=lambda spec, n=name: {"PATH": "p", n: "x"})
                with self.assertRaises(LifecycleError):
                    la.launch(self._spec())
                self.assertEqual(FakePopen.calls, [])

    def test_spec_env_cannot_smuggle_a_credential_in(self):
        FakePopen.calls = []
        la = ClaudeCliLauncher(which=lambda n: "/fake/bin/claude", popen=FakePopen, env_builder=lambda spec: {"PATH": "p"})
        spec = self._spec()
        spec.env = {"ANTHROPIC_API_KEY": "x"}
        with self.assertRaises(LifecycleError):
            la.launch(spec)
        self.assertEqual(FakePopen.calls, [])

    def test_extra_args_come_first_and_policy_env_is_kept(self):
        FakePopen.calls = []
        la = ClaudeCliLauncher(which=lambda n: "/fake/bin/claude", popen=FakePopen, extra_args=["--setting-sources", "project"],
                               env_builder=lambda spec: dict({"PATH": "p", "CLAUDE_CONFIG_DIR": "C:/worker-cfg"}))
        spec = self._spec()
        spec.env = {"CS_AUTHORITY_REPO_ROOT": "X"}
        la.launch(spec)
        argv, kw = FakePopen.calls[0]
        self.assertEqual(argv[1:3], ["--setting-sources", "project"])
        self.assertEqual(kw["env"]["CS_AUTHORITY_REPO_ROOT"], "X")

    def test_assign_never_accepts_anthropic_or_a_claude_model(self):
        s = self.sup()
        for provider, model in (("anthropic", "auto/coding"), ("Anthropic", "auto/coding"), ("gemini", "sonnet"),
                                ("gemini", "claude-opus-test"), ("gemini", "opus"), ("gemini", "auto/claude-sonnet")):
            with self.subTest(provider=provider, model=model):
                with self.assertRaises(LifecycleError):
                    s.assign(task_id="T-X-1", role="lane", branch="lane/x/L1", worktree=str(self.wt),
                             owner_stream="o", model=model, prompt_file=str(self.prompt), provider=provider)
                self.assertEqual(s.registry.list(), [], "a refused assignment must create no worker record")

    def test_assign_records_a_credential_free_allocation_on_the_worker_and_in_the_event(self):
        s = self.sup()
        alloc = {"role": "implementation", "provider": "gemini", "model": "auto/coding", "capability": 2,
                 "evidence": "test:synthetic", "reason": "lowest capability that fits"}
        w = s.assign(task_id="T-SYN-001", role="lane", branch="lane/syn/L1", worktree=str(self.wt),
                     owner_stream="claude:synthetic", model="auto/coding", provider="gemini",
                     prompt_file=str(self.prompt), allocation=alloc)
        self.assertEqual(w.allocation, alloc)
        self.assertIsNot(w.allocation, alloc)                          # a copy: later changes to the argument do not leak
        reread = s.registry.get(w.worker_id)
        self.assertEqual(reread.allocation, alloc)
        events = [e for e in self.store().read_events()[0] if e.get("kind") == "worker_assigned"]
        self.assertEqual(events[-1]["data"].get("allocation_role"), "implementation")
        self.assertNotIn("allocation", events[-1]["data"])             # the event names the role only, not the record

    def test_assign_without_an_allocation_records_an_empty_one_and_old_records_still_load(self):
        from dev_supervisor.workers import Worker
        s = self.sup()
        w = self.assign(s)
        self.assertEqual(w.allocation, {})
        d = w.to_dict()
        d.pop("allocation")
        self.assertEqual(Worker.from_dict(d).allocation, {})            # a record written before this field existed
        for i, junk in enumerate(("x", 5, None, ["a"])):
            with self.subTest(junk=junk):
                w2 = s.assign(task_id=f"T-SYN-1{i}", role="lane", branch=f"lane/syn/J{i}", worktree=str(self.wt / f"j{i}"),
                              owner_stream="claude:synthetic", model="auto/coding", provider="gemini",
                              prompt_file=str(self.prompt), allocation=junk)
                self.assertEqual(w2.allocation, {})                     # only a dict is recorded

    def test_the_launch_spec_carries_the_workers_recorded_provider(self):
        s = self.sup()
        w = self.assign(s)
        w.session_id = str(uuid.UUID(int=9))                  # a worker gets its session id at launch
        self.assertEqual(s.build_spec(w).provider, "gemini")

    def test_assign_requires_an_explicit_provider(self):
        s = self.sup()
        with self.assertRaises(TypeError):
            s.assign(task_id="T-X-2", role="lane", branch="lane/x/L1", worktree=str(self.wt), owner_stream="o",
                     model="auto/coding", prompt_file=str(self.prompt))

    def test_real_subprocess_receives_no_inherited_credentials(self):
        """A real child process: the parent holds Claude Pro style credentials, the worker must not receive them."""
        import json as _json
        import sys as _sys
        from dev_supervisor import provider_env
        out = self.tmp / "child_env.json"
        script = self.tmp / "dump_env.py"
        script.write_text(chr(10).join(["import json, os, sys",
                                        "json.dump({k: v for k, v in os.environ.items() if k.startswith(('ANTHROPIC', 'CLAUDE'))}, "
                                        "open(sys.argv[1], 'w'))", ""]),
                          encoding="utf-8")
        parent = dict(os.environ)
        parent.update({"ANTHROPIC_API_KEY": "sk-parent-key", "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth",
                       "ANTHROPIC_MODEL": "claude-opus-5-5", "CLAUDE_CONFIG_DIR": "C:/Users/someone/.claude",
                       "ANTHROPIC_BASE_URL": "http://localhost:20128", "ANTHROPIC_AUTH_TOKEN": "gateway-token-value"})
        builder = lambda spec: provider_env.worker_env(str(self.tmp / "worker-cfg"), parent)  # noqa: E731
        spec = self._spec()
        spec.argv = [_sys.executable, str(script), str(out)]       # the launcher replaces argv[0] with the resolved executable
        la = ClaudeCliLauncher(which=lambda n: _sys.executable, env_builder=builder)
        h = la.launch(spec)
        for _ in range(100):
            if la.poll(h.pid) is not None:
                break
            time.sleep(0.1)
        seen = _json.loads(out.read_text(encoding="utf-8"))
        self.assertNotIn("ANTHROPIC_API_KEY", seen)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen)
        self.assertNotIn("ANTHROPIC_MODEL", seen)
        self.assertEqual(seen["CLAUDE_CONFIG_DIR"], str((self.tmp / "worker-cfg").resolve()))
        self.assertEqual(seen["ANTHROPIC_BASE_URL"], "http://localhost:20128")
        self.assertIn("ANTHROPIC_AUTH_TOKEN", seen, "only the explicitly authorised gateway credential is passed")
        self.assertNotIn("sk-parent-key", _json.dumps(seen))


class TestGitChecker(Base):
    def run_fake(self, out="lane/x", code=0):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return mock.Mock(returncode=code, stdout=out + "\n")
        return run, calls

    def test_ok_and_argument_list(self):
        run, calls = self.run_fake()
        ok, _ = GitWorktreeChecker(run=run)(str(self.wt), "lane/x")
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], ["git", "-C", str(self.wt), "rev-parse", "--abbrev-ref", "HEAD"])
        self.assertIs(calls[0][1]["shell"], False)
        self.assertIn("timeout", calls[0][1])

    def test_wrong_branch_not_a_repo_missing_dir(self):
        run, _ = self.run_fake(out="main")
        self.assertFalse(GitWorktreeChecker(run=run)(str(self.wt), "lane/x")[0])
        run, _ = self.run_fake(code=128)
        self.assertFalse(GitWorktreeChecker(run=run)(str(self.wt), "lane/x")[0])
        run, calls = self.run_fake()
        self.assertFalse(GitWorktreeChecker(run=run)(str(self.tmp / "nope"), "lane/x")[0])
        self.assertEqual(calls, [])


# ====================================================================== monitor

class TestMonitor(Base):
    def test_clean_exit(self):
        s = self.sup()
        w = self.running(s)
        self.launcher.exit(w.pid, 0)
        self.assertEqual(s.monitor(), [{"worker_id": w.worker_id, "from": "RUNNING", "to": "EXITED"}])
        self.assertEqual(s.registry.get(w.worker_id).exit_code, 0)

    def test_failed_exit_sets_backoff(self):
        s = self.sup(policy=SupervisorPolicy(backoff_base_s=10))
        w = self.running(s)
        self.launcher.exit(w.pid, 3)
        s.monitor()
        w = s.registry.get(w.worker_id)
        self.assertEqual(w.status, WorkerStatus.FAILED)
        self.assertEqual(w.failures, ["exit:3"])
        self.assertEqual(ss.parse_iso(w.next_restart_not_before), T0 + 10)

    def test_stale_detection_and_heartbeat_revival(self):
        s = self.sup(policy=SupervisorPolicy(heartbeat_stale_s=60))
        w = self.running(s)
        self.clock.advance(59)
        s.monitor()
        self.assertEqual(s.registry.get(w.worker_id).status, WorkerStatus.RUNNING)
        self.clock.advance(2)
        s.monitor()
        self.assertEqual(s.registry.get(w.worker_id).status, WorkerStatus.STALE)
        s.heartbeat(w.worker_id)
        self.assertEqual(s.registry.get(w.worker_id).status, WorkerStatus.RUNNING)

    def test_activity_probe_counts_as_heartbeat(self):
        act = {"t": None}
        s = self.sup(policy=SupervisorPolicy(heartbeat_stale_s=60), activity_probe=lambda w: act["t"])
        w = self.running(s)
        self.clock.advance(100)
        s.monitor()
        self.assertEqual(s.registry.get(w.worker_id).status, WorkerStatus.STALE)
        act["t"] = self.clock.t - 1
        s.monitor()
        self.assertEqual(s.registry.get(w.worker_id).status, WorkerStatus.RUNNING)

    def test_default_activity_probe_reads_output_log_mtime(self):
        s = self.sup()
        s.activity_probe = s._log_activity
        w = self.running(s)
        p = s.stdout_path(w)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")
        os.utime(p, (T0 + 30, T0 + 30))
        self.clock.advance(40)
        s.monitor()
        self.assertEqual(ss.parse_iso(s.registry.get(w.worker_id).last_heartbeat), T0 + 30)

    def test_bounded_runtime(self):
        s = self.sup(policy=SupervisorPolicy(max_runtime_s=100, heartbeat_stale_s=10_000))
        w = self.running(s)
        self.clock.advance(101)
        s.monitor()
        w = s.registry.get(w.worker_id)
        self.assertEqual(w.status, WorkerStatus.FAILED)
        self.assertEqual(w.failures, ["timeout"])
        self.assertIn(w.pid, self.launcher.terminated)

    def test_unowned_dead_pid_marked_lost(self):
        s = self.sup()
        w = self.running(s)
        self.launcher.procs.clear()  # as if launched by another (dead) supervisor process
        s.monitor()
        self.assertEqual(s.registry.get(w.worker_id).status, WorkerStatus.LOST)


# ====================================================================== restart / backoff / blocking

class TestRestart(Base):
    def fail_once(self, s, w, code=1):
        self.launcher.exit(s.registry.get(w.worker_id).pid, code)
        s.monitor()
        return s.registry.get(w.worker_id)

    def test_restart_respects_backoff(self):
        s = self.sup(policy=SupervisorPolicy(backoff_base_s=30))
        w = self.fail_once(s, self.running(s))
        with self.assertRaises(BackoffActive):
            s.restart(w.worker_id)
        self.clock.advance(30)
        w2 = s.restart(w.worker_id)
        self.assertEqual(w2.status, WorkerStatus.RUNNING)
        self.assertEqual(w2.restart_count, 1)
        self.assertEqual(len(w2.previous_session_ids), 1)
        self.assertNotEqual(w2.previous_session_ids[0], w2.session_id)

    def test_backoff_is_exponential_and_capped(self):
        p = SupervisorPolicy(backoff_base_s=10, backoff_max_s=50)
        self.assertEqual([p.backoff(i) for i in range(5)], [10, 20, 40, 50, 50])

    def test_identical_failures_block_and_record_blocker(self):
        s = self.sup(policy=SupervisorPolicy(identical_failure_limit=2, max_restarts=10, backoff_base_s=1))
        w = self.fail_once(s, self.running(s), code=2)
        self.clock.advance(5)
        s.restart(w.worker_id)
        w = self.fail_once(s, w, code=2)
        self.assertEqual(w.status, WorkerStatus.BLOCKED)
        self.assertEqual(len(s.registry.blockers), 1)
        self.assertIn("identical", s.registry.blockers[0]["reason"])
        with self.assertRaises(WorkerBlocked):
            s.restart(w.worker_id)

    def test_differing_failures_hit_restart_budget(self):
        s = self.sup(policy=SupervisorPolicy(identical_failure_limit=3, max_restarts=2, backoff_base_s=1))
        w = self.running(s)
        for code in (1, 2):
            w = self.fail_once(s, w, code=code)
            self.clock.advance(100)
            s.restart(w.worker_id)
        w = self.fail_once(s, w, code=3)
        self.assertEqual(w.status, WorkerStatus.BLOCKED)
        self.assertIn("budget", s.registry.blockers[0]["reason"])

    def test_restart_storm_is_bounded(self):
        s = self.sup(policy=SupervisorPolicy(auto_restart=True, max_restarts=3, identical_failure_limit=3,
                                             backoff_base_s=5, backoff_max_s=20))
        w = self.running(s)
        for _ in range(200):  # the process crashes immediately, every time
            cur = s.registry.get(w.worker_id)
            if cur.pid is not None and cur.status == WorkerStatus.RUNNING:
                self.launcher.exit(cur.pid, 1)
            s.tick()
            self.clock.advance(1)
        self.assertEqual(s.registry.get(w.worker_id).status, WorkerStatus.BLOCKED)
        self.assertLessEqual(len(self.launcher.launches), 1 + 3)
        self.assertEqual(len([b for b in s.registry.blockers if not b["resolved"]]), 1)

    def test_no_auto_restart_by_default(self):
        s = self.sup()
        w = self.fail_once(s, self.running(s))
        self.clock.advance(10_000)
        self.assertEqual(s.tick()["restarted"], [])
        self.assertEqual(s.registry.get(w.worker_id).status, WorkerStatus.FAILED)

    def test_unblock_requires_reason_and_resets(self):
        s = self.sup(policy=SupervisorPolicy(identical_failure_limit=1))
        w = self.fail_once(s, self.running(s))
        self.assertEqual(w.status, WorkerStatus.BLOCKED)
        with self.assertRaises(LifecycleError):
            s.unblock(w.worker_id, " ")
        w = s.unblock(w.worker_id, "synthetic fix applied")
        self.assertEqual((w.status, w.restart_count, w.failures), (WorkerStatus.FAILED, 0, []))
        self.assertTrue(s.registry.blockers[0]["resolved"])
        self.assertEqual(s.restart(w.worker_id).status, WorkerStatus.RUNNING)

    def test_restart_refused_while_running(self):
        s = self.sup()
        w = self.running(s)
        with self.assertRaises(LifecycleError):
            s.restart(w.worker_id)

    def test_restart_refused_if_task_taken_meanwhile(self):
        s = self.sup(policy=SupervisorPolicy(backoff_base_s=0))
        w = self.fail_once(s, self.running(s))
        self.assign(s, wt=self.tmp)  # same task, different worktree: allowed because w is not active
        with self.assertRaises(DuplicateWorkerError):
            s.restart(w.worker_id)


# ====================================================================== stop

class TestStop(Base):
    def test_graceful_stop(self):
        s = self.sup()
        w = self.running(s)
        w = s.stop(w.worker_id)
        self.assertEqual(w.status, WorkerStatus.STOPPING)
        s.monitor()
        w = s.registry.get(w.worker_id)
        self.assertEqual(w.status, WorkerStatus.STOPPED)
        self.assertEqual(self.launcher.killed, [])

    def test_kill_after_grace(self):
        s = self.sup(launcher=FakeLauncher(exit_on_terminate=False),
                     policy=SupervisorPolicy(stop_grace_s=5, stop_poll_s=1))
        w = self.running(s)
        w = s.stop(w.worker_id, wait=True)
        self.assertEqual(w.status, WorkerStatus.STOPPED)
        self.assertEqual(self.launcher.killed, [w.pid])
        self.assertGreaterEqual(self.clock.t - T0, 5)

    def test_stop_assigned(self):
        s = self.sup()
        w = self.assign(s)
        self.assertEqual(s.stop(w.worker_id).status, WorkerStatus.STOPPED)
        self.assertEqual(self.launcher.launches, [])

    def test_unowned_pid_not_signalled(self):
        s = self.sup()
        w = self.running(s)
        self.launcher.procs.clear()
        with self.assertRaises(NotOwnedError):
            s.stop(w.worker_id)
        self.assertEqual(self.launcher.terminated, [])


# ====================================================================== results

class TestResults(Base):
    def two_workers(self, s):
        a = self.running(s, task="T-R")
        self.launcher.exit(a.pid, 0)
        s.monitor()
        b = self.running(s, task="T-R")  # second attempt on the same task after the first finished
        return a, b

    def test_collect_is_reported_and_no_verifier_keeps_it(self):
        s = self.sup()
        w = self.running(s)
        r = s.collect(w.worker_id, "DONE", {"tests": "pass"}, ref="lanes/L1/report.json")
        self.assertEqual(r.status, ResultStatus.REPORTED)
        self.assertEqual(s.registry.get(w.worker_id).last_result_ref, r.result_id)
        with self.assertRaises(LifecycleError):
            s.verify_result(r.result_id)

    def test_verifier_decides(self):
        verdicts = iter([("VERIFIED", "synthetic evidence"), ("REJECTED", "tests failed"), ("maybe", "")])
        s = self.sup(verifier=lambda r, w: next(verdicts))
        w = self.running(s)
        r1 = s.verify_result(s.collect(w.worker_id, "DONE").result_id)
        self.assertEqual(r1.status, ResultStatus.VERIFIED)
        r2 = s.verify_result(s.collect(w.worker_id, "DONE").result_id)
        self.assertEqual(r2.status, ResultStatus.REJECTED)
        r3 = s.verify_result(s.collect(w.worker_id, "DONE").result_id)
        self.assertEqual(r3.status, ResultStatus.REPORTED)

    def test_contradiction_detected_and_blocks_verification(self):
        s = self.sup(verifier=lambda r, w: ("VERIFIED", "ok") if r.outcome == "DONE" else ("REJECTED", "no"))
        a, b = self.two_workers(s)
        ra = s.collect(a.worker_id, "DONE", {"tests": "pass"})
        rb = s.collect(b.worker_id, "FAILED", {"tests": "fail"})
        self.assertTrue(ra.contradiction and rb.contradiction)
        self.assertEqual(len(s.registry.contradictions), 1)
        self.assertEqual(s.registry.contradictions[0]["fields"], ["outcome", "claims.tests"])
        self.assertEqual(s.verify_result(ra.result_id).status, ResultStatus.REPORTED)  # contradicted
        self.assertEqual(s.verify_result(rb.result_id).status, ResultStatus.REJECTED)
        self.assertEqual(s.verify_result(ra.result_id).status, ResultStatus.VERIFIED)

    def test_claim_only_contradiction(self):
        s = self.sup()
        a, b = self.two_workers(s)
        s.collect(a.worker_id, "DONE", {"head": "abc"})
        rb = s.collect(b.worker_id, "DONE", {"head": "def"})
        self.assertTrue(rb.contradiction)

    def test_agreement_and_same_worker_are_not_contradictions(self):
        s = self.sup()
        a, b = self.two_workers(s)
        s.collect(a.worker_id, "IN_PROGRESS")
        s.collect(a.worker_id, "DONE")  # same worker revising its own report
        s.collect(b.worker_id, "DONE", {"tests": "pass"})
        self.assertEqual(len(s.registry.contradictions), 1)  # only a's IN_PROGRESS vs b's DONE
        s2 = self.sup(store=StateStore(self.tmp / "s2", clock=self.clock))
        c, d = self.two_workers(s2)
        s2.collect(c.worker_id, "DONE", {"tests": "pass"})
        s2.collect(d.worker_id, "DONE", {"tests": "pass"})
        self.assertEqual(s2.registry.contradictions, [])

    def test_collect_report_file_keeps_only_counts_and_ref(self):
        s = self.sup()
        w = self.running(s)
        rep = self.tmp / "report.json"
        rep.write_text(json.dumps({"status": "DONE", "summary": "synthetic secret-free text",
                                   "commands": [{"command": "c", "result": "r"}], "unresolved": []}),
                       encoding="utf-8")
        r = s.collect_report_file(w.worker_id, str(rep))
        self.assertEqual((r.outcome, r.claims, r.ref), ("DONE", {"commands": "1", "unresolved": "0"}, str(rep)))
        self.assertNotIn("synthetic secret-free text", s.store.path("workers").read_text(encoding="utf-8"))

    def test_results_persist(self):
        s = self.sup()
        w = self.running(s)
        r = s.collect(w.worker_id, "DONE", {"note": "token " + FAKE_KEY})
        reg = WorkerRegistry(StateStore(self.state_dir, clock=self.clock))
        self.assertEqual(reg.results[r.result_id].status, ResultStatus.REPORTED)
        self.assertNotIn(FAKE_KEY, json.dumps(reg.results[r.result_id].to_dict()))


# ====================================================================== recovery

class TestRecovery(Base):
    def test_fresh_supervisor_reconciles_without_relaunching(self):
        s = self.sup()
        alive = self.running(s, task="T-A", wt=self.tmp / "a")
        dead = self.running(s, task="T-B", wt=self.tmp / "b")
        pending = self.assign(s, task="T-C", wt=self.tmp / "c")
        self.alive = {alive.pid}
        s.close()

        fresh_launcher = FakeLauncher()
        s2 = self.sup(launcher=fresh_launcher)
        plan = s2.recover()
        actions = {i.worker_id: (i.status, i.action) for i in plan.items}
        self.assertEqual(actions[alive.worker_id], (WorkerStatus.RUNNING, "observe_unowned"))
        self.assertEqual(actions[dead.worker_id], (WorkerStatus.LOST, "inspect_then_restart_manually"))
        self.assertEqual(actions[pending.worker_id], (WorkerStatus.ASSIGNED, "spawn_when_ready"))
        self.assertEqual(fresh_launcher.launches, [])
        self.assertEqual((fresh_launcher.terminated, fresh_launcher.killed), ([], []))
        self.assertEqual(plan.relaunched, 0)
        reg = WorkerRegistry(StateStore(self.state_dir, clock=self.clock))
        self.assertEqual(reg.get(dead.worker_id).status, WorkerStatus.LOST)

    def test_crash_during_launch_is_uncertain(self):
        launcher = FakeLauncher()

        def crash(spec):
            raise KeyboardInterrupt  # supervisor dies mid-launch
        launcher.on_launch = crash
        s = self.sup(launcher=launcher)
        w = self.assign(s)
        with self.assertRaises(KeyboardInterrupt):
            s.spawn(w.worker_id)
        s.close()
        s2 = self.sup()
        item = [i for i in s2.recover().items if i.worker_id == w.worker_id][0]
        self.assertEqual((item.previous_status, item.status, item.action),
                         (WorkerStatus.STARTING, WorkerStatus.LOST, "verify_before_continuing"))
        self.assertEqual(self.launcher.launches, [])

    def test_plan_lists_results_blockers_contradictions_and_is_json(self):
        s = self.sup(policy=SupervisorPolicy(identical_failure_limit=1))
        w = self.running(s)
        s.collect(w.worker_id, "DONE")
        self.launcher.exit(w.pid, 1)
        s.monitor()
        s.close()
        s2 = self.sup()
        plan = s2.recover()
        self.assertEqual(len(plan.results_awaiting_verification), 1)
        self.assertEqual(len(plan.blockers), 1)
        self.assertEqual(plan.items[0].action, "needs_human")
        json.dumps(plan.to_dict())

    def test_store_damage_reported_in_plan(self):
        s = self.sup()
        self.running(s)
        self.assign(s, task="T-2", wt=self.tmp / "x")
        s.close()
        p = self.state_dir / "workers.json"
        p.write_bytes(p.read_bytes()[:20])
        s2 = self.sup()
        plan = s2.recover()
        self.assertEqual(len(plan.store_recoveries), 1)
        self.assertEqual(len(plan.items), 1)  # the backup holds the state before the second assignment

    def test_second_live_supervisor_refused(self):
        s = self.sup()
        self.alive = {os.getpid() + 100000}
        lock = self.state_dir / ss.LOCK_FILE
        data = json.loads(lock.read_text(encoding="utf-8"))
        data["pid"] = os.getpid() + 100000
        lock.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(LockHeldError):
            Supervisor(StateStore(self.state_dir, clock=self.clock), FakeLauncher(), worktree_checker=ok_checker,
                       clock=self.clock, pid_probe=lambda pid: pid in self.alive)
        self.assertIsNotNone(s)

    def test_event_log_records_lifecycle(self):
        s = self.sup()
        w = self.running(s)
        self.launcher.exit(w.pid, 0)
        s.monitor()
        kinds = [e["kind"] for e in s.store.read_events()[0]]
        for k in ("worker_assigned", "launch_begin", "launch_done", "worker_exited"):
            self.assertIn(k, kinds)


if __name__ == "__main__":
    unittest.main()
