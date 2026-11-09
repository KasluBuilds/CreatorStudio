"""Confidence and scope derivation. Pure functions, no I/O.

Confidence is *derived* every time from the evidence, never stored, so changing
the rules (or rejecting one evidence item) can never leave stale numbers.

Model
-----
1. Weight of one evidence item = method_weight * strength
   (verbatim 1.0, measured 1.0, creator_statement 0.7, asr_unverified 0.6, ai_estimate 0.5).
   `strength` is the extractor's certainty. If it was NOT supplied (None):
     * VERBATIM / MEASURED / CREATOR_STATEMENT are exact by nature -> strength 1.0;
     * AI_ESTIMATE / ASR_UNVERIFIED are UNRATED: kept and listed, but they carry no
       confidence mass and count toward no source (nothing is invented for them).
2. Footage vs self-report. Evidence from the creator's own statements (CREATOR_STATEMENT) is
   self-report, not observation. On footage traits it is tracked separately in
   `self_report` and does NOT enter mass, consistency or scope. A trait in the SELF_REPORT layer
   (e.g. a creator correction) is scored from the creator statements themselves and its scope is
   UNASSESSED, because stability cannot be judged without footage.
3. Evidence is grouped by source recording. One recording is one independent witness: its mass
   is  min(1, strongest + 0.3 * sum(others)).
4. S = sum of masses of supporting sources, C = same for counterexamples.
   support_score = 1 - exp(-S / k)            (k=2: 1 source .39, 2 .63, 3 .78, 5 .92)
   consistency   = S / (S + C)
   footage_confidence = min(layer cap, support_score * consistency)
5. Layer cap: observed 0.95, interpretation 0.85, hypothesis 0.50, self_report 0.95.
6. Creator overrides: confirmed or creator-authored -> `confidence` = max(footage_confidence, 0.95);
   rejected -> 0.0. `footage_confidence` is always reported unchanged next to it, and
   `confidence_basis` says which source the final number rests on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .schema import Layer, Method, Polarity, Scope, Status

METHOD_WEIGHT = {
    Method.VERBATIM: 1.0,
    Method.MEASURED: 1.0,
    Method.CREATOR_STATEMENT: 0.7,   # self-description is weaker than footage (CREATOR_DNA.md §2)
    Method.ASR_UNVERIFIED: 0.6,
    Method.AI_ESTIMATE: 0.5,
}
EXACT_METHODS = frozenset({Method.VERBATIM, Method.MEASURED, Method.CREATOR_STATEMENT})
LAYER_CAP = {Layer.OBSERVED: 0.95, Layer.INTERPRETATION: 0.85, Layer.HYPOTHESIS: 0.50, Layer.SELF_REPORT: 0.95}
CREATOR_FLOOR = 0.95


@dataclass(frozen=True)
class ScoringConfig:
    k: float = 2.0                 # saturation speed of support_score
    same_source_extra: float = 0.3  # weight of 2nd, 3rd... example from the same recording
    stable_min_sources: int = 3
    stable_min_consistency: float = 0.8
    contextual_min_sources: int = 2


DEFAULT_CONFIG = ScoringConfig()


def effective_strength(e) -> float | None:
    """Strength used for scoring; None = unrated (no mass)."""
    if e.strength is not None:
        return e.strength
    return 1.0 if e.method in EXACT_METHODS else None


def source_masses(evidence, cfg: ScoringConfig = DEFAULT_CONFIG) -> dict[str, float]:
    by_source: dict[str, list[float]] = {}
    for e in evidence:
        s = effective_strength(e)
        if s is None:
            continue
        by_source.setdefault(e.source_id, []).append(METHOD_WEIGHT[e.method] * s)
    out = {}
    for src, ws in by_source.items():
        ws.sort(reverse=True)
        out[src] = min(1.0, ws[0] + cfg.same_source_extra * sum(ws[1:]))
    return out


def band(conf: float) -> str:
    if conf < 0.45:      # a single recording (0.39) must never read as "medium"
        return "low"
    if conf < 0.65:
        return "medium"
    if conf < 0.85:
        return "high"
    return "very_high"


@dataclass
class Assessment:
    confidence: float
    footage_confidence: float
    confidence_basis: str             # footage | footage_and_creator_confirmation | creator_confirmation_over_weak_footage
    #                                   | creator_self_report | creator_rejected
    band: str
    scope: Scope
    scope_source: str                 # "derived" or "creator_declared"
    support_sources: int
    counter_sources: int
    support_examples: int
    counter_examples: int
    unrated_examples: int
    unattributed_examples: int
    support_mass: float
    counter_mass: float
    support_score: float
    consistency: float
    base: float
    cap: float
    last_observed: str | None
    self_report: dict
    explanation: list[str]

    def to_dict(self) -> dict:
        return {
            "confidence": round(self.confidence, 3), "footage_confidence": round(self.footage_confidence, 3),
            "confidence_basis": self.confidence_basis, "band": self.band,
            "scope": self.scope.value, "scope_source": self.scope_source,
            "support_sources": self.support_sources, "counter_sources": self.counter_sources,
            "support_examples": self.support_examples, "counter_examples": self.counter_examples,
            "unrated_examples": self.unrated_examples, "unattributed_examples": self.unattributed_examples,
            "support_mass": round(self.support_mass, 3), "counter_mass": round(self.counter_mass, 3),
            "support_score": round(self.support_score, 3), "consistency": round(self.consistency, 3),
            "base": round(self.base, 3), "cap": self.cap, "last_observed": self.last_observed,
            "self_report": self.self_report, "explanation": self.explanation,
        }


def derive_scope(support, counter, s_sources: int, consistency: float,
                 cfg: ScoringConfig = DEFAULT_CONFIG) -> tuple[Scope, str]:
    """Returns (scope, reason). `support`/`counter` are the rated footage evidence items."""
    if s_sources == 0:
        return Scope.UNASSESSED, "no rated footage evidence, so stability cannot be judged"
    if s_sources < cfg.contextual_min_sources:
        return Scope.ONE_OFF, "supported by a single recording"
    contexts = {tuple(e.context) for e in support if e.context}
    single_context = len(contexts) == 1 and all(e.context for e in support)
    if counter and consistency < cfg.stable_min_consistency:
        return Scope.CONTEXTUAL, "counterexamples exist, so it does not hold everywhere"
    if single_context:
        return Scope.CONTEXTUAL, f"all supporting evidence shares one context {sorted(contexts)[0]}"
    if s_sources >= cfg.stable_min_sources and consistency >= cfg.stable_min_consistency:
        return Scope.STABLE, f"{s_sources} recordings, consistency {consistency:.2f}, no single-context limit"
    return Scope.EMERGING, f"only {s_sources} recordings; need {cfg.stable_min_sources} to call it stable"


def assess(trait, cfg: ScoringConfig = DEFAULT_CONFIG) -> Assessment:
    """`trait` is a store.TraitState (duck-typed: .evidence_items, .layer, .status,
    .creator_authored, .creator_scope)."""
    live = [i.evidence for i in trait.evidence_items if not i.rejected]
    self_ev = [e for e in live if e.method is Method.CREATOR_STATEMENT]
    all_footage = [e for e in live if e.method is not Method.CREATOR_STATEMENT]
    # Footage evidence only counts as evidence about the CREATOR if an eligibility decision stamped it.
    footage_ev = [e for e in all_footage if e.attribution is not None]
    unattributed = len(all_footage) - len(footage_ev)
    self_layer = trait.layer is Layer.SELF_REPORT
    scored = self_ev if self_layer else footage_ev          # what the number is computed from
    unrated = [e for e in scored if effective_strength(e) is None]
    rated = [e for e in scored if effective_strength(e) is not None]
    support = [e for e in rated if e.polarity is Polarity.SUPPORT]
    counter = [e for e in rated if e.polarity is Polarity.COUNTER]
    sm, cm = source_masses(support, cfg), source_masses(counter, cfg)
    S, C = sum(sm.values()), sum(cm.values())
    support_score = 1 - math.exp(-S / cfg.k)
    consistency = S / (S + C) if (S + C) > 0 else 0.0
    base = support_score * consistency
    cap = LAYER_CAP[trait.layer]
    footage_conf = min(base, cap)

    kind = "creator self-report" if self_layer else "footage"
    why = [
        f"{len(support)} supporting {kind} example(s) from {len(sm)} source(s); "
        f"independent support mass S={S:.2f} -> support_score={support_score:.2f}",
        f"{len(counter)} counterexample(s) from {len(cm)} source(s); "
        f"C={C:.2f} -> consistency={consistency:.2f}",
        f"base = {support_score:.2f} x {consistency:.2f} = {base:.2f}; "
        f"layer '{trait.layer.value}' caps confidence at {cap:.2f}",
    ]
    if unattributed and not self_layer:
        why.append(f"{unattributed} footage example(s) carry no creator attribution: listed, but never counted")
    if unrated:
        why.append(f"{len(unrated)} example(s) have no supplied confidence: listed, but carry no confidence mass")

    if self_layer:
        scope, reason = Scope.UNASSESSED, "creator self-report: stability cannot be judged from statements alone"
    else:
        scope, reason = derive_scope(support, counter, len(sm), consistency, cfg)
    scope_source = "derived"
    why.append(f"scope {scope.value}: {reason}")
    if trait.creator_scope is not None:
        scope, scope_source = trait.creator_scope, "creator_declared"
        why.append(f"scope declared by the creator: {scope.value}")

    confirmed = trait.status is Status.CONFIRMED
    conf, basis = footage_conf, "creator_self_report" if self_layer else "footage"
    if trait.status is Status.REJECTED:
        conf, basis = 0.0, "creator_rejected"
        why.append("creator rejected this trait: confidence forced to 0 and it is excluded from the active profile")
    elif confirmed or trait.creator_authored:
        conf = max(footage_conf, CREATOR_FLOOR)
        if self_layer:
            basis = "creator_self_report"
        elif footage_conf >= CREATOR_FLOOR:
            basis = "footage_and_creator_confirmation"
        else:
            basis = "creator_confirmation_over_weak_footage"
        why.append(f"creator confirmed/authored this trait: confidence floor {CREATOR_FLOOR} "
                   f"(footage alone gives {footage_conf:.2f}; basis: {basis})")

    dates = [e.source_date for e in support if e.source_date]
    sr_support = sum(1 for e in self_ev if e.polarity is Polarity.SUPPORT)
    self_report = {"confirmed": confirmed, "creator_authored": trait.creator_authored,
                   "statements_support": sr_support, "statements_counter": len(self_ev) - sr_support,
                   "counted_in_footage_confidence": False}
    return Assessment(
        confidence=conf, footage_confidence=footage_conf, confidence_basis=basis, band=band(conf), scope=scope,
        scope_source=scope_source, support_sources=len(sm), counter_sources=len(cm),
        support_examples=len(support), counter_examples=len(counter), unrated_examples=len(unrated),
        unattributed_examples=0 if self_layer else unattributed,
        support_mass=S, counter_mass=C, support_score=support_score, consistency=consistency, base=base, cap=cap,
        last_observed=max(dates) if dates else None, self_report=self_report, explanation=why)
