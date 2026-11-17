"""Provider interfaces for asset search / generation. Nothing here touches the network.

Everything a provider returns is untrusted: it is validated, sanitised, and converted only into
a PROVISIONAL record that is UNVERIFIED and carries "claim"-strength evidence, so it can never
reach `permitted` / `approved` on the provider's say-so. A candidate has no local file until the
separate, explicit acquisition step (acquisition.py) runs, and even then it stays quarantined
until a named human attests the rights facts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .evaluation import AssetRequest
from .models import (SHA256_RE, AssetRecord, Category, Evidence, ManifestError, MediaType, RightsBasis, Watermark,
                     clean_text, norm_tag, parse_date)
from .rights import RightsDecision, UseContext, evaluate_rights, parse_license


class ProviderError(Exception):
    pass


@dataclass
class AssetCandidate:
    """A provider's CLAIMS about an asset it found or generated. Not facts."""
    provider: str
    external_id: str
    title: str
    category: Category
    media_type: MediaType
    source_url: str = ""
    creator: str = ""
    tags: list[str] = field(default_factory=list)
    claimed_license_name: str = ""
    claimed_license_url: str = ""
    claimed_commercial_use: Optional[bool] = None
    claimed_modification: Optional[bool] = None
    claimed_attribution_text: str = ""
    claims_watermarked: Optional[bool] = None
    claims_preview: bool = False
    generated: bool = False
    notes: str = ""
    # --- acquisition inputs (all optional; added by T-AST-003, defaults keep older callers working) ---
    download_url: str = ""          # https URL of the file; only the acquisition step ever fetches it
    license_text_url: str = ""      # https URL of the licence text to snapshot as evidence
    expected_sha256: str = ""       # a provider-declared checksum; a mismatch aborts acquisition
    local_path: str = ""            # a user-supplied local file (mutually exclusive with download_url)
    claimed_basis: str = ""         # only "user_owned" is honoured here; everything else derives from the licence name
    owner_declaration: str = ""     # the user's own statement of ownership (sidecar); evidence of strength "declaration"


CLAIMED_BASES = ("", "user_owned", "public_domain", "creative_commons", "licensed_other")


@runtime_checkable
class AssetSearchProvider(Protocol):
    name: str

    def search(self, request: AssetRequest, max_results: int = 10) -> list[AssetCandidate]:
        """Return candidates. MUST NOT download anything or assert verified rights."""
        ...


@runtime_checkable
class AssetGenerationProvider(Protocol):
    name: str

    def generate(self, request: AssetRequest) -> AssetCandidate:
        """Describe a generation (terms are the provider's claim). Producing the file is a
        separate, explicit step that is not defined yet."""
        ...


def _safe_url(url: str) -> str:
    """http(s) only, no embedded credentials, no query string (may carry tokens)."""
    if not url:
        return ""
    try:
        p = urlsplit(url)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return ""
    return f"{p.scheme}://{p.hostname}{p.path}"[:500]


def safe_fetch_url(url: object) -> str:
    """A URL that acquisition may fetch: https only, a host, no embedded credentials, no control
    characters, at most 1000 characters. Unlike _safe_url the query string is kept (a download link may
    need it) so it must never be logged or stored: only _safe_url output is persisted. "" if unusable."""
    if not isinstance(url, str) or not url or len(url) > 1000 or any(ord(ch) < 33 for ch in url):
        return ""
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return ""
    if p.scheme != "https" or not p.hostname or p.username or p.password or port not in (None, 443) or p.fragment:
        return ""
    return url


def sanitize_candidate(c: AssetCandidate) -> AssetCandidate:
    """Raise ManifestError if the candidate is unusable; otherwise return a cleaned copy."""
    p: list[str] = []
    for key in ("download_url", "license_text_url"):
        raw = getattr(c, key, "")
        if raw and not safe_fetch_url(raw):
            p.append(f"{key}: must be a plain https URL")
    exp = getattr(c, "expected_sha256", "")
    if exp and not (isinstance(exp, str) and SHA256_RE.fullmatch(exp)):
        p.append("expected_sha256: must be 64 lowercase hex characters")
    basis = getattr(c, "claimed_basis", "")
    if basis not in CLAIMED_BASES:
        p.append("claimed_basis: not an accepted value")
    if getattr(c, "download_url", "") and getattr(c, "local_path", ""):
        p.append("download_url and local_path are mutually exclusive")
    if not isinstance(c.category, Category) or not isinstance(c.media_type, MediaType):
        raise ManifestError("candidate: category/media_type invalid")
    for k in ("claimed_commercial_use", "claimed_modification", "claims_watermarked"):
        v = getattr(c, k)
        if v is not None and not isinstance(v, bool):
            p.append(f"{k}: must be bool or None")
    tags = []
    for t in (c.tags if isinstance(c.tags, list) else []):
        t = norm_tag(clean_text(t, "tag", 64, p))
        if t and t not in tags:
            tags.append(t)
    out = AssetCandidate(
        provider=clean_text(c.provider, "provider", 100, p), external_id=clean_text(c.external_id, "external_id", 200, p),
        title=clean_text(c.title, "title", 300, p), category=c.category, media_type=c.media_type,
        source_url=_safe_url(c.source_url if isinstance(c.source_url, str) else ""),
        creator=clean_text(c.creator, "creator", 300, p), tags=tags[:50],
        claimed_license_name=clean_text(c.claimed_license_name, "license", 200, p),
        claimed_license_url=_safe_url(c.claimed_license_url if isinstance(c.claimed_license_url, str) else ""),
        claimed_commercial_use=c.claimed_commercial_use, claimed_modification=c.claimed_modification,
        claimed_attribution_text=clean_text(c.claimed_attribution_text, "attribution", 1000, p),
        claims_watermarked=c.claims_watermarked, claims_preview=bool(c.claims_preview),
        generated=bool(c.generated), notes=clean_text(c.notes, "notes", 1000, p),
        download_url=getattr(c, "download_url", "") if not p else "",
        license_text_url=getattr(c, "license_text_url", "") if not p else "",
        expected_sha256=exp if isinstance(exp, str) else "", local_path=clean_text(getattr(c, "local_path", ""), "local_path", 1000, p),
        claimed_basis=basis if basis in CLAIMED_BASES else "",
        owner_declaration=clean_text(getattr(c, "owner_declaration", ""), "owner_declaration", 1000, p))
    if not out.provider or not out.external_id or not out.title:
        p.append("provider, external_id and title are required")
    if p:
        raise ManifestError(p)
    return out


