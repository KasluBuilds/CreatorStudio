"""Explicit asset acquisition: triage -> bounded fetch through an INJECTED transport -> quarantined library record.

No network code lives here. A caller supplies a `Transport`; this module never opens a socket, never
imports an HTTP client, and tests prove it. Rules enforced:

  * Triage first. Unknown rights are never permission. `triage()` can never return "permitted" for a
    candidate that has no file, so the fetch gate is "ACQUIRABLE": not rejected, and every quarantine
    reason is one the acquisition step itself resolves (no file yet, unverified, claim-only evidence,
    watermark not yet checked...). Any other quarantine reason (unknown or unrecognised licence, missing
    attribution text, share-alike not accepted, hint of a preview ...) is recorded in a quarantine ledger
    and NOTHING is fetched. A rejected candidate is never fetched and never recorded as an asset.
  * Fetching is bounded: https only, per-provider host allow-list (also checked after redirects), maximum
    byte size, content-type allow-list per media type, magic-byte check, timeout parameter.
  * The licence text is snapshotted as evidence (url, retrieval date, sha256, verifier) BEFORE the file is
    fetched; `reverify_license` later detects drift and places a hold without deleting anything.
  * Nothing is approved here. The asset lands QUARANTINED; only a named human's `AssetLibrary.attest()`
    (the existing Attestation model) can approve it. There is no automated licence verification.
  * Every failure leaves no partial file behind and records a scrubbed error.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .evaluation import AssetRequest
from .library import AssetLibrary
from .models import (SCHEMA_VERSION, AssetExistsError, AssetRecord, DuplicateAssetError, Evidence, ManifestError,
                     MediaType, UnsafePathError, check_asset_id, parse_date, safe_suffix)
from .providers import (AssetCandidate, ProviderRegistry, _safe_url, candidate_to_record, safe_fetch_url,
                        sanitize_candidate, triage)
from .rights import REJECT, UseContext, evaluate_rights

VERIFIER = "asset-acquisition/1"          # who captured the licence snapshot (a tool, never a person)
SNAPSHOT_MARK = "Licence text snapshot captured at acquisition"
RECAPTURE_MARK = "Licence text snapshot re-captured after drift"
QUARANTINE_LEDGER = "candidate_quarantine.json"
STAGING = ".staging"

# Quarantine reasons that acquisition itself resolves or that only a human attestation can clear AFTER the
# file is in the library. Anything else blocks the fetch.
ACQUIRABLE_PENDING = frozenset({"NO_FILE", "UNVERIFIED", "NO_OWNER_DECLARATION", "WATERMARK_UNCHECKED",
                                "CLAIM_ONLY_EVIDENCE", "NO_DOCUMENT_EVIDENCE"})


# --------------------------------------------------------------------------------------------------
# transport (injected; nothing here performs I/O against a network)
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class TransportRequest:
    url: str
    timeout_s: float
    max_bytes: int
    accept: tuple = ()


@dataclass
class TransportResponse:
    status: int
    content_type: str = ""
    content_length: Optional[int] = None     # as declared by the server, if any
    final_url: str = ""                        # after redirects, if the transport followed any
    chunks: Iterable[bytes] = ()


@runtime_checkable
class Transport(Protocol):
    def fetch(self, request: TransportRequest) -> TransportResponse:
        """GET `request.url`. Must not send credentials. May raise any exception (it is contained)."""
        ...


RETRYABLE_CODES = frozenset({"RATE_LIMITED"})   # failures a caller may retry later; every other code is final


class FetchError(Exception):
    """A contained, already-scrubbed acquisition failure with a stable machine code."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = scrub(message)
        super().__init__(f"{code}: {self.message}" if self.message else code)


# --------------------------------------------------------------------------------------------------
# scrubbing
# --------------------------------------------------------------------------------------------------
_SCRUBBERS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|pwd|"
                r"authorization|auth|signature|sig|credential)s?\b\s*[=:]\s*[^\s,;&\"')]+"), r"\1=<redacted>"),
    (re.compile(r"(?i)(https?://)[^/\s@]+@"), r"\1<redacted>@"),
    (re.compile(r"(https?://[^\s?#\"')]+)\?[^\s\"')]*"), r"\1?<redacted>"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"\b(?:gh[pousr]_|xox[abprs]-|AKIA)[A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"[A-Za-z0-9_+/=-]{32,}"), "<redacted>"),
]


