"""Inputs: transcript material, judgements, Creator DNA adapter, Research adapter.

Everything here is read-only with respect to other subsystems: adapters take plain dicts (or an
object with `.export()` / a research packet dict), deep-copy what they need, and never write.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .schema import (ResearchCategory, ResearchItem, SourceRef, StyleConsideration, INFERRED)


class MaterialError(ValueError):
    """The supplied material is unusable (not merely thin)."""


class EvidenceError(ValueError):
    """A reference does not resolve against the supplied transcript."""


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(s: str) -> str:
    return _CTRL.sub("", s)


_BAD_CHARS = [chr(c) for c in range(32)] + [chr(127), '"', "'", "`", chr(92), "<", ">"]
_ID_BAD = re.compile("[" + re.escape("".join(_BAD_CHARS)) + "]")


def valid_identifier(s: object, *, max_len: int = 200) -> bool:
    """Ids (source ids, speakers, annotators, claim ids) are plain labels: non-empty, bounded, and free of
    control characters, quotes, backticks, backslashes and angle brackets, so they can never smuggle
    quoted text into descriptions or markup."""
    return isinstance(s, str) and 0 < len(s.strip()) <= max_len and not _ID_BAD.search(s)


def finite_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def valid_confidence(x: object) -> bool:
    return finite_number(x) and 0 <= x <= 1


# ----------------------------------------------------------------------------- transcript

@dataclass(frozen=True)
class Segment:
    segment_id: str
    source_id: str
    start_s: float
    end_s: float
    text: str
    verification: str = "unverified"   # "reviewed" (a named human reviewed it) | "unverified" (raw ASR)
    speaker: str | None = None

    @staticmethod
    def make(source_id: str, start_s: float, end_s: float, text: str, verification: str = "unverified",
             speaker: str | None = None) -> "Segment":
        for v in (start_s, end_s):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise MaterialError(f"segment times must be finite numbers >= 0 (got {v!r})")
        if end_s <= start_s:
            raise MaterialError(f"segment end must be after start ({source_id} {start_s}-{end_s})")
        if verification not in ("reviewed", "unverified"):
            raise MaterialError("verification must be 'reviewed' or 'unverified'")
        if not valid_identifier(source_id):
            raise MaterialError("source_id must be a plain label (no quotes, control characters or backslashes, "
                                "max 200 chars)")
        if speaker is not None and not valid_identifier(speaker, max_len=100):
            raise MaterialError("speaker must be a plain label or None")
        if not isinstance(text, str):
            raise MaterialError("segment text must be a string")
        return Segment(f"{source_id}@{float(start_s):.3f}-{float(end_s):.3f}", source_id, float(start_s),
                       float(end_s), clean_text(str(text)).strip(), verification, speaker)

    @staticmethod
    def from_any(obj: Any) -> "Segment":
        """Accepts creator_dna.TranscriptSegment or any object/dict with the same attribute names."""
        g = (lambda k, d=None: obj.get(k, d)) if isinstance(obj, Mapping) else (lambda k, d=None: getattr(obj, k, d))
        return Segment.make(g("source_id"), g("start_s"), g("end_s"), g("text", ""),
                            g("verification", "unverified") or "unverified", g("speaker"))


@dataclass(frozen=True)
class Annotation:
    """A judgement (human or model) about a span. It is an INTERPRETATION and says who made it.
    Spans are given by source_id + segment start/end seconds and must match segment boundaries."""
    source_id: str
    start_s: float
    end_s: float
    kind: str                       # a MomentKind value
    rationale: str
    confidence: float
    annotator: str                  # e.g. "human:ana" or "model:claude"


@dataclass(frozen=True)
class ClaimLink:
    """The creator's span asserts / questions / disputes a research claim (judged by annotator)."""
    source_id: str
    start_s: float
    end_s: float
    claim_id: str
    relation: str                   # "asserts" | "questions" | "disputes"
    confidence: float               # the annotator's confidence in the link
    annotator: str


@dataclass(frozen=True)
class CallbackAnnotation:
    setup: tuple[str, float, float]     # source_id, start_s, end_s
    payoff: tuple[str, float, float]
    rationale: str
    confidence: float
    annotator: str


@dataclass(frozen=True)
class Judgement:
    """Optional higher-level judgement supplied from outside (the planner does not guess these)."""
    central_idea: str | None = None
    audience: str | None = None
    audience_promise: str | None = None
    confidence: float = 0.6
    annotator: str = "unknown"


@dataclass
class Material:
    segments: list[Segment]
    creator_speakers: frozenset[str] | None = None   # None = unknown, nothing is attributed to the creator
    annotations: list[Annotation] = field(default_factory=list)
    callback_annotations: list[CallbackAnnotation] = field(default_factory=list)
    claim_links: list[ClaimLink] = field(default_factory=list)
    judgement: Judgement | None = None
    project_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    @staticmethod
    def build(segments: Iterable[Any], *, creator_speakers: Iterable[str] | None = None, **kw: Any) -> "Material":
        segs = [s if isinstance(s, Segment) else Segment.from_any(s) for s in segments]
        ids = [s.segment_id for s in segs]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise MaterialError(f"duplicate segments: {sorted(dup)}")
        segs.sort(key=lambda s: (s.source_id, s.start_s))
        m = Material(segs, None if creator_speakers is None else frozenset(creator_speakers), **kw)
        for a, b in zip(segs, segs[1:]):
            if a.source_id == b.source_id and b.start_s < a.end_s - 1e-6:
                m.warnings.append(f"overlapping segments in {a.source_id}: {a.segment_id} / {b.segment_id}; "
                                  "overlap is kept as supplied and not resolved")
        empty = [s.segment_id for s in segs if not s.text]
        if empty:
            m.warnings.append(f"{len(empty)} segment(s) have empty text and are ignored for analysis")
        return m

    def index(self) -> "TranscriptIndex":
        return TranscriptIndex(self)