def candidate_to_record(c: AssetCandidate, asset_id: str, today: str) -> AssetRecord:
    """Provisional, UNVERIFIED record. No file, no checksum, no attestation: it cannot be
    permitted, and the library will not index it without a real local file."""
    c = sanitize_candidate(c)
    basis = RightsBasis.UNKNOWN
    if c.generated:
        basis = RightsBasis.GENERATED
    elif c.claimed_basis == "user_owned" and c.owner_declaration:
        basis = RightsBasis.USER_OWNED   # a claim; rights still need a recorded user_declaration attestation
    elif c.claimed_license_name:
        t = parse_license(c.claimed_license_name)
        basis = RightsBasis.CREATIVE_COMMONS if t is not None and t.attribution else \
            RightsBasis.PUBLIC_DOMAIN if t is not None else RightsBasis.LICENSED_OTHER
    wm = Watermark.WATERMARKED if c.claims_watermarked else Watermark.UNCHECKED  # a claim of "clear" is not accepted
    desc = f"claim by provider {c.provider!r} (id {c.external_id}); not independently verified"
    evidence = [Evidence("provider_claim", desc, c.source_url, captured_at=parse_date(today, "today"), strength="claim")]
    if basis is RightsBasis.USER_OWNED:
        evidence.append(Evidence("owner_declaration", c.owner_declaration, captured_at=parse_date(today, "today"),
                                 strength="declaration"))
    return AssetRecord(
        asset_id=asset_id, title=c.title, category=c.category, media_type=c.media_type, tags=c.tags,
        source=c.provider, source_url=c.source_url, creator=c.creator, rights_basis=basis,
        license_name=c.claimed_license_name, license_url=c.claimed_license_url,
        evidence=evidence,
        attribution_text=c.claimed_attribution_text, commercial_use_allowed=c.claimed_commercial_use,
        modification_allowed=c.claimed_modification, acquisition_date="", watermark_status=wm,
        is_preview=c.claims_preview, notes=c.notes)


def triage(c: AssetCandidate, ctx: Optional[UseContext] = None, today: str = "1970-01-01") -> RightsDecision:
    """Metadata-only pre-screen. Can reject early; can NEVER return `permitted`."""
    d = evaluate_rights(candidate_to_record(c, "triage", today), ctx)
    assert not d.permitted, "an unverified candidate must never be permitted"
    return d


class ProviderRegistry:
    """Registers providers; isolates their failures; sanitises their output."""

    def __init__(self) -> None:
        self._search: dict[str, AssetSearchProvider] = {}
        self._gen: dict[str, AssetGenerationProvider] = {}

    def register_search(self, p: AssetSearchProvider) -> None:
        if not isinstance(p, AssetSearchProvider) or not p.name or p.name in self._search:
            raise ProviderError("invalid or duplicate search provider")
        self._search[p.name] = p

    def register_generator(self, p: AssetGenerationProvider) -> None:
        if not isinstance(p, AssetGenerationProvider) or not p.name or p.name in self._gen:
            raise ProviderError("invalid or duplicate generation provider")
        self._gen[p.name] = p

    def search_all(self, request: AssetRequest, max_results: int = 10) -> tuple[list[AssetCandidate], list[str]]:
        """Returns (clean candidates, problems). A failing provider or malformed record is
        reported and skipped; nothing is silently invented."""
        cands, problems = [], []
        for name, prov in self._search.items():
            try:
                raw = prov.search(request, max_results)
            except Exception as e:   # provider code is untrusted
                problems.append(f"{name}: search failed ({type(e).__name__})")
                continue
            for i, c in enumerate(list(raw)[:max_results] if isinstance(raw, list) else []):
                try:
                    cands.append(sanitize_candidate(c))
                except (ManifestError, AttributeError, TypeError) as e:
                    problems.append(f"{name}[{i}]: rejected malformed candidate")
        return cands, problems
