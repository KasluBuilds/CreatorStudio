"""LocalFolderProvider: offline search over a folder of user-supplied files described by sidecar rights files.

For `clip.mp4` the sidecar is `clip.mp4.rights.json`. The sidecar is STRICT and is the user's own claim, never
verification:

    {"schema_version": 1, "title": "...", "category": "broll|image|graphic|music|sfx|meme|generated",
     "rights_basis": "user_owned|public_domain|creative_commons|licensed_other",
     "media_type": "video|image|audio",                       (optional; must agree with the extension)
     "license_name": "CC-BY-4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
     "creator": "...", "source": "...", "source_url": "https://...", "attribution_text": "...",
     "commercial_use_allowed": true|false|null, "modification_allowed": true|false|null,
     "owner_declaration": "I filmed this myself", "tags": ["..."]}

A missing sidecar, unparseable JSON, an unknown key, a wrong type, a missing required key or a user_owned claim
without an owner_declaration all yield a candidate with NO rights claim, which triage quarantines. Nothing is ever
permitted from a sidecar alone, and the originals are only read.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .acquisition import _EXT_TYPE, scrub
from .evaluation import AssetRequest, _tokens
from .models import Category, MediaType, norm_tag
from .providers import AssetCandidate

NAME = "local-folder"
SIDECAR_SUFFIX = ".rights.json"
SIDECAR_SCHEMA_VERSION = 1
MAX_SIDECAR_BYTES = 64_000
ALLOWED_KEYS = {"schema_version", "title", "category", "rights_basis", "media_type", "license_name", "license_url",
                "creator", "source", "source_url", "attribution_text", "commercial_use_allowed",
                "modification_allowed", "owner_declaration", "tags"}
REQUIRED_KEYS = {"schema_version", "title", "category", "rights_basis"}
BASES = ("user_owned", "public_domain", "creative_commons", "licensed_other")
STR_KEYS = ("title", "license_name", "license_url", "creator", "source", "source_url", "attribution_text",
            "owner_declaration")

_MEDIA_OF_PREFIX = {"image": MediaType.IMAGE, "video": MediaType.VIDEO, "audio": MediaType.AUDIO}
_DEFAULT_CATEGORY = {MediaType.IMAGE: Category.IMAGE, MediaType.VIDEO: Category.BROLL, MediaType.AUDIO: Category.SFX}


def media_type_of(path: Path) -> MediaType | None:
    ct = _EXT_TYPE.get(path.suffix.lower())
    return _MEDIA_OF_PREFIX.get(ct.split("/")[0]) if ct else None


def validate_sidecar(raw: Any, media: MediaType) -> tuple[dict, list[str]]:
    """Return (clean values, problems). Any problem means the sidecar makes NO claim."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["sidecar must be a JSON object"]
    extra = sorted(set(raw) - ALLOWED_KEYS)
    if extra:
        problems.append(f"unknown keys {extra}")
    missing = sorted(REQUIRED_KEYS - set(raw))
    if missing:
        problems.append(f"missing required keys {missing}")
    if "schema_version" in raw and (type(raw["schema_version"]) is not int or raw["schema_version"] != SIDECAR_SCHEMA_VERSION):
        problems.append(f"schema_version must be {SIDECAR_SCHEMA_VERSION}")
    for k in STR_KEYS:
        if k in raw and not (isinstance(raw[k], str) and len(raw[k]) <= 1000):
            problems.append(f"{k} must be a string of at most 1000 characters")
    for k in ("commercial_use_allowed", "modification_allowed"):
        if k in raw and raw[k] is not None and not isinstance(raw[k], bool):
            problems.append(f"{k} must be true, false or null")
    if "tags" in raw and not (isinstance(raw["tags"], list) and len(raw["tags"]) <= 50
                              and all(isinstance(t, str) and len(t) <= 64 for t in raw["tags"])):
        problems.append("tags must be a list of at most 50 short strings")
    if raw.get("category") not in [c.value for c in Category]:
        problems.append("category is not valid")
    if raw.get("rights_basis") not in BASES:
        problems.append(f"rights_basis must be one of {BASES}")
    if "media_type" in raw and raw["media_type"] != media.value:
        problems.append("media_type disagrees with the file extension")
    if raw.get("rights_basis") == "user_owned" and not (isinstance(raw.get("owner_declaration"), str) and raw["owner_declaration"].strip()):
        problems.append("user_owned requires a non-empty owner_declaration")
    return ({} if problems else dict(raw)), problems


class LocalFolderProvider:
    """Search provider over a local folder. Offline by construction (it imports no network code)."""
    name = NAME

    def __init__(self, folder: Path | str):
        self.folder = Path(folder)

    def _files(self) -> list[Path]:
        out = []
        for p in sorted(self.folder.rglob("*")):
            try:
                if p.is_symlink() or not p.is_file() or p.name.endswith(SIDECAR_SUFFIX):
                    continue
                if any(a.is_symlink() for a in p.parents if a != self.folder and self.folder in a.parents):
                    continue   # never read through a linked sub-folder
            except (OSError, ValueError):
                continue
            if media_type_of(p) is not None:
                out.append(p)
        return out

    def candidate_for(self, p: Path) -> AssetCandidate:
        media = media_type_of(p)
        rel = p.relative_to(self.folder).as_posix()
        base = dict(provider=NAME, external_id=rel[:200], title=p.stem[:300], category=_DEFAULT_CATEGORY[media],
                    media_type=media, local_path=str(p.resolve()))
        side = p.with_name(p.name + SIDECAR_SUFFIX)
        try:
            if not side.is_file() or side.is_symlink():
                return AssetCandidate(**base, notes="no sidecar rights file: no rights claim")
            if side.stat().st_size > MAX_SIDECAR_BYTES:
                return AssetCandidate(**base, notes="sidecar too large: no rights claim")
            raw = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return AssetCandidate(**base, notes=f"sidecar unreadable ({type(e).__name__}): no rights claim")
        clean, problems = validate_sidecar(raw, media)
        if problems:
            return AssetCandidate(**base, notes=scrub("sidecar invalid, no rights claim: " + "; ".join(problems), 900))
        base.update(title=clean["title"], category=Category(clean["category"]))
        return AssetCandidate(
            **base, source_url=clean.get("source_url", ""), creator=clean.get("creator", ""),
            tags=[norm_tag(t) for t in clean.get("tags", []) if norm_tag(t)],
            claimed_license_name=clean.get("license_name", ""), claimed_license_url=clean.get("license_url", ""),
            claimed_commercial_use=clean.get("commercial_use_allowed"), claimed_modification=clean.get("modification_allowed"),
            claimed_attribution_text=clean.get("attribution_text", ""),
            claimed_basis=clean["rights_basis"] if clean["rights_basis"] == "user_owned" else "",
            owner_declaration=clean.get("owner_declaration", "") if clean["rights_basis"] == "user_owned" else "",
            notes=f"user-declared rights basis: {clean['rights_basis']}")

    def search(self, request: AssetRequest, max_results: int = 10) -> list[AssetCandidate]:
        if not self.folder.is_dir():
            return []
        want = _tokens(request.description) | {t for k in request.keywords for t in _tokens(k)}
        out = []
        for p in self._files():
            c = self.candidate_for(p)
            if request.media_types and c.media_type not in request.media_types:
                continue
            if request.categories and c.category not in request.categories:
                continue
            if want and not (want & (_tokens(c.title) | _tokens(p.stem) | {t for tag in c.tags for t in _tokens(tag)})):
                continue
            out.append(c)
            if len(out) >= max_results:
                break
        return out
