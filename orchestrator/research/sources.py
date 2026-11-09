"""Source classification, quality tiers, URL safety, lineage/independence, claim assessment.

Trust model (PROPOSED defaults, kept as plain data so they can be tuned):

* Provider-supplied metadata is UNTRUSTED. A provider's `declared_type` and
  `declared_peer_reviewed` are recorded but never used to rank a source.
* The type is derived from the URL (host AND page-path hints). A host only gives a *hint*:
  being on a .gov/.edu/journal domain never makes a page authoritative by itself.
* Without confirmation no source can exceed tier B. Tier A needs `confirmed_by`
  ("human:<name>" or "process:<name>"), set through `confirm_source`. The string is not
  authentication; the guarantee is that nothing arriving through a provider can set it.
* A derived source can never outrank what it derives from; retraction propagates.
* Corroboration is counted per *independence group* (shared work, version, URL, DOI,
  publisher, author or lineage = one group), and unknown origin is treated as one group.

Nothing here fetches anything.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import ipaddress
import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from .attestation import DEFAULT_TRUST, TrustPolicy, VerifyResult
from .models import (Attestation, Citation, Claim, ClaimKind, ClaimRisk, ClaimStatus, QualityTier, ReviewStatus,
                     Source, SourceType, Stance, TopicKind)
from .risk import classify_risk
from .safety import (AUTHOR_MAX, LANG_MAX, MAX_AUTHORS, NOTE_MAX, PUBLISHER_MAX, TITLE_MAX, URL_MAX,
                     has_token_pattern, is_secret_param, looks_like_instruction, sanitize_text,
                     unsafe_text_problems)

# ------------------------------------------------------------------ tier rules
BASE_TIER: dict[SourceType, QualityTier] = {
    SourceType.PRIMARY_RESEARCH: QualityTier.B,        # -> A only with confirmed peer review
    SourceType.OFFICIAL_DOCUMENTATION: QualityTier.A,  # (A only when confirmed; else B)
    SourceType.GOVERNMENT_INSTITUTIONAL: QualityTier.A,
    SourceType.ACADEMIC_INSTITUTION: QualityTier.A,
    SourceType.PREPRINT: QualityTier.B,
    SourceType.ORIGINAL_REPORTING: QualityTier.B,
    SourceType.EXPERT_COMMENTARY: QualityTier.B,
    SourceType.SECONDARY_REPORTING: QualityTier.C,
    SourceType.REFERENCE_WORK: QualityTier.C,
    SourceType.BLOG: QualityTier.C,
    SourceType.COMMERCIAL: QualityTier.C,
    SourceType.VIDEO_PLATFORM: QualityTier.C,
    SourceType.PUBLISHER_PAGE: QualityTier.C,
    SourceType.OPINION_COMMENTARY: QualityTier.C,
    SourceType.PERSONAL_PAGE: QualityTier.C,
    SourceType.SOCIAL_COMMUNITY: QualityTier.D,
    SourceType.AI_GENERATED: QualityTier.D,
    SourceType.RESOLVER: QualityTier.D,
    SourceType.UNKNOWN: QualityTier.D,
}
_ORDER = [QualityTier.A, QualityTier.B, QualityTier.C, QualityTier.D]

FRESHNESS_WINDOW_DAYS: dict[TopicKind, Optional[int]] = {
    TopicKind.CURRENT_EVENT: 30,
    TopicKind.TECHNOLOGY: 365,
    TopicKind.SCIENCE: 365 * 3,
    TopicKind.FORMAT_STUDY: 365,
    TopicKind.EVERGREEN: None,
}
DEFAULT_TIME_SENSITIVE_WINDOW_DAYS = 365   # used when an evergreen brief flags a claim time-sensitive
AUTO_CAVEAT = "[auto] "          # prefix of caveats DERIVED from the evidence (rewritten on every build)
MIN_ASSESSMENT_CHARS = 10
MIN_JUSTIFICATION_CHARS = 20


def _worse(a: QualityTier, b: QualityTier) -> QualityTier:
    return a if a.rank >= b.rank else b


def _lower(t: QualityTier) -> QualityTier:
    return _ORDER[min(_ORDER.index(t) + 1, 3)]


# ------------------------------------------------------------- host / path rules
RESOLVER_HOSTS = ("doi.org", "hdl.handle.net", "handle.net")
SHORTENERS = {"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "lnkd.in", "rb.gy",
              "cutt.ly", "shorturl.at", "fb.me"}
INDEX_HOSTS = ("ncbi.nlm.nih.gov",)
PREPRINT_HOSTS = ("arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com")
JOURNAL_HOSTS = ("nature.com", "science.org", "sciencedirect.com", "springer.com", "cell.com", "nejm.org",
                 "thelancet.com", "plos.org", "pnas.org", "wiley.com", "frontiersin.org", "mdpi.com")
GOV_HOSTS = ("gov", "gov.hr", "gov.uk", "europa.eu", "who.int", "un.org", "nasa.gov", "nih.gov")
ACADEMIC_HOSTS = ("edu", "ac.uk", "unizg.hr", "unist.hr", "uniri.hr", "unios.hr", "unipu.hr", "hazu.hr", "irb.hr")
NEWS_HOSTS = ("reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "hina.hr")
REFERENCE_HOSTS = ("wikipedia.org", "britannica.com")
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")
SOCIAL_HOSTS = ("reddit.com", "x.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com",
                "threads.net", "quora.com", "discord.com", "t.me")
BLOG_HOSTS = ("medium.com", "substack.com", "blogspot.com", "wordpress.com", "tumblr.com")

PERSONAL_MARKERS = {"students", "student", "people", "personal", "homepages", "homepage", "users", "user",
                    "blogs", "blog", "members", "staff"}
OPINION_MARKERS = {"opinion", "opinions", "commentary", "editorial", "editorials", "column", "columns",
                   "comment", "perspective", "perspectives", "viewpoint", "letters", "oped", "op-ed"}
JOURNAL_NEWS_MARKERS = {"news", "press", "features", "blog", "blogs"}
JOURNAL_RESEARCH_MARKERS = {"article", "articles", "doi", "abs", "pdf", "pmc", "fulltext", "paper", "papers"}
GOV_PUBLICATION_MARKERS = {"publications", "publication", "reports", "report", "data", "statistics",
                           "documents", "document", "docs", "guidance", "policy", "legislation", "standards"}
ACADEMIC_PUBLICATION_MARKERS = {"publications", "publication", "papers", "reports", "report", "datasets",
                                "dataset", "technical-reports", "theses", "journal"}
_TRACKING_PREFIX = ("utm_",)
_TRACKING_EXACT = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "msclkid", "yclid"}
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp", ".private")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")
_CONFIRMER_RE = re.compile(r"^(?:human|process):[A-Za-z0-9_.@ \-]{1,60}$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*$")


def hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _match(host: str, suffixes: Iterable[str]) -> bool:
    return any(_host_matches(host, s) for s in suffixes)


def normalize_doi(value: str) -> str:
    """Lower-cased bare DOI ('10.x/y') or '' when not a DOI."""
    v = unquote((value or "").strip()).lower()
    for pre in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if v.startswith(pre):
            v = v[len(pre):]
    return v if _DOI_RE.match(v) else ""


# --------------------------------------------------------------- URL validation
def _host_problem(host: str) -> Optional[tuple[str, str]]:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return ("URL_PRIVATE_HOST", "private/loopback/reserved address rejected")
        return ("URL_IP_LITERAL", "IP-literal hosts are not accepted as sources")
    if _NUMERIC_HOST.match(host):
        return ("URL_AMBIGUOUS_HOST", "numeric host form (possible disguised IP) rejected")
    if "." not in host:
        return ("URL_PRIVATE_HOST", "single-label host rejected (internal name)")
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return ("URL_PRIVATE_HOST", "local/internal host rejected")
    if "%" in host:
        return ("URL_INVALID", "host contains percent-encoding")
    if not host.isascii():
        return ("URL_HOST_NON_ASCII", "non-ASCII host rejected; use punycode and confirm the domain")
    return None


def url_problems(url: object) -> list[tuple[str, str]]:
    """Error-level problems as (code, message). Never echoes secret values."""
    if not isinstance(url, str) or not url.strip():
        return [("URL_INVALID", "URL missing")]
    out: list[tuple[str, str]] = []
    if len(url) > URL_MAX:
        return [("URL_TOO_LONG", f"URL longer than {URL_MAX} characters")]
    if re.search(r"[\s\x00-\x1f\x7f\\]", url):
        return [("URL_INVALID", "URL contains whitespace, control characters or backslashes")]
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return [("URL_INVALID", "URL cannot be parsed")]
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        return [("URL_INVALID", "URL must be http(s) with a host")]
    if parts.username is not None or parts.password is not None:
        out.append(("URL_CONTAINS_SECRET", "URL embeds credentials; remove before storing"))
    if port not in (None, 80, 443):
        out.append(("URL_PORT_UNUSUAL", "non-standard port rejected"))
    hp = _host_problem(host)
    if hp:
        out.append(hp)
    secret = any(is_secret_param(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True))
    if "=" in parts.fragment:
        secret = secret or any(is_secret_param(k) for k, _ in parse_qsl(parts.fragment, keep_blank_values=True))
    if has_token_pattern(url):
        secret = True
    if secret:
        out.append(("URL_CONTAINS_SECRET", "URL query/fragment/path looks like it carries a key or token"))
    return out


def url_warnings(url: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        parts = urlsplit(url)
    except ValueError:
        return out
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme == "http":
        out.append(("URL_NOT_HTTPS", "plain http: content can be altered in transit"))
    if any(l.startswith("xn--") for l in host.split(".")):
        out.append(("URL_PUNYCODE_HOST", "punycode host: check for look-alike characters"))
    if host in SHORTENERS:
        out.append(("URL_SHORTENER", "URL shortener hides the destination; supply final_url"))
    return out


def _is_tracking(k: str) -> bool:
    k = k.lower()
    return k.startswith(_TRACKING_PREFIX) or k in _TRACKING_EXACT


def normalize_url(url: str) -> str:
    """Dedupe key (not a fetchable URL): scheme-less, lower-case host without 'www.', default
    ports and fragments dropped, tracking params dropped, query sorted, trailing '/' dropped.
    The PATH KEEPS ITS CASE (paths are case-sensitive)."""
    raw = (url or "").strip()
    try:
        p = urlsplit(raw)
        port = p.port
    except ValueError:
        return raw
    host = (p.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    port_s = f":{port}" if port not in (None, 80, 443) else ""
    path = p.path.rstrip("/")
    q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _is_tracking(k))
    return f"//{host}{port_s}{path}" + (f"?{urlencode(q)}" if q else "")


# --------------------------------------------------------------- classification
@dataclass
class Classification:
    source_type: SourceType
    method: str                      # always "heuristic" from classify_*; "confirmed" via confirm_source
    notes: list[str] = field(default_factory=list)
    doi: str = ""


def _segments(url: str) -> list[str]:
    try:
        return [unquote(x).lower() for x in urlsplit(url).path.split("/") if x]
    except ValueError:
        return []


def _classify_one(url: str) -> Classification:
    host = hostname(url)
    segs = _segments(url)
    labels = host.split(".")
    notes: list[str] = []
    personal = (any(l in PERSONAL_MARKERS for l in labels[:-2]) or any(s in PERSONAL_MARKERS or s.startswith("~")
                                                                         for s in segs))
    opinion = any(s in OPINION_MARKERS for s in segs)

    def done(t: SourceType, doi: str = "") -> Classification:
        return Classification(t, "heuristic", notes, doi)

    if _match(host, RESOLVER_HOSTS):
        notes.append("DOI/handle resolver: says nothing about the target page; supply final_url")
        return done(SourceType.RESOLVER, normalize_doi("/".join(segs)) if _host_matches(host, "doi.org") else "")
    if host in SHORTENERS:
        notes.append("URL shortener: destination unknown")
        return done(SourceType.UNKNOWN)
    if _match(host, INDEX_HOSTS):
        notes.append("index record (may be a preprint or retracted); confirm peer review before tier A")
        return done(SourceType.PRIMARY_RESEARCH)
    if _match(host, PREPRINT_HOSTS):
        return done(SourceType.PREPRINT)
    if _match(host, JOURNAL_HOSTS):
        if opinion:
            return done(SourceType.OPINION_COMMENTARY)
        if any(s in JOURNAL_NEWS_MARKERS for s in segs):
            return done(SourceType.ORIGINAL_REPORTING)
        if any(s in JOURNAL_RESEARCH_MARKERS for s in segs):
            notes.append("research page inferred from path; news/opinion can share this pattern; confirm")
            return done(SourceType.PRIMARY_RESEARCH)
        notes.append("publisher host, document type not established")
        return done(SourceType.PUBLISHER_PAGE)
    if _match(host, NEWS_HOSTS):
        return done(SourceType.OPINION_COMMENTARY if opinion else SourceType.ORIGINAL_REPORTING)
    if _match(host, GOV_HOSTS):
        if personal:
            return done(SourceType.PERSONAL_PAGE)
        if opinion:
            return done(SourceType.OPINION_COMMENTARY)
        if any(s in GOV_PUBLICATION_MARKERS for s in segs):
            notes.append("institutional publication inferred from path; confirm for tier A")
            return done(SourceType.GOVERNMENT_INSTITUTIONAL)
        notes.append("institution host, document type not established")
        return done(SourceType.PUBLISHER_PAGE)
    if _match(host, ACADEMIC_HOSTS):
        if personal:
            return done(SourceType.PERSONAL_PAGE)
        if opinion:
            return done(SourceType.OPINION_COMMENTARY)
        if any(s in ACADEMIC_PUBLICATION_MARKERS for s in segs):
            notes.append("academic publication inferred from path; confirm for tier A")
            return done(SourceType.ACADEMIC_INSTITUTION)
        notes.append("academic host, document type not established")
        return done(SourceType.PUBLISHER_PAGE)
    if _match(host, REFERENCE_HOSTS):
        return done(SourceType.REFERENCE_WORK)
    if _match(host, VIDEO_HOSTS):
        return done(SourceType.VIDEO_PLATFORM)
    if _match(host, SOCIAL_HOSTS):
        return done(SourceType.SOCIAL_COMMUNITY)
    if _match(host, BLOG_HOSTS):
        return done(SourceType.BLOG)
    notes.append(f"host '{host}' not in rules; needs review")
    return done(SourceType.UNKNOWN)


def classify_page(url: str, final_url: Optional[str] = None) -> Classification:
    """Classify from the URL (and the post-redirect URL when known). Uses the LESS trusted of the
    two if their hosts differ; a resolver is classified by where it resolved to."""
    first = _classify_one(url)
    if not final_url or final_url == url:
        return first
    final = _classify_one(final_url)
    if first.source_type is SourceType.RESOLVER:
        final.notes.insert(0, "resolver classified by its final_url (unverified by this package)")
        final.doi = final.doi or first.doi
        return final
    if hostname(url) != hostname(final_url):
        pick = final if BASE_TIER[final.source_type].rank >= BASE_TIER[first.source_type].rank else first
        pick.notes.insert(0, "REDIRECT_HOST_CHANGE: classified by the less trusted of requested and final host")
        pick.doi = pick.doi or first.doi or final.doi
        return pick
    return first


def classify_source(url: str, declared: Optional[SourceType] = None) -> Classification:
    """Heuristic classification. `declared` is accepted for compatibility but is UNTRUSTED: it never
    changes the result, only adds a note when it disagrees."""
    c = classify_page(url)
    if declared is not None and declared is not c.source_type:
        c.notes.append(f"declared type {declared.value} ignored (untrusted); classified {c.source_type.value}")
    return c


def attested(s: Source, trust: Optional[TrustPolicy] = None, today: Optional[dt.date] = None) -> VerifyResult:
    """Does this source carry a VALID attestation (human or registered process) for its current
    classification? `confirmed_by` is only a label and is not consulted."""
    return (trust or DEFAULT_TRUST).verify_source(s, today or dt.date.today())


def is_confirmed(s: Source, trust: Optional[TrustPolicy] = None, today: Optional[dt.date] = None) -> bool:
    return attested(s, trust, today).ok


def confirm_source(s: Source, *, by: str, source_type: Optional[SourceType] = None,
                   peer_reviewed: Optional[bool] = None, note: str = "",
                   attestation: Optional[Attestation] = None) -> Source:
    """Record a classification decision (labels) and optionally attach the attestation for it.

    On its own this changes nothing about ranking: `confirmed_by` is a label. The classification only
    counts when `attestation` verifies under the TrustPolicy used for assessment, and the attestation
    must have been issued for exactly these fields (type, peer review, page identity). Returns a new
    Source; the original is untouched."""
    if not _CONFIRMER_RE.match(by or ""):
        raise ValueError("confirmer must look like 'human:<name>' or 'process:<name>'")
    upd: dict = {"confirmed_by": by, "classified_by": "confirmed",
                 "confirmation_note": sanitize_text(note, NOTE_MAX)}
    if source_type is not None:
        upd["source_type"] = source_type
    if peer_reviewed is not None:
        upd["peer_reviewed"] = peer_reviewed
    if attestation is not None:
        upd["attestation"] = attestation
    return replace(s, **upd)


def effective_type(s: Source, trust: Optional[TrustPolicy] = None,
                   today: Optional[dt.date] = None) -> tuple[SourceType, list[str]]:
    """The type used for ranking. Attested: the stored type. Otherwise: the less trusted of the
    stored type and the type the URL itself supports (stored claims cannot raise it)."""
    if is_confirmed(s, trust, today):
        return s.source_type, []
    h = classify_page(s.url, s.final_url or None)
    notes = list(h.notes)
    # the strictly worse tier wins; on a tie the stored type is kept (same trust either way)
    chosen = h.source_type if BASE_TIER[h.source_type].rank > BASE_TIER[s.source_type].rank else s.source_type
    if chosen is not s.source_type:
        notes.append(f"stored type {s.source_type.value} is unconfirmed and not supported by the URL; "
                     f"ranked as {chosen.value}")
    return chosen, notes


PRIMARY_TYPES = frozenset({SourceType.PRIMARY_RESEARCH, SourceType.PREPRINT, SourceType.OFFICIAL_DOCUMENTATION,
                           SourceType.GOVERNMENT_INSTITUTIONAL, SourceType.ACADEMIC_INSTITUTION})


class DuplicateSourceId(ValueError):
    """Two different sources share an id. Raised BEFORE any evidence assessment so results can never
    depend on input order."""


def check_unique_ids(sources: Iterable[Source]) -> list[Source]:
    srcs = list(sources)
    seen: set[str] = set()
    for s in srcs:
        if s.id in seen:
            raise DuplicateSourceId(f"duplicate source id '{s.id}': refusing to assess evidence")
        seen.add(s.id)
    return srcs


@dataclass
class SourceAssessment:
    tier: QualityTier
    reasons: list[str]
    retrieved: bool = False      # an authenticated retrieval record exists (or the policy does not require one)
    primary: bool = False        # the source originates its content (research, official/institutional record)

    @property
    def can_support_fact_alone(self) -> bool:
        return self.tier is QualityTier.A


def retrieval_ok(s: Source, trust: Optional[TrustPolicy] = None, today: Optional[dt.date] = None) -> VerifyResult:
    t = trust or DEFAULT_TRUST
    if not t.require_retrieval:
        return VerifyResult(True, ["retrieval not required by policy"])
    return t.verify_retrieval(s.retrieval, s, today or dt.date.today())


def assess_source(s: Source, trust: Optional[TrustPolicy] = None,
                  today: Optional[dt.date] = None) -> SourceAssessment:
    """Tier from this source alone (no lineage). Use assess_all() when lineage matters. The tier is the
    CLASSIFICATION tier; whether the source may count as evidence also needs `.retrieved`."""
    t, reasons = effective_type(s, trust, today)
    reasons = list(reasons)
    tier = BASE_TIER[t]
    reasons.append(f"type {t.value} -> base tier {tier.value}")
    rv = retrieval_ok(s, trust, today)
    if not rv.ok:
        reasons.append("no authenticated retrieval record -> a lead, not evidence: " + "; ".join(rv.reasons))
    primary = t in PRIMARY_TYPES
    if s.retracted:
        return SourceAssessment(QualityTier.D, reasons + ["retracted"], rv.ok, primary)
    v = attested(s, trust, today)
    if t is SourceType.PRIMARY_RESEARCH:
        if v.ok and s.peer_reviewed is True:
            tier = QualityTier.A
            reasons.append(f"peer review attested ({v.kind}) -> A")
        else:
            reasons.append("peer review not attested -> stays B")
    elif tier is QualityTier.A and not v.ok:
        tier = QualityTier.B
        reasons.append("tier A requires a valid human or process attestation -> B")
    if not s.authors and not s.publisher.strip():
        tier = _lower(tier)
        reasons.append("no author or publisher identified -> one tier lower")
    return SourceAssessment(tier, reasons, rv.ok, primary)


# ---------------------------------------------------------------- lineage & independence
def _url_index(sources: list[Source]) -> dict[str, str]:
    idx: dict[str, str] = {}
    for s in sources:
        for u in (s.url, s.final_url):
            if u:
                idx.setdefault(normalize_url(u), s.id)
    return idx


def lineage_parent(s: Source, by_id: dict[str, Source], by_url: dict[str, str]) -> tuple[Optional[Source], bool]:
    """(parent source or None, unresolved). `unresolved` = it claims a parent we do not have."""
    if s.derived_from:
        p = by_id.get(s.derived_from)
        return p, p is None
    if s.original_source_url:
        pid = by_url.get(normalize_url(s.original_source_url))
        if pid and pid != s.id:
            return by_id[pid], False
        return None, True
    return None, False


def assess_all(sources: Iterable[Source], trust: Optional[TrustPolicy] = None,
               today: Optional[dt.date] = None) -> dict[str, SourceAssessment]:
    """Tiers with lineage applied: a derived source is never stronger than its underlying
    source, an unresolved original caps at C, a retracted root makes descendants D, cycles -> D."""
    srcs = check_unique_ids(sources)
    by_id = {s.id: s for s in srcs}
    by_url = _url_index(srcs)
    base = {s.id: assess_source(s, trust, today) for s in srcs}
    out: dict[str, SourceAssessment] = {}
    for s in srcs:
        tier, reasons = base[s.id].tier, list(base[s.id].reasons)
        seen, cur = {s.id}, s
        while True:
            parent, unresolved = lineage_parent(cur, by_id, by_url)
            if unresolved:
                if tier.rank < QualityTier.C.rank:
                    tier = QualityTier.C
                    reasons.append("underlying source not among the included sources -> capped at C")
            if parent is None:
                break
            if parent.id in seen:
                tier = QualityTier.D
                reasons.append("circular lineage -> D")
                break
            seen.add(parent.id)
            pt = QualityTier.D if parent.retracted else base[parent.id].tier
            if pt.rank > tier.rank:
                tier = pt
                reasons.append(f"capped by the underlying source {parent.id} ({pt.value})")
            cur = parent
        out[s.id] = SourceAssessment(tier, reasons, base[s.id].retrieved, base[s.id].primary)
    return out


def _work_key(s: Source) -> str:
    """Identifier of the underlying work: 'doi:<doi>' or a provider work id; '' if none."""
    w = (s.work_id or "").strip().casefold()
    if w.startswith("doi:"):
        d = normalize_doi(w[4:])
        return f"doi:{d}" if d else ""
    return w


_GENERIC_BYLINES = {"staff", "editorial", "editors", "admin", "unknown", "anonymous", "news desk"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.casefold())).strip()


@dataclass
class IndependenceResult:
    group_of: dict[str, str]
    members: dict[str, list[str]]
    reasons: dict[str, list[str]]
    strict_group_of: dict[str, str] = field(default_factory=dict)   # explicit links only (no shared 'unknown' buckets)

    def count(self, ids: Iterable[str]) -> int:
        return len({self.group_of[i] for i in ids if i in self.group_of})


def independence_groups(sources: Iterable[Source], independence: Iterable = (),
                        trust: Optional[TrustPolicy] = None, today: Optional[dt.date] = None) -> IndependenceResult:
    """Group sources that are NOT independent of each other. Conservative: any shared link merges, and
    UNKNOWN independence is never treated as established independence.

    Links: derived_from / original_source_url / version_of, same normalised URL, same DOI, same
    work_id, same publisher, a shared named author, a shared bucket for sources of unknown origin.
    SECONDARY reporting (news, blogs, commentary, wikis ...) that does not resolve to an included
    source is placed in one shared 'unestablished origin' group: two outlets covering the same study,
    a syndicated wire story or rewrites of one press release cannot be told apart from genuinely
    independent reporting, so they count once, unless a valid reviewer-signed independence attestation
    names them. Primary sources (research, official/institutional records) are grouped only by the
    links above, and only when a reviewer attestation covers their identity metadata (publisher, authors,
    lineage): self-reported, unsigned metadata could otherwise be rewritten to fake independence. Duplicate source ids are rejected before anything is computed."""
    srcs = check_unique_ids(sources)
    by_id = {s.id: s for s in srcs}
    attested_independent: set[str] = set()
    for ia in independence:
        if (trust or DEFAULT_TRUST).verify_independence(ia, by_id, today or dt.date.today()).ok:
            attested_independent.update(ia.source_ids)
    by_url = _url_index(srcs)
    parent = {s.id: s.id for s in srcs}
    parent2 = {s.id: s.id for s in srcs}       # STRICT grouping: explicit links only (used to detect self-contradiction)
    why: dict[str, list[str]] = {s.id: [] for s in srcs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def find2(x: str) -> str:
        while parent2[x] != x:
            parent2[x] = parent2[parent2[x]]
            x = parent2[x]
        return x

    def union(a: str, b: str, reason: str, strict: bool = True) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
        if strict:
            r2a, r2b = find2(a), find2(b)
            if r2a != r2b:
                parent2[r2b] = r2a
        why[a].append(reason)
        why[b].append(reason)

    buckets: dict[tuple, list[str]] = {}

    def bucket(key: tuple, sid: str) -> None:
        buckets.setdefault(key, []).append(sid)

    for s in srcs:
        par, unresolved = lineage_parent(s, by_id, by_url)
        if par is not None:
            union(s.id, par.id, "derived_from")
        if s.version_of and s.version_of in by_id:
            union(s.id, s.version_of, "version_of")
        if unresolved and s.original_source_url:
            bucket(("orig", normalize_url(s.original_source_url)), s.id)
        for u in (s.url, s.final_url):
            if u:
                bucket(("url", normalize_url(u)), s.id)
        d = normalize_doi(s.doi)
        if d:
            bucket(("doi", d), s.id)
        wk = _work_key(s)
        if wk:
            bucket(tuple(wk.split(":", 1)) if wk.startswith("doi:") else ("work", wk), s.id)
        pub = _norm(s.publisher)
        authors = [a for a in (_norm(x) for x in s.authors) if len(a) >= 4 and a not in _GENERIC_BYLINES]
        if pub:
            bucket(("publisher", pub), s.id)
        for a in authors:
            bucket(("author", a), s.id)
        if not pub and not authors:
            bucket(("unknown-origin",), s.id)
        if (par is None and s.id not in attested_independent
                and not (effective_type(s, trust, today)[0] in PRIMARY_TYPES and is_confirmed(s, trust, today))):
            bucket(("unestablished-origin",), s.id)
    for key, ids in buckets.items():
        uniq = list(dict.fromkeys(ids))
        weak = key[0] in ("unestablished-origin", "unknown-origin")
        for other in uniq[1:]:
            union(uniq[0], other, f"same {key[0]}" + (f": {key[1]}" if len(key) > 1 and key[0] != "url" else ""),
                  strict=not weak)
    members: dict[str, list[str]] = {}
    for s in srcs:
        members.setdefault(find(s.id), []).append(s.id)
    gid = {root: "grp:" + min(ids) for root, ids in members.items()}
    return IndependenceResult(
        strict_group_of={s.id: "strict:" + find2(s.id) for s in srcs},
        group_of={s.id: gid[find(s.id)] for s in srcs},
        members={gid[r]: sorted(ids) for r, ids in members.items()},
        reasons={gid[r]: sorted({x for i in ids for x in why[i]}) for r, ids in members.items()})


# ------------------------------------------------------------------------ dates
_PARTIAL = re.compile(r"^([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?$", re.ASCII)


def parse_partial_date(value: Optional[str]) -> Optional[tuple[dt.date, dt.date]]:
    """'2024' -> (2024-01-01, 2024-12-31). None/'' -> None. Invalid -> ValueError."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"bad date {value!r}")
    m = _PARTIAL.match(value)
    if not m:
        raise ValueError(f"bad date '{value}' (use YYYY, YYYY-MM or YYYY-MM-DD)")
    y, mo, d = int(m[1]), m[2], m[3]
    if y < 1:
        raise ValueError(f"bad date '{value}'")
    if d:
        day = dt.date(y, int(mo), int(d))
        return day, day
    if mo:
        last = calendar.monthrange(y, int(mo))[1]
        return dt.date(y, int(mo), 1), dt.date(y, int(mo), last)
    return dt.date(y, 1, 1), dt.date(y, 12, 31)


