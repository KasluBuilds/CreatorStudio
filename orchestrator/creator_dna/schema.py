"""Data model for the Creator DNA profile (stdlib only).

Key ideas
---------
* A *trait* is a short statement about observable creative behaviour
  ("uses 'ajde de' as a sentence opener"). It never holds the creator's words
  itself; those live in *evidence*.
* *Evidence* points at a source recording (+ timecodes, quote, context) and either
  supports or counters (counterexample) a trait. Its `evidence_origin` says whether it
  comes from footage or from the creator's own statements (self-report).
* Every trait lives in exactly one *layer*: observed fact, AI interpretation, hypothesis,
  or creator self-report. Layers are kept separate in every export.
* Everything is append-only: state is rebuilt by replaying events (see store.py).

Schema v3 adds `EvidenceAttribution` (see below); v2 vs v1: ASR_UNVERIFIED method, SELF_REPORT layer, UNASSESSED scope,
`reviewed_by` (required on VERBATIM), `origin` (provenance), optional `strength`.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

SCHEMA_VERSION = 5   # v3: footage evidence carries an eligible-creator `attribution` stamp;
                     # v4: attribution revocation is its own `attribution_revoked` event and
                     #     evidence_review is strict (reject / restore only);
                     # v5: purpose-scoped consent (`consent_recorded`, `consent_withdrawn`), evidence
                     #     quarantine (`evidence_quarantined`, `quarantine_released`) and optional
                     #     `project_id` / `session_id` on Origin


class Category(str, Enum):
    """Closed list on purpose: keeps the profile on observable production
    behaviour and prevents unrelated personal inferences."""
    VOCABULARY = "vocabulary"            # words, slang, recurring phrases
    LANGUAGE = "language"                # Croatian / Kajkavian / English mix, pronunciation
    HUMOR = "humor"
    SPEECH_RHYTHM = "speech_rhythm"      # sentence length, pace
    PAUSES = "pauses"                    # silence, hesitation, filler
    DELIVERY = "delivery"                # energy, emphasis, emotional range
    GESTURE = "gesture"
    EXPRESSION = "expression"            # facial expression
    CAMERA_PRESENCE = "camera_presence"
    EDITING_PREFERENCE = "editing_preference"   # taste shown by creator feedback/edits
    TOPIC = "topic"                      # topics, explaining habits
    OTHER_PRODUCTION = "other_production"


class Layer(str, Enum):
    OBSERVED = "observed"                # checkable in the material; footage-derived evidence only
    INTERPRETATION = "interpretation"    # an AI reading of observed material
    HYPOTHESIS = "hypothesis"            # tentative, needs more evidence
    SELF_REPORT = "self_report"          # what the creator says about themselves; NOT footage evidence


class Method(str, Enum):
    """How an evidence item was obtained; decides how much it can be trusted."""
    VERBATIM = "verbatim"                # exact quote from a HUMAN-REVIEWED transcript (reviewed_by required)
    ASR_UNVERIFIED = "asr_unverified"    # wording recognised by a transcription engine, not reviewed
    MEASURED = "measured"                # deterministic measurement (e.g. pause length)
    AI_ESTIMATE = "ai_estimate"          # model/detector judgement (e.g. "looks amused")
    CREATOR_STATEMENT = "creator_statement"  # creator's own words about themselves


class Modality(str, Enum):
    TRANSCRIPT = "transcript"
    AUDIO = "audio"
    VISUAL = "visual"
    FEEDBACK = "feedback"                # creator feedback / corrections
    EDIT_HISTORY = "edit_history"


class Polarity(str, Enum):
    SUPPORT = "support"
    COUNTER = "counter"                  # counterexample


class Scope(str, Enum):
    ONE_OFF = "one_off"                  # a single source: never a style rule
    EMERGING = "emerging"                # 2 sources, not yet enough to classify
    CONTEXTUAL = "contextual"            # holds only in some contexts / has counterexamples
    STABLE = "stable"                    # repeated, consistent, across sources
    UNASSESSED = "unassessed"            # no rated footage evidence: stability cannot be judged


class Status(str, Enum):
    ACTIVE = "active"
    CONFIRMED = "confirmed"              # creator confirmed
    REJECTED = "rejected"                # creator rejected; never resurrected by learning
    CORRECTED = "corrected"              # creator replaced it with another trait


class DNAError(ValueError):
    """Invalid input or an operation the profile does not allow."""


def normalize_text(text: str) -> str:
    """Case/whitespace normalisation for comparison. Keeps diacritics (č, š, ž,
    đ are different letters, not noise) so Croatian words are not conflated."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().casefold()


