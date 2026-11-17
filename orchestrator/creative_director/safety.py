"""Systematic validation of a story brief (and, via `Checker`, of an editor view).

The validator is SCHEMA-DRIVEN: it walks the real dataclass type hints in schema.py, so every field, every
nested object and every list element gets the same treatment, and the validator cannot drift from the
schema. A source reference is checked wherever it appears (a dict value, a list element, a tuple of
ranges) because the check is attached to the TYPE `SourceRef`, not to a position in the document.

Layers
  1. structure : required fields, no unknown keys, types, enums, finite numbers, JSON-plain open dicts.
  2. object    : per-type rules (basis labels, confidence range, heuristic caps, constants, ...).
  3. evidence  : (needs the Material) every reference, excerpt, attribution, pause, source summary and the
                 transcript digest are re-derived from the transcript.
  4. cross     : ids resolve, sections cover their moments, arcs' reorder flags are derivable, short
                 concepts' hooks/payoffs/durations match their ranges, callbacks are ordered, research
                 categories keep their canonical usage, recommended arc exists, text safety.
It never raises on malformed input: problems are returned as strings.

What it cannot prove: that an unquoted paraphrase in a free-text field does not put an opinion in the
creator's mouth. That needs human review (see textsafety.py).
"""
from __future__ import annotations

import dataclasses
import enum
import math
import re
import types
import typing
from typing import Any, get_args, get_origin, get_type_hints

from . import schema as S
from .analysis import terms
from .inputs import (META_KEYS, PERSPECTIVE_KEYS, PROVENANCE_KEYS, SAFETY_STATES, SUPPORTED_RESEARCH_CONTRACTS,
                     USAGE_CHECKS, EvidenceError, Material, TranscriptIndex, check_producer_usage, clean_text,
                     compute_research_digest, derived_usage, material_digest, restriction_reasons, source_supports)
from .textsafety import attribution_risk, norm, transcript_norm