def newest_partial_date(values: Iterable[Optional[str]]) -> Optional[str]:
    """Latest of several partial dates, compared by their latest possible day (not as strings)."""
    best, best_key = None, None
    for v in values:
        try:
            r = parse_partial_date(v)
        except ValueError:
            continue
        if r and (best_key is None or r[1] > best_key):
            best, best_key = v, r[1]
    return best


# ------------------------------------------------------------ issues & validation
@dataclass
class Issue:
    code: str
    severity: str  # "error" | "warning"
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code} @ {self.path}: {self.message}"


def validate_sources(sources: Iterable[Source], today: dt.date,
                     trust: Optional[TrustPolicy] = None) -> list[Issue]:
    issues: list[Issue] = []
    srcs = list(sources)
    ids = {s.id for s in srcs}
    seen_ids: set[str] = set()
    seen_keys: dict[str, str] = {}
    by_id = {s.id: s for s in srcs}

    def err(code: str, p: str, msg: str) -> None:
        issues.append(Issue(code, "error", p, msg))

    def warn(code: str, p: str, msg: str) -> None:
        issues.append(Issue(code, "warning", p, msg))

    for i, s in enumerate(srcs):
        p = f"sources[{i}:{s.id}]"
        if not s.id:
            err("SOURCE_ID_MISSING", p, "source needs an id")
        elif not _SOURCE_ID_RE.match(s.id):
            err("SOURCE_ID_INVALID", p, "id must be 1-64 chars of letters, digits, _ . : -")
        if s.id in seen_ids:
            err("DUPLICATE_ID", p, "duplicate source id")
        seen_ids.add(s.id)
        problems = url_problems(s.url)
        for code, msg in problems:
            err(code, p, msg)
        for code, msg in url_warnings(s.url) if not problems else []:
            warn(code, p, msg)
        for label, extra in (("final_url", s.final_url), ("requested_url", s.requested_url),
                             ("original_source_url", s.original_source_url)):
            if extra:
                for code, msg in url_problems(extra):
                    err(code, f"{p}.{label}", msg)
        if not problems:
            n = normalize_url(s.url)
            if n in seen_keys:
                warn("DUPLICATE_SOURCE", p, f"same URL as {seen_keys[n]}")
            seen_keys.setdefault(n, s.id)
            if s.final_url and hostname(s.final_url) != hostname(s.url):
                warn("REDIRECT_HOST_CHANGE", p, "final_url is on a different host; the less trusted host is used")
        # untrusted text
        text_fields = [("title", s.title, TITLE_MAX), ("publisher", s.publisher, PUBLISHER_MAX),
                       ("notes", s.notes, NOTE_MAX), ("language", s.language, LANG_MAX),
                       ("confirmation_note", s.confirmation_note, NOTE_MAX)]
        text_fields += [(f"authors[{j}]", a, AUTHOR_MAX) for j, a in enumerate(s.authors)]
        text_fields += [(f"classification_warnings[{j}]", w, NOTE_MAX) for j, w in enumerate(s.classification_warnings)]
        if len(s.authors) > MAX_AUTHORS:
            err("UNSAFE_TEXT", f"{p}.authors", f"more than {MAX_AUTHORS} authors")
        for name, value, limit in text_fields:
            for why in unsafe_text_problems(value, limit):
                err("UNSAFE_TEXT", f"{p}.{name}", why)
            if looks_like_instruction(value):
                warn("INJECTION_SUSPECT", f"{p}.{name}", "text reads like an instruction to an AI; treat as data only")
        if not s.title.strip():
            err("SOURCE_TITLE_MISSING", p, "title required")
        if s.retrieved_at > today:
            err("RETRIEVED_IN_FUTURE", p, "retrieval date is after today")
        try:
            pd = parse_partial_date(s.published)
        except ValueError as e:
            err("PUBLISHED_INVALID", p, str(e))
            pd = None
        if s.published is None:
            warn("PUBLISHED_UNKNOWN", p, "publication date unknown")
        if pd and pd[0] > s.retrieved_at:
            err("PUBLISHED_AFTER_RETRIEVED", p, "published after it was retrieved")
        if not s.authors and not s.publisher.strip():
            warn("ANONYMOUS_SOURCE", p, "no author or publisher recorded")
        for field_name in ("derived_from", "version_of"):
            ref = getattr(s, field_name)
            if ref is not None and ref not in ids:
                err("UNKNOWN_REFERENCE", p, f"{field_name} '{ref}' not found")
            if ref == s.id:
                err("DERIVED_CYCLE", p, f"{field_name} points at itself")
        if s.doi and not normalize_doi(s.doi):
            err("DOI_INVALID", p, "doi is not a valid DOI")
        if s.confirmed_by and not _CONFIRMER_RE.match(s.confirmed_by):
            err("CONFIRMER_INVALID", p, "confirmed_by must look like 'human:<name>' or 'process:<name>'")
        verdict = attested(s, trust, today)
        if s.attestation is not None and not verdict.ok:
            err("ATTESTATION_INVALID", p, "source attestation does not verify: " + "; ".join(verdict.reasons))
        if s.confirmed_by and s.attestation is None:
            warn("CONFIRMATION_UNATTESTED", p, "confirmed_by is only a label; without a valid attestation it has no effect")
        if s.peer_reviewed is True and not verdict.ok:
            err("PEER_REVIEW_UNCONFIRMED", p, "peer_reviewed=True needs a valid attestation (provider/LLM claims are untrusted)")
        if not verdict.ok:
            eff, _ = effective_type(s, trust, today)
            if BASE_TIER[eff].rank > BASE_TIER[s.source_type].rank:
                err("SOURCE_TYPE_UNCONFIRMED", p,
                    f"stored type {s.source_type.value} is not supported by the URL and has no valid attestation "
                    f"(ranked as {eff.value}); attest it or correct the type")
        if s.correction_state not in ("", "corrected", "updated"):
            err("CORRECTION_STATE_INVALID", p, "correction_state must be '', 'corrected' or 'updated'")
        tp = trust or DEFAULT_TRUST
        if s.retrieval is not None:
            rv = tp.verify_retrieval(s.retrieval, s, today)
            if not rv.ok:
                err("RETRIEVAL_INVALID", p, "retrieval record does not verify: " + "; ".join(rv.reasons))
        elif tp.require_retrieval:
            warn("RETRIEVAL_MISSING", p, "no authenticated retrieval record: this source is a lead, not evidence")
        if s.source_type is SourceType.UNKNOWN:
            warn("SOURCE_UNCLASSIFIED", p, "type unknown -> tier D (lead only)")
        if s.source_type is SourceType.RESOLVER:
            warn("SOURCE_RESOLVER", p, "DOI/handle resolver cannot be evidence; supply final_url")
        if s.retracted:
            warn("SOURCE_RETRACTED", p, "retracted; cannot support any claim")
    # derived_from cycles of any length
    for s in srcs:
        seen, cur = {s.id}, s
        while cur.derived_from and cur.derived_from in by_id:
            cur = by_id[cur.derived_from]
            if cur.id in seen:
                if cur.id == s.id and cur.derived_from != cur.id:
                    err("DERIVED_CYCLE", f"sources[{s.id}]", "circular derived_from lineage")
                break
            seen.add(cur.id)
    return issues