def trait_id_for(category: Category, statement: str) -> str:
    h = hashlib.sha1(f"{category.value}|{normalize_text(statement)}".encode("utf-8")).hexdigest()
    return "tr_" + h[:12]


def check_reviewer(name: str | None) -> str:
    if not name or not name.startswith("human:") or not name.split(":", 1)[1].strip():
        raise DNAError("reviewer must look like 'human:<name>' (a named person)")
    return name


_RECORD_KINDS = ("transcription", "measurement", "detection")


@dataclass(frozen=True)
class ReviewInfo:
    """How a human reviewed a transcript segment (present only when the text was reviewed)."""
    reviewer: str                           # "human:<name>"
    review_id: str
    status: str                             # "confirmed" (engine text was right) | "corrected"
    original_record_id: str | None = None   # id of the original ASR segment
    original_text: str | None = None        # what the engine produced

    def __post_init__(self) -> None:
        check_reviewer(self.reviewer)
        if self.status not in ("confirmed", "corrected"):
            raise DNAError("review status must be 'confirmed' or 'corrected'")
        if not self.review_id:
            raise DNAError("review needs a review_id")

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class Origin:
    """Where an input/evidence item came from, so it can be traced to the exact original.

    `media_id`/`source_sha256`/`source_path` always identify the ORIGINAL recording, never a proxy
    or analysis copy (`analysis_copy_id` records that a copy was analysed; times stay on the original
    timeline). `record_id` is the upstream (media_analysis) record. `settings` holds the analysis
    parameters that give a measurement its meaning (e.g. the silence threshold)."""
    media_id: str
    source_sha256: str | None = None
    source_path: str | None = None
    record_id: str | None = None
    record_kind: str | None = None          # transcription | measurement | detection
    engine: str | None = None               # "name@version"
    settings: Mapping | None = None
    analysis_copy_id: str | None = None
    review: ReviewInfo | None = None
    project_id: str | None = None           # project-local project identity (consent is checked against it)
    session_id: str | None = None           # one shoot/session: recordings of one session are ONE independent witness

    def __post_init__(self) -> None:
        if not self.media_id or not self.media_id.strip():
            raise DNAError("origin needs the original media_id")
        for name in ("project_id", "session_id"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, str) or not v.strip()):
                raise DNAError(f"origin {name} must be a non-empty string or None")
        if self.source_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise DNAError("source_sha256 must be a 64-character lowercase hex digest")
        if self.record_kind is not None and self.record_kind not in _RECORD_KINDS:
            raise DNAError(f"record_kind must be one of {_RECORD_KINDS}")

    def to_dict(self) -> dict:
        d = dict(vars(self))
        d["settings"] = None if self.settings is None else dict(self.settings)
        d["review"] = None if self.review is None else self.review.to_dict()
        for k in ("project_id", "session_id"):      # only written when set, so older records stay byte-identical
            if d[k] is None:
                del d[k]
        return d

    @classmethod
    def from_dict(cls, d: Mapping) -> "Origin":
        rv = d.get("review")
        return cls(media_id=d["media_id"], source_sha256=d.get("source_sha256"), source_path=d.get("source_path"),
                   record_id=d.get("record_id"), record_kind=d.get("record_kind"), engine=d.get("engine"),
                   settings=d.get("settings"), analysis_copy_id=d.get("analysis_copy_id"),
                   review=None if rv is None else ReviewInfo(**rv),
                   project_id=d.get("project_id"), session_id=d.get("session_id"))


_CHANNELS = ("speech", "visual")


