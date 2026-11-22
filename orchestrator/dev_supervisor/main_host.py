"""Main terminal host and attached launch mode (VS Code integrated terminal, two tabs).

Why (REPORTED 2026-11-19): a Main started by the supervisor with ``CREATE_NEW_CONSOLE`` had no visible window, so the
user could not talk to it. Nothing outside VS Code can create an integrated-terminal tab, so the tab is opened by the
user and the interactive ``claude`` runs INSIDE it, as the child of a small host process:

    tab 1 "Main"        python -B -m dev_supervisor main-host --yes     (this module: MainHost)
    tab 2 "Supervisor"  python -B -m dev_supervisor start --yes         (cli.SupervisorLoop, main_launch_mode attached)

Rules (IMPLEMENTED, tested offline with fake claude processes; NOT TESTED live in VS Code):
- The host is the SOLE owner of the interactive Main child. In attached mode the supervisor never launches a Main.
- Coordination is machine-local, in the supervisor state directory, one writer per file, atomic writes:
  ``main_host.json`` (host only), ``main_host.lock`` (an OS lock held by the host process), ``main_commands/<id>.json``
  and ``<id>.cancel.json`` (supervisor only) and ``main_commands/<id>.ack.json`` (host only). No credentials are ever
  written (``state_store.scrub`` is applied).
- Duplicate prevention: the host refuses to start while ``main.json`` records a Main whose pid is alive (whatever its
  status), while an earlier host's child is alive, while another host holds the OS lock, or while a live Claude Code
  session with this Main's name runs in this repository (the session directory is machine-wide, so this also sees a Main
  recorded in another state directory). ``--ignore-recorded-main`` is the only override.
- The host lock is an OS lock (``msvcrt.locking`` / ``fcntl.flock``) on a held file handle: the OS releases it when the
  host process dies, so there is no stale-lock takeover race and no pid-reuse ambiguity.
- Replacement in ONE terminal cannot keep the old Main running while the new one starts (two interactive programs
  cannot share one console). The safest recoverable sequence is therefore:

  1. Supervisor verifies the handoff (``replace_session``, unchanged) and checks the host is alive and owns the old Main.
  2. ``handover`` command. The host PREFLIGHTS first with no side effect (command fresh, supervisor alive, host child is
     exactly the old session, prompt file inside the state directory, executable resolvable, argv builds, not cancelled).
     Any failure -> ``REJECTED``/``EXPIRED``/``CANCELLED``; the old Main is untouched.
  3. WRITE-AHEAD: the host publishes ``recover_session_id`` (and the transition stage) BEFORE it touches the old child.
     If that cannot be persisted nothing is stopped (``FAILED``). Only then ``ACCEPTED`` -> stop the old child
     (terminate, then kill; if it cannot be stopped the write-ahead is undone, ``FAILED``, old Main still there) ->
     start the new child in the same tab (``STARTED``). SIGINT (Ctrl+C reaches the host as well as ``claude``) is held
     off during these transitions, and an interruption that still gets through leaves durable recovery information.
  4. ``replace_session`` waits for the new session's first heartbeat (session file with the NEW session id). No
     heartbeat or an early exit -> the attempt failed; the supervisor sends ``stop_child`` and may retry once.
  5. All attempts failed -> the supervisor sends ``resume``: the host starts ``claude --resume <old id>`` in the tab.
     The old conversation is restored from Claude Code's own transcript; whether the exact previous state returns, and
     whether the id stays the same, is only what the session file then shows (observed, never assumed).
  6. No supervisor answer within ``host_recovery_wait_s`` (or the supervisor died): the host performs that same
     resume itself, once, UNLESS the new child had been observed running (its session file appeared): an exit after
     that is the user's own exit and is never "recovered". If the resume fails too the host reports NEEDS_ATTENTION
     and ends; nothing loops.
  7. Success is recorded only from observation: the new session id seen in a session file with a live pid, then the
     old child's exit recorded by the host (``AttachedMainRetirer``), then ``settle`` (acknowledged, retried).
- A supervisor that times out waiting for a handover asks the host to CANCEL; the host honours that only before it
  stops the old Main. A handover that completes anyway is adopted and reported as such, never as "rolled back".
- A supervisor restart reconciles from the host record (``AttachedMain.reconcile``): a live host child whose session
  file shows the expected id is adopted; a different id on the host's pid is IDENTITY_MISMATCH and is NOT adopted
  (except after a ``resume``, where the observed id is recorded as such). An old session is marked RETIRED only with
  host-recorded exit evidence.
- Errors: reading or writing a coordination file can fail transiently (sharing violation, torn read, antivirus).
  RETRYABLE = ``OSError``, ``StoreError``, ``ValueError`` (bad JSON): they are retried and never end the host while it
  has a child, nor the supervisor loop. FATAL for the host only with no child after ``FATAL_AFTER_ERRORS`` consecutive
  failures, or when it cannot be persisted before a destructive step (then the step is not taken).
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import provider_env, replacement
from .config import Config
from .launch import (MAIN_ROLE, LaunchError, MainProfile, NewConsoleTerminal, SessionFileHeartbeat, build_main_argv,
                     find_session, read_main, write_main, write_main_prompt)
from .lifecycle import LaunchHandle, LaunchSpec, NotOwnedError, pid_alive as _default_pid_alive
from .state_store import StateStore, StoreError, is_within, parse_iso, scrub, utc_iso

HOST_DOC = "main_host"
HOST_LOCK = "main_host.lock"
COMMANDS_DIR = "main_commands"
HEARTBEAT_EVERY_S = 2.0
STOP_GRACE_S = 15.0
KILL_GRACE_S = 5.0
HANDOVER_GRACE_S = 30.0         # after asking the host to cancel, how long the supervisor waits for its final answer
SELF_RECOVER_SUPERVISOR_DEAD_S = 5.0
UNSETTLED_S = 900.0     # a handover stays "recoverable" this long without a settle (2 attempts x heartbeat timeout)
FATAL_AFTER_ERRORS = 30           # consecutive host tick failures with NO child before the host gives up
BAD_COMMAND_READS = 3             # a command file that stays unparseable this many ticks is rejected
LOCK_OFFSET = 0x10000000          # the locked byte lies far beyond the lock file's body, so the body stays readable
TERMINAL_ACKS = ("STARTED", "DONE", "REJECTED", "EXPIRED", "FAILED", "CANCELLED")
OPS = ("handover", "stop_child", "resume", "settle")
RETRYABLE = (OSError, StoreError, ValueError)

# host states
S_STARTING, S_RUNNING, S_NO_CHILD, S_EXITED, S_NEEDS_ATTENTION = (
    "STARTING", "RUNNING", "NO_CHILD", "EXITED", "NEEDS_ATTENTION")


def _uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise LaunchError("not a valid session id") from None


def _norm_path(p: Any) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


# ------------------------------------------------------------------ spawning (host side)

class ForegroundSpawner:
    """Starts ``claude`` as a child that INHERITS the terminal the host runs in (no new console, no redirection), so the
    user types straight into it in the VS Code tab. The environment is ``provider_env.child_env()`` exactly as in
    console mode. A ``.cmd``/``.bat`` shim is refused (same rule as ``NewConsoleTerminal``)."""

    def __init__(self, which: Callable[[str], Optional[str]] = shutil.which,
                 popen: Callable[..., Any] = subprocess.Popen, os_name: Optional[str] = None) -> None:
        self._nc = NewConsoleTerminal(which=which, popen=popen, os_name=os_name)
        self._popen = popen

    def preflight(self, executable: str) -> str:
        return self._nc.resolve(executable)

    def open(self, argv: List[str], cwd: str) -> Any:
        if not argv or any(not isinstance(a, str) for a in argv):
            raise LaunchError("argv must be a non-empty list of strings")
        exe = self.preflight(argv[0])
        return self._popen([exe] + list(argv[1:]), cwd=cwd, shell=False, close_fds=True, env=provider_env.child_env())


# ------------------------------------------------------------------ OS lock

def _os_lock(fd: int) -> bool:
    """Take the exclusive, non-blocking OS lock on the open lock file. False when another handle holds it."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


