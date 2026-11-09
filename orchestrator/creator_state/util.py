"""Small shared helpers: clock, atomic file IO, hashing, locking."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    """UTC timestamp, second precision, ISO 8601 with Z suffix. Tests may patch this."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact_ts(ts: str) -> str:
    return ts.replace("-", "").replace(":", "")


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pretty_json(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Write via temp file in the same directory, fsync, then os.replace.

    Readers see either the old complete file or the new complete file, never a
    partial one. A crash can leave a stray .tmp file but never a torn target.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_new_text(path: Path, text: str) -> None:
    """Create a file that must not exist yet (used for append-only checkpoints)."""
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


class LockTimeout(Exception):
    pass


class FileLock:
    """Minimal cross-process lock using exclusive file creation.

    A lock older than `stale_after` seconds is assumed to belong to a crashed
    process and is removed (and the takeover is reported via `took_over_stale`).
    """

    def __init__(self, path: Path, timeout: float = 10.0, stale_after: float = 120.0):
        self.path = Path(path)
        self.timeout = timeout
        self.stale_after = stale_after
        self.took_over_stale = False

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"pid={os.getpid()} at={utcnow()}\n".encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale_after:
                    try:
                        self.path.unlink()
                        self.took_over_stale = True
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise LockTimeout(f"could not acquire {self.path} within {self.timeout}s")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass
        return False
