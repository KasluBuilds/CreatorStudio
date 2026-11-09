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
        return {k: getattr(self, k) for k in ("interest_fit", "audience_potential", "freshness",
                                              "evidence_quality")}


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
    """Lead-stage evidence from the SAME tier/lineage/independence logic that verifies claims:
    best tier, plus +0.1 per additional independent origin at tier A/B."""
    srcs = list(sources)
    if not srcs:
        return None
    tiers = assess_all(srcs, trust, today)
    ind = independence_groups(srcs, (), trust, today)
    best: dict[str, QualityTier] = {}
    for s in srcs:
        g, t = ind.group_of[s.id], tiers[s.id].tier
        if g not in best or t.rank < best[g].rank:
            best[g] = t
    top = min(best.values(), key=lambda t: t.rank)
    score = TIER_SCORE[top]
    if top.rank <= QualityTier.B.rank:
        score += 0.1 * (sum(1 for t in best.values() if t.rank <= QualityTier.B.rank) - 1)
    return _clamp(score)


def evidence_from_claims(claims: Iterable[Claim]) -> Optional[float]:
    scores = [CLAIM_SCORE[c.status] for c in claims if c.kind is ClaimKind.FACT and c.status in CLAIM_SCORE]
    return sum(scores) / len(scores) if scores else None


def reliability_from_claims(claims: Iterable[Claim]) -> Optional[float]:
    vals = [CLAIM_RELIABILITY[c.status] for c in claims if c.kind is ClaimKind.FACT and c.status in CLAIM_RELIABILITY]
    return sum(vals) / len(vals) if vals else None


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
