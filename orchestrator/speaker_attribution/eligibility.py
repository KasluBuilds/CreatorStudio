"""Creator DNA eligibility policy: one explicit decision per candidate, with a machine-readable reason.

Decision table (speech channel; the visual channel is decided separately and independently):

  playback / background audio (authoritative delivery)      -> ineligible   playback_or_background_audio
  creator confirmed, delivery known, confidence >= minimum  -> eligible     creator_speech_confirmed
  creator confirmed, delivery unknown                       -> requires_review  delivery_unknown
  creator only proposed by supporting evidence              -> requires_review  creator_proposed_unconfirmed
  confidence missing / below minimum                        -> requires_review  attribution_confidence_unquantified / low_attribution_confidence
  contradictory evidence                                    -> requires_review  conflicting_attribution_evidence
  other identified person                                   -> ineligible   other_speaker
  multiple/overlapping speakers (creator not isolated)      -> ineligible   overlapping_speakers
  creator plus another identity inside the range            -> ineligible   mixed_speakers_in_candidate
  unknown / unattributed / unmapped cluster                 -> ineligible   speaker_unknown / cluster_without_identity
  non-speech                                                -> ineligible   non_speech

A pause is the creator's delivery only when it lies between creator-attributed speech on BOTH sides.
A shot boundary is never person behaviour. Visual behaviour is eligible only when the visible subject
is attributed to the creator, independently of who is speaking.

Eligibility concerns attribution ONLY. Transcript review is a separate, independent requirement: the
decision records `transcript_review` and `max_evidence_grade` (`verbatim` only for a reviewed transcript,
otherwise `asr_unverified_only`) and never promotes unverified ASR.
"""
from __future__ import annotations

from typing import Callable

from .models import (Candidate, Channel, Decision, Delivery, EligibilityDecision, Identity, PolicyConfig,
                     Resolution, Status)

Resolver = Callable[[str, Channel, float, float], Resolution]


def _grade(c: Candidate) -> str:
    return {"transcript_segment": "verbatim" if c.transcript_state == "reviewed" else "asr_unverified_only",
            "audio_gap": "measured", "energy_event": "measured", "audio_window": "measured",
            "visual_event": "detection"}.get(c.kind, "none")


def _basis(r: Resolution) -> str:
    return "+".join(r.basis) + ("+cluster_mapping" if r.via_cluster else "")


def _speech(res: Resolution, policy: PolicyConfig) -> tuple[Decision, str, str]:
    D = Decision
    if res.delivery is Delivery.PLAYBACK_OR_BACKGROUND_AUDIO:
        return D.INELIGIBLE, "playback_or_background_audio", "playback or background audio is not live delivery by the creator, whoever is heard"
    if res.status is Status.CONFLICTED:
        return D.REQUIRES_REVIEW, "conflicting_attribution_evidence", "attribution evidence contradicts itself: " + "; ".join(res.explanation)
    if res.identity is Identity.OTHER_IDENTIFIED:
        return D.INELIGIBLE, "other_speaker", f"speech attributed to another person ({res.person_id})"
    if res.identity is Identity.MULTIPLE_OR_OVERLAPPING:
        return D.INELIGIBLE, "overlapping_speakers", "multiple or overlapping speakers and the creator's contribution is not isolated"
    if res.identity is Identity.NON_SPEECH:
        return D.INELIGIBLE, "non_speech", "marked as non-speech"
    if res.identity is Identity.UNKNOWN:
        if res.cluster_without_identity:
            return D.INELIGIBLE, "cluster_without_identity", "a diarisation cluster covers this but no human mapped it to an identity: a cluster is not an identity"
        return D.INELIGIBLE, "speaker_unknown", "the speaker is unknown" + (" (unknown narrator)" if res.delivery is Delivery.NARRATION else "")
    # identity is CREATOR from here
    if res.mixed and not res.isolated:
        return D.INELIGIBLE, "mixed_speakers_in_candidate", "another identity overlaps part of this range and the creator's contribution is not isolated"
    if res.status is Status.PROPOSED:
        return D.REQUIRES_REVIEW, "creator_proposed_unconfirmed", "only supporting evidence suggests the creator; a human must confirm"
    if res.confidence is None:
        return D.REQUIRES_REVIEW, "attribution_confidence_unquantified", "attribution confidence cannot be quantified (missing confidence of a supporting step)"
    if res.confidence < policy.min_eligible_confidence:
        return D.REQUIRES_REVIEW, "low_attribution_confidence", f"attribution confidence {res.confidence:.2f} is below {policy.min_eligible_confidence:.2f}"
    if res.delivery_conflict:
        return D.REQUIRES_REVIEW, "delivery_conflict", "contradictory delivery context"
    if res.delivery is Delivery.UNKNOWN and policy.unknown_delivery_requires_review:
        return D.REQUIRES_REVIEW, "delivery_unknown", "creator confirmed, but delivery context is unknown (it could be playback)"
    code = "creator_voiceover_confirmed" if res.delivery in (Delivery.VOICEOVER, Delivery.NARRATION) else "creator_speech_confirmed"
    return D.ELIGIBLE, code, f"creator confirmed ({_basis(res)}), delivery {res.delivery.value}"


