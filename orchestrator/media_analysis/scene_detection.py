"""Shot / visual-change detection interface and one deterministic ffmpeg-based detector.

A `ShotBoundary` is a VISUAL EVENT: the picture changed abruptly at that time. It does not
mean the subject, topic or scene in the story sense changed (a lighting flicker, a screen
recording window switch or a hand covering the lens can all trigger one).

`score` is the detector's own raw value (ffmpeg's scene score, 0..1) - it is NOT a
probability, so `confidence` stays None unless an engine really supplies one.
Frame references are timestamps (plus an optional extracted image path); frame numbers are
optional because they are unreliable on variable-frame-rate media.

If ffmpeg is missing, `FfmpegSceneDetector` raises CapabilityError: no detections are invented.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence, runtime_checkable

from .models import (CapabilityError, MediaAnalysisError, MediaRef, Provenance, check_range, check_time,
                     check_unit, stable_hash, t_key)
from .proxies import default_runner, find_ffmpeg


@dataclass(frozen=True)
class FrameRef:
    media_id: str
    time_s: float
    frame_index: int | None = None          # only if the engine supplied it; do not trust on VFR media
    image_path: str | None = None           # optional extracted still (an analysis file, never an original)

    def __post_init__(self) -> None:
        check_time(self.time_s, "frame time_s")
        if self.frame_index is not None and self.frame_index < 0:
            raise MediaAnalysisError("frame_index must be >= 0")


@dataclass(frozen=True)
class ShotBoundary:
    media_id: str
    time_s: float
    method: str                             # e.g. "ffmpeg-scene@0.4"
    score: float | None = None              # raw detector value
    confidence: float | None = None         # only if the engine provides a calibrated one
    frame_refs: tuple[FrameRef, ...] = ()
    analysis_copy_id: str | None = None     # copy that was analysed; time_s is still on the original timeline

    def __post_init__(self) -> None:
        if not self.media_id or not self.method:
            raise MediaAnalysisError("boundary needs media_id and method")
        check_time(self.time_s, "time_s")
        check_unit(self.confidence, "confidence")
        object.__setattr__(self, "frame_refs", tuple(self.frame_refs))
        for f in self.frame_refs:
            if f.media_id != self.media_id:
                raise MediaAnalysisError("frame ref belongs to a different media id")

    @property
    def start_s(self) -> float:
        return self.time_s

    @property
    def end_s(self) -> float:
        return self.time_s

    @property
    def record_id(self) -> str:
        return "shot_" + stable_hash(self.media_id, t_key(self.time_s), self.method)

    def to_dict(self) -> dict:
        return {"media_id": self.media_id, "time_s": self.time_s, "method": self.method, "score": self.score,
                "confidence": self.confidence,
                "frame_refs": [dict(vars(f)) for f in self.frame_refs], "analysis_copy_id": self.analysis_copy_id}

    @classmethod
    def from_dict(cls, d) -> "ShotBoundary":
        return cls(d["media_id"], d["time_s"], d["method"], d.get("score"), d.get("confidence"),
                   tuple(FrameRef(**f) for f in d.get("frame_refs", [])), d.get("analysis_copy_id"))


@dataclass(frozen=True)
class VisualEvent:
    """A time range with a notable visual change, labelled by the DETECTOR's vocabulary
    (e.g. "motion_burst"). Not a judgement about meaning."""
    media_id: str
    start_s: float
    end_s: float
    label: str
    method: str
    description: str = ""
    confidence: float | None = None
    frame_refs: tuple[FrameRef, ...] = ()

    def __post_init__(self) -> None:
        check_range(self.start_s, self.end_s)
        check_unit(self.confidence, "confidence")
        if not self.label or not self.method:
            raise MediaAnalysisError("visual event needs label and method")

    @property
    def record_id(self) -> str:
        return "vis_" + stable_hash(self.media_id, t_key(self.start_s), t_key(self.end_s), self.label, self.method)


@runtime_checkable
class SceneDetector(Protocol):
    name: str
    version: str
    def detect(self, media: MediaRef) -> Sequence[ShotBoundary]: ...


class UnavailableDetector:
    name, version = "none", "0"

    def __init__(self, reason: str = "no scene-detection engine available"):
        self._reason = reason

    def detect(self, media):
        raise CapabilityError(self._reason)


class FixtureDetector:
    """Returns prepared boundaries. SYNTHETIC; used by tests only."""
    name, version = "fixture", "1"

    def __init__(self, boundaries: Sequence[ShotBoundary]):
        self._b = tuple(boundaries)

    def detect(self, media):
        return [b for b in self._b if b.media_id == media.media_id]


_PTS_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=(\d+(?:\.\d+)?)")


def parse_scene_output(text: str, media_id: str, method: str, duration_s: float | None = None,
                       analysis_copy_id: str | None = None) -> list[ShotBoundary]:
    """Parse ffmpeg `select='gt(scene,T)',metadata=print:file=-` output (pure, unit-testable)."""
    out, pending = [], None
    for line in text.splitlines():
        m = _PTS_RE.search(line)
        if m:
            pending = float(m.group(1))
            continue
        s = _SCORE_RE.search(line)
        if s and pending is not None:
            if duration_s is None or pending <= duration_s + 0.5:
                out.append(ShotBoundary(media_id, pending, method, score=min(1.0, float(s.group(1))),
                                        frame_refs=(FrameRef(media_id, pending),),
                                        analysis_copy_id=analysis_copy_id))
            pending = None
    return out


class FfmpegSceneDetector:
    """Deterministic frame-difference detector using ffmpeg's `scene` score. Reads the video
    (original or proxy) and writes nothing."""
    name, version = "ffmpeg-scene", "1"

    def __init__(self, threshold: float = 0.4, ffmpeg: str | None = None,
                 runner: Callable[[list], tuple] = default_runner, which=shutil.which):
        if not 0.0 < threshold < 1.0:
            raise MediaAnalysisError("threshold must be within (0, 1)")
        self.threshold, self._ffmpeg, self._runner, self._which = threshold, ffmpeg, runner, which

    @property
    def method(self) -> str:
        return f"{self.name}@{self.threshold}"

    def provenance(self) -> Provenance:
        return Provenance(self.name, self.version, "deterministic", {"threshold": self.threshold})

    def detect(self, media: MediaRef, video_path: str | None = None,
               analysis_copy_id: str | None = None) -> list[ShotBoundary]:
        ffmpeg = find_ffmpeg(self._ffmpeg, self._which)
        if not ffmpeg:
            raise CapabilityError("ffmpeg is not available: scene detection cannot run (no detections invented)")
        src = video_path or media.path
        argv = [ffmpeg, "-hide_banner", "-nostdin", "-i", src, "-an", "-vf",
                f"select='gt(scene,{self.threshold})',metadata=print:file=-", "-f", "null", "-"]
        rc, out, err = self._runner(argv)
        if rc != 0:
            raise MediaAnalysisError(f"ffmpeg scene detection failed (exit {rc}): {err.strip()[-300:]}")
        return parse_scene_output(out, media.media_id, self.method, media.duration_s, analysis_copy_id)


SHOTS_SCHEMA = "media_analysis.shots/1"


def save_boundaries(directory, media_id: str, provenance: Provenance, boundaries: Sequence[ShotBoundary]):
    """Write-once per (media, engine, settings); returns the path."""
    from pathlib import Path
    from .models import atomic_write_json, read_json
    path = Path(directory) / f"{media_id}.shots-{provenance.settings_hash}.json"
    doc = {"schema": SHOTS_SCHEMA, "media_id": media_id, "provenance": provenance.to_dict(),
           "boundaries": [b.to_dict() for b in boundaries]}
    if path.exists():
        old = read_json(path)
        for d in (old, doc):
            d["provenance"].pop("created_at", None)
        if old != doc:
            raise MediaAnalysisError(f"{path.name} exists with different content; refusing to overwrite")
        return path
    atomic_write_json(path, doc)
    return path


def load_boundaries(path) -> tuple[Provenance, list[ShotBoundary]]:
    from .models import read_json
    d = read_json(path)
    if d.get("schema") != SHOTS_SCHEMA:
        raise MediaAnalysisError(f"unrecognised shots schema {d.get('schema')!r}")
    return Provenance.from_dict(d["provenance"]), [ShotBoundary.from_dict(b) for b in d["boundaries"]]