def scrub(text: Any, limit: int = 300) -> str:
    """Remove credential-like text (tokens, keys, userinfo, query strings, long opaque strings) from an error
    message and cap its length. Applied to EVERY error string this module records."""
    s = str(text)
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    for pat, rep in _SCRUBBERS:
        s = pat.sub(rep, s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


# --------------------------------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------------------------------
DEFAULT_CONTENT_TYPES: Mapping[MediaType, frozenset] = {
    MediaType.IMAGE: frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"}),
    MediaType.VIDEO: frozenset({"video/mp4", "video/webm", "video/quicktime"}),
    MediaType.AUDIO: frozenset({"audio/mpeg", "audio/wav", "audio/x-wav", "audio/flac", "audio/ogg", "audio/mp4"}),
}

_FAMILY = {"image/jpeg": "jpeg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
           "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/mpeg": "mp3",
           "audio/ogg": "ogg", "audio/flac": "flac", "audio/x-flac": "flac",
           "video/mp4": "isobmff", "video/quicktime": "isobmff", "audio/mp4": "isobmff", "audio/x-m4a": "isobmff",
           "video/webm": "matroska", "audio/webm": "matroska"}
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
        "audio/flac": ".flac", "audio/x-flac": ".flac", "video/mp4": ".mp4", "video/quicktime": ".mov",
        "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "video/webm": ".webm", "audio/webm": ".webm"}
_EXT_TYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
             ".webp": "image/webp", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
             ".flac": "audio/flac", ".m4a": "audio/mp4", ".mp4": "video/mp4", ".mov": "video/quicktime",
             ".webm": "video/webm"}


@dataclass(frozen=True)
class AcquisitionPolicy:
    """Everything the fetch gate needs. Defaults are conservative; a provider with no entry in
    `provider_hosts` can download nothing (fail closed)."""
    provider_hosts: Mapping[str, frozenset] = field(default_factory=dict)
    license_hosts: frozenset = frozenset({"creativecommons.org", "www.creativecommons.org"})
    max_bytes: int = 50_000_000
    max_license_bytes: int = 512_000
    content_types: Mapping[MediaType, frozenset] = field(default_factory=lambda: dict(DEFAULT_CONTENT_TYPES))
    license_content_types: frozenset = frozenset({"text/html", "text/plain"})
    timeout_s: float = 30.0

    def hosts_for(self, provider: str) -> frozenset:
        return frozenset(h.lower() for h in self.provider_hosts.get(provider, frozenset()))


@dataclass
class AcquisitionContext:
    policy: AcquisitionPolicy
    use: UseContext = field(default_factory=UseContext)
    today: str = "1970-01-01"
    max_results: int = 10