class TranscriptIndex:
    """Resolves references. The only way a SourceRef or excerpt comes into existence."""

    def __init__(self, material: Material):
        self.material = material
        self.by_source: dict[str, list[Segment]] = {}
        for s in material.segments:
            self.by_source.setdefault(s.source_id, []).append(s)
        self.by_id = {s.segment_id: s for s in material.segments}
        self._pos = {s.segment_id: i for segs in self.by_source.values() for i, s in enumerate(segs)}

    def segments_of(self, source_id: str) -> list[Segment]:
        return self.by_source.get(source_id, [])

    def ref_for_ids(self, segment_ids: Iterable[str]) -> SourceRef:
        ids = tuple(segment_ids)
        if not ids:
            raise EvidenceError("empty reference")
        try:
            segs = [self.by_id[i] for i in ids]
        except KeyError as e:
            raise EvidenceError(f"unknown segment {e.args[0]!r}") from None
        src = {s.source_id for s in segs}
        if len(src) != 1:
            raise EvidenceError("a reference spans one source only")
        positions = [self._pos[i] for i in ids]
        if positions != list(range(positions[0], positions[0] + len(ids))):
            raise EvidenceError("segments must be contiguous and in source order")
        vs = {s.verification for s in segs}
        return SourceRef(segs[0].source_id, segs[0].start_s, segs[-1].end_s, ids,
                         vs.pop() if len(vs) == 1 else "mixed")

    def ref_for_span(self, source_id: str, start_s: float, end_s: float) -> SourceRef:
        """Resolve seconds to a ref ONLY if both ends sit exactly on segment boundaries."""
        if not isinstance(source_id, str) or not finite_number(start_s) or not finite_number(end_s):
            raise EvidenceError("a reference needs a source id and finite numeric start/end times")
        segs = self.segments_of(source_id)
        if not segs:
            raise EvidenceError(f"unknown source {source_id!r}")
        eps = 1e-6
        first = next((s for s in segs if abs(s.start_s - start_s) <= eps), None)
        last = next((s for s in segs if abs(s.end_s - end_s) <= eps), None)
        if first is None or last is None:
            raise EvidenceError(f"{source_id} {start_s}-{end_s} does not match transcript segment boundaries")
        i, j = self._pos[first.segment_id], self._pos[last.segment_id]
        if j < i:
            raise EvidenceError("span end precedes span start")
        return self.ref_for_ids(s.segment_id for s in segs[i:j + 1])

    def ref_between(self, a: SourceRef, b: SourceRef) -> SourceRef:
        """Smallest ref covering both (same source): includes any segments between them."""
        if a.source_id != b.source_id:
            raise EvidenceError("refs are in different sources")
        ids = [*a.segment_ids, *b.segment_ids]
        lo = min(self._pos[i] for i in ids)
        hi = max(self._pos[i] for i in ids)
        return self.ref_for_ids(s.segment_id for s in self.by_source[a.source_id][lo:hi + 1])

    def excerpt(self, ref: SourceRef) -> str:
        return " ".join(self.by_id[i].text for i in ref.segment_ids if self.by_id[i].text)

    def attribution(self, ref: SourceRef) -> str:
        cs = self.material.creator_speakers
        sp = {self.by_id[i].speaker for i in ref.segment_ids}
        if cs is None or None in sp:
            return "unknown"
        return "creator" if sp <= cs else ("other" if not (sp & cs) else "unknown")

    def next_segment(self, segment_id: str) -> Segment | None:
        segs = self.by_source[self.by_id[segment_id].source_id]
        i = self._pos[segment_id]
        return segs[i + 1] if i + 1 < len(segs) else None

    def prev_segment(self, segment_id: str) -> Segment | None:
        segs = self.by_source[self.by_id[segment_id].source_id]
        i = self._pos[segment_id]
        return segs[i - 1] if i > 0 else None


# ----------------------------------------------------------------------------- Creator DNA

@dataclass(frozen=True)
class DnaTrait:
    trait_id: str
    category: str
    statement: str
    layer: str
    status: str
    confidence: float
    footage_confidence: float
    scope: str
    confidence_basis: str


