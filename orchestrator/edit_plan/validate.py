"""Plan validation: structure, exact frame arithmetic, per-track overlap, gaps, fidelity and capability gate.

`validate_plan` and `validate_plan_dict` never raise on bad user data: every problem becomes an issue with a code.
Timeline positions (`record_in`) are relative to the timeline start; ranges below are half-open `[start, end)`.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from . import fidelity, frames
from ._codec import PlanSchemaError
from .canonical import plan_hash
from .issues import PlanIssue, ValidationReport, error, warning
from .schema import (MARKER_COLORS, MEDIA_PARTS, PURPOSES, RANGE_CONVENTIONS, TRACK_KINDS, EditPlan)

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_DIMENSION = 8192

Transcript = Mapping[str, Sequence[Mapping[str, Any]]]


def validate_plan_dict(data: Any, *, export_allowlist: Iterable[str] = (),
                       transcript: Transcript | None = None) -> ValidationReport:
    """Load strictly, then validate. A schema failure becomes one error issue (code SCHEMA_*)."""
    try:
        plan = EditPlan.from_dict(data)
    except PlanSchemaError as exc:
        return ValidationReport(issues=(error(exc.code, exc.path or "/", exc.message),))
    return validate_plan(plan, export_allowlist=export_allowlist, transcript=transcript)


def validate_plan(plan: EditPlan, *, export_allowlist: Iterable[str] = (),
                  transcript: Transcript | None = None) -> ValidationReport:
    """Full offline validation. `export_allowlist`: directories exports may go into (empty = refuse all)."""
    allow = tuple(export_allowlist)
    try:
        issues, total, gaps = _Validator(plan, transcript).run()
        issues += fidelity.check_dialogue(plan)
        issues += fidelity.check_captions(plan, transcript)
        issues += fidelity.check_exports(plan, allow)
        from . import capability   # local import: capability builds on this module's helpers
        issues += capability.check_capabilities(plan, export_allowlist=allow)
        issues += _check_qc(plan, total, gaps)
        try:
            digest = plan_hash(plan)
        except (ValueError, TypeError):
            digest = None
        return ValidationReport(issues=tuple(issues), total_duration_frames=total, gaps=tuple(gaps), plan_hash=digest)
    except Exception as exc:  # noqa: BLE001 - the contract is "never raise on bad data"
        return ValidationReport(issues=(error("VALIDATION_INTERNAL_ERROR", "/", f"{type(exc).__name__}: {exc}"),))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class _Validator:
    def __init__(self, plan: EditPlan, transcript: Transcript | None):
        self.plan = plan
        self.transcript = transcript
        self.out: list[PlanIssue] = []
        self.tl: frames.FrameRate | None = None
        self.tracks = {t.track_id: t for t in plan.tracks}
        self.sources = {s.source_id: s for s in plan.sources}
        self.src_rate: dict[str, frames.FrameRate] = {}
        self.occ: dict[str, list[tuple[int, int, str]]] = {}      # track_id -> (start, end, item path)
        self.seg_span: dict[str, tuple[str, int, int]] = {}       # segment_id -> (video track id, start, end)

    def e(self, code: str, path: str, msg: str) -> None:
        self.out.append(error(code, path, msg))

    def w(self, code: str, path: str, msg: str) -> None:
        self.out.append(warning(code, path, msg))

    # ------------------------------------------------------------------
    def run(self):
        self.meta()
        self.ids()
        self.timeline()
        self.source_inventory()
        self.track_list()
        if not self.plan.segments:
            self.e("EMPTY_PLAN", "/segments", "the plan has no segments")
        for i, s in enumerate(self.plan.segments):
            self.segment(i, s)
        for i, c in enumerate(self.plan.captions):
            self.caption(i, c)
        total, gaps = self.occupancy()
        for i, t in enumerate(self.plan.transitions):
            self.transition(i, t)
        for i, m in enumerate(self.plan.markers):
            self.marker(i, m, total)
        return self.out, total, gaps

    # ------------------------------------------------------------------
    def rationale(self, path: str, r) -> None:
        if r is None or not isinstance(r.text, str) or not r.text.strip():
            self.e("RATIONALE_MISSING", f"{path}/rationale", "every item needs a rationale")
        elif self.plan.meta.source_brief_id and not r.brief_item_id:
            self.w("RATIONALE_NOT_LINKED", f"{path}/rationale",
                   "the plan names a source brief but this item does not link to a brief item")

    def meta(self) -> None:
        m = self.plan.meta
        for name in ("project_slug", "plan_id", "created_by"):
            v = getattr(m, name)
            if not isinstance(v, str) or not v.strip():
                self.e("META_INVALID", f"/meta/{name}", f"{name} must be a non-empty string")
        if m.source_brief_hash is not None and not _SHA256.match(str(m.source_brief_hash)):
            self.e("META_INVALID", "/meta/source_brief_hash", "must be 64 lower-case hex characters")

    def ids(self) -> None:
        p = self.plan
        groups = (("sources", [s.source_id for s in p.sources]), ("tracks", [t.track_id for t in p.tracks]),
                  ("segments", [s.segment_id for s in p.segments]),
                  ("transitions", [t.transition_id for t in p.transitions]),
                  ("markers", [m.marker_id for m in p.markers]), ("captions", [c.caption_id for c in p.captions]),
                  ("qc_expectations", [q.qc_id for q in p.qc_expectations]),
                  ("exports", [x.target_id for x in p.exports]))
        if not isinstance(p.meta.plan_id, str) or not ID_PATTERN.match(p.meta.plan_id):
            self.e("ID_INVALID", "/meta/plan_id", "plan_id must match " + ID_PATTERN.pattern)
        for name, ids in groups:
            seen: set[str] = set()
            for i, v in enumerate(ids):
                if not isinstance(v, str) or not ID_PATTERN.match(v):
                    self.e("ID_INVALID", f"/{name}/{i}", f"id {v!r} must match {ID_PATTERN.pattern}")
                if v in seen:
                    self.e("DUPLICATE_ID", f"/{name}/{i}", f"id {v!r} is used twice in {name}")
                seen.add(v)

    def timeline(self) -> None:
        t = self.plan.timeline
        try:
            self.tl = frames.to_rate(t.frame_rate)
        except frames.TimebaseError as exc:
            self.e("TIMELINE_RATE_INVALID", "/timeline/frame_rate", str(exc))
        for name in ("width", "height"):
            v = getattr(t, name)
            if not _is_int(v) or not 16 <= v <= MAX_DIMENSION or v % 2:
                self.e("TIMELINE_SIZE_INVALID", f"/timeline/{name}", f"{name} {v!r} must be an even int in [16, {MAX_DIMENSION}]")
        if self.tl is not None:
            try:
                frames.start_frame(t.start_timecode, self.tl)
            except frames.TimebaseError as exc:
                self.e("START_TIMECODE_INVALID", "/timeline/start_timecode", str(exc))

    def source_inventory(self) -> None:
        from . import paths
        for i, s in enumerate(self.plan.sources):
            p = f"/sources/{i}"
            try:
                self.src_rate[s.source_id] = frames.to_rate(s.frame_rate)
            except frames.TimebaseError as exc:
                self.e("SOURCE_RATE_INVALID", f"{p}/frame_rate", str(exc))
            if not _is_int(s.duration_frames) or s.duration_frames < 1:
                self.e("SOURCE_DURATION_INVALID", f"{p}/duration_frames", "duration must be an int >= 1 frame")
            if not _SHA256.match(str(s.content_hash)):
                self.e("SOURCE_HASH_INVALID", f"{p}/content_hash", "content_hash must be 64 lower-case hex characters")
            if paths.parts_of(s.path) is None:
                self.e("SOURCE_PATH_INVALID", f"{p}/path", "the source path must be an absolute plain Windows path")
            if not (s.has_video or s.has_audio):
                self.e("SOURCE_NO_MEDIA", p, "a source must have video, audio or both")

    def track_list(self) -> None:
        seen: set[tuple[str, int]] = set()
        for i, t in enumerate(self.plan.tracks):
            p = f"/tracks/{i}"
            if t.kind not in TRACK_KINDS:
                self.e("TRACK_KIND_INVALID", f"{p}/kind", f"{t.kind!r} is not one of {TRACK_KINDS}")
            if not _is_int(t.index) or t.index < 1:
                self.e("TRACK_INDEX_INVALID", f"{p}/index", "track index is 1-based")
            if (t.kind, t.index) in seen:
                self.e("TRACK_INDEX_DUPLICATE", p, f"two {t.kind} tracks share index {t.index}")
            seen.add((t.kind, t.index))

    # ------------------------------------------------------------------
    def put(self, track_id: str, start: int, end: int, path: str) -> None:
        self.occ.setdefault(track_id, []).append((start, end, path))

    def track_of_kind(self, track_id, kind: str, path: str) -> bool:
        t = self.tracks.get(track_id)
        if t is None:
            self.e("UNKNOWN_TRACK", path, f"track {track_id!r} is not declared")
            return False
        if t.kind != kind:
            self.e("TRACK_KIND_MISMATCH", path, f"track {track_id!r} is {t.kind}, {kind} is required here")
            return False
        return True

    def segment(self, i: int, s) -> None:
        p = f"/segments/{i}"
        self.rationale(p, s.rationale)
        if s.purpose not in PURPOSES:
            self.e("PURPOSE_INVALID", f"{p}/purpose", f"{s.purpose!r} is not one of {PURPOSES}")
        if not _is_int(s.record_in) or s.record_in < 0:
            self.e("RECORD_NEGATIVE", f"{p}/record_in", f"record_in {s.record_in!r} must be an int >= 0")
            return
        if s.audio_gain_db is not None:
            pass  # reported by the capability gate (set_clip_audio_level is UNSUPPORTED)
        if s.purpose == "title":
            self.title(p, s)
            return
        if s.media not in MEDIA_PARTS:
            self.e("MEDIA_INVALID", f"{p}/media", f"{s.media!r} is not one of {MEDIA_PARTS}")
            return
        if s.title_text is not None or s.title_duration_frames is not None:
            self.e("TITLE_FIELDS_ON_NON_TITLE", p, "title_text/title_duration_frames belong to purpose 'title' only")
        if s.purpose in ("music", "sfx") and s.media != "audio":
            self.e("PURPOSE_MEDIA_MISMATCH", f"{p}/media", f"{s.purpose} segments are audio-only")
        wants_video = s.media in ("video", "video_audio")
        wants_audio = s.media in ("audio", "video_audio")
        ok_track = self.track_of_kind(s.track_id, "video" if wants_video else "audio", f"{p}/track_id")
        if s.media == "video_audio":
            if not s.audio_track_id:
                self.e("AUDIO_TRACK_MISSING", f"{p}/audio_track_id", "media 'video_audio' needs an audio_track_id")
            else:
                ok_audio = self.track_of_kind(s.audio_track_id, "audio", f"{p}/audio_track_id")
                ok_track = ok_track and ok_audio
        elif s.audio_track_id is not None:
            self.e("AUDIO_TRACK_UNEXPECTED", f"{p}/audio_track_id", "audio_track_id is only used with 'video_audio'")
        src = self.sources.get(s.source_id) if s.source_id is not None else None
        if src is None:
            self.e("UNKNOWN_SOURCE", f"{p}/source_id", f"source {s.source_id!r} is not in the inventory")
            return
        if wants_video and not src.has_video:
            self.e("SOURCE_LACKS_MEDIA", f"{p}/media", f"source {src.source_id!r} has no video")
        if wants_audio and not src.has_audio:
            self.e("SOURCE_LACKS_MEDIA", f"{p}/media", f"source {src.source_id!r} has no audio")
        length = self.source_range(p, s, src)
        if length is None or self.tl is None or src.source_id not in self.src_rate or not ok_track:
            return
        srate = self.src_rate[src.source_id]
        dur = frames.timeline_duration(length, srate, self.tl)
        if srate != self.tl:
            self.w("RATE_MIXED", p, f"source rate {srate} differs from timeline rate {self.tl}; "
                                    f"{length} source frames become {dur} timeline frames")
        if not frames.is_exact(length, srate, self.tl):
            self.w("RATE_CONVERSION_INEXACT", p, f"{length} source frames at {srate} fps are not a whole number of "
                                                 f"{self.tl} fps frames; rounded down to {dur}")
        if dur < 1:
            self.e("ZERO_DURATION", p, f"{length} source frame(s) at {srate} fps occupy 0 frames at {self.tl} fps")
            return
        end = s.record_in + dur
        if wants_video:
            self.put(s.track_id, s.record_in, end, p)
            self.seg_span[s.segment_id] = (s.track_id, s.record_in, end)
        if s.media == "audio":
            self.put(s.track_id, s.record_in, end, p)
            self.seg_span[s.segment_id] = (s.track_id, s.record_in, end)
        elif s.media == "video_audio" and s.audio_track_id:
            self.put(s.audio_track_id, s.record_in, end, p)

    def source_range(self, p: str, s, src) -> int | None:
        """Checks the source range; returns its length in source frames, or None if unusable."""
        if s.range_convention not in RANGE_CONVENTIONS:
            self.e("RANGE_CONVENTION_MISSING", f"{p}/range_convention",
                   f"state one of {RANGE_CONVENTIONS}; the end frame is never guessed")
            return None
        if not _is_int(s.source_in) or not _is_int(s.source_out):
            self.e("SEGMENT_MISSING_RANGE", p, "source_in and source_out must be ints")
            return None
        if s.source_in < 0 or s.source_out < 0:
            self.e("RANGE_NEGATIVE", p, f"negative source frame in [{s.source_in}, {s.source_out}]")
            return None
        first, end = frames.half_open(s)
        if end < first or (s.range_convention == "inclusive_out" and s.source_out < s.source_in):
            self.e("RANGE_INVERTED", p, f"source_out {s.source_out} is before source_in {s.source_in}")
            return None
        if end == first:
            self.e("RANGE_EMPTY", p, "an exclusive range with source_out == source_in has no frames")
            return None
        if end > src.duration_frames:
            self.e("RANGE_OUT_OF_SOURCE", p, f"last frame {end - 1} is beyond the source's {src.duration_frames} "
                                             f"frames (frames 0..{src.duration_frames - 1})")
            return None
        return end - first

    def title(self, p: str, s) -> None:
        if s.source_id is not None or s.source_in is not None or s.source_out is not None:
            self.e("TITLE_HAS_SOURCE", p, "a title has no source range")
        if not isinstance(s.title_text, str) or not s.title_text.strip():
            self.e("TITLE_FIELDS_INVALID", f"{p}/title_text", "a title needs text")
        if not _is_int(s.title_duration_frames) or s.title_duration_frames < 1:
            self.e("TITLE_FIELDS_INVALID", f"{p}/title_duration_frames", "a title needs a duration >= 1 frame")
            return
        if self.track_of_kind(s.track_id, "video", f"{p}/track_id"):
            self.put(s.track_id, s.record_in, s.record_in + s.title_duration_frames, p)

    def caption(self, i: int, c) -> None:
        p = f"/captions/{i}"
        self.rationale(p, c.rationale)
        if not _is_int(c.record_in) or c.record_in < 0:
            self.e("RECORD_NEGATIVE", f"{p}/record_in", f"record_in {c.record_in!r} must be an int >= 0")
            return
        if not _is_int(c.duration_frames) or c.duration_frames < 1:
            self.e("CAPTION_DURATION_INVALID", f"{p}/duration_frames", "caption duration must be >= 1 frame")
            return
        kind = "subtitle" if c.render == "subtitle_track" else "video"
        if self.track_of_kind(c.track_id, kind, f"{p}/track_id"):
            self.put(c.track_id, c.record_in, c.record_in + c.duration_frames, p)

    def occupancy(self):
        total, gaps = 0, []
        for tid in sorted(self.occ):
            items = sorted(self.occ[tid])
            track = self.tracks.get(tid)
            base = track is not None and track.index == 1 and track.kind in ("video", "audio")
            prev_end = 0
            for n, (a, b, path) in enumerate(items):
                if n and a < prev_end:
                    self.e("TRACK_OVERLAP", path, f"overlaps the previous item on track {tid!r} "
                                                  f"([{a}, {b}) starts before frame {prev_end})")
                elif a > prev_end and (n or base):
                    gaps.append({"track_id": tid, "start": prev_end, "end": a})
                    if base:
                        self.w("GAP", path, f"gap of {a - prev_end} frame(s) on base track {tid!r} "
                                            f"([{prev_end}, {a}))")
                prev_end = max(prev_end, b)
            total = max(total, prev_end)
        return total, gaps

    def transition(self, i: int, t) -> None:
        p = f"/transitions/{i}"
        self.rationale(p, t.rationale)
        a, b = self.seg_span.get(t.from_segment_id), self.seg_span.get(t.to_segment_id)
        for sid, span, name in ((t.from_segment_id, a, "from_segment_id"), (t.to_segment_id, b, "to_segment_id")):
            if span is None:
                self.e("TRANSITION_UNKNOWN_SEGMENT", f"{p}/{name}", f"segment {sid!r} is unknown or has no valid placement")
        if a is None or b is None:
            return
        if a[0] != b[0] or a[2] != b[1]:
            self.e("TRANSITION_NOT_ADJACENT", p, "a transition joins two consecutive segments on the same track")
        if not _is_int(t.duration_frames):
            self.e("TRANSITION_DURATION_INVALID", f"{p}/duration_frames", "duration must be an int")
        elif t.kind == "cut":
            if t.duration_frames != 0:
                self.e("TRANSITION_DURATION_INVALID", f"{p}/duration_frames", "a cut has duration 0")
        elif t.duration_frames < 1 or t.duration_frames > min(a[2] - a[1], b[2] - b[1]):
            self.e("TRANSITION_DURATION_INVALID", f"{p}/duration_frames",
                   f"duration {t.duration_frames} must be 1..{min(a[2] - a[1], b[2] - b[1])} (the shorter segment)")

    def marker(self, i: int, m, total: int) -> None:
        p = f"/markers/{i}"
        self.rationale(p, m.rationale)
        if m.color not in MARKER_COLORS:
            self.e("MARKER_COLOR_INVALID", f"{p}/color", f"{m.color!r} is not a Resolve marker colour")
        if not _is_int(m.duration_frames) or m.duration_frames < 1:
            self.e("MARKER_DURATION_INVALID", f"{p}/duration_frames", "marker duration must be >= 1 frame")
        if not _is_int(m.frame) or m.frame < 0 or m.frame >= total:
            self.e("MARKER_OUT_OF_RANGE", f"{p}/frame", f"marker frame {m.frame!r} is outside the timeline [0, {total})")
        for j, other in enumerate(self.plan.markers[:i]):
            if other.frame == m.frame:
                self.e("MARKER_DUPLICATE_FRAME", f"{p}/frame", f"markers {j} and {i} share frame {m.frame} "
                                                              f"(Resolve keeps one marker per frame)")
                break


def _check_qc(plan: EditPlan, total: int, gaps: list[dict]) -> list[PlanIssue]:
    out: list[PlanIssue] = []
    seen: set[str] = set()
    for i, q in enumerate(plan.qc_expectations):
        p = f"/qc_expectations/{i}"
        exp = q.expected if isinstance(q.expected, dict) else {}
        shape = {"total_duration_frames": ("frames",), "frame_rate": ("num", "den"),
                 "resolution": ("width", "height")}.get(q.kind)
        if shape is not None and not all(_is_int(exp.get(k)) for k in shape):
            out.append(error("QC_EXPECTATION_INVALID", p, f"{q.kind} needs integer {list(shape)} in 'expected'"))
            continue
        if q.qc_id in seen:
            out.append(error("QC_DUPLICATE_ID", p, f"duplicate qc id {q.qc_id!r}"))
        seen.add(q.qc_id)
        t = plan.timeline
        unmet = None
        if q.kind == "total_duration_frames" and exp["frames"] != total:
            unmet = f"expected {exp['frames']} frames, the plan totals {total}"
        elif q.kind == "frame_rate" and (exp["num"] * t.frame_rate.den != exp["den"] * t.frame_rate.num):
            unmet = f"expected {exp['num']}/{exp['den']}, the timeline is {t.frame_rate.num}/{t.frame_rate.den}"
        elif q.kind == "resolution" and (exp["width"], exp["height"]) != (t.width, t.height):
            unmet = f"expected {exp['width']}x{exp['height']}, the timeline is {t.width}x{t.height}"
        elif q.kind == "no_gaps_on_base_tracks" and any(g["track_id"] for g in gaps if _base(plan, g["track_id"])):
            unmet = "the plan has gaps on a base track"
        if unmet:
            out.append(error("QC_EXPECTATION_UNMET", p, unmet))
    return out


def _base(plan: EditPlan, track_id: str) -> bool:
    return any(t.track_id == track_id and t.index == 1 and t.kind in ("video", "audio") for t in plan.tracks)
