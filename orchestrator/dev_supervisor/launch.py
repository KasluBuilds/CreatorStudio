"""Launch the interactive Main session and observe it (T-DEV-008, wave w03 lane L8).

Decision (resolves the open point from lane L3): Main is an INTERACTIVE ``claude`` session in its own console window;
workers stay headless (``claude -p`` through the L2 ``ClaudeCliLauncher``).

- Main argv (argument list, never a shell string)::

      claude --model <main model> --session-id <uuid> --name creatorstudio-main
             --append-system-prompt-file <state dir>/prompts/main-<uuid>.prompt.txt
             [--permission-mode <configured mode>] [--dangerously-skip-permissions]
             "<initial instruction>"

  ``--dangerously-skip-permissions`` is passed ONLY when the machine-local config sets
  ``dangerously_skip_permissions: true``. The appended prompt file lives in the machine-local state directory and
  carries the operating instructions plus, after a replacement, the verified handoff path and content hash.
- On Windows the terminal is a new console window opened with ``CREATE_NEW_CONSOLE`` (``subprocess.Popen`` with an
  argument list, ``shell=False``; no ``cmd /c start`` and no ``wt`` string parsing). The working directory is the
  repository root. A ``.cmd``/``.bat`` claude shim is refused (cmd.exe would re-parse the arguments).
- The child environment comes from ``provider_env.child_env`` (default Claude Pro: gateway overrides removed). Earlier text: the Pro versus OmniRoute choice was NOT made here: the child inherits the environment of the terminal the user
  opened with the launcher of their choice. This module never reads, copies or edits any launcher or ``.mcp.json``.

Limits (stated, not hidden):
- The supervisor observes an interactive Main only through the Claude Code session file (``<pid>.json``) and the
  transcript tail (lane L1). It cannot type into the session (no keystroke injection).
- "Compacting" an interactive Main therefore means: Main writes a handoff, the supervisor verifies it by read-back,
  then replaces the session (lane L3 ``replace_session`` through ``InteractiveMainLauncher``). ``/compact`` is never
  sent.
- An old Main the current supervisor process did not start is never signalled; it is reported for the user to close.

Status: IMPLEMENTED and tested offline with a fake terminal. The interactive Main launch is NOT TESTED live.
Flag names were checked against ``claude --help`` (2.1.316); ``--append-system-prompt-file`` is not listed there (only
named in the ``--bare`` help text) and was OBSERVED accepted by the CLI in print mode (see the L8 lane report).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

from . import provider_env, session_policy
from .config import DANGEROUS_MODES, PERMISSION_MODES, Config
from .evidence import Evidence, observed, unavailable
from .lifecycle import LaunchHandle, LaunchSpec, LifecycleError, NotOwnedError
from .state_store import StateStore, utc_iso

MAIN_ROLE = "main"
MAIN_DOC = "main"
SESSION_NAME = "creatorstudio-main"
PROMPT_SUBDIR = "prompts"
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,99}$")
DEFAULT_INITIAL_PROMPT = ("Read the appended system prompt, then follow it: confirm the repository state and continue "
                          "as Main under the development supervisor.")

OPERATING_INSTRUCTIONS = """You are Main, the coordinating Claude Code session of this repository, running under the \
development supervisor (python -B -m dev_supervisor, run from orchestrator/).
Operating rules:
1. Read CLAUDE.md, PROJECT_OPERATING_PROTOCOL.md and SESSION_HANDOFF.md first; they override anything here.
2. Delegate large work to headless workers: write a task prompt file outside the repository, then
   python -B -m dev_supervisor assign --task <id> --role <role> --branch <branch> --worktree <path> \
--owner-stream <stream> --prompt-file <file> --yes
   The supervisor loop spawns, monitors and restarts workers; check them with: python -B -m dev_supervisor workers
3. Check your own context pressure with: python -B -m dev_supervisor compact-check
   When it says COMPACT or REPLACE, write a handoff (summary of recorded facts, never transcript text) as a JSON facts \
file and run: python -B -m dev_supervisor handoff --input <facts.json> --yes
   A session is only replaced after that handoff verifies by read-back. A fresh session does NOT replenish account \