class CreatorProfileView:
    """Read-only, confidence-weighted view of a Creator DNA export (`DNAStore.export()` shape).

    STATUS: INFORMATIONAL. The view yields labelled `StyleConsideration`s that are copied into the brief
    (`style_considerations`). They do NOT control arcs, hooks, short-form concepts, preserve notes or
    guidance (see schema.DNA_NOTICE). Only confidence, scope, layer, status and confidence_basis are read;
    observation counts and trait context are not used yet.

    Built from a deep copy; the source object/dict is never touched and nothing is written.
    Rejected and corrected traits are excluded. Missing assessment data means confidence 0."""

    def __init__(self, traits: Iterable[DnaTrait], warnings: Iterable[str] = ()):
        self.traits = tuple(traits)
        self.warnings = tuple(warnings)

    @classmethod
    def from_export(cls, export: Mapping[str, Any]) -> "CreatorProfileView":
        data = copy.deepcopy(dict(export))
        traits: list[DnaTrait] = []
        warns: list[str] = []
        for section in ("observed_facts", "ai_interpretations", "hypotheses", "creator_self_report"):
            for t in data.get(section, []) or []:
                try:
                    a = t.get("assessment") or {}
                    conf = a.get("confidence")
                    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0 <= conf <= 1:
                        conf = 0.0
                        warns.append(f"trait {t.get('trait_id')}: no usable confidence; treated as 0")
                    fc = a.get("footage_confidence", conf)
                    traits.append(DnaTrait(str(t["trait_id"]), str(t.get("category", "other")),
                                           clean_text(str(t["statement"])), str(t.get("layer", section)),
                                           str(t.get("status", "active")), float(conf),
                                           float(fc) if isinstance(fc, (int, float)) else 0.0,
                                           str(a.get("scope", "unassessed")), str(a.get("confidence_basis", ""))))
                except (KeyError, TypeError, AttributeError):
                    warns.append(f"skipped a malformed trait entry in {section}")
        return cls(traits, warns)

    @classmethod
    def from_store(cls, store: Any) -> "CreatorProfileView":
        return cls.from_export(store.export())

    def considerations(self, min_guidance_confidence: float = 0.65) -> list[StyleConsideration]:
        out: list[StyleConsideration] = []
        for t in sorted(self.traits, key=lambda x: (-x.confidence, x.trait_id)):
            if t.status in ("rejected", "corrected"):
                continue
            creator_backed = t.confidence_basis in ("footage_and_creator_confirmation", "creator_self_report",
                                                   "creator_confirmation_over_weak_footage")
            if t.layer == "hypothesis":
                applied, why = "noted_only", "hypothesis layer: tentative, never drives a decision"
            elif t.confidence < min_guidance_confidence:
                applied, why = "noted_only", f"confidence {t.confidence:.2f} is below {min_guidance_confidence}"
            elif t.scope == "stable" or creator_backed:
                applied, why = "eligible_as_guidance", ("stable across recordings" if t.scope == "stable"
                                            else "confirmed by the creator")
            elif t.scope == "contextual":
                applied, why = "noted_only", "context-dependent: applies only where the context matches"
            else:
                applied, why = "noted_only", (f"scope '{t.scope}' is not a stable characteristic; one-off or "
                                              "emerging behaviour is not treated as a rule")
            out.append(StyleConsideration(t.trait_id, t.category, t.statement, t.layer, t.confidence,
                                          t.scope, applied, why))
        return out


# ----------------------------------------------------------------------------- digest

DIGEST_SCOPE = "transcript segments (source, times, text, verification, speaker) and creator_speakers"