# ---------------------------------------------------------------- claim evidence
_PLACEHOLDER_LOCATORS = {"n/a", "na", "none", "-", "--", "unknown", "tbd", "todo", "?", "see source",
                         "source", "link", "page", "article", "here"}


def meaningful_locator(loc: str) -> bool:
    t = (loc or "").strip()
    return len(t) >= 3 and t.casefold() not in _PLACEHOLDER_LOCATORS and any(ch.isalnum() for ch in t)


LEVEL_ASSESSED, LEVEL_HUMAN, LEVEL_PROCESS = "assessed", "human", "process"


def citation_level(c: Citation, claim: Optional[Claim] = None, source: Optional[Source] = None,
                   trust: Optional[TrustPolicy] = None, today: Optional[dt.date] = None,
                   resolved: bool = False) -> tuple[str, str]:
    """(level, why_not). level is '' when the citation does not count, else:

      'assessed' - relevance was assessed (by an LLM, or by anyone without a valid attestation);
                   it can support or (with limits) contradict, but never VERIFY or ESTABLISH on its own;
      'human'    - a VALID reviewer attestation, bound to this brief, covers exactly this claim, source,
                   stance, locator, assessment and excerpt;
      'process'  - the same from a registered, documented, reproducible process.

    To count at all the citation also needs an authenticated EXCERPT PROOF (the locator is tied to the
    retrieved content) from a source with an authenticated RETRIEVAL record. A review LABEL never
    upgrades the level. `resolved=True` means a reviewer has resolved a stance change; then only a
    valid human attestation counts."""
    t = trust or DEFAULT_TRUST
    today = today or dt.date.today()
    if c.stance is Stance.CONTEXT:
        return "", "context only"
    if not resolved and c.assessed_stance is not c.stance:
        return "", "stance differs from the stance that was assessed (relabelled or never assessed)"
    if not meaningful_locator(c.locator):
        return "", "no evidence locator"
    if len((c.assessment or "").strip()) < MIN_ASSESSMENT_CHARS:
        return "", "no recorded assessment of what the source establishes"
    if c.review is ReviewStatus.UNREVIEWED:
        return "", "unreviewed"
    if source is None:
        return "", "source not available"
    if t.require_retrieval:
        ex = t.verify_excerpt(c, source, today)
        if not ex.ok:
            return "", "the locator is not tied to retrieved content: " + "; ".join(ex.reasons)
    level = LEVEL_ASSESSED
    if c.attestation is not None and claim is not None:
        v = t.verify_citation(claim, c, source, today)
        if v.ok:
            level = LEVEL_HUMAN if v.kind == "human" else LEVEL_PROCESS
    if resolved and level not in (LEVEL_HUMAN, LEVEL_PROCESS):
        return "", "a stance change needs a valid human or registered-process attestation"
    if c.previous_stance is Stance.CONTEXT and level != LEVEL_HUMAN:
        return "", "relabelled from context; needs a valid human attestation"
    return level, ""


