"""Transcription result format and replaceable provider interface.

A transcript is an ENGINE'S RECOGNITION, not ground truth. This module therefore:
  * stores `text` exactly as the engine returned it (no normalisation, no spelling or
    grammar correction, diacritics/slang/false starts/repetitions kept);
  * is NEVER edited and NEVER marked reviewed: it is the engine's output, forever. Human review
    lives in a separate layer (review.py) that points at it by id + content hash;
  * keeps word timings only if the engine supplied them (`words=None` otherwise) and has
    no function that derives words from phrase timings;
  * records the language the ENGINE reported (`language`, optional `variety` such as
    "kajkavian", optional `language_spans` for code-switching) - nothing is guessed here.

No transcription engine is installed or chosen (open decision #11). `UnavailableProvider`
and `FixtureProvider` (synthetic data for tests) are the only providers.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from .models import (CapabilityError, MediaAnalysisError, MediaRef, Provenance, atomic_write_json,
                     check_range, check_time, check_unit, read_json, stable_hash, t_key)

SCHEMA = "media_analysis.transcript/1"
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
WORD_TOLERANCE_S = 0.01


def _check_lang(tag: str | None, name: str) -> str | None:
    if tag is None:
        return None
    if not _LANG_RE.match(tag):
        raise MediaAnalysisError(f"{name} {tag!r} is not a BCP-47-style tag (e.g. 'hr', 'en', 'hr-HR')")
    return tag


@dataclass(frozen=True)
class Word:
    text: str
    start_s: float
    end_s: float
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise MediaAnalysisError("empty word")
        check_range(self.start_s, self.end_s)
        check_unit(self.confidence, "word confidence")


@dataclass(frozen=True)
class LanguageSpan:
    """Character range [start_char, end_char) of `segment.text` in `language`, as reported by the engine."""
    start_char: int
    end_char: int
    language: str

    def __post_init__(self) -> None:
        if not (0 <= self.start_char < self.end_char):
            raise MediaAnalysisError("language span needs 0 <= start_char < end_char")
        _check_lang(self.language, "span language")


@dataclass(frozen=True)
class TranscriptSegment:
    media_id: str
    start_s: float
    end_s: float
    text: str                                   # verbatim engine output
    language: str | None = None                 # primary language reported by the engine
    variety: str | None = None                  # e.g. "kajkavian" if the engine reports a dialect
    language_confidence: float | None = None
    confidence: float | None = None             # engine-supplied only; never computed here
    speaker_id: str | None = None               # None until a diarisation engine supplies it
    words: tuple[Word, ...] | None = None       # None = engine gave no word timings
    language_spans: tuple[LanguageSpan, ...] | None = None

    def __post_init__(self) -> None:
        if not self.media_id:
            raise MediaAnalysisError("segment needs media_id")
        check_range(self.start_s, self.end_s)
        if not self.text.strip():
            raise MediaAnalysisError("segment text is empty")
        _check_lang(self.language, "language")
        check_unit(self.confidence, "confidence")
        check_unit(self.language_confidence, "language_confidence")
        if self.words is not None:
            object.__setattr__(self, "words", tuple(self.words))
            if not self.words:
                raise MediaAnalysisError("words must be None or non-empty")
            prev = self.start_s
            for w in self.words:
                if w.start_s < self.start_s - WORD_TOLERANCE_S or w.end_s > self.end_s + WORD_TOLERANCE_S:
                    raise MediaAnalysisError(f"word {w.text!r} lies outside its segment")
                if w.start_s < prev - WORD_TOLERANCE_S:
                    raise MediaAnalysisError("word timings are not in order")
                prev = w.start_s
        if self.language_spans is not None:
            object.__setattr__(self, "language_spans", tuple(self.language_spans))
            last = 0
            for sp in self.language_spans:
                if sp.end_char > len(self.text) or sp.start_char < last:
                    raise MediaAnalysisError("language spans must be ordered, non-overlapping and inside the text")
                last = sp.end_char

    @property
    def record_id(self) -> str:
        return "seg_" + stable_hash(self.media_id, t_key(self.start_s), t_key(self.end_s), self.text)

    def to_dict(self) -> dict:
        return {
            "media_id": self.media_id, "start_s": self.start_s, "end_s": self.end_s, "text": self.text,
            "language": self.language, "variety": self.variety,
            "language_confidence": self.language_confidence, "confidence": self.confidence,
            "speaker_id": self.speaker_id,
            "words": None if self.words is None else
            [{"text": w.text, "start_s": w.start_s, "end_s": w.end_s, "confidence": w.confidence} for w in self.words],
            "language_spans": None if self.language_spans is None else
            [{"start_char": s.start_char, "end_char": s.end_char, "language": s.language} for s in self.language_spans],
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "TranscriptSegment":
        words = d.get("words")
        spans = d.get("language_spans")
        return cls(
            media_id=d["media_id"], start_s=d["start_s"], end_s=d["end_s"], text=d["text"],
            language=d.get("language"), variety=d.get("variety"),
            language_confidence=d.get("language_confidence"), confidence=d.get("confidence"),
            speaker_id=d.get("speaker_id"),
            words=None if words is None else tuple(Word(**w) for w in words),
            language_spans=None if spans is None else tuple(LanguageSpan(**s) for s in spans))


@dataclass(frozen=True)
class Transcript:
    media_id: str
    segments: tuple[TranscriptSegment, ...]
    provenance: Provenance
    requested_language: str | None = None
    verification: str = "unverified"            # always "unverified"; reviews are a separate layer (review.py)
    duration_s: float | None = None             # source duration, if known (bounds check)
    word_timestamps: str = "none"               # "engine" | "none"
    audio_copy_id: str | None = None            # analysis copy that was transcribed (times stay original-timeline)

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", tuple(self.segments))
        if self.verification != "unverified":
            raise MediaAnalysisError("a Transcript is engine output and is always 'unverified'; "
                                     "record human review with review.TranscriptReview instead")
        prev = (-1.0, -1.0)
        for s in self.segments:
            if s.media_id != self.media_id:
                raise MediaAnalysisError(f"segment for {s.media_id} inside transcript of {self.media_id}")
            check_range(s.start_s, s.end_s, self.duration_s)
            if (s.start_s, s.end_s) < prev:
                raise MediaAnalysisError("segments must be ordered by start time (not reordered silently)")
            prev = (s.start_s, s.end_s)
        has_words = any(s.words for s in self.segments)
        if has_words and self.word_timestamps != "engine":
            raise MediaAnalysisError("word timings present but transcript does not declare engine-supplied word_timestamps")

    @property
    def content_hash(self) -> str:
        """Hash of what the engine said (segments + engine identity), independent of file timestamps."""
        return stable_hash(self.media_id, self.provenance.engine, self.provenance.version,
                           [s.to_dict() for s in self.segments], n=16)

    @property
    def transcript_id(self) -> str:
        return f"{self.media_id}.{self.provenance.engine}-{self.provenance.settings_hash}"

    def full_text(self) -> str:
        return "\n".join(s.text for s in self.segments)

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "media_id": self.media_id, "requested_language": self.requested_language,
                "verification": self.verification, "duration_s": self.duration_s,
                "word_timestamps": self.word_timestamps, "audio_copy_id": self.audio_copy_id,
                "provenance": self.provenance.to_dict(), "segments": [s.to_dict() for s in self.segments]}

    @classmethod
    def from_dict(cls, d: Mapping) -> "Transcript":
        if d.get("schema") != SCHEMA:
            raise MediaAnalysisError(f"unrecognised transcript schema {d.get('schema')!r}")
        return cls(media_id=d["media_id"], segments=tuple(TranscriptSegment.from_dict(s) for s in d["segments"]),
                   provenance=Provenance.from_dict(d["provenance"]), requested_language=d.get("requested_language"),
                   verification=d.get("verification", "unverified"),
                   duration_s=d.get("duration_s"), word_timestamps=d.get("word_timestamps", "none"),
                   audio_copy_id=d.get("audio_copy_id"))


# ------------------------------------------------------------------- persistence
def save_transcript(directory: str | os.PathLike, t: Transcript) -> Path:
    """Write-once per (media, engine, settings): an existing different transcript is never overwritten."""
    p = Path(directory) / f"{t.transcript_id}.transcript.json"
    if p.exists():
        if Transcript.from_dict(read_json(p)) == t:
            return p
        raise MediaAnalysisError(f"{p.name} already exists with different content; refusing to overwrite")
    atomic_write_json(p, t.to_dict())
    return p


def load_transcript(path: str | os.PathLike) -> Transcript:
    return Transcript.from_dict(read_json(path))


# ---------------------------------------------------------------------- providers
@dataclass(frozen=True)
class ProviderCapabilities:
    available: bool
    reason: str | None = None                   # why not available
    languages: tuple[str, ...] = ()             # tags it can handle; "*" = any
    varieties: tuple[str, ...] = ()             # e.g. ("kajkavian",) only if really supported
    word_timestamps: bool = False
    speaker_labels: bool = False


@dataclass(frozen=True)
class TranscriptionRequest:
    media: MediaRef
    audio_path: str                             # an analysis copy (or an audio original, read-only)
    language: str | None = None                 # hint; None = engine decides
    settings: Mapping = field(default_factory=dict)
    audio_copy_id: str | None = None            # copy_id of the analysis copy at `audio_path`, if it is one


@runtime_checkable
class TranscriptionProvider(Protocol):
    name: str
    version: str
    kind: str                                   # "ml" | "synthetic_fixture"
    def capabilities(self) -> ProviderCapabilities: ...
    def transcribe(self, request: TranscriptionRequest) -> Sequence[TranscriptSegment]: ...


class UnavailableProvider:
    """Placeholder that fails loudly. Used until a transcription engine is chosen."""
    kind = "ml"

    def __init__(self, name: str = "none", reason: str = "no transcription engine is installed or selected"):
        self.name, self.version, self._reason = name, "0", reason

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(available=False, reason=self._reason)

    def transcribe(self, request):
        raise CapabilityError(self._reason)


class FixtureProvider:
    """Replays prepared segments. SYNTHETIC: proves the plumbing, not recognition quality."""
    kind = "synthetic_fixture"
    version = "1"

    def __init__(self, segments: Sequence[TranscriptSegment], languages=("hr", "en"), varieties=("kajkavian",),
                 word_timestamps: bool = True, name: str = "fixture"):
        self.name, self._segments = name, tuple(segments)
        self._caps = ProviderCapabilities(True, None, tuple(languages), tuple(varieties), word_timestamps, False)

    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    def transcribe(self, request):
        return [s for s in self._segments if s.media_id == request.media.media_id]


def run_transcription(provider: TranscriptionProvider, request: TranscriptionRequest) -> Transcript:
    """Run a provider and validate/stamp its result. Raises CapabilityError when it cannot work."""
    caps = provider.capabilities()
    if not caps.available:
        raise CapabilityError(f"transcription provider {provider.name!r} unavailable: {caps.reason}")
    lang = request.language
    if lang and "*" not in caps.languages and lang.split("-")[0] not in {l.split("-")[0] for l in caps.languages}:
        raise CapabilityError(f"provider {provider.name!r} does not support language {lang!r}")
    if not Path(request.audio_path).is_file():
        raise MediaAnalysisError(f"audio input not found: {request.audio_path}")
    segments = tuple(provider.transcribe(request))
    for s in segments:
        if s.media_id != request.media.media_id:
            raise MediaAnalysisError("provider returned a segment for a different media id")
        if s.words and not caps.word_timestamps:
            raise MediaAnalysisError("provider does not declare word timestamps but returned word timings")
        if s.speaker_id and not caps.speaker_labels:
            raise MediaAnalysisError("provider does not declare speaker labels but returned a speaker id")
        if s.variety and s.variety not in caps.varieties:
            raise MediaAnalysisError(f"provider does not declare variety {s.variety!r} but returned it")
    prov = Provenance(provider.name, provider.version, provider.kind,
                      {"language": lang, **dict(request.settings)},
                      notes="synthetic fixture, not a real transcription" if provider.kind == "synthetic_fixture" else "")
    return Transcript(request.media.media_id, segments, prov, lang, "unverified",
                      request.media.duration_s, "engine" if any(s.words for s in segments) else "none",
                      request.audio_copy_id)
