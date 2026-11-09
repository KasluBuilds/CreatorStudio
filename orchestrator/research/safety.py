"""Secret redaction, untrusted-text sanitising and safe markdown rendering.

Self-contained on purpose (the research package must not import creator_state). The
orchestrator may inject an *additional* redactor; the built-in one always runs first.

Everything a provider returns (titles, publishers, authors, notes, URLs, error messages) is
UNTRUSTED DATA. These helpers bound its size, strip characters that hide or reorder text,
remove secrets, and keep it from being rendered as markup. They cannot make a string
"safe to obey": downstream consumers must still treat it as data (see outputs.py).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable, Optional

PLACEHOLDER = "[REDACTED]"
Redactor = Callable[[str], str]

# ------------------------------------------------------------------ limits
TITLE_MAX = 300
PUBLISHER_MAX = 120
AUTHOR_MAX = 100
MAX_AUTHORS = 50
NOTE_MAX = 500
LANG_MAX = 16
URL_MAX = 2048
ERROR_MAX = 200

# ---------------------------------------------------------------- redaction
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")
_TOKEN_PATTERNS = [re.compile(p) for p in (
    r"\bsk-ant-[A-Za-z0-9_\-]{6,}",                    # Anthropic
    r"\bsk-[A-Za-z0-9_\-]{8,}",                        # generic sk- keys
    r"\bAIza[0-9A-Za-z_\-]{20,}",                      # Google API keys
    r"\bgsk_[A-Za-z0-9]{10,}",                         # Groq
    r"\bgh[pousr]_[A-Za-z0-9]{16,}",                   # GitHub tokens
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",                # Slack
    r"\bAKIA[0-9A-Z]{16}\b",                           # AWS access key id
    r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",  # JWT
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{6,}",
)]
_NAMES = (r"(?:api[_\-]?key|apikey|access[_\-]?token|auth[_\-]?token|refresh[_\-]?token|id[_\-]?token|"
          r"client[_\-]?secret|secret|password|passwd|pwd|passphrase|token|authorization|credentials?)")
_KEYED = re.compile(
    r"(?i)\b((?:[\w.\-]*" + _NAMES + r"[\w.\-]*)|key|auth)(\"?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s\"',;&]+)")
_PHRASE = re.compile(r"(?i)\b(?:password|passwd|passphrase)\s+(?:is|was)\s+\S+(?:\s+\S+)?")


def has_token_pattern(text: str) -> bool:
    return any(p.search(text) for p in _TOKEN_PATTERNS)


def redact_text(text: str, extra: Optional[Redactor] = None) -> str:
    """Remove recognisable secrets. Best effort: a safety net, not a guarantee."""
    if not text:
        return text
    t = _PRIVATE_KEY.sub(PLACEHOLDER, text)
    t = _USERINFO.sub(lambda m: m.group(1) + PLACEHOLDER + "@", t)
    for p in _TOKEN_PATTERNS:
        t = p.sub(PLACEHOLDER, t)
    t = _KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}", t)
    t = _PHRASE.sub(PLACEHOLDER, t)
    if extra is not None:
        t = extra(t)
    return t


_SECRET_PARAM_EXACT = {
    "key", "apikey", "api_key", "api-key", "token", "access_token", "accesstoken", "auth", "auth_token",
    "authorization", "secret", "client_secret", "password", "passwd", "pwd", "pass", "sig", "signature",
    "session", "sessionid", "session_id", "sid", "jwt", "bearer", "credential", "credentials",
    "refresh_token", "id_token", "x-amz-signature", "x-amz-credential", "x-amz-security-token",
}
_SECRET_PARAM_SUFFIX = ("_token", "-token", "_key", "-key", "_secret", "-secret", "_password", "-password")


def is_secret_param(name: str) -> bool:
    """Whole-name match (so `author` and `design` are NOT secrets, `api_key` and `x-api-key` are)."""
    n = name.strip().lower()
    return n in _SECRET_PARAM_EXACT or n.endswith(_SECRET_PARAM_SUFFIX)


# ---------------------------------------------------------------- sanitising
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")
_WS = re.compile(r"\s+")


def sanitize_text(value: object, max_len: int, *, multiline: bool = False,
                  redactor: Optional[Redactor] = None) -> str:
    """Return a bounded, control-character-free, secret-redacted string. Never raises."""
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    s = unicodedata.normalize("NFC", s)
    s = _INVISIBLE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _CTRL.sub("", s)
    s = redact_text(s, redactor)
    if multiline:
        s = "\n".join(_WS.sub(" ", ln).strip() for ln in s.split("\n")).strip()
    else:
        s = _WS.sub(" ", s).strip()
    if len(s) > max_len:
        s = s[: max(0, max_len - 1)] + "…"
    return s


def unsafe_text_problems(value: str, max_len: int, *, multiline: bool = False,
                         check_secrets: bool = True) -> list[str]:
    """Why a stored string would NOT survive sanitize_text unchanged (for validation)."""
    out: list[str] = []
    if len(value) > max_len:
        out.append(f"longer than {max_len} characters")
    if _CTRL.search(value) or _INVISIBLE.search(value):
        out.append("contains control or invisible/bidi characters")
    if not multiline and ("\n" in value or "\r" in value):
        out.append("contains line breaks")
    if check_secrets and redact_text(value) != value:
        out.append("contains secret-like text")
    return out


_INJECTION = [re.compile(p, re.I) for p in (
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)",
    r"disregard\s+(?:the\s+|all\s+|any\s+)?(?:previous|prior|above|system|instructions?)",
    r"\bsystem\s+prompt\b", r"\bnew\s+instructions?\b", r"^\s*(?:system|assistant|developer)\s*:",
    r"<\|[^>]{0,40}\|>", r"\byou\s+(?:are|must)\s+now\b", r"do\s+not\s+(?:tell|inform)\s+the\s+user",
    r"\breveal\s+(?:your|the)\s+(?:prompt|instructions|keys?|secrets?)",
    r"\b(?:run|execute)\s+(?:this\s+)?(?:command|script|code)\b",
)]


def looks_like_instruction(text: str) -> bool:
    """Heuristic tripwire for prompt-injection phrasing. Flags, never proves."""
    return any(p.search(text) for p in _INJECTION)


# ------------------------------------------------------------ safe markdown
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#<>|~!&{}])")


def md_escape(value: object, max_len: int = TITLE_MAX) -> str:
    """Single-line, escaped text: cannot start headings, links, tables, HTML or code spans."""
    return _MD_SPECIAL.sub(r"\\\1", sanitize_text(value, max_len))


def md_code(value: object, max_len: int = URL_MAX) -> str:
    """Inline code span for URLs: no clickable link, no breaking out of the span."""
    return "`" + sanitize_text(value, max_len).replace("`", "%60") + "`"