def citation_counts(c: Citation, claim: Optional[Claim] = None, source: Optional[Source] = None,
                    trust: Optional[TrustPolicy] = None, today: Optional[dt.date] = None) -> tuple[bool, str]:
    level, why = citation_level(c, claim, source, trust, today)
    return bool(level), why


def is_safety_critical(claim: Claim, trust: Optional[TrustPolicy] = None, today: Optional[dt.date] = None) -> bool:
    """Declared safety-critical, or tripping the risk floor without a valid risk-downgrade attestation."""
    if claim.risk_class is ClaimRisk.SAFETY_CRITICAL:
        return True
    if classify_risk(claim.text):
        return not (trust or DEFAULT_TRUST).verify_risk_downgrade(claim, today or dt.date.today()).ok
    return False


def effective_time_sensitivity(claim: Claim, topic_kind: TopicKind, trust: Optional[TrustPolicy] = None,
                               today: Optional[dt.date] = None) -> tuple[bool, Optional[str]]:
    """(time_sensitive, note). Non-evergreen briefs: time-sensitive unless an evergreen exception is
    flagged AND covered by a valid reviewer attestation over this claim and its justification (a model
    cannot supply its own). Evergreen briefs: only when flagged. NOTE: the brief's topic_kind is itself
    unsigned input; classifying a current event as EVERGREEN is not caught here."""
    if topic_kind is TopicKind.EVERGREEN:
        return bool(claim.time_sensitive), None
    if claim.time_sensitive is False:
        v = (trust or DEFAULT_TRUST).verify_evergreen(claim, today or dt.date.today())
        if v.ok and len((claim.evergreen_justification or "").strip()) >= MIN_JUSTIFICATION_CHARS:
            return False, None
        return True, ("evergreen exception ignored: it needs a valid human/process attestation covering this "
                      "claim and its justification; treated as time-sensitive")
    return True, None