# --------------------------------------------------------------------------------------------------
# content sniffing
# --------------------------------------------------------------------------------------------------
def sniff_family(head: bytes) -> Optional[str]:
    """Container family from the first bytes, or None when unrecognised."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"fLaC"):
        return "flac"
    if head[4:8] == b"ftyp":
        return "isobmff"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0 and (head[1] >> 1) & 3):
        return "mp3"
    return None


def content_type_problem(content_type: str, media_type: MediaType, policy: AcquisitionPolicy, head: bytes) -> Optional[FetchError]:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in policy.content_types.get(media_type, frozenset()):
        return FetchError("CONTENT_TYPE_NOT_ALLOWED", f"{ct or 'missing'!r} is not allowed for {media_type.value}")
    if sniff_family(head) != _FAMILY.get(ct):
        return FetchError("CONTENT_MISMATCH", "file signature does not match the declared content type")
    return None


# --------------------------------------------------------------------------------------------------
# bounded fetch
# --------------------------------------------------------------------------------------------------
def check_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """https only, an allow-listed host, no credentials, default port. Returns the lower-case host."""
    if not safe_fetch_url(url):
        raise FetchError("URL_NOT_ALLOWED", "not a plain https URL")
    host = (urlsplit(url).hostname or "").lower()
    if host not in {h.lower() for h in allowed_hosts}:
        raise FetchError("HOST_NOT_ALLOWED", f"host {host!r} is not on the allow-list")
    return host


@dataclass
class FetchResult:
    sha256: str
    size: int
    content_type: str
    head: bytes


def fetch_to(transport: Transport, url: str, *, hosts: Iterable[str], max_bytes: int, timeout_s: float,
             accept: tuple, sink) -> FetchResult:
    """Stream `url` into the open binary file `sink`, enforcing every bound. Raises FetchError only."""
    hosts = list(hosts)
    check_url(url, hosts)
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise FetchError("BAD_POLICY", "max_bytes must be a positive integer")
    try:
        resp = transport.fetch(TransportRequest(url, float(timeout_s), max_bytes, tuple(accept)))
    except Exception as e:   # the transport is untrusted code
        raise FetchError("TRANSPORT_ERROR", f"{type(e).__name__}: {e}") from None
    if not isinstance(resp, TransportResponse):
        raise FetchError("TRANSPORT_ERROR", "transport returned an invalid response object")
    if resp.status == 429:   # observed live (Wikimedia, 2026-11-17): the host asks us to slow down; nothing was fetched
        raise FetchError("RATE_LIMITED", "status 429 (retryable later; not retried automatically)")
    if resp.status != 200:
        raise FetchError("HTTP_STATUS", f"status {resp.status}")
    if resp.final_url:
        check_url(resp.final_url, hosts) if safe_fetch_url(resp.final_url) else _bad_redirect()
    declared = resp.content_length
    if declared is not None and (not isinstance(declared, int) or isinstance(declared, bool) or declared < 0):
        raise FetchError("LENGTH_MISMATCH", "invalid declared length")
    if declared is not None and declared > max_bytes:
        raise FetchError("TOO_LARGE", f"declared {declared} bytes exceeds the {max_bytes} byte limit")
    h, size, head = hashlib.sha256(), 0, b""
    try:
        for chunk in resp.chunks:
            if not isinstance(chunk, (bytes, bytearray)):
                raise FetchError("TRANSPORT_ERROR", "transport yielded a non-bytes chunk")
            size += len(chunk)
            if size > max_bytes:
                raise FetchError("TOO_LARGE", f"download exceeds the {max_bytes} byte limit")
            if len(head) < 32:
                head = (head + bytes(chunk))[:32]
            h.update(chunk)
            sink.write(chunk)
    except FetchError:
        raise
    except Exception as e:   # a stream that dies half-way
        raise FetchError("TRANSPORT_ERROR", f"{type(e).__name__}: {e}") from None
    if size == 0:
        raise FetchError("EMPTY", "download is empty")
    if declared is not None and size < declared:
        raise FetchError("TRUNCATED", f"received {size} of {declared} declared bytes")
    if declared is not None and size > declared:
        raise FetchError("LENGTH_MISMATCH", f"received {size} bytes but {declared} were declared")
    sink.flush()
    return FetchResult(h.hexdigest(), size, (resp.content_type or "").split(";")[0].strip().lower(), head)


def _bad_redirect():
    raise FetchError("REDIRECT_NOT_ALLOWED", "redirect target is not a plain https URL")


# --------------------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------------------
OUTCOMES = ("acquired", "quarantined", "rejected", "failed", "duplicate")


@dataclass(frozen=True)
class ReportItem:
    provider: str
    external_id: str
    title: str
    outcome: str
    asset_id: str = ""
    reasons: tuple = ()
    detail: str = ""

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["reasons"] = list(self.reasons)
        return d


@dataclass
class AcquisitionReport:
    items: list = field(default_factory=list)
    provider_problems: list = field(default_factory=list)

    def _of(self, outcome: str) -> list:
        return sorted((i for i in self.items if i.outcome == outcome), key=lambda i: (i.provider, i.external_id))

    acquired = property(lambda self: self._of("acquired"))
    quarantined = property(lambda self: self._of("quarantined"))
    rejected = property(lambda self: self._of("rejected"))
    failed = property(lambda self: self._of("failed"))
    duplicates = property(lambda self: self._of("duplicate"))

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION,
                "counts": {o: len(self._of(o)) for o in OUTCOMES},
                **{o if o != "duplicate" else "duplicates": [i.to_dict() for i in self._of(o)] for o in OUTCOMES},
                "provider_problems": sorted(self.provider_problems)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------------------------------------
# candidate quarantine ledger (candidates have no file, so they cannot be library records)
# --------------------------------------------------------------------------------------------------
class QuarantineLedger:
    """Append-only record of candidates held back with their reason codes. Atomic writes; refuses to overwrite a
    damaged file; entries are never deleted."""
    KEYS = {"provider", "external_id", "title", "category", "media_type", "source_url", "claimed_license_name",
            "reasons", "recorded_at"}

    def __init__(self, root: Path | str):
        self.path = Path(root) / QUARANTINE_LEDGER
        self._entries: dict[tuple, dict] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise ManifestError(f"{QUARANTINE_LEDGER} unreadable ({type(e).__name__}); not overwriting it") from None
            if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION \
                    or not isinstance(data.get("candidates"), list):
                raise ManifestError(f"{QUARANTINE_LEDGER} has an invalid structure; not overwriting it")
            for i, e in enumerate(data["candidates"]):
                if not isinstance(e, dict) or set(e) != self.KEYS or not isinstance(e["reasons"], list) \
                        or not all(isinstance(x, str) for x in e["reasons"]):
                    raise ManifestError(f"{QUARANTINE_LEDGER}: candidates[{i}] is invalid")
                self._entries[(e["provider"], e["external_id"])] = e

    def entries(self) -> list[dict]:
        return [dict(e) for _, e in sorted(self._entries.items())]

    def record(self, c: AssetCandidate, reasons: list[str], at: str) -> None:
        self._entries[(c.provider, c.external_id)] = {
            "provider": c.provider, "external_id": c.external_id, "title": c.title,
            "category": c.category.value, "media_type": c.media_type.value, "source_url": _safe_url(c.source_url),
            "claimed_license_name": c.claimed_license_name, "reasons": sorted(set(reasons)), "recorded_at": at}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "candidates": self.entries()}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------------------
@dataclass
class Classification:
    outcome: str                 # "acquire" | "quarantine" | "reject"
    reasons: list = field(default_factory=list)   # codes


def classify(c: AssetCandidate, use: UseContext, today: str) -> Classification:
    """The fetch gate. Never returns "acquire" for a rejected or unknown-rights candidate."""
    d = triage(c, use, today)
    if d.verdict == REJECT:
        return Classification("reject", sorted({r.code for r in d.reasons if r.severity == "reject"}))
    blocking = sorted({r.code for r in d.reasons if r.severity == "quarantine" and r.code not in ACQUIRABLE_PENDING})
    extra = []
    if not c.local_path and not c.download_url:
        extra.append("NO_DOWNLOAD_SOURCE")
    if c.download_url and not c.license_text_url:
        extra.append("NO_LICENSE_TEXT_URL")      # no licence document can be captured: hold, never fetch
    if c.download_url and c.claimed_basis == "user_owned":
        extra.append("USER_OWNED_REMOTE")        # an ownership claim is only meaningful for the user's own local file
    # CLAIM_ONLY_EVIDENCE / NO_DOCUMENT_EVIDENCE are "pending" only because acquisition itself captures the licence
    # document. A local file has no such document, so a licence claim there is insufficiently supported: hold it.
    if c.local_path and c.claimed_basis != "user_owned":
        d = triage(c, use, today)
        for code in ("CLAIM_ONLY_EVIDENCE", "NO_DOCUMENT_EVIDENCE"):
            if code in d.codes():
                extra.append("LOCAL_LICENSE_UNDOCUMENTED")
                break
    if blocking or extra:
        return Classification("quarantine", sorted(set(blocking + extra)))
    return Classification("acquire", [])


def make_asset_id(provider: str, external_id: str) -> str:
    """Deterministic, filesystem-safe id: slug plus a short hash of the exact (provider, id) pair."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{provider}-{external_id}".lower()).strip("-")[:44].strip("-") or "asset"
    digest = hashlib.sha256(f"{provider}\x00{external_id}".encode("utf-8")).hexdigest()[:12]
    return check_asset_id(f"{slug}-{digest}")


