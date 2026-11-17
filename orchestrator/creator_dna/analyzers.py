"""Deterministic speech and delivery analyzers (T-DNA-002).

Three analyzers over the existing input contract. They MEASURE; they never interpret.

  SentenceRhythmAnalyzer  sentence length in words, from human-REVIEWED transcript segments only
  PauseProfileAnalyzer    typical pause length, from supplied `pause_duration_s` measurements
  EnergyRangeAnalyzer     typical size of energy rises, from supplied `energy_rise_db` measurements
                          (the window's `level_dbfs` / `baseline_dbfs` are quoted in the evidence note)

Shared rules
  * Deterministic: inputs are sorted by (source_id, start_s, end_s); no randomness, no clock, no I/O.
  * Every output trait is OBSERVED and rests on MEASURED evidence only (a count or a supplied number).
    Nothing is an AI estimate, nothing is labelled humor, emotion, emphasis or "awkward".
  * Nothing is invented: an input that lacks the needed measurement, the required provenance
    (`origin`) or an eligible-creator `attribution` is SKIPPED and listed in `analyzer.skipped`
    with its reason; an unreviewed transcript segment is never used by the rhythm analyzer.
  * Counter-evidence is kept: after the typical bucket is derived from ALL usable inputs, every input
    that falls in a different bucket is attached as COUNTER evidence to the same trait.
  * Too few usable inputs (fewer than `min_samples`) produce no trait at all.

The bucket boundaries below are engineering defaults, an OPEN product decision: they are not creator
decisions, and a bucket is a description of the measurements, not a judgement of style quality.

  sentence length (words, median of all sentences)  short <= 8 | medium 9-16 | long >= 17
  pause (seconds, median)                           short < 0.8 | medium 0.8-1.5 | long >= 1.5
  energy rise (dB above the baseline, median)       small < 6 | moderate 6-12 | large >= 12

Word counting: a word is a run of letters/digits (apostrophes and hyphens inside it are kept), so
Croatian diacritics count as letters. Sentences end at . ! ? or an ellipsis; a segment without end
punctuation is one sentence. Numbers of words are counts, not a judgement of speaking pace.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Sequence

from .inputs import AudioWindow, TranscriptSegment, evidence_from_window
from .schema import Category, DNAError, Evidence, Layer, Method, Modality, Polarity, TraitCandidate

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_WORD = re.compile(r"\w+(?:['’-]\w+)*", re.UNICODE)

SENTENCE_BUCKETS = (("short", 8, "8 words or fewer"), ("medium", 16, "9 to 16 words"), ("long", None, "17 words or more"))
PAUSE_BUCKETS = (("short", 0.8, "under 0.8 s"), ("medium", 1.5, "0.8 to 1.5 s"), ("long", None, "1.5 s or longer"))
ENERGY_BUCKETS = (("small", 6.0, "under 6 dB"), ("moderate", 12.0, "6 to 12 dB"), ("large", None, "12 dB or more"))


def _bucket(value: float, buckets, inclusive_top: bool) -> tuple[str, str]:
    """First bucket whose upper bound is not exceeded. Sentence lengths are integers (bound inclusive);
    pauses and energy rises use an exclusive bound (`<`)."""
    for name, bound, label in buckets:
        if bound is None or (value <= bound if inclusive_top else value < bound):
            return name, label
    raise AssertionError("unreachable")


def count_sentences(text: str) -> list[int]:
    """Word counts of each sentence in `text` (empty sentences are dropped)."""
    out = []
    for part in _SENT_SPLIT.split(text.strip()):
        n = len(_WORD.findall(part))
        if n:
            out.append(n)
    return out


@dataclass(frozen=True)
class Skipped:
    source_id: str
    start_s: float
    reason: str

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "start_s": self.start_s, "reason": self.reason}


class _Base:
    version = "1"
    name = "base"

    def __init__(self, min_samples: int = 3):
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
            raise DNAError("min_samples must be an integer >= 1")
        self.min_samples = min_samples
        self.skipped: list[Skipped] = []     # rebuilt on every call

    def _need_provenance(self, item, kind: str) -> str | None:
        if item.origin is None:
            return f"{kind} has no origin (provenance is required)"
        if item.attribution is None:
            return f"{kind} has no eligible-creator attribution"
        return None


class SentenceRhythmAnalyzer(_Base):
    name = "sentence-rhythm"

    def analyze_transcript(self, segments: Sequence[TranscriptSegment]) -> list[TraitCandidate]:
        self.skipped = []
        usable = []
        for seg in sorted(segments, key=lambda s: (s.source_id, s.start_s, s.end_s)):
            if seg.verification != "reviewed":
                self.skipped.append(Skipped(seg.source_id, seg.start_s, "transcript segment is not human-reviewed"))
                continue
            why = self._need_provenance(seg, "segment")
            lengths = count_sentences(seg.text) if why is None else []
            if why is None and not lengths:
                why = "segment holds no words"
            if why:
                self.skipped.append(Skipped(seg.source_id, seg.start_s, why))
                continue
            usable.append((seg, lengths, statistics.median(lengths)))
        pooled = [n for _s, ls, _m in usable for n in ls]
        if len(pooled) < self.min_samples:
            return []
        typical, label = _bucket(statistics.median(pooled), SENTENCE_BUCKETS, inclusive_top=True)
        evidence = []
        for seg, lengths, med in usable:
            seg_bucket, _ = _bucket(med, SENTENCE_BUCKETS, inclusive_top=True)
            evidence.append(Evidence(
                source_id=seg.source_id, modality=Modality.TRANSCRIPT, method=Method.MEASURED,
                polarity=Polarity.SUPPORT if seg_bucket == typical else Polarity.COUNTER,
                start_s=seg.start_s, end_s=seg.end_s,
                note=f"{len(lengths)} sentence(s) of {', '.join(map(str, lengths))} words; median {med:g} ({seg_bucket})",
                context=seg.context, source_hash=seg.origin.source_sha256, source_date=seg.source_date,
                origin=seg.origin, attribution=seg.attribution))
        if not any(e.polarity is Polarity.SUPPORT for e in evidence):
            return []
        return [TraitCandidate(Category.SPEECH_RHYTHM, f"Sentences are typically {typical} ({label})",
                               Layer.OBSERVED, evidence)]


class _WindowAnalyzer(_Base):
    metric = ""
    category = Category.PAUSES
    buckets = PAUSE_BUCKETS
    template = "{}"

    def _statement(self, bucket: str, label: str) -> str:
        return self.template.format(bucket, label)

    def _note(self, w: AudioWindow, value: float) -> str:
        return ""

    def analyze_audio(self, windows: Sequence[AudioWindow]) -> list[TraitCandidate]:
        self.skipped = []
        usable = []
        for w in sorted(windows, key=lambda w: (w.source_id, w.start_s, w.end_s)):
            value = w.metric(self.metric)
            if value is None:
                self.skipped.append(Skipped(w.source_id, w.start_s, f"window has no {self.metric} measurement"))
                continue
            why = self._need_provenance(w, "window")
            if why:
                self.skipped.append(Skipped(w.source_id, w.start_s, why))
                continue
            usable.append((w, value))
        if len(usable) < self.min_samples:
            return []
        typical, label = _bucket(statistics.median(v for _w, v in usable), self.buckets, inclusive_top=False)
        evidence = []
        for w, v in usable:
            b, _ = _bucket(v, self.buckets, inclusive_top=False)
            ev = evidence_from_window(w, self.metric, Polarity.SUPPORT if b == typical else Polarity.COUNTER,
                                      note=f"{b}" + self._note(w, v))
            evidence.append(ev)
        return [TraitCandidate(self.category, self._statement(typical, label), Layer.OBSERVED, evidence)]


class PauseProfileAnalyzer(_WindowAnalyzer):
    name = "pause-profile"
    metric = "pause_duration_s"
    category = Category.PAUSES
    buckets = PAUSE_BUCKETS

    def _statement(self, bucket: str, label: str) -> str:
        return f"Pauses are typically {bucket} ({label})"


class EnergyRangeAnalyzer(_WindowAnalyzer):
    name = "energy-range"
    metric = "energy_rise_db"
    category = Category.DELIVERY
    buckets = ENERGY_BUCKETS

    def _statement(self, bucket: str, label: str) -> str:
        return f"Energy rises are typically {bucket} ({label} above the recent baseline)"

    def _note(self, w: AudioWindow, value: float) -> str:
        lvl, base = w.metric("level_dbfs"), w.metric("baseline_dbfs")
        parts = [f"level {lvl:g} dBFS"] if lvl is not None else []
        if base is not None:
            parts.append(f"baseline {base:g} dBFS")
        return "; " + ", ".join(parts) if parts else ""
