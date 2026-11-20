"""Evidence ledger: contradictions survive relabelling, deletion and context changes (audit F-05).

THREAT. A claim's citations are plain data. Whoever can edit them (an LLM, a bad merge, a hand-edited JSON file) can
delete a contradicting citation, clear its assessed stance, relabel it as 'context', or retarget the claim text,
and the claim then verifies as if nothing opposed it. Re-deriving status from the citations alone cannot notice
what is no longer there.

DESIGN. The builder appends every citation it records to an append-only, hash-chained ledger kept in the brief,
with the stance that was ASSESSED. Assessment then compares the ledger with the claims:
  * a recorded CONTRADICTION whose claim is gone, whose text changed, or which no longer has a citation from that
    source assessed as contradicting is reported as an unresolved contradiction: it blocks VERIFIED, raises the
    safety warning on safety-critical claims, appears in the caveats and fails validation (CONTRADICTION_ERASED);
  * it stops being unresolved only through a ContradictionResolution: an explicit HUMAN attestation (never a
    process, never the LLM) over the ledger entry, the claim text as it is now and a written rationale. The
    entry itself is never removed, so the history stays auditable;
  * the chain digests are recomputed on validation, so editing, reordering, inserting or dropping an entry in the
    middle is caught (LEDGER_TAMPERED);
  * the orchestrator can seal the head with a 'ledger' attestation, and a policy with require_ledger_seal makes
    validation and export refuse an unsealed or re-written ledger.

LIMITS (stated plainly). Without a seal the ledger is tamper-EVIDENT, not tamper-PROOF: whoever can rewrite the
whole chain consistently can forge it, and an LLM-facing process that holds the brief can do that. Real protection
needs the orchestrator to seal the head after each build (it holds the key; this package does not) or to persist the
head in storage the agent cannot write. A contradiction that was never given to the builder is not in the ledger.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Iterable, Optional

from .attestation import TrustPolicy, canonical_bytes, sha256_text
from .models import Citation, Claim, ContradictionResolution, LedgerEntry, Stance

CONTRADICTS = Stance.CONTRADICTS.value


def effective_stance(c: Citation) -> Stance:
    """The stance that is in force: the one that was assessed, if any, else the current label."""
    return c.assessed_stance if c.assessed_stance is not None else c.stance


def _entry_core(claim_id: str, claim_text_sha256: str, claim_kind: str, source_id: str, stance: str, locator: str,
                excerpt_sha256: str) -> dict:
    return {"claim_id": claim_id, "claim_text_sha256": claim_text_sha256, "claim_kind": claim_kind,
            "source_id": source_id, "stance": stance, "locator": locator, "excerpt_sha256": excerpt_sha256}


def _digest(prev: str, core: dict) -> str:
    return hashlib.sha256(canonical_bytes({"prev": prev, "entry": core})).hexdigest()


def _entry_id(core: dict) -> str:
    return hashlib.sha256(canonical_bytes(core)).hexdigest()[:16]


def make_entry(prev: str, claim: Claim, c: Citation) -> LedgerEntry:
    core = _entry_core(claim.id, sha256_text(claim.text), claim.kind.value, c.source_id, effective_stance(c).value,
                       c.locator, c.excerpt_proof.excerpt_sha256 if c.excerpt_proof is not None else "")
    return LedgerEntry(entry_id=_entry_id(core), claim_id=claim.id, claim_text_sha256=core["claim_text_sha256"],
                       claim_kind=claim.kind.value, source_id=c.source_id, stance=core["stance"], locator=c.locator,
                       excerpt_sha256=core["excerpt_sha256"], prev=prev, digest=_digest(prev, core))


def head(ledger: list) -> str:
    return ledger[-1].digest if ledger else ""


def chain_problems(ledger: Iterable) -> list[str]:
    """Why the chain is not intact (empty when it is): wrong links, wrong digests, duplicate ids."""
    out: list[str] = []
    prev, seen = "", set()
    for i, e in enumerate(ledger):
        try:
            core = _entry_core(e.claim_id, e.claim_text_sha256, e.claim_kind, e.source_id, e.stance, e.locator,
                               e.excerpt_sha256)
            if e.prev != prev:
                out.append(f"entry {i} does not follow the previous entry")
            if e.digest != _digest(e.prev, core):
                out.append(f"entry {i} digest does not match its content")
            if e.entry_id != _entry_id(core):
                out.append(f"entry {i} id does not match its content")
            if e.entry_id in seen:
                out.append(f"entry {i} repeats an earlier entry id")
            seen.add(e.entry_id)
            prev = e.digest
        except Exception as ex:  # noqa: BLE001 - a malformed entry is a broken chain, never a crash
            out.append(f"entry {i} is malformed ({type(ex).__name__})")
            break
    return out


def record(brief) -> int:
    """Append an entry for every citation not yet in the ledger (same claim, source, assessed stance, locator and
    excerpt). Idempotent and append-only: nothing is ever removed or changed. Returns the number appended."""
    have = {e.entry_id for e in brief.evidence_ledger}
    added = 0
    for claim in brief.claims:
        for c in claim.citations:
            e = make_entry(head(brief.evidence_ledger), claim, c)
            if e.entry_id not in have:
                brief.evidence_ledger.append(e)
                have.add(e.entry_id)
                added += 1
    return added


@dataclass
class ContradictionState:
    entry: LedgerEntry
    state: str                      # "present" | "erased" | "resolved"
    reason: str = ""
    resolver: str = ""              # attestor of a valid resolution


def contradiction_states(ledger: Iterable, resolutions: Iterable, claim: Optional[Claim], claim_id: str,
                         trust: TrustPolicy, today: dt.date) -> list[ContradictionState]:
    """State of every recorded contradiction against `claim_id`, in ledger order. `claim` is the claim as it is NOW
    (None when it no longer exists in the brief)."""
    by_entry: dict[str, list] = {}
    for r in resolutions:
        if isinstance(r, ContradictionResolution):
            by_entry.setdefault(r.entry_id, []).append(r)
    now_sha = sha256_text(claim.text) if claim is not None else ""
    out: list[ContradictionState] = []
    for e in ledger:
        if e.claim_id != claim_id or e.stance != CONTRADICTS:
            continue
        why = ""
        if claim is None:
            why = "the claim it was recorded against no longer exists"
        elif now_sha != e.claim_text_sha256:
            why = "the claim text changed after the contradiction was recorded"
        elif claim.kind.value != e.claim_kind:
            why = f"the claim was relabelled from {e.claim_kind} to {claim.kind.value} after the contradiction was recorded"
        elif not any(c.source_id == e.source_id and effective_stance(c) is Stance.CONTRADICTS for c in claim.citations):
            why = f"no citation from {e.source_id} is assessed as contradicting any more"
        resolver = ""
        for r in by_entry.get(e.entry_id, []):
            v = trust.verify_resolution(r, e, now_sha, today)
            if v.ok:
                resolver = r.attestation.attestor
                break
        if not why:
            out.append(ContradictionState(e, "present"))
        elif resolver:
            out.append(ContradictionState(e, "resolved", why, resolver))
        else:
            out.append(ContradictionState(e, "erased", why))
    return out


def orphan_claim_ids(ledger: Iterable, claims: Iterable[Claim]) -> list[str]:
    """Claim ids that carry a recorded contradiction but are no longer in the brief."""
    live = {c.id for c in claims}
    return sorted({e.claim_id for e in ledger if e.stance == CONTRADICTS and e.claim_id not in live})