# --------------------------------------------------------------------------------------------------
# licence snapshots
# --------------------------------------------------------------------------------------------------
def _plain_dir(root: Path, *parts: str) -> Path:
    cur = root
    for p in parts:
        cur = cur / p
        if cur.is_symlink() or getattr(cur, "is_junction", lambda: False)():
            raise UnsafePathError(f"{p!r} is a symlink/junction inside the library")
    return cur


def _write_snapshot(lib: AssetLibrary, asset_id: str, data: bytes, digest: str) -> str:
    """Exclusive-create the snapshot; returns the library-relative posix path."""
    d = _plain_dir(lib.root, "evidence", check_asset_id(asset_id))
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"license-{digest[:12]}.txt"
    with open(dest, "xb") as f:
        f.write(data)
    return dest.relative_to(lib.root).as_posix()


def _remove_snapshot(lib: AssetLibrary, rel: str) -> None:
    try:
        p = lib.root / rel
        p.unlink()
        p.parent.rmdir()
    except OSError:
        pass


def license_evidence(url: str, digest: str, today: str, rel_path: str) -> Evidence:
    return Evidence("license_page", f"{SNAPSHOT_MARK} {today}; verifier {VERIFIER}; sha256 {digest[:12]}...",
                    _safe_url(url), rel_path, digest, parse_date(today, "today"), "", "document")


