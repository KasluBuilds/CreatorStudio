"""Purpose-scoped consent for Creator DNA learning (schema v5, T-DNA-002).

Consent is the creator's explicit permission, for a project, to learn from footage for named purposes.
It is recorded as events (append-only), withdrawn by events, and checked on every footage ingestion.

The purpose vocabulary is closed. The chosen purposes are engineering defaults, an OPEN product decision
(see CREATOR_DNA.md): they were NOT approved by Lukas.

  style_learning     learn style traits from the creator's footage (required for ingesting footage evidence)
  editing_guidance   let downstream stages (e.g. the Creative Director) use the learned profile
  profile_review     show the profile/evidence to the creator for confirmation and correction

Withdrawal stops further learning immediately and quarantines existing footage-derived evidence. Nothing
is deleted from the log and no source media is touched; physical deletion is a separate explicit decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping

from .schema import DNAError, check_reviewer


class Purpose(str, Enum):
    STYLE_LEARNING = "style_learning"
    EDITING_GUIDANCE = "editing_guidance"
    PROFILE_REVIEW = "profile_review"


PURPOSES = tuple(p.value for p in Purpose)


def parse_time(value: str) -> datetime:
    """ISO-8601 with a UTC offset (a trailing Z is accepted). Naive times are refused: consent expiry must
    not depend on the machine's local zone."""
    if not isinstance(value, str):
        raise DNAError("timestamp must be an ISO-8601 string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DNAError(f"invalid timestamp {value!r}") from exc
    if dt.tzinfo is None:
        raise DNAError(f"timestamp {value!r} must include a UTC offset")
    return dt.astimezone(timezone.utc)


def _clean_purposes(purposes) -> tuple[str, ...]:
    if isinstance(purposes, (str, bytes)) or not purposes:
        raise DNAError("consent needs a non-empty list of purposes")
    out = set()
    for p in purposes:
        v = p.value if isinstance(p, Purpose) else p
        if v not in PURPOSES:
            raise DNAError(f"unknown consent purpose {v!r}; the closed list is {list(PURPOSES)}")
        out.add(v)
    return tuple(sorted(out))


def _clean_provenance(prov: Mapping | None) -> dict:
    if prov is None:
        return {}
    if not isinstance(prov, Mapping):
        raise DNAError("consent provenance must be a mapping")
    out = {}
    for k, v in prov.items():
        if not isinstance(k, str) or not isinstance(v, (str, int, float, bool)) or isinstance(v, float) and v != v:
            raise DNAError("consent provenance holds only short scalar values keyed by strings")
        if isinstance(v, str) and len(v) > 300:
            raise DNAError("consent provenance values are limited to 300 characters")
        out[k] = v
    return out


@dataclass(frozen=True)
class ConsentRecord:
    consent_id: str
    project_id: str
    creator_id: str                     # project-local creator identity (no real name required)
    purposes: tuple[str, ...]
    authorized_by: str                  # "human:<name>": the person who gave the permission
    granted_at: str
    expires_at: str | None = None
    provenance: Mapping = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"consent_id": self.consent_id, "project_id": self.project_id, "creator_id": self.creator_id,
                "purposes": list(self.purposes), "authorized_by": self.authorized_by,
                "granted_at": self.granted_at, "expires_at": self.expires_at, "provenance": dict(self.provenance)}

    @classmethod
    def from_event(cls, d: Mapping, granted_at: str) -> "ConsentRecord":
        """Validated construction from an event payload (also used on replay)."""
        for k in ("consent_id", "project_id", "creator_id", "authorized_by"):
            if not isinstance(d.get(k), str) or not d[k].strip():
                raise DNAError(f"consent {k} must be a non-empty string")
        check_reviewer(d["authorized_by"])
        exp = d.get("expires_at")
        if exp is not None and parse_time(exp) <= parse_time(granted_at):
            raise DNAError("consent expires_at must be later than the grant time")
        return cls(d["consent_id"], d["project_id"], d["creator_id"], _clean_purposes(d.get("purposes")),
                   d["authorized_by"], granted_at, exp, _clean_provenance(d.get("provenance")))


@dataclass
class ConsentState:
    record: ConsentRecord
    withdrawn_at: str | None = None
    withdrawn_by: str | None = None
    withdrawn_reason: str = ""

    def status(self, now: Callable[[], datetime]) -> str:
        """`now` is only called when this consent can expire, so stores whose consents never expire do
        not depend on the clock at all."""
        if self.withdrawn_at is not None:
            return "withdrawn"
        if self.record.expires_at is not None and parse_time(self.record.expires_at) <= now():
            return "expired"
        return "active"

    def to_dict(self, now: Callable[[], datetime]) -> dict:
        d = self.record.to_dict()
        d.update(status=self.status(now), withdrawn_at=self.withdrawn_at, withdrawn_by=self.withdrawn_by,
                 withdrawn_reason=self.withdrawn_reason)
        return d


@dataclass(frozen=True)
class ConsentDecision:
    allowed: bool
    code: str                  # "ok" | "no_project" | "no_consent" | "expired" | "withdrawn" | "purpose_not_covered"
    detail: str
    consent_id: str | None = None
