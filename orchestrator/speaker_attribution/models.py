"""Models for speaker attribution and Creator DNA eligibility (stdlib only).

Four separate dimensions are never collapsed into one label:
  1. WHO     -> `Identity`  (creator / other_identified / unknown / multiple_or_overlapping / non_speech)
  2. HOW     -> `Delivery`  (on_camera_direct_speech / off_camera_speech / voiceover / narration /
                             playback_or_background_audio / conversation / unknown)
  3. WHETHER -> `Decision`  (eligible / ineligible / requires_review) with a machine-readable reason
  4. HOW SURE-> a derived confidence, kept apart from the decision

Speech and visual behaviour are separate CHANNELS: a visible person is not automatically the speaker,
and a speaker is not automatically visible.

Everything here is a CLAIM backed by evidence. Nothing is identified by this package: there is no
voice biometrics, no face recognition and no diarisation engine. Evidence comes from humans, project
metadata, or (future, optional) providers whose output can only ever be *supporting*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from media_analysis.models import MediaAnalysisError, check_range as _check_range, stable_hash, t_key, utc_now


class AttributionError(MediaAnalysisError):
    """Invalid attribution data or an operation the policy forbids."""


class Identity(str, Enum):
    CREATOR = "creator"
    OTHER_IDENTIFIED = "other_identified"      # someone else, with a stable PROJECT-LOCAL id (no real name needed)
    UNKNOWN = "unknown"
    MULTIPLE_OR_OVERLAPPING = "multiple_or_overlapping"
    NON_SPEECH = "non_speech"                  # speech channel only


class Delivery(str, Enum):
    """Delivery/source context of speech. NOT an identity: a creator or a friend can each do a voiceover."""
    ON_CAMERA_DIRECT_SPEECH = "on_camera_direct_speech"
    OFF_CAMERA_SPEECH = "off_camera_speech"
    VOICEOVER = "voiceover"
    NARRATION = "narration"
    PLAYBACK_OR_BACKGROUND_AUDIO = "playback_or_background_audio"
    CONVERSATION = "conversation"
    UNKNOWN = "unknown"


class Channel(str, Enum):
    SPEECH = "speech"
    VISUAL = "visual"


class AttributionMethod(str, Enum):
    # AUTHORITATIVE: a person decided (or the project states it). Can establish an attribution.
    HUMAN_CONFIRMATION = "human_confirmation"
    HUMAN_CORRECTION = "human_correction"          # overrides earlier evidence (range-scoped, traceable)
    PROJECT_METADATA = "project_metadata"
    # SUPPORTING: can only ever PROPOSE; never sufficient for eligibility on its own.
    DIARIZATION_CLUSTER = "diarization_cluster"    # "same voice as cluster X": a grouping, not an identity
    AV_SYNC = "av_sync"                            # lip/voice synchrony: supports, never proves
    VISUAL_CONTEXT = "visual_context"              # who is on screen: presence is not speech
    CONTINUITY = "continuity"                      # cross-clip similarity: not identity
    PRIOR_REVIEWED_DECISION = "prior_reviewed_decision"


AUTHORITATIVE = frozenset({AttributionMethod.HUMAN_CONFIRMATION, AttributionMethod.HUMAN_CORRECTION,
                           AttributionMethod.PROJECT_METADATA})
HUMAN_METHODS = frozenset({AttributionMethod.HUMAN_CONFIRMATION, AttributionMethod.HUMAN_CORRECTION})
RANK = {AttributionMethod.HUMAN_CONFIRMATION: 2, AttributionMethod.HUMAN_CORRECTION: 2,
        AttributionMethod.PROJECT_METADATA: 1}


class Status(str, Enum):
    CONFIRMED = "confirmed"                       # authoritative evidence, direct
    CONFIRMED_VIA_CLUSTER = "confirmed_via_cluster"   # a human mapped the provider's cluster to an identity
    PROPOSED = "proposed"                         # supporting evidence only: never eligible
    CONFLICTED = "conflicted"                     # contradictory evidence: identity treated as unknown
    UNATTRIBUTED = "unattributed"                 # no evidence covers this range


class Decision(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    REQUIRES_REVIEW = "requires_review"


@dataclass(frozen=True)
class PolicyConfig:
    """Tunable numbers. All are implementer defaults awaiting the creator (see DECISIONS Q37+)."""
    version: str = "1"
    human_confidence: float = 0.95        # base confidence of a human decision
    metadata_confidence: float = 0.85     # base confidence of an explicit project-metadata statement
    supporting_cap: float = 0.70          # supporting evidence can never exceed this
    min_eligible_confidence: float = 0.80
    coverage_tolerance_s: float = 0.25    # an evidence range "covers" a candidate within this slack
    gap_probe_s: float = 0.2              # how far either side of a pause is probed for speech attribution
    unknown_delivery_requires_review: bool = True

    def to_dict(self) -> dict:
        return dict(vars(self))


def check_range(start, end):
    try:
        return _check_range(start, end)
    except MediaAnalysisError as exc:
        raise AttributionError(str(exc)) from exc


def _conf(value: Any, name: str = "confidence") -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) <= 1.0:
        raise AttributionError(f"{name} must be a number within (0, 1] or None (never defaulted)")
    return float(value)


@dataclass(frozen=True)
class AttributionEvidence:
    """One piece of evidence about who (or which visible subject) is behind a time range.

    `scope="span"` evidence is about a time range. `scope="cluster"` evidence maps a provider's
    cluster id to an identity for the WHOLE recording (authoritative methods only)."""
    media_id: str
    channel: Channel
    method: AttributionMethod
    actor: str                              # "human:<name>" | "project:<name>" | "provider:<name>@<ver>" ...
    source_ref: str                         # where this came from: a note, a file, a provider run id
    rationale: str                          # plain-language why (required)
    start_s: float | None = None
    end_s: float | None = None
    scope: str = "span"                     # "span" | "cluster"
    identity: Identity | None = None
    person_id: str | None = None            # project-local id for other_identified
    delivery: Delivery | None = None
    cluster_id: str | None = None
    provider: str | None = None             # cluster ids are provider-local and media-local
    confidence: float | None = None
    isolated: bool = False                  # human asserts the creator's contribution is isolated in this range
    supersedes: tuple[str, ...] = ()        # evidence ids this item overrides WITHIN its own range
    refs: tuple[str, ...] = ()              # upstream record ids (transcript segment, diarization record, ...)
    media_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.media_id or not self.actor or not self.source_ref or not self.rationale.strip():
            raise AttributionError("attribution evidence needs media_id, actor, source_ref and a rationale")
        _conf(self.confidence)
        object.__setattr__(self, "supersedes", tuple(self.supersedes))
        object.__setattr__(self, "refs", tuple(self.refs))
        auth = self.method in AUTHORITATIVE
        if self.scope not in ("span", "cluster"):
            raise AttributionError("scope must be 'span' or 'cluster'")
        if self.method in HUMAN_METHODS and not (self.actor.startswith("human:") and self.actor.split(":", 1)[1].strip()):
            raise AttributionError("human attribution methods need actor 'human:<name>'")
        if self.method is AttributionMethod.PROJECT_METADATA and not self.actor.startswith(("human:", "project:")):
            raise AttributionError("project metadata must come from 'human:<name>' or 'project:<name>'")
        if not auth and self.actor.startswith("human:"):
            raise AttributionError("a human decision must use a human_* method; supporting methods are for providers")
        if self.scope == "cluster":
            if not auth or not self.cluster_id or not self.provider or self.identity is None:
                raise AttributionError("cluster scope maps a provider cluster to an identity and needs an "
                                       "authoritative method, cluster_id, provider and identity")
            if self.channel is not Channel.SPEECH:
                raise AttributionError("clusters exist on the speech channel only")
        else:
            if self.start_s is None or self.end_s is None:
                raise AttributionError("span evidence needs start_s and end_s")
            check_range(self.start_s, self.end_s)
        if self.method is AttributionMethod.DIARIZATION_CLUSTER:
            if self.identity is not None or self.delivery is not None:
                raise AttributionError("a diarisation cluster is a grouping, never an identity or a delivery")
            if not self.cluster_id or not self.provider or self.channel is not Channel.SPEECH:
                raise AttributionError("diarisation evidence needs cluster_id and provider (speech channel)")
        elif self.identity is None and self.delivery is None:
            raise AttributionError("evidence must claim an identity and/or a delivery")
        if self.identity is Identity.OTHER_IDENTIFIED and not self.person_id:
            raise AttributionError("other_identified needs a project-local person_id")
        if self.identity is not Identity.OTHER_IDENTIFIED and self.person_id:
            raise AttributionError("person_id is only valid with identity other_identified")
        if self.identity is Identity.NON_SPEECH and self.channel is not Channel.SPEECH:
            raise AttributionError("non_speech is a speech-channel identity")
        if self.delivery is not None and self.channel is not Channel.SPEECH:
            raise AttributionError("delivery context applies to speech only")
        if self.isolated and not (auth and self.identity is Identity.CREATOR and self.channel is Channel.SPEECH):
            raise AttributionError("'isolated' is a human/metadata claim about creator speech only")
        if self.method is AttributionMethod.HUMAN_CORRECTION and not self.supersedes:
            raise AttributionError("a correction must name the evidence it overrides (otherwise it is a confirmation)")

    @property
    def authoritative(self) -> bool:
        return self.method in AUTHORITATIVE

    @property
    def evidence_id(self) -> str:
        return "ae_" + stable_hash(
            self.media_id, self.channel.value, self.method.value, self.actor, self.source_ref, self.scope,
            t_key(self.start_s), t_key(self.end_s), self.identity.value if self.identity else None,
            self.person_id, self.delivery.value if self.delivery else None, self.cluster_id, self.provider,
            self.confidence, self.isolated, sorted(self.supersedes), sorted(self.refs), self.rationale)

    def to_dict(self) -> dict:
        return {
            "media_id": self.media_id, "channel": self.channel.value, "method": self.method.value,
            "actor": self.actor, "source_ref": self.source_ref, "rationale": self.rationale,
            "start_s": self.start_s, "end_s": self.end_s, "scope": self.scope,
            "identity": self.identity.value if self.identity else None, "person_id": self.person_id,
            "delivery": self.delivery.value if self.delivery else None, "cluster_id": self.cluster_id,
            "provider": self.provider, "confidence": self.confidence, "isolated": self.isolated,
            "supersedes": list(self.supersedes), "refs": list(self.refs), "media_sha256": self.media_sha256}

    @classmethod
    def from_dict(cls, d: Mapping) -> "AttributionEvidence":
        return cls(
            media_id=d["media_id"], channel=Channel(d["channel"]), method=AttributionMethod(d["method"]),
            actor=d["actor"], source_ref=d["source_ref"], rationale=d["rationale"], start_s=d.get("start_s"),
            end_s=d.get("end_s"), scope=d.get("scope", "span"),
            identity=Identity(d["identity"]) if d.get("identity") else None, person_id=d.get("person_id"),
            delivery=Delivery(d["delivery"]) if d.get("delivery") else None, cluster_id=d.get("cluster_id"),
            provider=d.get("provider"), confidence=d.get("confidence"), isolated=d.get("isolated", False),
            supersedes=tuple(d.get("supersedes", ())), refs=tuple(d.get("refs", ())),
            media_sha256=d.get("media_sha256"))


@dataclass(frozen=True)
class Resolution:
    """The derived attribution of one candidate time range. Recomputed from evidence, never stored as truth."""
    media_id: str
    channel: Channel
    start_s: float
    end_s: float
    identity: Identity
    person_id: str | None
    delivery: Delivery
    status: Status
    confidence: float | None               # None = cannot be quantified (never invented)
    basis: tuple[str, ...]                 # methods that carried the result
    evidence_ids: tuple[str, ...]          # covering evidence used
    partial_ids: tuple[str, ...]           # evidence overlapping only part of the range
    overridden_ids: tuple[str, ...]        # evidence ignored because a correction overrides it here
    via_cluster: bool
    mixed: bool                            # another identity overlaps part of the range
    delivery_conflict: bool
    cluster_without_identity: bool
    proposed_delivery: tuple[str, ...]     # delivery claimed only by supporting evidence (not trusted)
    isolated: bool
    explanation: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.media_id, self.channel.value, self.identity.value, self.person_id,
                           self.delivery.value, self.status.value, self.confidence, sorted(self.evidence_ids),
                           sorted(self.partial_ids), sorted(self.overridden_ids), self.mixed, self.isolated)

    def to_dict(self) -> dict:
        return {"media_id": self.media_id, "channel": self.channel.value, "start_s": self.start_s,
                "end_s": self.end_s, "identity": self.identity.value, "person_id": self.person_id,
                "delivery": self.delivery.value, "status": self.status.value,
                "confidence": None if self.confidence is None else round(self.confidence, 3),
                "basis": list(self.basis), "evidence_ids": list(self.evidence_ids),
                "partial_ids": list(self.partial_ids), "overridden_ids": list(self.overridden_ids),
                "via_cluster": self.via_cluster, "mixed": self.mixed, "delivery_conflict": self.delivery_conflict,
                "cluster_without_identity": self.cluster_without_identity,
                "proposed_delivery": list(self.proposed_delivery), "isolated": self.isolated,
                "explanation": list(self.explanation), "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class Candidate:
    """Something that might become Creator DNA evidence, awaiting an eligibility decision."""
    kind: str            # transcript_segment | audio_gap | energy_event | audio_window | visual_event | shot_boundary
    record_id: str       # the upstream (media_analysis) record id
    media_id: str
    start_s: float
    end_s: float
    transcript_state: str = "not_applicable"   # "reviewed" | "unverified" | "not_applicable"
    position: str | None = None                # for audio_gap: between | leading | trailing | whole
    media_sha256: str | None = None

    CHANNEL = {"transcript_segment": Channel.SPEECH, "audio_gap": Channel.SPEECH, "energy_event": Channel.SPEECH,
               "audio_window": Channel.SPEECH, "visual_event": Channel.VISUAL, "shot_boundary": Channel.VISUAL}

    def __post_init__(self) -> None:
        if self.kind not in self.CHANNEL:
            raise AttributionError(f"unknown candidate kind {self.kind!r}")
        if not self.record_id or not self.media_id:
            raise AttributionError("candidate needs record_id and media_id")
        check_range(self.start_s, self.end_s)
        if self.transcript_state not in ("reviewed", "unverified", "not_applicable"):
            raise AttributionError("transcript_state must be reviewed, unverified or not_applicable")
        if (self.kind == "transcript_segment") == (self.transcript_state == "not_applicable"):
            raise AttributionError("transcript_state applies to transcript segments only (and is required for them)")

    @property
    def channel(self) -> Channel:
        return self.CHANNEL[self.kind]

    @property
    def key(self) -> str:
        return f"{self.media_id}|{self.kind}|{self.record_id}"


@dataclass(frozen=True)
class EligibilityDecision:
    """An explicit, persisted decision about one candidate, with its reason and provenance.
    Ineligible and requires_review decisions are kept: the system can always explain an exclusion."""
    candidate: Candidate
    decision: Decision
    reason_code: str
    reason: str
    resolution: Mapping[str, Any]          # Resolution.to_dict() (or the neighbour resolutions for a pause)
    policy_version: str
    attribution_evidence_ids: tuple[str, ...]
    identity_basis: str | None             # for eligible decisions: how identity was established
    confidence: float | None
    max_evidence_grade: str                # verbatim | asr_unverified_only | measured | detection | none
    transcript_review: str                 # reviewed | unverified | not_applicable (an INDEPENDENT requirement)
    supersedes: str | None = None
    recorded_at: str = field(default_factory=utc_now)

    @property
    def decision_id(self) -> str:
        return "dec_" + stable_hash(self.candidate.key, self.candidate.start_s, self.candidate.end_s,
                                    self.decision.value, self.reason_code, self.policy_version,
                                    sorted(self.attribution_evidence_ids), self.confidence,
                                    self.transcript_review, self.resolution.get("fingerprint"))

    def to_dict(self) -> dict:
        c = self.candidate
        return {"decision_id": self.decision_id, "kind": c.kind, "record_id": c.record_id, "media_id": c.media_id,
                "start_s": c.start_s, "end_s": c.end_s, "transcript_state": c.transcript_state,
                "position": c.position, "media_sha256": c.media_sha256, "decision": self.decision.value,
                "reason_code": self.reason_code, "reason": self.reason, "resolution": dict(self.resolution),
                "policy_version": self.policy_version, "attribution_evidence_ids": list(self.attribution_evidence_ids),
                "identity_basis": self.identity_basis, "confidence": self.confidence,
                "max_evidence_grade": self.max_evidence_grade, "transcript_review": self.transcript_review,
                "supersedes": self.supersedes, "recorded_at": self.recorded_at}

    @classmethod
    def from_dict(cls, d: Mapping) -> "EligibilityDecision":
        cand = Candidate(d["kind"], d["record_id"], d["media_id"], d["start_s"], d["end_s"],
                         d.get("transcript_state", "not_applicable"), d.get("position"), d.get("media_sha256"))
        e = cls(cand, Decision(d["decision"]), d["reason_code"], d["reason"], d["resolution"], d["policy_version"],
                tuple(d.get("attribution_evidence_ids", ())), d.get("identity_basis"), d.get("confidence"),
                d["max_evidence_grade"], d["transcript_review"], d.get("supersedes"), d.get("recorded_at", ""))
        if e.decision_id != d.get("decision_id"):
            raise AttributionError("decision id mismatch: log edited or corrupt")
        return e
