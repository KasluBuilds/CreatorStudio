"""Worker lifecycle for the development supervisor: assign, spawn, monitor, collect, restart, stop, recover.

T-DEV-002 (issue #44 part B). IMPLEMENTED and tested offline with a fake launcher and a fake clock. The real
``ClaudeCliLauncher`` builds an argument list for the ``claude`` CLI in print mode with stream-json output, an explicit
``--model`` and ``--session-id``, and feeds the task prompt file on stdin; it is never started by the unit tests and has
NOT been exercised end to end (NOT TESTED).

Design rules:
- Processes are started only through an injected ``ProcessLauncher``; argument lists only, never a shell string.
- Every intent is persisted before it is acted on (``STARTING`` is saved before the launch), so a crash leaves a record a
  fresh supervisor can reason about.
- Restarts are bounded: exponential backoff, a restart budget, and a hard stop (``BLOCKED`` plus a blocker record) after
  N identical failure signatures in a row.
- Results are ``REPORTED`` until an injected verifier decides; contradictory reports on one task are flagged.
- Recovery (``Supervisor.recover``) reloads the registry, reconciles with live pids, marks vanished workers ``LOST`` and
  returns a plan. It never launches, restarts or signals anything.
- A pid this supervisor process did not start is never signalled (pid reuse could hit an unrelated process).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from . import provider_env, worker_spawner
from .session_policy import PolicyInstallError

try:
    from .state_store import StateStore, StoreError, parse_iso, utc_iso
    from .workers import (DuplicateWorkerError, ResultRecord, ResultStatus, UnknownWorkerError, Worker,
                          WorkerRegistry, WorkerStatus)
except ImportError:  # pragma: no cover - standalone import
    from state_store import StateStore, StoreError, parse_iso, utc_iso  # type: ignore
    from workers import (DuplicateWorkerError, ResultRecord, ResultStatus, UnknownWorkerError, Worker,  # type: ignore
                         WorkerRegistry, WorkerStatus)

__all__ = ["SupervisorPolicy", "LaunchSpec", "LaunchHandle", "ProcessLauncher", "ClaudeCliLauncher",
           "GitWorktreeChecker", "Supervisor", "RecoveryPlan", "LifecycleError", "BackoffActive",
           "WorkerBlocked", "NotOwnedError", "WorktreeCheckFailed", "pid_alive", "build_claude_argv",
           "DuplicateWorkerError", "UnknownWorkerError"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
# A plain id (claude-style) or one provider/model gateway route, e.g. "auto/coding"; never a path, never a leading dash.
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,99}(/[A-Za-z0-9][A-Za-z0-9._:\-]{0,99})?$")
_SAFE_ARG = re.compile(r"^[A-Za-z0-9._:/\\\-=\[\]@+]+$")  # no shell or cmd.exe metacharacters, no spaces
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")  # claude 2.1.316 --help


class LifecycleError(StoreError):
    pass


class BackoffActive(LifecycleError):
    """A restart was requested before the backoff window elapsed."""


class WorkerBlocked(LifecycleError):
    """The worker is BLOCKED (restart budget or identical-failure limit reached); a human must unblock it."""


class NotOwnedError(LifecycleError):
    """The pid was not started by this supervisor process; it is never signalled."""


class WorktreeCheckFailed(LifecycleError):
    pass


@dataclass
class SupervisorPolicy:
    heartbeat_stale_s: float = 600.0     # no activity for this long -> STALE
    max_runtime_s: float = 4 * 3600.0    # bounded runtime: terminate, then kill, then FAILED("timeout")
    max_restarts: int = 3                # restart budget per worker
    identical_failure_limit: int = 3     # N identical failure signatures in a row -> BLOCKED
    backoff_base_s: float = 30.0
    backoff_max_s: float = 900.0
    stop_grace_s: float = 30.0           # graceful stop window before a kill
    auto_restart: bool = False           # tick() restarts eligible FAILED workers only when enabled
    stop_poll_s: float = 1.0

    def backoff(self, restart_count: int) -> float:
        return float(min(self.backoff_base_s * (2 ** max(0, restart_count)), self.backoff_max_s))


@dataclass
class LaunchSpec:
    argv: List[str]
    cwd: str
    stdin_path: str
    stdout_path: str
    stderr_path: str
    max_runtime_s: float
    env: Dict[str, str] = field(default_factory=dict)   # extra child environment (authority policy), merged last
    provider: str = ""                                  # the worker's recorded provider; the launch gate checks the pair


@dataclass
class LaunchHandle:
    pid: int


class ProcessLauncher(Protocol):
    def launch(self, spec: LaunchSpec) -> LaunchHandle: ...
    def owns(self, pid: int) -> bool: ...
    def poll(self, pid: int) -> Optional[int]: ...      # None while running, else the exit code
    def terminate(self, pid: int) -> None: ...          # graceful request
    def kill(self, pid: int) -> None: ...


class WorktreeChecker(Protocol):
    def __call__(self, worktree: str, branch: str) -> Tuple[bool, str]: ...


Verifier = Callable[[ResultRecord, Worker], Tuple[str, str]]


# ------------------------------------------------------------------ OS helpers

def pid_alive(pid: Optional[int]) -> bool:
    """Best-effort liveness probe. True does not prove identity: a pid can be reused by another process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: exists but not ours
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def build_claude_argv(model: str, session_id: str, executable: str = "claude",
                      permission_mode: Optional[str] = None, max_budget_usd: Optional[float] = None) -> List[str]:
    """Argument list for one non-interactive worker run. The prompt is NOT an argument: it is fed on stdin.

    Flags confirmed present in ``claude --help`` (2.1.316): -p/--print, --output-format stream-json, --verbose,
    --model, --session-id (must be a UUID), --permission-mode, --max-budget-usd (print mode only).
    """
    if not _MODEL.match(model or ""):
        raise LifecycleError(f"refusing model identifier {model!r}: an explicit, plain model id is required")
    try:
        sid = str(uuid.UUID(str(session_id)))
    except ValueError:
        raise LifecycleError("session id must be a UUID") from None
    argv = [executable, "-p", "--output-format", "stream-json", "--verbose", "--model", model, "--session-id", sid]
    if permission_mode is not None:
        if permission_mode not in PERMISSION_MODES:
            raise LifecycleError(f"unknown permission mode {permission_mode!r}")
        argv += ["--permission-mode", permission_mode]
    if max_budget_usd is not None:
        if not (isinstance(max_budget_usd, (int, float)) and 0 < float(max_budget_usd) <= 1000):
            raise LifecycleError("max_budget_usd must be a positive number up to 1000")
        argv += ["--max-budget-usd", f"{float(max_budget_usd):.2f}"]
    for a in argv[1:]:
        if not _SAFE_ARG.match(a):
            raise LifecycleError(f"unsafe argument {a!r}")
    return argv