# ------------------------------------------------------------------ coordination files

def read_host_ex(store: StateStore, *, sleep: Callable[[float], None] = time.sleep,
                 attempts: int = 3) -> Tuple[Dict[str, Any], bool]:
    """(document, readable). Transient failures are retried; an unreadable document is ``({}, False)`` so a caller can
    tell "no host" from "could not read the host record"."""
    for i in range(max(1, attempts)):
        try:
            return (store.read(HOST_DOC, default=None) or {}), True
        except RETRYABLE:
            if i + 1 < attempts:
                sleep(0.05)
    return {}, False


def read_host(store: StateStore, *, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    return read_host_ex(store, sleep=sleep)[0]


def host_fresh(host: Dict[str, Any], now: float, stale_s: float, alive: Callable[[Optional[int]], bool]) -> bool:
    hb = parse_iso(host.get("heartbeat_at"))
    return bool(host and alive(host.get("pid")) and hb is not None and 0 <= now - hb <= stale_s)


def _cmd_dir(store: StateStore) -> Path:
    return store.directory / COMMANDS_DIR


def _write_json(store: StateStore, path: Path, body: Dict[str, Any]) -> None:
    os.makedirs(str(path.parent), exist_ok=True)
    store.atomic_write_bytes(path, (json.dumps(scrub(body), sort_keys=True) + "\n").encode("utf-8"))


def _read_json_ex(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """(value, state) with state ok | missing | transient (OS error, retry) | corrupt (not a JSON object)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "transient"
    try:
        v = json.loads(raw)
    except ValueError:
        return None, "corrupt"
    return (v, "ok") if isinstance(v, dict) else (None, "corrupt")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    return _read_json_ex(path)[0]


def send_command(store: StateStore, op: str, payload: Dict[str, Any], ttl_s: float,
                 supervisor_pid: Optional[int] = None) -> str:
    if op not in OPS:
        raise LaunchError(f"unknown host command {op!r}")
    now = float(store.clock())
    cid = f"{int(now * 1000):015d}-{uuid.uuid4().hex[:8]}"
    _write_json(store, _cmd_dir(store) / f"{cid}.json",
                {"id": cid, "op": op, "payload": payload, "created_ts": now, "expires_ts": now + float(ttl_s),
                 "supervisor_pid": os.getpid() if supervisor_pid is None else supervisor_pid})
    store.append_event("main_host_command", command_id=cid, op=op)
    return cid


def send_cancel(store: StateStore, cid: str) -> None:
    """Supervisor: ask the host not to start (or continue) command ``cid``. The host honours it only before it stops
    the old Main; after that the command runs to its end and the supervisor reads the real outcome."""
    _write_json(store, _cmd_dir(store) / f"{cid}.cancel.json",
                {"id": cid, "at": utc_iso(store.clock()), "supervisor_pid": os.getpid()})
    store.append_event("main_host_command_cancel_requested", command_id=cid)


def is_cancelled(store: StateStore, cid: str) -> bool:
    return (_cmd_dir(store) / f"{cid}.cancel.json").exists()


def read_ack(store: StateStore, cid: str) -> Optional[Dict[str, Any]]:
    return _read_json(_cmd_dir(store) / f"{cid}.ack.json")


def write_ack(store: StateStore, cid: str, status: str, **fields: Any) -> None:
    _write_json(store, _cmd_dir(store) / f"{cid}.ack.json",
                {"id": cid, "status": status, "at": utc_iso(store.clock()), **fields})


def pending_commands(store: StateStore) -> List[Tuple[str, Dict[str, Any]]]:
    """Commands without a terminal ack, oldest first. A file that cannot be read right now (sharing violation) is left
    for the next tick; one that is not a JSON object comes back as op ``unreadable`` (the host rejects it only after
    ``BAD_COMMAND_READS`` consecutive looks)."""
    d = _cmd_dir(store)
    try:
        if not d.is_dir():
            return []
        files = sorted(d.glob("*.json"))
    except OSError:
        return []
    out = []
    for p in files:
        if p.name.endswith(".ack.json") or p.name.endswith(".cancel.json"):
            continue
        cid = p.name[:-len(".json")]
        ack = read_ack(store, cid)
        if ack is not None and ack.get("status") in TERMINAL_ACKS:
            continue
        cmd, state = _read_json_ex(p)
        if state == "transient" or state == "missing":
            continue
        out.append((cid, cmd if state == "ok" else {"id": cid, "op": "unreadable", "payload": {}}))
    return out


# ------------------------------------------------------------------ the host (tab 1)

class MainHost:
    """Runs and owns the interactive Main child in this terminal. ``tick()`` is one step (tests drive it directly)."""

    def __init__(self, store: StateStore, cfg: Config, spawner: Any, *, root: str,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 pid_alive: Callable[[Optional[int]], bool] = _default_pid_alive,
                 session_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
                 out: Any = None, host_pid: Optional[int] = None, poll_s: float = 1.0,
                 read_sessions: Optional[Callable[..., List[Dict[str, Any]]]] = None) -> None:
        self.store, self.cfg, self.spawner, self.root = store, cfg, spawner, root
        self.clock, self.sleep, self.pid_alive, self.session_factory = clock, sleep, pid_alive, session_factory
        self.out, self.poll_s = out, poll_s
        self.pid = host_pid if host_pid is not None else os.getpid()
        self.host_id = uuid.uuid4().hex[:12]
        self.read_sessions = read_sessions
        self.profile = MainProfile.from_config(cfg)
        self.child: Optional[Dict[str, Any]] = None        # {handle,pid,session_id,kind,started_at,confirmed}
        self.ended: Dict[str, Dict[str, Any]] = {}          # session id -> how its child ended (last 8)
        self.state = S_STARTING
        self.recover_sid: Optional[str] = None
        self.recover_since: Optional[float] = None
        self.no_child_since: Optional[float] = None
        self.last_supervisor_pid: Optional[int] = None
        self.last_command: Optional[Dict[str, Any]] = None
        self.transition: Optional[Dict[str, Any]] = None    # handover in progress: cid, stage, old, new (+ undo values)
        self.exit_code: Optional[int] = None
        self.sigint_held = 0
        self._bad_reads: Dict[str, int] = {}
        self._last_pub = 0.0
        self._locked = False
        self._lock_fd: Optional[int] = None
        try:
            self.provider_label = provider_env.provider(dict(os.environ))
        except ValueError:
            self.provider_label = "invalid"

    # -------------------------------------------------- output

    def say(self, text: str) -> None:
        if self.out is not None:
            try:
                self.out.write(text.rstrip("\n") + "\n")
                self.out.flush()
            except (AttributeError, OSError, ValueError):
                pass

    def _event(self, kind: str, **fields: Any) -> None:
        try:
            self.store.append_event(kind, **fields)
        except RETRYABLE:
            pass

    # -------------------------------------------------- OS lock and duplicate guard

    def _lock_path(self) -> Path:
        return self.store.directory / HOST_LOCK

    def acquire(self) -> None:
        """Take the host's OS lock. The lock belongs to this process: the OS drops it when the process dies, so a crashed
        host never leaves a lock behind and two hosts can never both believe they hold it."""
        path = self._lock_path()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(str(path), flags, 0o600)
        except OSError:
            raise LaunchError("the Main host lock file could not be opened") from None
        if not _os_lock(fd):
            held = _read_json(path) or {}
            os.close(fd)
            raise LaunchError(f"another Main terminal host is already running (pid {held.get('pid')}); "
                              "use that terminal tab")
        previous = _read_json(path) or {}
        hp = previous.get("pid")
        try:
            body = json.dumps({"host_id": self.host_id, "pid": self.pid, "acquired_at": utc_iso(self.clock())}).encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, body)
        except OSError:
            _os_unlock(fd)
            os.close(fd)
            raise LaunchError("the Main host lock could not be recorded") from None
        self._lock_fd, self._locked = fd, True
        if isinstance(hp, int) and hp != self.pid:
            self._event("main_host_stale_lock_taken_over", previous_pid=hp, previous_pid_alive=bool(self.pid_alive(hp)))

    def release(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if self._locked and fd is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)           # an orderly release leaves no stale owner record
            except OSError:
                pass
            _os_unlock(fd)
            try:
                os.close(fd)
            except OSError:
                pass
        self._locked = False

    def simulate_process_death(self) -> None:
        """What the OS does when the host process dies: the lock handle closes, the owner record stays behind."""
        fd, self._lock_fd, self._locked = self._lock_fd, None, False
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def guard(self, ignore_recorded: bool = False) -> None:
        """Refuse to start a second Main. Raises ``LaunchError`` naming the live session. Fails closed: if the records
        cannot be read, a duplicate cannot be ruled out and nothing is started."""
        if self.cfg.main_launch_mode != "attached":
            raise LaunchError('main-host needs "main_launch_mode": "attached" in the machine-local config')
        host, host_ok = read_host_ex(self.store, sleep=self.sleep)
        if not host_ok:
            raise LaunchError("the Main host record could not be read; refusing to start (a live Main cannot be ruled out)")
        child = host.get("child") or {}
        if child.get("pid") and host.get("host_id") != self.host_id and self.pid_alive(child.get("pid")):
            raise LaunchError(f"a Main started by an earlier host is still alive (pid {child.get('pid')}, session "
                              f"{str(child.get('session_id'))[:8]}); close it, or reuse that tab")
        if ignore_recorded:
            return
        try:
            main = read_main(self.store)
        except RETRYABLE:
            raise LaunchError("the Main record could not be read; refusing to start (a live Main cannot be ruled out)") \
                from None
        mp = main.get("pid")
        if isinstance(mp, int) and self.pid_alive(mp):
            raise LaunchError(f"Main session {str(main.get('session_id'))[:8]} is recorded as {main.get('status')} "
                              f"and its process (pid {mp}) is alive. Starting another Main would duplicate it. "
                              "Close that Main yourself first (it may be an old console Main with no visible window); "
                              "if the pid is stale after you verified that, add --ignore-recorded-main")
        self._guard_live_sessions()

    def _guard_live_sessions(self) -> None:
        """Claude Code's own session files are machine-wide: a live Main-named session in THIS repository is a duplicate
        even when it is recorded in a different state directory (or not recorded at all)."""
        if self.read_sessions is None:
            return
        try:
            sessions = self.read_sessions(self.cfg.sessions_directory(), pid_alive=self.pid_alive)
        except Exception:  # noqa: BLE001 - unreadable session files cannot prove a duplicate
            return
        here = _norm_path(self.root)
        for s in sessions:
            if "error" in s:
                continue
            alive, cwd, name = s.get("alive"), s.get("cwd"), s.get("name")
            if not (alive is not None and getattr(alive, "value", None) is True):
                continue
            if not (getattr(cwd, "available", False) and getattr(name, "available", False)):
                continue
            if _norm_path(cwd.value) == here and name.value == self.profile.name:
                pid = getattr(s.get("pid"), "value", None)
                raise LaunchError(f"a live Claude Code session named {self.profile.name!r} already runs in this "
                                  f"repository (pid {pid}, session {str(getattr(s.get('session_id'), 'value', ''))[:8]}) "
                                  "although no live Main is recorded here. Starting another Main would duplicate it. "
                                  "Close it first, or use a different repo_root for a test; "
                                  "--ignore-recorded-main overrides this check")

    # -------------------------------------------------- planning (no side effects except prompt files)

    def _profile_for(self, model: Optional[str]) -> MainProfile:
        return self.profile if not model else dataclasses.replace(self.profile, model=model)

    def plan_new(self, session_id: str, *, model: Optional[str] = None,
                 resume_prompt_file: Optional[str] = None) -> List[str]:
        sid = _uuid(session_id)
        if resume_prompt_file is not None and not is_within(Path(resume_prompt_file), self.store.directory):
            raise LaunchError("the resume prompt must be inside the supervisor state directory")
        prompt = write_main_prompt(self.store, sid, resume_prompt_file=resume_prompt_file)
        argv = build_main_argv(self._profile_for(model), sid, prompt)
        self.spawner.preflight(argv[0])
        return argv

    def plan_resume(self, session_id: str) -> List[str]:
        sid = _uuid(session_id)
        prompt = self.store.directory / "prompts" / f"main-{sid}.prompt.txt"
        if not prompt.is_file():
            prompt = write_main_prompt(self.store, sid)
        argv = build_main_argv(self.profile, sid, prompt, resume=True)
        self.spawner.preflight(argv[0])
        return argv

    # -------------------------------------------------- child control

    def _spawn(self, argv: List[str], sid: str, kind: str) -> Dict[str, Any]:
        handle = self.spawner.open(argv, self.root)
        try:
            self.child = {"handle": handle, "pid": int(handle.pid), "session_id": sid, "kind": kind,
                          "started_at": utc_iso(self.clock()), "confirmed": False}
        except BaseException:
            # Never leave an unowned interactive child behind: it was started a moment ago and nothing tracks it.
            try:
                handle.terminate()
            except Exception:  # noqa: BLE001
                pass
            raise
        self.state = S_RUNNING
        self.no_child_since = None
        self._event("main_host_child_started", session_id=sid, pid=self.child["pid"], child_kind=kind)
        self.publish(force=True)
        return self.child

    def _stop_child(self, reason: str) -> bool:
        """Terminate then kill the host's own child. True once it has ended (its exit is recorded)."""
        c = self.child
        if c is None:
            return True
        h = c["handle"]
        try:
            if h.poll() is None:
                h.terminate()
            deadline = float(self.clock()) + STOP_GRACE_S
            while h.poll() is None and float(self.clock()) < deadline:
                self.sleep(0.2)
                self._heartbeat_quietly()
            if h.poll() is None:
                h.kill()
                deadline = float(self.clock()) + KILL_GRACE_S
                while h.poll() is None and float(self.clock()) < deadline:
                    self.sleep(0.2)
                    self._heartbeat_quietly()
        except Exception as e:  # noqa: BLE001
            self._event("main_host_stop_error", error=e.__class__.__name__)
        code = h.poll()
        if code is None:
            return False
        self._record_exit(code, reason)
        return True

    def _heartbeat_quietly(self) -> None:
        """Keep the heartbeat fresh while a slow stop is in progress; a failed write must not abort the stop."""
        try:
            self.publish()
        except RETRYABLE:
            pass

    def _record_exit(self, code: Any, reason: str) -> None:
        c = self.child
        self.ended[c["session_id"]] = {"pid": c["pid"], "session_id": c["session_id"], "exit_code": code,
                                       "ended_at": utc_iso(self.clock()), "reason": reason,
                                       "confirmed": bool(c.get("confirmed"))}
        while len(self.ended) > 8:
            self.ended.pop(next(iter(self.ended)))
        self.child = None
        self._event("main_host_child_ended", session_id=c["session_id"], pid=c["pid"], exit_code=code, reason=reason)

    def _observe_confirmation(self) -> None:
        """While a replacement is unsettled, note when the host ITSELF sees the new child's session file with a live pid.
        A child that has been seen running is not "a Main that failed to start": if it exits later, that is the user's
        own exit and the old session must not be resurrected."""
        c = self.child
        if c is None or c.get("confirmed") or self.recover_sid is None or self.read_sessions is None:
            return
        try:
            s = find_session(self.read_sessions(self.cfg.sessions_directory(), pid_alive=self.pid_alive),
                             c["session_id"])
        except Exception:  # noqa: BLE001
            return
        alive = (s or {}).get("alive")
        if alive is not None and getattr(alive, "value", None) is True:
            c["confirmed"] = True
            self._event("main_host_child_confirmed", session_id=c["session_id"], pid=c["pid"])

    # -------------------------------------------------- publishing

    def publish(self, force: bool = False) -> None:
        now = float(self.clock())
        if not force and now - self._last_pub < HEARTBEAT_EVERY_S:
            return
        c = self.child
        t = self.transition
        doc = {"host_id": self.host_id, "pid": self.pid, "state": self.state, "heartbeat_at": utc_iso(now),
               "cwd": str(self.root), "mode": self.cfg.main_launch_mode, "provider": self.provider_label,
               "child": (dict({k: c[k] for k in ("pid", "session_id", "kind", "started_at")},
                              confirmed=bool(c.get("confirmed"))) if c else None),
               "ended_children": self.ended, "recover_session_id": self.recover_sid,
               "recover_since": self.recover_since, "last_command": self.last_command,
               "transition": ({k: t.get(k) for k in ("cid", "stage", "old", "new")} if t else None)}
        self.store.write(HOST_DOC, doc)
        self._last_pub = now

    # -------------------------------------------------- start / run

    def start(self, *, resume_last: bool = False, ignore_recorded: bool = False) -> None:
        self.acquire()
        try:
            self.guard(ignore_recorded)
            self._inherit_history()
            self._sweep_commands()
            if resume_last:
                try:
                    sid = _uuid(read_main(self.store).get("session_id") or "")
                except RETRYABLE:
                    raise LaunchError("the Main record could not be read; cannot resume the last session") from None
                argv, kind = self.plan_resume(sid), "resume"
            else:
                sid = _uuid(self.session_factory())
                argv, kind = self.plan_new(sid), "start"
            self._spawn(argv, sid, kind)
        except BaseException:
            self.state = S_EXITED
            try:
                self.publish(force=True)
            except BaseException:  # noqa: BLE001
                pass
            finally:
                self.release()
            raise
        try:
            self.say(f"Main (session {sid[:8]}) is running in this tab. The supervisor runs in the other tab.")
        except KeyboardInterrupt:
            pass

    def _inherit_history(self) -> None:
        """Carry over what an earlier host recorded about the Mains it ran, and note the one it left behind if that
        process is gone: this host OBSERVES it dead now. That is the evidence that lets the supervisor retire the
        old session record (the exit code of a process that died with its tab is unknown, so it stays null)."""
        prior, ok = read_host_ex(self.store, sleep=self.sleep)
        if not ok:
            return
        for sid, rec in (prior.get("ended_children") or {}).items():
            if isinstance(rec, dict):
                self.ended[sid] = rec
        ch = prior.get("child") or {}
        sid = ch.get("session_id")
        if sid and sid not in self.ended and ch.get("pid") and not self.pid_alive(ch.get("pid")):
            self.ended[sid] = {"pid": ch["pid"], "session_id": sid, "exit_code": None, "observed_dead": True,
                               "ended_at": utc_iso(self.clock()), "reason": "earlier host ended; process observed gone",
                               "confirmed": bool(ch.get("confirmed"))}
        while len(self.ended) > 8:
            self.ended.pop(next(iter(self.ended)))

    def _sweep_commands(self) -> None:
        """Commands left over from before this host existed are never executed."""
        for cid, _cmd in pending_commands(self.store):
            write_ack(self.store, cid, "EXPIRED", reason="left over from before this host started")

    @contextlib.contextmanager
    def _shield(self) -> Iterator[None]:
        """Hold SIGINT off during a destructive transition. Ctrl+C is delivered to every process on the console, so it
        reaches this host whenever the user presses it in ``claude``; ``claude`` handles its own copy."""
        prev = None
        installed = False
        if threading.current_thread() is threading.main_thread():
            try:
                prev = signal.signal(signal.SIGINT, self._hold_sigint)
                installed = True
            except (ValueError, OSError):
                installed = False
        try:
            yield
        finally:
            if installed and prev is not None:
                try:
                    signal.signal(signal.SIGINT, prev)
                except (ValueError, OSError):
                    pass

    def _hold_sigint(self, _signum: int, _frame: Any) -> None:
        self.sigint_held += 1

    def run(self, *, resume_last: bool = False, ignore_recorded: bool = False, max_ticks: Optional[int] = None) -> int:
        self.start(resume_last=resume_last, ignore_recorded=ignore_recorded)
        ticks = errors = 0
        clean = False
        try:
            while True:
                try:
                    code = self.tick()
                    errors = 0
                    if code is not None:
                        clean = True
                        return code
                    ticks += 1
                    if max_ticks is not None and ticks >= max_ticks:
                        clean = True
                        return 0
                    self.sleep(self.poll_s)
                except KeyboardInterrupt:      # Ctrl+C goes to the shared console: the child handles it, we stay up
                    continue
                except Exception as e:  # noqa: BLE001 - never let a bad tick strand a running interactive child
                    errors += 1
                    self._event("main_host_tick_error", error=e.__class__.__name__, consecutive=errors)
                    if errors == 1 or errors % 10 == 0:
                        self.say(f"Main host: a coordination step failed ({e.__class__.__name__}); retrying. "
                                 "Your Main keeps running.")
                    if self.child is None and errors >= FATAL_AFTER_ERRORS:
                        self.state, self.exit_code = S_NEEDS_ATTENTION, 1
                        clean = True
                        return 1
                    try:
                        self.sleep(self.poll_s)
                    except KeyboardInterrupt:
                        pass
        finally:
            if not clean and self.child is not None:
                # Abnormal end of the host: do not leave an interactive claude running in this tab with no host.
                self.say("Main host is stopping unexpectedly: ending its Main so no unattended session stays in this tab.")
                try:
                    self._stop_child("host_exit")
                except BaseException:  # noqa: BLE001
                    pass
            if self.child is None and self.state not in (S_EXITED, S_NEEDS_ATTENTION):
                self.state = S_EXITED
            try:
                self.publish(force=True)
            except BaseException:  # noqa: BLE001
                pass
            self.release()

    # -------------------------------------------------- one step

    def tick(self) -> Optional[int]:
        now = float(self.clock())
        if self.child is not None:
            code = self.child["handle"].poll()
            if code is not None:
                self._child_exited(code, now)
                if self.exit_code is not None:
                    self.release()
                    return self.exit_code
            else:
                self._observe_confirmation()
        for cid, cmd in pending_commands(self.store)[:1]:
            self._handle(cid, cmd, now)
        if self.child is None and self.state == S_NO_CHILD:
            res = self._maybe_self_recover(now)
            if res is not None:
                self.release()
                return res
        if (self.recover_sid and self.child is not None and self.recover_since is not None
                and now - self.recover_since > UNSETTLED_S):
            self.recover_sid = self.recover_since = None      # the new Main ran past the window: nothing to recover
        self.publish()
        return None

    def _child_exited(self, code: Any, now: float) -> None:
        c = self.child
        sid, confirmed = c["session_id"], bool(c.get("confirmed"))
        self._record_exit(code, "exited")
        within = (not confirmed and self.recover_sid is not None and self.recover_since is not None
                  and now - self.recover_since <= UNSETTLED_S)
        if within:
            self.state, self.no_child_since = S_NO_CHILD, now
            self.say(f"Main (session {sid[:8]}) ended during a replacement. Waiting for the supervisor; the previous "
                     "session will be resumed here if nothing happens.")
        else:
            if confirmed and self.recover_sid is not None:
                self._event("main_host_recovery_dropped", session_id=sid, recover_session_id=self.recover_sid,
                            reason="the new Main had been observed running; its exit is the user's own")
                self.recover_sid = self.recover_since = None
            self.state = S_EXITED
            self.exit_code = 0 if code == 0 else 1
            self.say(f"Main (session {sid[:8]}) ended with exit code {code}. Run main-host again to start a new one.")
        self.publish(force=True)

    def _maybe_self_recover(self, now: float) -> Optional[int]:
        if self.recover_sid is None or self.no_child_since is None:
            self.state, self.exit_code = S_EXITED, 1
            self.publish(force=True)
            return 1
        waited = now - self.no_child_since
        sup_dead = self.last_supervisor_pid is not None and not self.pid_alive(self.last_supervisor_pid)
        if waited < float(self.cfg.host_recovery_wait_s) and not (sup_dead and waited >= SELF_RECOVER_SUPERVISOR_DEAD_S):
            return None
        sid = self.recover_sid
        self._event("main_host_self_recovery", session_id=sid, waited_s=round(waited, 1))
        try:
            with self._shield():
                self._spawn(self.plan_resume(sid), sid, "resume")
        except Exception as e:  # noqa: BLE001
            self.state, self.exit_code = S_NEEDS_ATTENTION, 1
            self._event("main_host_self_recovery_failed", error=e.__class__.__name__)
            self.say("Could not resume the previous Main. Start a fresh one: main-host --yes (or --resume-last).")
            try:
                self.publish(force=True)
            except RETRYABLE:
                pass
            return 1
        self.recover_sid = self.recover_since = None
        self.say(f"Resumed previous Main session {sid[:8]} (claude --resume).")
        return None

    # -------------------------------------------------- commands

    def _safe_ack(self, cid: str, status: str, **fields: Any) -> None:
        try:
            write_ack(self.store, cid, status, **fields)
        except RETRYABLE as e:
            self._event("main_host_ack_failed", command_id=cid, status=status, error=e.__class__.__name__)

    def _handle(self, cid: str, cmd: Dict[str, Any], now: float) -> None:
        op, p = cmd.get("op"), cmd.get("payload") or {}
        if op == "unreadable":
            self._bad_reads[cid] = self._bad_reads.get(cid, 0) + 1
            if self._bad_reads[cid] >= BAD_COMMAND_READS:
                self._safe_ack(cid, "REJECTED", reason="unreadable or corrupt command file")
            return
        sup = cmd.get("supervisor_pid")
        self.last_supervisor_pid = sup if isinstance(sup, int) else self.last_supervisor_pid
        self.last_command = {"id": cid, "op": op}
        try:
            with self._shield():
                if op not in OPS:
                    return self._safe_ack(cid, "REJECTED", reason="unknown or unreadable command")
                if op != "settle" and is_cancelled(self.store, cid):
                    return self._safe_ack(cid, "CANCELLED", reason="cancelled by the supervisor before the host acted")
                if float(cmd.get("expires_ts") or 0) < now:
                    return self._safe_ack(cid, "EXPIRED", reason="command expired before the host acted")
                if not (isinstance(sup, int) and self.pid_alive(sup)):
                    return self._safe_ack(cid, "REJECTED", reason="the issuing supervisor is not alive")
                getattr(self, "_cmd_" + op)(cid, p)
        except LaunchError as e:
            self._safe_ack(cid, "REJECTED", reason=str(e)[:200])
        except KeyboardInterrupt:
            self._after_interrupt(cid)
        except Exception as e:  # noqa: BLE001
            self._after_failure(cid, e)
        self.last_command = {"id": cid, "op": op}
        self.publish(force=True)

    def _undo_write_ahead(self) -> None:
        t = self.transition or {}
        self.recover_sid, self.recover_since = t.get("prev_sid"), t.get("prev_since")
        self.transition = None

    def _after_interrupt(self, cid: str) -> None:
        """An interruption got through the shield. Whatever stage it hit, durable recovery information exists (it is
        written BEFORE the old child is touched); make the in-memory state agree with what is on disk."""
        t = self.transition or {}
        stage = t.get("stage")
        c = self.child
        self._event("main_host_interrupted", command_id=cid, stage=stage)
        if stage == "STOPPING" and c is not None and c["session_id"] == t.get("old") and c["handle"].poll() is None:
            self._undo_write_ahead()                       # the old Main is still running: nothing was lost
            msg = "interrupted while stopping the old Main; it is still running"
        elif c is None and self.recover_sid is not None:
            self.state = S_NO_CHILD
            self.no_child_since = self.no_child_since or float(self.clock())
            self.transition = None
            msg = "interrupted after the old Main stopped; the previous session will be resumed (recovery is recorded)"
        else:
            self.transition = None
            msg = "interrupted" + ("" if stage else " before anything was changed")
        self._safe_ack(cid, "FAILED", reason=msg)

    def _after_failure(self, cid: str, e: Exception) -> None:
        t = self.transition or {}
        if t.get("stage") == "STOPPING" and self.child is not None and self.child["session_id"] == t.get("old"):
            self._undo_write_ahead()
        elif self.child is None and self.recover_sid is not None:
            self.state = S_NO_CHILD
            self.no_child_since = self.no_child_since or float(self.clock())
            self.transition = None
        else:
            self.transition = None
        self._safe_ack(cid, "FAILED", reason=e.__class__.__name__)

    def _cmd_handover(self, cid: str, p: Dict[str, Any]) -> None:
        old, new = _uuid(p.get("old_session_id")), _uuid(p.get("new_session_id"))
        if old == new:
            raise LaunchError("new session id equals the old one")
        have_old = self.child is not None and self.child["session_id"] == old and self.child["handle"].poll() is None
        retry = self.child is None and self.state == S_NO_CHILD and self.recover_sid == old
        if not (have_old or retry):
            raise LaunchError("the host does not own the old Main (identity mismatch); nothing was touched")
        model = p.get("model") if isinstance(p.get("model"), str) else None
        argv = self.plan_new(new, model=model, resume_prompt_file=p.get("prompt_file"))      # preflight: no side effect
        if is_cancelled(self.store, cid):
            return write_ack(self.store, cid, "CANCELLED", reason="cancelled by the supervisor; nothing was touched")
        write_ack(self.store, cid, "ACCEPTED", old_session_id=old, new_session_id=new)
        now = float(self.clock())
        # WRITE-AHEAD: durable recovery information BEFORE the old child is touched. If it cannot be persisted, the old
        # Main is not touched at all.
        prev = (self.recover_sid, self.recover_since)
        self.transition = {"cid": cid, "stage": "STOPPING", "old": old, "new": new, "prev_sid": prev[0],
                           "prev_since": prev[1]}
        self.recover_sid, self.recover_since = old, now
        try:
            self.publish(force=True)
        except BaseException as e:  # noqa: BLE001
            self._undo_write_ahead()
            if isinstance(e, KeyboardInterrupt):
                raise
            return write_ack(self.store, cid, "FAILED", reason="the recovery record could not be saved; the old Main "
                             "was not touched")
        if have_old and not self._stop_child("handover"):
            self._undo_write_ahead()
            return write_ack(self.store, cid, "FAILED", reason="the old Main could not be stopped; it is still "
                             "running and nothing else was started")
        self.transition["stage"] = "STOPPED"
        self.state, self.no_child_since = S_NO_CHILD, now
        self.publish(force=True)
        self.transition["stage"] = "SPAWNING"
        try:
            c = self._spawn(argv, new, "handover")
        except Exception as e:  # noqa: BLE001
            self.transition = None
            self.say("The new Main could not be started. Waiting for the supervisor; the previous session will be "
                     "resumed here if nothing happens.")
            return write_ack(self.store, cid, "FAILED", reason=f"new Main did not start ({e.__class__.__name__}); "
                             "the old Main was already stopped and is recoverable with resume")
        self.transition = None
        try:
            self.say(f"New Main (session {new[:8]}) started in this tab, replacing {old[:8]}.")
        except KeyboardInterrupt:
            pass
        write_ack(self.store, cid, "STARTED", pid=c["pid"], session_id=new, replaces=old)

    def _cmd_stop_child(self, cid: str, p: Dict[str, Any]) -> None:
        c = self.child
        if c is None:
            return write_ack(self.store, cid, "DONE", reason="no child was running")
        if c["pid"] != p.get("pid") or c["session_id"] != p.get("session_id"):
            raise LaunchError("the child to stop does not match the host's child (identity mismatch)")
        if not self._stop_child("stop_child"):
            return write_ack(self.store, cid, "FAILED", reason="the child could not be stopped")
        self.state, self.no_child_since = S_NO_CHILD, float(self.clock())
        if self.recover_sid is None:
            self.state, self.exit_code = S_EXITED, 0
        write_ack(self.store, cid, "DONE")
        self.publish(force=True)

    def _cmd_resume(self, cid: str, p: Dict[str, Any]) -> None:
        sid = _uuid(p.get("session_id"))
        if self.child is not None:
            raise LaunchError("a Main is already running in this host; refusing to start a second one")
        if self.recover_sid != sid:
            raise LaunchError("the session to resume is not the recoverable session of this host")
        c = self._spawn(self.plan_resume(sid), sid, "resume")
        self.recover_sid = self.recover_since = None
        self.say(f"Resumed previous Main session {sid[:8]} (claude --resume).")
        write_ack(self.store, cid, "STARTED", pid=c["pid"], session_id=sid)

    def _cmd_settle(self, cid: str, p: Dict[str, Any]) -> None:
        sid = _uuid(p.get("session_id"))
        if self.child is not None and self.child["session_id"] == sid:
            self.recover_sid = self.recover_since = None
            return write_ack(self.store, cid, "DONE")
        if self.child is None and sid in self.ended:
            # the new Main already ended (the user quit it): there is nothing left to settle or recover
            self.recover_sid = self.recover_since = None
            return write_ack(self.store, cid, "DONE", reason="that Main has already ended")
        raise LaunchError("settle does not match the host's running Main")


# ------------------------------------------------------------------ supervisor side

class HostChannel:
    """The supervisor's view of the host: readiness checks, commands and bounded waits."""

    def __init__(self, store: StateStore, cfg: Config, *, clock: Callable[[], float], sleep: Callable[[float], None],
                 pid_alive: Callable[[Optional[int]], bool]) -> None:
        self.store, self.cfg, self.clock, self.sleep, self.pid_alive = store, cfg, clock, sleep, pid_alive
        self.read_ok = True

    def host(self) -> Dict[str, Any]:
        doc, self.read_ok = read_host_ex(self.store, sleep=self.sleep)
        return doc

    def fresh(self) -> bool:
        return host_fresh(self.host(), float(self.clock()), float(self.cfg.host_heartbeat_stale_s), self.pid_alive)

    def request(self, op: str, payload: Dict[str, Any]) -> str:
        return send_command(self.store, op, payload, float(self.cfg.host_command_timeout_s))

    def wait(self, cid: str, timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Last ack once it is terminal, else the latest non-terminal ack (or None) after the timeout."""
        deadline = float(self.clock()) + float(timeout_s if timeout_s is not None else self.cfg.host_command_timeout_s)
        while True:
            ack = read_ack(self.store, cid)
            if ack is not None and ack.get("status") in TERMINAL_ACKS:
                return ack
            if float(self.clock()) >= deadline:
                return ack
            self.sleep(0.5)


class AttachedMainLauncher:
    """L2 ``ProcessLauncher`` for attached mode: it asks the host to start the new Main; it starts nothing itself."""

    def __init__(self, channel: HostChannel) -> None:
        self.channel = channel
        self.old_session_id: Optional[str] = None
        self.handoff_id: Optional[str] = None
        self._started: Dict[int, str] = {}
        self.cancelled: List[str] = []

    def begin(self, old_session_id: str, handoff_id: str) -> None:
        self.old_session_id, self.handoff_id = old_session_id, handoff_id

    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        argv = list(spec.argv)
        try:
            model = argv[argv.index("--model") + 1]
            sid = argv[argv.index("--session-id") + 1]
        except (ValueError, IndexError):
            raise LaunchError("launch spec carries no --model/--session-id") from None
        if not self.old_session_id:
            raise LaunchError("no old Main session recorded for this replacement")
        host = self.channel.host()
        if not self.channel.fresh():
            raise LaunchError("no live Main terminal host (its heartbeat is stale or missing); start main-host")
        child = host.get("child") or {}
        no_child_retry = host.get("recover_session_id") == self.old_session_id and (
            host.get("state") == S_NO_CHILD or not self.channel.pid_alive(child.get("pid")))
        if child.get("session_id") != self.old_session_id and not no_child_retry:
            raise LaunchError("the host does not run the old Main (identity mismatch); refusing to replace")
        cid = self.channel.request("handover", {"old_session_id": self.old_session_id, "new_session_id": sid,
                                                "model": model, "prompt_file": spec.stdin_path,
                                                "handoff_id": self.handoff_id})
        ack = self.channel.wait(cid)
        if (ack or {}).get("status") not in TERMINAL_ACKS:
            # No final answer in time (silence, or ACCEPTED and still working). Ask the host to cancel: it honours that
            # only before it stops the old Main. Then wait for its FINAL answer instead of guessing: a handover that
            # completes anyway is a started Main, not a failure.
            send_cancel(self.channel.store, cid)
            self.cancelled.append(cid)
            ack = self.channel.wait(cid, HANDOVER_GRACE_S)
        status = (ack or {}).get("status")
        if status != "STARTED":
            why = (ack or {}).get("reason") or ("host did not answer in time" if ack is None else f"status {status}")
            raise LaunchError(f"handover {status or 'UNANSWERED'}: {why}")
        pid = int(ack["pid"])
        self._started[pid] = sid
        return LaunchHandle(pid=pid)

    def owns(self, pid: int) -> bool:
        return pid in self._started

    def poll(self, pid: int) -> Optional[int]:
        if pid not in self._started:
            raise NotOwnedError(f"pid {pid} was not started through this launcher")
        if self.channel.pid_alive(pid):
            return None
        for rec in (self.channel.host().get("ended_children") or {}).values():
            if rec.get("pid") == pid and isinstance(rec.get("exit_code"), int):
                return rec["exit_code"]
        return -1

    def terminate(self, pid: int) -> None:
        sid = self._started.get(pid)
        if sid is None:
            raise NotOwnedError(f"pid {pid} was not started through this launcher")
        cid = self.channel.request("stop_child", {"pid": pid, "session_id": sid})
        self.channel.wait(cid)

    def kill(self, pid: int) -> None:
        self.terminate(pid)           # the host escalates terminate -> kill itself


class AttachedMainRetirer:
    """L3 ``SessionRetirer``: the host already ended the old Main at handover; this only checks the recorded evidence."""

    def __init__(self, channel: HostChannel) -> None:
        self.channel = channel

    def retire(self, session_id: str) -> Tuple[bool, str]:
        prev = (self.channel.host().get("ended_children") or {}).get(session_id) or {}
        if prev.get("exit_code") is not None and not self.channel.pid_alive(prev.get("pid")):
            return True, "old Main ended by its terminal host at handover (exit recorded by the host)"
        return False, "the host has no recorded exit of the old Main; close that session by hand"


class AttachedMain:
    """Supervisor-side coordinator for attached mode: reconcile with the host, replace, recover."""

    SETTLE_TRIES = 5

    def __init__(self, store: StateStore, cfg: Config, *, read_sessions: Callable[..., List[Dict[str, Any]]],
                 pid_alive: Callable[[Optional[int]], bool], clock: Callable[[], float],
                 sleep: Callable[[float], None]) -> None:
        self.store, self.cfg, self.read_sessions, self.pid_alive = store, cfg, read_sessions, pid_alive
        self.clock, self.sleep = clock, sleep
        self.channel = HostChannel(store, cfg, clock=clock, sleep=sleep, pid_alive=pid_alive)
        self.launcher = AttachedMainLauncher(self.channel)
        self.retirer = AttachedMainRetirer(self.channel)
        self.pending_settle: Optional[str] = None
        self.settle_tries = 0

    def heartbeat_probe(self) -> SessionFileHeartbeat:
        return SessionFileHeartbeat(self.cfg.sessions_directory(), self.read_sessions, self.pid_alive, self.clock)

    # -------------------------------------------------- reconcile (also after a supervisor restart)

    def _sessions(self) -> List[Dict[str, Any]]:
        return self.read_sessions(self.cfg.sessions_directory(), pid_alive=self.pid_alive)

    def safe_reconcile(self) -> Dict[str, Any]:
        """``reconcile`` for the supervisor loop: a transient coordination-file error is retried on the next tick; it
        never ends the loop. Anything that is not a coordination read/write error is a bug and still propagates."""
        try:
            r = self.reconcile()
            self.retry_settle()
            return r
        except RETRYABLE as e:
            try:
                self.store.append_event("main_reconcile_error", error=e.__class__.__name__)
            except RETRYABLE:
                pass
            return {"state": "read_error", "error": e.__class__.__name__}

    def reconcile(self) -> Dict[str, Any]:
        host = self.channel.host()
        child = host.get("child")
        if not child or not self.pid_alive(child.get("pid")):
            return {"state": "no_child"}
        pid, expected = child["pid"], child["session_id"]
        sessions = self._sessions()
        by_sid = find_session(sessions, expected)
        observed, note = None, None
        if by_sid is not None and by_sid.get("alive") is not None and by_sid["alive"].value is True:
            observed = expected
        else:
            by_pid = next((s for s in sessions if "error" not in s and s["pid"].available and s["pid"].value == pid
                           and s["session_id"].available), None)
            if by_pid is not None:
                observed = by_pid["session_id"].value
        if observed is None:
            return {"state": "awaiting_session", "expected": expected}
        if observed != expected:
            if child.get("kind") != "resume":
                cur = read_main(self.store)
                if cur.get("status") != "IDENTITY_MISMATCH" or cur.get("observed_session_id") != observed:
                    write_main(self.store, status="IDENTITY_MISMATCH", pid=pid, expected_session_id=expected,
                               observed_session_id=observed)
                    self.store.append_event("main_identity_mismatch", expected=expected, observed=observed, pid=pid)
                return {"state": "identity_mismatch", "expected": expected, "observed": observed}
            note = f"resumed session {expected[:8]} is running under a new id (observed)"
        return {"state": "adopted", "session_id": observed, "pid": pid, "kind": child.get("kind"), "note": note,
                **self._adopt(observed, pid, child, note, host)}

    def _adopt(self, sid: str, pid: int, child: Dict[str, Any], note: Optional[str],
               host: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        main = read_main(self.store)
        if main.get("session_id") == sid and main.get("status") == "RUNNING" and main.get("pid") == pid:
            return {"changed": False}
        host = host if host is not None else self.channel.host()
        old = main.get("session_id")
        old_pid = main.get("pid")
        if old and old != sid and isinstance(old_pid, int) and old_pid != pid and self.pid_alive(old_pid):
            self.store.append_event("main_adopted_while_other_main_alive", session_id=sid, other_session_id=old,
                                    other_pid=old_pid)
        write_main(self.store, session_id=sid, status="RUNNING", pid=pid, launched_at=child.get("started_at"),
                   adopted_from_host=True, host_note=note, expected_session_id=None, observed_session_id=None)
        doc = replacement.load_sessions(self.store)
        # register_session() marks the previous authoritative Main EXITED, so the old session's status must be read
        # before it runs: the retirement decision below depends on what it was (RETIRING or ACTIVE), not on that write.
        old_status_before = (doc["sessions"].get(old) or {}).get("status") if old else None
        if sid not in doc["sessions"] or doc["sessions"][sid].get("status") != replacement.SessionStatus.ACTIVE:
            replacement.register_session(self.store, sid, MAIN_ROLE)
            doc = replacement.load_sessions(self.store)
        if old and old != sid and old in doc["sessions"]:
            rec = doc["sessions"][old]
            if old_status_before in (replacement.SessionStatus.RETIRING, replacement.SessionStatus.ACTIVE):
                ev = (host.get("ended_children") or {}).get(old) or {}
                if ((ev.get("exit_code") is not None or ev.get("observed_dead") is True)
                        and not self.pid_alive(ev.get("pid"))):
                    rec.update(status=replacement.SessionStatus.RETIRED, authoritative=False, replaced_by=sid,
                               updated_at=utc_iso(self.store.clock()))
                else:
                    # No host-recorded exit: the old process may still be alive. Never record it as retired.
                    rec.update(status=replacement.SessionStatus.RETIRE_FAILED, authoritative=False, replaced_by=sid,
                               failure="old session not confirmed ended (no host-recorded exit); stop it by hand "
                                       "if it is still running", updated_at=utc_iso(self.store.clock()))
                    self.store.append_event("session_retire_unconfirmed", session_id=old, replaced_by=sid)
        for k, v in doc["sessions"].items():
            if k != sid and v.get("role") == MAIN_ROLE and v.get("authoritative") and k == old:
                v["authoritative"] = False
        self.store.write(replacement.SESSIONS_DOC, doc)
        self.store.append_event("main_adopted_from_host", session_id=sid, pid=pid, child_kind=child.get("kind"))
        return {"changed": True}

    # -------------------------------------------------- recovery after a replacement that did not complete

    def needs_attention(self, old_sid: str, why: str, *, mark_old: bool = True) -> None:
        write_main(self.store, status="NEEDS_ATTENTION", failure=why[:200])
        if mark_old:
            doc = replacement.load_sessions(self.store)
            if old_sid in doc["sessions"]:
                doc["sessions"][old_sid].update(status=replacement.SessionStatus.FAILED, authoritative=False,
                                                failure=why[:200], updated_at=utc_iso(self.store.clock()))
                self.store.write(replacement.SESSIONS_DOC, doc)
        self.store.append_event("main_needs_attention", session_id=old_sid, reason=why[:200])

    def _stable_host(self) -> Optional[Dict[str, Any]]:
        for _ in range(5):
            doc, ok = read_host_ex(self.store, sleep=self.sleep)
            if ok:
                return doc
            self.sleep(1.0)
        return None

    def recover_after_failed_replace(self, old_sid: str) -> Dict[str, Any]:
        host = self._stable_host()
        if host is None:
            try:
                self.needs_attention(old_sid, "the host record could not be read; state unknown", mark_old=False)
            except RETRYABLE:
                pass            # the record store is unreadable too; the loop retries and the host's own window expires
            return {"outcome": "HOST_STATE_UNREADABLE"}
        child = host.get("child")
        main = read_main(self.store)
        if main.get("session_id") == old_sid and self.pid_alive(main.get("pid")) and not (
                child and child.get("session_id") != old_sid and self.pid_alive(child.get("pid"))):
            return {"outcome": "OLD_MAIN_INTACT"}          # nothing of the old Main was touched (or it is not ours)
        if child and self.pid_alive(child.get("pid")):
            if child.get("session_id") == old_sid:
                return {"outcome": "OLD_MAIN_INTACT"}
            # The host completed a handover the supervisor had given up on (or lost the answer to). The host's child is
            # the authoritative Main: wait for its session file, then adopt it.
            deadline = float(self.clock()) + float(self.cfg.host_command_timeout_s)
            while True:
                r = self.reconcile()
                if r.get("state") in ("adopted", "identity_mismatch") or float(self.clock()) >= deadline:
                    break
                self.sleep(1.0)
            return {"outcome": "HOST_HAS_OTHER_MAIN" if r.get("state") == "adopted" else "HOST_HAS_UNCONFIRMED_MAIN",
                    "reconcile": r.get("state"), "session_id": r.get("session_id")}
        if not self.channel.fresh():
            self.needs_attention(old_sid, "the old Main ended and no live host could resume it")
            return {"outcome": "NEEDS_ATTENTION", "reason": "no live host"}
        if host.get("recover_session_id") != old_sid:
            self.needs_attention(old_sid, "the host has no recoverable session for the old Main")
            return {"outcome": "NEEDS_ATTENTION", "reason": "not recoverable"}
        ack = self.channel.wait(self.channel.request("resume", {"session_id": old_sid}))
        if (ack or {}).get("status") != "STARTED":
            self.needs_attention(old_sid, f"resume {(ack or {}).get('status') or 'UNANSWERED'}")
            return {"outcome": "NEEDS_ATTENTION", "reason": "resume refused"}
        deadline = float(self.clock()) + float(self.cfg.host_command_timeout_s)
        while True:
            r = self.reconcile()
            if r.get("state") == "adopted":
                same = r["session_id"] == old_sid
                return {"outcome": "RESUMED_SAME_ID" if same else "RESUMED_NEW_ID", "session_id": r["session_id"],
                        "note": "state restored from Claude Code's own transcript; exact previous state is not "
                                "verified by the supervisor"}
            if r.get("state") == "identity_mismatch" or float(self.clock()) >= deadline:
                self.needs_attention(old_sid, f"resumed Main not confirmed ({r.get('state')})")
                return {"outcome": "RESUME_UNCONFIRMED", "reconcile": r.get("state")}
            self.sleep(1.0)

    # -------------------------------------------------- settle (acknowledged and retried)

    def settle(self, session_id: str) -> bool:
        """Tell the host the replacement is confirmed. True only when the host ACKNOWLEDGED it (DONE). Otherwise the
        request is remembered and retried on later ticks."""
        ack = None
        try:
            ack = self.channel.wait(self.channel.request("settle", {"session_id": session_id}), 10.0)
        except RETRYABLE:
            ack = None
        if (ack or {}).get("status") == "DONE":
            self.pending_settle, self.settle_tries = None, 0
            return True
        self.pending_settle, self.settle_tries = session_id, 1
        self.store.append_event("main_settle_unconfirmed", session_id=session_id, ack=(ack or {}).get("status"))
        return False

    def retry_settle(self) -> None:
        sid = self.pending_settle
        if sid is None:
            return
        host = self.channel.host()
        if (self.settle_tries >= self.SETTLE_TRIES or host.get("state") in (S_EXITED, S_NEEDS_ATTENTION)
                or not host_fresh(host, float(self.clock()), float(self.cfg.host_heartbeat_stale_s), self.pid_alive)):
            self.pending_settle = None          # the host is gone or kept refusing; its own window expires
            return
        self.settle_tries += 1
        try:
            ack = self.channel.wait(self.channel.request("settle", {"session_id": sid}), 5.0)
        except RETRYABLE:
            return
        if (ack or {}).get("status") in ("DONE", "REJECTED"):
            self.pending_settle = None
