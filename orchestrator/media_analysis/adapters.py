"""Adapters from media_analysis records to the input shapes of `creator_dna` (read-only import).

creator_dna is the consumer and owns its shapes and validation; this module only fills them.
Contract (nothing is invented; anything missing upstream stays None/absent downstream):

  Phase 2 record                    -> Creator DNA input              DNA evidence method downstream
  --------------------------------------------------------------------------------------------------
  Transcript segment (ASR only)     -> TranscriptSegment unverified   ASR_UNVERIFIED (hypothesis layer)
  ReviewedSegment (human review)    -> TranscriptSegment reviewed     VERBATIM (+ reviewed_by)
  QuietGap / EnergyEvent / window   -> AudioWindow (metrics per       MEASURED
                                       creator_dna.metrics contract)
  ShotBoundary / VisualEvent        -> VisualObservation              AI_ESTIMATE (confidence may be None)

Every input carries an `Origin` (media_id + sha256 + path of the ORIGINAL, Phase 2 record id and kind,
engine, the analysis settings that give a measurement its meaning, the analysis copy used, and for
reviewed text the reviewer / review id / original ASR text). Times are on the original timeline.

What is handed over is decided by an explicit `DnaEligibility` policy; the default hands over no
unreviewed ASR. Handing records to DNA never creates a trait: traits come only from DNA analyzers and its
confidence model (one-off evidence stays "one_off"; a measurement or a scene boundary is not a trait).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .audio_analysis import AudioAnalysis
from .models import MediaAnalysisError, MediaRef
from .proxies import AnalysisCopyRegistry
from .review import ReviewedTranscript
from .scene_detection import ShotBoundary, VisualEvent
from .transcription import Transcript

CONTRACT_NOTES = [
    "Unreviewed ASR is never VERBATIM: it reaches DNA as an unverified segment (ASR_UNVERIFIED, hypothesis layer) "
    "and only if DnaEligibility.include_unverified_transcripts is set.",
    "Reviewed text carries the reviewer, review id and original ASR text; the ASR transcript file is untouched.",
    "Visual confidence is passed through as supplied; None stays None (no default).",
    "Audio metrics follow creator_dna.metrics; analysis parameters travel in Origin.settings.",
    "Origin always names the ORIGINAL media; an analysis copy is recorded separately and rejected as a source.",
    "Output of build_handoff() carries NO creator attribution and the Creator DNA store refuses it. Use "
    "speaker_attribution.build_attributed_handoff() (or pipeline.prepare_dna_handoff()), which keeps only records an "
    "eligibility decision found eligible as CREATOR evidence and stamps them.",
]


def _dna():
    try:
        import creator_dna
    except ImportError as exc:                       # pragma: no cover
        raise MediaAnalysisError("creator_dna package is not importable") from exc
    return creator_dna


@dataclass(frozen=True)
class DnaEligibility:
    """What may be handed to Creator DNA. Defaults are conservative."""
    include_unverified_transcripts: bool = False
    min_asr_confidence: float | None = None       # applies to unverified segments that report a confidence
    include_audio_gaps: bool = True
    include_energy_rises: bool = True
    include_level_windows: bool = False
    include_visual: bool = True
    include_source_paths: bool = True


@dataclass
class HandoffReport:
    included: list = field(default_factory=list)   # {"record_id","kind","status"}
    skipped: list = field(default_factory=list)    # {"record_id","reason"}

    def to_dict(self) -> dict:
        return {"included": self.included, "skipped": self.skipped}


class SourceResolver:
    """Maps media ids to ORIGINAL sources and builds `creator_dna.Origin`s.

    `refs` must come from the inventory (originals). If the analysis-copy registry is supplied, a
    copy id or a path inside the registry is refused as a source: a proxy is never an original."""

    def __init__(self, refs: Mapping[str, MediaRef], copies: AnalysisCopyRegistry | None = None,
                 source_id_fn: Callable[[str], str] = lambda m: m, include_paths: bool = True):
        self.refs, self.copies, self.source_id_fn, self.include_paths = dict(refs), copies, source_id_fn, include_paths
        if copies is not None:
            copy_ids = {r["copy_id"] for r in copies.records}
            copy_paths = {os.path.normcase(str(copies._output_path(r).resolve())) for r in copies.records}
            root = os.path.normcase(str(copies.directory.resolve()))
            for mid, ref in self.refs.items():
                p = os.path.normcase(str(Path(ref.path).resolve()))
                if mid in copy_ids or p in copy_paths or p.startswith(root + os.sep):
                    raise MediaAnalysisError(f"{mid}: this is an analysis copy, not an original; "
                                             "provenance must name the original media")

    def ref(self, media_id: str) -> MediaRef:
        if media_id not in self.refs:
            raise MediaAnalysisError(f"media {media_id} is not a known original (not in the inventory refs); "
                                     "cannot trace provenance")
        return self.refs[media_id]

    def origin(self, media_id: str, **kw):
        ref = self.ref(media_id)
        return _dna().Origin(media_id=media_id, source_sha256=ref.sha256,
                             source_path=ref.path if self.include_paths else None, **kw)

    def source_id(self, media_id: str) -> str:
        return self.source_id_fn(self.ref(media_id).media_id)


# ------------------------------------------------------------------ transcripts
def to_dna_transcript(source: Transcript | ReviewedTranscript, resolver: SourceResolver,
                      policy: DnaEligibility = DnaEligibility(), source_date: str | None = None) -> tuple[list, HandoffReport]:
    """-> (list of creator_dna.TranscriptSegment, report). Text is passed through untouched; reviewed
    status comes ONLY from a ReviewedTranscript built from a named human's TranscriptReview."""
    dna = _dna()
    rt = source if isinstance(source, ReviewedTranscript) else None
    transcript = rt.original if rt else source
    if rt is None:
        from .review import apply_review
        rt = apply_review(transcript, None)
    engine = f"{transcript.provenance.engine}@{transcript.provenance.version}"
    out, rep = [], HandoffReport()
    for rs in rt.segments:
        seg = rs.original
        ctx = [f"engine:{engine}"]
        if seg.variety:
            ctx.append(f"variety:{seg.variety}")
        if seg.language_spans:
            ctx.append("code_switching")
        if rs.reviewed:
            review = rt.review
            info = dna.ReviewInfo(review.reviewer, review.review_id, rs.status, seg.record_id,
                                  seg.text if rs.status == "corrected" else None)
            origin = resolver.origin(seg.media_id, record_id=seg.record_id, record_kind="transcription",
                                     engine=engine, analysis_copy_id=transcript.audio_copy_id, review=info)
            out.append(dna.TranscriptSegment(
                resolver.source_id(seg.media_id), seg.start_s, seg.end_s, rs.text, seg.language, seg.speaker_id,
                source_date, tuple(ctx + ["transcript:reviewed"]), None, "reviewed", review.reviewer, origin))
            rep.included.append({"record_id": seg.record_id, "kind": "transcript_segment", "status": rs.status})
            continue
        if not policy.include_unverified_transcripts:
            rep.skipped.append({"record_id": seg.record_id, "reason": "unreviewed ASR (policy excludes it)"})
            continue
        if policy.min_asr_confidence is not None and seg.confidence is not None \
                and seg.confidence < policy.min_asr_confidence:
            rep.skipped.append({"record_id": seg.record_id,
                                "reason": f"ASR confidence {seg.confidence} below {policy.min_asr_confidence}"})
            continue
        origin = resolver.origin(seg.media_id, record_id=seg.record_id, record_kind="transcription",
                                 engine=engine, analysis_copy_id=transcript.audio_copy_id)
        out.append(dna.TranscriptSegment(
            resolver.source_id(seg.media_id), seg.start_s, seg.end_s, seg.text, seg.language, seg.speaker_id,
            source_date, tuple(ctx + ["transcript:unverified"]), seg.confidence, "unverified", None, origin))
        rep.included.append({"record_id": seg.record_id, "kind": "transcript_segment", "status": "unverified"})
    return out, rep


