"""Best-effort secret scrubbing. Applied to everything persisted (state, logs, handoff).

This is a safety net, not a licence to pass secrets around: callers should never
put credentials into state in the first place.
"""
from __future__ import annotations

import re

PLACEHOLDER = "[REDACTED]"

_KEYED = re.compile(
    r"""(?ix)
    (\b[\w.-]*(?:api[_-]?key|auth[_-]?token|access[_-]?token|secret|password|passwd|authorization|token)[\w.-]*)
    (["']?\s*[:=]\s*["']?)
    (?!\[REDACTED\])
    ([^\s"',;]+)
    """
)
_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
]


def redact_text(text: str) -> str:
    if not text:
        return text
    for pat in _PATTERNS:
        text = pat.sub(PLACEHOLDER, text)
    return _KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}", text)


def redact_obj(obj):
    """Recursively redact all strings (dict keys are left alone)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj
