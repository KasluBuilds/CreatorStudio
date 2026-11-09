"""Media classification, ffprobe detection/parsing and per-file facts.

Everything here is READ-ONLY with respect to media files: files are opened 'rb'
and handed to ffprobe, nothing is ever written next to or over an original.
Missing metadata stays missing (None); nothing is guessed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from . import util

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".mxf", ".webm", ".wmv",
             ".mpg", ".mpeg", ".3gp", ".mts", ".ts", ".flv"}
AUDIO_EXT = {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".aif", ".aiff", ".wma", ".bwf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif", ".avif"}
NOTES_EXT = {".txt", ".md", ".rtf"}
SUBTITLE_EXT = {".srt", ".vtt", ".ass", ".ssa"}

IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
INCOMPLETE_SUFFIX = {".tmp", ".part", ".partial", ".crdownload", ".download"}
SIDECAR_EXT = {".thm", ".lrv", ".lrf", ".xmp", ".ctg", ".ffindex", ".bim"}

CATEGORY_ALIASES = {
    "camera": "camera", "cam": "camera", "camera_footage": "camera",
    "phone": "phone", "mobile": "phone", "phone_footage": "phone",
    "screen": "screen", "screen_recordings": "screen", "screenrecordings": "screen", "screencap": "screen",
    "audio": "audio", "sound": "audio", "voiceover": "audio",
    "images": "images", "image": "images", "photos": "images", "pictures": "images",
    "notes": "notes", "docs": "notes", "text": "notes",
}
CATEGORIES = ("camera", "phone", "screen", "audio", "images", "notes", "unsorted")

HASH_AUTO_MAX_BYTES = 4 * 1024 ** 3  # 'auto' hashes files up to 4 GiB


def classify_extension(ext: str):
    """Return video/audio/image/notes/subtitle, or None if unsupported."""
    e = ext.lower()
    for name, group in (("video", VIDEO_EXT), ("audio", AUDIO_EXT), ("image", IMAGE_EXT),
                        ("notes", NOTES_EXT), ("subtitle", SUBTITLE_EXT)):
        if e in group:
            return name
    return None


def ignore_reason(name: str):
    """Why a file is deliberately not catalogued (system junk, partial downloads, sidecars)."""
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if low in IGNORED_NAMES or low.startswith("~$") or low.startswith("._"):
        return "system/temporary file"
    if name.startswith("."):
        return "hidden file"
    if ext in INCOMPLETE_SUFFIX:
        return "incomplete or temporary download"
    if ext in SIDECAR_EXT:
        return "camera/editor sidecar (not catalogued)"
    return None


def category_for(rel_parts) -> str:
    """Source category from the first subfolder name, else 'unsorted'."""
    if len(rel_parts) > 1:
        return CATEGORY_ALIASES.get(rel_parts[0].lower(), "unsorted")
    return "unsorted"


def iso_utc(ts_ns_or_s, ns: bool = False):
    try:
        sec = ts_ns_or_s / 1e9 if ns else ts_ns_or_s
        return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def item_id(path: str) -> str:
    """Stable id from the normalized absolute path (so re-ingest never duplicates an entry)."""
    import hashlib
    return "m_" + hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------------------- ffprobe
class ProbeTimeout(Exception):
    pass


def _run(argv, timeout):
    try:
        cp = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeTimeout(f"timed out after {timeout}s")
    dec = lambda b: b.decode("utf-8", "replace")
    return cp.returncode, dec(cp.stdout), dec(cp.stderr)


class Prober:
    """Wraps ffprobe. `available` is detected, never assumed."""

    def __init__(self, path=None, version=None, reason=None, runner=None, timeout=120):
        self.path, self.version, self.reason = path, version, reason
        self.runner = runner or _run
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self.path is not None

    @classmethod
    def detect(cls, explicit=None, which=shutil.which, runner=None, timeout=120):
        runner = runner or _run
        cands = [c for c in (explicit, os.environ.get("CREATOR_FFPROBE")) if c]
        found = which("ffprobe")
        if found:
            cands.append(found)
        if not cands:
            return cls(reason="ffprobe not found on PATH (install FFmpeg, or pass --ffprobe PATH)",
                       runner=runner, timeout=timeout)
        last = "ffprobe could not be executed"
        for c in cands:
            try:
                rc, out, err = runner([c, "-version"], 15)
            except (OSError, ProbeTimeout) as e:
                last = f"{c}: {e}"
                continue
            if rc == 0 and "ffprobe version" in out:
                return cls(path=c, version=out.splitlines()[0].strip(), runner=runner, timeout=timeout)
            last = f"{c}: did not behave like ffprobe"
        return cls(reason=last, runner=runner, timeout=timeout)

    def describe(self) -> dict:
        return {"available": self.available, "path": self.path, "version": self.version,
                "reason": self.reason}

    def probe(self, file_path) -> dict:
        """Run ffprobe. Returns {'status': 'ok'|'failed'|'unavailable', 'data'?, 'error'?}."""
        if not self.available:
            return {"status": "unavailable", "error": self.reason}
        argv = [self.path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                "-i", str(file_path)]
        try:
            rc, out, err = self.runner(argv, self.timeout)
        except ProbeTimeout as e:
            return {"status": "failed", "error": f"ffprobe {e}"}
        except OSError as e:
            return {"status": "failed", "error": f"could not run ffprobe: {e}"}
        if rc != 0:
            return {"status": "failed", "error": (err.strip() or f"ffprobe exit code {rc}")[:500]}
        try:
            data = json.loads(out)
        except ValueError:
            return {"status": "failed", "error": "ffprobe produced unparseable output"}
        if not isinstance(data, dict) or (not data.get("streams") and not data.get("format")):
            return {"status": "failed", "error": "ffprobe reported no streams or format"}
        return {"status": "ok", "data": data}


def _rate(s):
    """'30000/1001' -> Fraction, or None for 0/0, missing or malformed."""
    try:
        fr = Fraction(str(s))
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return fr if fr > 0 else None


def _float(v):
    try:
        f = float(v)
        return f if f >= 0 else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fps_info(r_raw, avg_raw) -> dict:
    """Describe frame rate honestly: exact rational vs. estimated, constant vs. suspected VFR."""
    r, avg = _rate(r_raw), _rate(avg_raw)
    out = {"r_frame_rate": r_raw if r else None, "avg_frame_rate": avg_raw if avg else None,
           "fps": None, "fps_rational": None, "exact": False, "kind": "unknown"}
    if r and avg:
        if abs(float(r) - float(avg)) / float(r) < 0.001:
            out.update(fps=round(float(r), 6), fps_rational=f"{r.numerator}/{r.denominator}",
                       exact=True, kind="constant")
        else:
            out.update(fps=round(float(avg), 6), exact=False, kind="variable_suspected")
    elif r or avg:
        v = r or avg
        out.update(fps=round(float(v), 6), exact=False, kind="unknown")
    return out


def _rotation(stream: dict) -> int:
    """Display rotation in degrees clockwise-normalised to 0/90/180/270 (0 if not declared)."""
    raw = None
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            raw = sd["rotation"]
    if raw is None:
        raw = (stream.get("tags") or {}).get("rotate")
    try:
        return int(round(float(raw))) % 360
    except (TypeError, ValueError):
        return 0


def parse_ffprobe(data: dict, media_class: str) -> dict:
    """Pure function: ffprobe JSON -> inventory fields + warnings. No guessing."""
    warnings = []
    fmt = data.get("format") or {}
    tags = fmt.get("tags") or {}
    container = {
        "format_name": fmt.get("format_name"),
        "duration_seconds": _float(fmt.get("duration")),
        "bit_rate": _int(fmt.get("bit_rate")),
        "creation_time": tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate"),
    }
    video, audio, image = [], [], None
    for s in data.get("streams") or []:
        ctype = s.get("codec_type")
        attached = bool((s.get("disposition") or {}).get("attached_pic"))
        if ctype == "video" and not attached:
            w, h = _int(s.get("width")), _int(s.get("height"))
            rot = _rotation(s)
            entry = {"index": s.get("index"), "codec": s.get("codec_name"), "profile": s.get("profile"),
                     "width": w, "height": h, "pix_fmt": s.get("pix_fmt"), "rotation": rot,
                     "display_width": (h if rot in (90, 270) else w),
                     "display_height": (w if rot in (90, 270) else h),
                     "field_order": s.get("field_order"), "nb_frames": _int(s.get("nb_frames")),
                     "duration_seconds": _float(s.get("duration")), "bit_rate": _int(s.get("bit_rate")),
                     "color_space": s.get("color_space"), "color_transfer": s.get("color_transfer")}
            if media_class == "image":
                image = {k: entry[k] for k in ("codec", "width", "height", "pix_fmt", "rotation",
                                               "display_width", "display_height")}
            else:
                entry["frame_rate"] = fps_info(s.get("r_frame_rate"), s.get("avg_frame_rate"))
                if entry["frame_rate"]["kind"] == "variable_suspected":
                    warnings.append(("VARIABLE_FRAME_RATE",
                                     f"video stream {s.get('index')} looks variable-frame-rate "
                                     f"(r={s.get('r_frame_rate')}, avg={s.get('avg_frame_rate')}); "
                                     "needs special handling before precise editing"))
                video.append(entry)
        elif ctype == "audio":
            audio.append({"index": s.get("index"), "codec": s.get("codec_name"),
                          "channels": _int(s.get("channels")), "channel_layout": s.get("channel_layout"),
                          "sample_rate": _int(s.get("sample_rate")), "bit_rate": _int(s.get("bit_rate")),
                          "duration_seconds": _float(s.get("duration"))})
    derived = media_class
    if media_class == "video":
        if not video and audio:
            derived = "audio"
            warnings.append(("EXTENSION_MISMATCH", "file has a video extension but contains only audio"))
        elif not video:
            warnings.append(("NO_MEDIA_STREAMS", "no video or audio stream found"))
        elif not audio:
            warnings.append(("NO_AUDIO_STREAM", "video has no audio stream"))
    elif media_class == "audio" and not audio:
        warnings.append(("NO_MEDIA_STREAMS", "no audio stream found"))
    elif media_class == "image" and image is None:
        warnings.append(("NO_MEDIA_STREAMS", "no image stream found"))
    if media_class in ("video", "audio") and container["duration_seconds"] in (None, 0.0):
        warnings.append(("ZERO_DURATION", "duration is missing or zero"))
    return {"container": container, "video": video, "audio": audio, "image": image,
            "derived_type": derived, "warnings": warnings}


# --------------------------------------------------------------------------- file facts
def open_probe_read(path) -> None:
    """Confirm the file can be opened for reading (reads 1 byte). Raises OSError if not."""
    with open(path, "rb") as f:
        f.read(1)


def expected_hash_status(size: int, hash_policy: str) -> str:
    """The hash_status a fresh run would produce under this policy (used to validate cached records)."""
    if hash_policy == "never":
        return "disabled"
    if hash_policy == "auto" and size > HASH_AUTO_MAX_BYTES:
        return "skipped_large"
    return "computed"


def file_facts(path: str, st: os.stat_result, hash_policy: str = "auto") -> dict:
    """Filesystem facts + checksum per policy ('auto' | 'always' | 'never')."""
    facts = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "mtime": iso_utc(st.st_mtime_ns, ns=True),
             "created": iso_utc(getattr(st, "st_birthtime", None) * 1e9, ns=True)
             if getattr(st, "st_birthtime", None) else None,
             "sha256": None, "hash_status": "disabled"}
    if hash_policy == "never":
        return facts
    if hash_policy == "auto" and st.st_size > HASH_AUTO_MAX_BYTES:
        facts["hash_status"] = "skipped_large"
        return facts
    try:
        facts["sha256"] = util.sha256_file(Path(path))
        facts["hash_status"] = "computed"
    except OSError:
        facts["hash_status"] = "failed"
    return facts
