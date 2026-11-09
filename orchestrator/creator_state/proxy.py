"""Proxy generation: configurable, deterministic, verified, and ONLY on explicit request.

Ingestion never calls this module. Proxies are written under <project>/proxies/ with
names derived from (media id, settings hash), so the same inputs always map to the same
file. A proxy is renamed into place only after it passes verification, an existing file
is never overwritten, and sources are only ever read.

Status: the encode settings below have been exercised only on small synthetic clips
(see tests). They have NOT been validated on real footage.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import ingest, media, util
from .project import Project, StateError

PROXY_DIR = "proxies"
PROXY_MANIFEST = "analysis/proxy_manifest.json"
SETTINGS_FILE = "proxy_settings.json"

DEFAULT_SETTINGS = {
    "container": "mp4", "video_codec": "libx264", "crf": 23, "preset": "veryfast",
    "max_height": 540, "pix_fmt": "yuv420p", "audio_codec": "aac", "audio_bitrate": "128k",
}
_TYPES = {"container": str, "video_codec": str, "crf": int, "preset": str, "max_height": int,
          "pix_fmt": str, "audio_codec": str, "audio_bitrate": str}
_SAFE_CONTAINERS = {"mp4", "mov", "mkv"}


class ProxyError(StateError):
    pass


def load_settings(project: Project, overrides: dict | None = None) -> dict:
    """Defaults < <project>/proxy_settings.json < explicit overrides. Validated."""
    s = dict(DEFAULT_SETTINGS)
    f = project.dir / SETTINGS_FILE
    if f.exists():
        try:
            s.update(json.loads(f.read_text(encoding="utf-8")))
        except ValueError:
            raise ProxyError(f"{SETTINGS_FILE} is not valid JSON")
    s.update({k: v for k, v in (overrides or {}).items() if v is not None})
    for k, v in s.items():
        if k not in _TYPES:
            raise ProxyError(f"unknown proxy setting '{k}'")
        if not isinstance(v, _TYPES[k]) or isinstance(v, bool):
            raise ProxyError(f"proxy setting '{k}' must be {_TYPES[k].__name__}")
    if s["container"] not in _SAFE_CONTAINERS:
        raise ProxyError(f"container must be one of {sorted(_SAFE_CONTAINERS)}")
    if not 0 <= s["crf"] <= 51 or s["max_height"] < 16:
        raise ProxyError("crf must be 0-51 and max_height at least 16")
    return s


def settings_hash(s: dict) -> str:
    return util.sha256_text(util.canonical_json(s))[:8]


def proxy_path(project: Project, media_id: str, s: dict) -> Path:
    return project.dir / PROXY_DIR / f"{media_id}_{settings_hash(s)}.{s['container']}"


def _load_pm(project: Project) -> dict:
    f = project.dir / PROXY_MANIFEST
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            raise ProxyError(f"{PROXY_MANIFEST} is corrupt")
    return {"manifest_version": 1, "proxies": []}


def plan(project: Project, settings: dict) -> list:
    """What `make` would do, per video in the inventory. Touches nothing."""
    m = ingest.load_manifest(project)
    if m is None:
        raise ProxyError("no source_manifest.json; run ingest first")
    pm = {p["source_id"] + "|" + p["settings_hash"]: p for p in _load_pm(project)["proxies"]}
    rows = []
    for it in m["items"]:
        if it["media_type"] != "video":
            continue
        dest = proxy_path(project, it["id"], settings)
        row = {"id": it["id"], "name": it["name"], "source": it["path"],
               "proxy": str(dest.relative_to(project.dir)).replace("\\", "/"), "action": "create", "reason": ""}
        if it["status"] != "ok":
            row.update(action="blocked", reason=f"source is {it['status']}")
        elif not it.get("video"):
            row.update(action="blocked", reason="no probed video stream (ffprobe unavailable or failed)")
        elif dest.exists():
            known = pm.get(it["id"] + "|" + settings_hash(settings))
            row.update(action="exists", reason="already generated and verified" if known
                       else "file exists but has no verification record; will not overwrite")
        rows.append(row)
    return rows


def build_argv(ffmpeg: str, src: str, tmp: Path, s: dict) -> list:
    return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n", "-i", src,
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", f"scale=-2:'min({s['max_height']},ih)'",
            "-c:v", s["video_codec"], "-preset", s["preset"], "-crf", str(s["crf"]),
            "-pix_fmt", s["pix_fmt"], "-c:a", s["audio_codec"], "-b:a", s["audio_bitrate"], str(tmp)]


def find_ffmpeg(explicit=None, which=shutil.which):
    cands = [c for c in (explicit, os.environ.get("CREATOR_FFMPEG"), which("ffmpeg")) if c]
    return cands[0] if cands else None


def verify_proxy(tmp: Path, item: dict, s: dict, prober: media.Prober) -> list:
    """Return a list of problems (empty = verified)."""
    probs = []
    if not tmp.exists() or tmp.stat().st_size == 0:
        return ["proxy file missing or empty"]
    res = prober.probe(tmp)
    if res["status"] != "ok":
        return [f"proxy not readable by ffprobe: {res.get('error')}"]
    parsed = media.parse_ffprobe(res["data"], "video")
    if not parsed["video"]:
        probs.append("proxy has no video stream")
    else:
        v = parsed["video"][0]
        src_h = item["video"][0].get("display_height") or v["height"]
        if v["height"] and v["height"] > max(s["max_height"], 0) and v["height"] > (src_h or 0):
            probs.append(f"proxy height {v['height']} exceeds limit")
    if item.get("audio") and not parsed["audio"]:
        probs.append("source has audio but proxy has none")
    sd = (item.get("container") or {}).get("duration_seconds")
    pd = parsed["container"]["duration_seconds"]
    if sd and pd is not None and abs(pd - sd) > max(0.5, 0.02 * sd):
        probs.append(f"proxy duration {pd:.2f}s differs from source {sd:.2f}s")
    elif sd and pd is None:
        probs.append("proxy duration unknown")
    return probs


def make(project: Project, settings: dict, prober: media.Prober, ffmpeg: str | None,
         only_ids=None, runner=None, progress=None) -> list:
    """Generate proxies for planned items. Explicit call only. Returns per-item results."""
    if not ffmpeg:
        raise ProxyError("ffmpeg not found (install FFmpeg or set CREATOR_FFMPEG / pass --ffmpeg)")
    if not prober.available:
        raise ProxyError("ffprobe is required to verify proxies and is not available")
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, timeout=3600))
    progress = progress or (lambda m: None)
    items = {i["id"]: i for i in ingest.load_manifest(project)["items"]}
    pdir = project.dir / PROXY_DIR
    pdir.mkdir(exist_ok=True)
    results = []
    for row in plan(project, settings):
        if only_ids and row["id"] not in only_ids:
            continue
        res = {"id": row["id"], "name": row["name"], "proxy": row["proxy"]}
        if row["action"] != "create":
            res.update(status="skipped", reason=row["reason"] or row["action"])
            results.append(res)
            continue
        item = items[row["id"]]
        src = item["path"]
        try:
            st = os.stat(src)
            f = item["file"]
            if st.st_size != f["size"] or st.st_mtime_ns != f["mtime_ns"]:
                res.update(status="blocked", reason="source changed since ingest; re-run ingest first")
                results.append(res)
                continue
        except OSError as e:
            res.update(status="blocked", reason=f"source not accessible: {e.strerror or e}")
            results.append(res)
            continue
        final = project.dir / row["proxy"]
        tmp = pdir / f".{final.stem}.part{final.suffix}"
        if tmp.exists():
            tmp.unlink()  # our own leftover from an interrupted run
        progress(f"proxy {row['name']} -> {row['proxy']}")
        project.log.event("proxy.started", f"Generating proxy for {row['name']} -> {row['proxy']}")
        try:
            cp = runner(build_argv(ffmpeg, src, tmp, settings))
        except (OSError, subprocess.TimeoutExpired) as e:
            cp = None
            err = str(e)
        else:
            err = (cp.stderr or b"").decode("utf-8", "replace").strip()[:400] if cp.returncode else ""
        problems = [f"ffmpeg failed: {err}"] if (cp is None or cp.returncode) else verify_proxy(tmp, item, settings, prober)
        if problems:
            if tmp.exists():
                tmp.unlink()
            res.update(status="failed", reason="; ".join(problems))
            project.log.event("proxy.failed", f"{row['name']}: {res['reason']}", "ERROR")
            results.append(res)
            continue
        if final.exists():  # someone created it meanwhile: never overwrite
            tmp.unlink()
            res.update(status="skipped", reason="destination appeared during generation; not overwritten")
            results.append(res)
            continue
        os.rename(tmp, final)
        rec = {"source_id": item["id"], "source_path": src, "source_sha256": item["file"]["sha256"],
               "proxy_path": row["proxy"], "settings": settings, "settings_hash": settings_hash(settings),
               "proxy_size": final.stat().st_size, "proxy_sha256": util.sha256_file(final),
               "created_at": util.utcnow(), "verified": True, "verification": "ffprobe: stream, height, audio, duration"}
        pm = _load_pm(project)
        pm["proxies"].append(rec)
        (project.dir / "analysis").mkdir(exist_ok=True)
        util.atomic_write_text(project.dir / PROXY_MANIFEST, util.pretty_json(pm))
        res.update(status="created", reason="")
        project.log.event("proxy.created", f"Proxy verified and recorded: {row['proxy']} <- {src}")
        results.append(res)
    return results