def fetch_license_text(transport: Transport, url: str, policy: AcquisitionPolicy) -> tuple[bytes, str]:
    import io
    buf = io.BytesIO()
    res = fetch_to(transport, url, hosts=policy.license_hosts, max_bytes=policy.max_license_bytes,
                   timeout_s=policy.timeout_s, accept=tuple(sorted(policy.license_content_types)), sink=buf)
    if res.content_type not in policy.license_content_types:
        raise FetchError("CONTENT_TYPE_NOT_ALLOWED", f"licence text type {res.content_type or 'missing'!r} is not allowed")
    return buf.getvalue(), res.sha256


# --------------------------------------------------------------------------------------------------
# acquiring one candidate
# --------------------------------------------------------------------------------------------------
def _item(c: AssetCandidate, outcome: str, asset_id: str = "", reasons=(), detail: str = "") -> ReportItem:
    return ReportItem(c.provider, c.external_id, c.title, outcome, asset_id, tuple(reasons), scrub(detail))


def _expected_dest(lib: AssetLibrary, rec: AssetRecord, name: str) -> Path:
    return lib.root / "files" / rec.category.value / (rec.asset_id + safe_suffix(name))


def _acquire(c: AssetCandidate, lib: AssetLibrary, transport: Transport, ctx: AcquisitionContext) -> ReportItem:
    policy = ctx.policy
    asset_id = make_asset_id(c.provider, c.external_id)
    try:
        lib.get(asset_id)
        return _item(c, "duplicate", asset_id, ["ALREADY_ACQUIRED"], "this candidate was acquired earlier")
    except KeyError:
        pass
    rec = candidate_to_record(c, asset_id, ctx.today)
    rec.acquisition_date = parse_date(ctx.today, "today")
    stage_root = lib.root / STAGING
    stage_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=stage_root, prefix="acq-"))
    snapshot_rel = ""
    dest_created: Optional[Path] = None
    try:
        # ---- get the bytes into a staging file ------------------------------------------------
        if c.local_path:
            src = _stage_local(c, rec, policy)
            ev_snapshot = None
        else:
            ev_snapshot = None
            if c.claimed_basis != "user_owned":
                text, digest = fetch_license_text(transport, c.license_text_url, policy)
                ev_snapshot = (text, digest)
            part = stage / "payload.part"
            with open(part, "wb") as sink:
                res = fetch_to(transport, c.download_url, hosts=policy.hosts_for(c.provider), max_bytes=policy.max_bytes,
                               timeout_s=policy.timeout_s, accept=tuple(sorted(policy.content_types.get(c.media_type, ()))),
                               sink=sink)
            problem = content_type_problem(res.content_type, c.media_type, policy, res.head)
            if problem:
                raise problem
            if c.expected_sha256 and c.expected_sha256 != res.sha256:
                raise FetchError("HASH_MISMATCH", f"declared {c.expected_sha256[:12]}... but received {res.sha256[:12]}...")
            src = stage / ("payload" + _EXT[res.content_type])
            os.replace(part, src)
            rec.checksum_sha256 = res.sha256
        # ---- rights check on the facts we now hold (a reject here never reaches the library) --------
        if ev_snapshot is not None:
            text, digest = ev_snapshot
            snapshot_rel = _write_snapshot(lib, asset_id, text, digest)
            rec.evidence.append(license_evidence(c.license_text_url, digest, ctx.today, snapshot_rel))
        probe = dataclasses.replace(rec, file_path=str(src), checksum_sha256=rec.checksum_sha256 or "0" * 64)
        decision = evaluate_rights(probe, ctx.use)
        if decision.verdict == REJECT:
            if snapshot_rel:
                _remove_snapshot(lib, snapshot_rel)
            return _item(c, "rejected", "", sorted({r.code for r in decision.reasons if r.severity == "reject"}))
        # ---- hand over to the library (copy mode: exclusive create, checksum re-verified) -----------
        dest = _expected_dest(lib, rec, src.name)
        existed = dest.exists()
        try:
            added = lib.add(rec, src, mode="copy", ctx=ctx.use)
        except DuplicateAssetError as e:
            if snapshot_rel:
                _remove_snapshot(lib, snapshot_rel)
            return _item(c, "duplicate", e.existing_id, ["DUPLICATE_CONTENT"], f"identical content is already indexed as {e.existing_id}")
        except AssetExistsError as e:
            if snapshot_rel:
                _remove_snapshot(lib, snapshot_rel)
            return _item(c, "duplicate", asset_id, ["ALREADY_EXISTS"], str(e))
        except Exception as e:   # the library write path failed: nothing may stay behind
            if not existed and dest.exists():
                dest_created = dest
            raise FetchError("LIBRARY_WRITE_ERROR", f"{type(e).__name__}: {e}") from None
        codes = sorted({r.code for r in evaluate_rights(added, ctx.use).reasons if r.severity != "info"})
        return _item(c, "acquired", added.asset_id, codes,
                     f"state {added.state.value}; sha256 {added.checksum_sha256[:12]}...; needs a named human attestation")
    except FetchError as e:
        if snapshot_rel:
            _remove_snapshot(lib, snapshot_rel)
        if dest_created is not None:
            try:
                dest_created.unlink()
            except OSError:
                pass
        return _item(c, "failed", "", [e.code], e.message)
    finally:
        shutil.rmtree(stage, ignore_errors=True)   # our own staging directory only
        try:
            stage_root.rmdir()                     # only if empty
        except OSError:
            pass