usage.
4. Never print, store or commit secrets, tokens or keys. The repository is public.
5. Results stay REPORTED until verified with command output; never claim VERIFIED without evidence.
"""

STANDING_AUTHORITY = """STANDING AUTHORITY (Lukas, 2026-11-20; enforced by the authority hook, see \
orchestrator/dev_supervisor/STANDING_AUTONOMY.md):
- Routine, no approval needed: register tasks, dispatch and replace workers, branches, commits, push task/lane branches, \
open PRs, reviews, run the 8 integration gates, merge ONLY when every gate passes, safe recovery, your own supervisor \
maintenance.
- Protected: paid API, spending, destructive data operations, irreversible production changes (Resolve production \
mutation), disabling safety controls, changing or expanding the authority boundaries (grants, hook settings, approvals), \
launchers/tools/.mcp.json, publishing outward-facing content.
- Never ask Lukas for routine operations. Protected actions are queued as pending approvals by the hook; continue with \
unrelated work. Only Lukas approves, from his own terminal. If a gate fails, stop and report; never force it.
"""


class LaunchError(LifecycleError):
    """Main could not be launched (nothing was started)."""


# ------------------------------------------------------------------ profile and argv

@dataclass(frozen=True)
class MainProfile:
    """Only flags: model, permission mode, the dangerous flag when configured. Nothing about providers or launchers."""
    model: str
    executable: str = "claude"
    permission_mode: Optional[str] = None
    dangerously_skip_permissions: bool = False
    initial_prompt: str = DEFAULT_INITIAL_PROMPT
    name: str = SESSION_NAME

    @classmethod
    def from_config(cls, cfg: Config) -> "MainProfile":
        return cls(model=cfg.model(MAIN_ROLE), executable=cfg.claude_executable,
                   permission_mode=cfg.main_permission_mode,
                   dangerously_skip_permissions=cfg.dangerously_skip_permissions is True)


def _uuid(session_id: str) -> str:
    try:
        return str(uuid.UUID(str(session_id)))
    except ValueError:
        raise LaunchError("session id must be a UUID") from None


def build_main_argv(profile: MainProfile, session_id: str, prompt_file: os.PathLike | str) -> List[str]:
    """Argument list for the interactive Main. Raises ``LaunchError`` on anything unsafe."""
    if not _MODEL.match(profile.model or ""):
        raise LaunchError(f"refusing model identifier {profile.model!r}: an explicit, plain model id is required")
    sid = _uuid(session_id)
    argv = [profile.executable, "--model", profile.model, "--session-id", sid, "--name", profile.name,
            "--append-system-prompt-file", str(prompt_file)]
    if profile.permission_mode is not None:
        if profile.permission_mode not in PERMISSION_MODES:
            raise LaunchError(f"unknown permission mode {profile.permission_mode!r}")
        if profile.permission_mode in DANGEROUS_MODES and profile.dangerously_skip_permissions is not True:
            raise LaunchError(f"permission mode {profile.permission_mode} needs dangerously_skip_permissions: true "
                              "in the machine-local config")
        argv += ["--permission-mode", profile.permission_mode]
    if profile.dangerously_skip_permissions is True:
        argv.append("--dangerously-skip-permissions")
    prompt = (profile.initial_prompt or "").strip()
    if prompt:
        if prompt.startswith("-") or "\n" in prompt or "\x00" in prompt:
            raise LaunchError("the initial prompt must be one line and must not start with '-'")
        argv.append(prompt)
    return argv


def write_main_prompt(store: StateStore, session_id: str, *, verified: Any = None,
                      resume_prompt_file: Optional[str] = None) -> Path:
    """Write the appended system prompt for one Main session into ``<state>/prompts/`` (never the repository)."""
    sid = _uuid(session_id)
    parts = [OPERATING_INSTRUCTIONS.strip(), "", STANDING_AUTHORITY.strip(), "", f"Your session id is {sid}."]
    if verified is not None:
        parts += ["", "You replace an earlier Main. Before doing anything else read the verified handoff JSON at",
                  f"{verified.json_path}", f"and confirm its content hash is {verified.content_hash}.",
                  "If the file is missing or the hash differs, stop and report; do not continue the work."]
    if resume_prompt_file:
        try:
            extra = Path(resume_prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            raise LaunchError(f"resume prompt file unreadable ({e.__class__.__name__})") from None
        parts += ["", extra.strip()]
    d = store.directory / PROMPT_SUBDIR
    os.makedirs(str(d), exist_ok=True)
    p = d / f"main-{sid}.prompt.txt"
    store.atomic_write_bytes(p, ("\n".join(parts) + "\n").encode("utf-8"))
    return p


# ------------------------------------------------------------------ terminal

class TerminalHandle(Protocol):
    pid: int
    def poll(self) -> Optional[int]: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class Terminal(Protocol):
    def open(self, argv: List[str], cwd: str, env: Optional[Mapping[str, str]] = None) -> TerminalHandle: ...


class NewConsoleTerminal:
    """Start a program in its own new console window (Windows). NOT TESTED live for an interactive Main."""

    def __init__(self, which: Callable[[str], Optional[str]] = shutil.which, popen: Callable[..., Any] = subprocess.Popen,
                 os_name: Optional[str] = None) -> None:
        self._which, self._popen = which, popen
        self.os_name = os_name or os.name

    def resolve(self, executable: str) -> str:
        path = self._which(executable)
        if not path:
            raise LaunchError(f"{executable} not found on PATH")
        if self.os_name == "nt" and Path(path).suffix.lower() in (".cmd", ".bat"):
            raise LaunchError("refusing a .cmd/.bat claude shim: cmd.exe would re-parse the arguments; "
                              "install the native claude executable")
        return path

    def open(self, argv: List[str], cwd: str, env: Optional[Mapping[str, str]] = None) -> Any:
        if self.os_name != "nt":
            raise LaunchError("an interactive Main in a new terminal window is implemented for Windows only")
        if not argv or any(not isinstance(a, str) for a in argv):
            raise LaunchError("argv must be a non-empty list of strings")
        exe = self.resolve(argv[0])
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
        return self._popen([exe] + list(argv[1:]), cwd=cwd, shell=False, close_fds=True, creationflags=flags,
                           env={**provider_env.child_env(), **(env or {})})


# ------------------------------------------------------------------ the interactive Main launcher

def claude_project_slug(repo_root: os.PathLike) -> str:
    """Claude Code's project folder name: the path with every non-alphanumeric character replaced by "-"."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(repo_root))