# ------------------------------------------------------------------------ audio
def _audio_settings(a: AudioAnalysis) -> dict:
    keep = ("window_s", "min_gap_s", "long_gap_s", "baseline_s", "rise_db")
    s = {k: a.params[k] for k in keep if k in a.params}
    s["resolved_threshold_dbfs"] = a.resolved_threshold_dbfs
    return s


def to_dna_audio(a: AudioAnalysis, resolver: SourceResolver, policy: DnaEligibility = DnaEligibility(),
                 source_date: str | None = None) -> tuple[list, HandoffReport]:
    """Gaps, energy rises and (optionally) level windows as creator_dna.AudioWindow measurements."""
    dna = _dna()
    sid, engine, settings = resolver.source_id(a.media_id), f"{a.provenance.engine}@{a.provenance.version}", _audio_settings(a)

    def origin(record_id):
        return resolver.origin(a.media_id, record_id=record_id, record_kind="measurement", engine=engine,
                               settings=settings, analysis_copy_id=a.analysis_copy_id)
    out, rep = [], HandoffReport()
    if policy.include_audio_gaps:
        for g in a.gaps:
            out.append(dna.AudioWindow(sid, g.start_s, g.end_s, {"pause_duration_s": g.duration_s}, source_date,
                                       (f"gap:{g.position}", "measurement:quiet_run"), origin(g.record_id)))
            rep.included.append({"record_id": g.record_id, "kind": "audio_gap", "status": "measurement"})
    if policy.include_energy_rises:
        for e in a.energy_events:
            out.append(dna.AudioWindow(sid, e.start_s, e.end_s,
                                       {"energy_rise_db": e.delta_db, "level_dbfs": e.level_dbfs,
                                        "baseline_dbfs": e.baseline_dbfs}, source_date,
                                       ("measurement:energy_rise",), origin(e.record_id)))
            rep.included.append({"record_id": e.record_id, "kind": "energy_event", "status": "measurement"})
    if policy.include_level_windows:
        for w in a.windows:
            out.append(dna.AudioWindow(sid, w.start_s, w.end_s, {"rms_dbfs": w.rms_dbfs, "peak_dbfs": w.peak_dbfs},
                                       source_date, ("measurement:level",), origin(w.record_id)))
            rep.included.append({"record_id": w.record_id, "kind": "audio_window", "status": "measurement"})
    return sorted(out, key=lambda w: (w.start_s, w.end_s)), rep


