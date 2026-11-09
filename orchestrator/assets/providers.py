"""Provider interfaces for FUTURE asset search / generation. No concrete provider, no network,
no downloader exists in this package.

Everything a provider returns is untrusted: it is validated, sanitised, and converted only into
a PROVISIONAL record that is UNVERIFIED and carries "claim"-strength evidence, so it can never
reach `permitted` / `approved` on the provider's say-so. A candidate has no local file until a
separate, explicit acquisition step (not implemented) exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .evaluation import AssetRequest
from .models import (AssetRecord, Category, Evidence, ManifestError, MediaType, RightsBasis, Watermark,
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


def sanitize_candidate(c: AssetCandidate) -> AssetCandidate:
    """Raise ManifestError if the candidate is unusable; otherwise return a cleaned copy."""
    p: list[str] = []
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
        generated=bool(c.generated), notes=clean_text(c.notes, "notes", 1000, p))
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
    elif c.claimed_license_name:
        t = parse_license(c.claimed_license_name)
        basis = RightsBasis.CREATIVE_COMMONS if t is not None and t.attribution else \
            RightsBasis.PUBLIC_DOMAIN if t is not None else RightsBasis.LICENSED_OTHER
    wm = Watermark.WATERMARKED if c.claims_watermarked else Watermark.UNCHECKED  # a claim of "clear" is not accepted
    desc = f"claim by provider {c.provider!r} (id {c.external_id}); not independently verified"
    return AssetRecord(
        asset_id=asset_id, title=c.title, category=c.category, media_type=c.media_type, tags=c.tags,
        source=c.provider, source_url=c.source_url, creator=c.creator, rights_basis=basis,
        license_name=c.claimed_license_name, license_url=c.claimed_license_url,
        evidence=[Evidence("provider_claim", desc, c.source_url, captured_at=parse_date(today, "today"), strength="claim")],
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
