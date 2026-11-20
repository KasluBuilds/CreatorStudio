"""Export verification: read a rendered file with ffprobe and compare it with what was requested.

The file is the proof of a render, never Resolve's job status (a "Complete" job can still write a stub or an
audio-only file; see RESOLVE_INTEGRATION.md). ffprobe is located with `shutil.which` or given explicitly; when it is
missing, verification fails closed (`ffprobe_unavailable`) instead of passing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional

from .timebase import TimebaseError, parse_rate


@dataclass(frozen=True)
class StreamInfo:
    kind: str                         # "video" | "audio" | other codec_type
    codec: str
    width: Optional[int] = None
    height: Optional[int] = None
    frame_rate: Optional[str] = None  # exact "num/den" from r_frame_rate
    frames: Optional[int] = None      # nb_frames (or counted)
    duration: Optional[str] = None    # seconds as text, as ffprobe printed it
    sample_rate: Optional[int] = None
    channels: Optional[int] = None


@dataclass(frozen=True)
class MediaInfo:
    path: str
    size_bytes: int
    container: str
    duration: Optional[str]
    streams: tuple = field(default_factory=tuple)

    @property
    def video(self) -> List[StreamInfo]:
        return [s for s in self.streams if s.kind == "video"]

    @property
    def audio(self) -> List[StreamInfo]:
        return [s for s in self.streams if s.kind == "audio"]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["streams"] = [asdict(s) for s in self.streams]
        return d


class ProbeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


Prober = Callable[[str], MediaInfo]


def _i(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_ffprobe_json(path: str, size: int, doc: Mapping[str, Any]) -> MediaInfo:
    streams = []
    for s in doc.get("streams") or []:
        if not isinstance(s, dict):
            continue
        rate = s.get("r_frame_rate") if s.get("codec_type") == "video" else None
        streams.append(StreamInfo(kind=str(s.get("codec_type") or ""), codec=str(s.get("codec_name") or ""),
                                  width=_i(s.get("width")), height=_i(s.get("height")),
                                  frame_rate=str(rate) if rate else None, frames=_i(s.get("nb_frames")),
                                  duration=str(s["duration"]) if s.get("duration") else None,
                                  sample_rate=_i(s.get("sample_rate")), channels=_i(s.get("channels"))))
    fmt = doc.get("format") if isinstance(doc.get("format"), dict) else {}
    return MediaInfo(path=path, size_bytes=size, container=str(fmt.get("format_name") or ""),
                     duration=str(fmt["duration"]) if fmt.get("duration") else None, streams=tuple(streams))


def ffprobe(path: str, *, executable: Optional[str] = None, timeout: int = 60,
            runner: Optional[Callable[..., Any]] = None) -> MediaInfo:
    """Probe `path`. `runner` replaces `subprocess.run` (same signature) so tests can inject ffprobe's output."""
    exe = executable or shutil.which("ffprobe")
    if not exe:
        raise ProbeError("ffprobe_unavailable", "ffprobe was not found; the export cannot be verified")
    if not os.path.isfile(path):
        raise ProbeError("missing", f"no file at {path}")
    size = os.path.getsize(path)
    try:
        proc = (runner or subprocess.run)(
            [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ProbeError("probe_timeout", "ffprobe did not finish")
    except OSError as exc:
        raise ProbeError("ffprobe_unavailable", f"ffprobe could not be started ({type(exc).__name__})")
    if proc.returncode != 0:
        raise ProbeError("unreadable", (proc.stderr or "ffprobe failed").strip()[:300])
    try:
        doc = json.loads(proc.stdout or "{}")
    except ValueError:
        raise ProbeError("unreadable", "ffprobe printed invalid JSON")
    return parse_ffprobe_json(path, size, doc)


def count_frames(path: str, *, executable: Optional[str] = None, timeout: int = 300,
                 runner: Optional[Callable[..., Any]] = None) -> Optional[int]:
    """Decode-count video frames (slow but exact) when the container has no nb_frames."""
    exe = executable or shutil.which("ffprobe")
    if not exe:
        return None
    try:
        proc = (runner or subprocess.run)(
            [exe, "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
             "stream=nb_read_frames", "-of", "default=nokey=1:noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None
    return _i((proc.stdout or "").strip()) if proc.returncode == 0 else None


# Codec names ffprobe reports for the render codec ids this package accepts.
FFPROBE_CODEC = {"H264": "h264", "H265": "hevc", "H.264": "h264", "H.265": "hevc"}


def verify_media(info: MediaInfo, expect: Mapping[str, Any]) -> List[str]:
    """Compare a probed file with an expectation. Keys (all optional): video_codec, width, height, frame_rate,
    frames (exact video frame count), audio (bool: an audio stream must / must not exist), audio_channels,
    min_size_bytes. Returns human-readable problems; empty means the file matches."""
    problems: List[str] = []
    if info.size_bytes <= int(expect.get("min_size_bytes", 1)):
        problems.append(f"file is too small ({info.size_bytes} bytes)")
    want_video = expect.get("video", True)
    v = info.video[0] if info.video else None
    if want_video and v is None:
        problems.append("no video stream")
    if not want_video and v is not None:
        problems.append("unexpected video stream")
    if v is not None:
        codec = expect.get("video_codec")
        if codec and v.codec != FFPROBE_CODEC.get(codec, codec).lower():
            problems.append(f"video codec {v.codec!r}, expected {FFPROBE_CODEC.get(codec, codec)!r}")
        for key in ("width", "height"):
            if expect.get(key) is not None and getattr(v, key) != int(expect[key]):
                problems.append(f"{key} {getattr(v, key)}, expected {expect[key]}")
        if expect.get("frame_rate") is not None:
            try:
                got = parse_rate(v.frame_rate) if v.frame_rate else None
                want = parse_rate(str(expect["frame_rate"]))
            except TimebaseError as exc:
                problems.append(f"frame rate unreadable: {exc}")
            else:
                if got != want:
                    problems.append(f"frame rate {v.frame_rate}, expected {want}")
        if expect.get("frames") is not None:
            if v.frames is None:
                problems.append("video frame count unavailable")
            elif v.frames != int(expect["frames"]):
                problems.append(f"{v.frames} video frames, expected {expect['frames']}")
    if "audio" in expect:
        if expect["audio"] and not info.audio:
            problems.append("no audio stream")
        if not expect["audio"] and info.audio:
            problems.append("unexpected audio stream")
    if expect.get("audio_channels") is not None and info.audio:
        if info.audio[0].channels != int(expect["audio_channels"]):
            problems.append(f"audio channels {info.audio[0].channels}, expected {expect['audio_channels']}")
    return problems


def duration_frames(info: MediaInfo, rate: str) -> Optional[int]:
    """Container duration in whole frames at `rate` (nearest), for a cross-check against nb_frames."""
    if not info.duration:
        return None
    try:
        sec = Fraction(info.duration)
    except ValueError:
        return None
    q = sec * parse_rate(rate).value
    return (2 * q.numerator + q.denominator) // (2 * q.denominator)


def report(info: Optional[MediaInfo], problems: List[str], error: Optional[str] = None) -> Dict[str, Any]:
    return {"ok": not problems and error is None, "problems": list(problems), "error": error,
            "media": info.to_dict() if info else None}