# ----------------------------------------------------------------------- visual
def to_dna_visual(items: Iterable[ShotBoundary | VisualEvent], resolver: SourceResolver,
                  policy: DnaEligibility = DnaEligibility(), source_date: str | None = None) -> tuple[list, HandoffReport]:
    """Detections as creator_dna.VisualObservation. `estimate_confidence` is the detector's confidence,
    None when it supplied none - never a default. A boundary says the picture changed, nothing more."""
    dna = _dna()
    out, rep = [], HandoffReport()
    for it in items:
        if not policy.include_visual:
            rep.skipped.append({"record_id": it.record_id, "reason": "policy excludes visual detections"})
            continue
        sid = resolver.source_id(it.media_id)
        if isinstance(it, ShotBoundary):
            origin = resolver.origin(it.media_id, record_id=it.record_id, record_kind="detection", engine=it.method,
                                     analysis_copy_id=it.analysis_copy_id)
            desc = f"shot boundary by {it.method}" + (f", raw score {it.score:.3f}" if it.score is not None else "")
            out.append(dna.VisualObservation(sid, it.time_s, it.time_s, "shot_boundary", desc, it.confidence,
                                             source_date, ("detection:visual_change",), origin))
            kind = "shot_boundary"
        else:
            origin = resolver.origin(it.media_id, record_id=it.record_id, record_kind="detection", engine=it.method)
            out.append(dna.VisualObservation(sid, it.start_s, it.end_s, it.label,
                                             it.description or f"visual event by {it.method}", it.confidence,
                                             source_date, ("detection:visual_event",), origin))
            kind = "visual_event"
        rep.included.append({"record_id": it.record_id, "kind": kind, "status": "detection"})
    return out, rep


# ---------------------------------------------------------------------- bundle
@dataclass
class DnaHandoff:
    transcript_segments: list
    audio_windows: list
    visual_observations: list
    report: HandoffReport
    policy: DnaEligibility


def build_handoff(resolver: SourceResolver, policy: DnaEligibility = DnaEligibility(),
                  transcripts: Sequence[Transcript | ReviewedTranscript] = (),
                  audio: Sequence[AudioAnalysis] = (),
                  visual: Sequence[ShotBoundary | VisualEvent] = (),
                  source_date: str | None = None) -> DnaHandoff:
    """Everything eligible under `policy`, plus a report of what was included/skipped and why."""
    rep, segs, wins = HandoffReport(), [], []
    for t in transcripts:
        s, r = to_dna_transcript(t, resolver, policy, source_date)
        segs += s
        rep.included += r.included
        rep.skipped += r.skipped
    for a in audio:
        w, r = to_dna_audio(a, resolver, policy, source_date)
        wins += w
        rep.included += r.included
        rep.skipped += r.skipped
    vis, r = to_dna_visual(visual, resolver, policy, source_date)
    rep.included += r.included
    rep.skipped += r.skipped
    return DnaHandoff(segs, wins, vis, rep, policy)
