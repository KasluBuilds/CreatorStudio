"""Dialogue-attribution safeguards for free text (annotator rationales, planner descriptions, prompts).

Goal: keep the transcript the ONLY source of creator dialogue, without rejecting ordinary text that
merely contains quotation marks.

What counts as an attribution risk (`attribution_risk`):
  1. A quoted passage of 3+ words placed next to a speech-attribution cue (says / said / told /
     claims / kaže / rekao ...), e.g.  he says "this is a wild reveal"  or  "...," she said.
     EXCEPTION: when the transcript is available and the quoted passage occurs verbatim in it, the
     text is citing existing words, which is allowed (and is verifiable).
  2. A speaker-label prefix at the start of a line (`Creator: ...`, `Host: ...`).
  3. strict mode (prompts, hooks, talking points that must never be scripts): additionally
     a quoted passage of 4+ words that is NOT in the transcript, and first-person phrasing.

Plain quoted terms (`the "humidity drop" problem`, `called "Project Moss"`) are not risks.

Limits (stated, not hidden): unquoted paraphrase attributed to the creator ("the creator says the fan is
useless") is not detected; that needs human review.
"""
from __future__ import annotations

import re

_CUE = (r"says?|said|saying|states?|stated|tells?|told|claims?|claimed|quotes?|quoted|exclaims?|exclaimed|"
        r"whispers?|shouts?|shouted|kaže|kazao|rekao|rekla|izjavio|izjavila|tvrdi|poručuje|veli")
_CUE_RE = re.compile(rf"\b(?:{_CUE})\b", re.I)
_QUOTE_RE = re.compile(r'"([^"]{3,}?)"|“([^”]{3,}?)”|„([^“”]{3,}?)[“”]|«([^»]{3,}?)»')
_LABEL_RE = re.compile(r"(?:^|\n)\s*(?:the\s+)?(?:creator|host|speaker|narrator|kreator|voditelj|presenter)\s*[:\-–—]",
                       re.I)
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'll|I'd|my|mine|myself|me|ja|moj|moja|moje|sam)\b")
_NORM_RE = re.compile(r"[\W_]+", re.UNICODE)


def norm(text: str) -> str:
    return _NORM_RE.sub(" ", text.casefold()).strip()


def transcript_norm(texts) -> str:
    return " | ".join(norm(t) for t in texts)


def _quotes(text: str) -> list[tuple[int, int, str]]:
    out = []
    for m in _QUOTE_RE.finditer(text):
        q = next(g for g in m.groups() if g)
        out.append((m.start(), m.end(), q))
    return out


def in_transcript(quote: str, tnorm: str | None) -> bool:
    return bool(tnorm) and norm(quote) != "" and norm(quote) in tnorm


def attribution_risk(text: str, tnorm: str | None = None, *, strict: bool = False) -> str | None:
    """Returns a short reason when the text looks like attributed or scripted creator speech, else None.
    `tnorm` is transcript_norm(...) of the material, or None when no transcript is available (then
    quotes cannot be shown to be genuine, so a cue + quote is treated as a risk)."""
    if not isinstance(text, str) or not text:
        return None
    if _LABEL_RE.search(text):
        return "starts a line with a speaker label (e.g. 'Creator:'), which reads as dialogue"
    for s, e, q in _quotes(text):
        words = len(q.split())
        genuine = in_transcript(q, tnorm)
        if words >= 3 and not genuine:
            near = text[max(0, s - 60):s] + " " + text[e:e + 40]
            if _CUE_RE.search(near):
                return f"quoted passage ({words} words) next to a speech-attribution cue and not found in the transcript"
        if strict and words >= 4 and not genuine:
            return f"quoted passage of {words} words that is not in the transcript (possible scripted line)"
    if strict and _FIRST_PERSON.search(text):
        return "first-person phrasing in a prompt (prompts must address the creator, not speak for them)"
    return None