def freshness_window(topic_kind: TopicKind) -> int:
    return FRESHNESS_WINDOW_DAYS[topic_kind] or DEFAULT_TIME_SENSITIVE_WINDOW_DAYS


def _is_stale(s: Source, today: dt.date, window: int) -> Optional[bool]:
    """True/False, or None when the date is unknown/invalid. A partial date ('2026-10') is judged by
    its EARLIEST possible day: if it might be too old, it cannot establish freshness (conservative;
    the scoring decay uses the latest day instead because ranking is not verification)."""
    try:
        pd = parse_partial_date(s.published)
    except ValueError:
        return None
    if pd is None:
        return None
    return (today - pd[0]).days > window


CURRENT, HISTORICAL, UNDATED = "current", "historical", "undated"


def _currency(s: Source, today: dt.date, window: int) -> str:
    st = _is_stale(s, today, window)
    return CURRENT if st is False else (HISTORICAL if st else UNDATED)


# Evidence strength = tier weight x review-level weight, taken per independence group (a group is ONE
# witness however many pages it has). Tier D and unretrieved sources never count. PROPOSED defaults.
TIER_WEIGHT = {QualityTier.A: 1.0, QualityTier.B: 0.6, QualityTier.C: 0.3}
LEVEL_WEIGHT = {LEVEL_HUMAN: 1.0, LEVEL_PROCESS: 1.0, LEVEL_ASSESSED: 0.75}
CREDIBLE_MIN = 0.45        # at least a tier-B source with an assessed citation
SECONDARY_WEIGHT = 0.25    # each further independent group adds a quarter of its own strength ...
SIDE_CAP = 0.25            # ... but all further groups together add at most a quarter of the TOP group:
                           # a side can never exceed 1.25x its strongest witness, whatever the count
