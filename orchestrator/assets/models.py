"""Asset metadata schema, strict (de)serialisation and path/identifier safety.

Design rules enforced here:
  * Unknown is a first-class value. Rights flags are tri-state (True / False / None=unknown) and
    None is never treated as permission anywhere in this package.
  * Manifests are untrusted input: from_dict() is strict (unknown keys, wrong types, bad enums,
    bad checksums, bad dates all raise ManifestError) so a damaged manifest can never silently
    become a permissive record.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional

SCHEMA_VERSION = 1

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCOPE_RE = re.compile(r"^(global|project:[a-z0-9][a-z0-9_-]{0,63})$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

MAX_TAGS = 50
MAX_TAG_LEN = 64
MAX_TEXT = 2000
MAX_LIST = 100


class AssetError(Exception):
    """Base class for asset-subsystem errors."""


class ManifestError(AssetError):
    """A manifest / record / index is malformed. Carries the list of problems."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class UnsafePathError(AssetError):
    """A path or identifier would escape the library or is otherwise unsafe."""


class AssetExistsError(AssetError):
    """An asset with this id (or destination file) already exists; nothing was overwritten."""


class DuplicateAssetError(AssetError):
    """Another asset with identical content (checksum) is already indexed."""

    def __init__(self, existing_id: str):
        self.existing_id = existing_id
        super().__init__(f"identical content already indexed as {existing_id!r}")


class Category(str, Enum):
    BROLL = "broll"
    IMAGE = "image"
    GRAPHIC = "graphic"
    MUSIC = "music"
    SFX = "sfx"
    MEME = "meme"
    GENERATED = "generated"