@dataclass(frozen=True)
class EvidenceAttribution:
    """Stamp saying: an upstream eligibility decision (speaker_attribution) found this item eligible as
    evidence about the CREATOR.

    It can only describe an eligible creator attribution: there is no way to construct one for an unknown
    person, another person, overlapping speakers or playback audio, so such evidence cannot carry a valid
    stamp. The stamp points at the decision and the attribution evidence behind it; it says nothing about
    whether the content (e.g. the transcript wording) is accurate."""
    decision_id: str
    candidate_record_id: str            # the upstream record the decision was made for
    media_id: str
    channel: str                        # "speech" | "visual"
    start_s: float
    end_s: float
    identity_basis: str                 # e.g. "human_confirmation", "project_metadata", "cluster_mapping"
    confidence: float                   # attribution confidence (0..1), as derived upstream
    policy_version: str
    delivery: str | None = None         # speech only, e.g. "on_camera_direct_speech"
    attribution_evidence_ids: tuple[str, ...] = ()
    subject: str = "creator"
    decision: str = "eligible"

    def __post_init__(self) -> None:
        if self.subject != "creator" or self.decision != "eligible":
            raise DNAError("an evidence attribution can only state: subject=creator, decision=eligible")
        if not (self.decision_id and self.candidate_record_id and self.media_id and self.identity_basis
                and self.policy_version):
            raise DNAError("evidence attribution needs decision_id, candidate_record_id, media_id, "
                           "identity_basis and policy_version")
        if self.channel not in _CHANNELS:
            raise DNAError(f"channel must be one of {_CHANNELS}")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) \
                or not 0.0 < self.confidence <= 1.0:
            raise DNAError("attribution confidence must be a number within (0, 1]; it is never defaulted")
        if self.start_s < 0 or self.end_s < self.start_s:
            raise DNAError("attribution needs 0 <= start_s <= end_s")
        if not self.attribution_evidence_ids:
            raise DNAError("an attribution must cite the attribution evidence it rests on")
        object.__setattr__(self, "attribution_evidence_ids", tuple(self.attribution_evidence_ids))

    def to_dict(self) -> dict:
        d = dict(vars(self))
        d["attribution_evidence_ids"] = list(self.attribution_evidence_ids)
        return d

    @classmethod
    def from_dict(cls, d: Mapping) -> "EvidenceAttribution":
        d = dict(d)
        d["attribution_evidence_ids"] = tuple(d.get("attribution_evidence_ids", ()))
        return cls(**d)


