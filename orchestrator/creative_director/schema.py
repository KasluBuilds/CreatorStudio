"""Story-brief data model (Creative Director's OWN schema, independent of the future shared edit plan).

Vocabulary used throughout
- observed   : exists in the supplied transcript (a span of segments, with verbatim text copied by code).
- inferred   : an interpretation of observed material (a reading, a label, a judgement).
- suggested  : a proposal for structure or future filming. Never footage, never creator speech.

Dialogue safety is structural: the only field that carries creator words is `Moment.excerpt`, and
it is filled by code from the transcript and re-checked by validation. Everything else is
description, never speech.
"""
from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "creative_director.story_brief/0.3"          # 0.3 (T-CRD-001): research safety metadata + research_digest
EDITOR_VIEW_CONTRACT = "creative_director.editor_view/0.3"    # 0.3: research_context items carry the safety metadata

OBSERVED, INFERRED, SUGGESTED = "observed", "inferred", "suggested"


class _E(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover
        return self.value


class Mode(_E):
    LONG_FORM = "long_form"
    SHORT_FORM = "short_form"
    BOTH = "both"


class MomentKind(_E):
    CENTRAL_STATEMENT = "central_statement"
    HUMOR_OR_REACTION = "humor_or_reaction"
    QUESTION = "question"
    DEMONSTRATION = "demonstration"
    REVEAL = "reveal"
    OPINION = "opinion"
    EXPLANATION = "explanation"
    CALLBACK_SETUP = "callback_setup"
    CALLBACK_PAYOFF = "callback_payoff"
    OTHER = "other"


class Function(_E):
    HOOK = "hook"
    SETUP = "setup"
    DEVELOPMENT = "development"
    PAYOFF = "payoff"
    RESOLUTION = "resolution"


class ResearchCategory(_E):
    VERIFIED_FACT = "verified_fact"
    PARTIALLY_SUPPORTED = "partially_supported_fact"
    UNVERIFIED_FACT = "unverified_fact"
    DISPUTED = "disputed_claim"
    CONTRADICTED = "contradicted_claim"
    OPINION = "opinion"
    INTERPRETATION = "interpretation"
    HYPOTHESIS = "hypothesis"


@dataclass(frozen=True)
class SourceRef:
    """A span of whole transcript segments of one source. Times are seconds on the ORIGINAL media's
    timeline and are copied from segment boundaries; they are never computed or rounded here."""
    source_id: str
    start_s: float
    end_s: float
    segment_ids: tuple[str, ...]
    verification: str            # "reviewed" | "unverified" | "mixed"

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True)
class Moment:
    moment_id: str
    kind: MomentKind
    ref: SourceRef                      # OBSERVED: where it is
    excerpt: str                        # OBSERVED: verbatim transcript text, copied by code
    attribution: str                    # "creator" | "other" | "unknown" (from speaker labels only)
    interpretation: str                 # INFERRED: what it might be / why it matters
    confidence: float                   # of the interpretation, not of the transcript
    rationale: str
    detected_by: str                    # "heuristic" | "annotation:<annotator>"
    warnings: tuple[str, ...] = ()
    basis: str = INFERRED


@dataclass(frozen=True)
class Pause:
    pause_id: str
    source_id: str
    start_s: float
    end_s: float
    before_segment: str
    after_segment: str
    note: str = ("measured from transcript timestamps only; says nothing about whether the pause is "
                 "meaningful. Check audio before cutting.")
    basis: str = OBSERVED

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True)
class Callback:
    callback_id: str
    setup_moment_id: str
    payoff_moment_id: str
    shared_terms: tuple[str, ...]
    confidence: float
    rationale: str
    detected_by: str
    basis: str = INFERRED


@dataclass(frozen=True)
class CentralIdea:
    statement: str                      # description by the planner, NOT creator speech
    confidence: float
    basis: str
    detected_by: str
    supporting_moment_ids: tuple[str, ...] = ()
    candidate_terms: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    rationale: str = ""


@dataclass(frozen=True)
class Audience:
    description: str | None
    promise: str | None
    basis: str                          # "annotation" | "research_brief" | "unresolved"
    confidence: float
    note: str = ""


@dataclass(frozen=True)
class Finding:
    """An opportunity, weakness, missing-context item or other analysis finding."""
    finding_id: str
    kind: str                           # opportunity | weakness | missing_context | boundary_candidate
    text: str
    moment_ids: tuple[str, ...] = ()
    refs: tuple[SourceRef, ...] = ()
    confidence: float = 0.0
    basis: str = INFERRED
    rationale: str = ""


