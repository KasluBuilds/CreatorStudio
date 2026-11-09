"""Managed copy of ONE original into the project: explicit, separately verified, never overwriting.

Not part of ingestion. Use when a working copy genuinely helps (e.g. the original sits on a
slow or removable drive). The original is only read; the copy goes to <project>/assets/managed/.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from . import ingest, util
from .project import Project, StateError

MANAGED_DIR = "assets/managed"
MANAGED_MANIFEST = "analysis/managed_copies.json"


class ManagedCopyError(StateError):
    pass


def managed_copy(project: Project, media_id: str) -> dict:
    m = ingest.load_manifest(project)
    if m is None:
        raise ManagedCopyError("no source_manifest.json; run ingest first")
    item = next((i for i in m["items"] if i["id"] == media_id), None)
    if item is None:
        raise ManagedCopyError(f"unknown media id '{media_id}' (see `inventory`)")
    src = Path(item["path"])
    if item["status"] != "ok" or not src.is_file():
        raise ManagedCopyError(f"original is not available: {src}")
    mdir = project.dir / MANAGED_DIR
    mdir.mkdir(parents=True, exist_ok=True)
    dest = mdir / f"{item['id']}_{src.name}"
    if dest.exists():
        raise ManagedCopyError(f"{dest.name} already exists; refusing to overwrite")
    size = src.stat().st_size
    free = shutil.disk_usage(mdir).free
    if free < size * 1.1:
        raise ManagedCopyError(f"not enough free space ({free} bytes free, {size} needed)")
    tmp = mdir / f".{dest.name}.part"
    if tmp.exists():
        tmp.unlink()
    project.log.event("managed_copy.started", f"Copying {src} -> {dest.relative_to(project.dir)}")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            while True:
                b = fi.read(1 << 20)
                if not b:
                    break
                h.update(b)
                fo.write(b)
            fo.flush()
            os.fsync(fo.fileno())
        src_sha = h.hexdigest()
        problems = []
        recorded = (item.get("file") or {}).get("sha256")
        if recorded and recorded != src_sha:
            problems.append("original no longer matches the checksum recorded at ingest")
        if tmp.stat().st_size != size or src.stat().st_size != size:
            problems.append("size mismatch after copy")
        if util.sha256_file(tmp) != src_sha:
            problems.append("copy checksum differs from original")
        if problems:
            raise ManagedCopyError("; ".join(problems))
        if dest.exists():
            raise ManagedCopyError(f"{dest.name} appeared during copy; refusing to overwrite")
        os.rename(tmp, dest)  # fails rather than overwrites if dest exists
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        project.log.event("managed_copy.failed", f"{item['name']}: {e}", "ERROR")
        raise
    rec = {"source_id": item["id"], "source_path": str(src), "copy_path": str(dest.relative_to(project.dir)).replace("\\", "/"),
           "size": size, "sha256": src_sha, "verified": True, "verified_at": util.utcnow()}
    f = project.dir / MANAGED_MANIFEST
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"manifest_version": 1, "copies": []}
    data["copies"].append(rec)
    (project.dir / "analysis").mkdir(exist_ok=True)
    util.atomic_write_text(f, util.pretty_json(data))
    project.log.event("managed_copy.created", f"Verified copy of {item['name']} at {rec['copy_path']} "
                      "(original untouched; it remains the source of truth).")
    return rec
