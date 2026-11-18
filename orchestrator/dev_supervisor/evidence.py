"""Evidence model: every telemetry value carries a source label, a timestamp and a staleness check.

Labels: OBSERVED (read directly from a local source now), REPORTED (stated by a third party / statusline
payload, not independently read), INFERRED (derived from other values), UNAVAILABLE (no source; value is None
and `reason` says why). An UNAVAILABLE evidence never holds a value; no exact number is ever invented.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


class Label(str, enum.Enum):
    OBSERVED = "OBSERVED"
    REPORTED = "REPORTED"
    INFERRED = "INFERRED"
    UNAVAILABLE = "UNAVAILABLE"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(ts: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Evidence:
    label: Label
    value: Any = None
    timestamp: str = ""
    source: str = ""
    reason: str = ""

    def __post_init__(self):
        if self.label == Label.UNAVAILABLE and self.value is not None:
            raise ValueError("UNAVAILABLE evidence must not carry a value")
        if self.label != Label.UNAVAILABLE and self.value is None:
            raise ValueError("%s evidence needs a value" % self.label.value)

    @property
    def available(self) -> bool:
        return self.label != Label.UNAVAILABLE

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        t = parse_ts(self.timestamp)
        if t is None:
            return None
        return ((now or datetime.now(timezone.utc)) - t).total_seconds()

    def is_stale(self, max_age_seconds: float, now: Optional[datetime] = None) -> bool:
        """True when older than max_age_seconds or the timestamp is missing/unparseable."""
        age = self.age_seconds(now)
        return age is None or age > max_age_seconds

    def to_dict(self) -> dict:
        d = {"label": self.label.value, "value": self.value, "timestamp": self.timestamp, "source": self.source}
        if self.reason:
            d["reason"] = self.reason
        return d


def _mk(label, value, source, timestamp, reason=""):
    return Evidence(label, value, timestamp or now_iso(), source, reason)


def observed(value, source="", timestamp=None) -> Evidence:
    return _mk(Label.OBSERVED, value, source, timestamp)


def reported(value, source="", timestamp=None) -> Evidence:
    return _mk(Label.REPORTED, value, source, timestamp)


def inferred(value, source="", timestamp=None) -> Evidence:
    return _mk(Label.INFERRED, value, source, timestamp)


def unavailable(reason: str, source="", timestamp=None) -> Evidence:
    return _mk(Label.UNAVAILABLE, None, source, timestamp, reason)
