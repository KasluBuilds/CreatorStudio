"""Verification side of the trust boundary. NOTHING IN THIS PACKAGE CAN SIGN.

Two authorities exist OUTSIDE the research package (the future orchestrator provides them):
  * a REVIEW boundary that authenticates a human reviewer (or runs a registered verification process)
    and signs an `Attestation` over exactly what was reviewed;
  * an INGESTION boundary that fetches pages and signs a `RetrievalRecord` (and an `ExcerptProof` tying a
    citation locator to the retrieved content).

The research package receives only a VERIFIER: a callable `(purpose, message, signature, key_id) -> bool`
(purpose is "review" or "ingestion"). It holds no key, imports no cryptographic primitive and contains
no code that mints anything; `unsigned_*` helpers below only assemble the data a signer must sign.

WHAT THE SIGNATURES PROTECT (assuming the orchestrator's authorities and verifier are sound)
  * every decision-relevant field is inside the signed subject, so editing it after review invalidates
    the attestation (source: identity, exact URLs, classification, peer review, retraction/correction,
    publication date, publisher, authors, lineage, DOI, work id, retrieval; citation: claim id/text/kind/
    time-sensitivity/evergreen justification/risk, the source digest, stance, assessed stance, locator,
    assessment, excerpt hash; and more);
  * attestations are bound to a brief id (replay across briefs fails), carry an id (revocable), a key id
    (rotation) and an expiry; a missing, expired, revoked, unbound or mismatched attestation fails closed;
  * retrieval records and excerpt proofs can only come from the ingestion boundary.

WHAT THEY DO NOT PROTECT
  * Reviewer identity: a signature proves the review service signed it, not that "human:ana" is who they
    say. Authenticating reviewers is the review service's job.
  * Process isolation: this is an INTERFACE. If the orchestrator injects an in-process verifier that
    closes over a key, that key can be reached by introspection. Real separation needs the verifier to be
    a call to a separate service/process; this package cannot enforce that and does not pretend to.
  * A symmetric-key verifier can also sign; prefer asymmetric signatures in the orchestrator.
  * Truth of a process record: methods are registered, documented and declared reproducible /
    model-free by whoever registers them; the package never re-runs a check.
  * Key revocation/rotation state lives with the verifier and the optional RevocationList.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .models import (Attestation, Citation, Claim, ExcerptProof, ProcessRecord, RetrievalRecord, Source)
from .safety import NOTE_MAX, unsafe_text_problems

SCOPE_SOURCE = "source"
SCOPE_CITATION = "citation"
SCOPE_EVERGREEN = "evergreen"
SCOPE_INDEPENDENCE = "independence"
SCOPE_KIND = "claim_kind"
SCOPE_RISK = "risk_downgrade"
ALL_SCOPES = frozenset({SCOPE_SOURCE, SCOPE_CITATION, SCOPE_EVERGREEN, SCOPE_INDEPENDENCE, SCOPE_KIND, SCOPE_RISK})
PURPOSE_REVIEW = "review"
PURPOSE_INGESTION = "ingestion"
METHOD_SCOPES = frozenset({"source_type", "peer_review", "citation_support", "evergreen_exemption"})
_PROCESS_NEEDS = {SCOPE_SOURCE: "source_type", SCOPE_CITATION: "citation_support", SCOPE_EVERGREEN: "evergreen_exemption"}
DEFAULT_MAX_AGE_DAYS = 180
MAX_EXCERPT_CHARS = 600

# (purpose, message, signature, key_id) -> True only if the signature is valid AND the key is currently accepted
Verifier = Callable[[str, bytes, str, str], bool]

_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9_.@ \-]{1,60}$")
_METHOD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{2,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------------------------------------------ canonical encoding
def canonical_bytes(obj) -> bytes:
    """Unambiguous encoding: sorted keys, no whitespace, ASCII-only escapes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def subject_digest(subject: dict) -> str:
    return hashlib.sha256(canonical_bytes(subject)).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------- subjects
def _iso(d: Optional[dt.date]) -> Optional[str]:
    return d.isoformat() if isinstance(d, dt.date) else None