@dataclass(frozen=True)
class BoundaryOption:
    option_id: str
    which: str                          # "beginning" | "ending"
    description: str
    moment_ids: tuple[str, ...]
    ref: SourceRef | None
    rationale: str
    confidence: float
    basis: str = SUGGESTED


@dataclass(frozen=True)
class RangeUse:
    ref: SourceRef
    reused: bool = False                # same footage used more than once in this arc (e.g. cold open)


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str                          # editorial label for the section, not dialogue
    function: Function
    purpose: str
    moment_ids: tuple[str, ...]
    ranges: tuple[RangeUse, ...]
    position_rationale: str             # why it sits here (required: "explain why each decision helps")
    preserve: tuple[str, ...] = ()      # pause ids / notes the editor must keep
    basis: str = SUGGESTED


@dataclass(frozen=True)
class Arc:
    arc_id: str
    name: str
    order_kind: str                     # "chronological" | "non_linear"
    description: str
    fits_when: str
    sections: tuple[Section, ...]
    reasoning: str
    risks: tuple[str, ...]
    confidence: float
    reorders_source: bool
    basis: str = SUGGESTED


@dataclass(frozen=True)
class Hook:
    hook_id: str
    kind: str                           # "existing_moment" | "concept"
    description: str                    # what the viewer would encounter; never a line of dialogue
    moment_ids: tuple[str, ...]         # footage the hook is grounded in
    rationale: str
    promise_delivered_by: tuple[str, ...]   # moments later in the video that fulfil the hook
    risks: tuple[str, ...] = ()
    confidence: float = 0.0
    relies_on_claim_ids: tuple[str, ...] = ()
    basis: str = SUGGESTED


CONTEXT_ASSESSMENTS = ("dependency_detected", "none_detected", "unknown")


@dataclass(frozen=True)
class StandaloneCheck:
    """`context_assessment` is a WORD-LIST HEURISTIC, never a verification: 'none_detected' means only
    that no dependency was found by the heuristic, not that the clip is understandable on its own."""
    has_hook: bool
    has_payoff: bool
    context_assessment: str             # one of CONTEXT_ASSESSMENTS
    context_basis: str                  # always "heuristic"
    issues: tuple[str, ...]             # problems found
    uncertainties: tuple[str, ...] = () # things the planner could not decide (delayed reactions, speakers, ...)


@dataclass(frozen=True)
class ShortConcept:
    concept_id: str
    title: str                          # label, not dialogue
    ranges: tuple[SourceRef, ...]       # contiguous, source order, nothing reordered
    moment_ids: tuple[str, ...]
    hook_moment_id: str | None
    payoff_moment_id: str | None
    standalone: StandaloneCheck
    duration_s: float
    promotes_long_form: bool            # never forced; default False
    rationale: str
    confidence: float
    warnings: tuple[str, ...] = ()
    preserve: tuple[str, ...] = ()
    basis: str = SUGGESTED


@dataclass(frozen=True)
class StyleConsideration:
    trait_id: str
    category: str
    statement: str
    layer: str
    confidence: float
    scope: str
    eligibility: str                    # "eligible_as_guidance" | "noted_only". NOTHING is applied: see DNA_NOTICE
    reason: str
    basis: str = INFERRED


@dataclass(frozen=True)
class ResearchItem:
    claim_id: str
    text: str                           # copied as DATA from the research packet
    category: ResearchCategory
    usage: str                          # what the planner allows itself to do with it
    attributed_to: str | None = None
    source_ids: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    provenance: tuple[dict, ...] = ()   # per-source metadata exactly as supplied (whitelisted keys), never invented
    conflicts: tuple[str, ...] = ()     # duplicate/conflicting entries seen for this claim id
    presented_as: str = "research_context_not_a_creator_statement"
    basis: str = "research_brief"
    # ---- T-CRD-001: what the Research producer said, kept apart from what this package derives from it.
    # `usage` above is OURS (derived); `producer_usage` is the producer's own string, never replaced or discarded.
    producer_kind: str = "fact"                     # fact | opinion | interpretation | hypothesis | unknown
    producer_status: str | None = None              # the claim status exactly as the packet gave it (cleaned)
    safety_critical: bool | None = None             # None = missing or malformed (fails closed), see safety_metadata
    unresolved_safety_warning: bool | None = None
    safety_metadata: str = "not_applicable"         # ok | missing | malformed | not_applicable
    producer_usage: str | None = None
    usage_check: str = "missing"                    # matches | mismatch | missing | unverifiable
    restricted: bool = False                        # True: capped below verified; never ordinary factual guidance
    restriction_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuidanceItem:
    """FUTURE filming guidance. Not existing footage, not dialogue."""
    guidance_id: str
    kind: str                           # context_pickup | answer_pickup | clarification_pickup | demonstration_check | visual_explanation
    reason: str
    talking_points: tuple[str, ...]     # prompts/topics for the creator, never sentences to say
    related_moment_ids: tuple[str, ...] = ()
    issue_id: str | None = None
    optional: bool = True
    status: str = "future_filming_guidance_not_existing_footage"
    basis: str = SUGGESTED


