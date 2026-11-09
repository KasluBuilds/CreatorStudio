"""Input interfaces for analyzers (transcript, audio, visual) and small evidence helpers.

Nothing here transcribes or analyses media. These are the shapes the Creator DNA store expects.

Rules every analyzer must follow (the store enforces them):
  * transcript wording from a human-REVIEWED transcript -> Method.VERBATIM (+ reviewed_by);
    wording straight from a transcription engine -> Method.ASR_UNVERIFIED, never VERBATIM;
  * deterministic audio numbers -> Method.MEASURED, read through `AudioWindow.metric()` (may be None);
  * model/detector judgements -> Method.AI_ESTIMATE with the detector's own confidence, which may be
    None; None stays None (such evidence is listed but carries no confidence mass);
  * INTERPRETATION / HYPOTHESIS layers for anything AI-derived, never OBSERVED.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable

from .metrics import METRICS, validate_metrics
from .schema import (Category, DNAError, Evidence, EvidenceAttribution, Layer, Method, Modality, Origin, Polarity,
                     TraitCandidate, check_reviewer, normalize_text)


@dataclass(frozen=True)
class TranscriptSegment:
    """One time-aligned piece of transcript (seconds on the original media's timeline).

    `verification` defaults to "unverified": a segment is only "reviewed" when a named human
    reviewed it (`reviewer="human:<name>"`). Nothing promotes it automatically."""
    source_id: str
    start_s: float
    end_s: float
    text: str
    language: str | None = None          # e.g. "hr", "hr-x-kajkavian", "en" - whatever the transcriber reports
    speaker: str | None = None
    source_date: str | None = None
    context: tuple[str, ...] = ()
    confidence: float | None = None      # engine-supplied only
    verification: str = "unverified"     # "unverified" | "reviewed"
    reviewer: str | None = None
    origin: Origin | None = None
    attribution: EvidenceAttribution | None = None   # None = not attributed to the creator (never implied)

    def __post_init__(self) -> None:
        if self.verification not in ("unverified", "reviewed"):
            raise DNAError("verification must be 'unverified' or 'reviewed'")
        if self.verification == "reviewed":
            check_reviewer(self.reviewer)
        elif self.reviewer is not None:
            raise DNAError("an unverified segment cannot name a reviewer")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise DNAError("confidence must be within 0..1 or None")


@dataclass(frozen=True)
class AudioWindow:
    """Measured delivery features for a time window; `metrics` obey creator_dna.metrics (all optional)."""
    source_id: str
    start_s: float
    end_s: float
    metrics: Mapping[str, float]
    source_date: str | None = None
    context: tuple[str, ...] = ()
    origin: Origin | None = None
    attribution: EvidenceAttribution | None = None

    def __post_init__(self) -> None:
        if self.end_s < self.start_s or self.start_s < 0:
            raise DNAError("audio window needs 0 <= start_s <= end_s")
        object.__setattr__(self, "metrics", validate_metrics(self.metrics))

    def metric(self, name: str) -> float | None:
        """The metric's value, or None if this window does not carry it."""
        if name not in METRICS:
            raise DNAError(f"unknown audio metric {name!r}")
        return self.metrics.get(name)


@dataclass(frozen=True)
class VisualObservation:
    """A visual-analysis finding. `estimate_confidence` is the detector's own certainty and is
    None when the detector gave none; it is never filled in with a default."""
    source_id: str
    start_s: float
    end_s: float
    label: str                           # e.g. "gesture:hand_wave", "shot_boundary"
    description: str = ""
    estimate_confidence: float | None = None
    source_date: str | None = None
    context: tuple[str, ...] = ()
    origin: Origin | None = None
    attribution: EvidenceAttribution | None = None

    def __post_init__(self) -> None:
        if self.end_s < self.start_s or self.start_s < 0:
            raise DNAError("visual observation needs 0 <= start_s <= end_s")
        if self.estimate_confidence is not None and not 0.0 <= self.estimate_confidence <= 1.0:
            raise DNAError("estimate_confidence must be within 0..1 or None")


@runtime_checkable
class TranscriptAnalyzer(Protocol):
    name: str
    version: str
    def analyze_transcript(self, segments: Sequence[TranscriptSegment]) -> list[TraitCandidate]: ...


@runtime_checkable
class AudioAnalyzer(Protocol):
    name: str
    version: str
    def analyze_audio(self, windows: Sequence[AudioWindow]) -> list[TraitCandidate]: ...


@runtime_checkable
class VisualAnalyzer(Protocol):
    name: str
    version: str
    def analyze_visual(self, observations: Sequence[VisualObservation]) -> list[TraitCandidate]: ...


def actor_name(analyzer) -> str:
    return f"{analyzer.name}@{analyzer.version}"


def _hash_of(origin: Origin | None) -> str | None:
    return origin.source_sha256 if origin else None


# ----------------------------------------------------------------- evidence helpers
def evidence_from_window(w: AudioWindow, metric: str, polarity: Polarity = Polarity.SUPPORT,
                         note: str = "") -> Evidence | None:
    """MEASURED audio evidence for one metric of a window, or None if the window lacks that metric."""
    value = w.metric(metric)
    if value is None:
        return None
    spec = METRICS[metric]
    desc = f"{metric}={value:g} {spec.unit}" + (f"; {note}" if note else "")
    return Evidence(w.source_id, Modality.AUDIO, Method.MEASURED, polarity, w.start_s, w.end_s, note=desc,
                    context=w.context, source_hash=_hash_of(w.origin), source_date=w.source_date, origin=w.origin,
                    attribution=w.attribution)


def evidence_from_visual(o: VisualObservation, polarity: Polarity = Polarity.SUPPORT, note: str = "") -> Evidence:
    """AI_ESTIMATE visual evidence. Strength is the detector's confidence; None stays None."""
    desc = (o.description or o.label) + (f"; {note}" if note else "")
    return Evidence(o.source_id, Modality.VISUAL, Method.AI_ESTIMATE, polarity, o.start_s, o.end_s, note=desc,
                    context=o.context, strength=o.estimate_confidence, source_hash=_hash_of(o.origin),
                    source_date=o.source_date, origin=o.origin, attribution=o.attribution)


def quote_in_transcript(ev: Evidence, segments: Sequence[TranscriptSegment]) -> bool:
    """True if the evidence's quote really occurs (case/whitespace-insensitive) in a transcript segment
    of the same source overlapping the evidence's time range. For VERBATIM evidence the segment must
    ALSO be reviewed by the same named reviewer: unverified text can never confirm verbatim evidence."""
    if ev.method not in (Method.VERBATIM, Method.ASR_UNVERIFIED) or not ev.quote:
        return False
    needle = normalize_text(ev.quote)
    for seg in segments:
        if seg.source_id != ev.source_id:
            continue
        if ev.start_s is not None:
            end = ev.end_s if ev.end_s is not None else ev.start_s
            if seg.end_s < ev.start_s or seg.start_s > end:
                continue
        if needle in normalize_text(seg.text):
            if ev.method is Method.VERBATIM and not (seg.verification == "reviewed" and seg.reviewer == ev.reviewed_by):
                continue
            return True
    return False


class RecurringPhraseAnalyzer:
    """Minimal reference analyzer: finds *given* phrases verbatim in a transcript.

    The phrases must come from a human - the analyzer does not guess what is characteristic.
    Reviewed segments yield VERBATIM evidence; unverified ones yield ASR_UNVERIFIED evidence, and a
    phrase with any unverified occurrence is proposed only as a HYPOTHESIS (never OBSERVED).
    """
    name = "recurring-phrase"
    version = "2"

    def __init__(self, phrases: Sequence[str], category: Category = Category.VOCABULARY):
        self.phrases = [p for p in phrases if p.strip()]
        self.category = category

    def analyze_transcript(self, segments: Sequence[TranscriptSegment]) -> list[TraitCandidate]:
        out = []
        for phrase in self.phrases:
            pattern = re.compile(
                r"(?<!\w)" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"(?!\w)", re.IGNORECASE)
            evidence, all_reviewed = [], True
            for seg in segments:
                for m in pattern.finditer(seg.text):
                    reviewed = seg.verification == "reviewed"
                    all_reviewed &= reviewed
                    evidence.append(Evidence(
                        source_id=seg.source_id, modality=Modality.TRANSCRIPT,
                        method=Method.VERBATIM if reviewed else Method.ASR_UNVERIFIED,
                        polarity=Polarity.SUPPORT, start_s=seg.start_s, end_s=seg.end_s, quote=m.group(0),
                        note=f"phrase occurs in segment: {seg.text[:80]}", context=seg.context,
                        strength=None if reviewed else seg.confidence, source_hash=_hash_of(seg.origin),
                        source_date=seg.source_date, reviewed_by=seg.reviewer if reviewed else None,
                        origin=seg.origin, attribution=seg.attribution))
            if evidence:
                out.append(TraitCandidate(
                    category=self.category, statement=f"Says “{phrase}”",
                    layer=Layer.OBSERVED if all_reviewed else Layer.HYPOTHESIS, evidence=evidence))
        return out