class MediaType(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class RightsBasis(str, Enum):
    PUBLIC_DOMAIN = "public_domain"
    CREATIVE_COMMONS = "creative_commons"
    USER_OWNED = "user_owned"
    GENERATED = "generated"
    LICENSED_OTHER = "licensed_other"
    UNKNOWN = "unknown"


class Verification(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"      # only ever set by attest(): a named human/user attestation
    DISPUTED = "disputed"


class State(str, Enum):
    APPROVED = "approved"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class Watermark(str, Enum):
    CLEAR = "clear"            # someone checked and found none (still not proof of permission)
    WATERMARKED = "watermarked"
    SUSPECTED = "suspected"
    UNCHECKED = "unchecked"


EVIDENCE_KINDS = ("license_page", "receipt", "screenshot", "provider_terms", "owner_declaration",
                  "provider_claim", "other")
ATTEST_METHODS = ("human_review", "user_declaration")   # no "automated" method exists on purpose


def clean_text(value: Any, field_name: str, max_len: int = MAX_TEXT, problems: Optional[list] = None) -> str:
    """Single-line-safe text: str only, no control characters (except space), length capped."""
    if value is None:
        return ""
    if not isinstance(value, str):
        _p(problems, f"{field_name}: must be a string")
        return ""
    if any(unicodedata.category(c).startswith("C") and c not in "\n\t" for c in value):
        _p(problems, f"{field_name}: contains control characters")
        return ""
    value = value.replace("\n", " ").replace("\t", " ").strip()
    if len(value) > max_len:
        _p(problems, f"{field_name}: longer than {max_len} characters")
        return value[:max_len]
    return value


def _p(problems: Optional[list], msg: str) -> None:
    if problems is None:
        raise ManifestError(msg)
    problems.append(msg)


def norm_tag(tag: str) -> str:
    return re.sub(r"\s+", " ", tag.strip().lower())


# Windows device names: a file called con.mp4 / nul.mp4 is not an ordinary file there.
RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(10)} | {f"lpt{i}" for i in range(10)}


def check_asset_id(asset_id: Any) -> str:
    if not isinstance(asset_id, str) or not ID_RE.fullmatch(asset_id):
        raise UnsafePathError("asset_id must match [a-z0-9][a-z0-9_-]{0,63}")
    if asset_id in RESERVED_NAMES:
        raise UnsafePathError("asset_id is a reserved Windows device name")
    return asset_id


def path_problem(p: Optional[str], managed: bool) -> Optional[str]:
    """Validate a path stored in a manifest. Managed copies must be library-relative under
    files/ with no traversal; references must be absolute local paths (no UNC/device paths,
    no alternate data streams, no '..')."""
    if p is None:
        return None
    if not p or len(p) > 1000 or any(ord(c) < 32 for c in p):
        return "file_path: empty, too long or contains control characters"
    if p.startswith(("\\\\", "//")):
        return "file_path: UNC / device paths are not accepted"
    win, posix = PureWindowsPath(p), PurePosixPath(p)
    if managed:
        if win.is_absolute() or posix.is_absolute() or win.drive or win.root or "\\" in p or ":" in p:
            return "file_path: managed copy path must be relative"
        parts = p.split("/")
        if any(x in ("", ".", "..") for x in parts) or parts[0] != "files":
            return "file_path: managed copy path must be files/<...> without traversal"
        return None
    if not (win.is_absolute() or posix.is_absolute()):
        return "file_path: referenced original must be an absolute path"
    if win.drive and ":" in p[2:]:
        return "file_path: alternate data streams are not accepted"
    if ".." in win.parts or ".." in posix.parts:
        return "file_path: traversal ('..') is not accepted"
    return None


def is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def parse_date(value: Any, field_name: str, problems: Optional[list] = None) -> str:
    if value in (None, ""):
        return ""
    try:
        if not isinstance(value, str):
            raise ValueError
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        _p(problems, f"{field_name}: must be an ISO date (YYYY-MM-DD)")
        return ""


@dataclass
class Evidence:
    """Something that supports a rights claim. Never deleted or edited; only appended."""
    kind: str
    description: str
    url: str = ""
    local_path: str = ""
    sha256: str = ""
    captured_at: str = ""
    license_name: str = ""     # the licence this evidence actually shows (checked against the record)
    # "claim" = a statement by the provider/uploader (NOT verification); "document" = a captured
    # first-party license text/receipt/terms page; "declaration" = the user's own statement.
    strength: str = "claim"

    @classmethod
    def from_dict(cls, d: Any, problems: list, prefix: str = "evidence") -> "Evidence":
        if not isinstance(d, dict):
            problems.append(f"{prefix}: must be an object")
            return cls("other", "")
        extra = set(d) - {f.name for f in dataclasses.fields(cls)}
        if extra:
            problems.append(f"{prefix}: unknown keys {sorted(extra)}")
        kind = d.get("kind")
        if kind not in EVIDENCE_KINDS:
            problems.append(f"{prefix}.kind: must be one of {EVIDENCE_KINDS}")
            kind = "other"
        strength = d.get("strength", "claim")
        if strength not in ("claim", "document", "declaration"):
            problems.append(f"{prefix}.strength: invalid")
            strength = "claim"
        sha = d.get("sha256", "") or ""
        if sha and not (isinstance(sha, str) and SHA256_RE.fullmatch(sha)):
            problems.append(f"{prefix}.sha256: invalid")
            sha = ""
        desc = clean_text(d.get("description"), f"{prefix}.description", problems=problems)
        if not desc:
            problems.append(f"{prefix}.description: required")
        return cls(kind, desc, clean_text(d.get("url"), f"{prefix}.url", 500, problems),
                   clean_text(d.get("local_path"), f"{prefix}.local_path", 500, problems), sha,
                   parse_date(d.get("captured_at"), f"{prefix}.captured_at", problems),
                   clean_text(d.get("license_name"), f"{prefix}.license_name", 200, problems), strength)


@dataclass
class Attestation:
    """A named person confirmed the rights facts. Recorded, never inferred."""
    verifier: str
    method: str
    note: str
    at: str
    fingerprint: str = ""   # hash of the rights facts that were attested; stale if they change


@dataclass
class AssetRecord:
    asset_id: str
    title: str
    category: Category
    media_type: MediaType
    file_path: Optional[str] = None          # absolute (referenced) or library-relative (managed copy)
    managed_copy: bool = False
    tags: list[str] = field(default_factory=list)
    source: str = ""                         # site / provider / "creator"
    source_url: str = ""
    creator: str = ""
    rights_basis: RightsBasis = RightsBasis.UNKNOWN
    license_name: str = ""
    license_url: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    allowed_uses: list[str] = field(default_factory=list)
    restrictions: list[str] = field(default_factory=list)
    attribution_required: Optional[bool] = None
    attribution_text: str = ""
    commercial_use_allowed: Optional[bool] = None
    modification_allowed: Optional[bool] = None
    acquisition_date: str = ""
    checksum_sha256: str = ""
    file_size: Optional[int] = None
    verification_status: Verification = Verification.UNVERIFIED
    attestations: list[Attestation] = field(default_factory=list)
    watermark_status: Watermark = Watermark.UNCHECKED
    is_preview: bool = False
    scope: str = "global"
    projects: list[str] = field(default_factory=list)
    technical: dict = field(default_factory=dict)   # caller-supplied: width,height,fps,duration_s,sample_rate,channels,codec
    quality: dict = field(default_factory=dict)     # caller-supplied: visual_score,audio_score (0..1), notes
    state: State = State.QUARANTINED                 # default is the safe state
    state_reason: str = ""
    history: list[dict] = field(default_factory=list)
    notes: str = ""

    # ---- serialisation -------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        for k in ("category", "media_type", "rights_basis", "verification_status", "watermark_status", "state"):
            d[k] = getattr(self, k).value
        d["schema_version"] = SCHEMA_VERSION
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "AssetRecord":
        p: list[str] = []
        if not isinstance(d, dict):
            raise ManifestError("record must be a JSON object")
        d = dict(d)
        ver = d.pop("schema_version", None)
        if type(ver) is not int or ver != SCHEMA_VERSION:   # `True == 1` must not pass
            p.append(f"schema_version: expected {SCHEMA_VERSION}, got {ver!r}")
        names = {f.name for f in dataclasses.fields(cls)}
        extra = set(d) - names
        if extra:
            p.append(f"unknown keys {sorted(extra)}")

        def enum(cls_, key, default=None):
            v = d.get(key, default)
            try:
                return cls_(v)
            except ValueError:
                p.append(f"{key}: invalid value {v!r}")
                return default if default is not None else list(cls_)[0]

        def tri(key):
            v = d.get(key)
            if v is not None and not isinstance(v, bool):
                p.append(f"{key}: must be true, false or null")
                return None
            return v

        def strlist(key, maxn=MAX_LIST, norm=False, maxlen=MAX_TEXT):
            v = d.get(key, [])
            if not isinstance(v, list) or len(v) > maxn:
                p.append(f"{key}: must be a list of at most {maxn} strings")
                return []
            out = []
            for i, x in enumerate(v):
                if not isinstance(x, str):
                    p.append(f"{key}[{i}]: must be a string")
                    continue
                s = clean_text(x, f"{key}[{i}]", maxlen, p)
                if norm:
                    s = norm_tag(s)
                if s and s not in out:
                    out.append(s)
            return out

        for req in ("asset_id", "title", "category", "media_type"):
            if req not in d:
                p.append(f"{req}: required")
        asset_id = d.get("asset_id")
        if not isinstance(asset_id, str) or not ID_RE.fullmatch(asset_id) or asset_id in RESERVED_NAMES:
            p.append("asset_id: invalid")
            asset_id = "invalid"
        checksum = d.get("checksum_sha256", "") or ""
        if checksum and not (isinstance(checksum, str) and SHA256_RE.fullmatch(checksum)):
            p.append("checksum_sha256: must be 64 lowercase hex characters")
            checksum = ""
        scope = d.get("scope", "global")
        if not (isinstance(scope, str) and SCOPE_RE.fullmatch(scope)):
            p.append("scope: must be 'global' or 'project:<slug>'")
            scope = "global"
        projects = strlist("projects", maxlen=64)
        for pr in projects:
            if not PROJECT_RE.fullmatch(pr):
                p.append(f"projects: invalid slug {pr!r}")
        size = d.get("file_size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            p.append("file_size: must be a non-negative integer or null")
            size = None
        for key in ("technical", "quality"):
            if not isinstance(d.get(key, {}), dict):
                p.append(f"{key}: must be an object")
        for key in ("managed_copy", "is_preview"):
            if not isinstance(d.get(key, False), bool):
                p.append(f"{key}: must be boolean")
        ev = d.get("evidence", [])
        if isinstance(ev, list) and len(ev) > MAX_LIST:
            p.append("evidence: too many items")
            ev = []
        evidence = [Evidence.from_dict(e, p, f"evidence[{i}]") for i, e in enumerate(ev)] if isinstance(ev, list) else []
        if not isinstance(ev, list):
            p.append("evidence: must be a list")
        att_raw = d.get("attestations", [])
        attestations = []
        if not isinstance(att_raw, list):
            p.append("attestations: must be a list")
        else:
            for i, a in enumerate(att_raw):
                if not isinstance(a, dict) or set(a) != {"verifier", "method", "note", "at", "fingerprint"} \
                        or a.get("method") not in ATTEST_METHODS or not isinstance(a.get("verifier"), str) \
                        or not a.get("verifier") or not isinstance(a.get("at"), str) \
                        or not isinstance(a.get("note"), str) or not isinstance(a.get("fingerprint"), str) \
                        or not (a["fingerprint"] == "" or SHA256_RE.fullmatch(a["fingerprint"])):
                    p.append(f"attestations[{i}]: invalid")
                    continue
                attestations.append(Attestation(clean_text(a["verifier"], "verifier", 200, p), a["method"],
                                                clean_text(a["note"], "note", MAX_TEXT, p), a["at"][:40],
                                                a["fingerprint"]))
            if len(att_raw) > MAX_LIST:
                p.append("attestations: too many")
        hist = d.get("history", [])
        if not isinstance(hist, list) or not all(isinstance(h, dict) for h in hist):
            p.append("history: must be a list of objects")
            hist = []
        fp = d.get("file_path")
        if fp is not None and not isinstance(fp, str):
            p.append("file_path: must be a string or null")
            fp = None
        elif isinstance(d.get("managed_copy", False), bool):
            pp = path_problem(fp, bool(d.get("managed_copy", False)))
            if pp:
                p.append(pp)
                fp = None
        rec = cls(
            asset_id=asset_id,
            title=clean_text(d.get("title"), "title", 300, p),
            category=enum(Category, "category"),
            media_type=enum(MediaType, "media_type"),
            file_path=fp,
            managed_copy=bool(d.get("managed_copy", False)),
            tags=strlist("tags", MAX_TAGS, True, MAX_TAG_LEN),
            source=clean_text(d.get("source"), "source", 300, p),
            source_url=clean_text(d.get("source_url"), "source_url", 500, p),
            creator=clean_text(d.get("creator"), "creator", 300, p),
            rights_basis=enum(RightsBasis, "rights_basis", RightsBasis.UNKNOWN),
            license_name=clean_text(d.get("license_name"), "license_name", 200, p),
            license_url=clean_text(d.get("license_url"), "license_url", 500, p),
            evidence=evidence,
            allowed_uses=strlist("allowed_uses", maxlen=100),
            restrictions=strlist("restrictions", maxlen=200),
            attribution_required=tri("attribution_required"),
            attribution_text=clean_text(d.get("attribution_text"), "attribution_text", 1000, p),
            commercial_use_allowed=tri("commercial_use_allowed"),
            modification_allowed=tri("modification_allowed"),
            acquisition_date=parse_date(d.get("acquisition_date"), "acquisition_date", p),
            checksum_sha256=checksum,
            file_size=size,
            verification_status=enum(Verification, "verification_status", Verification.UNVERIFIED),
            attestations=attestations,
            watermark_status=enum(Watermark, "watermark_status", Watermark.UNCHECKED),
            is_preview=bool(d.get("is_preview", False)),
            scope=scope,
            projects=projects,
            technical=dict(d.get("technical", {})) if isinstance(d.get("technical", {}), dict) else {},
            quality=dict(d.get("quality", {})) if isinstance(d.get("quality", {}), dict) else {},
            state=enum(State, "state", State.QUARANTINED),
            state_reason=clean_text(d.get("state_reason"), "state_reason", MAX_TEXT, p),
            history=hist,
            notes=clean_text(d.get("notes"), "notes", MAX_TEXT, p),
        )
        p.extend(rec.validate(structural_only=True))
        if rec.verification_status is Verification.VERIFIED and not rec.attestations:
            p.append("verification_status: 'verified' requires at least one attestation")
        if p:
            raise ManifestError(p)
        return rec

    def validate(self, structural_only: bool = False) -> list[str]:
        """Return problems (empty = structurally valid). Does not judge rights."""
        p: list[str] = []
        if not isinstance(self.asset_id, str) or not ID_RE.fullmatch(self.asset_id) or self.asset_id in RESERVED_NAMES:
            p.append("asset_id: invalid")
        if not self.title:
            p.append("title: required")
        for q in self.quality_problems():
            p.append(q)
        for k in ("width", "height", "channels", "sample_rate"):
            v = self.technical.get(k)
            if v is not None and (not is_num(v) or v <= 0):
                p.append(f"technical.{k}: must be a positive number")
        for k in ("fps", "duration_s"):
            v = self.technical.get(k)
            if v is not None and (not is_num(v) or v <= 0):
                p.append(f"technical.{k}: must be a positive number")
        if self.scope != "global" and not SCOPE_RE.fullmatch(self.scope):
            p.append("scope: invalid")
        return p

    def quality_problems(self) -> list[str]:
        out = []
        for k in ("visual_score", "audio_score"):
            v = self.quality.get(k)
            if v is not None and (not is_num(v) or not 0 <= v <= 1):
                out.append(f"quality.{k}: must be a number in [0, 1]")
        return out


def safe_suffix(name: str) -> str:
    """Extension for a managed copy: short, alphanumeric only (no dots-in-dots, no ADS ':')."""
    suf = Path(name).suffix.lower()
    return suf if re.fullmatch(r"\.[a-z0-9]{1,8}", suf) else ""
