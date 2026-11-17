"""Openverse catalogue adapter: a PURE request builder plus response parser, run over the injected Transport.

NOT TESTED against the live service. Request parameters and the response shape below follow the public Openverse
API documentation (https://api.openverse.org/v1/, endpoints /images/ and /audio/) as the author understood it;
the fixtures in the tests are written from that documentation, not recorded from the service. Run it once against the
real API before relying on it. Openverse has no video endpoint, so VIDEO requests produce no search.

Everything parsed here is a CLAIM by Openverse or the original uploader. The licence NAME is derived from the
documented `license`/`license_version` fields only; anything unrecognised is left blank (unknown rights), never guessed.
`download_url` points at the ORIGINAL host (not Openverse), so the caller must allow-list that host explicitly in the
AcquisitionPolicy; with no allow-list entry nothing is downloaded.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlencode

from .acquisition import AcquisitionPolicy, FetchError, Transport, TransportRequest, fetch_to, scrub
from .evaluation import AssetRequest
from .models import Category, MediaType, clean_text
from .providers import AssetCandidate, ProviderError, safe_fetch_url

NAME = "openverse"
API_HOST = "api.openverse.org"
ENDPOINTS = {MediaType.IMAGE: "/v1/images/", MediaType.AUDIO: "/v1/audio/"}
MAX_RESPONSE_BYTES = 2_000_000
_SIMPLE = re.compile(r"^by(-nc)?(-nd|-sa)?$|^by-nc-sa$|^by-nc-nd$")
_VERSION = re.compile(r"^[0-9]\.[0-9]$")
_CATEGORY = {MediaType.IMAGE: Category.IMAGE, MediaType.AUDIO: Category.SFX}


def build_search_url(request: AssetRequest, media_type: MediaType, max_results: int) -> str:
    """The search URL for one media type. No credentials are ever added."""
    if media_type not in ENDPOINTS:
        raise ProviderError(f"openverse has no {media_type.value} endpoint")
    terms = " ".join([request.description, *request.keywords]).split()
    q = " ".join(terms)[:200]
    params = {"q": q, "page_size": max(1, min(int(max_results), 20)), "mature": "false",
              "license_type": "commercial,modification"}
    return f"https://{API_HOST}{ENDPOINTS[media_type]}?{urlencode(params)}"


def license_name(license_code: Any, version: Any) -> str:
    """The licence name the documented fields spell out, or "" when they do not spell one out."""
    if not isinstance(license_code, str):
        return ""
    code = license_code.strip().lower()
    ver = version.strip() if isinstance(version, str) and _VERSION.match(version.strip()) else ""
    if code == "cc0":
        return "CC0-1.0"
    if code == "pdm":
        return "PDM-1.0"
    if _SIMPLE.match(code) and ver:
        return f"CC-{code.upper()}-{ver}"
    return ""


def canonical_license_url(license_code: Any, version: Any) -> str:
    """The creativecommons.org legal page derived ONLY from the documented license/version fields, or "".

    The response's own license_url is never used: the live run of 2026-11-17 showed it can carry a trailing
    ``deed.en/`` path that returns 404. Anything not derivable here yields "" (no licence text can be captured, so
    the candidate is held back, never fetched)."""
    name = license_name(license_code, version)
    if name == "CC0-1.0":
        return "https://creativecommons.org/publicdomain/zero/1.0/"
    if name == "PDM-1.0":
        return "https://creativecommons.org/publicdomain/mark/1.0/"
    if name.startswith("CC-"):
        code, _, ver = name[3:].rpartition("-")
        return f"https://creativecommons.org/licenses/{code.lower()}/{ver}/"
    return ""


def parse_search_response(body: bytes, media_type: MediaType) -> tuple[list[AssetCandidate], list[str]]:
    """(candidates, problems). Malformed, oversize or hostile payloads are reported, never raised."""
    problems: list[str] = []
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_RESPONSE_BYTES:
        return [], ["response missing or larger than the size limit"]
    try:
        data = json.loads(bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return [], ["response is not valid JSON"]
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return [], ["response has no results list"]
    out: list[AssetCandidate] = []
    for i, r in enumerate(results[:50]):
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"] or len(r["id"]) > 100:
            problems.append(f"results[{i}]: no usable id")
            continue
        if r.get("mature") is True:
            problems.append(f"results[{i}]: mature content skipped")
            continue
        url = r.get("url")
        if not safe_fetch_url(url):
            problems.append(f"results[{i}]: no plain https file URL")
            continue
        lic_url = canonical_license_url(r.get("license"), r.get("license_version"))
        tags = [t.get("name") for t in r.get("tags", []) if isinstance(t, dict) and isinstance(t.get("name"), str)] \
            if isinstance(r.get("tags"), list) else []
        landing = r.get("foreign_landing_url")
        out.append(AssetCandidate(
            provider=NAME, external_id=r["id"], title=clean_text(r.get("title") or r["id"], "title", 300, problems) or r["id"],
            category=_CATEGORY[media_type], media_type=media_type,
            source_url=landing if safe_fetch_url(landing) else "",
            creator=clean_text(r.get("creator"), "creator", 300, problems), tags=tags[:50],
            claimed_license_name=license_name(r.get("license"), r.get("license_version")),
            claimed_license_url=lic_url,
            claimed_attribution_text=clean_text(r.get("attribution"), "attribution", 1000, problems),
            download_url=url, license_text_url=lic_url))
    return out, [scrub(p) for p in problems]


class OpenverseProvider:
    """AssetSearchProvider over the injected transport. Search only; downloading is the acquisition step's job."""
    name = NAME

    def __init__(self, transport: Transport, policy: Optional[AcquisitionPolicy] = None):
        self.transport = transport
        self.policy = policy or AcquisitionPolicy()
        self.last_problems: list[str] = []

    def search(self, request: AssetRequest, max_results: int = 10) -> list[AssetCandidate]:
        import io
        wanted = request.media_types or [MediaType.IMAGE, MediaType.AUDIO]
        out: list[AssetCandidate] = []
        self.last_problems = []
        for mt in [m for m in (MediaType.IMAGE, MediaType.AUDIO) if m in wanted]:
            url = build_search_url(request, mt, max_results)
            buf = io.BytesIO()
            try:
                fetch_to(self.transport, url, hosts=[API_HOST], max_bytes=MAX_RESPONSE_BYTES, timeout_s=self.policy.timeout_s,
                         accept=("application/json",), sink=buf)
            except FetchError as e:
                raise ProviderError(f"{e.code}") from None
            cands, problems = parse_search_response(buf.getvalue(), mt)
            out.extend(cands)
            self.last_problems.extend(problems)
        return out[:max_results]
