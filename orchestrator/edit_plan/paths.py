"""Windows path comparison without touching the filesystem (offline, deterministic).

Paths are compared as Windows paths on every platform: case-insensitive, `/` and `\\` equal, `.` and `..`
resolved lexically, trailing dots/spaces dropped from components (Windows ignores them). Symlinks, junctions and
8.3 short names cannot be resolved without the filesystem; that is a documented limit (README), so the allowlist
must be given as real long-form paths.
"""
from __future__ import annotations

import ntpath
from typing import Optional, Sequence

Parts = tuple  # (drive, (component, ...))


def parts_of(path: object) -> Optional[Parts]:
    """Normalised (drive, components) of an absolute Windows path, or None if unusable."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return None
    if path.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        return None
    if not ntpath.isabs(path):
        return None
    drive, rest = ntpath.splitdrive(ntpath.normpath(path))
    if not drive:
        return None
    comps = [c for c in rest.replace("/", "\\").split("\\") if c]
    out = []
    for c in comps:
        if ":" in c:
            return None                      # alternate data stream syntax
        out.append(c.rstrip(". ").casefold())
    if any(c == "" for c in out):
        return None
    return drive.casefold(), tuple(out)


def is_inside(child: Parts, root: Parts) -> bool:
    """True when `child` equals `root` or lies below it (component-wise, never by string prefix)."""
    return child[0] == root[0] and child[1][:len(root[1])] == root[1]


def contains_sequence(p: Parts, seq: Sequence[str]) -> bool:
    """True when the components contain `seq` as consecutive components (e.g. creatorstudio, projects)."""
    seq = tuple(s.casefold() for s in seq)
    n = len(seq)
    return any(p[1][i:i + n] == seq for i in range(len(p[1]) - n + 1))


def dirname(path: str) -> str:
    return ntpath.dirname(ntpath.normpath(path))
