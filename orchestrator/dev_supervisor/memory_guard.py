"""Memory guard for worker dispatch (T-DEV-053).

A worker is a full Claude Code process. Starting one while the machine is nearly out of memory is how the first overnight
run lost a background job (Claude Code reaped it, "the system is running low on memory"). This module only READS free
physical memory and compares it with a floor; the dispatch gate (``dispatch_routing.decide_worker_dispatch``) refuses to
start a worker below the floor. It never kills a process, never changes a system setting and never raises.

* ``free_memory_mb()``: available physical memory in whole MB, or ``None`` when it cannot be read (the gate then refuses:
  an unknown reading is not permission). Windows: ``GlobalMemoryStatusEx``. Linux: ``MemAvailable`` in ``/proc/meminfo``.
* ``min_free_mb(environ)``: the floor. ``CREATORSTUDIO_MIN_FREE_MB`` when it is a non-negative whole number, else
  ``DEFAULT_MIN_FREE_MB``. A bad value falls back to the default (it never disables the guard); ``0`` is an explicit
  owner choice to accept any readable value.
* ``check(free_mb, floor_mb)``: ``(ok, reason)``.

Standard library only.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Mapping, Optional, Tuple

__all__ = ["DEFAULT_MIN_FREE_MB", "ENV_MIN_FREE_MB", "free_memory_mb", "min_free_mb", "check"]

DEFAULT_MIN_FREE_MB = 1024
ENV_MIN_FREE_MB = "CREATORSTUDIO_MIN_FREE_MB"
_MAX_FLOOR_MB = 1024 * 1024          # 1 TB: anything larger is a typo, not a floor


def _windows_available_mb() -> Optional[int]:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return int(st.ullAvailPhys // (1024 * 1024))


def _linux_available_mb() -> Optional[int]:
    with open("/proc/meminfo", encoding="ascii", errors="replace") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024          # kB -> MB
    return None


def free_memory_mb() -> Optional[int]:
    """Available physical memory in whole MB, or None when it cannot be read. Never raises."""
    try:
        if sys.platform == "win32":
            return _windows_available_mb()
        if sys.platform.startswith("linux"):
            return _linux_available_mb()
    except Exception:                                       # unreadable is a normal, reported outcome
        return None
    return None


def min_free_mb(environ: Optional[Mapping[str, str]] = None) -> int:
    """The floor in MB: the environment value when valid, else the default."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_MIN_FREE_MB)
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        if 0 <= value <= _MAX_FLOOR_MB:
            return value
    return DEFAULT_MIN_FREE_MB


def check(free_mb: Any, floor_mb: int) -> Tuple[bool, str]:
    """(True, "") when ``free_mb`` is at least the floor; (False, reason) otherwise or when the reading is unusable."""
    if isinstance(free_mb, bool) or not isinstance(free_mb, int) or free_mb < 0:
        return False, "memory guard: free memory could not be read, refusing to start a worker"
    if free_mb < floor_mb:
        return False, (f"memory guard: {free_mb} MB free, at least {floor_mb} MB required "
                       f"({ENV_MIN_FREE_MB} changes the floor); close other applications or wait")
    return True, ""