def source_fields(s: Source) -> dict:
    """EXACT values (no URL normalisation): everything that can change a tier, freshness, lineage or
    independence decision is in here."""
    from .sources import normalize_doi
    r = s.retrieval
    return {"id": s.id, "url": s.url, "final_url": s.final_url, "requested_url": s.requested_url,
            "doi": normalize_doi(s.doi), "source_type": s.source_type.value, "peer_reviewed": s.peer_reviewed,
            "retracted": bool(s.retracted), "correction_state": s.correction_state, "published": s.published,
            "retrieved_at": _iso(s.retrieved_at), "publisher": s.publisher, "authors": list(s.authors),
            "derived_from": s.derived_from, "version_of": s.version_of,
            "original_source_url": s.original_source_url, "work_id": s.work_id,
            "retrieval_id": r.record_id if r else None, "content_sha256": r.content_sha256 if r else None}


def source_subject(s: Source) -> dict:
    return {"scope": SCOPE_SOURCE, **source_fields(s)}


def claim_fields(c: Claim) -> dict:
    return {"claim_id": c.id, "claim_text_sha256": sha256_text(c.text), "kind": c.kind.value,
            "time_sensitive": c.time_sensitive, "evergreen_justification": c.evergreen_justification,
            "risk_class": c.risk_class.value}


def citation_subject(claim: Claim, c: Citation, s: Source) -> dict:
    p = c.excerpt_proof
    return {"scope": SCOPE_CITATION, **claim_fields(claim), "source_digest": subject_digest(source_subject(s)),
            "stance": c.stance.value, "assessed_stance": c.assessed_stance.value if c.assessed_stance else None,
            "previous_stance": c.previous_stance.value if c.previous_stance else None,
            "locator": c.locator, "note": c.note, "assessment": c.assessment,
            "excerpt_sha256": p.excerpt_sha256 if p else None, "record_id": p.record_id if p else None}


def evergreen_subject(c: Claim) -> dict:
    return {"scope": SCOPE_EVERGREEN, **claim_fields(c)}


def kind_subject(c: Claim) -> dict:
    return {"scope": SCOPE_KIND, "claim_id": c.id, "claim_text_sha256": sha256_text(c.text),
            "original_kind": c.original_kind.value if c.original_kind else None, "kind": c.kind.value}


def risk_subject(c: Claim) -> dict:
    return {"scope": SCOPE_RISK, "claim_id": c.id, "claim_text_sha256": sha256_text(c.text),
            "declared_risk": c.risk_class.value}


def independence_subject(sources: Iterable[Source]) -> dict:
    return {"scope": SCOPE_INDEPENDENCE,
            "sources": sorted(({"id": s.id, "digest": subject_digest(source_subject(s))} for s in sources),
                              key=lambda x: x["id"])}


# --------------------------------------------------------------------- signing messages
def signing_message(a: Attestation) -> bytes:
    """The exact bytes the REVIEW boundary must sign."""
    return canonical_bytes({
        "v": 2, "purpose": PURPOSE_REVIEW, "kind": a.kind, "attestor": a.attestor, "scope": a.scope,
        "attestation_id": a.attestation_id, "context_id": a.context_id, "key_id": a.key_id,
        "issued_at": _iso(a.issued_at), "expires_at": _iso(a.expires_at), "subject_digest": a.subject_digest,
        "process": None if a.process is None else {
            "method_id": a.process.method_id, "method_version": a.process.method_version,
            "verified_at": a.process.verified_at, "evidence_checked": a.process.evidence_checked,
            "result": a.process.result, "record_reference": a.process.record_reference}})


def retrieval_message(r: RetrievalRecord) -> bytes:
    """The exact bytes the INGESTION boundary must sign for a retrieval record."""
    return canonical_bytes({
        "v": 2, "purpose": PURPOSE_INGESTION, "type": "retrieval", "record_id": r.record_id,
        "requested_url": r.requested_url, "final_url": r.final_url, "retrieved_at": _iso(r.retrieved_at),
        "content_sha256": r.content_sha256, "content_ref": r.content_ref, "gatherer": r.gatherer, "key_id": r.key_id})