_FORBIDDEN_WORKER_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def forbidden_worker_env(name: str) -> bool:
    """A direct Anthropic credential or login variable: never allowed in a worker environment, in any spelling."""
    up = name.upper()
    return (up in _FORBIDDEN_WORKER_ENV or "OAUTH" in up or up.endswith("API_KEY") or up.endswith("_API_KEY_FILE_DESCRIPTOR")
            or up.startswith("CLAUDE_CODE_USE_"))


def _no_worker_env(spec: "LaunchSpec") -> Dict[str, str]:
    """Default environment builder: there is none. A launcher built without an explicit, safe builder refuses to start a
    worker, so a worker can never fall back to the supervisor's own (Claude Pro) environment."""
    raise LifecycleError("no worker environment is configured: refusing to launch a worker (fail closed; "
                         "workers never inherit the supervisor's Claude login or a default provider)")


class ClaudeCliLauncher:
    """Runs ``claude`` as a child process from an argument list (``shell=False``), prompt on stdin, output to files.

    NOT TESTED end to end: unit tests use a fake launcher. On Windows the process gets its own process group so a
    graceful stop can send CTRL_BREAK; ``kill`` is the fallback after the grace window.
    """

    def __init__(self, executable: str = "claude", which: Callable[[str], Optional[str]] = shutil.which,
                 popen: Callable[..., Any] = subprocess.Popen,
                 env_builder: Callable[["LaunchSpec"], Dict[str, str]] = _no_worker_env,
                 extra_args: Sequence[str] = ()) -> None:
        self.executable = executable
        self._which, self._popen = which, popen
        self._env_builder = env_builder
        self._extra_args = list(extra_args)
        self._procs: Dict[int, Any] = {}

    def resolve_executable(self) -> str:
        path = self._which(self.executable)
        if not path:
            raise FileNotFoundError(f"{self.executable} not found on PATH")
        return path

    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        if not spec.argv or any(not isinstance(a, str) for a in spec.argv):
            raise LifecycleError("argv must be a non-empty list of strings")
        env = self._env_builder(spec)              # fail closed: the default builder raises
        if not isinstance(env, dict) or any(forbidden_worker_env(k) for k in env):
            raise LifecycleError("worker environment carries a direct Anthropic credential: refusing to launch")
        for k in spec.env:                         # only the authority-policy variables may be added, never overridden
            if not isinstance(k, str) or not k.upper().startswith("CS_AUTHORITY_"):
                raise LifecycleError("launch spec environment may only carry CS_AUTHORITY_ variables: refusing to launch")
            if any(k.upper() == e.upper() for e in env):
                raise LifecycleError("launch spec environment may not override the worker environment: refusing to launch")
        env = {**env, **spec.env}
        args = list(spec.argv[1:])
        sources = [a for a in args if a == "--setting-sources" or a.startswith("--setting-sources=")]
        if sources:
            # The authority policy already supplies --setting-sources (project,local). Whatever supplies it, user settings
            # must be excluded: a worker never loads the supervisor user's own settings.
            if len(sources) != 1:
                raise LifecycleError("worker command line carries more than one --setting-sources: refusing to launch")
            first = sources[0]
            value = first.split("=", 1)[1] if "=" in first else (args[args.index(first) + 1] if args.index(first) + 1 < len(args) else "")
            if not value or "user" in [v.strip() for v in value.split(",")]:
                raise LifecycleError("worker setting sources must exclude user settings: refusing to launch")
            lead: List[str] = []
        else:
            lead = list(self._extra_args)
        argv = [self.resolve_executable()] + lead + args
        kwargs: Dict[str, Any] = {"cwd": spec.cwd, "shell": False, "close_fds": True, "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        else:
            kwargs["start_new_session"] = True
        with open(spec.stdin_path, "rb") as fin, open(spec.stdout_path, "ab") as fout, \
                open(spec.stderr_path, "ab") as ferr:
            proc = self._popen(argv, stdin=fin, stdout=fout, stderr=ferr, **kwargs)
        self._procs[proc.pid] = proc
        return LaunchHandle(pid=proc.pid)

    def owns(self, pid: int) -> bool:
        return pid in self._procs

    def _proc(self, pid: int) -> Any:
        if pid not in self._procs:
            raise NotOwnedError(f"pid {pid} was not started by this launcher")
        return self._procs[pid]

    def poll(self, pid: int) -> Optional[int]:
        return self._proc(pid).poll()

    def terminate(self, pid: int) -> None:
        proc = self._proc(pid)
        if proc.poll() is not None:
            return
        if os.name == "nt":
            import signal
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
                return
            except (OSError, ValueError, AttributeError):
                pass
        proc.terminate()

    def kill(self, pid: int) -> None:
        proc = self._proc(pid)
        if proc.poll() is None:
            proc.kill()


class GitWorktreeChecker:
    """Checks that ``worktree`` is a git working tree whose checked-out branch is ``branch`` (argument lists only)."""

    def __init__(self, git: str = "git", timeout_s: float = 20.0,
                 run: Callable[..., Any] = subprocess.run) -> None:
        self.git, self.timeout_s, self._run = git, timeout_s, run

    def __call__(self, worktree: str, branch: str) -> Tuple[bool, str]:
        if not os.path.isdir(worktree):
            return False, "worktree directory does not exist"
        try:
            r = self._run([self.git, "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                          text=True, timeout=self.timeout_s, shell=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"git failed: {e.__class__.__name__}"
        if r.returncode != 0:
            return False, "not a git working tree"
        head = (r.stdout or "").strip()
        if head != branch:
            return False, f"worktree is on branch {head!r}, expected {branch!r}"
        return True, "ok"


# ------------------------------------------------------------------ recovery plan

@dataclass
class RecoveryItem:
    worker_id: str
    task_id: str
    previous_status: str
    status: str
    action: str
    detail: str


@dataclass
class RecoveryPlan:
    generated_at: str
    items: List[RecoveryItem] = field(default_factory=list)
    results_awaiting_verification: List[str] = field(default_factory=list)
    contradictions: List[Dict[str, Any]] = field(default_factory=list)
    blockers: List[Dict[str, Any]] = field(default_factory=list)
    store_recoveries: List[Dict[str, Any]] = field(default_factory=list)
    relaunched: int = 0  # always 0: recovery never launches anything

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ the supervisor

class Supervisor:
    def __init__(self, store: StateStore, launcher: ProcessLauncher, *,
                 worktree_checker: WorktreeChecker, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, policy: Optional[SupervisorPolicy] = None,
                 verifier: Optional[Verifier] = None, pid_probe: Callable[[Optional[int]], bool] = pid_alive,
                 session_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
                 activity_probe: Optional[Callable[[Worker], Optional[float]]] = None,
                 executable: str = "claude", lock: bool = True) -> None:
        self.store, self.launcher = store, launcher
        self.check_worktree = worktree_checker
        self.clock, self.sleep = clock, sleep
        self.policy = policy or SupervisorPolicy()
        self.verifier = verifier
        self.pid_probe = pid_probe
        self.session_factory = session_factory
        self.activity_probe = activity_probe or self._log_activity
        self.executable = executable
        self._locked = False
        if lock:
            if store.pid_alive is None:
                store.pid_alive = pid_probe
            store.acquire_lock("dev_supervisor")
            self._locked = True
        self.registry = WorkerRegistry(store)

    def close(self) -> None:
        if self._locked:
            self.store.release_lock()
            self._locked = False

    # -------------------------------------------------- helpers

    def _now(self) -> float:
        return float(self.clock())

    def _event(self, kind: str, w: Optional[Worker] = None, **data: Any) -> None:
        if w is not None:
            data = {"worker_id": w.worker_id, "task_id": w.task_id, "status": w.status, **data}
        self.store.append_event(kind, **data)

    def _log_dir(self, w: Worker) -> Path:
        d = self.store.directory / "logs" / w.worker_id
        os.makedirs(str(d), exist_ok=True)
        return d

    def stdout_path(self, w: Worker) -> Path:
        return self.store.directory / "logs" / w.worker_id / f"run-{w.restart_count}.stream.jsonl"

    def _log_activity(self, w: Worker) -> Optional[float]:
        try:
            return os.stat(str(self.stdout_path(w))).st_mtime
        except OSError:
            return None

    # -------------------------------------------------- assign / spawn

    def assign(self, *, task_id: str, role: str, branch: str, worktree: str, owner_stream: str, model: str,
               prompt_file: str, provider: str) -> Worker:
        for label, v in (("task_id", task_id), ("role", role), ("owner_stream", owner_stream),
                         ("provider", provider)):
            if not _ID.match(v or ""):
                raise LifecycleError(f"invalid {label} {v!r}")
        if not _MODEL.match(model or ""):
            raise LifecycleError("an explicit model id is required")
        if provider.strip().lower() == "anthropic":
            raise LifecycleError("provider anthropic (Claude Pro) is never a worker provider")
        if worker_spawner.is_claude_model(model):
            raise LifecycleError("a Claude model is never a worker model")
        if not branch or branch.startswith("-") or any(c.isspace() for c in branch):
            raise LifecycleError(f"invalid branch {branch!r}")
        worktree = os.path.abspath(worktree)
        other = self.registry.find_conflict(task_id, worktree)
        if other is not None:
            self._event("assign_refused_duplicate", other, requested_task=task_id)
            raise DuplicateWorkerError(f"active worker {other.worker_id} already holds task {other.task_id} "
                                       f"or this worktree")
        ok, reason = self.check_worktree(worktree, branch)
        if not ok:
            self._event("assign_refused_worktree", None, task_id=task_id, reason=reason)
            raise WorktreeCheckFailed(f"worktree/branch check failed: {reason}")
        w = Worker(worker_id=self.registry.new_id(), role=role, task_id=task_id, branch=branch,
                   worktree=worktree, owner_stream=owner_stream, provider=provider, model=model,
                   prompt_file=os.path.abspath(prompt_file), assigned_at=utc_iso(self._now()))
        self.registry.add(w)
        self._event("worker_assigned", w, branch=branch, model=model, provider=provider)
        return w

    def build_spec(self, w: Worker) -> LaunchSpec:
        argv = build_claude_argv(w.model, w.session_id or "", executable=self.executable)
        d = self._log_dir(w)
        return LaunchSpec(argv=argv, cwd=w.worktree, stdin_path=str(w.prompt_file),
                          stdout_path=str(self.stdout_path(w)), stderr_path=str(d / f"run-{w.restart_count}.stderr.log"),
                          max_runtime_s=self.policy.max_runtime_s, provider=w.provider)

    def spawn(self, worker_id: str) -> Worker:
        w = self.registry.get(worker_id)
        if w.status != WorkerStatus.ASSIGNED:
            raise LifecycleError(f"worker {worker_id} is {w.status}; only an ASSIGNED worker can be spawned")
        return self._launch(w)

    def _launch(self, w: Worker) -> Worker:
        if not w.prompt_file or not os.path.isfile(w.prompt_file):
            raise LifecycleError("task prompt file does not exist")
        ok, reason = self.check_worktree(w.worktree, w.branch)
        if not ok:
            raise WorktreeCheckFailed(f"worktree/branch check failed: {reason}")
        if w.session_id:
            w.previous_session_ids.append(w.session_id)
        w.session_id = str(uuid.UUID(str(self.session_factory())))
        w.pid, w.exit_code, w.ended_at, w.stop_requested_at = None, None, None, None
        try:
            spec = self.build_spec(w)
        except PolicyInstallError as e:       # never launch a worker without its authority hook
            self._event("policy_install_failed", w, error=str(e)[:200])
            self._fail(w, "policy_install_failed")
            return w
        w.status = WorkerStatus.STARTING
        self.registry.update(w)               # intent persisted before the launch
        self._event("launch_begin", w, session_id=w.session_id, restart_count=w.restart_count)
        try:
            handle = self.launcher.launch(spec)
        except Exception as e:  # noqa: BLE001 - every launch error becomes a recorded failure signature
            self._fail(w, f"launch_error:{e.__class__.__name__}")
            return w
        now = self._now()
        w.pid, w.started_at, w.last_heartbeat = int(handle.pid), utc_iso(now), utc_iso(now)
        w.status = WorkerStatus.RUNNING
        self.registry.update(w)
        self._event("launch_done", w, pid=w.pid)
        return w

    # -------------------------------------------------- failure, backoff, blocking

    def _fail(self, w: Worker, signature: str) -> None:
        now = self._now()
        w.failures.append(signature)
        w.failures = w.failures[-50:]
        w.ended_at = utc_iso(now)
        w.status = WorkerStatus.FAILED
        w.next_restart_not_before = utc_iso(now + self.policy.backoff(w.restart_count))
        n = self.policy.identical_failure_limit
        identical = n > 0 and len(w.failures) >= n and len(set(w.failures[-n:])) == 1
        if identical or w.restart_count >= self.policy.max_restarts:
            reason = (f"{n} identical failures in a row: {signature}" if identical
                      else f"restart budget exhausted ({w.restart_count}/{self.policy.max_restarts}); last: {signature}")
            self._block(w, reason)
            return
        self.registry.update(w)
        self._event("worker_failed", w, signature=signature, next_restart_not_before=w.next_restart_not_before)

    def _block(self, w: Worker, reason: str) -> None:
        w.status = WorkerStatus.BLOCKED
        w.next_restart_not_before = None
        blocker = {"worker_id": w.worker_id, "task_id": w.task_id, "reason": reason,
                   "failures": list(w.failures[-self.policy.identical_failure_limit:]),
                   "recorded_at": utc_iso(self._now()), "resolved": False}
        self.registry.blockers.append(blocker)
        self.registry.update(w)
        self._event("worker_blocked", w, reason=reason)

    def unblock(self, worker_id: str, reason: str) -> Worker:
        """A human decision: clear the failure history and restart budget of a BLOCKED worker (it becomes FAILED)."""
        w = self.registry.get(worker_id)
        if w.status != WorkerStatus.BLOCKED:
            raise LifecycleError(f"worker {worker_id} is {w.status}, not BLOCKED")
        if not reason.strip():
            raise LifecycleError("a reason is required")
        for b in self.registry.blockers:
            if b["worker_id"] == worker_id and not b["resolved"]:
                b["resolved"], b["resolution"] = True, reason
        w.failures, w.restart_count, w.next_restart_not_before = [], 0, None
        w.status = WorkerStatus.FAILED
        self.registry.update(w)
        self._event("worker_unblocked", w, reason=reason)
        return w

    def restart(self, worker_id: str) -> Worker:
        w = self.registry.get(worker_id)
        if w.status == WorkerStatus.BLOCKED:
            raise WorkerBlocked(f"worker {worker_id} is BLOCKED; unblock it first")
        if w.status not in (WorkerStatus.FAILED, WorkerStatus.LOST, WorkerStatus.STOPPED):
            raise LifecycleError(f"worker {worker_id} is {w.status}; stop it before restarting")
        other = self.registry.find_conflict(w.task_id, w.worktree, ignore=w.worker_id)
        if other is not None:
            raise DuplicateWorkerError(f"active worker {other.worker_id} now holds this task or worktree")
        if w.restart_count >= self.policy.max_restarts:
            self._block(w, f"restart budget exhausted ({w.restart_count}/{self.policy.max_restarts})")
            raise WorkerBlocked(f"worker {worker_id} reached the restart budget")
        nb = parse_iso(w.next_restart_not_before)
        if nb is not None and self._now() < nb:
            self._event("restart_refused_backoff", w, not_before=w.next_restart_not_before)
            raise BackoffActive(f"restart not before {w.next_restart_not_before}")
        w.restart_count += 1
        self._event("worker_restart", w, restart_count=w.restart_count)
        return self._launch(w)

    # -------------------------------------------------- monitor

    def heartbeat(self, worker_id: str, at: Optional[float] = None) -> Worker:
        w = self.registry.get(worker_id)
        if w.status not in WorkerStatus.LIVE:
            return w
        w.last_heartbeat = utc_iso(self._now() if at is None else at)
        if w.status == WorkerStatus.STALE:
            w.status = WorkerStatus.RUNNING
            self._event("worker_heartbeat_resumed", w)
        self.registry.update(w)
        return w

    def monitor(self) -> List[Dict[str, Any]]:
        """One pass over live workers. Returns the status changes made."""
        changes: List[Dict[str, Any]] = []
        for w in self.registry.list(WorkerStatus.LIVE):
            before = w.status
            try:
                self._monitor_one(w)
            except NotOwnedError:
                pass
            if w.status != before:
                changes.append({"worker_id": w.worker_id, "from": before, "to": w.status})
        return changes

    def _monitor_one(self, w: Worker) -> None:
        now = self._now()
        if w.pid is None:
            return  # STARTING without pid: only recovery can judge it
        if not self.launcher.owns(w.pid):
            if not self.pid_probe(w.pid):
                self._mark_lost(w, "pid no longer alive")
            return
        code = self.launcher.poll(w.pid)
        if code is not None:
            w.exit_code, w.ended_at = int(code), utc_iso(now)
            if w.status == WorkerStatus.STOPPING:
                w.status = WorkerStatus.STOPPED
                self.registry.update(w)
                self._event("worker_stopped", w, exit_code=w.exit_code)
            elif code == 0:
                w.status = WorkerStatus.EXITED
                self.registry.update(w)
                self._event("worker_exited", w, exit_code=0)
            else:
                self._fail(w, f"exit:{int(code)}")
            return
        if w.status == WorkerStatus.STOPPING:
            req = parse_iso(w.stop_requested_at) or now
            if now - req >= self.policy.stop_grace_s:
                self.launcher.kill(w.pid)
                w.status, w.ended_at = WorkerStatus.STOPPED, utc_iso(now)
                self.registry.update(w)
                self._event("worker_killed_after_grace", w)
            return
        started = parse_iso(w.started_at) or now
        if now - started > self.policy.max_runtime_s:
            self.launcher.terminate(w.pid)
            self.launcher.kill(w.pid)
            self._fail(w, "timeout")
            return
        act = self.activity_probe(w)
        hb = parse_iso(w.last_heartbeat)
        if act is not None and (hb is None or act > hb):
            w.last_heartbeat, hb = utc_iso(act), act
            if w.status == WorkerStatus.STALE:
                w.status = WorkerStatus.RUNNING
                self._event("worker_heartbeat_resumed", w)
            self.registry.update(w)
        if w.status == WorkerStatus.RUNNING and hb is not None and now - hb > self.policy.heartbeat_stale_s:
            w.status = WorkerStatus.STALE
            self.registry.update(w)
            self._event("worker_stale", w, seconds_since_heartbeat=int(now - hb))

    def _mark_lost(self, w: Worker, why: str) -> None:
        w.status, w.ended_at = WorkerStatus.LOST, utc_iso(self._now())
        w.note = why
        self.registry.update(w)
        self._event("worker_lost", w, reason=why)

    def tick(self) -> Dict[str, Any]:
        """Monitor, then (only when ``policy.auto_restart``) restart FAILED workers whose backoff has elapsed."""
        changes = self.monitor()
        restarted, refused = [], []
        if self.policy.auto_restart:
            for w in self.registry.list([WorkerStatus.FAILED]):
                try:
                    self.restart(w.worker_id)
                    restarted.append(w.worker_id)
                except (BackoffActive, WorkerBlocked, DuplicateWorkerError, LifecycleError) as e:
                    refused.append({"worker_id": w.worker_id, "reason": e.__class__.__name__})
        return {"changes": changes, "restarted": restarted, "refused": refused}

    # -------------------------------------------------- stop

    def stop(self, worker_id: str, wait: bool = False) -> Worker:
        w = self.registry.get(worker_id)
        now = self._now()
        if w.status == WorkerStatus.ASSIGNED:
            w.status, w.ended_at = WorkerStatus.STOPPED, utc_iso(now)
            self.registry.update(w)
            self._event("worker_stopped", w, note="never launched")
            return w
        if w.status not in WorkerStatus.LIVE:
            return w
        if w.pid is None or not self.launcher.owns(w.pid):
            raise NotOwnedError(f"worker {worker_id} pid {w.pid} was not started by this supervisor process; "
                                "it is not signalled (stop it by hand, then record it)")
        if w.status != WorkerStatus.STOPPING:
            w.status, w.stop_requested_at = WorkerStatus.STOPPING, utc_iso(now)
            self.registry.update(w)
            self._event("worker_stop_requested", w)
            self.launcher.terminate(w.pid)
        if wait:
            deadline = now + self.policy.stop_grace_s
            while True:
                self._monitor_one(w)
                if w.status != WorkerStatus.STOPPING:
                    break
                if self._now() >= deadline:
                    self._monitor_one(w)  # grace elapsed: kills
                    break
                self.sleep(self.policy.stop_poll_s)
        return w

    # -------------------------------------------------- results

    def collect(self, worker_id: str, outcome: str, claims: Optional[Dict[str, Any]] = None,
                ref: Optional[str] = None) -> ResultRecord:
        w = self.registry.get(worker_id)
        if not _ID.match(outcome or ""):
            raise LifecycleError(f"invalid outcome {outcome!r}")
        clean = {str(k)[:64]: str(v)[:200] for k, v in (claims or {}).items()}
        r = ResultRecord(result_id=self.registry.new_id("r"), worker_id=w.worker_id, task_id=w.task_id,
                         outcome=outcome, claims=clean, ref=ref, reported_at=utc_iso(self._now()))
        self.registry.results[r.result_id] = r
        w.last_result_ref = r.result_id
        self.registry.workers[w.worker_id] = w
        self._detect_contradictions(r)
        self.registry.save()
        self._event("result_reported", w, result_id=r.result_id, outcome=outcome)
        return r

    def collect_report_file(self, worker_id: str, path: str) -> ResultRecord:
        """Collect a lane-style ``report.json``: only its status and command count are kept, plus the path."""
        with open(path, "r", encoding="utf-8") as fh:
            rep = json.load(fh)
        if not isinstance(rep, dict) or not isinstance(rep.get("status"), str):
            raise LifecycleError("report has no status")
        cmds = rep.get("commands") if isinstance(rep.get("commands"), list) else []
        unresolved = rep.get("unresolved") if isinstance(rep.get("unresolved"), list) else []
        return self.collect(worker_id, rep["status"],
                            {"commands": str(len(cmds)), "unresolved": str(len(unresolved))}, ref=path)

    def _detect_contradictions(self, new: ResultRecord) -> None:
        for other in self.registry.results_for_task(new.task_id):
            if other.result_id == new.result_id or other.worker_id == new.worker_id:
                continue
            if other.status == ResultStatus.REJECTED:
                continue
            diffs = []
            if other.outcome != new.outcome:
                diffs.append("outcome")
            for k in sorted(set(other.claims) & set(new.claims)):
                if other.claims[k] != new.claims[k]:
                    diffs.append(f"claims.{k}")
            if not diffs:
                continue
            other.contradiction = new.contradiction = True
            rec = {"task_id": new.task_id, "results": sorted([other.result_id, new.result_id]), "fields": diffs,
                   "detected_at": utc_iso(self._now()), "resolved": False}
            self.registry.contradictions.append(rec)
            self.store.append_event("result_contradiction", task_id=new.task_id, results=rec["results"],
                                    fields=diffs)

    def open_contradictions(self, result_id: str) -> List[Dict[str, Any]]:
        out = []
        for c in self.registry.contradictions:
            if c["resolved"] or result_id not in c["results"]:
                continue
            if any(self.registry.results[r].status == ResultStatus.REJECTED for r in c["results"]
                   if r in self.registry.results):
                c["resolved"] = True
                continue
            out.append(c)
        return out

    def verify_result(self, result_id: str) -> ResultRecord:
        if self.verifier is None:
            raise LifecycleError("no verifier configured: the result stays REPORTED")
        r = self.registry.results.get(result_id)
        if r is None:
            raise LifecycleError(f"no result {result_id}")
        if r.status != ResultStatus.REPORTED:
            return r
        w = self.registry.get(r.worker_id)
        verdict, note = self.verifier(r, w)
        if verdict not in (ResultStatus.VERIFIED, ResultStatus.REJECTED):
            r.verifier_note = f"verifier gave no verdict ({verdict!r}); stays REPORTED"
        elif verdict == ResultStatus.VERIFIED and self.open_contradictions(result_id):
            r.verifier_note = "contradicted by another worker's report; reject one side first; stays REPORTED"
        else:
            r.status, r.decided_at, r.verifier_note = verdict, utc_iso(self._now()), str(note)[:500]
            if verdict == ResultStatus.REJECTED:
                self.open_contradictions(result_id)  # resolves contradictions this rejection settles
        self.registry.save()
        self._event("result_decided", w, result_id=result_id, result_status=r.status)
        return r

    # -------------------------------------------------- recovery

    def recover(self) -> RecoveryPlan:
        """Reconcile the persisted registry with live pids. Never launches, restarts or signals a process."""
        self.registry.load()
        now = self._now()
        plan = RecoveryPlan(generated_at=utc_iso(now), store_recoveries=list(self.store.recoveries))
        for w in self.registry.list():
            prev = w.status
            if w.status == WorkerStatus.STARTING and w.pid is None:
                self._mark_lost(w, "supervisor stopped during launch; launch outcome unknown")
                action = "verify_before_continuing"
                detail = (f"check for a claude session {w.session_id} and changes in the worktree before any "
                          "restart; the launch may or may not have happened")
            elif w.status in WorkerStatus.LIVE:
                owned = w.pid is not None and self.launcher.owns(w.pid)
                if owned:
                    action, detail = "monitor", "process owned by this supervisor"
                elif self.pid_probe(w.pid):
                    action = "observe_unowned"
                    detail = ("pid is alive but was not started by this supervisor process (identity unconfirmed: "
                              "pids can be reused); it will not be signalled; stop it by hand if needed")
                else:
                    self._mark_lost(w, "pid not alive at recovery")
                    action = "inspect_then_restart_manually"
                    detail = "inspect the worktree and the last output log; restart only by an explicit restart call"
            elif w.status == WorkerStatus.ASSIGNED:
                action, detail = "spawn_when_ready", "assigned, never launched"
            elif w.status == WorkerStatus.FAILED:
                action = "restart_eligible"
                detail = f"restart not before {w.next_restart_not_before}; last failure {w.failures[-1:]}"
            elif w.status == WorkerStatus.BLOCKED:
                action, detail = "needs_human", "blocked: see blockers"
            elif w.status == WorkerStatus.LOST:
                action, detail = "inspect_then_restart_manually", w.note or "lost earlier"
            else:
                continue  # STOPPED / EXITED: nothing to do
            plan.items.append(RecoveryItem(w.worker_id, w.task_id, prev, w.status, action, detail))
        plan.results_awaiting_verification = [r.result_id for r in self.registry.results.values()
                                              if r.status == ResultStatus.REPORTED]
        plan.contradictions = [c for c in self.registry.contradictions if not c.get("resolved")]
        plan.blockers = [b for b in self.registry.blockers if not b.get("resolved")]
        self.store.append_event("recovery_plan", items=len(plan.items),
                                lost=sum(1 for i in plan.items if i.status == WorkerStatus.LOST))
        return plan
