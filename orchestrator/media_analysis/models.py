"""Shared types for media_analysis (stdlib only).

Three kinds of record are kept apart on purpose (see `RecordKind`):
  MEASUREMENT   deterministic numbers from signal processing (a silence, an RMS value)
  TRANSCRIPTION an engine's recognised wording (not ground truth)
  DETECTION     a detector's output (a shot boundary is a visual event, not a topic change)
Subjective readings (joke, awkward, interesting) are never records here; they exist
only as `moment_index.Interpretation` attached to evidence.

All times are seconds on the source media's own timeline (container timestamps),
never frame numbers: frame numbers are unreliable for variable-frame-rate media.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class MediaAnalysisError(ValueError):
    """Invalid data or an operation the subsystem refuses."""


class CapabilityError(MediaAnalysisError):
    """A required executable/engine is not available. Never papered over."""


class RecordKind(str, Enum):
    MEASUREMENT = "measurement"
    TRANSCRIPTION = "transcription"
    DETECTION = "detection"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_time(value: Any, name: str = "time") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MediaAnalysisError(f"{name} must be a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v) or v < 0:
        raise MediaAnalysisError(f"{name} must be finite and >= 0, got {value!r}")
    return v


def check_range(start: Any, end: Any, duration: float | None = None) -> tuple[float, float]:
    s, e = check_time(start, "start_s"), check_time(end, "end_s")
    if e < s:
        raise MediaAnalysisError(f"end_s ({e}) is before start_s ({s})")
    if duration is not None and e > duration + 0.5:   # 0.5 s slack for container rounding
        raise MediaAnalysisError(f"end_s ({e}) is beyond media duration ({duration})")
    return s, e


def check_unit(value: Any, name: str) -> float | None:
    if value is None:
        return None
    v = check_time(value, name)
    if v > 1.0:
        raise MediaAnalysisError(f"{name} must be within 0..1, got {value!r}")
    return v


def stable_hash(*parts: Any, n: int = 12) -> str:
    """Deterministic short hash of JSON-serialisable parts."""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:n]


def t_key(t: float | None) -> str:
    return "" if t is None else f"{t:.3f}"


def atomic_write_json(path: str | os.PathLike, obj: Any) -> None:
    """Write via temp file + fsync + replace: readers never see a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_json(path: str | os.PathLike) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MediaAnalysisError(f"cannot read {path}: {exc}") from exc


@dataclass(frozen=True)
class MediaRef:
    """Exact reference to one original recording. Downstream records carry only
    `media_id`; this resolves it. `sha256`/`size`/`mtime_ns` pin the exact bytes."""
    media_id: str
    path: str
    size: int | None = None
    mtime_ns: int | None = None
    sha256: str | None = None
    duration_s: float | None = None

    def __post_init__(self) -> None:
        if not self.media_id or not self.path:
            raise MediaAnalysisError("MediaRef needs media_id and path")

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> "MediaRef":
        """Build from a manifest item (this package's or creator_state's source_manifest.json)."""
        f = item.get("file") or {}
        c = item.get("container") or {}
        return cls(media_id=item["id"], path=item["path"], size=f.get("size"),
                   mtime_ns=f.get("mtime_ns"), sha256=f.get("sha256"),
                   duration_s=c.get("duration_seconds"))

    def to_dict(self) -> dict:
        return {"media_id": self.media_id, "path": self.path, "size": self.size,
                "mtime_ns": self.mtime_ns, "sha256": self.sha256, "duration_s": self.duration_s}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "MediaRef":
        return cls(d["media_id"], d["path"], d.get("size"), d.get("mtime_ns"), d.get("sha256"),
                   d.get("duration_s"))


@dataclass(frozen=True)
class Provenance:
    """Who/what produced a record and with which settings."""
    engine: str                       # e.g. "energy-activity", "ffmpeg-scene", "fixture"
    version: str
    kind: str = "deterministic"       # deterministic | ml | synthetic_fixture | human
    settings: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.engine.strip() or not self.version.strip():
            raise MediaAnalysisError("provenance needs engine and version")
        if self.kind not in ("deterministic", "ml", "synthetic_fixture", "human"):
            raise MediaAnalysisError(f"unknown provenance kind {self.kind!r}")

    @property
    def settings_hash(self) -> str:
        return stable_hash(self.engine, self.version, dict(self.settings), n=8)

    def to_dict(self) -> dict:
        return {"engine": self.engine, "version": self.version, "kind": self.kind,
                "settings": dict(self.settings), "created_at": self.created_at, "notes": self.notes}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Provenance":
        return cls(d["engine"], d["version"], d.get("kind", "deterministic"), d.get("settings", {}),
                   d.get("created_at", ""), d.get("notes", ""))