def _link_project(lib: AssetLibrary, existing_id: str, project: Optional[str]) -> None:
    if project:
        try:
            lib.link_project(existing_id, project)
        except (UnsafePathError, KeyError):
            pass


def _stage_local(c: AssetCandidate, rec: AssetRecord, policy: AcquisitionPolicy) -> Path:
    """Validate a user-supplied local file WITHOUT touching it (read-only checks). The library copies it."""
    p = Path(c.local_path)
    ext = p.suffix.lower()
    ct = _EXT_TYPE.get(ext)
    if ct is None or ct not in policy.content_types.get(c.media_type, frozenset()):
        raise FetchError("CONTENT_TYPE_NOT_ALLOWED", f"extension {ext or 'missing'!r} is not allowed for {c.media_type.value}")
    try:
        if p.is_symlink() or not p.is_file():
            raise FetchError("LOCAL_FILE_UNUSABLE", "not an existing regular file")
        size = p.stat().st_size
        if size == 0:
            raise FetchError("EMPTY", "file is empty")
        if size > policy.max_bytes:
            raise FetchError("TOO_LARGE", f"{size} bytes exceeds the {policy.max_bytes} byte limit")
        with open(p, "rb") as f:
            head = f.read(32)
    except OSError as e:
        raise FetchError("LOCAL_FILE_UNUSABLE", f"{type(e).__name__}") from None
    if sniff_family(head) != _FAMILY.get(ct):
        raise FetchError("CONTENT_MISMATCH", "file signature does not match its extension")
    return p