HISTORICAL_WEIGHT = 0.25   # old/undated contradictions of a time-sensitive, non-safety claim count a quarter
WEAK_RATIO = 0.5           # contradiction <= half the support: outweighed (kept as a caveat)
STRONGER_RATIO = 1.5       # contradiction >= 1.5x the support: CONTRADICTED; in between: DISPUTED
ROUTE_HUMAN = "human_reviewed_tier_a"
ROUTE_PROCESS = "process_verified"
ROUTE_TWO_B = "two_independent_tier_b"
MAX_LISTED_CAVEATS = 3


@dataclass
class EvidenceItem:
    source_id: str
    group: str
    tier: str
    level: str
    strength: float
    stance: str
    currency: str = CURRENT
    primary: bool = False
    strict_group: str = ""


@dataclass
class ExcludedEvidence:
    """A citation that did NOT count, and why. Nothing assessed is silently dropped."""
    source_id: str
    stance: str
    reason: str


@dataclass
class ClaimAssessment:
    status: ClaimStatus
    reasons: list[str]
    route: str = ""                                   # which verification route established VERIFIED
    support: list[EvidenceItem] = field(default_factory=list)
    contradiction: list[EvidenceItem] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)   # preserved weaker/unresolved disagreement
    balance: dict = field(default_factory=dict)        # support/contradiction scores and ratio
    excluded: list[ExcludedEvidence] = field(default_factory=list)
    safety_critical: bool = False
    unresolved_safety_warning: bool = False
    ambiguous_sources: list[str] = field(default_factory=list)
    outweighed_credible: list[str] = field(default_factory=list)   # credible dissent kept as caveat


def explain(a: ClaimAssessment) -> str:
    """Human-readable evidence trail: why the claim got its status."""
    lines = [f"status: {a.status.value}" + (f" via {a.route}" if a.route else "")
             + (" [safety-critical]" if a.safety_critical else "")
             + (" [UNRESOLVED SAFETY WARNING]" if a.unresolved_safety_warning else "")]
    for label, items in (("supporting", a.support), ("contradicting", a.contradiction)):
        for i in items:
            lines.append(f"  {label}: {i.source_id} tier {i.tier}, {i.level} citation, strength {i.strength:.2f}, "
                         f"{'primary' if i.primary else 'secondary'}, {i.currency} (origin {i.group})")
    for x in a.excluded:
        lines.append(f"  not counted: {x.source_id} ({x.stance}): {x.reason}")
    if a.balance:
        lines.append("  balance: " + ", ".join(f"{k}={v}" for k, v in a.balance.items()))
    lines += [f"  - {r}" for r in a.reasons]
    lines += [f"  caveat: {c}" for c in a.caveats]
    return "\n".join(lines)


def _side(items: list[EvidenceItem]) -> tuple[float, float, list[EvidenceItem]]:
    """(score, top strength, best item per group). Score = top group + a bounded bonus for further
    independent groups: min(0.25 * sum(others), 0.25 * top). A side is never worth more than 1.25x its
    strongest witness, so many weak sources cannot outweigh one strong one."""
    best: dict[str, EvidenceItem] = {}
    for it in items:
        if it.group not in best or it.strength > best[it.group].strength:
            best[it.group] = it
    ordered = sorted(best.values(), key=lambda i: -i.strength)
    if not ordered:
        return 0.0, 0.0, []
    top = ordered[0].strength
    bonus = min(SECONDARY_WEIGHT * sum(i.strength for i in ordered[1:]), SIDE_CAP * top)
    return round(top + bonus, 6), top, ordered


