"""Topic and trend discovery interfaces (no provider implementations, no network).

A discovery provider turns a DiscoveryRequest into TopicCandidates. The ranking step is
provider-independent and lives here.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from .models import FormatStudy, RelevanceScore, Source, TopicKind
from .scoring import (CreatorInterests, ScoreInputs, ScoreWeights, TopicSignals, evidence_from_sources,
                      lead_reliability, score_topic)


@dataclass
class DiscoveryRequest:
    mode: str = "weekly"                       # "weekly" | "on_demand"
    as_of: Optional[dt.date] = None
    categories: list[str] = field(default_factory=list)   # empty = all
    query: str = ""                            # on-demand: the question/topic seed
    language: str = "hr"
    max_candidates: int = 20
    include_foreign_formats: bool = True


@dataclass
class TopicCandidate:
    id: str
    title: str
    summary: str
    category: str
    topic_kind: TopicKind = TopicKind.EVERGREEN
    keywords: list[str] = field(default_factory=list)
    lead_sources: list[Source] = field(default_factory=list)
    signals: TopicSignals = field(default_factory=TopicSignals)
    event_date: Optional[str] = None           # partial ISO date of the triggering event/source
    reference_format: Optional[FormatStudy] = None
    provider: str = ""


@runtime_checkable
class DiscoveryProvider(Protocol):
    """Implemented later by any trend/topic source. Must not mutate project state."""
    name: str

    def discover(self, request: DiscoveryRequest) -> list[TopicCandidate]: ...


@dataclass
class ScoredCandidate:
    candidate: TopicCandidate
    score: RelevanceScore


@dataclass
class ProviderError:
    provider: str
    error_type: str
    message: str


@dataclass
class WeeklyReport:
    request: DiscoveryRequest
    generated_on: dt.date
    ranked: list[ScoredCandidate] = field(default_factory=list)
    excluded: list[ScoredCandidate] = field(default_factory=list)
    provider_errors: list[ProviderError] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _norm_title(t: str) -> str:
    return re.sub(r"\W+", " ", t.casefold()).strip()


def dedupe(candidates: list[TopicCandidate]) -> list[TopicCandidate]:
    """Merge candidates with the same normalised title; lead sources are unioned by URL."""
    merged: dict[str, TopicCandidate] = {}
    for c in candidates:
        key = _norm_title(c.title)
        cur = merged.get(key)
        if cur is None:
            merged[key] = c
            continue
        have = {s.id for s in cur.lead_sources} | {s.url for s in cur.lead_sources}
        cur.lead_sources.extend(s for s in c.lead_sources if s.url not in have and s.id not in have)
        cur.keywords = sorted(set(cur.keywords) | set(c.keywords))
    return list(merged.values())


def score_candidate(c: TopicCandidate, interests: CreatorInterests, today: dt.date,
                    weights: Optional[ScoreWeights] = None) -> RelevanceScore:
    ev = evidence_from_sources(c.lead_sources)
    extra = " ".join([c.summary, *(s.title for s in c.lead_sources)])   # checked for `avoid` only
    inputs = ScoreInputs(category=c.category, text=" ".join([c.title, *c.keywords]),
                         topic_kind=c.topic_kind, newest_date=c.event_date, signals=c.signals,
                         evidence=ev, evidence_reliability=lead_reliability(ev), extra_text=extra)
    return score_topic(inputs, interests, today, weights)


def rank_candidates(candidates: list[TopicCandidate], interests: CreatorInterests, today: dt.date,
                    weights: Optional[ScoreWeights] = None, limit: Optional[int] = None,
                    errors: Optional[list[ProviderError]] = None
                    ) -> tuple[list[ScoredCandidate], list[ScoredCandidate]]:
    """Return (ranked, excluded). Ties break on confidence then title for determinism. A candidate
    that cannot be scored is skipped and reported in `errors` instead of aborting the whole run."""
    scored: list[ScoredCandidate] = []
    for c in dedupe(candidates):
        try:
            scored.append(ScoredCandidate(c, score_candidate(c, interests, today, weights)))
        except Exception as e:  # noqa: BLE001
            if errors is not None:
                errors.append(ProviderError(str(getattr(c, "provider", "") or "?")[:60], "ScoringFailed",
                                            f"candidate skipped: {type(e).__name__}"))
    ok = sorted((s for s in scored if not s.score.excluded),
                key=lambda s: (-s.score.overall, -s.score.confidence, s.candidate.title))
    bad = [s for s in scored if s.score.excluded]
    return (ok[:limit] if limit else ok), bad
