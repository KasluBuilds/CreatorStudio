"""Crash-safe, machine-local state store for the development supervisor (T-DEV-002, issue #44 part B).

What it does (IMPLEMENTED, tested offline with synthetic data only):
- Keeps small JSON documents (``<name>.json``) in a directory that defaults OUTSIDE the repository
  (``%LOCALAPPDATA%\\CreatorStudio\\dev_supervisor`` on Windows, ``$XDG_STATE_HOME/creatorstudio/dev_supervisor``
  or ``~/.local/state/creatorstudio/dev_supervisor`` elsewhere; ``CREATORSTUDIO_DEV_SUPERVISOR_DIR`` overrides).
- Refuses a directory inside a git working tree (any ancestor holding ``.git``) or inside an explicitly given repo root.
- Every write is atomic: temp file in the same directory, flush + fsync, previous valid file copied to ``<name>.json.bak``
  (itself written atomically), ``os.replace`` onto the target (bounded retries for Windows sharing violations),
  best-effort directory fsync.
- Every document is wrapped in an envelope with ``schema_version`` and a SHA-256 checksum of its canonical payload, so a
  torn, truncated or hand-damaged file is detected. A damaged file is quarantined (kept, never deleted) and the document
  is recovered from the backup; if both are damaged the store refuses (``StoreCorruptError``) instead of starting empty.
- An append-only JSON-lines event log (``events.jsonl``); a torn final line (crash mid-append) is skipped and counted.
- Everything written is scrubbed first: values under credential-like keys and credential-shaped strings are replaced.
- A single-supervisor lock file (exclusive create, stale when its pid is dead).

Not done here: cross-machine sync, encryption, multi-writer concurrency beyond the single-supervisor lock.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1
ENV_DIR = "CREATORSTUDIO_DEV_SUPERVISOR_DIR"
EVENT_LOG = "events.jsonl"
LOCK_FILE = "supervisor.lock"
REDACTED = "[REDACTED]"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_TMP_SUFFIX = ".tmp"

# Keys whose values are never written. Matches whole key segments: "api_key", "auth_token", "password" are scrubbed;
# "input_tokens", "max_tokens", "session_id" are not.
_SECRET_KEY = re.compile(r"(?i)(^|[_\-.])(api[_-]?key|access[_-]?key|secret|client[_-]?secret|password|passwd|token|"
                         r"auth[_-]?token|access[_-]?token|refresh[_-]?token|bearer|authorization|cookie|"
                         r"credentials?|private[_-]?key)$")
# Credential-shaped substrings inside any string value.
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}"                 # Anthropic/OpenAI style keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}"             # GitHub tokens
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"                       # AWS access key id
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}"          # Slack
    r"|(?i:bearer)\s+[A-Za-z0-9._\-+/=]{16,}"
    r"|-----BEGIN [A-Z ]*PRIVATE[ ]KEY-----)")


class StoreError(Exception):
    """Base class for state store errors."""


class UnsafeStoreLocation(StoreError):
    """The requested directory is inside a repository working tree."""


class StoreCorruptError(StoreError):
    """A document and its backup are both unreadable; nothing is guessed."""


class SchemaError(StoreError):
    """A document has a schema_version this code does not understand (for example a newer one)."""


class LockHeldError(StoreError):
    """Another live supervisor holds the lock."""


# ------------------------------------------------------------------ helpers

def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


def scrub(value: Any, _key: str = "") -> Any:
    """Return a copy of ``value`` with credentials replaced by ``[REDACTED]`` (recursive, JSON types only)."""
    if _key and _SECRET_KEY.search(_key) and value not in (None, "", [], {}):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(REDACTED, value)
    return value


def contains_secret(value: Any) -> bool:
    return scrub(value) != _plain(value)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def fs_path(p: os.PathLike | str, os_name: Optional[str] = None) -> str:
    """Absolute path string; on Windows paths of 248+ characters get the ``\\\\?\\`` extended-length prefix."""
    os_name = os_name or os.name
    s = str(p) if os_name != os.name else os.path.abspath(str(p))
    if os_name == "nt" and len(s) >= 248 and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s


def default_directory(env: Optional[Dict[str, str]] = None, os_name: Optional[str] = None,
                      home: Optional[str] = None) -> Path:
    env = os.environ if env is None else env
    os_name = os_name or os.name
    if env.get(ENV_DIR):
        return Path(env[ENV_DIR])
    if os_name == "nt":
        base = env.get("LOCALAPPDATA") or os.path.join(home or os.path.expanduser("~"), "AppData", "Local")
        return Path(base) / "CreatorStudio" / "dev_supervisor"
    base = env.get("XDG_STATE_HOME") or os.path.join(home or os.path.expanduser("~"), ".local", "state")
    return Path(base) / "creatorstudio" / "dev_supervisor"


def this_repository_root() -> Optional[Path]:
    """The working tree that contains this module, if any (``.git`` directory or worktree ``.git`` file)."""
    return find_git_root(Path(__file__).resolve().parent)


def find_git_root(start: Path) -> Optional[Path]:
    p = Path(os.path.abspath(str(start)))
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")


def is_within(child: Path, parent: Path) -> bool:
    c, par = _norm(child), _norm(parent)
    return c == par or c.startswith(par + os.sep)


def check_location(directory: Path, repo_roots: Iterable[Path] = ()) -> None:
    """Raise ``UnsafeStoreLocation`` if ``directory`` lies inside a repository working tree."""
    d = Path(os.path.abspath(str(directory)))
    for root in [r for r in repo_roots if r is not None]:
        if is_within(d, root):
            raise UnsafeStoreLocation(f"state directory is inside the repository working tree: {root}")
    # The directory may not exist yet: walk up through the nearest existing ancestor.
    probe = d
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    git_root = find_git_root(probe)
    if git_root is not None:
        raise UnsafeStoreLocation(f"state directory is inside a git working tree: {git_root}")


def _envelope(kind: str, data: Any, written_at: str) -> Dict[str, Any]:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {"schema_version": SCHEMA_VERSION, "kind": kind, "written_at": written_at,
            "checksum": "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(), "data": data}


def decode_envelope(raw: bytes, kind: str) -> Any:
    """Return the payload of a stored document; raise ``ValueError`` (damaged) or ``SchemaError`` (unknown schema)."""
    try:
        env = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"not valid JSON: {e.__class__.__name__}") from None
    if not isinstance(env, dict) or "data" not in env or "checksum" not in env:
        raise ValueError("envelope fields missing")
    sv = env.get("schema_version")
    if not isinstance(sv, int):
        raise ValueError("schema_version missing")
    if sv > SCHEMA_VERSION:
        raise SchemaError(f"schema_version {sv} is newer than supported {SCHEMA_VERSION}")
    if env.get("kind") != kind:
        raise ValueError(f"kind is {env.get('kind')!r}, expected {kind!r}")
    payload = json.dumps(env["data"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if env["checksum"] != "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest():
        raise ValueError("checksum mismatch")
    return env["data"]


# ------------------------------------------------------------------ the store

class StateStore:
    """Crash-safe JSON documents plus an append-only event log in one machine-local directory."""

    def __init__(self, directory: Optional[os.PathLike | str] = None, *, repo_roots: Iterable[Path] = (),
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 pid_alive: Optional[Callable[[int], bool]] = None, replace_retries: int = 5,
                 create: bool = True) -> None:
        self.directory = Path(os.path.abspath(str(directory if directory is not None else default_directory())))
        roots = list(repo_roots)
        own = this_repository_root()
        if own is not None:
            roots.append(own)
        check_location(self.directory, roots)
        self.clock, self.sleep = clock, sleep
        self.pid_alive = pid_alive
        self.replace_retries = max(1, int(replace_retries))
        self.recoveries: List[Dict[str, Any]] = []
        if create:
            os.makedirs(fs_path(self.directory), exist_ok=True)
        self.stale_temps_removed = self._remove_stale_temps() if self.directory.exists() else 0

    # -------------------------------------------------- paths

    def path(self, name: str) -> Path:
        if not _NAME.match(name):
            raise StoreError(f"invalid document name {name!r}")
        return self.directory / f"{name}.json"

    def backup_path(self, name: str) -> Path:
        return self.directory / f"{name}.json.bak"

    def _remove_stale_temps(self) -> int:
        """Temp files are only ever left by a crash mid-write; the target file was never replaced, so drop them."""
        n = 0
        for entry in os.scandir(fs_path(self.directory)):
            if entry.is_file() and entry.name.startswith(".") and entry.name.endswith(_TMP_SUFFIX):
                try:
                    os.remove(entry.path)
                    n += 1
                except OSError:
                    pass
        return n

    # -------------------------------------------------- low-level writes

    def _write_temp(self, target: Path, data: bytes) -> str:
        tmp = fs_path(target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}{_TMP_SUFFIX}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            _silent_remove(tmp)
            raise
        return tmp

    def _replace(self, src: str, dst: Path) -> None:
        """``os.replace`` with bounded retries: on Windows a reader or a virus scanner can briefly hold the target."""
        last: Optional[OSError] = None
        for attempt in range(self.replace_retries):
            try:
                os.replace(src, fs_path(dst))
                return
            except PermissionError as e:
                last = e
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EBUSY) and getattr(e, "winerror", None) not in (5, 32, 33):
                    raise
                last = e
            if attempt + 1 < self.replace_retries:
                self.sleep(min(0.05 * (2 ** attempt), 1.0))
        assert last is not None
        raise last

    def _fsync_dir(self) -> None:
        if os.name == "nt":
            return  # directories cannot be opened for fsync on Windows; NTFS journals the rename
        try:
            fd = os.open(fs_path(self.directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def atomic_write_bytes(self, target: Path, data: bytes) -> None:
        tmp = self._write_temp(target, data)
        try:
            self._replace(tmp, target)
        except BaseException:
            _silent_remove(tmp)
            raise
        self._fsync_dir()

    # -------------------------------------------------- documents

    def write(self, name: str, data: Any) -> Dict[str, Any]:
        """Scrub, wrap and atomically write ``data``; the previous valid version becomes ``<name>.json.bak``."""
        target = self.path(name)
        clean = scrub(data)
        env = _envelope(name, clean, utc_iso(self.clock()))
        raw = (json.dumps(env, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        previous = _read_bytes(target)
        if previous is not None:
            try:
                decode_envelope(previous, name)
            except (ValueError, SchemaError):
                pass  # never overwrite the last good backup with a damaged or foreign file
            else:
                self.atomic_write_bytes(self.backup_path(name), previous)
        self.atomic_write_bytes(target, raw)
        return clean

    def read(self, name: str, default: Any = None) -> Any:
        """Read a document. A damaged file is quarantined and recovered from the backup (recorded in the event log)."""
        target, bak = self.path(name), self.backup_path(name)
        raw = _read_bytes(target)
        if raw is not None:
            try:
                return decode_envelope(raw, name)
            except ValueError as e:
                problem = str(e)
        else:
            problem = "missing"
            if _read_bytes(bak) is None:
                return default
        braw = _read_bytes(bak)
        if braw is None:
            raise StoreCorruptError(f"{target.name} is damaged ({problem}) and no backup exists")
        try:
            data = decode_envelope(braw, name)
        except ValueError as e:
            raise StoreCorruptError(f"{target.name} is damaged ({problem}) and its backup too ({e})") from None
        quarantined = None
        if raw is not None:
            quarantined = self.directory / f"{name}.json.corrupt-{int(self.clock())}-{uuid.uuid4().hex[:6]}"
            os.replace(fs_path(target), fs_path(quarantined))
        self.atomic_write_bytes(target, braw)
        rec = {"document": name, "problem": problem,
               "quarantined_as": quarantined.name if quarantined else None}
        self.recoveries.append(rec)
        self.append_event("store_recovered_from_backup", **rec)
        return data

    # -------------------------------------------------- event log

    def append_event(self, kind: str, **data: Any) -> Dict[str, Any]:
        ev = scrub({"ts": utc_iso(self.clock()), "kind": kind, "data": data})
        line = json.dumps(ev, sort_keys=True, ensure_ascii=False) + "\n"
        path = fs_path(self.directory / EVENT_LOG)
        with open(path, "ab") as fh:
            if fh.tell() > 0:
                # A crash mid-append can leave a torn last line without a newline; start on a fresh line.
                with open(path, "rb") as rd:
                    rd.seek(-1, os.SEEK_END)
                    if rd.read(1) != b"\n":
                        fh.write(b"\n")
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        return ev

    def read_events(self) -> Tuple[List[Dict[str, Any]], int]:
        """All parseable events in order, and the count of damaged lines that were skipped."""
        raw = _read_bytes(self.directory / EVENT_LOG)
        if raw is None:
            return [], 0
        events, bad = [], 0
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line.decode("utf-8"))
                if not isinstance(ev, dict) or "kind" not in ev:
                    raise ValueError
                events.append(ev)
            except (UnicodeDecodeError, ValueError):
                bad += 1
        return events, bad

    # -------------------------------------------------- single-supervisor lock

    def acquire_lock(self, owner: str = "supervisor") -> Dict[str, Any]:
        path = fs_path(self.directory / LOCK_FILE)
        info = {"pid": os.getpid(), "owner": owner, "acquired_at": utc_iso(self.clock())}
        for _ in range(2):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                held = _load_json(path)
                pid = held.get("pid") if isinstance(held, dict) else None
                alive = self.pid_alive(pid) if (self.pid_alive and isinstance(pid, int)) else isinstance(pid, int)
                if isinstance(pid, int) and pid != os.getpid() and alive:
                    raise LockHeldError(f"supervisor lock held by live pid {pid}")
                _silent_remove(path)  # stale (dead pid, our own pid, or unreadable)
                self.append_event("lock_stale_removed", previous_pid=pid)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(info, fh)
                fh.flush()
                os.fsync(fh.fileno())
            return info
        raise LockHeldError("could not acquire supervisor lock")

    def release_lock(self) -> None:
        path = fs_path(self.directory / LOCK_FILE)
        held = _load_json(path)
        if isinstance(held, dict) and held.get("pid") == os.getpid():
            _silent_remove(path)


def _read_bytes(p: Path) -> Optional[bytes]:
    try:
        with open(fs_path(p), "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
