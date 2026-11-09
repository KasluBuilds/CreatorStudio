"""Pipeline step between media analysis and Creator DNA:

    media analysis -> attribution evidence -> ELIGIBILITY DECISION -> (human review where required) -> DNA handoff

Each step is independently inspectable: the evidence and decisions live in the `AttributionStore`; this module
only (1) turns media_analysis records into decision candidates, (2) evaluates them, (3) reuses the existing
`media_analysis` adapters to build the Creator DNA inputs and stamps ONLY eligible ones with an
`EvidenceAttribution`, and (4) revokes DNA evidence if an attribution is later withdrawn.

Nothing here ingests into the Creator DNA profile. The DNA store itself refuses footage evidence without an
eligible-creator stamp, so even a caller that bypasses this module cannot leak unattributed material.
"""
from __future__ import annotations

import dataclasses
from typing import Mapping, Sequence

from media_analysis import adapters as ma_adapters
from media_analysis.adapters import DnaEligibility, DnaHandoff, HandoffReport, SourceResolver
from media_analysis.audio_analysis import AudioAnalysis
from media_analysis.models import MediaRef
from media_analysis.review import ReviewedTranscript, apply_review
from media_analysis.scene_detection import ShotBoundary, VisualEvent
from media_analysis.transcription import Transcript

from .models import AttributionError, Candidate, Decision, EligibilityDecision
from .store import AttributionStore


# ------------------------------------------------------------ candidate builders
def candidates_from_transcript(source: Transcript | ReviewedTranscript,
                               refs: Mapping[str, MediaRef] | None = None) -> list[Candidate]:
    rt = source if isinstance(source, ReviewedTranscript) else apply_review(source, None)
    out = []
    for rs in rt.segments:
        s = rs.original
        out.append(Candidate("transcript_segment", s.record_id, s.media_id, s.start_s, s.end_s,
                             "reviewed" if rs.reviewed else "unverified",
                             media_sha256=(refs or {}).get(s.media_id, MediaRef("x", "x")).sha256))
    return out


def candidates_from_audio(a: AudioAnalysis, include_windows: bool = False,
                          refs: Mapping[str, MediaRef] | None = None) -> list[Candidate]:
    sha = (refs or {}).get(a.media_id, MediaRef("x", "x")).sha256
    out = [Candidate("audio_gap", g.record_id, a.media_id, g.start_s, g.end_s, position=g.position, media_sha256=sha)
           for g in a.gaps]
    out += [Candidate("energy_event", e.record_id, a.media_id, e.start_s, e.end_s, media_sha256=sha)
            for e in a.energy_events]
    if include_windows:
        out += [Candidate("audio_window", w.record_id, a.media_id, w.start_s, w.end_s, media_sha256=sha)
                for w in a.windows]
    return out


def candidates_from_visual(items: Sequence[ShotBoundary | VisualEvent],
                           refs: Mapping[str, MediaRef] | None = None) -> list[Candidate]:
    out = []
    for it in items:
        sha = (refs or {}).get(it.media_id, MediaRef("x", "x")).sha256
        if isinstance(it, ShotBoundary):
            out.append(Candidate("shot_boundary", it.record_id, it.media_id, it.time_s, it.time_s, media_sha256=sha))
        else:
            out.append(Candidate("visual_event", it.record_id, it.media_id, it.start_s, it.end_s, media_sha256=sha))
    return out


# --------------------------------------------------------------------- DNA stamp
def dna_stamp(d: EligibilityDecision):
    """The creator_dna.EvidenceAttribution for an ELIGIBLE decision. Refuses anything else."""
    import creator_dna
    if d.decision is not Decision.ELIGIBLE:
        raise AttributionError(f"only eligible decisions can be stamped onto evidence (got {d.decision.value})")
    c = d.candidate
    delivery = d.resolution.get("delivery") if "delivery" in d.resolution else None
    return creator_dna.EvidenceAttribution(
        decision_id=d.decision_id, candidate_record_id=c.record_id, media_id=c.media_id, channel=c.channel.value,
        start_s=c.start_s, end_s=c.end_s, identity_basis=d.identity_basis or "unspecified",
        confidence=d.confidence, policy_version=d.policy_version, delivery=delivery,
        attribution_evidence_ids=d.attribution_evidence_ids)


# ---------------------------------------------------------------- the handoff
def build_attributed_handoff(resolver: SourceResolver, store: AttributionStore,
                             policy: DnaEligibility = DnaEligibility(),
                             transcripts: Sequence[Transcript | ReviewedTranscript] = (),
                             audio: Sequence[AudioAnalysis] = (),
                             visual: Sequence[ShotBoundary | VisualEvent] = (),
                             source_date: str | None = None) -> DnaHandoff:
    """Evaluate eligibility for every record (persisting each decision, ineligible ones included), then build the
    Creator DNA inputs allowed by `policy` and keep only those with an eligible decision."""
    refs = resolver.refs
    candidates: list[Candidate] = []
    for t in transcripts:
        candidates += candidates_from_transcript(t, refs)
    for a in audio:
        candidates += candidates_from_audio(a, policy.include_level_windows, refs)
    candidates += candidates_from_visual(visual, refs)
    decisions = store.evaluate(candidates)
    by_record = {(d.candidate.media_id, d.candidate.record_id): d for d in decisions}

    raw = ma_adapters.build_handoff(resolver, policy, transcripts, audio, visual, source_date)
    report = HandoffReport(included=[], skipped=list(raw.report.skipped))

    def gate(items: list, kind: str) -> list:
        kept = []
        for it in items:
            o = it.origin
            d = by_record.get((o.media_id, o.record_id)) if o else None
            if d is None:
                report.skipped.append({"record_id": o.record_id if o else "?", "reason": "attribution:no_decision"})
            elif d.decision is not Decision.ELIGIBLE:
                report.skipped.append({"record_id": o.record_id,
                                       "reason": f"attribution:{d.decision.value}:{d.reason_code}",
                                       "decision_id": d.decision_id})
            else:
                kept.append(dataclasses.replace(it, attribution=dna_stamp(d)))
                report.included.append({"record_id": o.record_id, "kind": kind, "status": "eligible",
                                        "decision_id": d.decision_id})
        return kept

    return DnaHandoff(gate(raw.transcript_segments, "transcript_segment"), gate(raw.audio_windows, "audio"),
                      gate(raw.visual_observations, "visual"), report, policy)


# ------------------------------------------------------------------ revocation
def sync_revocations(dna_store, store: AttributionStore, actor: str = "speaker_attribution") -> list[tuple[str, str]]:
    """If an attribution that once made evidence eligible has since been withdrawn or corrected, the Creator DNA
    evidence stamped with it stops counting (rejected with a traceable note; nothing is deleted).
    Idempotent. Returns (trait_id, evidence_id) pairs revoked by this call."""
    revoked = []
    for old, cur in store.withdrawn_eligibility():
        revoked += dna_store.revoke_attribution(
            old.decision_id, f"decision now {cur.decision.value}: {cur.reason_code}", actor)
    return revoked


def summarize(decisions: Sequence[EligibilityDecision]) -> dict:
    """Counts per decision and reason code, for reports."""
    out: dict = {"total": len(decisions), "by_decision": {}, "by_reason": {}}
    for d in decisions:
        out["by_decision"][d.decision.value] = out["by_decision"].get(d.decision.value, 0) + 1
        k = f"{d.decision.value}:{d.reason_code}"
        out["by_reason"][k] = out["by_reason"].get(k, 0) + 1
    return out