MAX_ERRORS = 200
_DETECTED_BY = re.compile(r"^(?:heuristic|annotation:[^\s\"'`\\<>]{1,200})$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
ELIGIBILITY = ("eligible_as_guidance", "noted_only")
_NON_FACT_KINDS = ("opinion", "interpretation", "hypothesis")
SUGGESTION_BASIS = S.SUGGESTED

# (field, strict) narrative fields per class: checked for dialogue-attribution risk.
NARRATIVE: dict[str, tuple[tuple[str, bool], ...]] = {
    "Moment": (("interpretation", False), ("rationale", False), ("warnings", False)),
    "Callback": (("rationale", False),),
    "CentralIdea": (("statement", False), ("rationale", False), ("alternatives", False)),
    "Audience": (("description", False), ("promise", False), ("note", False)),
    "Finding": (("text", False), ("rationale", False)),
    "BoundaryOption": (("description", False), ("rationale", False)),
    "Section": (("title", False), ("purpose", False), ("position_rationale", False), ("preserve", False)),
    "Arc": (("name", False), ("description", False), ("fits_when", False), ("reasoning", False), ("risks", False)),
    "Hook": (("description", True), ("rationale", False), ("risks", False)),
    "StandaloneCheck": (("issues", False), ("uncertainties", False)),
    "ShortConcept": (("title", False), ("rationale", False), ("warnings", False), ("preserve", False)),
    "StyleConsideration": (("reason", False),),
    "GuidanceItem": (("reason", False), ("talking_points", True)),
    "Issue": (("text", False), ("needs", False)),
}


class Checker:
    def __init__(self, material: Material | None = None):
        self.errs: list[str] = []
        self.material = material
        self.ix = TranscriptIndex(material) if material is not None else None
        self.tnorm = transcript_norm(s.text for s in material.segments) if material is not None else None
        self.research_texts: list[str] = []
        self._hints: dict[type, dict] = {}

    # ------------------------------------------------------------------ plumbing
    def err(self, path: str, msg: str) -> None:
        if len(self.errs) < MAX_ERRORS:
            self.errs.append(f"{path or '/'}: {msg}")

    def hints(self, cls: type) -> dict:
        if cls not in self._hints:
            self._hints[cls] = get_type_hints(cls)
        return self._hints[cls]

    def plain(self, v: Any, path: str, depth: int = 0) -> None:
        if depth > 8:
            return self.err(path, "nesting too deep")
        if v is None or isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            if not math.isfinite(v):
                self.err(path, "non-finite number")
        elif isinstance(v, str):
            if clean_text(v) != v:
                self.err(path, "contains control characters")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                self.plain(x, f"{path}[{i}]", depth + 1)
        elif isinstance(v, dict):
            for k, x in v.items():
                if not isinstance(k, str):
                    self.err(path, "non-string key")
                else:
                    self.plain(x, f"{path}/{k}", depth + 1)
        else:
            self.err(path, f"not JSON-plain ({type(v).__name__})")

    # ------------------------------------------------------------------ structure walk
    def walk(self, v: Any, hint: Any, path: str) -> None:
        if hint is Any:
            return self.plain(v, path)
        origin = get_origin(hint)
        if origin in (typing.Union, types.UnionType):
            args = get_args(hint)
            nn = [a for a in args if a is not type(None)]
            if v is None:
                if len(nn) < len(args):
                    return
                return self.err(path, "may not be null")
            return self.walk(v, nn[0], path)
        if dataclasses.is_dataclass(hint):
            return self.obj(v, hint, path)
        if isinstance(hint, type) and issubclass(hint, enum.Enum):
            if not isinstance(v, str) or v not in {e.value for e in hint}:
                self.err(path, f"must be one of {[e.value for e in hint]}")
            return
        if hint is str:
            if not isinstance(v, str):
                return self.err(path, "must be text")
            if len(v) > 20000 or clean_text(v) != v:
                self.err(path, "text too long or contains control characters")
            return
        if hint is bool:
            if not isinstance(v, bool):
                self.err(path, "must be true/false")
            return
        if hint is int:
            if isinstance(v, bool) or not isinstance(v, int):
                self.err(path, "must be an integer")
            return
        if hint is float:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                self.err(path, "must be a finite number")
            return
        if origin is tuple or hint is tuple:
            if not isinstance(v, list):
                return self.err(path, "must be a list")
            el = get_args(hint)[0] if get_args(hint) else Any
            for i, x in enumerate(v):
                self.walk(x, el, f"{path}[{i}]")
            return
        if origin is dict or hint is dict:
            if not isinstance(v, dict):
                return self.err(path, "must be an object")
            return self.plain(v, path)
        self.err(path, f"unsupported schema type {hint}")

    def obj(self, v: Any, cls: type, path: str) -> None:
        if not isinstance(v, dict):
            return self.err(path, f"must be an object ({cls.__name__})")
        names = [f.name for f in dataclasses.fields(cls)]
        for k in v:
            if k not in names:
                self.err(path, f"unknown key {k!r} in {cls.__name__}")
        for n in names:
            if n not in v:
                self.err(path, f"missing field {n!r} in {cls.__name__}")
        before = len(self.errs)
        hints = self.hints(cls)
        for n in names:
            if n in v:
                self.walk(v[n], hints[n], f"{path}/{n}")
        if len(self.errs) == before:
            self.post(cls, v, path)

    # ------------------------------------------------------------------ object rules
    def post(self, cls: type, v: dict, path: str) -> None:
        c = v.get("confidence")
        if isinstance(c, (int, float)) and not isinstance(c, bool) and not 0 <= c <= 1:
            self.err(path, "confidence must be within 0..1")
        self.narrative(cls.__name__, v, path)
        fn = getattr(self, "post_" + cls.__name__, None)
        if fn:
            fn(v, path)

    def narrative(self, name: str, v: dict, path: str) -> None:
        if name == "Issue" and v.get("kind") == "research_uncertainty":
            return          # carries research text (untrusted data), not a planner statement
        for field, strict in NARRATIVE.get(name, ()):
            if name == "Audience" and field == "description" and v.get("basis") == "research_brief":
                continue
            val = v.get(field)
            for i, s in enumerate(val if isinstance(val, list) else [val]):
                if not isinstance(s, str):
                    continue
                p = f"{path}/{field}" + (f"[{i}]" if isinstance(val, list) else "")
                risk = attribution_risk(s, self.tnorm, strict=strict)
                if risk:
                    self.err(p, f"possible creator dialogue or script: {risk}")
                ns = norm(s)
                for rt in self.research_texts:
                    if rt in ns:
                        self.err(p, "research claim text is embedded in a planner statement")
                        break

    def _basis(self, v: dict, path: str, want: str) -> None:
        if v.get("basis") != want:
            self.err(path + "/basis", f"must be '{want}' (got {v.get('basis')!r})")

    def post_SourceRef(self, v: dict, path: str) -> None:
        if not v["segment_ids"]:
            self.err(path, "empty segment list")
        if v["start_s"] < 0 or v["end_s"] <= v["start_s"]:
            self.err(path, "invalid time range")
        if v["verification"] not in S.VERIFICATIONS:
            self.err(path, "invalid verification status")
        if self.ix is not None:
            try:
                ref = self.ix.ref_for_ids(v["segment_ids"])
            except EvidenceError as e:
                return self.err(path, str(e))
            if (ref.source_id, ref.start_s, ref.end_s) != (v["source_id"], v["start_s"], v["end_s"]):
                self.err(path, "time range or source does not match its transcript segments")
            if ref.verification != v["verification"]:
                self.err(path, "verification status does not match the transcript")

    def post_Moment(self, v: dict, path: str) -> None:
        self._basis(v, path, S.INFERRED)
        if not _DETECTED_BY.match(v["detected_by"]):
            self.err(path + "/detected_by", "must be 'heuristic' or 'annotation:<annotator>'")
        if v["detected_by"] == "heuristic" and v["confidence"] > 0.5:
            self.err(path + "/confidence", "heuristic moments are capped at 0.5")
        if not v["rationale"].strip():
            self.err(path + "/rationale", "missing")
        if v["attribution"] not in S.ATTRIBUTIONS:
            self.err(path + "/attribution", f"must be one of {S.ATTRIBUTIONS}")
        elif v["attribution"] != "creator" and not any("attribution" in w for w in v["warnings"]):
            self.err(path, "non-creator/unknown speech lacks an attribution warning")
        if v["ref"]["verification"] != "reviewed" and not any("unreviewed" in w for w in v["warnings"]):
            self.err(path, "unreviewed transcript text lacks a warning")
        if self.ix is not None:
            try:
                ref = self.ix.ref_for_ids(v["ref"]["segment_ids"])
            except EvidenceError:
                return                       # already reported by the SourceRef check
            if v["excerpt"] != self.ix.excerpt(ref):
                self.err(path + "/excerpt", "is not the transcript text of its range")
            if v["attribution"] != self.ix.attribution(ref):
                self.err(path + "/attribution", "does not match the speaker labels")

    def post_Pause(self, v: dict, path: str) -> None:
        self._basis(v, path, S.OBSERVED)
        if v["end_s"] <= v["start_s"]:
            self.err(path, "invalid time range")
        if self.ix is not None:
            b, a = self.ix.by_id.get(v["before_segment"]), self.ix.by_id.get(v["after_segment"])
            if not b or not a or b.source_id != a.source_id:
                return self.err(path, "does not name two transcript segments of one source")
            if (b.source_id, b.end_s, a.start_s) != (v["source_id"], v["start_s"], v["end_s"]):
                self.err(path, "does not match the gap between its two transcript segments")
            if self.ix.next_segment(b.segment_id) is not a:
                self.err(path, "the two segments are not adjacent")

    def post_Callback(self, v: dict, path: str) -> None:
        self._basis(v, path, S.INFERRED)
        if not _DETECTED_BY.match(v["detected_by"]):
            self.err(path + "/detected_by", "must be 'heuristic' or 'annotation:<annotator>'")
        if v["detected_by"] == "heuristic" and (v["confidence"] > 0.5 or not v["shared_terms"]):
            self.err(path, "heuristic callbacks need shared terms and are capped at 0.5")
        if v["setup_moment_id"] == v["payoff_moment_id"]:
            self.err(path, "setup and payoff are the same moment")

    def post_CentralIdea(self, v: dict, path: str) -> None:
        self._basis(v, path, S.INFERRED)
        if not _DETECTED_BY.match(v["detected_by"]):
            self.err(path + "/detected_by", "must be 'heuristic' or 'annotation:<annotator>'")
        if v["detected_by"] == "heuristic" and v["confidence"] > 0.5:
            self.err(path + "/confidence", "a heuristic central idea is capped at 0.5")
        if self.material is not None and v["detected_by"] == "heuristic" and v["candidate_terms"]:
            have = set().union(*(terms(s.text) for s in self.material.segments)) if self.material.segments else set()
            for t in v["candidate_terms"]:
                if t not in have:
                    self.err(path + "/candidate_terms", f"term {t!r} does not occur in the transcript")

    def post_Audience(self, v: dict, path: str) -> None:
        if v["basis"] not in S.AUDIENCE_BASES:
            self.err(path + "/basis", f"must be one of {S.AUDIENCE_BASES}")
        elif v["basis"] == "unresolved" and (v["description"] is not None or v["promise"] is not None
                                             or v["confidence"] != 0):
            self.err(path, "an unresolved audience cannot carry content or confidence")
        elif v["basis"] == "research_brief" and (v["promise"] is not None or v["confidence"] > 0.5):
            self.err(path, "a research-derived audience has no promise and is capped at 0.5")

    def post_Finding(self, v: dict, path: str) -> None:
        self._basis(v, path, S.INFERRED)
        if v["kind"] not in S.FINDING_KINDS:
            self.err(path + "/kind", f"must be one of {S.FINDING_KINDS}")

    def post_BoundaryOption(self, v: dict, path: str) -> None:
        self._basis(v, path, S.SUGGESTED)
        if v["which"] not in S.BOUNDARY_KINDS:
            self.err(path + "/which", f"must be one of {S.BOUNDARY_KINDS}")

    def post_Section(self, v: dict, path: str) -> None:
        self._basis(v, path, S.SUGGESTED)
        if not v["position_rationale"].strip():
            self.err(path, "missing position rationale")
        if not v["ranges"]:
            self.err(path, "a section needs at least one range")

    def post_Arc(self, v: dict, path: str) -> None:
        self._basis(v, path, S.SUGGESTED)
        if v["order_kind"] not in S.ARC_ORDER_KINDS:
            self.err(path + "/order_kind", f"must be one of {S.ARC_ORDER_KINDS}")
        if not v["reasoning"].strip() or not v["sections"]:
            self.err(path, "an arc needs reasoning and at least one section")

    def post_Hook(self, v: dict, path: str) -> None:
        self._basis(v, path, S.SUGGESTED)
        if v["kind"] not in S.HOOK_KINDS:
            self.err(path + "/kind", f"must be one of {S.HOOK_KINDS}")
        if not v["moment_ids"]:
            self.err(path, "a hook must be grounded in at least one moment")
        if not v["rationale"].strip():
            self.err(path + "/rationale", "missing")

    def post_StandaloneCheck(self, v: dict, path: str) -> None:
        if v["context_assessment"] not in S.CONTEXT_ASSESSMENTS:
            self.err(path + "/context_assessment", f"must be one of {S.CONTEXT_ASSESSMENTS}")
        if v["context_basis"] != "heuristic":
            self.err(path + "/context_basis", "context assessment is a heuristic and must say so")

    def post_ShortConcept(self, v: dict, path: str) -> None:
        self._basis(v, path, S.SUGGESTED)
        if not v["ranges"]:
            self.err(path, "a short concept needs at least one range")
        if v["duration_s"] < 0:
            self.err(path + "/duration_s", "negative")
        if not v["rationale"].strip():
            self.err(path + "/rationale", "missing")

    def post_StyleConsideration(self, v: dict, path: str) -> None:
        self._basis(v, path, S.INFERRED)
        if v["eligibility"] not in ELIGIBILITY:
            self.err(path + "/eligibility", f"must be one of {ELIGIBILITY}")
        if v["layer"] == "hypothesis" and v["eligibility"] != "noted_only":
            self.err(path, "a hypothesis trait can only be noted")

    def post_ResearchItem(self, v: dict, path: str) -> None:
        if v["basis"] != "research_brief":
            self.err(path + "/basis", "must be 'research_brief'")
        if v["presented_as"] != "research_context_not_a_creator_statement":
            self.err(path + "/presented_as", "research claims are never creator statements")
        self.research_safety(v, path)
        pids = []
        for i, pv in enumerate(v["provenance"]):
            if not isinstance(pv, dict) or not isinstance(pv.get("id"), str) or set(pv) - set(PROVENANCE_KEYS):
                self.err(f"{path}/provenance[{i}]", "must be an object with an id and only known provenance keys")
                continue
            pids.append(pv["id"])
            if "conflicts" in pv and (not isinstance(pv["conflicts"], list)
                                      or not all(isinstance(x, str) for x in pv["conflicts"])):
                self.err(f"{path}/provenance[{i}]/conflicts", "must be a list of text")
            if "duplicate_records" in pv and (isinstance(pv["duplicate_records"], bool)
                                              or not isinstance(pv["duplicate_records"], int)
                                              or pv["duplicate_records"] < 2):
                self.err(f"{path}/provenance[{i}]/duplicate_records", "must be an integer >= 2")
            conflicted = any(isinstance(x, str) and x.startswith(("stance", "counted_as_evidence"))
                             for x in pv.get("conflicts", []) if isinstance(pv.get("conflicts"), list))
            if conflicted and pv.get("counted_as_evidence") is not False:
                self.err(f"{path}/provenance[{i}]", "a source with conflicting records cannot count as evidence")
        if len(pids) != len(set(pids)):
            self.err(path + "/provenance", "duplicate source ids (records must be merged per source)")
        if v["category"] == "verified_fact":
            if not v["source_ids"]:
                self.err(path, "a verified fact needs at least one source reference")
            elif not any(source_supports(pv)[0] for pv in v["provenance"] if isinstance(pv, dict)):
                self.err(path, "a verified fact needs at least one supporting source record (stance 'supports', "
                               "counted_as_evidence true, not unreviewed, no conflicting duplicate records)")
        if set(pids) != set(v["source_ids"]):
            self.err(path, "source_ids and provenance ids differ")

    def research_safety(self, v: dict, path: str) -> None:
        """T-CRD-001: every derived field of a research item is recomputed from the preserved producer fields,
        so a tampered category, usage, flag or restriction is caught. Fails closed."""
        kind, status = v["producer_kind"], v["producer_status"]
        sc, uw, state = v["safety_critical"], v["unresolved_safety_warning"], v["safety_metadata"]
        nonfact = kind in _NON_FACT_KINDS
        if kind not in ("fact", "unknown") + _NON_FACT_KINDS:
            self.err(path + "/producer_kind", "unrecognised claim kind")
        if state not in SAFETY_STATES:
            return self.err(path + "/safety_metadata", f"must be one of {SAFETY_STATES}")
        if v["usage_check"] not in USAGE_CHECKS:
            return self.err(path + "/usage_check", f"must be one of {USAGE_CHECKS}")
        if nonfact:
            if state != "not_applicable" or sc is not None or uw is not None:
                self.err(path + "/safety_metadata", "an opinion/interpretation/hypothesis carries no safety flags")
        elif state == "not_applicable":
            self.err(path + "/safety_metadata", "a factual claim needs safety metadata or a missing/malformed marker")
        elif state == "ok":
            if not isinstance(sc, bool) or not isinstance(uw, bool):
                self.err(path + "/safety_metadata", "'ok' requires both safety flags to be true/false")
        elif sc is not None and uw is not None:
            self.err(path + "/safety_metadata", "marked missing/malformed although both flags are present")
        expected = check_producer_usage(kind, status, sc, v["producer_usage"])
        if v["usage_check"] != expected:
            self.err(path + "/usage_check", f"does not match the producer usage rule (expected {expected!r})")
        reasons = restriction_reasons(kind, status, sc, uw, state, expected)
        if tuple(v["restriction_reasons"]) != reasons:
            self.err(path + "/restriction_reasons", "do not match the preserved safety metadata and producer usage")
        if v["restricted"] is not bool(reasons):
            self.err(path + "/restricted", "does not match the preserved safety metadata and producer usage")
        cat = S.ResearchCategory(v["category"])
        if v["usage"] != derived_usage(cat, bool(reasons)):
            self.err(path + "/usage", "does not match the canonical usage rule for this category")
        if reasons and cat in (S.ResearchCategory.VERIFIED_FACT, S.ResearchCategory.PARTIALLY_SUPPORTED):
            self.err(path + "/category", "a restricted claim (unresolved safety, missing or malformed safety "
                                         "metadata, producer usage mismatch) cannot be above unverified")
        if cat is S.ResearchCategory.VERIFIED_FACT and (kind != "fact" or status != "verified"):
            self.err(path + "/category", "verified_fact requires the producer to have said fact/verified")
        if nonfact != (cat in (S.ResearchCategory.OPINION, S.ResearchCategory.INTERPRETATION,
                               S.ResearchCategory.HYPOTHESIS)):
            self.err(path + "/category", "category does not fit the producer's claim kind")

    def post_GuidanceItem(self, v: dict, path: str) -> None:
        self._basis(v, path, S.SUGGESTED)
        if v["kind"] not in S.GUIDANCE_KINDS:
            self.err(path + "/kind", f"must be one of {S.GUIDANCE_KINDS}")
        if v["status"] != "future_filming_guidance_not_existing_footage":
            self.err(path + "/status", "must be labelled future filming guidance")
        if v["optional"] is not True:
            self.err(path + "/optional", "filming guidance is optional")
        if not v["talking_points"]:
            self.err(path, "guidance needs at least one talking point")

    def post_Issue(self, v: dict, path: str) -> None:
        if v["severity"] not in S.SEVERITIES:
            self.err(path + "/severity", f"must be one of {S.SEVERITIES}")
        if not v["kind"].strip():
            self.err(path + "/kind", "missing")

    def post_SourceSummary(self, v: dict, path: str) -> None:
        if min(v["segments"], v["reviewed_segments"], v["unverified_segments"]) < 0 or \
                v["reviewed_segments"] + v["unverified_segments"] != v["segments"]:
            self.err(path, "inconsistent segment counts")
        if self.ix is not None:
            segs = self.ix.segments_of(v["source_id"])
            if not segs:
                return self.err(path, "source is not in the transcript")
            exp = (len(segs), segs[0].start_s, segs[-1].end_s, sum(s.verification == "reviewed" for s in segs),
                   sum(s.verification != "reviewed" for s in segs))
            if exp != (v["segments"], v["first_start_s"], v["last_end_s"], v["reviewed_segments"],
                       v["unverified_segments"]):
                self.err(path, "does not match the transcript")

    # ------------------------------------------------------------------ helpers for cross checks
    def unique(self, items: list, key: str, path: str) -> set:
        seen: set = set()
        for it in items:
            k = it.get(key)
            if k in seen:
                self.err(path, f"duplicate {key} {k!r}")
            seen.add(k)
        return seen

    def need(self, ids, known: set, path: str, what: str = "moment id") -> None:
        for i in ids:
            if i not in known:
                self.err(path, f"unknown {what} {i!r}")

    def digest(self, d: dict, path: str) -> None:
        keys = {"algorithm", "scope", "value", "segments"}
        if set(d) != keys:
            return self.err(path, f"must have exactly the keys {sorted(keys)}")
        if d["algorithm"] != "sha256" or not isinstance(d["value"], str) or not _SHA.match(d["value"]) or \
                isinstance(d["segments"], bool) or not isinstance(d["segments"], int) or d["segments"] < 0:
            return self.err(path, "malformed digest")
        if self.material is not None:
            if material_digest(self.material) != d:
                self.err(path, "source material changed since this was planned (transcript digest mismatch)")


def check_research_digest(c: "Checker", d: dict) -> None:
    """The research digest must be exactly what the research claims and contract hash to. This catches edits to
    any digest-covered field (category, usage, original status, safety metadata, restriction, contract). It is an
    INTEGRITY CONSISTENCY check, not authentication: anyone able to rewrite the brief can recompute it."""
    dg = d["research_digest"]
    keys = {"algorithm", "scope", "value", "claims", "contract"}
    if not isinstance(dg, dict) or set(dg) != keys:
        return c.err("/research_digest", f"must have exactly the keys {sorted(keys)}")
    if dg != compute_research_digest(d["research_meta"].get("contract"), d["research_context"]):
        c.err("/research_digest", "research claims, safety metadata or contract changed since planning "
                                  "(digest mismatch; integrity check only, not authentication)")


def check_hook_claims(c: "Checker", h: dict, hp: str, research_context: list) -> None:
    """A hook may rely only on a claim that is a verified fact AND not safety-critical, not warned, not
    restricted and carrying valid safety metadata. A verified safety-critical claim may stay verified, but it can
    never be a hook."""
    for cid in h["relies_on_claim_ids"]:
        rc = next((r for r in research_context if r["claim_id"] == cid), None)
        if rc is None or rc["category"] != "verified_fact":
            c.err(hp, f"relies on claim {cid!r} which is not a verified fact")
        elif (rc["safety_critical"] is not False or rc["unresolved_safety_warning"] is not False
              or rc["restricted"] or rc["safety_metadata"] != "ok"):
            c.err(hp, f"relies on claim {cid!r} which is safety-critical, carries an unresolved warning, is "
                      "restricted or lacks valid safety metadata; such claims can never be hooks")


def check_research_contract(c: "Checker", d: dict) -> None:
    meta = d["research_meta"]
    if "contract" in meta and meta["contract"] not in SUPPORTED_RESEARCH_CONTRACTS:
        c.err("/research_meta/contract", f"only {SUPPORTED_RESEARCH_CONTRACTS} is accepted")
    if d["research_context"] and "contract" not in meta:
        c.err("/research_meta/contract", "research claims present without the supported research contract")


def _segset(refs) -> set[str]:
    return {i for r in refs for i in r["segment_ids"]}


# ====================================================================================== brief
def brief_cross(c: Checker, d: dict) -> None:
    """Cross-field rules. Only called when the structure is valid, so keys exist and have the right types."""
    if d["schema_version"] != S.SCHEMA_VERSION:
        c.err("/schema_version", f"{d['schema_version']!r} != {S.SCHEMA_VERSION!r}")
    if d["status"] not in S.STATUSES:
        c.err("/status", f"must be one of {S.STATUSES}")
    c.digest(d["material_digest"], "/material_digest")
    if d["dna_notice"] != S.DNA_NOTICE:
        c.err("/dna_notice", "must carry the Creator DNA informational notice")
    if d["authenticity"] != S.AUTHENTICITY_STATEMENT:
        c.err("/authenticity", "must equal the authenticity statement")
    conf = d["confidence"]
    if set(conf) != {"overall", "central_idea", "median_moment", "basis"}:
        c.err("/confidence", "unexpected keys")
    else:
        for k in ("overall", "central_idea"):
            if not isinstance(conf[k], (int, float)) or isinstance(conf[k], bool) or not 0 <= conf[k] <= 1:
                c.err(f"/confidence/{k}", "must be within 0..1")
        m = conf["median_moment"]
        if m is not None and (not isinstance(m, (int, float)) or isinstance(m, bool) or not 0 <= m <= 1):
            c.err("/confidence/median_moment", "must be within 0..1 or null")
        if not isinstance(conf["basis"], str):
            c.err("/confidence/basis", "must be text")
    for i, r in enumerate(d["rejected_inputs"]):
        if not isinstance(r, dict) or set(r) != {"input", "reason"} or not all(isinstance(x, str) for x in r.values()):
            c.err(f"/rejected_inputs[{i}]", "must be {input, reason} text")
    _research_blocks(c, d)
    check_research_contract(c, d)
    check_research_digest(c, d)

    moments, mids = d["moments"], c.unique(d["moments"], "moment_id", "/moments")
    byid = {m["moment_id"]: m for m in moments}
    pause_ids = c.unique(d["pauses"], "pause_id", "/pauses")
    c.unique(d["callbacks"], "callback_id", "/callbacks")
    c.unique(d["findings"], "finding_id", "/findings")
    c.unique(d["sources"], "source_id", "/sources")
    arc_ids = c.unique(d["arcs"], "arc_id", "/arcs")
    c.unique(d["hooks"], "hook_id", "/hooks")
    c.unique(d["short_concepts"], "concept_id", "/short_concepts")
    c.unique(d["filming_guidance"], "guidance_id", "/filming_guidance")
    issue_ids = c.unique(d["issues"], "issue_id", "/issues")
    c.unique(d["beginnings"] + d["endings"], "option_id", "/beginnings+endings")
    del pause_ids

    # mode / status consistency
    mode = d["mode"]
    if d["status"] == "insufficient_material":
        if any(d[k] for k in ("moments", "arcs", "hooks", "short_concepts", "callbacks", "filming_guidance")):
            c.err("/status", "insufficient material cannot carry a plan")
        if not any(i["severity"] == "blocking" for i in d["issues"]):
            c.err("/issues", "insufficient material requires a blocking issue")
    if mode == "short_form" and (d["arcs"] or d["hooks"] or d["recommended_arc_id"] is not None):
        c.err("/mode", "short_form briefs carry no long-form arcs, hooks or recommendation")
    if mode == "long_form" and d["short_concepts"]:
        c.err("/mode", "long_form briefs carry no short concepts")
    rec = d["recommended_arc_id"]
    if d["arcs"]:
        if rec not in arc_ids:
            c.err("/recommended_arc_id", f"{rec!r} is not an arc in this brief")
        if not d["recommendation_basis"].strip():
            c.err("/recommendation_basis", "missing")
    elif rec is not None:
        c.err("/recommended_arc_id", "there are no arcs")

    # evidence links
    ci = d["central_idea"]
    if ci is not None:
        c.need(ci["supporting_moment_ids"], mids, "/central_idea/supporting_moment_ids")
    elif d["status"] == "ok":
        c.err("/central_idea", "required when status is ok")
    for cb in d["callbacks"]:
        p = f"/callbacks[{cb['callback_id']}]"
        c.need([cb["setup_moment_id"], cb["payoff_moment_id"]], mids, p)
        s, y = byid.get(cb["setup_moment_id"]), byid.get(cb["payoff_moment_id"])
        if s and y:
            if s["ref"]["source_id"] == y["ref"]["source_id"] and y["ref"]["start_s"] < s["ref"]["end_s"]:
                c.err(p, "payoff does not come after setup in the source")
            if cb["detected_by"] == "heuristic":
                for t in cb["shared_terms"]:
                    if t not in terms(s["excerpt"]) or t not in terms(y["excerpt"]):
                        c.err(p, f"shared term {t!r} is not in both moments")
    for f in d["findings"]:
        c.need(f["moment_ids"], mids, f"/findings[{f['finding_id']}]")
    for lst, which in ((d["beginnings"], "beginning"), (d["endings"], "ending")):
        for o in lst:
            c.need(o["moment_ids"], mids, f"/{which}s[{o['option_id']}]")
            if o["which"] != which:
                c.err(f"/{which}s[{o['option_id']}]", f"listed as {which} but marked {o['which']}")
    for i in d["issues"]:
        c.need(i["moment_ids"], mids, f"/issues[{i['issue_id']}]")
    for g in d["filming_guidance"]:
        p = f"/filming_guidance[{g['guidance_id']}]"
        c.need(g["related_moment_ids"], mids, p)
        if g["issue_id"] is not None and g["issue_id"] not in issue_ids:
            c.err(p, f"unknown issue id {g['issue_id']!r}")

    for arc in d["arcs"]:
        check_arc(c, arc, d, byid, mids)

    for h in d["hooks"]:
        hp = f"/hooks[{h['hook_id']}]"
        c.need(h["moment_ids"], mids, hp)
        c.need(h["promise_delivered_by"], mids, hp)
        check_hook_claims(c, h, hp, d["research_context"])

    for sc in d["short_concepts"]:
        check_short(c, sc, byid, mids)

    # research
    c.unique(d["research_context"], "claim_id", "/research_context")


def check_arc(c: Checker, arc: dict, d: dict, byid: dict, mids: set) -> None:
    """Section/range/reorder/callback rules for one arc. `d` supplies the callbacks."""
    ap = f"/arcs[{arc['arc_id']}]"
    c.unique(arc["sections"], "section_id", ap)
    seq, per_seg = [], {}
    for sec in arc["sections"]:
        sp = f"{ap}/{sec['section_id']}"
        c.need(sec["moment_ids"], mids, sp)
        covered = _segset(u["ref"] for u in sec["ranges"])
        for mid in sec["moment_ids"]:
            m = byid.get(mid)
            if m and not set(m["ref"]["segment_ids"]) <= covered:
                c.err(sp, f"moment {mid} is not inside the section's ranges")
        for u in sec["ranges"]:
            seq.append((u["ref"]["source_id"], u["ref"]["start_s"]))
            for sid in u["ref"]["segment_ids"]:
                per_seg.setdefault(sid, []).append(u["reused"])
    for sid, flags in per_seg.items():
        if len(flags) > 1 and not any(flags):
            c.err(ap, f"segment {sid} is used more than once but no use is marked reused")
    for sec in arc["sections"]:
        for u in sec["ranges"]:
            if u["reused"] and not any(len(per_seg[sid]) > 1 for sid in u["ref"]["segment_ids"]):
                c.err(f"{ap}/{sec['section_id']}", "a range is marked reused but nothing else uses its footage")
    derived = any(u["reused"] for s in arc["sections"] for u in s["ranges"]) or seq != sorted(seq)
    if arc["reorders_source"] != derived:
        c.err(ap + "/reorders_source", f"declared {arc['reorders_source']} but the sections imply {derived}")
    if (arc["order_kind"] == "non_linear") != derived:
        c.err(ap + "/order_kind", "order_kind does not match the section order")
    order = [mid for s in arc["sections"] for mid in s["moment_ids"]]
    for cb in d["callbacks"]:
        su, pa = cb["setup_moment_id"], cb["payoff_moment_id"]
        if su in byid and pa in byid and byid[su]["ref"]["source_id"] == byid[pa]["ref"]["source_id"] \
                and su in order and pa in order and order.index(su) > order.index(pa):
            c.err(ap, f"payoff {pa} comes before its setup {su}")


def check_short(c: Checker, sc: dict, byid: dict, mids: set) -> None:
    """Hook/payoff/range/duration rules for one short-form concept."""
    sp = f"/short_concepts[{sc['concept_id']}]"
    c.need(sc["moment_ids"], mids, sp)
    covered = _segset(sc["ranges"])
    for mid in sc["moment_ids"]:
        m = byid.get(mid)
        if m and not set(m["ref"]["segment_ids"]) <= covered:
            c.err(sp, f"moment {mid} is not inside the concept's ranges")
    for role in ("hook_moment_id", "payoff_moment_id"):
        mid = sc[role]
        if mid is not None:
            c.need([mid], mids, sp)
            if mid not in sc["moment_ids"]:
                c.err(sp, f"{role} {mid} is not among the concept's moments")
    if sc["hook_moment_id"] is not None and sc["hook_moment_id"] == sc["payoff_moment_id"]:
        c.err(sp, "hook and payoff are the same moment")
    st = sc["standalone"]
    if st["has_hook"] != (sc["hook_moment_id"] is not None):
        c.err(sp, "standalone.has_hook disagrees with hook_moment_id")
    if st["has_payoff"] != (sc["payoff_moment_id"] is not None):
        c.err(sp, "standalone.has_payoff disagrees with payoff_moment_id")
    if not st["has_hook"] and not st["has_payoff"]:
        c.err(sp, "a concept with neither hook nor payoff must not be offered")
    total = sum(r["end_s"] - r["start_s"] for r in sc["ranges"])
    if abs(total - sc["duration_s"]) > 0.01:
        c.err(sp + "/duration_s", f"{sc['duration_s']} does not match its ranges ({total:.3f})")
    rs = sc["ranges"]
    for a, b in zip(rs, rs[1:]):
        if a["source_id"] == b["source_id"] and b["start_s"] < a["end_s"]:
            c.err(sp, "ranges are reordered or overlap")
    if st["context_assessment"] == "dependency_detected" and not st["issues"]:
        c.err(sp, "a detected context dependency must be listed among the issues")


def _research_blocks(c: Checker, d: dict) -> None:
    meta = d["research_meta"]
    if set(meta) - set(META_KEYS):
        c.err("/research_meta", f"unknown keys {sorted(set(meta) - set(META_KEYS))}")
    for i, p in enumerate(meta.get("perspectives", []) or []):
        if not isinstance(p, dict) or set(p) - set(PERSPECTIVE_KEYS):
            c.err(f"/research_meta/perspectives[{i}]", "must be an object with only known perspective keys")
    angle = {"type", "id", "title", "description", "grounded_in_footage", "origin"}
    unc = {"type", "kind", "text", "claim_ids", "origin"}
    for i, s in enumerate(d["research_suggestions"]):
        if not isinstance(s, dict) or s.get("type") not in ("angle", "uncertainty"):
            c.err(f"/research_suggestions[{i}]", "type must be 'angle' or 'uncertainty'")
        elif set(s) != (angle if s["type"] == "angle" else unc):
            c.err(f"/research_suggestions[{i}]", "unexpected or missing keys")
        elif s["type"] == "angle" and s["grounded_in_footage"] is not False:
            c.err(f"/research_suggestions[{i}]", "research angles are never grounded in footage")
        elif s.get("origin") != "research_brief":
            c.err(f"/research_suggestions[{i}]", "origin must be 'research_brief'")


def validate_brief_dict(d: Any, material: Material | None = None) -> list[str]:
    """All problems found in a brief dict, or []. With `material`, evidence is re-verified against the
    transcript (references, excerpts, attribution, pauses, source summaries, digest). Without it only
    structural and internal-consistency rules run: such a result is NOT evidence verification."""
    c = Checker(material)
    try:
        if not isinstance(d, dict):
            return ["/: a brief must be an object"]
        # research claim texts are collected first so planner statements can be checked against them
        rc = d.get("research_context")
        if isinstance(rc, list):
            c.research_texts = [norm(r["text"]) for r in rc if isinstance(r, dict) and isinstance(r.get("text"), str)
                                and len(norm(r["text"])) >= 20]
        c.walk(d, S.StoryBrief, "")
        if not c.errs:
            brief_cross(c, d)
    except Exception as e:                       # fail safe: never raise, never accept
        c.errs.append(f"/: validator could not process this document ({type(e).__name__}); treated as invalid")
    return c.errs
