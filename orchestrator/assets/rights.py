"""Rights decision engine. Pure function of (record, intended use). Fail-closed.

`permitted` requires ALL of: a local file with a checksum, a known rights basis, terms that are
EXPLICITLY compatible with the intended use (True, never "not False"), evidence of the right
strength, no contradiction between licence name / licence URL / evidence, no watermark/preview
signal, and a recorded attestation that is bound to the CURRENT rights facts (fingerprint).
Anything unknown -> quarantine; anything definitely incompatible or a watermarked preview -> reject.

There is NO automated licence verification and no legal opinion here. The CC table only
interprets a licence NAME/URL the record already carries; it cannot confirm that the name is true.

Ownership policy (USER_OWNED): ownership is a declaration, not a grant of unknown permissions.
commercial_use_allowed and modification_allowed must be explicitly True. Attribution to oneself is
not required, so attribution_required=None is read as False for USER_OWNED only.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from .models import AssetRecord, RightsBasis, Verification, Watermark

PERMITTED, QUARANTINE, REJECT = "permitted", "quarantine", "reject"

# Elements of the Creative Commons licence family (public, documented terms):
#   BY: attribution required   NC: no commercial use   ND: no derivatives   SA: share-alike
# Only versions CC has actually published are accepted.
_VERSIONS = r"(?:-(?:1\.0|2\.0|2\.5|3\.0|4\.0))?"
_CC_RE = re.compile(r"CC-BY(-NC)?(-ND|-SA)?" + _VERSIONS)
_CC0_RE = re.compile(r"CC0(-1\.0)?")
_PDM_RE = re.compile(r"(PDM(-1\.0)?|PUBLIC-DOMAIN-MARK(-1\.0)?)")

KNOWN_RESTRICTIONS = {
    "no_commercial": ("commercial", "reject"),
    "no_modification": ("modification", "reject"),
    "editorial_only": ("commercial", "reject"),   # editorial-only licences exclude commercial/promotional use
    "no_redistribution": (None, "info"),          # informational: publishing a video is not redistributing the file
}

WATERMARK_HINT = re.compile(r"\b(watermark(ed)?|preview|comp|sample|screener)\b", re.I)


@dataclass
class Terms:
    commercial: Optional[bool]
    modification: Optional[bool]
    attribution: Optional[bool]
    share_alike: bool = False
    known_license: bool = True
    kind: str = "other"   # cc_license | cc0 | pdm | other


def _norm_license(name: str) -> str:
    n = re.sub(r"[\s_]+", "-", (name or "").strip().upper())
    return n.replace("CREATIVE-COMMONS-", "CC-").replace("CC-ZERO", "CC0")


def parse_license(name: str) -> Optional[Terms]:
    """Interpret a licence NAME. Returns None for anything not in the CC/PD families."""
    n = _norm_license(name)
    if _CC0_RE.fullmatch(n):
        return Terms(True, True, False, kind="cc0")          # a waiver by the rightsholder
    if _PDM_RE.fullmatch(n):
        return Terms(True, True, False, kind="pdm")          # a mark asserting public-domain status
    m = _CC_RE.fullmatch(n)
    if m:
        nc, nd_sa = bool(m.group(1)), m.group(2)
        return Terms(commercial=not nc, modification=nd_sa != "-ND", attribution=True,
                     share_alike=nd_sa == "-SA", kind="cc_license")
    return None


def parse_license_url(url: str) -> Optional[Terms]:
    """Terms implied by a creativecommons.org licence URL; None if not such a URL / not parseable."""
    try:
        u = urlsplit(url or "")
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in ("creativecommons.org", "www.creativecommons.org"):
        return None
    parts = [x for x in u.path.split("/") if x]
    if len(parts) >= 2 and parts[0] == "licenses":
        return parse_license("CC-" + parts[1].upper() + (("-" + parts[2]) if len(parts) > 2 else ""))
    if len(parts) >= 2 and parts[0] == "publicdomain" and parts[1] in ("zero", "mark"):
        return Terms(True, True, False)
    return None


def _same(a: Terms, b: Terms) -> bool:
    return (a.commercial, a.modification, a.attribution, a.share_alike) == \
           (b.commercial, b.modification, b.attribution, b.share_alike)


def rights_fingerprint(rec: AssetRecord) -> str:
    """Hash of every fact an attestation vouches for. Any change makes older attestations stale."""
    v = lambda x: getattr(x, "value", x)
    payload = {
        "basis": v(rec.rights_basis), "license": rec.license_name, "license_url": rec.license_url,
        "source": rec.source, "source_url": rec.source_url, "creator": rec.creator,
        "commercial": rec.commercial_use_allowed, "modification": rec.modification_allowed,
        "attribution_required": rec.attribution_required, "attribution_text": rec.attribution_text,
        "restrictions": list(rec.restrictions), "allowed_uses": list(rec.allowed_uses),
        "watermark": v(rec.watermark_status), "preview": rec.is_preview, "checksum": rec.checksum_sha256,
        "evidence": [[e.kind, e.description, e.url, e.local_path, e.sha256, e.strength, e.license_name]
                     for e in rec.evidence],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class UseContext:
    """How the asset would be used. Defaults are the conservative case for a monetised video
    that is edited (trimmed/cropped/mixed). ND licences therefore fail by default."""
    commercial: bool = True
    modification: bool = True
    accept_share_alike: bool = False
    required_uses: tuple = ()


@dataclass
class Reason:
    code: str
    message: str
    severity: str   # "reject" | "quarantine" | "info"


@dataclass
class RightsDecision:
    verdict: str
    confidence: str   # "high" only when permitted; "low" = plausible but unverified; "none" = unknown/incompatible
    reasons: list[Reason] = field(default_factory=list)
    # Duties that come WITH use (attribution text, share-alike). Present even when permitted, so an
    # accepted obligation is never lost; callers must carry them into the finished video.
    obligations: list[str] = field(default_factory=list)
    license_kind: str = ""   # cc_license | cc0 | pdm | public_domain_statement | ""

    @property
    def permitted(self) -> bool:
        return self.verdict == PERMITTED

    def codes(self) -> list[str]:
        return [r.code for r in self.reasons]


def _effective(rec: AssetRecord, add) -> Terms:
    """Terms actually relied on.

    CC / public domain: the licence is the baseline; record flags can only RESTRICT it.
    USER_OWNED / GENERATED / LICENSED_OTHER: only EXPLICIT recorded values count; None stays
    unknown. A CC/PD licence NAME on those bases can restrict but never supply permission."""
    basis = rec.rights_basis
    by_name = parse_license(rec.license_name) if rec.license_name else None
    explicit = Terms(rec.commercial_use_allowed, rec.modification_allowed, rec.attribution_required)

    if basis is RightsBasis.CREATIVE_COMMONS:
        if by_name is None:
            add("UNRECOGNISED_LICENSE", "quarantine",
                f"licence {rec.license_name!r} is not a recognised Creative Commons licence name")
            return Terms(None, None, None, known_license=False)
        if by_name.kind == "pdm":
            add("BASIS_LICENSE_MISMATCH", "quarantine",
                "a Public Domain Mark is not a Creative Commons licence; use the public_domain basis")
        base = Terms(by_name.commercial, by_name.modification, by_name.attribution, by_name.share_alike)
    elif basis is RightsBasis.PUBLIC_DOMAIN:
        if by_name is not None and by_name.attribution:
            add("BASIS_LICENSE_MISMATCH", "quarantine", "public_domain basis but licence name is an attribution licence")
        elif by_name is None and rec.license_name and "public domain" not in rec.license_name.lower():
            add("BASIS_LICENSE_MISMATCH", "quarantine", "public_domain basis but licence name is not a public-domain tool/statement")
        base = Terms(True, True, False)   # public domain: documented evidence + attestation required (checked later)
        if by_name is not None and by_name.attribution:
            base = Terms(by_name.commercial, by_name.modification, True, by_name.share_alike)
    else:
        base = Terms(explicit.commercial, explicit.modification, explicit.attribution)
        if basis is RightsBasis.USER_OWNED and base.attribution is None:
            base.attribution = False
        if by_name is not None and basis is RightsBasis.LICENSED_OTHER:
            add("BASIS_LICENSE_MISMATCH", "quarantine", "licensed_other basis but licence name is a CC/PD tool; use that basis")

    if by_name is not None:   # a CC/PD licence name only ever restricts (all bases)
        if by_name.commercial is False:
            if rec.commercial_use_allowed is True:
                add("LICENSE_FIELD_MISMATCH", "quarantine", f"record says commercial_use_allowed=true but {rec.license_name!r} forbids it")
            base.commercial = False
        if by_name.modification is False:
            if rec.modification_allowed is True:
                add("LICENSE_FIELD_MISMATCH", "quarantine", f"record says modification_allowed=true but {rec.license_name!r} forbids it")
            base.modification = False
        if by_name.attribution:
            base.attribution = True
        base.share_alike = base.share_alike or by_name.share_alike

    if basis in (RightsBasis.CREATIVE_COMMONS, RightsBasis.PUBLIC_DOMAIN):   # record flags may only restrict
        if rec.commercial_use_allowed is False:
            base.commercial = False
        if rec.modification_allowed is False:
            base.modification = False
        if rec.attribution_required is True:
            base.attribution = True

    if rec.license_url:   # URL and name must not disagree
        by_url = parse_license_url(rec.license_url)
        if by_url is not None and (by_name is None or not _same(by_url, by_name)):
            add("LICENSE_URL_MISMATCH", "quarantine", "licence URL implies different terms than the licence name")
    return base


def evaluate_rights(rec: AssetRecord, ctx: Optional[UseContext] = None) -> RightsDecision:
    ctx = ctx or UseContext()
    reasons: list[Reason] = []

    def add(code, sev, msg):
        reasons.append(Reason(code, msg, sev))

    # 1. watermark / preview
    if rec.watermark_status is Watermark.WATERMARKED or rec.is_preview:
        add("WATERMARKED_PREVIEW", "reject", "watermarked or preview-only asset; rights cannot be obtained from a preview")
    elif rec.watermark_status is Watermark.SUSPECTED:
        add("WATERMARK_SUSPECTED", "quarantine", "watermark suspected; needs a human check")
    elif rec.watermark_status is not Watermark.CLEAR:
        add("WATERMARK_UNCHECKED", "quarantine", "watermark status not checked")
    hint_text = " ".join([rec.title, " ".join(rec.tags), rec.file_path or ""])
    if WATERMARK_HINT.search(hint_text) and not (rec.watermark_status is Watermark.WATERMARKED or rec.is_preview):
        add("WATERMARK_HINT", "quarantine", "title/tags/filename suggest a preview or watermark")

    # 2. a real local file with an integrity record
    if not rec.file_path or not rec.checksum_sha256:
        add("NO_FILE", "quarantine", "no local file / checksum recorded (not acquired)")

    # 3. provenance and terms
    if rec.rights_basis is not RightsBasis.USER_OWNED and not (rec.source or rec.source_url):
        add("SOURCE_UNKNOWN", "reject", "source cannot be established")

    if rec.rights_basis is RightsBasis.UNKNOWN:
        add("UNKNOWN_RIGHTS", "quarantine", "rights basis unknown; unknown is never permission")
        terms = Terms(None, None, None, known_license=False)
    else:
        if rec.rights_basis in (RightsBasis.CREATIVE_COMMONS, RightsBasis.LICENSED_OTHER) and not rec.license_name:
            add("MISSING_LICENSE", "quarantine", "no licence name recorded")
        terms = _effective(rec, add)
        if rec.rights_basis is RightsBasis.GENERATED:
            if not rec.source:
                add("GENERATOR_UNKNOWN", "quarantine", "generating provider not recorded")
            if not any(e.kind == "provider_terms" for e in rec.evidence):
                add("NO_PROVIDER_TERMS", "quarantine", "no captured provider terms for the generated asset")

    # 4. compatibility with the intended use: only an explicit True is permission
    if ctx.commercial:
        if terms.commercial is False:
            add("NO_COMMERCIAL", "reject", "licence/terms forbid commercial use; project may be monetised")
        elif terms.commercial is not True:
            add("COMMERCIAL_UNKNOWN", "quarantine", "commercial-use status unknown")
    if ctx.modification:
        if terms.modification is False:
            add("NO_MODIFICATION", "reject", "licence/terms forbid modification; editing would be a derivative")
        elif terms.modification is not True:
            add("MODIFICATION_UNKNOWN", "quarantine", "modification status unknown")
    if terms.share_alike and ctx.modification and not ctx.accept_share_alike:
        add("SHARE_ALIKE", "quarantine", "share-alike licence: adaptations must carry the same licence; needs explicit acceptance")
    if terms.attribution is True:
        if not rec.attribution_text:
            add("ATTRIBUTION_TEXT_MISSING", "quarantine", "attribution required but no attribution text recorded")
    elif terms.attribution is not False:
        add("ATTRIBUTION_UNKNOWN", "quarantine", "attribution requirement unknown")

    for r in rec.restrictions:
        key = re.sub(r"[\s-]+", "_", r.strip().lower())
        known = KNOWN_RESTRICTIONS.get(key)
        if known is None:
            add("UNRECOGNISED_RESTRICTION", "quarantine", f"restriction {r!r} not understood; needs human review")
        else:
            kind, sev = known
            if sev == "info":
                add("RESTRICTION_NOTED", "info", f"restriction {r!r} noted")
            elif (kind == "commercial" and ctx.commercial) or (kind == "modification" and ctx.modification):
                add("RESTRICTION_INCOMPATIBLE", "reject", f"restriction {r!r} conflicts with intended use")
    if ctx.required_uses:
        if not rec.allowed_uses:
            # Fail closed: a required use cannot be confirmed against an empty allow-list.
            add("USE_NOT_DECLARED", "quarantine",
                f"required uses {list(ctx.required_uses)} cannot be confirmed: allowed_uses is empty")
        else:
            missing = [u for u in ctx.required_uses if u not in rec.allowed_uses]
            if missing:
                add("USE_NOT_ALLOWED", "quarantine", f"intended use not in allowed_uses: {missing}")

    # 5. evidence: right kind and strength for the basis, and no contradiction
    if not rec.evidence:
        add("NO_EVIDENCE", "quarantine", "no evidence supporting the rights claim")
    elif rec.rights_basis is RightsBasis.USER_OWNED:
        if not any(e.kind == "owner_declaration" and e.strength in ("declaration", "document") for e in rec.evidence):
            add("NO_OWNER_EVIDENCE", "quarantine", "user-owned claim has no owner_declaration evidence")
    elif all(e.strength == "claim" for e in rec.evidence):
        add("CLAIM_ONLY_EVIDENCE", "quarantine", "evidence consists only of the provider/uploader's own claim")
    elif not any(e.strength == "document" for e in rec.evidence):
        add("NO_DOCUMENT_EVIDENCE", "quarantine", "no captured document (licence text, receipt, terms) among the evidence")
    for e in rec.evidence:
        if e.license_name and _norm_license(e.license_name) != _norm_license(rec.license_name):
            add("EVIDENCE_CONTRADICTS_LICENSE", "quarantine",
                f"evidence shows licence {e.license_name!r} but the record says {rec.license_name!r}")
            break

    # 6. verification: a recorded attestation bound to the CURRENT facts
    fp = rights_fingerprint(rec)
    current = [a for a in rec.attestations if a.fingerprint == fp]
    valid = [a for a in current if a.method == "human_review"
             or (a.method == "user_declaration" and rec.rights_basis is RightsBasis.USER_OWNED)]
    if rec.verification_status is Verification.DISPUTED:
        add("DISPUTED", "quarantine", "rights are disputed")
    elif rec.verification_status is not Verification.VERIFIED:
        add("UNVERIFIED", "quarantine", "rights not verified by a recorded attestation")
    elif not current:
        add("ATTESTATION_STALE", "quarantine", "rights facts changed since the last attestation; re-verify")
    elif not valid:
        add("ATTESTATION_METHOD_INVALID", "quarantine", "attestation method is not valid for this rights basis")
    if rec.rights_basis is RightsBasis.USER_OWNED and not any(a.method == "user_declaration" for a in valid):
        add("NO_OWNER_DECLARATION", "quarantine", "user-owned claim lacks a current user declaration")

    obligations = []
    if terms.share_alike:
        obligations.append("share_alike: adaptations/the finished work must be offered under the same licence terms")
    if terms.attribution is True and rec.attribution_text:
        obligations.append("attribution: " + rec.attribution_text)
    lic = parse_license(rec.license_name) if rec.license_name else None
    kind = lic.kind if lic else ("public_domain_statement" if rec.rights_basis is RightsBasis.PUBLIC_DOMAIN
                                 and rec.license_name else "")

    if any(r.severity == "reject" for r in reasons):
        verdict, conf = REJECT, "none"
    elif any(r.severity == "quarantine" for r in reasons):
        verdict = QUARANTINE
        conf = "none" if "UNKNOWN_RIGHTS" in {r.code for r in reasons} else "low"
    else:
        verdict, conf = PERMITTED, "high"
    return RightsDecision(verdict, conf, reasons, obligations, kind)
