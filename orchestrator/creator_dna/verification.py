"""Attribution verification contract (schema v5, T-DNA-002).

Why this exists. An `EvidenceAttribution` stamp is only a CLAIM that an upstream eligibility decision found a
piece of footage eligible as evidence about the creator. A caller can build a perfectly well-formed stamp
that no upstream decision ever backed. The store therefore never treats a stamp as proof: before footage
evidence is written, an injected `AttributionVerifier` must answer VERIFIED for exactly that evidence.

Dependency direction. Creator DNA defines this contract and imports nothing from `speaker_attribution` or
`media_analysis`. A future integration adapter (outside this package) implements `AttributionVerifier`
against the real attribution store and is handed to `DNAStore(verifier=...)`.

Outcomes (every non-VERIFIED outcome refuses the write; none is a "soft pass"):
  VERIFIED  the upstream decision exists, is current, is eligible for the creator and covers this evidence
  STALE     the decision existed but was withdrawn, superseded or corrected since it was stamped
  UNKNOWN   the verifier has no record of this decision (e.g. a forged or foreign stamp)
  INVALID   a decision exists but does not match this evidence (other media, record, channel or time range)
A verifier that is unreachable raises `VerificationUnavailable` (or any exception); the store treats that
as a refusal too ("unavailable"). Nothing is verified by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .schema import EvidenceAttribution


class Outcome(str, Enum):
    VERIFIED = "verified"
    STALE = "stale"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class VerificationUnavailable(Exception):
    """Raised by a verifier that cannot answer right now (upstream store unreadable, offline, ...)."""


@dataclass(frozen=True)
class Verification:
    outcome: Outcome
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, Outcome):
            raise TypeError("Verification.outcome must be an Outcome")

    @classmethod
    def verified(cls, reason: str = "") -> "Verification":
        return cls(Outcome.VERIFIED, reason)

    @classmethod
    def stale(cls, reason: str) -> "Verification":
        return cls(Outcome.STALE, reason)

    @classmethod
    def unknown(cls, reason: str) -> "Verification":
        return cls(Outcome.UNKNOWN, reason)

    @classmethod
    def invalid(cls, reason: str) -> "Verification":
        return cls(Outcome.INVALID, reason)


@dataclass(frozen=True)
class VerificationRequest:
    """Everything a verifier needs to check ONE evidence item against upstream state. Built by the store
    from the evidence itself, never from caller-supplied claims beyond the stamp."""
    evidence_id: str
    source_id: str
    media_id: str | None               # origin.media_id when the evidence has an origin
    source_hash: str | None
    record_id: str | None              # origin.record_id (the upstream record the decision was made for)
    modality: str
    method: str
    start_s: float | None
    end_s: float | None
    stamp: EvidenceAttribution


@runtime_checkable
class AttributionVerifier(Protocol):
    def verify(self, request: VerificationRequest) -> Verification: ...


def request_for(evidence) -> VerificationRequest:
    if evidence.attribution is None:
        raise ValueError("evidence has no attribution stamp to verify")
    o = evidence.origin
    return VerificationRequest(
        evidence_id=evidence.evidence_id, source_id=evidence.source_id,
        media_id=None if o is None else o.media_id, source_hash=evidence.source_hash,
        record_id=None if o is None else o.record_id, modality=evidence.modality.value,
        method=evidence.method.value, start_s=evidence.start_s, end_s=evidence.end_s, stamp=evidence.attribution)


def safe_verify(verifier, evidence) -> tuple[Outcome | None, str]:
    """Run the verifier defensively. Returns (outcome, reason); outcome is None when the verifier is
    missing, raised, or returned something that is not a Verification (all treated as a refusal)."""
    if verifier is None:
        return None, "no attribution verifier is configured (fail closed)"
    try:
        result = verifier.verify(request_for(evidence))
    except VerificationUnavailable as exc:
        return None, f"verifier unavailable: {str(exc)[:160]}"
    except Exception as exc:                      # a broken verifier must never read as a pass
        return None, f"verifier failed ({type(exc).__name__}); treated as unavailable"
    if not isinstance(result, Verification):
        return None, "verifier returned an invalid response; treated as unavailable"
    return result.outcome, result.reason[:200]