def main_roots(repo: Path, store_dir: os.PathLike, home: Optional[Path] = None) -> List[str]:
    """Paths a hooked Main may read and write: repo, sibling worktrees, supervisor state, its Claude project folder."""
    home = Path(home) if home is not None else Path.home()
    return [str(repo), str(repo.parent / "CreatorStudio-worktrees"), str(Path(store_dir)),
            str(home / ".claude" / "projects" / claude_project_slug(repo))]


class InteractiveMainLauncher:
    """Implements the L2 ``ProcessLauncher`` protocol for Main, so L3 ``replace_session`` can relaunch Main.

    ``launch(spec)`` takes the model and session id from the headless argv L3 builds, uses ``spec.stdin_path`` (the L3
    resume prompt that points at the verified handoff) as extra prompt text, and opens an INTERACTIVE session instead.
    """

    def __init__(self, store: StateStore, terminal: Terminal, profile: MainProfile, cwd: str,
                 policy_install: Callable[..., Any] = session_policy.install) -> None:
        self.store, self.terminal, self.profile, self.cwd = store, terminal, profile, cwd
        self.policy_install = policy_install
        self._handles: Dict[int, Any] = {}
        self.launched: List[Dict[str, Any]] = []

    def open_main(self, session_id: str, *, model: Optional[str] = None, cwd: Optional[str] = None,
                  verified: Any = None, resume_prompt_file: Optional[str] = None) -> Tuple[int, List[str], Path]:
        profile = self.profile if model is None else MainProfile(**{**self.profile.__dict__, "model": model})
        prompt = write_main_prompt(self.store, session_id, verified=verified, resume_prompt_file=resume_prompt_file)
        argv = build_main_argv(profile, session_id, prompt)
        repo = Path(self.cwd).resolve()
        roots = main_roots(repo, self.store.directory)
        try:
            inst = self.policy_install(self.store.directory, _uuid(session_id), "main", str(repo), roots=roots)
            argv = session_policy.argv_with_policy(argv, inst)
        except session_policy.PolicyInstallError as e:
            raise LaunchError(f"authority policy could not be installed; Main not opened: {str(e)[:200]}") from None
        handle = self.terminal.open(argv, cwd or self.cwd, env=inst.env)
        pid = int(handle.pid)
        self._handles[pid] = handle
        self.launched.append({"session_id": _uuid(session_id), "pid": pid})
        return pid, argv, prompt

    # ProcessLauncher protocol (used by replacement.replace_session)
    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        argv = list(spec.argv)
        try:
            model = argv[argv.index("--model") + 1]
            sid = argv[argv.index("--session-id") + 1]
        except (ValueError, IndexError):
            raise LaunchError("launch spec carries no --model/--session-id") from None
        pid, _argv, _prompt = self.open_main(sid, model=model, cwd=spec.cwd, resume_prompt_file=spec.stdin_path)
        return LaunchHandle(pid=pid)

    def owns(self, pid: int) -> bool:
        return pid in self._handles

    def _handle(self, pid: int) -> Any:
        if pid not in self._handles:
            raise NotOwnedError(f"pid {pid} was not started by this launcher")
        return self._handles[pid]

    def poll(self, pid: int) -> Optional[int]:
        return self._handle(pid).poll()

    def terminate(self, pid: int) -> None:
        h = self._handle(pid)
        if h.poll() is None:
            h.terminate()

    def kill(self, pid: int) -> None:
        h = self._handle(pid)
        if h.poll() is None:
            h.kill()


