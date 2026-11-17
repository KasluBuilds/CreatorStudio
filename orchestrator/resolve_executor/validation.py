"""Deterministic request validation. Never imports or calls a Resolve adapter.

`validate(op, policy)` returns a list of problems; empty means the request is well-formed and safe to *attempt*.
Checks that need the live timeline (is the slot free, does the track exist, is the source range inside the media)
are preconditions, done by the executor from adapter readback after this step.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from . import operations as ops
from .timebase import FrameRange, TimebaseError, parse_rate, timecode_to_frames

_OP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()-]{0,99}$")       # project/timeline names
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")      # render output base names
SUPPORTED_TIMELINE_RATES = ("23.976", "24", "25", "29.97", "30", "50", "59.94", "60")
RENDER_FORMATS = {"mp4": ("H264", "H265"), "mov": ("H264", "H265")}
MAX_DIMENSION = 8192
MAX_TEXT = 500
PAGES = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")


@dataclass(frozen=True)
class Policy:
    """Where the executor may read media from and write renders to. Paths are compared after normalisation."""

    allowed_media_roots: Tuple[str, ...] = ()
    allowed_output_roots: Tuple[str, ...] = ()
    file_exists: Callable[[str], bool] = os.path.isfile
    dir_exists: Callable[[str], bool] = os.path.isdir
    forbidden_media_names: Tuple[str, ...] = ()          # e.g. real recordings tests must never touch
    max_tracks: int = 32


def normalise(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def inside(path: str, roots: Sequence[str]) -> bool:
    """True when `path` is a root or lies below one. Compares normalised absolute paths component-wise, so
    `C:\\Users\\K\\Movies2` is not inside `C:\\Users\\K\\Movies` and `..` cannot escape."""
    p = normalise(path)
    for root in roots:
        r = normalise(root)
        if p == r:
            return True
        try:
            if os.path.commonpath([p, r]) == r:
                return True
        except ValueError:      # different drives
            continue
    return False


def _real_inside(path: str, roots: Sequence[str]) -> bool:
    """Also check the symlink-resolved path, so a link inside a root cannot point outside it."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return inside(path, roots) and inside(real, roots)


def _check_name(label: str, value: str, problems: List[str]) -> None:
    if not isinstance(value, str) or not _NAME.match(value):
        problems.append(f"{label} {value!r} must be 1-100 characters of letters, digits, space, _ . ( ) -")


def _check_int(label: str, value, problems: List[str], minimum: int = 0, maximum: Optional[int] = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"{label} must be an int")
        return False
    if value < minimum or (maximum is not None and value > maximum):
        problems.append(f"{label} {value} is outside [{minimum}, {maximum if maximum is not None else 'inf'}]")
        return False
    return True


def _check_track(label: str, kind: str, index, problems: List[str], policy: Policy) -> None:
    if kind not in ops.TRACK_KINDS:
        problems.append(f"{label} track kind {kind!r} must be one of {ops.TRACK_KINDS}")
    _check_int(f"{label} track index", index, problems, 1, policy.max_tracks)


def _check_item(op, problems: List[str], policy: Policy) -> None:
    ref = op.item
    if not isinstance(ref, ops.ItemRef):
        problems.append("item must be an ItemRef")
        return
    _check_track("item", ref.track_kind, ref.track_index, problems, policy)
    _check_int("item start_frame", ref.start_frame, problems, 0)


def _check_media_path(path: str, problems: List[str], policy: Policy) -> None:
    if not isinstance(path, str) or not path:
        problems.append("media path is empty")
        return
    if not os.path.isabs(path):
        problems.append(f"media path {path!r} must be absolute")
        return
    if os.path.basename(path).lower() in {n.lower() for n in policy.forbidden_media_names}:
        problems.append(f"media {os.path.basename(path)!r} is on the forbidden list")
    if not policy.allowed_media_roots:
        problems.append("no allowed media roots are configured")
    elif not _real_inside(path, policy.allowed_media_roots):
        problems.append(f"media path {path!r} is outside the allowed media roots")
    if not policy.file_exists(path):
        problems.append(f"media file does not exist: {path!r}")


