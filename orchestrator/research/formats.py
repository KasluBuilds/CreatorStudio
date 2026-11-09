"""Guards for foreign-format studies: learn the mechanics, never copy the expression.

These checks are heuristic safety nets, not a legal determination of originality. They catch
the mechanical failures: pasted transcripts/scripts, verbatim overlap with supplied reference
text, reuse flags, channel branding leaking into the adaptation, quote-heavy text.
"""
from __future__ import annotations

import re
from typing import Iterable

from .models import FormatStudy
from .sources import Issue

MAX_WORDS_PER_FIELD = 60      # an abstraction, not a transcript
SHINGLE = 8                   # consecutive shared words that count as verbatim copying
_WORD = re.compile(r"\w+", re.UNICODE)


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _shingles(words: list[str], n: int = SHINGLE) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _text_fields(f: FormatStudy) -> list[tuple[str, str]]:
    out = [("audience_promise", f.audience_promise), ("topic_framing", f.topic_framing),
           ("demonstration_method", f.demonstration_method), ("visual_explanation", f.visual_explanation),
           ("pacing_notes", f.pacing_notes), ("performance_note", f.performance_note),
           ("adaptation.croatian_context", f.adaptation.croatian_context),
           ("adaptation.differentiation", f.adaptation.differentiation),
           ("adaptation.creator_voice_fit", f.adaptation.creator_voice_fit)]
    out += [(f"structure_beats[{i}]", t) for i, t in enumerate(f.structure_beats)]
    out += [(f"adaptation.what_to_adapt[{i}]", t) for i, t in enumerate(f.adaptation.what_to_adapt)]
    return out


def check_format_study(f: FormatStudy, reference_texts: Iterable[str] = (),
                       path: str = "format_study") -> list[Issue]:
    issues: list[Issue] = []
    for name in ("copies_script", "copies_footage", "copies_branding", "copies_distinctive_expressions"):
        if getattr(f.reuse, name):
            issues.append(Issue("FORMAT_REUSE_DECLARED", "error", f"{path}.reuse.{name}",
                                "adaptation may study structure but must not reuse this"))
    if not f.adaptation.what_to_adapt:
        issues.append(Issue("ADAPTATION_MISSING", "warning", f"{path}.adaptation",
                            "no abstract elements listed to adapt"))
    if not f.adaptation.differentiation.strip():
        issues.append(Issue("NO_DIFFERENTIATION", "warning", f"{path}.adaptation.differentiation",
                            "say how the adaptation differs from the reference"))
    ref_shingles: set[tuple[str, ...]] = set()
    for t in reference_texts:
        ref_shingles |= _shingles(_words(t))
    channel = f.reference_channel.strip().casefold()
    for name, text in _text_fields(f):
        w = _words(text)
        where = f"{path}.{name}"
        if len(w) > MAX_WORDS_PER_FIELD:
            issues.append(Issue("FIELD_TOO_LONG", "error", where,
                                f"{len(w)} words > {MAX_WORDS_PER_FIELD}: summarise, do not transcribe"))
        if ref_shingles and _shingles(w) & ref_shingles:
            issues.append(Issue("VERBATIM_OVERLAP", "error", where,
                                f"shares a run of {SHINGLE}+ words with the reference text"))
        if text.count('"') + text.count("“") + text.count("„") >= 2:
            issues.append(Issue("QUOTED_EXPRESSION", "warning", where,
                                "quotation marks suggest a distinctive expression; paraphrase"))
        if name.startswith("adaptation.") and channel and channel in text.casefold():
            issues.append(Issue("BRANDING_REFERENCE", "warning", where,
                                "adaptation text mentions the reference channel/brand"))
    return issues
