"""Capability gate and plan -> resolve_executor request mapping. Nothing here executes anything or contacts Resolve.

Every plan item that would need an executor operation is mapped to a request; the request's capability status comes
from `resolve_executor.capabilities`. Anything whose status is not executable (UNSUPPORTED or NOT_TESTED), and anything
that cannot be mapped at all, is a hard error: it is never dropped silently. Requests are also run through the
executor's own `validation.validate` (offline policy: media roots = the directories of the sources the plan uses,
output roots = the export allowlist, existence checks off) so a plan cannot pass here and then be refused for a
reason that was already knowable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from . import paths
from .frames import FrameRate, TimebaseError, half_open, start_frame, to_rate
from .issues import PlanIssue, ValidationReport, error
from .schema import EditPlan

try:
    from resolve_executor import capabilities as caps
    from resolve_executor import operations as ops
    from resolve_executor import validation as exval
    from resolve_executor.timebase import FrameRange
except ImportError:  # pragma: no cover - same fallback as frames.py
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from resolve_executor import capabilities as caps
    from resolve_executor import operations as ops
    from resolve_executor import validation as exval
    from resolve_executor.timebase import FrameRange

TRANSITION_NAMES = {"dissolve": "Cross Dissolve", "dip_to_black": "Dip To Color Dissolve"}


class PlanNotExecutable(ValueError):
    """Raised by to_executor_requests when the plan has errors. `report` carries every issue."""

    def __init__(self, report: ValidationReport):
        super().__init__(f"plan is not executable: {len(report.errors)} error(s): " + ", ".join(sorted(set(report.codes('error')))))
        self.report = report


@dataclass(frozen=True)
class MappedRequest:
    op: ops.Operation
    path: str            # location of the plan item the request comes from


def _index(plan: EditPlan, track_id: Any, kind: str) -> int:
    for t in plan.tracks:
        if t.track_id == track_id and t.kind == kind:
            return t.index
    raise KeyError(f"no {kind} track {track_id!r}")


def build_requests(plan: EditPlan) -> tuple[list[MappedRequest], list[PlanIssue]]:
    """Best-effort mapping. An item that cannot be mapped yields a CAP_UNMAPPABLE error, never silence."""
    reqs: list[MappedRequest] = []
    issues: list[PlanIssue] = []
    pid, tl = plan.meta.plan_id, plan.timeline.name

    def op_id(prefix: str, item_id: str) -> str:
        return f"{pid}:{prefix}:{item_id}"

    def add(op: ops.Operation, path: str) -> None:
        reqs.append(MappedRequest(op, path))

    def guarded(path: str, fn) -> None:
        try:
            fn()
        except (TimebaseError, KeyError, TypeError, ValueError) as exc:
            issues.append(error("CAP_UNMAPPABLE", path, f"cannot map to an executor request: {exc}"))

    try:
        rate: FrameRate = to_rate(plan.timeline.frame_rate)
        base = start_frame(plan.timeline.start_timecode, rate)
    except TimebaseError as exc:
        return reqs, [error("CAP_UNMAPPABLE", "/timeline", f"timeline cannot be mapped: {exc}")]

    def create():
        key = op_id("tl", "create")
        add(ops.CreateTimeline(key, name=tl, frame_rate=rate.resolve_text(), width=plan.timeline.width,
                               height=plan.timeline.height, start_timecode=plan.timeline.start_timecode,
                               idempotency_key=key), "/timeline")
    guarded("/timeline", create)

    # tracks: Resolve's new timeline has V1/A1; one AddTrack per further index (indices are filled, in order)
    for kind in ("video", "audio", "subtitle"):
        indices = [t.index for t in plan.tracks if t.kind == kind]
        if kind == "subtitle":
            for i, t in enumerate(plan.tracks):
                if t.kind == "subtitle":
                    issues.append(error("CAP_UNSUPPORTED", f"/tracks/{i}",
                                        "subtitle tracks cannot be created (capability add_track/add_subtitle is "
                                        "UNSUPPORTED on this Resolve edition)"))
            continue
        for n in range(2, (max(indices) if indices else 1) + 1):
            key = op_id("trk", f"{kind}{n}")
            add(ops.AddTrack(key, timeline=tl, track_kind=kind, audio_format="stereo", idempotency_key=key),
                f"/tracks[{kind}{n}]")

    used = []
    for s in plan.segments:
        src = next((x for x in plan.sources if x.source_id == s.source_id), None)
        if src is not None and src.path not in used:
            used.append(src.path)
    if used:
        key = op_id("imp", "media")
        add(ops.ImportMedia(key, paths=tuple(used), idempotency_key=key), "/sources")

    for i, s in sorted(enumerate(plan.segments), key=lambda x: (x[1].record_in, x[1].segment_id)):
        p = f"/segments/{i}"

        def seg(s=s, p=p):
            at = base + s.record_in
            if s.purpose == "title":
                key = op_id("ttl", s.segment_id)
                add(ops.PlaceTitle(key, timeline=tl, text=s.title_text or "", track_index=_index(plan, s.track_id, "video"),
                                   record_frame=at, duration=s.title_duration_frames or 0, idempotency_key=key), p)
                return
            src = next(x for x in plan.sources if x.source_id == s.source_id)
            first, end = half_open(s)
            video_idx = _index(plan, s.track_id, "audio" if s.media == "audio" else "video")
            audio_idx = _index(plan, s.audio_track_id, "audio") if s.media == "video_audio" else None
            key = op_id("seg", s.segment_id)
            add(ops.AppendSegment(key, timeline=tl, media_path=src.path, source=FrameRange(first, end),
                                  track_index=video_idx, record_frame=at, media=s.media,
                                  audio_track_index=audio_idx, idempotency_key=key), p)
            if s.audio_gain_db is not None:
                a_idx = audio_idx if audio_idx is not None else video_idx
                gk = op_id("gain", s.segment_id)
                add(ops.SetClipAudioLevel(gk, timeline=tl, item=ops.ItemRef("audio", a_idx, at),
                                          volume_db=float(s.audio_gain_db), idempotency_key=gk), f"{p}/audio_gain_db")
        guarded(p, seg)

    for i, c in enumerate(plan.captions):
        p = f"/captions/{i}"

        def cap(c=c, p=p):
            at = base + c.record_in
            key = op_id("cap", c.caption_id)
            if c.render == "burn_in":
                add(ops.PlaceTitle(key, timeline=tl, text=c.text, track_index=_index(plan, c.track_id, "video"),
                                   record_frame=at, duration=c.duration_frames, idempotency_key=key), p)
            else:
                add(ops.AddSubtitle(key, timeline=tl, text=c.text, record_frame=at, duration=c.duration_frames,
                                    idempotency_key=key), p)
        guarded(p, cap)

    for i, m in enumerate(plan.markers):
        def mark(m=m, i=i):
            key = op_id("mrk", m.marker_id)
            add(ops.AddMarker(key, timeline=tl, frame=m.frame, color=m.color, name=m.name, note=m.note,
                              duration=m.duration_frames, idempotency_key=key), f"/markers/{i}")
        guarded(f"/markers/{i}", mark)

    for i, t in enumerate(plan.transitions):
        p = f"/transitions/{i}"

        def trans(t=t, p=p):
            if t.kind == "cut":
                return                              # a cut is plain adjacency: no operation exists or is needed
            to = next(s for s in plan.segments if s.segment_id == t.to_segment_id)
            key = op_id("trn", t.transition_id)
            add(ops.AddTransition(key, timeline=tl, item=ops.ItemRef("video", _index(plan, to.track_id, "video"),
                                                                     base + to.record_in),
                                  transition=TRANSITION_NAMES[t.kind], duration=t.duration_frames,
                                  idempotency_key=key), p)
        guarded(p, trans)

    for i, x in enumerate(plan.exports):
        def render(x=x, i=i):
            key = op_id("rnd", x.target_id)
            spec = ops.RenderSpec(target_dir=x.directory, file_name=x.file_name, format=x.format, codec=x.codec,
                                  width=plan.timeline.width, height=plan.timeline.height)
            add(ops.RenderTimeline(key, timeline=tl, spec=spec, idempotency_key=key), f"/exports/{i}")
        guarded(f"/exports/{i}", render)
    return reqs, issues


def _policy(plan: EditPlan, requests: list[MappedRequest], allowlist: Iterable[str]) -> exval.Policy:
    roots: list[str] = []
    for r in requests:
        if isinstance(r.op, ops.ImportMedia):
            for p in r.op.paths:
                d = paths.dirname(p)
                if d and d not in roots:
                    roots.append(d)
    return exval.Policy(allowed_media_roots=tuple(roots), allowed_output_roots=tuple(allowlist),
                        file_exists=lambda _p: True, dir_exists=lambda _p: True)


def check_capabilities(plan: EditPlan, *, export_allowlist: Iterable[str] = ()) -> list[PlanIssue]:
    """Status gate plus the executor's own request validation. Never raises on bad plan data."""
    try:
        reqs, issues = build_requests(plan)
        policy = _policy(plan, reqs, export_allowlist)
        for r in reqs:
            cap = caps.capability_for(r.op.KIND)
            if cap.status not in caps.EXECUTABLE:
                issues.append(error("CAP_UNSUPPORTED", r.path,
                                    f"needs executor operation {r.op.KIND!r}, capability status {cap.status}: "
                                    f"{cap.limitations[:160]}"))
                continue
            for problem in exval.validate(r.op, policy):
                issues.append(error("CAP_REQUEST_INVALID", r.path, f"{r.op.KIND}: {problem}"))
        return issues
    except Exception as exc:  # noqa: BLE001
        return [error("CAP_UNMAPPABLE", "/", f"capability check failed: {type(exc).__name__}: {exc}")]


def to_executor_requests(plan: EditPlan, *, export_allowlist: Iterable[str] = ()) -> tuple[ops.Operation, ...]:
    """Build the executor requests for a VALID plan, in execution order. Executes nothing.

    Raises PlanNotExecutable (carrying the full report) if the plan has any error, including any UNSUPPORTED item.
    """
    from .validate import validate_plan
    report = validate_plan(plan, export_allowlist=tuple(export_allowlist))
    if not report.ok:
        raise PlanNotExecutable(report)
    reqs, _ = build_requests(plan)
    return tuple(r.op for r in reqs)
