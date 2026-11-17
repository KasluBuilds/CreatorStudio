"""Relevance scoring: creator interest, audience potential, freshness, evidence quality.

All weights/thresholds are PROPOSED defaults (the docs list "relevance vs trend popularity" as an
OPEN question). Design intent:

* popularity can never rescue a topic the creator does not care about (or whose category the
  profile does not know), nor one with no credible evidence: those are hard caps;
* invalid inputs (NaN, infinity, out of 0..1, wrong type) are NEVER clamped into a good score:
  they are treated as unknown, which scores pessimistically, and the explanation says so;
* missing audience signals count as a weak prior (0.2), not as neutral;
* `confidence` is min(data_completeness, evidence_reliability): completeness (how much of the
  input was known) is reported separately from reliability (how much of the evidence holds up).
"""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .attestation import TrustPolicy
from .models import ClaimKind, ClaimStatus, Claim, QualityTier, RelevanceScore, Source, TopicKind
from .sources import assess_all, independence_groups, parse_partial_date

TIER_SCORE = {QualityTier.A: 1.0, QualityTier.B: 0.75, QualityTier.C: 0.45, QualityTier.D: 0.15}
CLAIM_SCORE = {ClaimStatus.VERIFIED: 1.0, ClaimStatus.PARTIAL: 0.55, ClaimStatus.DISPUTED: 0.40,
               ClaimStatus.UNVERIFIED: 0.15, ClaimStatus.CONTRADICTED: 0.10}
CLAIM_RELIABILITY = {ClaimStatus.VERIFIED: 1.0, ClaimStatus.PARTIAL: 0.5, ClaimStatus.DISPUTED: 0.3,
                     ClaimStatus.UNVERIFIED: 0.0, ClaimStatus.CONTRADICTED: 0.0}
HALF_LIFE_DAYS = {TopicKind.CURRENT_EVENT: 10, TopicKind.TECHNOLOGY: 90,
                  TopicKind.SCIENCE: 365, TopicKind.FORMAT_STUDY: 180}
EVERGREEN_FRESHNESS = 0.7        # neutral: evergreen topics are neither boosted nor penalised
UNKNOWN_DATE_FRESHNESS = 0.3
UNKNOWN_SIGNAL_PRIOR = 0.2       # a missing audience signal counts as a weak prior, not neutral
LEAD_RELIABILITY_CAP = 0.3       # unreviewed lead sources can never look "reliable"
LEAD_EVIDENCE_CAP = 0.25         # a source known only by address (no authenticated retrieval) is below ANY evidence floor
ORIGIN_BONUS = 0.1               # per further independent, retrieved tier A/B origin ...
ORIGIN_BONUS_CAP = 0.1           # ... but never more than this in total: quantity cannot stand in for quality
DISPUTED_EVIDENCE_CAP = 0.65     # one disputed fact caps the average: it cannot be diluted by easy facts
CONTRADICTED_EVIDENCE_CAP = 0.35  # one contradicted fact caps the average even when most facts verify
CONTESTED_RELIABILITY_CAP = 0.5