@dataclass(frozen=True)
class Issue:
    issue_id: str
    severity: str                       # "blocking" | "high" | "medium" | "low"
    kind: str
    text: str
    moment_ids: tuple[str, ...] = ()
    needs: str = ""                     # who/what can resolve it


@dataclass(frozen=True)
class SourceSummary:
    source_id: str
    segments: int
    first_start_s: float
    last_end_s: float
    reviewed_segments: int
    unverified_segments: int


@dataclass(frozen=True)
class StoryBrief:
    schema_version: str
    brief_id: str
    mode: Mode
    project_id: str | None
    status: str                         # "ok" | "insufficient_material"
    sources: tuple[SourceSummary, ...]
    central_idea: CentralIdea | None
    audience: Audience
    moments: tuple[Moment, ...]
    pauses: tuple[Pause, ...]
    callbacks: tuple[Callback, ...]
    findings: tuple[Finding, ...]
    beginnings: tuple[BoundaryOption, ...]
    endings: tuple[BoundaryOption, ...]
    arcs: tuple[Arc, ...]
    recommended_arc_id: str | None
    recommendation_basis: str
    hooks: tuple[Hook, ...]
    short_concepts: tuple[ShortConcept, ...]
    style_considerations: tuple[StyleConsideration, ...]
    research_context: tuple[ResearchItem, ...]
    research_suggestions: tuple[dict, ...]
    filming_guidance: tuple[GuidanceItem, ...]
    rejected_inputs: tuple[dict, ...]
    issues: tuple[Issue, ...]
    warnings: tuple[str, ...]
    confidence: dict[str, Any]
    authenticity: dict[str, Any]
    material_digest: dict[str, Any]     # sha256 of the transcript the brief was planned from
    research_meta: dict[str, Any]
    research_digest: dict[str, Any]    # integrity consistency check over the research claims (NOT authentication)
    dna_notice: str

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


def to_plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    return obj


AUTHENTICITY_STATEMENT = {
    "creator_words": "Only `moments[].excerpt` carries creator words; copied verbatim from the supplied "
                     "transcript by code and re-verified by validate_brief.",
    "everything_else": "Titles, hooks, purposes, talking points and rationales are descriptions or "
                       "prompts. None is, or may be presented as, something the creator said.",
    "timestamps": "Every time range is a whole-segment span copied from the supplied transcript; "
                  "none is estimated.",
    "filming_guidance": "Filming guidance is optional future material, not existing footage.",
    "edit_plan": "This brief is story guidance. It is not an edit plan and contains no cut decisions.",
}


DNA_NOTICE = ("Creator DNA currently supplies labelled considerations only. It does not control arcs, hooks, "
              "short-form concepts, preserve notes or guidance. Confidence-weighted structural influence is "
              "left to a later integration stage.")

ARC_ORDER_KINDS = ("chronological", "non_linear")
HOOK_KINDS = ("existing_moment", "concept")
FINDING_KINDS = ("opportunity", "weakness", "missing_context", "boundary_candidate")
GUIDANCE_KINDS = ("context_pickup", "answer_pickup", "clarification_pickup", "demonstration_check",
                  "visual_explanation")
SEVERITIES = ("blocking", "high", "medium", "low")
ATTRIBUTIONS = ("creator", "other", "unknown")
VERIFICATIONS = ("reviewed", "unverified", "mixed")
AUDIENCE_BASES = ("annotation", "research_brief", "unresolved")
STATUSES = ("ok", "insufficient_material")
BOUNDARY_KINDS = ("beginning", "ending")