def _visual(res: Resolution, policy: PolicyConfig) -> tuple[Decision, str, str]:
    D = Decision
    if res.status is Status.CONFLICTED:
        return D.REQUIRES_REVIEW, "conflicting_attribution_evidence", "visual-subject evidence contradicts itself: " + "; ".join(res.explanation)
    if res.identity is Identity.OTHER_IDENTIFIED:
        return D.INELIGIBLE, "other_person_visible", f"the visible subject is another person ({res.person_id})"
    if res.identity is Identity.MULTIPLE_OR_OVERLAPPING:
        return D.INELIGIBLE, "multiple_subjects", "several people are in the behaviour range"
    if res.identity is Identity.UNKNOWN:
        return D.INELIGIBLE, "visual_subject_unknown", "the visible subject is not attributed to the creator (a person being present is not enough)"
    if res.mixed:
        return D.INELIGIBLE, "mixed_subjects_in_candidate", "another subject overlaps part of this range"
    if res.status is Status.PROPOSED:
        return D.REQUIRES_REVIEW, "creator_proposed_unconfirmed", "only supporting evidence suggests the creator is the subject"
    if res.confidence is None:
        return D.REQUIRES_REVIEW, "attribution_confidence_unquantified", "attribution confidence cannot be quantified"
    if res.confidence < policy.min_eligible_confidence:
        return D.REQUIRES_REVIEW, "low_attribution_confidence", f"attribution confidence {res.confidence:.2f} is below {policy.min_eligible_confidence:.2f}"
    return D.ELIGIBLE, "creator_visual_confirmed", f"visible subject confirmed as the creator ({_basis(res)})"


def decide(c: Candidate, resolver: Resolver, policy: PolicyConfig) -> EligibilityDecision:
    D = Decision

    def make(decision, code, reason, resolution, ids=(), basis=None, conf=None):
        elig = decision is D.ELIGIBLE
        return EligibilityDecision(
            candidate=c, decision=decision, reason_code=code, reason=reason, resolution=resolution,
            policy_version=policy.version, attribution_evidence_ids=tuple(ids),
            identity_basis=basis if elig else None, confidence=conf if elig else None,
            max_evidence_grade=_grade(c) if elig else "none", transcript_review=c.transcript_state)

    if c.kind == "shot_boundary":
        return make(D.INELIGIBLE, "not_person_behavior",
                    "a shot boundary is a picture change, not behaviour of a person", {"fingerprint": "n/a"})
    if c.kind == "visual_event":
        r = resolver(c.media_id, Channel.VISUAL, c.start_s, c.end_s)
        d, code, why = _visual(r, policy)
        return make(d, code, why, r.to_dict(), r.evidence_ids, _basis(r), r.confidence)
    if c.kind == "audio_gap":
        if c.position != "between":
            return make(D.INELIGIBLE, "gap_without_two_sided_speech",
                        f"a {c.position or 'unpositioned'} pause has no speech on both sides, so it is not delivery",
                        {"fingerprint": "n/a"})
        p = policy.gap_probe_s
        before = resolver(c.media_id, Channel.SPEECH, max(0.0, c.start_s - p), c.start_s)
        after = resolver(c.media_id, Channel.SPEECH, c.end_s, c.end_s + p)
        parts = [(before, *_speech(before, policy)), (after, *_speech(after, policy))]
        resolution = {"fingerprint": "gap:" + before.fingerprint + ":" + after.fingerprint,
                      "before": before.to_dict(), "after": after.to_dict()}
        ids = sorted(set(before.evidence_ids) | set(after.evidence_ids))
        if all(d is D.ELIGIBLE for _, d, _, _ in parts):
            conf = min(before.confidence, after.confidence)
            return make(D.ELIGIBLE, "creator_pause_between_creator_speech",
                        "pause lies between creator-attributed speech on both sides", resolution, ids,
                        _basis(before), conf)
        if any(d is D.REQUIRES_REVIEW for _, d, _, _ in parts):
            return make(D.REQUIRES_REVIEW, "gap_neighbour_needs_review",
                        "a neighbouring speech attribution needs review: " + "; ".join(w for _, d, _, w in parts if d is D.REQUIRES_REVIEW),
                        resolution, ids)
        if all(code in ("speaker_unknown", "cluster_without_identity") for _, _, code, _ in parts):
            return make(D.INELIGIBLE, "speaker_unknown", "the speakers around this pause are unknown", resolution, ids)
        return make(D.INELIGIBLE, "gap_not_between_creator_speech",
                    "the pause is not between creator-attributed speech: " + "; ".join(w for _, _, _, w in parts),
                    resolution, ids)
    r = resolver(c.media_id, Channel.SPEECH, c.start_s, c.end_s)          # transcript_segment, energy_event, audio_window
    d, code, why = _speech(r, policy)
    return make(d, code, why, r.to_dict(), r.evidence_ids, _basis(r), r.confidence)