def excerpt_message(r: RetrievalRecord, p: ExcerptProof) -> bytes:
    """The exact bytes the INGESTION boundary must sign for an excerpt proof (binds locator -> content)."""
    return canonical_bytes({
        "v": 2, "purpose": PURPOSE_INGESTION, "type": "excerpt", "record_id": p.record_id,
        "content_sha256": r.content_sha256, "locator": p.locator, "excerpt_sha256": p.excerpt_sha256,
        "key_id": p.key_id})


def unsigned_attestation(*, kind: str, attestor: str, scope: str, subject: dict, context_id: str, key_id: str,
                         attestation_id: str, issued_at: dt.date, expires_at: Optional[dt.date] = None,
                         process: Optional[ProcessRecord] = None) -> Attestation:
    """Assemble an attestation WITHOUT a signature, for the review boundary to sign. Holds no secret."""
    return Attestation(kind=kind, attestor=attestor, scope=scope, issued_at=issued_at,
                       subject_digest=subject_digest(subject), signature="", process=process,
                       attestation_id=attestation_id, context_id=context_id, key_id=key_id, expires_at=expires_at)


# --------------------------------------------------------------------- method registry
@dataclass(frozen=True)
class VerificationMethod:
    """A named, documented verification mechanism. NOT a confidence score."""
    method_id: str
    version: str
    title: str
    documentation_ref: str            # where the procedure is written down (doc path / URL / spec id)
    scopes: frozenset                 # subset of METHOD_SCOPES it is competent to verify
    reproducible: bool                # declared: can be re-run from the recorded inputs with the same result
    model_judgment: bool = False      # declared: True if an LLM/heuristic score decides the outcome (not allowed)
    description: str = ""


class MethodRegistry:
    def __init__(self) -> None:
        self._methods: dict[tuple[str, str], VerificationMethod] = {}

    def register(self, m: VerificationMethod) -> None:
        problems = []
        if not _METHOD_ID_RE.match(m.method_id or ""):
            problems.append("method_id must be 3-64 chars of a-z 0-9 _ . -")
        if not _VERSION_RE.match(m.version or ""):
            problems.append("version required")
        if len((m.title or "").strip()) < 5:
            problems.append("title required")
        if len((m.documentation_ref or "").strip()) < 8:
            problems.append("documentation_ref required: the procedure must be written down")
        if len((m.description or "").strip()) < 20:
            problems.append("description required (what is checked, against what)")
        if not m.scopes or not set(m.scopes) <= METHOD_SCOPES:
            problems.append(f"scopes must be a non-empty subset of {sorted(METHOD_SCOPES)}")
        if m.reproducible is not True:
            problems.append("method must be reproducible")
        if m.model_judgment is not False:
            problems.append("a method whose outcome rests on model judgment or a heuristic score is not a verification process")
        if (m.method_id, m.version) in self._methods:
            problems.append("method id/version already registered")
        if problems:
            raise ValueError("; ".join(problems))
        self._methods[(m.method_id, m.version)] = m

    def get(self, method_id, version) -> Optional[VerificationMethod]:
        try:
            return self._methods.get((method_id, version))
        except TypeError:             # unhashable garbage from a malformed record
            return None

    def digest(self) -> str:
        rows = sorted((m.method_id, m.version, sorted(m.scopes), m.reproducible, m.model_judgment, m.documentation_ref)
                      for m in self._methods.values())
        return hashlib.sha256(canonical_bytes(rows)).hexdigest()

    def __len__(self) -> int:
        return len(self._methods)


class RevocationList:
    """Revocation interface: any object with is_revoked(id) and identity() works. This one is in-memory."""

    def __init__(self, name: str = "revocations") -> None:
        self._name = name
        self._ids: set[str] = set()

    def revoke(self, item_id: str) -> None:
        self._ids.add(item_id)

    def is_revoked(self, item_id: str) -> bool:
        return item_id in self._ids

    def identity(self) -> str:
        return self._name


# ------------------------------------------------------------------------- verification
@dataclass
class VerifyResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    kind: str = ""          # "human" | "process" when ok


