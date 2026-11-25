"""Offline Editor planning workflow: Creative Director brief + source inventory -> CANDIDATE edit plan + report.

This is deliberately simple and deterministic. It places the ranges of the brief's recommended arc (or a named arc),
in the arc's own section order, one after another on V1/A1, with a marker per section. It makes no creative
decisions: no trimming for pacing, no B-roll, no music, no transitions beyond plain cuts. The result is a candidate
for human/QC review, never a finished edit, and nothing here is VERIFIED.

Inputs
* `brief`: a creative_director StoryBrief, or its `to_dict()` form.
* `inventory`: {"sources": [{"source_id", "path", "frame_rate": {"num","den"}, "duration_frames", "content_hash",
  "origin", "has_video", "has_audio"}], "timeline": {"name", "frame_rate": {"num","den"}, "width", "height",
  "start_timecode"} (optional; default: the first video source's rate, 1920x1080)}. Synthetic data only.
* `transcript` (optional): source_id -> [{"start_frame", "end_frame", "text"}] in source frames, half-open. With
  `captions=True` each overlapping entry becomes a burn-in caption that quotes it verbatim.

Source seconds in the brief become frames exactly (Fraction of the decimal text): start rounds down, end rounds up,
so a whole transcript segment is never cut short; each rounding is recorded in `notes`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping, Sequence

from . import frames
from .issues import ValidationReport, error
from .schema import (SCHEMA_VERSION, Caption, EditPlan, ExportTarget, Marker, PlanMeta, QCExpectation, Rate, Rationale,
                     Segment, SourceEntry, TimelineSettings, Track, TranscriptRange)
from .validate import validate_plan

PLANNER_ID = "editor_planner/offline-0.1"


@dataclass(frozen=True)
class PlanningResult:
    plan: EditPlan | None
    report: ValidationReport
    notes: tuple[str, ...] = ()
    candidate_notice: str = ("CANDIDATE plan for human/QC review. Produced by a deterministic offline planner that "
                             "orders the brief's chosen ranges; it is not a creative engine and not VERIFIED.")


def _g(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    return obj


def brief_hash(brief: Any) -> str:
    return hashlib.sha256(json.dumps(_plain(brief), sort_keys=True, separators=(",", ":"), default=str,
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def _rate(d: Any) -> Rate:
    return Rate(int(_g(d, "num")), int(_g(d, "den", 1)))


def _source_entries(inventory: Mapping[str, Any]) -> tuple[SourceEntry, ...]:
    return tuple(SourceEntry(source_id=s["source_id"], path=s["path"], frame_rate=_rate(s["frame_rate"]),
                             duration_frames=s["duration_frames"], content_hash=s["content_hash"],
                             origin=s.get("origin", "creator_recording"), has_video=s.get("has_video", True),
                             has_audio=s.get("has_audio", True)) for s in inventory.get("sources", ()))


def _seconds_to_frame(seconds: Any, rate: frames.FrameRate, rounding: str) -> int:
    return frames.seconds_to_frames(Fraction(repr(float(seconds))) if not isinstance(seconds, str) else Fraction(seconds),
                                    rate, rounding)


def _pick_arc(brief: Any, arc_id: str | None):
    wanted = arc_id or _g(brief, "recommended_arc_id")
    for a in _g(brief, "arcs", ()) or ():
        if _g(a, "arc_id") == wanted:
            return a
    return None


def plan_from_brief(brief: Any, inventory: Mapping[str, Any], transcript: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
                    *, project_slug: str | None = None, plan_id: str = "plan-1", arc_id: str | None = None,
                    captions: bool = False, export_allowlist: Sequence[str] = ()) -> PlanningResult:
    """Build a candidate plan and its validation report. Never raises on bad brief/inventory content."""
    try:
        return _plan(brief, inventory, transcript, project_slug, plan_id, arc_id, captions, tuple(export_allowlist))
    except (KeyError, TypeError, ValueError, AttributeError, frames.TimebaseError) as exc:
        rep = ValidationReport(issues=(error("PLANNER_INPUT_INVALID", "/", f"{type(exc).__name__}: {exc}"),))
        return PlanningResult(None, rep)


def _refuse(code: str, message: str) -> PlanningResult:
    return PlanningResult(None, ValidationReport(issues=(error(code, "/", message),)))


def _plan(brief, inventory, transcript, project_slug, plan_id, arc_id, with_captions, allowlist) -> PlanningResult:
    if _g(brief, "status") != "ok":
        return _refuse("PLANNER_BRIEF_UNUSABLE", f"the brief status is {_g(brief, 'status')!r}, not 'ok'")
    arc = _pick_arc(brief, arc_id)
    if arc is None:
        return _refuse("PLANNER_NO_ARC", "the brief has no recommended arc (and none was named)")
    sources = _source_entries(inventory)
    by_id = {s.source_id: s for s in sources}
    moments = {_g(m, "moment_id"): m for m in _g(brief, "moments", ()) or ()}
    tl_in = inventory.get("timeline") or {}
    first_video = next((s for s in sources if s.has_video), sources[0] if sources else None)
    if first_video is None:
        return _refuse("PLANNER_NO_SOURCES", "the inventory has no sources")
    tl_rate = _rate(tl_in["frame_rate"]) if "frame_rate" in tl_in else first_video.frame_rate
    timeline = TimelineSettings(name=tl_in.get("name", plan_id), frame_rate=tl_rate, width=tl_in.get("width", 1920),
                                height=tl_in.get("height", 1080),
                                start_timecode=tl_in.get("start_timecode", "01:00:00:00"))
    tl = frames.to_rate(tl_rate)
    brief_id = _g(brief, "brief_id")
    notes: list[str] = []
    segs: list[Segment] = []
    marks: list[Marker] = []
    caps: list[Caption] = []
    cursor = 0
    n = 0
    for section in _g(arc, "sections", ()) or ():
        sid = _g(section, "section_id")
        section_start = cursor
        for use in _g(section, "ranges", ()) or ():
            ref = _g(use, "ref")
            src = by_id.get(_g(ref, "source_id"))
            if src is None:
                return _refuse("PLANNER_UNKNOWN_SOURCE", f"section {sid!r} uses source {_g(ref, 'source_id')!r}, "
                                                          f"which is not in the inventory")
            srate = frames.to_rate(src.frame_rate)
            a = _seconds_to_frame(_g(ref, "start_s"), srate, "floor")
            b = _seconds_to_frame(_g(ref, "end_s"), srate, "ceil")
            if b > src.duration_frames:
                notes.append(f"{sid}: end clamped from frame {b} to the source's {src.duration_frames} frames")
                b = src.duration_frames
            if not (Fraction(repr(float(_g(ref, "start_s")))) * srate.value).denominator == 1 or \
                    not (Fraction(repr(float(_g(ref, "end_s")))) * srate.value).denominator == 1:
                notes.append(f"{sid}: range {_g(ref, 'start_s')}-{_g(ref, 'end_s')} s widened to whole frames [{a}, {b})")
            speakers = {_g(moments[m], "attribution") for m in _g(section, "moment_ids", ()) if m in moments
                        and _g(_g(moments[m], "ref"), "source_id") == src.source_id}
            speaker = "creator" if speakers == {"creator"} else "unknown"
            n += 1
            seg = Segment(segment_id=f"seg{n}", purpose="dialogue", track_id="v1", record_in=cursor,
                          rationale=Rationale(f"Arc {_g(arc, 'arc_id')!r} section {sid!r}: "
                                              f"{_g(section, 'position_rationale', '') or _g(section, 'purpose', '')}",
                                              brief_item_id=sid),
                          source_id=src.source_id, source_in=a, source_out=b, range_convention="exclusive_out",
                          media="video_audio", audio_track_id="a1", speaker=speaker)
            segs.append(seg)
            dur = frames.timeline_duration(b - a, srate, tl)
            if with_captions and transcript and src.source_id in transcript:
                for k, entry in enumerate(transcript[src.source_id]):
                    lo, hi = max(entry["start_frame"], a), min(entry["end_frame"], b)
                    if lo >= hi or not str(entry.get("text", "")).strip():
                        continue
                    rec = cursor + frames.timeline_duration(lo - a, srate, tl)
                    d = frames.timeline_duration(hi - a, srate, tl) - (rec - cursor)
                    if d < 1:
                        continue
                    caps.append(Caption(caption_id=f"cap{len(caps) + 1}", text=entry["text"], track_id="v2",
                                        record_in=rec, duration_frames=d,
                                        rationale=Rationale(f"verbatim caption of transcript entry {k} of {src.source_id}",
                                                            brief_item_id=sid),
                                        transcript_ref=TranscriptRange(src.source_id, entry["start_frame"], entry["end_frame"])))
            cursor += dur
        if cursor > section_start and len(marks) < 10_000:
            marks.append(Marker(marker_id=f"mk{len(marks) + 1}", frame=section_start, name=str(_g(section, "title", sid))[:60],
                                rationale=Rationale(f"start of section {sid!r}", brief_item_id=sid)))
    if not segs:
        return _refuse("PLANNER_NOTHING_TO_PLACE", "the chosen arc has no ranges")

    tracks = [Track("v1", "video", 1), Track("a1", "audio", 1)]
    if caps:
        tracks.append(Track("v2", "video", 2))
    qc = [QCExpectation("qc-duration", "total_duration_frames", "planned total length", {"frames": cursor}),
          QCExpectation("qc-rate", "frame_rate", "timeline frame rate", {"num": tl_rate.num, "den": tl_rate.den}),
          QCExpectation("qc-res", "resolution", "timeline resolution",
                        {"width": timeline.width, "height": timeline.height}),
          QCExpectation("qc-gaps", "no_gaps_on_base_tracks", "no gaps on V1/A1"),
          QCExpectation("qc-src", "sources_unmodified", "originals unchanged (hash compare)")]
    if caps:
        qc.append(QCExpectation("qc-captions", "captions_match_transcript", "every caption quotes its transcript range"))
    plan = EditPlan(version=SCHEMA_VERSION,
                    meta=PlanMeta(project_slug=project_slug or _g(brief, "project_id") or "unassigned", plan_id=plan_id,
                                  created_by=PLANNER_ID, source_brief_id=brief_id, source_brief_hash=brief_hash(brief)),
                    timeline=timeline, sources=sources, tracks=tuple(tracks), segments=tuple(segs),
                    markers=tuple(marks), captions=tuple(caps), qc_expectations=tuple(qc))
    report = validate_plan(plan, export_allowlist=allowlist, transcript=transcript)
    return PlanningResult(plan, report, tuple(notes))
