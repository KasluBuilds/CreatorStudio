"""Research data model: enums, dataclasses and a strict dict/JSON codec.

Pure data, no I/O, no provider knowledge. Everything here is DESIGN-level (PROPOSED), not a
user-approved schema. Dates inside sources may be partial ("2024", "2024-03"), so they are
kept as ISO-prefix strings; see sources.parse_partial_date.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
import math
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = "research-brief/0.4"
SUPPORTED_MIGRATIONS = ("research-brief/0.1", "research-brief/0.2", "research-brief/0.3")


# --------------------------------------------------------------------------- enums
class _E(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class SourceType(_E):
    PRIMARY_RESEARCH = "primary_research"          # journal article, dataset, original study
    PREPRINT = "preprint"                          # arXiv/bioRxiv etc. - not peer reviewed
    OFFICIAL_DOCUMENTATION = "official_documentation"  # vendor docs, standards, specs, manuals
    GOVERNMENT_INSTITUTIONAL = "government_institutional"
    ACADEMIC_INSTITUTION = "academic_institution"
    ORIGINAL_REPORTING = "original_reporting"      # news org doing its own reporting
    SECONDARY_REPORTING = "secondary_reporting"    # aggregators, rewrites, explainers
    REFERENCE_WORK = "reference_work"              # encyclopedias
    EXPERT_COMMENTARY = "expert_commentary"        # identified expert, own name/credentials
    BLOG = "blog"
    COMMERCIAL = "commercial"                      # marketing / product pages
    VIDEO_PLATFORM = "video_platform"
    SOCIAL_COMMUNITY = "social_community"          # forums, social posts, comments
    AI_GENERATED = "ai_generated"
    PUBLISHER_PAGE = "publisher_page"              # page on a journal/institution host, kind not established
    OPINION_COMMENTARY = "opinion_commentary"      # op-eds, columns, editorials
    PERSONAL_PAGE = "personal_page"                # personal/student/staff homepage or blog
    RESOLVER = "resolver"                          # DOI/handle resolver: says nothing about the target
    UNKNOWN = "unknown"


class QualityTier(_E):
    A = "A"  # authoritative: may support a fact on its own
    B = "B"  # reliable: needs a second independent B (or an A) to verify
    C = "C"  # supporting: context / corroboration, never sufficient alone
    D = "D"  # lead only: may point to evidence, never counts as evidence

    @property
    def rank(self) -> int:
        return "ABCD".index(self.value)  # lower = better


class ClaimKind(_E):
    FACT = "fact"
    INTERPRETATION = "interpretation"  # reasoned reading of facts
    OPINION = "opinion"                # someone's stated view (must be attributed)
    HYPOTHESIS = "hypothesis"          # plausible but untested


class ClaimStatus(_E):
    VERIFIED = "verified"
    PARTIAL = "partial"          # credible support, but not enough to verify
    UNVERIFIED = "unverified"
    DISPUTED = "disputed"              # substantial credible evidence on both sides, not resolvable by quality
    CONTRADICTED = "contradicted"      # stronger credible evidence weighs against the claim
    NOT_APPLICABLE = "not_applicable"  # non-fact kinds


class ClaimRisk(_E):
    """How much harm a wrong claim could do. A floor set by risk.classify_risk() cannot be lowered by a
    declaration; only a valid attestation can downgrade it."""
    ORDINARY = "ordinary"
    SAFETY_CRITICAL = "safety_critical"   # toxicity, edibility, health, physical danger


class ReviewStatus(_E):
    """What a citation's review LABEL says. Only a label: HUMAN_REVIEWED and PROCESS_VERIFIED count
    as such only when the citation also carries a valid attestation (see attestation.py)."""
    UNREVIEWED = "unreviewed"        # pointer only; can never establish anything
    LLM_ASSESSED = "llm_assessed"    # a model read it and recorded an assessment (never establishes truth alone)
    HUMAN_REVIEWED = "human_reviewed"
    PROCESS_VERIFIED = "process_verified"


class PerspectiveWeight(_E):
    """How strong the evidence behind a perspective is relative to the others on the same claim."""
    UNSPECIFIED = "unspecified"
    PREVAILING = "prevailing"   # the evidence-weighted view
    COMPARABLE = "comparable"   # equally credible evidence (only legitimate for DISPUTED claims)
    MINORITY = "minority"       # credible but weaker; kept visible, not presented as equal


class Stance(_E):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"


class TopicKind(_E):
    CURRENT_EVENT = "current_event"
    TECHNOLOGY = "technology"
    SCIENCE = "science"
    EVERGREEN = "evergreen"
    FORMAT_STUDY = "format_study"


class BriefType(_E):
    TOPIC_OPPORTUNITY = "topic_opportunity"
    FACT_CHECK = "fact_check"
    REFERENCE_VIDEO = "reference_video"
    ON_DEMAND = "on_demand"


class PerspectiveBasis(_E):
    EVIDENCE_BASED = "evidence_based"
    VALUES_BASED = "values_based"
    FRINGE_MINORITY = "fringe_minority"
    UNKNOWN = "unknown"


class UncertaintyKind(_E):
    GAP = "gap"
    LIMITATION = "limitation"
    DISAGREEMENT = "disagreement"


class VisualKind(_E):
    DIAGRAM = "diagram"
    ANIMATION = "animation"
    CHART = "chart"
    MAP = "map"
    PHOTO = "photo"
    B_ROLL = "b_roll"
    MACRO_FOOTAGE = "macro_footage"
    SCREEN_RECORDING = "screen_recording"
    DEMONSTRATION = "demonstration"


class FootageLinkKind(_E):
    EXISTING_FOOTAGE = "existing_footage"
    FUTURE_SHOOT = "future_shoot"


class BriefStatus(_E):
    DRAFT = "draft"
    VALIDATED = "validated"  # set by the builder only when validation had zero errors


# ------------------------------------------------------------------ attestations
@dataclass
class ProcessRecord:
    """What a documented verification process did. Only meaningful inside a valid process attestation."""
    method_id: str
    method_version: str
    verified_at: str            # ISO date the check ran
    evidence_checked: str       # the record/evidence examined (identifier or description)
    result: str                 # must be "pass" to count
    record_reference: str       # where the checked record can be found again


@dataclass
class Attestation:
    """A signed statement, made by the orchestrator's review boundary, that a human or a registered
    process reviewed ONE specific subject in ONE brief.

    The signature (produced outside the research package) covers the subject digest, attestor, scope,
    brief/context id, attestation id, key id, issue and expiry dates and any process record. The research
    package can only VERIFY: it never holds a signing key. Whether `attestor` is really that person is
    decided by the review service that signed it, not by anything stored here. See attestation.py."""
    kind: str                   # "human" | "process"
    attestor: str               # "human:<name>" | "process:<method_id>" (as asserted by the signer)
    scope: str                  # source | citation | evergreen | independence | claim_kind | risk_downgrade
    issued_at: dt.date
    subject_digest: str
    signature: str
    process: Optional[ProcessRecord] = None
    attestation_id: str = ""    # unique per attestation: the handle for revocation
    context_id: str = ""        # the brief/project this attestation is bound to (replay protection)
    key_id: str = ""            # which signing key; lets the verifier reject rotated/revoked keys
    expires_at: Optional[dt.date] = None


@dataclass
class IndependenceAttestation:
    """A reviewer's signed statement that these specific sources have independent origins."""
    source_ids: list[str]
    attestation: Attestation