# ----------------------------------------------------------------------- inputs
def clean01(x: object) -> Optional[float]:
    """A finite number within 0..1, else None. Out-of-range values are rejected, not clamped."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if not math.isfinite(x) or x < 0.0 or x > 1.0:
        return None
    return float(x)


@dataclass
class ScoreWeights:
    interest_fit: float = 0.30
    audience_potential: float = 0.25
    freshness: float = 0.20
    evidence_quality: float = 0.25
    evidence_floor: float = 0.30      # evidence below this caps the overall score ...
    evidence_cap: float = 0.40        # ... at this value
    interest_floor: float = 0.20      # interest below this (or unknown category) caps the overall score ...
    interest_cap: float = 0.35        # ... at this value

    def __post_init__(self) -> None:
        for name in ("interest_fit", "audience_potential", "freshness", "evidence_quality",
                     "evidence_floor", "evidence_cap", "interest_floor", "interest_cap"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{name} must be a finite number")
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{name} must be within 0..1 (got {v})")
        total = self.interest_fit + self.audience_potential + self.freshness + self.evidence_quality
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"weights must sum to 1.0, got {total}")

    def as_dict(self) -> dict[str, float]:
        """Every parameter that shaped the score, so a stored relevance can be re-checked (audit F-07)."""
        return {k: getattr(self, k) for k in ("interest_fit", "audience_potential", "freshness", "evidence_quality",
                                              "evidence_floor", "evidence_cap", "interest_floor", "interest_cap")}


_TRANSLATE = str.maketrans({"đ": "d", "Đ": "d"})


def tokens(text: object) -> list[str]:
    """Accent-insensitive, case-insensitive word tokens (so 'Čovjek' == 'covjek')."""
    s = unicodedata.normalize("NFKD", str(text or "").translate(_TRANSLATE))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.findall(r"\w+", s.casefold())


def normalize_category(category: object) -> str:
    return "_".join(tokens(category))


def _phrase_in(hay: list[str], phrase: str) -> bool:
    """Whole-token (contiguous) match; a trailing '*' makes the LAST token a prefix match
    (for inflected languages: 'kockan*' matches kockanje, kockanja)."""
    prefix = phrase.strip().endswith("*")
    pt = tokens(phrase.replace("*", " "))
    if not pt:
        return False
    n = len(pt)
    for i in range(len(hay) - n + 1):
        win = hay[i:i + n]
        if win[:-1] == pt[:-1] and (win[-1].startswith(pt[-1]) if prefix else win[-1] == pt[-1]):
            return True
    return False


@dataclass
class FitResult:
    fit: float
    excluded_reason: Optional[str]
    known_category: bool
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class CreatorInterests:
    """Supplied by the caller (later: derived from Creator DNA). `placeholder()` exists only so
    tests/demos have a starting point. Categories are matched after normalisation
    ('Vlog / Humor' == 'vlog_humor'); `avoid` and keywords match whole tokens, never substrings."""
    category_weights: dict[str, float] = field(default_factory=dict)
    keyword_weights: dict[str, float] = field(default_factory=dict)
    avoid: list[str] = field(default_factory=list)
    unknown_category_weight: float = 0.2

    def __post_init__(self) -> None:
        for name, d in (("category_weights", self.category_weights), ("keyword_weights", self.keyword_weights)):
            for k, v in d.items():
                if clean01(v) is None:
                    raise ValueError(f"{name}[{k!r}] must be a finite number within 0..1")
        if clean01(self.unknown_category_weight) is None:
            raise ValueError("unknown_category_weight must be within 0..1")

    @classmethod
    def placeholder(cls) -> "CreatorInterests":
        cats = ["mycology", "diy", "technology", "biology_science", "vlog_humor", "opinion", "current_events"]
        return cls(category_weights={c: 0.7 for c in cats})

    def assess(self, category: str, text: str, extra_text: str = "") -> FitResult:
        """`text` = title + keywords (positive interest AND avoid); `extra_text` (summary, lead
        titles) is checked for `avoid` only."""
        main, both = tokens(text), tokens(text) + tokens(extra_text)
        for a in self.avoid:
            if _phrase_in(both, a):
                return FitResult(0.0, f"matches avoided topic '{a}'", True)
        cats = {normalize_category(k): v for k, v in self.category_weights.items()}
        key = normalize_category(category)
        known = key in cats
        w = cats[key] if known else self.unknown_category_weight
        if not self.keyword_weights:
            return FitResult(_clamp(w), None, known)
        hit = [k for k in self.keyword_weights if _phrase_in(main, k)]
        kw = sum(self.keyword_weights[k] for k in hit)
        return FitResult(_clamp(0.7 * w + 0.3 * min(1.0, kw)), None, known, hit)

    def fit(self, category: str, text: str) -> tuple[float, Optional[str]]:
        r = self.assess(category, text)
        return r.fit, r.excluded_reason


@dataclass
class TopicSignals:
    """Optional 0..1 market signals. None = unknown. Anything not a finite number in 0..1 is
    treated as unknown (and reported) at scoring time."""
    trend_popularity: Optional[float] = None
    local_gap: Optional[float] = None      # how underserved the topic is in Croatian
    format_proof: Optional[float] = None   # evidence the format works elsewhere
    saturation: Optional[float] = None     # how crowded; high saturation dampens potential


@dataclass
class ScoreInputs:
    category: str
    text: str                                  # title + keywords
    topic_kind: TopicKind = TopicKind.EVERGREEN
    newest_date: Optional[str] = None          # partial ISO date of the newest relevant event/source
    signals: TopicSignals = field(default_factory=TopicSignals)
    evidence: Optional[float] = None           # 0..1, None = no evidence assessed yet
    evidence_reliability: Optional[float] = None  # 0..1, None = nothing reviewed
    extra_text: str = ""                       # summary etc.: used for `avoid` only


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x)) if math.isfinite(x) else 0.0


def clean_signals(sig: TopicSignals) -> tuple[TopicSignals, list[str]]:
    out, problems = {}, []
    for name in ("trend_popularity", "local_gap", "format_proof", "saturation"):
        raw = getattr(sig, name)
        v = clean01(raw)
        if raw is not None and v is None:
            problems.append(f"{name} invalid ({raw!r}); treated as unknown")
        out[name] = v
    return TopicSignals(**out), problems


# ---------------------------------------------------------------------- components
def freshness_score(kind: TopicKind, newest_date: Optional[str], today: dt.date) -> tuple[float, bool]:
    """Return (score, known). Exponential decay with a per-kind half-life; invalid or future dates
    count as unknown."""
    s, known, _ = _freshness(kind, newest_date, today)
    return s, known


def _freshness(kind: TopicKind, newest_date: Optional[str], today: dt.date) -> tuple[float, bool, Optional[str]]:
    if kind is TopicKind.EVERGREEN:
        return EVERGREEN_FRESHNESS, True, None
    try:
        pd = parse_partial_date(newest_date)
    except ValueError:
        return UNKNOWN_DATE_FRESHNESS, False, f"event date {newest_date!r} invalid; treated as unknown"
    if pd is None:
        return UNKNOWN_DATE_FRESHNESS, False, "event date unknown: freshness set to a low default"
    if pd[0] > today:
        return UNKNOWN_DATE_FRESHNESS, False, "event date is in the future; treated as unknown"
    age = max(0, (today - min(pd[1], today)).days)
    return 0.5 ** (age / HALF_LIFE_DAYS[kind]), True, None


def evidence_from_sources(sources: Iterable[Source], trust: Optional[TrustPolicy] = None,
                          today: Optional[dt.date] = None) -> Optional[float]:
    """Lead-stage evidence from the SAME tier/lineage/independence logic that verifies claims. Only sources with an
    authenticated retrieval record count at their tier; a source known by address alone (what an LLM or a provider
    hands over) is a lead and is worth at most LEAD_EVIDENCE_CAP, below any evidence floor, so invented URLs cannot
    lift a topic over the floor however many there are (audit F-04). Further independent retrieved tier A/B origins
    add ORIGIN_BONUS each, at most ORIGIN_BONUS_CAP in total (audit F-06)."""
    srcs = list(sources)
    if not srcs:
        return None
    tiers = assess_all(srcs, trust, today)
    ind = independence_groups(srcs, (), trust, today)
    best: dict[str, QualityTier] = {}
    lead_best: Optional[QualityTier] = None
    for s in srcs:
        a = tiers[s.id]
        if not a.retrieved:
            if lead_best is None or a.tier.rank < lead_best.rank:
                lead_best = a.tier
            continue
        g = ind.group_of[s.id]
        if g not in best or a.tier.rank < best[g].rank:
            best[g] = a.tier
    if not best:
        return _clamp(min(TIER_SCORE[lead_best], LEAD_EVIDENCE_CAP)) if lead_best is not None else None
    top = min(best.values(), key=lambda t: t.rank)
    score = TIER_SCORE[top]
    if top.rank <= QualityTier.B.rank:
        extra = sum(1 for t in best.values() if t.rank <= QualityTier.B.rank) - 1
        score += min(ORIGIN_BONUS * extra, ORIGIN_BONUS_CAP)
    return _clamp(score)


def evidence_from_claims(claims: Iterable[Claim]) -> Optional[float]:
    """Mean claim strength, capped by the worst contested fact: verified facts cannot dilute a contradicted or
    disputed one (audit F-06)."""
    facts = [c for c in claims if c.kind is ClaimKind.FACT and c.status in CLAIM_SCORE]
    if not facts:
        return None
    score = sum(CLAIM_SCORE[c.status] for c in facts) / len(facts)
    if any(c.status is ClaimStatus.CONTRADICTED for c in facts):
        score = min(score, CONTRADICTED_EVIDENCE_CAP)
    elif any(c.status is ClaimStatus.DISPUTED for c in facts):
        score = min(score, DISPUTED_EVIDENCE_CAP)
    return score


def reliability_from_claims(claims: Iterable[Claim]) -> Optional[float]:
    facts = [c for c in claims if c.kind is ClaimKind.FACT and c.status in CLAIM_RELIABILITY]
    if not facts:
        return None
    rel = sum(CLAIM_RELIABILITY[c.status] for c in facts) / len(facts)
    if any(c.status in (ClaimStatus.CONTRADICTED, ClaimStatus.DISPUTED) for c in facts):
        rel = min(rel, CONTESTED_RELIABILITY_CAP)
    return rel


def lead_reliability(evidence: Optional[float]) -> float:
    """Leads are unreviewed: reliability stays low whatever the source tiers look like."""
    return 0.0 if evidence is None else min(LEAD_RELIABILITY_CAP, 0.4 * evidence)


def audience_potential(signals: TopicSignals) -> tuple[float, float]:
    """Return (score, known_fraction). Unknown main signals count as UNKNOWN_SIGNAL_PRIOR;
    saturation (when known) always dampens, by at most half."""
    sig, _ = clean_signals(signals)
    main = [sig.trend_popularity, sig.local_gap, sig.format_proof]
    known = [v for v in main if v is not None]
    base = (sum(known) + UNKNOWN_SIGNAL_PRIOR * (len(main) - len(known))) / len(main)
    if sig.saturation is not None:
        base *= 0.5 + 0.5 * (1 - sig.saturation)
    frac = (len(known) + (1 if sig.saturation is not None else 0)) / 4
    return _clamp(base), frac


# ---------------------------------------------------------------------------- score
def score_topic(inputs: ScoreInputs, interests: CreatorInterests, today: dt.date,
                weights: Optional[ScoreWeights] = None) -> RelevanceScore:
    w = weights or ScoreWeights()
    why: list[str] = []
    sig, problems = clean_signals(inputs.signals)
    why += problems
    fit_r = interests.assess(inputs.category, inputs.text, inputs.extra_text)
    fit = fit_r.fit
    fresh, fresh_known, fresh_note = _freshness(inputs.topic_kind, inputs.newest_date, today)
    if fresh_note:
        why.append(fresh_note)
    aud, aud_frac = audience_potential(sig)
    ev = clean01(inputs.evidence)
    if inputs.evidence is not None and ev is None:
        why.append(f"evidence invalid ({inputs.evidence!r}); treated as unknown")
    rel = clean01(inputs.evidence_reliability)
    if inputs.evidence_reliability is not None and rel is None:
        why.append(f"evidence_reliability invalid ({inputs.evidence_reliability!r}); treated as 0")
    ev_known = ev is not None
    ev_v = ev if ev_known else 0.0

    overall = (w.interest_fit * fit + w.audience_potential * aud + w.freshness * fresh
               + w.evidence_quality * ev_v)
    caps: list[str] = []
    excluded = fit_r.excluded_reason is not None
    if excluded:
        why.append(f"excluded: {fit_r.excluded_reason}")
        overall = 0.0
    else:
        if ev_v < w.evidence_floor:
            overall = min(overall, w.evidence_cap)
            caps.append("EVIDENCE_FLOOR")
            why.append(f"evidence {ev_v:.2f} < {w.evidence_floor}: capped at {w.evidence_cap} "
                       "(popularity cannot compensate for missing evidence)")
        if not fit_r.known_category:
            overall = min(overall, w.interest_cap)
            caps.append("UNKNOWN_CATEGORY")
            why.append(f"category {inputs.category!r} is not in the creator profile: capped at "
                       f"{w.interest_cap} (extend the profile to lift this)")
        elif fit < w.interest_floor:
            overall = min(overall, w.interest_cap)
            caps.append("INTEREST_FLOOR")
            why.append(f"interest fit {fit:.2f} < {w.interest_floor}: capped at {w.interest_cap} "
                       "(do not push the creator into a trend)")
    if aud_frac < 1.0:
        why.append(f"only {aud_frac:.0%} of audience signals known (unknown ones count as {UNKNOWN_SIGNAL_PRIOR})")
    if not ev_known:
        why.append("no evidence assessed yet")
    completeness = (0.25 * (1.0 if fit_r.known_category else 0.0) + 0.25 * aud_frac
                    + 0.25 * (1.0 if fresh_known else 0.0) + 0.25 * (1.0 if ev_known else 0.0))
    reliability = rel if rel is not None else 0.0
    confidence = min(completeness, reliability)
    r = lambda x: round(_clamp(x), 4)  # noqa: E731
    return RelevanceScore(overall=r(overall), interest_fit=r(fit), audience_potential=r(aud),
                          freshness=r(fresh), evidence_quality=r(ev_v), confidence=r(confidence),
                          data_completeness=r(completeness), evidence_reliability=r(reliability),
                          excluded=excluded, caps_applied=caps, explanation=why, weights=w.as_dict())


# ------------------------------------------------------------------ stored-score consistency (audit F-07)
_REL_COMPONENTS = ("overall", "interest_fit", "audience_potential", "freshness", "evidence_quality", "confidence",
                   "data_completeness", "evidence_reliability")
_KNOWN_CAPS = {"EVIDENCE_FLOOR", "UNKNOWN_CATEGORY", "INTEREST_FLOOR"}
_TOL = 1.5e-3      # components are stored rounded to 4 places; sums of them drift by a few ten-thousandths


def relevance_problems(rel, claims: Iterable[Claim], sources: Iterable[Source], trust: Optional[TrustPolicy],
                       today: dt.date) -> list[str]:
    """Why a STORED RelevanceScore cannot be what score_topic produced (empty when it is consistent). The inputs
    the brief does not keep (interests, audience signals) cannot be recomputed, so each derived number is checked
    for range, against its own recorded weights and caps, and against the evidence that IS in the brief."""
    if rel is None:
        return []
    out: list[str] = []
    vals = {k: getattr(rel, k, None) for k in _REL_COMPONENTS}
    for k, v in vals.items():
        if clean01(v) is None:
            out.append(f"{k} is not a finite number within 0..1")
    if out:
        return out
    wd = rel.weights if isinstance(rel.weights, dict) else {}
    try:
        w = ScoreWeights(**{k: wd[k] for k in ("interest_fit", "audience_potential", "freshness", "evidence_quality",
                                               "evidence_floor", "evidence_cap", "interest_floor", "interest_cap")})
    except (KeyError, TypeError, ValueError):
        return ["the recorded weights are missing or invalid, so the score cannot be checked"]
    if not isinstance(rel.caps_applied, list) or not set(rel.caps_applied) <= _KNOWN_CAPS:
        out.append("caps_applied names a cap that does not exist")
    caps = set(rel.caps_applied) if isinstance(rel.caps_applied, list) else set()
    base = (w.interest_fit * rel.interest_fit + w.audience_potential * rel.audience_potential
            + w.freshness * rel.freshness + w.evidence_quality * rel.evidence_quality)
    if rel.excluded:
        if rel.overall != 0.0:
            out.append("an excluded topic must score 0")
    else:
        if rel.overall > base + _TOL:
            out.append("overall exceeds the weighted sum of its own components")
        if rel.evidence_quality < w.evidence_floor:
            if "EVIDENCE_FLOOR" not in caps:
                out.append("evidence is below the floor but the evidence cap is not recorded as applied")
            if rel.overall > w.evidence_cap + _TOL:
                out.append("overall exceeds the evidence cap")
        elif "EVIDENCE_FLOOR" in caps:
            out.append("the evidence cap is recorded as applied although evidence is above the floor")
        if caps & {"UNKNOWN_CATEGORY", "INTEREST_FLOOR"} and rel.overall > w.interest_cap + _TOL:
            out.append("overall exceeds the interest cap")
        if rel.interest_fit < w.interest_floor and not caps & {"UNKNOWN_CATEGORY", "INTEREST_FLOOR"}:
            out.append("interest fit is below the floor but no interest cap is recorded as applied")
        if not caps and abs(rel.overall - base) > _TOL:
            out.append("overall differs from the weighted sum although no cap was applied")
    if abs(rel.confidence - min(rel.data_completeness, rel.evidence_reliability)) > _TOL:
        out.append("confidence is not min(data_completeness, evidence_reliability)")
    claims, sources = list(claims), list(sources)
    ev, reliability = evidence_from_claims(claims), reliability_from_claims(claims)
    if ev is None:
        ev = evidence_from_sources(sources, trust, today) if sources else None
        reliability = lead_reliability(ev)
    if (ev if ev is not None else 0.0) - rel.evidence_quality > _TOL or rel.evidence_quality - (ev if ev is not None else 0.0) > _TOL:
        out.append("evidence_quality differs from what the brief's own evidence gives")
    if abs((reliability or 0.0) - rel.evidence_reliability) > _TOL:
        out.append("evidence_reliability differs from what the brief's own evidence gives")
    return out
