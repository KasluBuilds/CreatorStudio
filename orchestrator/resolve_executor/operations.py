"""Typed operation requests: the only vocabulary the executor accepts.

An Editor (or any caller) builds these frozen dataclasses; nothing here talks to Resolve. Each request names its
kind, whether it mutates Resolve, and carries an `op_id` (unique per request, used in logs) and an optional
`idempotency_key` (stable across retries of the *same intended change*). There is deliberately no "call this API
method" request: anything not expressible here is unsupported.

Frame conventions are those of `timebase`: source ranges are half-open source frames counted from the first frame
of the file; `record_frame` and `ItemRef.start_frame` are absolute timeline frames (they include the timeline start
frame); marker frames are relative to the timeline start (as Resolve's marker API uses them).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Mapping, Optional, Tuple

from .timebase import FrameRange

TRACK_KINDS = ("video", "audio")
MEDIA_PARTS = ("video", "audio", "video_audio")
AUDIO_FORMATS = ("mono", "stereo")
MARKER_COLORS = ("Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky",
                 "Mint", "Lemon", "Sand", "Cocoa", "Cream")

# Edit-page Inspector transform keys accepted by SetTransform, with inclusive numeric bounds.
# Bounds follow the Inspector's ranges; they are a sanity guard, not a claim about what looks good.
TRANSFORM_KEYS: Mapping[str, Tuple[float, float]] = {
    "ZoomX": (0.0, 100.0), "ZoomY": (0.0, 100.0),
    "Pan": (-100000.0, 100000.0), "Tilt": (-100000.0, 100000.0),
    "RotationAngle": (-360.0, 360.0),
    "AnchorPointX": (-100000.0, 100000.0), "AnchorPointY": (-100000.0, 100000.0),
    "Pitch": (-1.5, 1.5), "Yaw": (-1.5, 1.5),
    "CropLeft": (0.0, 100000.0), "CropRight": (0.0, 100000.0),
    "CropTop": (0.0, 100000.0), "CropBottom": (0.0, 100000.0),
    "CropSoftness": (-100.0, 100.0),
    "Opacity": (0.0, 100.0),
}
BOOL_TRANSFORM_KEYS = ("FlipX", "FlipY", "CropRetain")


@dataclass(frozen=True)
class ItemRef:
    """Identifies one timeline item by track and absolute start frame (unique on a track: items cannot overlap)."""

    track_kind: str
    track_index: int
    start_frame: int

    def to_dict(self) -> dict:
        return {"track_kind": self.track_kind, "track_index": self.track_index, "start_frame": self.start_frame}


@dataclass(frozen=True)
class Operation:
    KIND: ClassVar[str] = ""
    MUTATING: ClassVar[bool] = True
    DESTRUCTIVE: ClassVar[bool] = False   # removes something from a timeline (never source media)

    op_id: str

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.KIND}
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            if isinstance(v, FrameRange):
                v = {"start": v.start, "end": v.end}
            elif isinstance(v, ItemRef):
                v = v.to_dict()
            elif isinstance(v, tuple):
                v = [x.to_dict() if isinstance(x, ItemRef) else x for x in v]
            elif isinstance(v, Mapping):
                v = dict(v)
            out[f.name] = v
        return out


# ── read-only ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InspectProject(Operation):
    KIND: ClassVar[str] = "inspect_project"
    MUTATING: ClassVar[bool] = False


@dataclass(frozen=True)
class InspectTimeline(Operation):
    KIND: ClassVar[str] = "inspect_timeline"
    MUTATING: ClassVar[bool] = False
    timeline: str = ""


@dataclass(frozen=True)
class VerifyExport(Operation):
    """Probe an existing file and compare it with an expectation (no Resolve call)."""

    KIND: ClassVar[str] = "verify_export"
    MUTATING: ClassVar[bool] = False
    path: str = ""
    expect: Mapping[str, Any] = field(default_factory=dict)


# ── mutating ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CreateProject(Operation):
    """Create and open a NEW project (refused if the name exists). Used for disposable test projects."""

    KIND: ClassVar[str] = "create_project"
    name: str = ""
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class ImportMedia(Operation):
    KIND: ClassVar[str] = "import_media"
    paths: Tuple[str, ...] = ()
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class CreateTimeline(Operation):
    KIND: ClassVar[str] = "create_timeline"
    name: str = ""
    frame_rate: str = ""          # "25", "29.97", "23.976", "30000/1001", ...
    width: int = 0
    height: int = 0
    start_timecode: str = "01:00:00:00"
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class AddTrack(Operation):
    KIND: ClassVar[str] = "add_track"
    timeline: str = ""
    track_kind: str = "video"
    audio_format: str = "stereo"
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class AppendSegment(Operation):
    """Place source frames `[source.start, source.end)` of one media file at `record_frame` on a track.

    `media`: "video" (video part only, onto video track `track_index`), "audio" (audio part only, onto audio track
    `track_index`) or "video_audio" (both, video onto V`track_index`, audio onto A`audio_track_index`).
    `record_frame` None means "after the last item of the target track".
    """

    KIND: ClassVar[str] = "append_segment"
    timeline: str = ""
    media_path: str = ""
    source: Optional[FrameRange] = None
    track_index: int = 1
    record_frame: Optional[int] = None
    media: str = "video"
    audio_track_index: Optional[int] = None
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class SetTransform(Operation):
    KIND: ClassVar[str] = "set_transform"
    timeline: str = ""
    item: Optional[ItemRef] = None
    values: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class AddMarker(Operation):
    KIND: ClassVar[str] = "add_marker"
    timeline: str = ""
    frame: int = 0                # relative to the timeline start
    color: str = "Blue"
    name: str = ""
    note: str = ""
    duration: int = 1
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class PlaceTitle(Operation):
    """A Text+ title on a chosen video track and frame range (nested-timeline route; see capabilities)."""

    KIND: ClassVar[str] = "place_title"
    timeline: str = ""
    text: str = ""
    track_index: int = 2
    record_frame: int = 0
    duration: int = 0
    size: Optional[float] = None          # Text+ Size (0..1 of frame height-ish units, Fusion's own)
    center: Optional[Tuple[float, float]] = None   # Text+ Center, normalized 0..1 (0.5, 0.5 = middle)
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class SetClipAudioLevel(Operation):
    """Per-clip volume in dB. Kept as a request type so it can be refused explicitly (see capabilities)."""

    KIND: ClassVar[str] = "set_clip_audio_level"
    timeline: str = ""
    item: Optional[ItemRef] = None
    volume_db: float = 0.0
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class AddTransition(Operation):
    """A transition at an edit point. Kept so it can be refused explicitly on builds without AddTransition."""

    KIND: ClassVar[str] = "add_transition"
    timeline: str = ""
    item: Optional[ItemRef] = None
    transition: str = "Cross Dissolve"
    duration: int = 0
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class AddSubtitle(Operation):
    """A subtitle-track caption. Kept so it can be refused explicitly (no safe API on 21.0; see capabilities)."""

    KIND: ClassVar[str] = "add_subtitle"
    timeline: str = ""
    text: str = ""
    record_frame: int = 0
    duration: int = 0
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class DeleteItems(Operation):
    """Remove timeline items (never media, never files). Used to undo a wrong placement."""

    KIND: ClassVar[str] = "delete_items"
    DESTRUCTIVE: ClassVar[bool] = True
    timeline: str = ""
    items: Tuple[ItemRef, ...] = ()
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class SaveProject(Operation):
    KIND: ClassVar[str] = "save_project"
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class RenderSpec:
    """What to render and what the result must look like."""

    target_dir: str
    file_name: str                      # without extension; the format decides the extension
    format: str = "mp4"
    codec: str = "H264"
    width: int = 1920
    height: int = 1080
    export_video: bool = True
    export_audio: bool = True
    mark_in: Optional[int] = None       # absolute timeline frames, half-open [mark_in, mark_out)
    mark_out: Optional[int] = None
    timeout_seconds: int = 600

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class RenderTimeline(Operation):
    """Configure, queue, start, monitor and verify one render of a timeline."""

    KIND: ClassVar[str] = "render_timeline"
    timeline: str = ""
    spec: Optional[RenderSpec] = None
    idempotency_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = super().to_dict()
        out["spec"] = self.spec.to_dict() if self.spec else None
        return out


ALL_OPERATIONS = (InspectProject, InspectTimeline, VerifyExport, CreateProject, ImportMedia, CreateTimeline, AddTrack,
                  AppendSegment, SetTransform, AddMarker, PlaceTitle, SetClipAudioLevel, AddTransition, AddSubtitle,
                  DeleteItems, SaveProject, RenderTimeline)
OPERATION_KINDS = {cls.KIND: cls for cls in ALL_OPERATIONS}