@dataclass
class RetrievalRecord:
    """Receipt, signed by the trusted INGESTION boundary (not by research, a provider or an LLM), that a
    page was actually fetched and what it contained."""
    record_id: str
    requested_url: str
    final_url: str
    retrieved_at: dt.date
    content_sha256: str
    content_ref: str            # opaque reference to the stored content (held by the ingestion boundary)
    gatherer: str               # identity of the ingestion component that fetched it
    key_id: str
    signature: str


@dataclass
class ExcerptProof:
    """Ingestion-signed proof that `excerpt` is what the retrieved page contains at `locator`. It ties a
    citation locator to retrieved content."""
    record_id: str
    locator: str
    excerpt: str
    excerpt_sha256: str
    key_id: str
    signature: str


# ------------------------------------------------------------------ source & claims
@dataclass
class Source:
    id: str
    url: str
    title: str
    retrieved_at: dt.date
    source_type: SourceType = SourceType.UNKNOWN
    publisher: str = ""
    authors: list[str] = field(default_factory=list)
    published: Optional[str] = None       # "YYYY" | "YYYY-MM" | "YYYY-MM-DD"; None = unknown
    language: str = ""
    peer_reviewed: Optional[bool] = None  # None = not confirmed
    retracted: bool = False
    derived_from: Optional[str] = None    # id of the source this one merely repeats/cites
    is_primary: bool = False
    classified_by: str = "declared"       # "declared" | "heuristic"
    provider: str = ""                    # which provider surfaced it (provenance)
    notes: str = ""
    # --- untrusted provider metadata (recorded, never used to raise a tier)
    declared_type: Optional[SourceType] = None
    declared_peer_reviewed: Optional[bool] = None
    # --- confirmation: only a human or a named verification process may upgrade a classification.
    # `peer_reviewed` above counts only together with confirmed_by.
    confirmed_by: str = ""                # "human:<name>" | "process:<name>"
    confirmation_note: str = ""
    # --- identity / lineage
    doi: str = ""
    work_id: str = ""                     # identifier of the underlying work (same study = same id)
    version_of: Optional[str] = None      # id of another version of the same work (preprint <-> article)
    original_source_url: str = ""         # the page this one repeats/cites, when not yet an included source
    # --- URL provenance
    requested_url: str = ""               # link as first seen, if different from `url`
    final_url: str = ""                   # where it ended up after redirects, if known
    classification_warnings: list[str] = field(default_factory=list)
    text_origin: str = "provider"         # "provider" (untrusted) | "human"
    attestation: Optional[Attestation] = None   # the only thing that can raise a classification above tier B
    retrieval: Optional[RetrievalRecord] = None  # without a valid one the source is a lead, never evidence
    correction_state: str = ""            # "" | "corrected" | "updated" (part of the signed classification)