def validate_render_spec(spec, problems: List[str], policy: Policy) -> None:
    if not isinstance(spec, ops.RenderSpec):
        problems.append("spec must be a RenderSpec")
        return
    if spec.format not in RENDER_FORMATS:
        problems.append(f"render format {spec.format!r} is not supported (supported: {sorted(RENDER_FORMATS)})")
    elif spec.codec not in RENDER_FORMATS[spec.format]:
        problems.append(f"codec {spec.codec!r} is not supported for {spec.format} (use a Resolve codec id: {RENDER_FORMATS[spec.format]})")
    for label, v in (("width", spec.width), ("height", spec.height)):
        if _check_int(label, v, problems, 16, MAX_DIMENSION) and v % 2:
            problems.append(f"{label} {v} must be even for 4:2:0 H.264/H.265")
    if not isinstance(spec.file_name, str) or not _FILE_NAME.match(spec.file_name) or ".." in spec.file_name:
        problems.append(f"file_name {spec.file_name!r} must be 1-120 characters of letters, digits, _ . - and no path")
    if not isinstance(spec.target_dir, str) or not os.path.isabs(spec.target_dir or ""):
        problems.append("target_dir must be an absolute path")
    elif not policy.allowed_output_roots:
        problems.append("no allowed output roots are configured")
    elif not _real_inside(spec.target_dir, policy.allowed_output_roots):
        problems.append(f"target_dir {spec.target_dir!r} is outside the allowed output roots")
    elif not policy.dir_exists(spec.target_dir):
        problems.append(f"target_dir does not exist: {spec.target_dir!r}")
    if not (spec.export_video or spec.export_audio):
        problems.append("a render must export video, audio or both")
    if (spec.mark_in is None) != (spec.mark_out is None):
        problems.append("mark_in and mark_out must be given together")
    elif spec.mark_in is not None:
        if _check_int("mark_in", spec.mark_in, problems, 0) and _check_int("mark_out", spec.mark_out, problems, 0):
            if spec.mark_out <= spec.mark_in:
                problems.append("mark_out must be greater than mark_in (half-open range)")
    _check_int("timeout_seconds", spec.timeout_seconds, problems, 5, 6 * 3600)