def acquire(c: AssetCandidate, lib: AssetLibrary, transport: Transport, ctx: AcquisitionContext,
            ledger: Optional[QuarantineLedger] = None) -> ReportItem:
    """The explicit acquire step for ONE candidate: classify, then record / skip / fetch."""
    try:
        c = sanitize_candidate(c)
    except (ManifestError, AttributeError, TypeError):
        return ReportItem(str(getattr(c, "provider", "?"))[:100], str(getattr(c, "external_id", "?"))[:200], "",
                          "rejected", "", ("MALFORMED_CANDIDATE",), "")
    cl = classify(c, ctx.use, ctx.today)
    if cl.outcome == "reject":
        return _item(c, "rejected", "", cl.reasons)
    if cl.outcome == "quarantine":
        if ledger is not None:
            ledger.record(c, cl.reasons, lib.clock())
        return _item(c, "quarantined", "", cl.reasons, "held as a quarantined candidate; nothing was fetched")
    return _acquire(c, lib, transport, ctx)


def find_and_acquire(request: AssetRequest, registry: ProviderRegistry, library: AssetLibrary, transport: Transport,
                     context: AcquisitionContext) -> AcquisitionReport:
    """Search, triage and acquire. Deterministic: candidates are processed in (provider, external_id) order."""
    report = AcquisitionReport()
    cands, problems = registry.search_all(request, context.max_results)
    report.provider_problems = [scrub(p) for p in problems]
    seen, ordered = set(), []
    for c in sorted(cands, key=lambda c: (c.provider, c.external_id)):
        if (c.provider, c.external_id) not in seen:
            seen.add((c.provider, c.external_id))
            ordered.append(c)
    ledger = QuarantineLedger(library.root)
    for c in ordered:
        item = acquire(c, library, transport, context, ledger)
        report.items.append(item)
        if item.outcome in ("acquired", "duplicate") and item.asset_id and request.project:
            _link_project(library, item.asset_id, request.project)
    if any(i.outcome == "quarantined" for i in report.items):
        ledger.save()
    return report


# --------------------------------------------------------------------------------------------------
# licence drift
# --------------------------------------------------------------------------------------------------
@dataclass
class LicenseCheck:
    asset_id: str
    status: str          # unchanged | drifted | unverifiable | no_snapshot
    detail: str = ""


def reverify_license(library: AssetLibrary, asset_id: str, transport: Transport, policy: AcquisitionPolicy,
                     today: str = "1970-01-01", quarantine_on_unavailable: bool = False) -> LicenseCheck:
    """Re-fetch the licence text recorded at acquisition and compare hashes. A changed text places a hold
    (quarantine, kept through re-evaluation; only release_hold() lifts it) and appends the new snapshot as
    evidence. Nothing is deleted. An unreachable licence page is reported as unverifiable and changes nothing
    unless `quarantine_on_unavailable` is set. Attestations are untouched (they become stale by themselves)."""
    rec = library.get(asset_id)
    snaps = [e for e in rec.evidence if e.kind == "license_page" and e.strength == "document"
             and e.description.startswith((SNAPSHOT_MARK, RECAPTURE_MARK)) and e.sha256 and e.url]
    if not snaps:
        return LicenseCheck(asset_id, "no_snapshot", "no licence snapshot was captured at acquisition")
    snap = snaps[-1]
    try:
        text, digest = fetch_license_text(transport, snap.url, policy)
    except FetchError as e:
        if quarantine_on_unavailable:
            library.quarantine(asset_id, f"licence text could not be re-verified ({e.code})")
        return LicenseCheck(asset_id, "unverifiable", f"{e.code}: {e.message}")
    if digest == snap.sha256:
        return LicenseCheck(asset_id, "unchanged", "licence text is identical to the captured snapshot")
    library.quarantine(asset_id, f"licence text changed since acquisition (was {snap.sha256[:12]}..., now {digest[:12]}...)")
    rel = ""
    try:
        rel = _write_snapshot(library, asset_id, text, digest)
    except (OSError, UnsafePathError):
        rel = ""
    # The new text becomes the baseline for later checks, but the hold stays until a human releases it, and
    # add_evidence() resets verification, so the changed text must be reviewed and re-attested.
    library.add_evidence(asset_id, Evidence(
        "license_page", f"{RECAPTURE_MARK} {today}; verifier {VERIFIER}; sha256 {digest[:12]}...",
        snap.url, rel, digest, parse_date(today, "today"), "", "document"))
    return LicenseCheck(asset_id, "drifted", f"was {snap.sha256[:12]}..., now {digest[:12]}...")