# ------------------------------------------------------------------ observing Main (L1 session files)

def find_session(sessions: List[Dict[str, Any]], session_id: str) -> Optional[Dict[str, Any]]:
    for s in sessions:
        if "error" in s:
            continue
        ev = s.get("session_id")
        if isinstance(ev, Evidence) and ev.available and ev.value == session_id:
            return s
    return None


class SessionFileHeartbeat:
    """L3 ``HeartbeatProbe``: the first heartbeat of a session is its Claude Code session file with a live pid."""

    def __init__(self, sessions_dir: os.PathLike | str, read_sessions: Callable[..., List[Dict[str, Any]]],
                 pid_alive: Callable[[int], bool], clock: Callable[[], float] = time.time) -> None:
        self.sessions_dir, self._read, self.pid_alive, self.clock = sessions_dir, read_sessions, pid_alive, clock

    def first_heartbeat(self, session_id: str, pid: int) -> Optional[float]:
        s = find_session(self._read(self.sessions_dir, pid_alive=self.pid_alive), session_id)
        if s is None:
            return None
        alive = s.get("alive")
        if not (isinstance(alive, Evidence) and alive.value is True):
            return None
        return float(self.clock())


def idle_evidence(session: Optional[Dict[str, Any]]) -> Evidence:
    """Idle signal for ``compaction.decide``: OBSERVED True/False from the session file status, else UNAVAILABLE."""
    if session is None:
        return unavailable("Main session file not found")
    st = session.get("status")
    if not isinstance(st, Evidence) or not st.available or st.value not in ("idle", "busy"):
        return unavailable("Main session status not observed as idle or busy")
    return observed(st.value == "idle", st.source, st.timestamp)


class OwnedMainRetirer:
    """L3 ``SessionRetirer`` for Main: ends the old session only when THIS process started it; otherwise the user must
    close the old window (no keystroke injection, no signalling of a process we did not start)."""

    def __init__(self, launcher: InteractiveMainLauncher, pid_of: Callable[[str], Optional[int]]) -> None:
        self.launcher, self.pid_of = launcher, pid_of

    def retire(self, session_id: str) -> Tuple[bool, str]:
        pid = self.pid_of(session_id)
        if pid is None or not self.launcher.owns(pid):
            return False, "old Main was not started by this supervisor process; close its window by hand"
        self.launcher.terminate(pid)
        return True, "old Main terminated (started by this supervisor process)"


# ------------------------------------------------------------------ the Main record

def read_main(store: StateStore) -> Dict[str, Any]:
    return store.read(MAIN_DOC, default=None) or {}


def write_main(store: StateStore, **fields: Any) -> Dict[str, Any]:
    doc = read_main(store)
    doc.update(fields)
    doc["updated_at"] = utc_iso(store.clock())
    store.write(MAIN_DOC, doc)
    return doc


def write_main_fresh(store: StateStore, **fields: Any) -> Dict[str, Any]:
    """Write main.json for a NEW Main launch: no field of the previous record is carried over."""
    doc = dict(fields)
    doc["updated_at"] = utc_iso(store.clock())
    store.write(MAIN_DOC, doc)
    return doc