def validate(op: ops.Operation, policy: Policy) -> List[str]:
    problems: List[str] = []
    if not isinstance(op, ops.Operation) or type(op) not in ops.ALL_OPERATIONS:
        return ["not a supported operation request"]
    if not isinstance(op.op_id, str) or not _OP_ID.match(op.op_id):
        problems.append("op_id must be 1-96 characters: letters, digits, . _ : -")
    key = getattr(op, "idempotency_key", None)
    if key is not None and (not isinstance(key, str) or not _OP_ID.match(key)):
        problems.append("idempotency_key must be 1-96 characters: letters, digits, . _ : -")
    if hasattr(op, "timeline") and not isinstance(op, ops.VerifyExport):
        _check_name("timeline", op.timeline, problems)

    if isinstance(op, ops.CreateProject):
        _check_name("project name", op.name, problems)
    elif isinstance(op, ops.ImportMedia):
        if not op.paths or not isinstance(op.paths, tuple):
            problems.append("paths must be a non-empty tuple")
        else:
            if len(set(normalise(p) for p in op.paths if isinstance(p, str))) != len(op.paths):
                problems.append("paths contain duplicates")
            for p in op.paths:
                _check_media_path(p, problems, policy)
    elif isinstance(op, ops.CreateTimeline):
        _check_name("timeline name", op.name, problems)
        if op.frame_rate not in SUPPORTED_TIMELINE_RATES:
            problems.append(f"frame_rate {op.frame_rate!r} must be one of {SUPPORTED_TIMELINE_RATES}")
        for label, v in (("width", op.width), ("height", op.height)):
            if _check_int(label, v, problems, 16, MAX_DIMENSION) and v % 2:
                problems.append(f"{label} {v} must be even")
        if op.frame_rate in SUPPORTED_TIMELINE_RATES:
            try:
                timecode_to_frames(op.start_timecode, op.frame_rate)
            except TimebaseError as exc:
                problems.append(f"start_timecode: {exc}")
    elif isinstance(op, ops.AddTrack):
        if op.track_kind not in ops.TRACK_KINDS:
            problems.append(f"track_kind {op.track_kind!r} must be one of {ops.TRACK_KINDS}")
        if op.track_kind == "audio" and op.audio_format not in ops.AUDIO_FORMATS:
            problems.append(f"audio_format {op.audio_format!r} must be one of {ops.AUDIO_FORMATS}")
    elif isinstance(op, ops.AppendSegment):
        _check_media_path(op.media_path, problems, policy)
        if not isinstance(op.source, FrameRange):
            problems.append("source must be a FrameRange (half-open source frames)")
        if op.media not in ops.MEDIA_PARTS:
            problems.append(f"media {op.media!r} must be one of {ops.MEDIA_PARTS}")
        _check_int("track_index", op.track_index, problems, 1, policy.max_tracks)
        if op.record_frame is not None:
            _check_int("record_frame", op.record_frame, problems, 0)
        if op.media == "video_audio":
            if op.audio_track_index is None:
                problems.append("video_audio needs audio_track_index")
            else:
                _check_int("audio_track_index", op.audio_track_index, problems, 1, policy.max_tracks)
        elif op.audio_track_index is not None:
            problems.append("audio_track_index is only used with media='video_audio'")
    elif isinstance(op, ops.SetTransform):
        _check_item(op, problems, policy)
        if isinstance(op.item, ops.ItemRef) and op.item.track_kind != "video":
            problems.append("transforms apply to video items only")
        if not op.values:
            problems.append("values is empty")
        for k, v in dict(op.values).items():
            if k in ops.BOOL_TRANSFORM_KEYS:
                if not isinstance(v, bool):
                    problems.append(f"{k} must be a bool")
            elif k in ops.TRANSFORM_KEYS:
                lo, hi = ops.TRANSFORM_KEYS[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                    problems.append(f"{k} must be a finite number")
                elif not lo <= float(v) <= hi:
                    problems.append(f"{k} {v} is outside [{lo}, {hi}]")
            else:
                problems.append(f"{k!r} is not a supported transform key")
    elif isinstance(op, ops.AddMarker):
        _check_int("frame", op.frame, problems, 0)
        _check_int("duration", op.duration, problems, 1)
        if op.color not in ops.MARKER_COLORS:
            problems.append(f"color {op.color!r} must be one of {ops.MARKER_COLORS}")
        for label, v in (("name", op.name), ("note", op.note)):
            if not isinstance(v, str) or len(v) > MAX_TEXT:
                problems.append(f"{label} must be a string of at most {MAX_TEXT} characters")
    elif isinstance(op, ops.PlaceTitle):
        if not isinstance(op.text, str) or not op.text.strip() or len(op.text) > MAX_TEXT:
            problems.append(f"text must be 1-{MAX_TEXT} characters")
        _check_int("track_index", op.track_index, problems, 1, policy.max_tracks)
        _check_int("record_frame", op.record_frame, problems, 0)
        _check_int("duration", op.duration, problems, 1)
        if op.size is not None and (isinstance(op.size, bool) or not isinstance(op.size, (int, float))
                                    or not 0 < float(op.size) <= 1):
            problems.append("size must be in (0, 1]")
        if op.center is not None:
            if (not isinstance(op.center, tuple) or len(op.center) != 2
                    or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1 for x in op.center)):
                problems.append("center must be a (x, y) tuple with values in [0, 1]")
    elif isinstance(op, (ops.SetClipAudioLevel, ops.AddTransition)):
        if op.item is not None:
            _check_item(op, problems, policy)
    elif isinstance(op, ops.DeleteItems):
        if not op.items or not isinstance(op.items, tuple):
            problems.append("items must be a non-empty tuple")
        for ref in op.items if isinstance(op.items, tuple) else ():
            if not isinstance(ref, ops.ItemRef):
                problems.append("items must be ItemRef values")
                continue
            _check_track("item", ref.track_kind, ref.track_index, problems, policy)
            _check_int("item start_frame", ref.start_frame, problems, 0)
        if isinstance(op.items, tuple) and len(set(op.items)) != len(op.items):
            problems.append("items contain duplicates")
    elif isinstance(op, ops.RenderTimeline):
        validate_render_spec(op.spec, problems, policy)
    elif isinstance(op, ops.VerifyExport):
        if not isinstance(op.path, str) or not os.path.isabs(op.path or ""):
            problems.append("path must be absolute")
        elif policy.allowed_output_roots and not _real_inside(op.path, policy.allowed_output_roots):
            problems.append("path is outside the allowed output roots")
    return problems


def check_rate(rate: str) -> Optional[str]:
    try:
        parse_rate(rate)
    except TimebaseError as exc:
        return str(exc)
    return None