def assess_claim(claim: Claim, sources: Iterable[Source], *, today: dt.date,
                 topic_kind: TopicKind = TopicKind.EVERGREEN, trust: Optional[TrustPolicy] = None,
                 independence: Iterable = ()) -> ClaimAssessment:
    """Strongest status the evidence justifies, with an inspectable trail. `trust` must be bound to the
    brief (TrustPolicy.bound_to); an unbound policy verifies nothing.

    WHAT COUNTS. A citation counts only if its source has an authenticated retrieval record, its locator
    has an authenticated excerpt proof, it has a locator, a recorded assessment, an assessed stance equal to
    its stance and a review (citation_level). Tier D and unretrieved sources are leads: listed under
    `excluded`, never silently dropped. Sources that share an origin are one witness; secondary reporting
    whose independence is not established counts as one origin.

    STANCE INTEGRITY. A stance later changed (e.g. contradicts -> context) keeps its ORIGINAL assessed
    stance in force unless a valid human attestation resolves it. A source cited both for and against is
    ambiguous: its support is removed, its contradiction is KEPT, VERIFIED is blocked.

    BALANCE. Strength = tier x level per independent group; a side is capped at 1.25x its top witness.
    A credible contradiction (>= CREDIBLE_MIN) with fresh support: >= 1.5x -> CONTRADICTED; > 0.5x ->
    DISPUTED; otherwise outweighed and kept as a caveat. DISPUTED / CONTRADICTED additionally need
    human- or process-reviewed contradicting evidence: LLM-only contradictions cannot establish them,
    but block VERIFIED as unresolved. Old/undated contradictions of a time-sensitive claim count a
    quarter, except for safety-critical claims.

    VERIFIED needs: (1) a tier-A source with a human-attested citation; or (2) a process-attested citation
    on a tier A/B source; or (3) two independent tier A/B origins with assessed citations (for SCIENCE at
    least one primary). Safety-critical claims never use route 3 and are never VERIFIED while any warning
    (retrieved, unretrieved, historical, unassessed) is unresolved."""
    if claim.kind is not ClaimKind.FACT:
        return ClaimAssessment(ClaimStatus.NOT_APPLICABLE, [f"{claim.kind.value} is not a verifiable fact"])
    t = trust or DEFAULT_TRUST
    srcs = check_unique_ids(sources)
    by_id = {s.id: s for s in srcs}
    tiers = assess_all(srcs, t, today)
    ind = independence_groups(srcs, independence, t, today)
    safety = is_safety_critical(claim, t, today)
    ts, note = effective_time_sensitivity(claim, topic_kind, t, today)
    window = freshness_window(topic_kind)
    reasons: list[str] = []
    caveats: list[str] = []
    excluded: list[ExcludedEvidence] = []
    if note:
        reasons.append(note)
    if safety:
        reasons.append("safety-critical claim: conservative verification (no corroboration-only route; any "
                       "unresolved warning blocks VERIFIED)")
    sup: list[EvidenceItem] = []
    con: list[EvidenceItem] = []
    unresolved_con = False
    unresolved_safety = False
    taint: list[str] = []
    warned_sources: set[str] = set()

    def exclude(sid: str, stance: Stance, why: str) -> None:
        excluded.append(ExcludedEvidence(sid, stance.value, why))

    for c in claim.citations:
        s = by_id.get(c.source_id)
        if s is None:
            exclude(c.source_id, c.stance, "unknown source")
            reasons.append(f"citation to unknown source '{c.source_id}' ignored")
            continue
        if c.attestation is not None:
            v = t.verify_citation(claim, c, s, today)
            if not v.ok:
                msg = (f"the attestation on the citation to {s.id} does not verify ({'; '.join(v.reasons)}): "
                       "the reviewed content may have been altered")
                taint.append(msg)
                reasons.append(msg)
        if s.attestation is not None and s.id not in warned_sources:
            warned_sources.add(s.id)
            sv = t.verify_source(s, today)
            if not sv.ok:
                reasons.append(f"the attestation on source {s.id} does not verify ({'; '.join(sv.reasons)}): its "
                               "classification, dates or lineage may have been altered after review")
        c_eval, resolved = c, False
        if c.assessed_stance is not None and c.stance is not c.assessed_stance:
            resolved = c.attestation is not None and t.verify_citation(claim, c, s, today).ok
            if resolved:
                reasons.append(f"citation to {s.id} was changed {c.assessed_stance.value} -> {c.stance.value}; the "
                               "change is covered by a valid reviewer attestation")
            else:
                c_eval = replace(c, stance=c.assessed_stance)
                msg = (f"The citation to {s.id} was relabelled {c.assessed_stance.value} -> {c.stance.value} without an "
                       "authenticated resolution; the original assessment stays in force.")
                reasons.append(msg)
                caveats.append(msg)
        stance = c_eval.stance
        if stance is Stance.CONTEXT:
            continue
        a = tiers[s.id]
        if a.tier is QualityTier.D:
            exclude(s.id, stance, "tier D (lead only)")
            reasons.append(f"{s.id} is tier D (lead only); not counted as evidence")
            if stance is Stance.CONTRADICTS:
                caveats.append(f"A contradicting lead from {s.id} (tier D) was not counted.")
                unresolved_safety = unresolved_safety or safety
            continue
        if not a.retrieved:
            exclude(s.id, stance, "no authenticated retrieval record")
            reasons.append(f"{s.id} has no authenticated retrieval record: a lead, not evidence")
            if stance is Stance.CONTRADICTS:
                caveats.append(f"A contradicting lead from {s.id} has no authenticated retrieval record and was not counted.")
                unresolved_safety = unresolved_safety or safety
            continue
        level, why = citation_level(c_eval, claim, s, t, today, resolved)
        if not level:
            exclude(s.id, stance, why)
            reasons.append(f"citation to {s.id} does not count: {why}")
            if stance is Stance.CONTRADICTS:
                caveats.append(f"A contradicting citation from {s.id} (tier {a.tier.value}) could not be evaluated ({why}).")
                if a.tier.rank <= QualityTier.B.rank:
                    unresolved_con = True
                unresolved_safety = unresolved_safety or safety
            continue
        cur = _currency(s, today, window) if ts else CURRENT
        strength = round(TIER_WEIGHT[a.tier] * LEVEL_WEIGHT[level], 6)
        if stance is Stance.CONTRADICTS and cur != CURRENT and not safety:
            strength = round(strength * HISTORICAL_WEIGHT, 6)
        item = EvidenceItem(s.id, ind.group_of[s.id], a.tier.value, level, strength, stance.value, cur, a.primary,
                            ind.strict_group_of[s.id])
        (sup if stance is Stance.SUPPORTS else con).append(item)

    both = {i.strict_group for i in sup} & {i.strict_group for i in con}
    ambiguous_ids: list[str] = []
    if both:
        ambiguous_ids = sorted({i.source_id for i in sup + con if i.strict_group in both})
        reasons.append(f"{', '.join(ambiguous_ids)} are cited both for and against the claim from one origin: ambiguous. "
                       "The support is removed, the contradiction is kept, and VERIFIED is blocked until a human resolves it")
        caveats.append(f"Evidence from {', '.join(ambiguous_ids)} points both ways; only its contradiction was counted.")
        for i in sup:
            if i.strict_group in both:
                exclude(i.source_id, Stance.SUPPORTS, "ambiguous: the same origin is also cited against the claim")
        sup = [i for i in sup if i.strict_group not in both]

    fresh = [i for i in sup if i.currency == CURRENT]
    for i in sup:
        if i.currency != CURRENT:
            exclude(i.source_id, Stance.SUPPORTS, f"{i.currency} for a time-sensitive claim (window {window} days)")
            reasons.append(f"support from {i.source_id} is {i.currency} for a time-sensitive claim: not usable")
    sup_score, sup_top, _ = _side(fresh)
    con_score, con_top, con_groups = _side(con)
    balance = {"support": round(sup_score, 3), "contradiction": round(con_score, 3),
               "ratio": round(con_score / sup_score, 3) if sup_score else (None if not con_score else "inf")}
    safety_warning = safety and bool(con or unresolved_safety or unresolved_con)
    flags = dict(safety_critical=safety, unresolved_safety_warning=safety_warning, ambiguous_sources=ambiguous_ids)

    def result(status: ClaimStatus, why: str, route: str = "", outweighed=()) -> ClaimAssessment:
        return ClaimAssessment(status, reasons + [why], route, fresh, con, caveats, balance, excluded,
                               outweighed_credible=list(outweighed), **flags)

    con_reviewed = any(i.level in (LEVEL_HUMAN, LEVEL_PROCESS) for i in con)
    verdict: Optional[ClaimStatus] = None
    if con and con_top >= CREDIBLE_MIN:
        if not fresh:
            verdict, vwhy = ClaimStatus.CONTRADICTED, "credible contradicting evidence and no usable supporting evidence"
        elif sup_top < CREDIBLE_MIN:
            if con_score >= sup_score:
                verdict, vwhy = ClaimStatus.CONTRADICTED, "credible contradiction outweighs weak support"
            else:
                verdict, vwhy = ClaimStatus.DISPUTED, "a credible contradiction is not outweighed by weak supports"
        elif con_score >= STRONGER_RATIO * sup_score:
            verdict, vwhy = ClaimStatus.CONTRADICTED, (f"stronger credible evidence weighs against the claim "
                                                     f"({con_score:.2f} vs {sup_score:.2f})")
        elif con_score > WEAK_RATIO * sup_score:
            verdict, vwhy = ClaimStatus.DISPUTED, (f"comparably credible evidence on both sides "
                                                  f"({sup_score:.2f} for, {con_score:.2f} against): quality cannot settle it")
    unresolved_llm_con = False
    if verdict is not None and not con_reviewed:
        unresolved_llm_con = True
        reasons.append(f"the contradiction would give {verdict.value}, but it rests on LLM-assessed evidence only; "
                       "that cannot establish a contested status")
        caveats.append("A credible contradiction rests on LLM-assessed evidence only: it blocks VERIFIED and needs "
                       "human or process review to resolve.")
        verdict = None
    if verdict is not None:
        return result(verdict, vwhy)

    outweighed: list[str] = []
    if con:
        listed = con_groups if len(con_groups) <= MAX_LISTED_CAVEATS else []
        for i in listed:
            kind = "credible but weaker" if i.strength >= CREDIBLE_MIN else "weak"
            hist = f", {i.currency}" if i.currency != CURRENT else ""
            vs = (f"was outweighed by the supporting evidence ({sup_score:.2f})" if sup_score
                  else "is not enough to count against the claim (and nothing supports it either)")
            caveats.append(f"A {kind} disagreement from {i.source_id} (tier {i.tier}, {i.level} citation{hist}, strength "
                           f"{i.strength:.2f}) {vs}; kept as a caveat.")
        if not listed:
            ids = ", ".join(i.source_id for i in con_groups)
            caveats.append(f"{len(con_groups)} weaker disagreements ({ids}) did not outweigh the supporting evidence "
                           f"({sup_score:.2f}); kept as a caveat.")
        outweighed = [i.source_id for i in con_groups if i.strength >= CREDIBLE_MIN]
        reasons.append(f"contradicting evidence ({con_score:.2f}) does not change the status (support {sup_score:.2f})")

    if not sup:
        return result(ClaimStatus.UNVERIFIED, "no counted supporting evidence", outweighed=outweighed)
    if not fresh:
        return result(ClaimStatus.PARTIAL, "time-sensitive claim: no supporting source is current", outweighed=outweighed)
    ab = [i for i in fresh if i.tier in ("A", "B")]
    groups_ab = {i.group for i in ab}
    primary_ab = {i.group for i in ab if i.primary}
    route = ""
    if any(i.level == LEVEL_HUMAN and i.tier == "A" for i in fresh):
        route = ROUTE_HUMAN
    elif any(i.level == LEVEL_PROCESS and i.tier in ("A", "B") for i in fresh):
        route = ROUTE_PROCESS
    elif len(groups_ab) >= 2 and not safety and (topic_kind is not TopicKind.SCIENCE or primary_ab):
        route = ROUTE_TWO_B
    blockers: list[str] = []
    if unresolved_con:
        blockers.append("a contradicting citation from a tier A/B source could not be evaluated")
    if unresolved_llm_con:
        blockers.append("an LLM-only credible contradiction is unresolved")
    if ambiguous_ids:
        blockers.append("a source is cited both for and against the claim")
    if taint:
        blockers.extend(taint)
    if safety_warning:
        blockers.append("a safety warning is unresolved (a credible, historical, unretrieved or unassessed warning "
                        "has not been resolved by a human or process review)")
    if route and not blockers:
        return result(ClaimStatus.VERIFIED, f"verified via {route}", route, outweighed)
    if route:
        return result(ClaimStatus.PARTIAL, "would be verified via " + route + " but is blocked: " + "; ".join(blockers),
                      outweighed=outweighed)
    if safety and len(groups_ab) >= 2:
        return result(ClaimStatus.PARTIAL, "safety-critical claims are not verified by corroboration alone: a human or "
                      "process review is required", outweighed=outweighed)
    if any(i.tier == "A" for i in fresh):
        return result(ClaimStatus.PARTIAL, "tier A support rests only on an assessment that no valid human or "
                      "process attestation covers: an LLM assessment cannot verify a claim", outweighed=outweighed)
    uncounted = [g for g in {i.group for i in ab} if any("unestablished-origin" in r for r in ind.reasons.get(g, []))]
    extra = (" Secondary reporting whose independence is not established counts as one origin."
             if uncounted or len(fresh) >= 2 else "")
    return result(ClaimStatus.PARTIAL, "support exists but is not enough to verify (one origin, or tier C only)." + extra,
                  outweighed=outweighed)