@dataclass(frozen=True)
class Evidence:
    """One pointer into the source material. Immutable."""
    source_id: str                       # opaque recording id, e.g. "<project-slug>/<media-id>"
    modality: Modality
    method: Method
    polarity: Polarity = Polarity.SUPPORT
    start_s: float | None = None         # seconds on the ORIGINAL media's timeline; None = untimed
    end_s: float | None = None
    quote: str | None = None             # transcript text only, never a paraphrase
    note: str = ""                       # what the evidence shows (description)
    context: tuple[str, ...] = ()        # tags like "topic:mycology", "mood:excited"
    strength: float | None = None        # extractor certainty 0..1; None = not supplied (see confidence.py)
    source_hash: str | None = None       # sha256 of the ORIGINAL media, to detect changed media
    source_date: str | None = None       # ISO date of the recording (for "last observed")
    reviewed_by: str | None = None       # "human:<name>"; required for VERBATIM, forbidden otherwise
    origin: Origin | None = None         # trace back to the upstream record and the original media
    attribution: EvidenceAttribution | None = None   # eligible-creator stamp; required for footage evidence in the store

    def __post_init__(self) -> None:
        if not self.source_id or not self.source_id.strip():
            raise DNAError("evidence needs a source_id")
        if self.strength is not None and not 0.0 <= self.strength <= 1.0:
            raise DNAError("strength must be within 0..1 (or None when not supplied)")
        if self.start_s is not None and self.start_s < 0:
            raise DNAError("start_s must be >= 0")
        if self.end_s is not None:
            if self.start_s is None:
                raise DNAError("end_s given without start_s")
            if self.end_s < self.start_s:
                raise DNAError("end_s must be >= start_s")
        quoted = (Method.VERBATIM, Method.ASR_UNVERIFIED)
        if self.method in quoted and not (self.quote and self.quote.strip()):
            raise DNAError(f"{self.method.value} evidence requires the exact quoted text")
        if self.method in quoted and self.modality is not Modality.TRANSCRIPT:
            raise DNAError(f"{self.method.value} evidence must come from the transcript modality")
        if self.method not in quoted and self.quote is not None:
            raise DNAError("quote is only allowed for transcript evidence; use note for descriptions")
        if self.method is Method.VERBATIM:
            if not self.reviewed_by:
                raise DNAError("verbatim evidence needs reviewed_by: only human-reviewed text counts as verified "
                               "speech; use Method.ASR_UNVERIFIED for engine output")
            check_reviewer(self.reviewed_by)
        elif self.reviewed_by is not None:
            raise DNAError("reviewed_by is only valid on verbatim evidence")
        if self.method is Method.CREATOR_STATEMENT and self.modality is not Modality.FEEDBACK:
            raise DNAError("creator statements use the feedback modality")
        if self.method is not Method.CREATOR_STATEMENT and self.modality is Modality.FEEDBACK:
            raise DNAError("the feedback modality is for creator statements only")
        if self.attribution is not None:
            a = self.attribution
            if self.method is Method.CREATOR_STATEMENT:
                raise DNAError("creator statements are self-report and carry no footage attribution")
            expected = {Modality.TRANSCRIPT: "speech", Modality.AUDIO: "speech", Modality.VISUAL: "visual"}.get(self.modality)
            if expected is None or a.channel != expected:
                raise DNAError(f"a {a.channel} attribution cannot back {self.modality.value} evidence")
            if self.start_s is None:
                raise DNAError("attributed evidence must be timed (start_s/end_s)")
            end = self.end_s if self.end_s is not None else self.start_s
            if self.start_s < a.start_s - 1e-3 or end > a.end_s + 1e-3:
                raise DNAError("evidence lies outside the time range its attribution was decided for")
            if self.origin is not None:
                if self.origin.media_id != a.media_id:
                    raise DNAError("attribution was decided for a different recording than the evidence's origin")
                if self.origin.record_id and self.origin.record_id != a.candidate_record_id:
                    raise DNAError("attribution was decided for a different upstream record than the evidence's origin")
        object.__setattr__(self, "context", tuple(sorted({c.strip() for c in self.context if c.strip()})))

    @property
    def evidence_origin(self) -> str:
        """'creator_self_report' for the creator's own statements, else 'footage'."""
        return "creator_self_report" if self.method is Method.CREATOR_STATEMENT else "footage"

    @property
    def evidence_id(self) -> str:
        """Content-derived id: re-ingesting identical evidence is a no-op."""
        key = "|".join([
            self.polarity.value, self.source_id, self.modality.value, self.method.value,
            "" if self.start_s is None else f"{self.start_s:.3f}",
            "" if self.end_s is None else f"{self.end_s:.3f}",
            normalize_text(self.quote or self.note),
        ])
        return "ev_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id, "modality": self.modality.value,
            "method": self.method.value, "polarity": self.polarity.value,
            "start_s": self.start_s, "end_s": self.end_s, "quote": self.quote,
            "note": self.note, "context": list(self.context), "strength": self.strength,
            "source_hash": self.source_hash, "source_date": self.source_date,
            "reviewed_by": self.reviewed_by,
            "origin": None if self.origin is None else self.origin.to_dict(),
            "attribution": None if self.attribution is None else self.attribution.to_dict(),
            "evidence_origin": self.evidence_origin,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        return cls(
            source_id=d["source_id"], modality=Modality(d["modality"]), method=Method(d["method"]),
            polarity=Polarity(d.get("polarity", "support")), start_s=d.get("start_s"),
            end_s=d.get("end_s"), quote=d.get("quote"), note=d.get("note", ""),
            context=tuple(d.get("context", ())), strength=d.get("strength"),
            source_hash=d.get("source_hash"), source_date=d.get("source_date"),
            reviewed_by=d.get("reviewed_by"),
            origin=None if d.get("origin") is None else Origin.from_dict(d["origin"]),
            attribution=None if d.get("attribution") is None else EvidenceAttribution.from_dict(d["attribution"]),
        )


# Footage-derived methods an OBSERVED fact may rest on. AI estimates and unreviewed ASR can never
# back an "observed" fact, and neither can the creator's own statements (they are self-report).
OBSERVED_METHODS = frozenset({Method.VERBATIM, Method.MEASURED})
INTERPRETIVE_METHODS = frozenset({Method.VERBATIM, Method.MEASURED, Method.AI_ESTIMATE, Method.ASR_UNVERIFIED})
LAYER_METHODS = {
    Layer.OBSERVED: OBSERVED_METHODS,
    Layer.INTERPRETATION: INTERPRETIVE_METHODS,
    Layer.HYPOTHESIS: INTERPRETIVE_METHODS,
    Layer.SELF_REPORT: frozenset({Method.CREATOR_STATEMENT}),
}
# A creator statement may additionally be attached as a remark to any footage trait; it is then
# tracked separately and never counted as footage evidence (see confidence.py).


@dataclass
class TraitCandidate:
    """What an analyzer proposes: a trait statement plus evidence for it."""
    category: Category
    statement: str
    layer: Layer
    evidence: list[Evidence] = field(default_factory=list)
    based_on: tuple[str, ...] = ()       # trait_ids of OBSERVED traits an interpretation rests on