@dataclass
class Citation:
    source_id: str
    stance: Stance = Stance.SUPPORTS
    locator: str = ""        # section / page / timestamp - a pointer, not a quotation
    note: str = ""
    assessment: str = ""     # in the assessor's own words: what the source establishes about THIS claim
    review: ReviewStatus = ReviewStatus.UNREVIEWED
    reviewed_by: str = ""
    previous_stance: Optional[Stance] = None   # set when a stance was changed after the fact
    assessed_stance: Optional[Stance] = None   # the stance the reviewer actually assessed; must equal `stance`
    attestation: Optional[Attestation] = None  # required for HUMAN_REVIEWED / PROCESS_VERIFIED to count
    excerpt_proof: Optional[ExcerptProof] = None  # ties `locator` to retrieved content; required to count
    stance_history: list[str] = field(default_factory=list)   # recorded stance changes ("from>to")


@dataclass
class Claim:
    id: str
    text: str
    kind: ClaimKind = ClaimKind.FACT
    status: ClaimStatus = ClaimStatus.UNVERIFIED
    citations: list[Citation] = field(default_factory=list)
    attributed_to: str = ""                          # required for OPINION
    based_on: list[str] = field(default_factory=list)  # claim ids an INTERPRETATION rests on
    time_sensitive: Optional[bool] = None   # None = default (True for non-evergreen briefs)
    evergreen_justification: str = ""        # required to claim time_sensitive=False on a non-evergreen brief
    status_reasons: list[str] = field(default_factory=list)   # derived: why the claim has its status
    evidence_route: str = ""                                    # derived: which verification route applied
    caveats: list[str] = field(default_factory=list)
    testable_by: str = ""                            # HYPOTHESIS: how it could be tested
    evergreen_attestation: Optional[Attestation] = None   # required for time_sensitive=False on a non-evergreen brief
    risk_class: ClaimRisk = ClaimRisk.ORDINARY           # declared; risk.classify_risk() sets a floor
    risk_downgrade_attestation: Optional[Attestation] = None
    original_kind: Optional[ClaimKind] = None            # set when a FACT is reclassified
    kind_attestation: Optional[Attestation] = None       # required for a reviewed FACT -> non-fact reclassification
    dissent_waiver: str = ""                             # explicit reason a credible outweighed dissent has no perspective


@dataclass
class Perspective:
    id: str
    label: str
    summary: str
    basis: PerspectiveBasis = PerspectiveBasis.UNKNOWN
    held_by: str = ""
    claim_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    evidence_assessment: str = ""  # how well supported - avoids false balance
    weight: PerspectiveWeight = PerspectiveWeight.UNSPECIFIED  # strength relative to the others on the claim


@dataclass
class Uncertainty:
    text: str
    kind: UncertaintyKind = UncertaintyKind.GAP
    claim_ids: list[str] = field(default_factory=list)


# ------------------------------------------------------------ creative / hand-off parts
@dataclass
class AudienceProfile:
    description: str
    language: str = "hr"
    knowledge_level: str = ""
    platforms: list[str] = field(default_factory=list)


@dataclass
class CreativeAngle:
    id: str
    title: str
    description: str
    audience_promise: str = ""
    needs_creator_input: bool = True  # the creator supplies the words/opinion, never the system


@dataclass
class HookPayoff:
    """A CONCEPT for the Creative Director - guidance, never dialogue."""
    id: str
    hook_concept: str
    payoff_concept: str
    angle_id: str = ""
    claim_ids: list[str] = field(default_factory=list)


