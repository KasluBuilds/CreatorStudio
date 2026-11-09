"""Media inventory for the analysis layer.

The project inventory already exists (`creator_state` ingest -> `source_manifest.json`).
This module does NOT replace it: it
  * reads/validates that manifest and resolves `MediaRef`s from it, and
  * can build records of the SAME shape and with the SAME media ids for standalone use
    (a folder outside a project, synthetic test fixtures), re-using creator_state's
    read-only probe/parse/checksum helpers instead of duplicating them.

Originals are only opened 'rb' (and handed to ffprobe). Nothing is written next to them.
Missing metadata stays None; frame-rate honesty comes from creator_state.media.fps_info.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping

from creator_state import media as csm

from .models import (MediaAnalysisError, MediaRef, atomic_write_json, read_json, utc_now)

MANIFEST_SCHEMA = "media_analysis.manifest/1"
ANALYSABLE = ("video", "audio", "image")


def discover(root: str | os.PathLike) -> list[Path]:
    """Supported media files under `root` (read-only walk, sorted, junk/sidecars skipped)."""
    root = Path(root)
    if root.is_file():
        return [root] if csm.classify_extension(root.suffix) in ANALYSABLE else []
    if not root.is_dir():
        raise MediaAnalysisError(f"not a file or folder: {root}")
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        for name in sorted(filenames):
            if csm.ignore_reason(name):
                continue
            if csm.classify_extension(os.path.splitext(name)[1]) in ANALYSABLE:
                found.append(Path(dirpath) / name)
    return found


def _w(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def build_item(path: str | os.PathLike, prober: csm.Prober, hash_policy: str = "auto") -> dict:
    """Catalogue one file. Never raises for a bad file: the problem becomes status + warnings."""
    path = os.path.abspath(path)
    cls = csm.classify_extension(os.path.splitext(path)[1])
    item = {"id": csm.item_id(path), "name": os.path.basename(path), "path": path,
            "extension": os.path.splitext(path)[1].lower(), "media_type": cls or "unsupported",
            "media_type_source": "extension", "status": "ok", "file": None,
            "probe": {"status": "not_applicable"}, "container": None, "video": None, "audio": None,
            "image": None, "warnings": [], "last_checked": utc_now()}
    if cls is None:
        item["status"] = "unsupported"
        item["warnings"].append(_w("UNSUPPORTED_TYPE", f"extension {item['extension']!r} is not a supported media type"))
        return item
    try:
        st = os.stat(path)
        csm.open_probe_read(path)
    except FileNotFoundError:
        item["status"] = "missing"
        item["warnings"].append(_w("SOURCE_MISSING", "file does not exist"))
        return item
    except OSError as exc:
        item["status"] = "inaccessible"
        item["warnings"].append(_w("FILE_INACCESSIBLE", f"cannot read file: {exc.strerror or exc}"))
        return item
    item["file"] = csm.file_facts(path, st, hash_policy)
    if item["file"]["hash_status"] == "skipped_large":
        item["warnings"].append(_w("HASH_SKIPPED_LARGE", "file exceeds the automatic checksum limit"))
    elif item["file"]["hash_status"] == "failed":
        item["warnings"].append(_w("HASH_FAILED", "could not read file to compute checksum"))
    if cls in ANALYSABLE:
        res = prober.probe(path)
        if res["status"] == "ok":
            parsed = csm.parse_ffprobe(res["data"], cls)
            item["probe"] = {"status": "ok", "tool": prober.version}
            for k in ("container", "video", "audio", "image"):
                item[k] = parsed[k]
            item["media_type"], item["media_type_source"] = parsed["derived_type"], "ffprobe"
            item["warnings"] += [_w(c, m) for c, m in parsed["warnings"]]
        elif res["status"] == "unavailable":
            item["probe"] = {"status": "unavailable", "error": res["error"]}
            item["warnings"].append(_w("FFPROBE_UNAVAILABLE", "ffprobe not available: technical metadata not recorded"))
        else:
            item["status"] = "invalid"
            item["probe"] = {"status": "failed", "error": res["error"], "tool": prober.version}
            item["warnings"].append(_w("PROBE_FAILED", f"ffprobe could not read this file: {res['error']}"))
    return item


def build_manifest(source: str | os.PathLike | Iterable[str | os.PathLike],
                   prober: csm.Prober | None = None, hash_policy: str = "auto") -> dict:
    """Inventory of a folder/file/list of files. `prober` defaults to detecting ffprobe."""
    prober = prober or csm.Prober.detect()
    if isinstance(source, (str, os.PathLike)):
        paths, label = discover(source), str(source)
    else:
        paths, label = [Path(p) for p in source], "explicit file list"
    items = [build_item(p, prober, hash_policy) for p in paths]
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        raise MediaAnalysisError("duplicate media ids in one manifest")
    mf = {"schema": MANIFEST_SCHEMA, "generated_at": utc_now(), "source": label,
          "ffprobe": prober.describe(), "items": items}
    mf["duplicates"] = find_duplicate_content(items)
    return mf


# ---------------------------------------------------------------- validation
def validate_item(item: Mapping) -> list[str]:
    """Problems that make an item unusable as an analysis input (empty list = fine)."""
    problems = []
    for key in ("id", "path", "media_type", "status"):
        if not item.get(key):
            problems.append(f"missing field {key!r}")
    if item.get("status") != "ok":
        problems.append(f"status is {item.get('status')!r}")
    if item.get("media_type") in ("video", "audio"):
        dur = (item.get("container") or {}).get("duration_seconds")
        if dur is None:
            problems.append("duration unknown")
        elif dur <= 0:
            problems.append("duration is zero")
    return problems


def video_frame_rate_kinds(item: Mapping) -> list[str]:
    """'constant' | 'variable_suspected' | 'unknown' per video stream. Anything but
    'constant' means frame numbers must not be trusted; use seconds."""
    return [(v.get("frame_rate") or {}).get("kind", "unknown") for v in (item.get("video") or [])]


def is_variable_frame_rate(item: Mapping) -> bool:
    return any(k != "constant" for k in video_frame_rate_kinds(item))


def find_duplicate_content(items: Iterable[Mapping]) -> list[list[str]]:
    """Groups of media ids with identical sha256 (same bytes at different paths)."""
    by_hash: dict[str, list[str]] = {}
    for it in items:
        h = (it.get("file") or {}).get("sha256")
        if h:
            by_hash.setdefault(h, []).append(it["id"])
    return sorted(sorted(g) for g in by_hash.values() if len(g) > 1)


def verify_item(item: Mapping, deep: bool = False) -> dict:
    """Is the original still there and unchanged? Read-only.
    status: ok | missing | inaccessible | changed | unverifiable"""
    path, f = item.get("path"), item.get("file") or {}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {"status": "missing", "reasons": ["file not found"]}
    except OSError as exc:
        return {"status": "inaccessible", "reasons": [str(exc)]}
    reasons = []
    if f.get("size") is not None and f["size"] != st.st_size:
        reasons.append(f"size {f['size']} -> {st.st_size}")
    if f.get("mtime_ns") is not None and f["mtime_ns"] != st.st_mtime_ns:
        reasons.append("modification time differs")
    if deep and f.get("sha256"):
        try:
            from creator_state import util
            if util.sha256_file(Path(path)) != f["sha256"]:
                reasons.append("sha256 differs")
        except OSError as exc:
            return {"status": "inaccessible", "reasons": [str(exc)]}
    if reasons:
        return {"status": "changed", "reasons": reasons}
    if not f:
        return {"status": "unverifiable", "reasons": ["no recorded file facts"]}
    return {"status": "ok", "reasons": []}


# ---------------------------------------------------------------- persistence
def save_manifest(manifest: Mapping, path: str | os.PathLike) -> None:
    atomic_write_json(path, manifest)


def load_manifest(path: str | os.PathLike) -> dict:
    """Load either this package's manifest or creator_state's source_manifest.json."""
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise MediaAnalysisError(f"{path}: not a media manifest (no 'items' list)")
    ids = [i.get("id") for i in data["items"]]
    if None in ids or len(set(ids)) != len(ids):
        raise MediaAnalysisError(f"{path}: items without id or with duplicate ids")
    return data


def get_ref(manifest: Mapping, media_id: str) -> MediaRef:
    for it in manifest["items"]:
        if it["id"] == media_id:
            return MediaRef.from_item(it)
    raise MediaAnalysisError(f"unknown media id {media_id}")