# ------------------------------------------------------------ construction helpers
def stable_source_id(url: str, doi: str = "", prefix: str = "S") -> str:
    """Content-derived id: the same DOI/URL always gets the same id, different ones differ, so ids
    from separate gather calls cannot collide or silently re-point a citation."""
    key = f"doi:{normalize_doi(doi)}" if normalize_doi(doi) else f"url:{normalize_url(url)}"
    return f"{prefix}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}"


def make_source(url: str, title: str, retrieved_at: dt.date, *, id: str,
                declared_type: Optional[SourceType] = None, **kw) -> Source:
    """Build a Source from untrusted input: classify from the URL, record (not trust) declarations,
    sanitise all text. Legacy `peer_reviewed=` / `source_type=` keywords are treated as DECLARATIONS."""
    if "peer_reviewed" in kw:
        kw["declared_peer_reviewed"] = kw.pop("peer_reviewed")
    if "source_type" in kw:
        declared_type = declared_type or kw.pop("source_type")
        kw.pop("source_type", None)
    c = classify_page(url, kw.get("final_url") or None)
    warnings = list(c.notes)
    if declared_type is not None and declared_type is not c.source_type:
        warnings.append(f"provider/caller declared type {declared_type.value} ignored (untrusted); "
                        f"classified {c.source_type.value}")
    if kw.get("declared_peer_reviewed") is True:
        warnings.append("declared peer review ignored (untrusted); confirm via confirm_source")
    for code, msg in url_warnings(url):
        warnings.append(f"{code}: {msg}")
    for key, limit in (("publisher", PUBLISHER_MAX), ("notes", NOTE_MAX), ("language", LANG_MAX)):
        if key in kw:
            kw[key] = sanitize_text(kw[key], limit)
    if "authors" in kw:
        kw["authors"] = [a for a in (sanitize_text(x, AUTHOR_MAX) for x in list(kw["authors"])[:MAX_AUTHORS]) if a]
    doi = normalize_doi(kw.pop("doi", "") or "") or c.doi
    return Source(id=id, url=url, title=sanitize_text(title, TITLE_MAX), retrieved_at=retrieved_at,
                  source_type=c.source_type, classified_by="heuristic", declared_type=declared_type,
                  doi=doi, classification_warnings=[sanitize_text(w, NOTE_MAX) for w in warnings], **kw)


class SourceIdConflict(ValueError):
    pass


class SourceCatalog:
    """Cross-call registry: one entry per underlying page/work, stable ids, no silent re-pointing."""

    def __init__(self) -> None:
        self._by_id: dict[str, Source] = {}
        self._by_key: dict[str, str] = {}

    @staticmethod
    def keys(s: Source) -> list[str]:
        ks = []
        d = normalize_doi(s.doi)
        if d:
            ks.append(f"doi:{d}")
        # NOT the work id: a preprint and its journal version are different pages of one work
        for u in (s.url, s.final_url):
            if u:
                ks.append(f"url:{normalize_url(u)}")
        return ks

    def find(self, s: Source) -> Optional[Source]:
        for k in self.keys(s):
            sid = self._by_key.get(k)
            if sid:
                return self._by_id[sid]
        return None

    def add(self, s: Source) -> tuple[Source, bool]:
        """Return (canonical source, is_new). A duplicate merges provenance into the existing entry;
        an id that is already used by a DIFFERENT source raises SourceIdConflict."""
        existing = self.find(s)
        if existing is not None:
            if s.provider and s.provider not in existing.provider.split(","):
                existing.provider = ",".join(x for x in (existing.provider, s.provider) if x)
            if existing.attestation is None:      # never alter fields a reviewer signed over
                for attr in ("doi", "published", "publisher", "language", "work_id"):
                    if not getattr(existing, attr) and getattr(s, attr):
                        setattr(existing, attr, getattr(s, attr))
                if not existing.authors and s.authors:
                    existing.authors = list(s.authors)
            return existing, False
        if s.id in self._by_id:
            raise SourceIdConflict(f"id '{s.id}' already used by a different source")
        self._by_id[s.id] = s
        for k in self.keys(s):
            self._by_key.setdefault(k, s.id)
        return s, True

    @property
    def sources(self) -> list[Source]:
        return list(self._by_id.values())

    def get(self, sid: str) -> Optional[Source]:
        return self._by_id.get(sid)

    def attach_retrieval(self, sid: str, record, trust: Optional[TrustPolicy], today: dt.date) -> Source:
        """Attach a retrieval record issued by the ingestion boundary. Refused unless it verifies against
        THIS source under the policy: a provider or an LLM cannot invent or alter a receipt."""
        s = self._by_id.get(sid)
        if s is None:
            raise KeyError(f"unknown source '{sid}'")
        v = (trust or DEFAULT_TRUST).verify_retrieval(record, s, today)
        if not v.ok:
            raise ValueError("retrieval record rejected: " + "; ".join(v.reasons))
        s.retrieval = record
        return s