@dataclass
class VisualOpportunity:
    id: str
    description: str
    kind: VisualKind = VisualKind.DIAGRAM
    claim_ids: list[str] = field(default_factory=list)
    reference_source_ids: list[str] = field(default_factory=list)  # leads only, never downloaded
    notes: str = ""


@dataclass
class FootageConnection:
    kind: FootageLinkKind
    description: str
    project_slug: str = ""   # reference only; nothing is read or modified here
    filming_prompt: str = ""  # a question/situation to film, not lines to say


@dataclass
class ReuseDeclaration:
    copies_script: bool = False
    copies_footage: bool = False
    copies_branding: bool = False
    copies_distinctive_expressions: bool = False


@dataclass
class FormatAdaptation:
    what_to_adapt: list[str] = field(default_factory=list)  # abstract structure/ideas only
    croatian_context: str = ""
    differentiation: str = ""
    creator_voice_fit: str = ""


@dataclass
class FormatStudy:
    """Study of a foreign video's *mechanics*. All text is the analyst's own abstraction."""
    reference_source_id: str
    reference_channel: str = ""
    audience_promise: str = ""
    topic_framing: str = ""
    structure_beats: list[str] = field(default_factory=list)
    demonstration_method: str = ""
    visual_explanation: str = ""
    pacing_notes: str = ""
    performance_note: str = ""
    performance_as_of: Optional[str] = None
    adaptation: FormatAdaptation = field(default_factory=FormatAdaptation)
    reuse: ReuseDeclaration = field(default_factory=ReuseDeclaration)


@dataclass
class RelevanceScore:
    overall: float
    interest_fit: float
    audience_potential: float
    freshness: float
    evidence_quality: float
    confidence: float
    data_completeness: float = 0.0       # how much of the scoring input was actually known
    evidence_reliability: float = 0.0    # how much of the evidence has been reviewed and holds up
    excluded: bool = False
    caps_applied: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)