class TrustPolicy:
    """Verification-only policy. Holds NO key. With no verifier, nothing verifies (fail closed).

    `verifier_id` is a label chosen by the orchestrator for the verifier/key-set generation; it is part of
    the policy fingerprint, so a brief assessed under one verifier will not silently export under another."""

    def __init__(self, verifier: Optional[Verifier] = None, *, verifier_id: str = "",
                 methods: Optional[MethodRegistry] = None, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                 revocation=None, trusted_gatherers: Iterable[str] = (), require_retrieval: bool = True,
                 context_id: str = "") -> None:
        if verifier is not None and not callable(verifier):
            raise ValueError("verifier must be callable")
        if not isinstance(max_age_days, int) or isinstance(max_age_days, bool) or max_age_days < 1:
            raise ValueError("max_age_days must be a positive integer")
        self._verifier = verifier
        self.verifier_id = verifier_id
        self.methods = methods if methods is not None else MethodRegistry()
        self.max_age_days = max_age_days
        self.revocation = revocation
        self.trusted_gatherers = frozenset(trusted_gatherers)
        self.require_retrieval = bool(require_retrieval)
        self.context_id = context_id

    def __repr__(self) -> str:
        return (f"TrustPolicy(verifier={'set' if self._verifier else 'none'}, id={self.verifier_id!r}, "
                f"methods={len(self.methods)}, context={self.context_id!r})")

    @property
    def can_verify(self) -> bool:
        return self._verifier is not None

    def bound_to(self, context_id: str) -> "TrustPolicy":
        """A copy bound to one brief/project id. Attestations for any other context fail."""
        return TrustPolicy(self._verifier, verifier_id=self.verifier_id, methods=self.methods,
                           max_age_days=self.max_age_days, revocation=self.revocation,
                           trusted_gatherers=self.trusted_gatherers, require_retrieval=self.require_retrieval,
                           context_id=context_id)

    def fingerprint(self) -> str:
        """Identity of the policy (verifier id, method registry, expiry, retrieval and gatherer rules,
        revocation list identity). Context id is excluded: it is per brief."""
        return hashlib.sha256(canonical_bytes({
            "verifier_id": self.verifier_id, "methods": self.methods.digest(), "max_age_days": self.max_age_days,
            "require_retrieval": self.require_retrieval, "trusted_gatherers": sorted(self.trusted_gatherers),
            "revocation": self.revocation.identity() if self.revocation is not None else "",
            "can_verify": self.can_verify})).hexdigest()[:32]

    # ---- generic review attestations
    def verify(self, att, subject: dict, today: dt.date) -> VerifyResult:
        try:
            return self._verify(att, subject, today)
        except Exception as e:  # noqa: BLE001 - malformed data must fail closed, never crash or accept
            return VerifyResult(False, [f"verification error ({type(e).__name__}); failing closed"])

    def _verify(self, att, subject: dict, today: dt.date) -> VerifyResult:
        if att is None:
            return VerifyResult(False, ["no attestation"])
        if not isinstance(att, Attestation):
            return VerifyResult(False, ["not an attestation"])
        if self._verifier is None:
            return VerifyResult(False, ["no verifier configured: attestations cannot be verified"])
        bad: list[str] = []
        if not self.context_id:
            bad.append("trust policy is not bound to a brief")
        elif att.context_id != self.context_id:
            bad.append("attestation is bound to a different brief/context")
        if not isinstance(att.key_id, str) or not att.key_id:
            bad.append("attestation has no key id")
        if not isinstance(att.attestation_id, str) or not att.attestation_id:
            bad.append("attestation has no id")
        elif self.revocation is not None and self.revocation.is_revoked(att.attestation_id):
            bad.append("attestation has been revoked")
        if not isinstance(att.issued_at, dt.date):
            bad.append("issued_at missing")
        else:
            if att.issued_at > today:
                bad.append("issued_at is in the future")
            elif (today - att.issued_at).days > self.max_age_days:
                bad.append(f"attestation is older than the {self.max_age_days}-day limit")
        if att.expires_at is not None and (not isinstance(att.expires_at, dt.date) or att.expires_at < today):
            bad.append("attestation has expired")
        if att.scope != subject.get("scope") or att.scope not in ALL_SCOPES:
            bad.append(f"attestation scope '{att.scope}' does not match the subject")
        if att.kind not in ("human", "process"):
            bad.append("attestation kind must be human or process")
        if att.subject_digest != subject_digest(subject):
            bad.append("subject changed since it was attested (digest mismatch)")
        if att.kind == "human":
            if not isinstance(att.attestor, str) or not _HUMAN_RE.match(att.attestor):
                bad.append("human attestor must look like 'human:<name>' (identity is NOT authenticated here)")
            if att.process is not None:
                bad.append("a human attestation must not carry a process record")
        elif att.kind == "process":
            bad += self._process_problems(att, subject, today)
        if not bad:
            if not isinstance(att.signature, str) or not att.signature:
                bad.append("signature missing")
            elif self._verifier(PURPOSE_REVIEW, signing_message(att), att.signature, att.key_id) is not True:
                bad.append("signature invalid")
        return VerifyResult(not bad, bad, att.kind if not bad else "")

    def _process_problems(self, att: Attestation, subject: dict, today: dt.date) -> list[str]:
        p = att.process
        if not isinstance(p, ProcessRecord):
            return ["process attestation needs a verification record"]
        scope = subject.get("scope")
        if scope not in _PROCESS_NEEDS:
            return [f"a process attestation is not permitted for scope '{scope}'"]
        out: list[str] = []
        for name in ("method_id", "method_version", "verified_at", "evidence_checked", "result", "record_reference"):
            if not isinstance(getattr(p, name), str):
                out.append(f"process record field '{name}' must be text")
        if out:
            return out
        if att.attestor != f"process:{p.method_id}":
            out.append("attestor must be 'process:<method_id>' matching the record")
        m = self.methods.get(p.method_id, p.method_version)
        if m is None:
            out.append(f"method '{p.method_id}' version '{p.method_version}' is not a registered, documented method")
        else:
            if _PROCESS_NEEDS[scope] not in m.scopes:
                out.append(f"method is not competent for '{_PROCESS_NEEDS[scope]}'")
            if scope == SCOPE_SOURCE and subject.get("peer_reviewed") is True and "peer_review" not in m.scopes:
                out.append("method is not competent to establish peer review")
        if p.result != "pass":
            out.append("verification result is not 'pass'")
        try:
            vd = dt.date.fromisoformat(p.verified_at)
            if vd > today or (isinstance(att.issued_at, dt.date) and vd > att.issued_at):
                out.append("verified_at is in the future or after issuance")
        except ValueError:
            out.append("verified_at is not a valid ISO date")
        for name in ("evidence_checked", "record_reference"):
            val = getattr(p, name)
            if len(val.strip()) < 3:
                out.append(f"{name} required")
            elif unsafe_text_problems(val, NOTE_MAX):
                out.append(f"{name} contains unsafe text")
        return out

    def verify_source(self, s: Source, today: dt.date) -> VerifyResult:
        return self.verify(s.attestation, source_subject(s), today)

    def verify_citation(self, claim: Claim, c: Citation, s: Source, today: dt.date) -> VerifyResult:
        return self.verify(c.attestation, citation_subject(claim, c, s), today)

    def verify_evergreen(self, claim: Claim, today: dt.date) -> VerifyResult:
        return self.verify(claim.evergreen_attestation, evergreen_subject(claim), today)

    def verify_kind(self, claim: Claim, today: dt.date) -> VerifyResult:
        return self.verify(claim.kind_attestation, kind_subject(claim), today)

    def verify_risk_downgrade(self, claim: Claim, today: dt.date) -> VerifyResult:
        return self.verify(claim.risk_downgrade_attestation, risk_subject(claim), today)

    def verify_independence(self, ia, sources_by_id: dict, today: dt.date) -> VerifyResult:
        try:
            srcs = [sources_by_id[i] for i in ia.source_ids]
        except (KeyError, TypeError):
            return VerifyResult(False, ["independence attestation names a source that is not in the brief"])
        if len(set(ia.source_ids)) != len(ia.source_ids) or len(srcs) < 2:
            return VerifyResult(False, ["independence attestation needs two or more distinct sources"])
        return self.verify(ia.attestation, independence_subject(srcs), today)

    # ---- ingestion boundary: retrieval records and excerpt proofs
    def verify_retrieval(self, r, s: Source, today: dt.date) -> VerifyResult:
        try:
            return self._verify_retrieval(r, s, today)
        except Exception as e:  # noqa: BLE001
            return VerifyResult(False, [f"verification error ({type(e).__name__}); failing closed"])

    def _verify_retrieval(self, r, s: Source, today: dt.date) -> VerifyResult:
        if r is None:
            return VerifyResult(False, ["no retrieval record"])
        if not isinstance(r, RetrievalRecord):
            return VerifyResult(False, ["not a retrieval record"])
        if self._verifier is None:
            return VerifyResult(False, ["no verifier configured: retrieval records cannot be verified"])
        bad: list[str] = []
        for name in ("record_id", "requested_url", "final_url", "content_sha256", "content_ref", "gatherer",
                     "key_id", "signature"):
            if not isinstance(getattr(r, name), str) or not getattr(r, name):
                bad.append(f"retrieval record field '{name}' missing")
        if bad:
            return VerifyResult(False, bad)
        if not _HEX64.match(r.content_sha256):
            bad.append("content hash is not a sha256 hex digest")
        if self.trusted_gatherers and r.gatherer not in self.trusted_gatherers:
            bad.append(f"gatherer '{r.gatherer}' is not a trusted ingestion component")
        if self.revocation is not None and self.revocation.is_revoked(r.record_id):
            bad.append("retrieval record has been revoked")
        if not isinstance(r.retrieved_at, dt.date) or r.retrieved_at > today:
            bad.append("retrieved_at missing or in the future")
        if r.retrieved_at != s.retrieved_at:
            bad.append("retrieval date differs from the source's retrieved_at")
        if r.requested_url != (s.requested_url or s.url):
            bad.append("requested URL differs from the source's URL")
        if r.final_url != (s.final_url or s.url):
            bad.append("final URL differs from the source's final URL")
        if not bad and self._verifier(PURPOSE_INGESTION, retrieval_message(r), r.signature, r.key_id) is not True:
            bad.append("retrieval signature invalid")
        return VerifyResult(not bad, bad)

    def verify_excerpt(self, c: Citation, s: Source, today: dt.date) -> VerifyResult:
        try:
            return self._verify_excerpt(c, s, today)
        except Exception as e:  # noqa: BLE001
            return VerifyResult(False, [f"verification error ({type(e).__name__}); failing closed"])

    def _verify_excerpt(self, c: Citation, s: Source, today: dt.date) -> VerifyResult:
        p = c.excerpt_proof
        if p is None:
            return VerifyResult(False, ["no excerpt proof: the locator is not tied to retrieved content"])
        if not isinstance(p, ExcerptProof):
            return VerifyResult(False, ["not an excerpt proof"])
        rv = self.verify_retrieval(s.retrieval, s, today)
        if not rv.ok:
            return VerifyResult(False, ["source retrieval does not verify: " + "; ".join(rv.reasons)])
        bad: list[str] = []
        for name in ("record_id", "locator", "excerpt", "excerpt_sha256", "key_id", "signature"):
            if not isinstance(getattr(p, name), str) or not getattr(p, name):
                bad.append(f"excerpt proof field '{name}' missing")
        if bad:
            return VerifyResult(False, bad)
        if p.record_id != s.retrieval.record_id:
            bad.append("excerpt proof belongs to a different retrieval record")
        if p.locator != c.locator:
            bad.append("citation locator differs from the proven locator")
        if len(p.excerpt) > MAX_EXCERPT_CHARS:
            bad.append("excerpt too long")
        if sha256_text(p.excerpt) != p.excerpt_sha256:
            bad.append("excerpt text does not match its hash")
        if not bad and self._verifier(PURPOSE_INGESTION, excerpt_message(s.retrieval, p), p.signature, p.key_id) is not True:
            bad.append("excerpt proof signature invalid")
        return VerifyResult(not bad, bad)


DEFAULT_TRUST = TrustPolicy()      # no verifier: fails closed
