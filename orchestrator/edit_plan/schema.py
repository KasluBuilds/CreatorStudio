"""Edit-plan schema "edit-plan/0.1": the contract between creative reasoning and deterministic execution.

Conventions (all explicit in the data, nothing implied):
* Frame rates are exact rationals (`Rate(num, den)`), never floats. 29.97 is Rate(30000, 1001).
* Source ranges give `source_in` (always an included frame) and `source_out`, and each segment states in
  `range_convention` whether `source_out` is "inclusive_out" (the last included frame) or "exclusive_out" (the
  first frame NOT included; the half-open convention of resolve_executor.timebase). Source frames count from
  frame 0 of the file.
* Record positions (`record_in`) are timeline frames RELATIVE to the timeline start (0 = first frame at the
  start timecode). Record durations are derived from the source length and the two rates (floor, as Resolve
  does), so they are never stored for source segments.
* Nothing here is creator speech except what a caption quotes from a transcript range.

This is a PROPOSED/implemented-offline contract: unit-tested only, nothing about it is VERIFIED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import _codec
from ._codec import PlanSchemaError

SCHEMA_VERSION = "edit-plan/0.1"

TRACK_KINDS = ("video", "audio", "subtitle")
PURPOSES = ("dialogue", "b_roll", "music", "sfx", "title")
MEDIA_PARTS = ("video", "audio", "video_audio")
RANGE_CONVENTIONS = ("inclusive_out", "exclusive_out")
ORIGINS = ("creator_recording", "other_recording", "stock_footage", "music", "sfx", "graphic", "generated_speech")
SPEAKERS = ("creator", "other", "unknown", "none")
TRANSITION_KINDS = ("cut", "dissolve", "dip_to_black")
CAPTION_RENDERS = ("burn_in", "subtitle_track")
MARKER_COLORS = ("Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky",
                 "Mint", "Lemon", "Sand", "Cocoa", "Cream")
QC_KINDS = ("total_duration_frames", "frame_rate", "resolution", "no_gaps_on_base_tracks",
            "captions_match_transcript", "sources_unmodified", "custom")
EXPORT_FORMATS = ("mp4", "mov")
EXPORT_CODECS = ("H264", "H265")


def _ch(values: tuple[str, ...], **kw: Any):
    return field(metadata={"choices": values}, **kw)


@dataclass(frozen=True)
class Rationale:
    """Why this item exists. `text` is a description by the planner, never something the creator said."""
    text: str
    brief_item_id: str | None = None        # the brief's section/moment/concept id this item serves


@dataclass(frozen=True)
class Rate:
    num: int
    den: int = 1


@dataclass(frozen=True)
class PlanMeta:
    project_slug: str
    plan_id: str
    created_by: str
    source_brief_id: str | None = None
    source_brief_hash: str | None = None    # sha256 of the brief (or its material digest) this plan was derived from


@dataclass(frozen=True)
class TimelineSettings:
    name: str
    frame_rate: Rate
    width: int
    height: int
    start_timecode: str = "01:00:00:00"     # non-drop HH:MM:SS:FF


@dataclass(frozen=True)
class SourceEntry:
    """Reference to one original. The path is a reference only: it is never opened, copied or modified here."""
    source_id: str
    path: str
    frame_rate: Rate
    duration_frames: int
    content_hash: str                        # sha256 hex of the file content
    origin: str = _ch(ORIGINS)
    has_video: bool = True
    has_audio: bool = True


@dataclass(frozen=True)
class Track:
    track_id: str
    kind: str = _ch(TRACK_KINDS)
    index: int = 1                           # 1-based, as in Resolve (V1, A1, ...)


@dataclass(frozen=True)
class Segment:
    segment_id: str
    purpose: str = _ch(PURPOSES)
    track_id: str
    record_in: int
    rationale: Rationale
    source_id: str | None = None             # None only for purpose "title"
    source_in: int | None = None
    source_out: int | None = None
    range_convention: str | None = _ch(RANGE_CONVENTIONS, default=None)
    media: str = _ch(MEDIA_PARTS, default="video_audio")
    audio_track_id: str | None = None        # required with media "video_audio"
    speaker: str = _ch(SPEAKERS, default="none")   # who is heard; "creator" only for the creator's own recording
    title_text: str | None = None            # purpose "title" only: on-screen text, not dialogue
    title_duration_frames: int | None = None  # purpose "title" only (timeline frames)
    audio_gain_db: int | None = None         # any value is a request the executor cannot honour (capability gate)


@dataclass(frozen=True)
class Transition:
    transition_id: str
    kind: str = _ch(TRANSITION_KINDS)
    from_segment_id: str
    to_segment_id: str
    duration_frames: int
    rationale: Rationale


@dataclass(frozen=True)
class Marker:
    marker_id: str
    frame: int                               # timeline frames relative to the timeline start
    rationale: Rationale
    name: str = ""
    note: str = ""
    color: str = _ch(MARKER_COLORS, default="Blue")
    duration_frames: int = 1


@dataclass(frozen=True)
class TranscriptRange:
    """The transcript span a caption quotes: source frames `[start_frame, end_frame)` of `source_id`."""
    source_id: str
    start_frame: int
    end_frame: int


@dataclass(frozen=True)
class Caption:
    caption_id: str
    text: str                                # must be a verbatim quote of `transcript_ref`; never free dialogue
    track_id: str
    record_in: int
    duration_frames: int
    rationale: Rationale
    transcript_ref: TranscriptRange | None = None
    render: str = _ch(CAPTION_RENDERS, default="burn_in")


@dataclass(frozen=True)
class QCExpectation:
    qc_id: str
    kind: str = _ch(QC_KINDS)
    description: str = ""
    expected: dict[str, Any] = field(default_factory=dict)   # e.g. {"frames": 250}; scalars only


@dataclass(frozen=True)
class ExportTarget:
    """A requested output. Only the directory is checked against the allowlist; nothing is written here."""
    target_id: str
    directory: str
    file_name: str
    format: str = _ch(EXPORT_FORMATS, default="mp4")
    codec: str = _ch(EXPORT_CODECS, default="H264")


@dataclass(frozen=True)
class EditPlan:
    version: str
    meta: PlanMeta
    timeline: TimelineSettings
    sources: tuple[SourceEntry, ...]
    tracks: tuple[Track, ...]
    segments: tuple[Segment, ...]
    transitions: tuple[Transition, ...] = ()
    markers: tuple[Marker, ...] = ()
    captions: tuple[Caption, ...] = ()
    qc_expectations: tuple[QCExpectation, ...] = ()
    exports: tuple[ExportTarget, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _codec.dump(self)

    @classmethod
    def from_dict(cls, data: Any) -> "EditPlan":
        """Strict load. Unknown fields, unknown versions, missing fields and wrong types raise PlanSchemaError."""
        if not isinstance(data, dict):
            raise PlanSchemaError("SCHEMA_BAD_VALUE", "", "an edit plan must be an object")
        version = data.get("version")
        if version != SCHEMA_VERSION:
            raise PlanSchemaError("SCHEMA_UNKNOWN_VERSION", "/version",
                                  f"{version!r} is not the supported version {SCHEMA_VERSION!r}")
        return _codec.load(cls, data)


def json_schema() -> dict[str, Any]:
    """The JSON Schema of an edit plan (draft 2020-12). `version` is pinned to the supported version."""
    s = _codec.json_schema(EditPlan)
    s["title"] = f"Edit plan {SCHEMA_VERSION}"
    s["properties"]["version"] = {"const": SCHEMA_VERSION}
    return s