@dataclass
class IndependenceGroup:
    """Sources that share an origin and therefore count as ONE piece of corroboration."""
    group_id: str
    source_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class ResearchBrief:
    brief_id: str
    topic: str
    audience: AudienceProfile
    created_at: dt.date
    brief_type: BriefType = BriefType.TOPIC_OPPORTUNITY
    topic_kind: TopicKind = TopicKind.EVERGREEN
    why_now: str = ""
    controversial: bool = False
    claims: list[Claim] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    perspectives: list[Perspective] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)
    angles: list[CreativeAngle] = field(default_factory=list)
    hooks: list[HookPayoff] = field(default_factory=list)
    visuals: list[VisualOpportunity] = field(default_factory=list)
    footage: list[FootageConnection] = field(default_factory=list)
    format_study: Optional[FormatStudy] = None
    relevance: Optional[RelevanceScore] = None
    queries: list[str] = field(default_factory=list)      # provenance of how it was researched
    status: BriefStatus = BriefStatus.DRAFT
    schema_version: str = SCHEMA_VERSION
    independence: list[IndependenceGroup] = field(default_factory=list)  # derived; recomputed on use
    migrated_from: str = ""
    assessed_under: str = ""          # fingerprint of the TrustPolicy this brief was built/validated under
    independence_attestations: list[IndependenceAttestation] = field(default_factory=list)

    def source(self, source_id: str) -> Optional[Source]:
        return next((s for s in self.sources if s.id == source_id), None)

    def to_dict(self) -> dict:
        return to_plain(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchBrief":
        if not isinstance(data, dict):
            raise ValueError("brief must be a JSON object")
        ver = data.get("schema_version")
        if ver in SUPPORTED_MIGRATIONS:
            if ver == "research-brief/0.1":
                data = migrate_0_1(data)
            if ver in ("research-brief/0.1", "research-brief/0.2"):
                data = migrate_0_2(data)
            data = migrate_0_3(data)
        elif ver != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {ver!r} (supported: {SCHEMA_VERSION}, "
                             f"migratable: {', '.join(SUPPORTED_MIGRATIONS)})")
        return from_plain(cls, data)

    @classmethod
    def from_json(cls, text: str) -> "ResearchBrief":
        return cls.from_dict(json.loads(text))


# ------------------------------------------------------------------------- codec
def to_plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, dt.date):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [to_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    return obj


def migrate_0_3(data: dict) -> dict:
    """research-brief/0.3 -> 0.4. Trust is NOT carried over. 0.3 attestations were not bound to a brief,
    key id, attestation id or expiry, and covered only part of each subject, so they will not verify under
    a 0.4 policy; sources have no retrieval records and citations no excerpt proofs, so nothing in a migrated
    brief can count as evidence until it is re-retrieved and re-attested. The brief records no trust-policy
    fingerprint and will not export until it has been rebuilt under the active policy."""
    import copy
    d = copy.deepcopy(data)
    d["schema_version"] = SCHEMA_VERSION
    d["migrated_from"] = d.get("migrated_from") or "research-brief/0.3"
    for s in d.get("sources", []):
        if isinstance(s, dict) and s.get("attestation"):
            s.setdefault("classification_warnings", []).append(
                "migrated from 0.3: the attestation predates brief binding, key ids and complete subjects and will not verify")
    return d


def migrate_0_2(data: dict) -> dict:
    """research-brief/0.2 -> 0.3. The new fields all default safely. NOTHING is upgraded in trust:
    0.2 'confirmed_by' labels and HUMAN_REVIEWED labels carried no signature, so after migration they
    are unattested (labels only) and claims that relied on them fall back to what the evidence alone
    supports (validation will report STATUS_OVERSTATED / REVIEW_UNATTESTED until re-attested)."""
    import copy
    d = copy.deepcopy(data)
    d["schema_version"] = SCHEMA_VERSION
    d["migrated_from"] = d.get("migrated_from") or "research-brief/0.2"
    for s in d.get("sources", []):
        if isinstance(s, dict) and s.get("confirmed_by"):
            s.setdefault("classification_warnings", []).append(
                "migrated: confirmed_by was an unsigned label; classification needs an attestation to count")
    return d


def migrate_0_1(data: dict) -> dict:
    """research-brief/0.1 -> 0.2. Old briefs load, but trust is NOT silently carried over:
    sources become unconfirmed and citations unreviewed, so previously 'verified' claims fail
    validation until re-assessed. That is intentional (see report, section E)."""
    import copy
    d = copy.deepcopy(data)
    d["schema_version"] = SCHEMA_VERSION
    d["migrated_from"] = "research-brief/0.1"
    for s in d.get("sources", []):
        if isinstance(s, dict):
            s.setdefault("classification_warnings", []).append(
                "migrated from 0.1: classification unconfirmed (provider/caller declared)")
    rel = d.get("relevance")
    if isinstance(rel, dict) and "data_completeness" not in rel:
        rel["data_completeness"] = rel.get("confidence", 0.0)   # the old 'confidence' was completeness
        rel["evidence_reliability"] = 0.0
        rel["confidence"] = 0.0
    return d


def from_plain(tp: Any, data: Any) -> Any:
    """Strict inverse of to_plain. Wrong types, nulls in non-Optional fields, unknown keys, bad
    enum values and missing required fields all raise ValueError."""
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    is_union = origin is typing.Union or origin is types.UnionType
    if data is None:
        if is_union and type(None) in args:
            return None
        raise ValueError("null is not allowed here")
    if is_union:
        inner = [a for a in args if a is not type(None)]
        return from_plain(inner[0], data)
    if origin is list:
        if not isinstance(data, list):
            raise ValueError("expected a list")
        return [from_plain(args[0], x) for x in data]
    if origin is dict:
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return {k: from_plain(args[1], v) for k, v in data.items()}
    if isinstance(tp, type):
        if issubclass(tp, enum.Enum):
            return tp(data)
        if tp is dt.date:
            if not isinstance(data, str):
                raise ValueError("date must be an ISO string")
            return dt.date.fromisoformat(data)
        if tp is str and not isinstance(data, str):
            raise ValueError(f"expected string, got {type(data).__name__}")
        if tp is bool and not isinstance(data, bool):
            raise ValueError(f"expected boolean, got {type(data).__name__}")
        if tp in (int, float):
            if isinstance(data, bool) or not isinstance(data, (int, float)):
                raise ValueError(f"expected number, got {type(data).__name__}")
            if not math.isfinite(data):
                raise ValueError("non-finite number")
        if dataclasses.is_dataclass(tp):
            if not isinstance(data, dict):
                raise ValueError(f"{tp.__name__}: expected an object")
            hints = typing.get_type_hints(tp)
            unknown = set(data) - set(hints)
            if unknown:
                raise ValueError(f"{tp.__name__}: unknown field(s) {sorted(unknown)}")
            try:
                return tp(**{k: from_plain(hints[k], v) for k, v in data.items()})
            except TypeError as e:  # missing required field
                raise ValueError(f"{tp.__name__}: {e}") from None
    return data
