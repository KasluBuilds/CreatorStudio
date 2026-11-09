"""Provider abstraction. The core never imports a concrete search service.

Concrete providers (web search, scholarly APIs, YouTube data, trend feeds ...) are NOT part of
this package; they implement `ResearchProvider` / `DiscoveryProvider` and are registered by the
orchestrator. `StaticProvider` is an in-memory fixture provider for tests and demos.

EVERYTHING a provider returns is untrusted. Each record is validated and sanitised on its own:
a malformed record is quarantined (reported, never stored, never echoing its URL or secrets)
and the rest of that provider's output is still processed. Provider-declared types and peer
review are recorded as declarations and never used to rank a source.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from .discovery import DiscoveryProvider, DiscoveryRequest, ProviderError, TopicCandidate
from .models import Source, SourceType, TopicKind
from .safety import (AUTHOR_MAX, ERROR_MAX, MAX_AUTHORS, NOTE_MAX, PUBLISHER_MAX, TITLE_MAX, Redactor,
                     sanitize_text)
from .sources import (SourceCatalog, SourceIdConflict, effective_type, hostname, make_source, normalize_doi,
                      parse_partial_date, stable_source_id, url_problems, validate_sources)

MAX_SNIPPET = 300
MAX_KEYWORDS = 30


@dataclass
class SearchQuery:
    text: str
    purpose: str = "verify"       # "discover" | "verify" | "reference_format"
    language: str = ""
    date_from: Optional[str] = None
    max_results: int = 10


@dataclass
class RawHit:
    """What a search provider returns. All of it is untrusted metadata."""
    url: str
    title: str
    publisher: str = ""
    authors: list[str] = field(default_factory=list)
    published: Optional[str] = None
    language: str = ""
    declared_type: Optional[SourceType] = None   # a CLAIM; never used to rank
    peer_reviewed: Optional[bool] = None          # a CLAIM; never establishes peer review
    snippet: str = ""                             # transient review aid; never persisted
    doi: str = ""
    work_id: str = ""                             # provider's identifier for the underlying work
    version_of_doi: str = ""                      # DOI of another version of the same work
    original_source_url: str = ""                 # page this one repeats/cites (lineage)
    final_url: str = ""                           # where `url` ended up after redirects

    def __post_init__(self) -> None:
        self.snippet = self.snippet[:MAX_SNIPPET] if isinstance(self.snippet, str) else ""


@runtime_checkable
class ResearchProvider(Protocol):
    name: str

    def search(self, query: SearchQuery) -> list[RawHit]: ...


def scrub(message: object, limit: int = ERROR_MAX, redactor: Optional[Redactor] = None) -> str:
    """Make provider-originated text safe to store: redact secrets, drop control characters, truncate."""
    return sanitize_text(message, limit, redactor=redactor)


@dataclass
class ResearchBudget:
    """Per-run limits. None = unlimited. (Real defaults are an OPEN question.)"""
    max_queries: Optional[int] = None
    max_sources: Optional[int] = None
    queries_used: int = 0
    sources_collected: int = 0

    def can_query(self) -> bool:
        return self.max_queries is None or self.queries_used < self.max_queries

    def room_for_sources(self) -> int:
        return 10**9 if self.max_sources is None else max(0, self.max_sources - self.sources_collected)


@dataclass
class QuarantinedRecord:
    provider: str
    index: int
    reasons: list[str]
    host: str = ""            # host only; the URL itself is never kept


@dataclass
class Gathered:
    sources: list[Source] = field(default_factory=list)
    errors: list[ProviderError] = field(default_factory=list)
    quarantined: list[QuarantinedRecord] = field(default_factory=list)
    budget_exhausted: bool = False


class RecordRejected(Exception):
    def __init__(self, reasons: list[str], host: str = "") -> None:
        super().__init__("; ".join(reasons))
        self.reasons, self.host = reasons, host


def _text(value: object, field_name: str, reasons: list[str], *, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            reasons.append(f"{field_name} missing")
        return ""
    if not isinstance(value, str):
        reasons.append(f"{field_name} must be text")
        return ""
    return value


def coerce_hit(item: object, provider: str, today: dt.date, *, id_prefix: str = "S",
               redactor: Optional[Redactor] = None) -> Source:
    """Validate one untrusted record and build a sanitised, classified Source. Raises RecordRejected."""
    reasons: list[str] = []
    if isinstance(item, Mapping):
        try:
            item = RawHit(**dict(item))
        except TypeError:
            raise RecordRejected(["record has missing or unexpected fields"]) from None
    if not isinstance(item, RawHit):
        raise RecordRejected([f"record is {type(item).__name__}, not a RawHit"])
    h = item
    url = _text(h.url, "url", reasons, required=True)
    host = hostname(url) if url else ""
    if url:
        reasons += [f"{code}" for code, _ in url_problems(url)]
    title = sanitize_text(_text(h.title, "title", reasons, required=True), TITLE_MAX, redactor=redactor)
    if h.title and isinstance(h.title, str) and not title:
        reasons.append("title empty after sanitising")
    published = h.published
    if published is not None:
        if not isinstance(published, str):
            reasons.append("published must be text")
        else:
            try:
                parse_partial_date(published)
            except ValueError:
                reasons.append("published is not a valid YYYY / YYYY-MM / YYYY-MM-DD date")
    declared = h.declared_type
    if declared is not None and not isinstance(declared, SourceType):
        try:
            declared = SourceType(declared)
        except ValueError:
            reasons.append("declared_type is not a known source type")
            declared = None
    if h.peer_reviewed is not None and not isinstance(h.peer_reviewed, bool):
        reasons.append("peer_reviewed must be true/false")
    publisher = _text(h.publisher, "publisher", reasons)
    language = _text(h.language, "language", reasons)
    authors = h.authors
    if not isinstance(authors, (list, tuple)) or not all(isinstance(a, str) for a in authors):
        reasons.append("authors must be a list of text")
        authors = []
    doi = _text(h.doi, "doi", reasons)
    if doi and not normalize_doi(doi):
        reasons.append("doi is not a valid DOI")
    work_id = _text(h.work_id, "work_id", reasons)
    version_doi = _text(h.version_of_doi, "version_of_doi", reasons)
    if version_doi and not normalize_doi(version_doi):
        reasons.append("version_of_doi is not a valid DOI")
    for label, extra in (("final_url", _text(h.final_url, "final_url", reasons)),
                         ("original_source_url", _text(h.original_source_url, "original_source_url", reasons))):
        if extra:
            reasons += [f"{label}: {code}" for code, _ in url_problems(extra)]
    if reasons:
        raise RecordRejected(reasons, host if isinstance(host, str) else "")
    work = sanitize_text(work_id, NOTE_MAX) or (f"doi:{normalize_doi(version_doi)}" if version_doi else "")
    src = make_source(
        url, title, today, id=stable_source_id(url, doi, id_prefix), declared_type=declared,
        publisher=sanitize_text(publisher, PUBLISHER_MAX, redactor=redactor),
        authors=[sanitize_text(a, AUTHOR_MAX, redactor=redactor) for a in list(authors)[:MAX_AUTHORS]],
        published=published, language=language, peer_reviewed=h.peer_reviewed, provider=provider,
        doi=doi, work_id=work, original_source_url=h.original_source_url, final_url=h.final_url)
    errors = [i.code for i in validate_sources([src], today) if i.severity == "error"]
    if errors:
        raise RecordRejected(errors, host)
    return src


def coerce_candidate(item: object, provider: str, today: dt.date,
                     redactor: Optional[Redactor] = None) -> tuple[TopicCandidate, list[str]]:
    """Validate/sanitise one untrusted topic candidate. Returns (candidate, dropped-lead notes)."""
    reasons: list[str] = []
    if not isinstance(item, TopicCandidate):
        raise RecordRejected([f"record is {type(item).__name__}, not a TopicCandidate"])
    c = item
    for name in ("id", "title", "summary", "category"):
        if not isinstance(getattr(c, name), str):
            reasons.append(f"{name} must be text")
    if isinstance(c.title, str) and not sanitize_text(c.title, TITLE_MAX):
        reasons.append("title missing")
    if not isinstance(c.topic_kind, TopicKind):
        reasons.append("topic_kind is not a TopicKind")
    if not isinstance(c.keywords, (list, tuple)) or not all(isinstance(k, str) for k in c.keywords):
        reasons.append("keywords must be a list of text")
    if c.event_date is not None:
        try:
            if not isinstance(c.event_date, str):
                raise ValueError
            parse_partial_date(c.event_date)
        except ValueError:
            reasons.append("event_date is not a valid YYYY / YYYY-MM / YYYY-MM-DD date")
    if not dataclasses.is_dataclass(c.signals):
        reasons.append("signals missing")
    if not isinstance(c.lead_sources, (list, tuple)):
        reasons.append("lead_sources must be a list")
    if reasons:
        raise RecordRejected(reasons)
    dropped: list[str] = []
    leads: list[Source] = []
    for j, s in enumerate(c.lead_sources):
        if not isinstance(s, Source):
            dropped.append(f"lead_sources[{j}] is not a Source")
            continue
        clean = dataclasses.replace(
            s, title=sanitize_text(s.title, TITLE_MAX, redactor=redactor),
            publisher=sanitize_text(s.publisher, PUBLISHER_MAX, redactor=redactor),
            authors=[sanitize_text(a, AUTHOR_MAX, redactor=redactor) for a in list(s.authors)[:MAX_AUTHORS]],
            notes=sanitize_text(s.notes, NOTE_MAX, redactor=redactor),
            language=sanitize_text(s.language, 16),
            confirmed_by="", confirmation_note="", text_origin="provider", attestation=None,
            retrieval=None,   # a provider cannot supply a retrieval receipt: only the ingestion boundary can
            declared_peer_reviewed=s.declared_peer_reviewed if s.declared_peer_reviewed is not None else s.peer_reviewed,
            peer_reviewed=None, declared_type=s.declared_type or s.source_type, provider=s.provider or provider)
        eff, _ = effective_type(clean)           # a stored type the URL does not support is downgraded, not trusted
        if eff is not clean.source_type:
            clean = dataclasses.replace(clean, source_type=eff, classification_warnings=[
                *clean.classification_warnings,
                f"provider-supplied type {clean.source_type.value} not supported by the URL; using {eff.value}"])
        bad = [i.code for i in validate_sources([clean], today) if i.severity == "error"]
        if bad:
            dropped.append(f"lead_sources[{j}] dropped: {', '.join(bad)}")
            continue
        leads.append(clean)
    out = dataclasses.replace(
        c, id=sanitize_text(c.id, 64), title=sanitize_text(c.title, TITLE_MAX, redactor=redactor),
        summary=sanitize_text(c.summary, 1000, multiline=True, redactor=redactor),
        category=sanitize_text(c.category, 60),
        keywords=[k for k in (sanitize_text(k, 60, redactor=redactor) for k in list(c.keywords)[:MAX_KEYWORDS]) if k],
        lead_sources=leads, provider=c.provider if isinstance(c.provider, str) and c.provider else provider)
    return out, dropped


def _safe_name(p: object) -> str:
    return sanitize_text(getattr(p, "name", None) or type(p).__name__, 60)


def _iter_safely(raw: object):
    """Yield ('item', x) or ('error', exception) without letting iteration failures escape."""
    it = iter(raw)  # type: ignore[call-overload]
    while True:
        try:
            yield "item", next(it)
        except StopIteration:
            return
        except Exception as e:  # noqa: BLE001 - a hostile/broken iterator must not crash the run
            yield "error", e
            return


class ProviderRegistry:
    def __init__(self, redactor: Optional[Redactor] = None) -> None:
        """`redactor`: optional EXTRA redaction (injected by the orchestrator); the built-in one always runs."""
        self._search: list[ResearchProvider] = []
        self._discovery: list[DiscoveryProvider] = []
        self._redactor = redactor

    @staticmethod
    def _check(p: object, method: str, existing: list) -> None:
        name = getattr(p, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("provider needs a non-empty text `name`")
        if not callable(getattr(p, method, None)):
            raise ValueError(f"provider '{name}' has no callable {method}()")
        if any(getattr(e, "name", None) == name for e in existing):
            raise ValueError(f"a provider named '{name}' is already registered")

    def register_search(self, p: ResearchProvider) -> None:
        self._check(p, "search", self._search)
        self._search.append(p)

    def register_discovery(self, p: DiscoveryProvider) -> None:
        self._check(p, "discover", self._discovery)
        self._discovery.append(p)

    @property
    def search_providers(self) -> list[ResearchProvider]:
        return list(self._search)

    @property
    def discovery_providers(self) -> list[DiscoveryProvider]:
        return list(self._discovery)

    def _err(self, name: str, kind: str, message: object) -> ProviderError:
        return ProviderError(name, kind, scrub(message, redactor=self._redactor))

    def discover(self, request: DiscoveryRequest, today: Optional[dt.date] = None
                 ) -> tuple[list[TopicCandidate], list[ProviderError]]:
        today = today or request.as_of or dt.date.today()
        found: list[TopicCandidate] = []
        errors: list[ProviderError] = []
        for p in self._discovery:
            name = _safe_name(p)
            try:
                raw = p.discover(request)
            except Exception as e:  # a broken provider must not sink the whole report
                errors.append(self._err(name, type(e).__name__, e))
                continue
            if not isinstance(raw, (list, tuple)):
                errors.append(self._err(name, "InvalidResponse", f"expected a list, got {type(raw).__name__}"))
                continue
            for index, (kind, item) in enumerate(_iter_safely(raw)):
                if kind == "error":
                    errors.append(self._err(name, "IterationError", f"{type(item).__name__} while reading results"))
                    break
                try:
                    cand, dropped = coerce_candidate(item, name, today, self._redactor)
                except RecordRejected as r:
                    errors.append(self._err(name, "QuarantinedRecord", f"record {index}: {'; '.join(r.reasons)}"))
                    continue
                except Exception as e:  # noqa: BLE001
                    errors.append(self._err(name, "QuarantinedRecord", f"record {index}: {type(e).__name__}"))
                    continue
                for d in dropped:
                    errors.append(self._err(name, "QuarantinedRecord", f"record {index}: {d}"))
                found.append(cand)
        return found, errors

    def gather(self, query: SearchQuery, today: dt.date, budget: Optional[ResearchBudget] = None,
               id_prefix: str = "S", catalog: Optional[SourceCatalog] = None) -> Gathered:
        """Run the query on every search provider. Pass the same `catalog` across calls to dedupe
        across calls; ids are content-derived so they never collide or re-point a citation."""
        if not isinstance(query, SearchQuery) or not isinstance(query.text, str) or not query.text.strip():
            raise ValueError("query needs non-empty text")
        out = Gathered()
        budget = budget or ResearchBudget()
        catalog = catalog if catalog is not None else SourceCatalog()
        in_call: set[str] = set()
        for p in self._search:
            name = _safe_name(p)
            if not budget.can_query():
                out.budget_exhausted = True
                break
            budget.queries_used += 1
            try:
                raw = p.search(query)
            except Exception as e:
                out.errors.append(self._err(name, type(e).__name__, e))
                continue
            if not isinstance(raw, (list, tuple)):
                out.errors.append(self._err(name, "InvalidResponse", f"expected a list, got {type(raw).__name__}"))
                continue
            taken = 0
            for index, (kind, item) in enumerate(_iter_safely(raw)):
                if kind == "error":
                    out.errors.append(self._err(name, "IterationError", f"{type(item).__name__} while reading results"))
                    break
                if taken >= query.max_results:
                    break
                taken += 1
                if budget.room_for_sources() <= 0:
                    out.budget_exhausted = True
                    break
                try:
                    src = coerce_hit(item, name, today, id_prefix=id_prefix, redactor=self._redactor)
                    canonical, is_new = catalog.add(src)
                except RecordRejected as r:
                    out.quarantined.append(QuarantinedRecord(name, index, [scrub(x) for x in r.reasons], r.host))
                    out.errors.append(self._err(name, "QuarantinedRecord", f"record {index}: {'; '.join(r.reasons)}"))
                    continue
                except SourceIdConflict:
                    out.quarantined.append(QuarantinedRecord(name, index, ["id collision with a different source"]))
                    continue
                except Exception as e:  # noqa: BLE001
                    out.quarantined.append(QuarantinedRecord(name, index, [f"unexpected {type(e).__name__}"]))
                    continue
                if canonical.id not in in_call:
                    in_call.add(canonical.id)
                    out.sources.append(canonical)
                    if is_new:
                        budget.sources_collected += 1
        return out


class StaticProvider:
    """Fixture provider: returns canned hits / candidates. No network, no I/O."""

    def __init__(self, name: str, hits: Optional[dict[str, list]] = None,
                 candidates: Optional[list] = None) -> None:
        self.name = name
        self._hits = hits or {}
        self._candidates = candidates or []

    def search(self, query: SearchQuery) -> list:
        low = query.text.casefold()
        return [h for kw, hs in self._hits.items() if kw.casefold() in low for h in hs]

    def discover(self, request: DiscoveryRequest) -> list:
        cats = set(request.categories)
        return [c for c in self._candidates if not cats or getattr(c, "category", None) in cats][: request.max_candidates]