def material_digest(material: Material) -> dict:
    """sha256 over the transcript the brief is planned from. Annotations, judgements and claim links are
    NOT covered: they are inputs of the planner, not source material."""
    segs = [[s.source_id, s.start_s, s.end_s, s.text, s.verification, s.speaker] for s in material.segments]
    cs = None if material.creator_speakers is None else sorted(material.creator_speakers)
    blob = json.dumps({"segments": segs, "creator_speakers": cs}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return {"algorithm": "sha256", "scope": DIGEST_SCOPE, "value": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            "segments": len(segs)}


# ----------------------------------------------------------------------------- Research

_STATUS_TO_CAT = {"verified": ResearchCategory.VERIFIED_FACT, "partial": ResearchCategory.PARTIALLY_SUPPORTED,
                  "unverified": ResearchCategory.UNVERIFIED_FACT, "disputed": ResearchCategory.DISPUTED,
                  "contradicted": ResearchCategory.CONTRADICTED}
_KIND_TO_CAT = {"opinion": ResearchCategory.OPINION, "interpretation": ResearchCategory.INTERPRETATION,
                "hypothesis": ResearchCategory.HYPOTHESIS}
CATEGORY_USAGE = {
    ResearchCategory.VERIFIED_FACT: "may_be_stated_as_fact_via_separate_sourced_graphic_or_narration_by_creator_only_if_they_choose",
    ResearchCategory.PARTIALLY_SUPPORTED: "attribute_and_hedge",
    ResearchCategory.UNVERIFIED_FACT: "do_not_state_as_fact",
    ResearchCategory.DISPUTED: "present_all_perspectives_do_not_pick_a_side",
    ResearchCategory.CONTRADICTED: "do_not_state_as_fact_flag_if_creator_asserts_it",
    ResearchCategory.OPINION: "label_as_opinion_of_attributed_party",
    ResearchCategory.INTERPRETATION: "label_as_interpretation",
    ResearchCategory.HYPOTHESIS: "label_as_hypothesis"}
# Permissiveness: how freely the planner may treat the claim as fact. LOWER = more restrictive.
# Conflicting duplicates always resolve to the lowest rank (never the more permissive status).
PERMISSIVENESS = {ResearchCategory.CONTRADICTED: 0, ResearchCategory.DISPUTED: 1, ResearchCategory.UNVERIFIED_FACT: 2,
                  ResearchCategory.HYPOTHESIS: 3, ResearchCategory.OPINION: 4, ResearchCategory.INTERPRETATION: 5,
                  ResearchCategory.PARTIALLY_SUPPORTED: 6, ResearchCategory.VERIFIED_FACT: 7}

# ---- T-CRD-001: exact contract, safety metadata, producer-usage rules, research digest -------------------------
# Only this exact contract is read. Anything else (missing, non-string, malformed, older, newer) makes Research
# UNUSABLE: no claim, source, angle or audience from such a packet is used.
RESEARCH_CONTRACT = "research->creative_director/0.3"
SUPPORTED_RESEARCH_CONTRACTS = (RESEARCH_CONTRACT,)

# Mirror of the Research producer's usage rule (research/outputs.py, `_USAGE` and the safety override).
# Copied BY VALUE: this package never imports Research internals. MANUAL SYNCHRONIZATION RISK: if the producer
# changes its rule, this table must be updated by hand together with a new contract version; the tests pin it
# against explicit expected cases in this package only (no cross-package test).
PRODUCER_STATUS_USAGE = {"verified": "may_state_as_fact", "partial": "attribute_and_hedge",
                         "unverified": "do_not_state_as_fact", "disputed": "present_all_perspectives",
                         "contradicted": "report_as_contradicted"}
PRODUCER_UNRESOLVED_SAFETY_USAGE = "unresolved_safety_claim_do_not_recommend"
# OUR usage for a restricted claim; distinct from the producer's own string, which is kept separately.
UNRESOLVED_SAFETY_USAGE = "unresolved_safety_do_not_recommend"
SAFETY_STATES = ("ok", "missing", "malformed", "not_applicable")
USAGE_CHECKS = ("matches", "mismatch", "missing", "unverifiable")
_CAPPABLE = (ResearchCategory.VERIFIED_FACT, ResearchCategory.PARTIALLY_SUPPORTED)


def expected_producer_usage(kind: str, status: str | None, safety_critical: bool | None) -> str | None:
    """What the Research producer's own rule gives for this claim, or None when it cannot be determined."""
    if kind in _KIND_TO_CAT:
        return "label_as_" + kind
    if kind != "fact" or status not in PRODUCER_STATUS_USAGE or not isinstance(safety_critical, bool):
        return None
    if safety_critical and status != "verified":
        return PRODUCER_UNRESOLVED_SAFETY_USAGE
    return PRODUCER_STATUS_USAGE[status]


def check_producer_usage(kind: str, status: str | None, safety_critical: bool | None,
                         supplied: str | None) -> str:
    if supplied is None:
        return "missing"
    exp = expected_producer_usage(kind, status, safety_critical)
    return "unverifiable" if exp is None else ("matches" if supplied == exp else "mismatch")


def restriction_reasons(kind: str, status: str | None, safety_critical: bool | None,
                        unresolved_warning: bool | None, safety_state: str, usage_check: str,
                        metadata_conflict: bool = False) -> tuple[str, ...]:
    """Why a claim is restricted (capped below verified, never ordinary factual guidance); () if it is not.
    A VERIFIED safety-critical claim without a warning is NOT restricted, but may never be a hook."""
    r: list[str] = []
    is_fact = kind not in _KIND_TO_CAT
    if is_fact:
        if kind != "fact":
            r.append("unrecognised claim kind")
        if status not in PRODUCER_STATUS_USAGE:
            r.append("unrecognised claim status")
        if safety_state in ("missing", "malformed"):
            r.append(f"safety metadata {safety_state}")
        if unresolved_warning is True:
            r.append("unresolved safety warning")
        if safety_critical is True and status != "verified":
            r.append("safety-critical claim is not verified")
    if usage_check != "matches":
        r.append(f"producer usage {usage_check}")
    if metadata_conflict:
        r.append("duplicate entries disagree on producer metadata")
    return tuple(r)


def derived_usage(category: ResearchCategory, restricted: bool) -> str:
    return UNRESOLVED_SAFETY_USAGE if restricted else CATEGORY_USAGE[category]


RESEARCH_DIGEST_SCOPE = ("research claims: id, text, attribution, source ids, derived category and usage, "
                         "original producer kind/status/usage, safety metadata, duplicate-metadata conflicts, "
                         "restriction, plus the research contract. An integrity consistency check only: it is NOT "
                         "authentication and not proof of who wrote the packet")
_DIGEST_FIELDS = ("claim_id", "text", "attributed_to", "source_ids", "category", "usage", "producer_kind",
                  "producer_status", "producer_usage",
                  "usage_check", "safety_critical", "unresolved_safety_warning", "safety_metadata",
                  "metadata_conflicts", "restricted", "restriction_reasons")


def compute_research_digest(contract: str | None, items: Iterable[Mapping[str, Any]]) -> dict:
    """Deterministic sha256 over plain research-item dicts (order independent). Used both when a brief is built
    and when it is validated, so the two cannot disagree."""
    rows = sorted(({k: it[k] for k in _DIGEST_FIELDS} for it in items), key=lambda r: r["claim_id"])
    blob = json.dumps({"contract": contract, "claims": rows}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return {"algorithm": "sha256", "scope": RESEARCH_DIGEST_SCOPE, "value": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            "claims": len(rows), "contract": contract}


# ---- the Research Agent's per-source record, as sent in `research->creative_director/*` (research/outputs.py)
#   stance               "supports" | "contradicts" | "context"     (research.models.Stance)
#   review_label         "unreviewed" | "llm_assessed" | "human_reviewed" | "process_verified"
#                        A LABEL only; 'unreviewed' means "pointer only; can never establish anything".
#   verification_level   None | "assessed" | "human" | "process"    (computed upstream under the trust policy)
#   counted_as_evidence  bool = verification_level is not None       (computed upstream)
# Nothing here upgrades a label into evidence. Whether a human_reviewed/process_verified label is backed by a
# valid attestation cannot be checked in this package; we rely on `counted_as_evidence`, which the Research
# Agent recomputes under its trust policy.
STANCES = ("supports", "contradicts", "context")
REVIEW_LABELS = ("unreviewed", "llm_assessed", "human_reviewed", "process_verified")
VERIFICATION_LEVELS = ("assessed", "human", "process")
PROVENANCE_KEYS = ("id", "title", "publisher", "authors", "url", "final_url", "doi", "published", "retrieved_at",
                   "tier", "stance", "locator", "review_label", "verification_level", "counted_as_evidence",
                   "untrusted_text", "classification_confirmed", "conflicts", "duplicate_records")
META_KEYS = ("contract", "brief_id", "topic", "topic_kind", "why_now", "controversial", "perspectives", "relevance")
PERSPECTIVE_KEYS = ("id", "label", "summary", "basis", "held_by", "evidence_assessment", "claim_ids", "source_ids")
_SUPPORT_FIELDS = ("stance", "counted_as_evidence", "review_label", "verification_level")


def _plain(v, depth=0):
    """JSON-plain copy of untrusted data: strings cleaned, depth bounded, odd types dropped to None."""
    if depth > 6:
        return None
    if isinstance(v, str):
        return clean_text(v)[:4000]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    if isinstance(v, (list, tuple)):
        return [_plain(x, depth + 1) for x in list(v)[:100]]
    if isinstance(v, Mapping):
        return {str(k): _plain(x, depth + 1) for k, x in list(v.items())[:50]}
    return None


def source_supports(p: Any) -> tuple[bool, str]:
    """Does this provenance record qualify as SUPPORTING evidence for a verified fact?
    Used both when the packet is read and when a brief is validated, so the two cannot disagree.
    Returns (qualifies, reason-if-not). All of these must hold:
      - stance == "supports"            ("contradicts" and "context" never support)
      - counted_as_evidence is True     (False, missing or non-boolean never supports)
      - review_label is present and recognized (and not "unreviewed")
      - verification_level is present and one of assessed/human/process
      - no conflicting duplicate records for the same source id (stance or counted_as_evidence)"""
    if not isinstance(p, dict):
        return False, "not a source record"
    for c in p.get("conflicts") or []:
        if isinstance(c, str) and c.startswith(("stance", "counted_as_evidence")):
            return False, "conflicting duplicate records"
    stance = p.get("stance")
    if stance != "supports":
        return False, ("stance is contradicts" if stance == "contradicts" else
                       "stance is context only" if stance == "context" else "stance missing or unrecognised")
    if p.get("counted_as_evidence") is not True:
        return False, ("not counted as evidence" if p.get("counted_as_evidence") is False
                       else "counted_as_evidence missing or not a boolean")
    review_label = p.get("review_label")
    if review_label not in REVIEW_LABELS:
        return False, "review_label missing or unrecognised"
    if review_label == "unreviewed":
        return False, "unreviewed"
    if p.get("verification_level") not in VERIFICATION_LEVELS:
        return False, "verification_level missing or unrecognised"
    return True, ""


def _canon(r: Any) -> str:
    return json.dumps(r, sort_keys=True, ensure_ascii=False)


_RANKS = {
    "stance": lambda v: {"contradicts": 0, "context": 1, "supports": 2}.get(v, -1),
    "counted_as_evidence": lambda v: {False: 0, True: 1}.get(v, -1) if isinstance(v, bool) else -1,
    "review_label": lambda v: {"unreviewed": 0, "llm_assessed": 1, "human_reviewed": 2,
                               "process_verified": 3}.get(v, -1) if isinstance(v, str) else -1,
    "verification_level": lambda v: {"assessed": 1, "human": 2, "process": 3}.get(v, -1) if isinstance(v, str) else -1,
}


def _label(v: Any, allowed: tuple) -> str:
    """Echo an untrusted value only if it is one of the known values."""
    return str(v).lower() if v in allowed or isinstance(v, bool) else ("missing" if v is None else "unrecognised")


def merge_provenance(records: list[dict]) -> tuple[list[dict], list[str]]:
    """Merge source records by source id. Deterministic: the result does not depend on input order.

    - Identical duplicates collapse; `duplicate_records` records how many were seen. A duplicate NEVER adds
      supporting evidence: support is judged per source id, after the merge.
    - For each support-deciding field (stance, counted_as_evidence, review_label, verification_level) the LEAST
      supportive value wins; a missing value wins over a present one. Support can only go down by merging.
    - If the records disagree about stance or counted_as_evidence the disagreement is recorded under
      `conflicts`, `counted_as_evidence` is forced to false, and the source cannot count as support (no
      upstream rule is available that could resolve the conflict). Contradicting records are kept.
    - Other fields are filled from the remaining records in a fixed (sorted) order.
    Returns (records sorted by source id, notes about stance/counted conflicts)."""
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r["id"], []).append(r)
    out: list[dict] = []
    notes: list[str] = []
    for sid in sorted(groups):
        g = sorted(groups[sid], key=_canon)
        distinct = [json.loads(x) for x in sorted({_canon(r) for r in g})]
        base = dict(distinct[0])
        if len(g) > 1:
            base["duplicate_records"] = len(g)
        conflicts: list[str] = []
        if len(distinct) > 1:
            for f in _SUPPORT_FIELDS:
                vals = [r.get(f) for r in distinct]
                chosen = min(vals, key=_RANKS[f])
                if chosen is None or _RANKS[f](chosen) == -1:
                    base.pop(f, None) if chosen is None else base.__setitem__(f, chosen)
                else:
                    base[f] = chosen
                if len({_canon(v) for v in vals}) > 1:
                    allowed = {"stance": STANCES, "review_label": REVIEW_LABELS,
                               "verification_level": VERIFICATION_LEVELS, "counted_as_evidence": (True, False)}[f]
                    conflicts.append(f"{f}: " + "|".join(sorted({_label(v, allowed) for v in vals})))
            for r in distinct[1:]:
                for k in sorted(r):
                    if k in _SUPPORT_FIELDS or k in ("conflicts", "duplicate_records"):
                        continue
                    if k not in base:
                        base[k] = r[k]
                    elif _canon(base[k]) != _canon(r[k]):
                        conflicts.append(f"{k} differs")
            if any(c.startswith(("stance", "counted_as_evidence")) for c in conflicts):
                base["counted_as_evidence"] = False
                notes.append(f"source {sid}: records disagree ({'; '.join(c for c in conflicts if c.startswith(('stance', 'counted_as_evidence')))})")
        if conflicts:
            base["conflicts"] = sorted(set(conflicts))
        out.append(base)
    return out, notes


def _parse_sources(srcs: Any) -> tuple[list[dict], list[str]]:
    """Returns (records, reasons for dropped entries). A record without a valid id (or that is not an object)
    is MALFORMED and dropped, never reconstructed. Records with a valid id but incomplete fields are kept as
    provenance and simply cannot count as support (source_supports)."""
    if srcs is None:
        return [], []
    if not isinstance(srcs, (list, tuple)):
        return [], ["sources is not a list"]
    records: list[dict] = []
    dropped: list[str] = []
    for s in srcs:
        if not isinstance(s, Mapping):
            dropped.append("not an object")
            continue
        sid = s.get("id") or s.get("source_id")
        if not valid_identifier(sid):
            dropped.append("missing or invalid id")
            continue
        rec = {"id": str(sid)}
        src = dict(s)
        if "review_label" not in src and "review" in src:        # older packet shape
            src["review_label"] = src["review"]
        for k in PROVENANCE_KEYS[1:]:
            if k in ("conflicts", "duplicate_records"):
                continue                                         # only this package writes these
            if k in src and src[k] is not None:
                rec[k] = _plain(src[k])
        records.append(rec)
    return records, dropped


def _count_reasons(reasons: list[str]) -> str:
    counts: dict[str, int] = {}
    for r in reasons:
        counts[r] = counts.get(r, 0) + 1
    return ", ".join(f"{r} x{n}" for r, n in sorted(counts.items()))


def _flag(c: Mapping[str, Any], key: str) -> Any:
    """A safety flag as supplied: a real bool, else the marker 'missing' / 'malformed' (never coerced)."""
    if key not in c or c[key] is None:
        return "missing"
    return c[key] if isinstance(c[key], bool) else "malformed"


def _merge_flag(vals: list) -> bool | None:
    """Pessimistic merge: any True wins, then any unusable value (None), else False."""
    if any(v is True for v in vals):
        return True
    return None if any(not isinstance(v, bool) for v in vals) else False


def _merge_state(vals: list) -> str:
    if any(v == "malformed" for v in vals):
        return "malformed"
    if any(v == "missing" for v in vals):
        return "missing"
    return "ok"


@dataclass(frozen=True)
class ResearchContext:
    brief_id: str | None
    topic: str | None
    audience_description: str | None
    items: tuple[ResearchItem, ...]
    suggestions: tuple[dict, ...]       # angles / uncertainties, passed through as research suggestions
    warnings: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict)
    issues: tuple[dict, ...] = ()       # {"kind", "severity", "text"}: provenance problems for the brief's issue list

    def item(self, claim_id: str) -> ResearchItem | None:
        return next((i for i in self.items if i.claim_id == claim_id), None)

    @classmethod
    def _unusable(cls, reason: str, kind: str) -> "ResearchContext":
        """Fail closed: NOTHING from the packet is used (no claims, sources, angles, audience, topic)."""
        txt = f"research unusable: {reason}; no research context was used"
        return cls(None, None, None, (), (), (txt,), {}, ({"kind": kind, "severity": "high", "text": txt},))

    @property
    def usable(self) -> bool:
        return not any(i["kind"] in ("research_contract_unsupported", "research_packet_unusable")
                       for i in self.issues)

    @classmethod
    def from_packet(cls, packet: Mapping[str, Any]) -> "ResearchContext":
        """Reads a `research->creative_director/*` packet (dict). Never writes, never invents sources.

        - Every string is untrusted DATA: cleaned and stored, never interpreted.
        - Duplicate claim ids: identical entries collapse; CONFLICTING entries resolve to the most
          restrictive category (PERMISSIVENESS), the conflict is recorded on the item and in warnings.
        - Source records are merged per source id by `merge_provenance` (conservative, order-independent).
        - A 'verified' fact needs at least one source that qualifies under `source_supports`; otherwise it is
          downgraded to unverified and an issue is recorded. Contradicting sources stay in the provenance.
        - Malformed source records (no valid id) are dropped with a warning, an issue and a caveat on the claim."""
        if not isinstance(packet, Mapping):
            return cls._unusable("research packet is not a mapping", "research_packet_unusable")
        p = copy.deepcopy(dict(packet))
        warns: list[str] = []
        issues: list[dict] = []
        contract = p.get("contract")
        if not isinstance(contract, str) or contract not in SUPPORTED_RESEARCH_CONTRACTS:
            shown = (repr(clean_text(contract)[:60]) if isinstance(contract, str)
                     else "missing" if contract is None else f"a non-text {type(contract).__name__}")
            return cls._unusable(f"unsupported research contract ({shown}); only "
                                 f"{RESEARCH_CONTRACT!r} is accepted", "research_contract_unsupported")
        entries: dict[str, list[dict]] = {}
        order: list[str] = []
        for key in ("facts", "interpretations_opinions_hypotheses"):
            seq = p.get(key, []) or []
            if not isinstance(seq, (list, tuple)):
                warns.append(f"{key} is not a list; ignored")
                continue
            for c in seq:
                if not isinstance(c, Mapping) or not valid_identifier(c.get("id")) or not isinstance(c.get("text"), str):
                    warns.append(f"skipped malformed claim in {key}")
                    continue
                cid, text = str(c["id"]), clean_text(c["text"])
                kind, status = str(c.get("kind", "fact")), str(c.get("status", ""))
                caveats = [clean_text(str(x)) for x in (c.get("caveats") or ()) if isinstance(x, (str, int, float))]
                if kind in _KIND_TO_CAT:
                    cat = _KIND_TO_CAT[kind]
                elif status in _STATUS_TO_CAT:
                    cat = _STATUS_TO_CAT[status]
                else:
                    cat = ResearchCategory.UNVERIFIED_FACT
                    warns.append(f"claim {cid}: unknown status {status!r}; treated as unverified")
                records, dropped = _parse_sources(c.get("sources"))
                pkind = kind if (kind in _KIND_TO_CAT or kind == "fact") else "unknown"
                pstatus = clean_text(c["status"])[:40] if isinstance(c.get("status"), str) else None
                pusage = clean_text(c["usage"])[:200] if isinstance(c.get("usage"), str) else None
                if pkind in _KIND_TO_CAT:
                    sc = uw = None
                else:
                    sc, uw = _flag(c, "safety_critical"), _flag(c, "unresolved_safety_warning")
                attributed = c.get("attributed_to")
                attributed = (clean_text(str(attributed))
                              if isinstance(attributed, (str, int, float)) and attributed != "" else None)
                # this entry's own restriction, from its own metadata; a restricted entry is capped here, so a
                # duplicate can never be more permissive than its own metadata allows
                e_sc, e_uw = (None, None) if pkind in _KIND_TO_CAT else (_merge_flag([sc]), _merge_flag([uw]))
                e_state = "not_applicable" if pkind in _KIND_TO_CAT else _merge_state([sc, uw])
                e_reasons = restriction_reasons(pkind, pstatus, e_sc, e_uw, e_state,
                                                check_producer_usage(pkind, pstatus, e_sc, pusage))
                # `eff` is the category after this entry's own cap: used to order and compare duplicates only;
                # `cat` stays as derived so the later "verified without a qualifying source" check still fires
                eff = ResearchCategory.UNVERIFIED_FACT if (e_reasons and cat in _CAPPABLE) else cat
                if cid not in entries:
                    order.append(cid)
                entries.setdefault(cid, []).append({"text": text, "cat": cat, "caveats": caveats, "attr": attributed,
                                                    "records": records, "dropped": dropped, "kind": pkind, "eff": eff,
                                                    "pstatus": pstatus, "pusage": pusage, "sc": sc, "uw": uw,
                                                    "restricted": bool(e_reasons)})
        items: list[ResearchItem] = []
        for cid in order:
            group = entries[cid]
            best = min(group, key=lambda e: (PERMISSIVENESS[e["eff"]], not e["restricted"], e["kind"],
                                             e["pstatus"] or "", e["pusage"] or "", e["text"], e["attr"] or ""))
            cat, text = best["cat"], best["text"]
            caveats = list(best["caveats"]) + sorted({x for e in group for x in e["caveats"]} - set(best["caveats"]))
            attributed = best["attr"] if best["attr"] is not None else min(
                (e["attr"] for e in group if e["attr"] is not None), default=None)
            conflict_notes: list[str] = []
            differing = [e for e in group if (e["eff"], e["text"]) != (best["eff"], text)]
            if len(group) > 1 and not differing:
                warns.append(f"duplicate claim id {cid!r}: identical entries collapsed")
            if differing:
                desc = sorted({e["eff"].value for e in group})
                note = (f"conflicting duplicate entries ({', '.join(desc)}); the most restrictive "
                        f"({best['eff'].value}) was kept")
                warns.append(f"claim {cid}: {note}")
                conflict_notes.append(note)
            # ---- provenance: merge conservatively, report what was dropped or disputed
            # ---- safety metadata: kept as supplied (typed); duplicates merge pessimistically
            pkind = best["kind"]
            same = [e for e in group if (e["kind"] in _KIND_TO_CAT) == (pkind in _KIND_TO_CAT)]
            if pkind in _KIND_TO_CAT:
                sc = uw = None
                sstate = "not_applicable"
            else:
                sc = _merge_flag([e["sc"] for e in same])
                uw = _merge_flag([e["uw"] for e in same])
                sstate = _merge_state([e["sc"] for e in same] + [e["uw"] for e in same])
            pstatus, pusage = best["pstatus"], best["pusage"]
            mconf: list[str] = []
            for label, key, pool in (("kind", "kind", group), ("status", "pstatus", group),
                                     ("usage", "pusage", group), ("safety_critical", "sc", same),
                                     ("unresolved_safety_warning", "uw", same)):
                vals = sorted({"<missing>" if e[key] is None else str(e[key])[:60] for e in pool})
                if len(vals) > 1:
                    mconf.append(f"producer {label}: " + " | ".join(vals))
            ucheck = check_producer_usage(pkind, pstatus, sc, pusage)
            reasons = restriction_reasons(pkind, pstatus, sc, uw, sstate, ucheck, bool(mconf))
            if reasons:
                caveats.append("restricted (" + "; ".join(reasons) + "): never ordinary factual guidance, "
                               "never to be recommended or presented as verified")
                for kindname, sev, hit in (
                        ("research_safety_metadata_invalid", "high",
                         [x for x in reasons if x.startswith("safety metadata")]),
                        ("research_safety_unresolved", "high",
                         [x for x in reasons if "safety" in x and not x.startswith("safety metadata")]),
                        ("research_usage_mismatch", "medium", [x for x in reasons if x.startswith("producer usage")]),
                        ("research_claim_unrecognised", "medium",
                         [x for x in reasons if x.startswith("unrecognised")]),
                        ("research_duplicate_metadata_conflict", "medium",
                         [x for x in reasons if x.startswith("duplicate entries")] + mconf)):
                    if hit:
                        txt = f"claim {cid} is restricted: {'; '.join(hit)}"
                        warns.append(txt)
                        issues.append({"kind": kindname, "severity": sev, "text": txt})
            elif sc is True:
                caveats.append("safety-critical claim: verified, but it may never be used as a hook")
            prov, src_notes = merge_provenance([r for e in group for r in e["records"]])
            dropped = [d for e in group for d in e["dropped"]]
            if dropped:
                txt = (f"claim {cid}: {len(dropped)} malformed source record(s) dropped ({_count_reasons(dropped)}); "
                       "the provenance list is incomplete")
                warns.append(txt)
                issues.append({"kind": "research_source_dropped", "severity": "low", "text": txt})
                caveats.append(f"{len(dropped)} malformed source record(s) dropped; provenance is incomplete")
            for n in src_notes:
                txt = f"claim {cid}: {n}; it cannot count as supporting evidence"
                warns.append(txt)
                issues.append({"kind": "research_source_conflict", "severity": "medium", "text": txt})
            for pv in prov:
                if pv.get("stance") not in STANCES or not isinstance(pv.get("counted_as_evidence"), bool):
                    txt = (f"claim {cid}: source {pv['id']} lacks a recognised stance or a boolean "
                           "counted_as_evidence; it cannot count as supporting evidence")
                    warns.append(txt)
                    issues.append({"kind": "research_source_incomplete", "severity": "low", "text": txt})
            if cat is ResearchCategory.VERIFIED_FACT:
                verdicts = [(pv["id"], source_supports(pv)) for pv in prov]
                if not any(ok for _, (ok, _) in verdicts):
                    why = "; ".join(f"{sid}: {r}" for sid, (_, r) in verdicts) or "no usable source record"
                    cat = ResearchCategory.UNVERIFIED_FACT
                    caveats.append(f"downgraded from verified: no qualifying supporting source ({why})")
                    txt = (f"claim {cid} was marked verified but has no qualifying supporting source ({why}); "
                           "treated as unverified")
                    warns.append(txt)
                    issues.append({"kind": "research_verified_downgraded", "severity": "medium", "text": txt})
            if reasons and cat in _CAPPABLE:         # capped below verified, whatever else was decided above
                cat = ResearchCategory.UNVERIFIED_FACT
            if cat is ResearchCategory.OPINION and attributed is None:
                caveats.append("opinion with no attribution supplied; do not present it as anyone's view")
            items.append(ResearchItem(cid, text, cat, derived_usage(cat, bool(reasons)), attributed,
                                      tuple(pv["id"] for pv in prov), tuple(dict.fromkeys(caveats)), tuple(prov),
                                      tuple(conflict_notes), producer_kind=pkind, producer_status=pstatus,
                                      safety_critical=sc, unresolved_safety_warning=uw, safety_metadata=sstate,
                                      producer_usage=pusage, usage_check=ucheck, restricted=bool(reasons),
                                      restriction_reasons=reasons, metadata_conflicts=tuple(mconf)))
        sugg: list[dict] = []
        for a in p.get("angles", []) or []:
            if isinstance(a, Mapping):
                sugg.append({"type": "angle", "id": _plain(a.get("id")), "title": _plain(str(a.get("title", ""))),
                             "description": _plain(str(a.get("description", ""))),
                             "grounded_in_footage": False, "origin": "research_brief"})
        for u in p.get("uncertainties", []) or []:
            if isinstance(u, Mapping):
                sugg.append({"type": "uncertainty", "kind": _plain(u.get("kind")),
                             "text": _plain(str(u.get("text", ""))),
                             "claim_ids": [str(x) for x in (u.get("claim_ids") or []) if isinstance(x, str)],
                             "origin": "research_brief"})
        meta = {k: _plain(p[k]) for k in ("brief_id", "topic", "topic_kind", "why_now", "controversial", "relevance")
                if k in p}
        meta["contract"] = contract        # exactly the supported version (anything else returned earlier)
        meta["perspectives"] = [{k: _plain(x[k]) for k in PERSPECTIVE_KEYS if k in x}
                                for x in (p.get("perspectives") or []) if isinstance(x, Mapping)]
        aud = p.get("audience") if isinstance(p.get("audience"), Mapping) else {}
        ad = aud.get("description")
        return cls(p.get("brief_id") if isinstance(p.get("brief_id"), str) else None,
                   p.get("topic") if isinstance(p.get("topic"), str) else None,
                   clean_text(ad) if isinstance(ad, str) and ad.strip() else None, tuple(items), tuple(sugg),
                   tuple(warns), meta, tuple(issues))
