"""Asset evaluation: relevance, quality metadata, technical compatibility, rights, duplicates.

Everything here works on METADATA supplied by the caller. Nothing opens or analyses media.
Missing metadata yields "unknown", never a pass. Relevance is simple lexical overlap, not
semantic understanding; treat it as a ranking aid.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .models import AssetRecord, Category, MediaType, State, norm_tag
from .rights import QUARANTINE, REJECT, Reason, RightsDecision, UseContext, evaluate_rights

_WORD = re.compile(r"[a-z0-9]+")
STOP = {"a", "an", "the", "of", "and", "or", "to", "in", "on", "for", "with", "is", "are", "at", "by"}
MIN_RELEVANCE = 0.15


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if t not in STOP and len(t) > 1}


@dataclass
class AssetRequest:
    """What the video needs (e.g. built from the Research Agent's asset requests)."""
    project: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    media_types: list[MediaType] = field(default_factory=list)


@dataclass
class TargetSpec:
    """Technical needs of the timeline. All fields optional; unset = no requirement."""
    min_width: Optional[int] = None
    min_height: Optional[int] = None
    allowed_extensions: tuple = ()
    min_sample_rate: Optional[int] = None
    min_duration_s: Optional[float] = None
    max_duration_s: Optional[float] = None


@dataclass
class Relevance:
    score: float
    matched: list[str]
    reasons: list[Reason] = field(default_factory=list)


@dataclass
class Technical:
    status: str                      # compatible | incompatible | unknown
    issues: list[str] = field(default_factory=list)


@dataclass
class AssetEvaluation:
    asset_id: str
    verdict: str                     # usable | review | quarantine | reject | duplicate | unsuitable
    relevance: Relevance
    technical: Technical
    quality_score: Optional[float]
    rights: RightsDecision
    duplicates: list[str]
    reasons: list[Reason]

    @property
    def usable(self) -> bool:
        return self.verdict == "usable"


def relevance(rec: AssetRecord, req: AssetRequest) -> Relevance:
    reasons: list[Reason] = []
    if req.categories and rec.category not in req.categories:
        reasons.append(Reason("CATEGORY_MISMATCH", f"category {rec.category.value} not requested", "reject"))
    if req.media_types and rec.media_type not in req.media_types:
        reasons.append(Reason("MEDIA_TYPE_MISMATCH", f"media type {rec.media_type.value} not requested", "reject"))
    want = _tokens(req.description) | {t for k in req.keywords for t in _tokens(k)}
    have = set(rec.tags) | _tokens(rec.title) | {t for tag in rec.tags for t in _tokens(tag)}
    if not want:
        return Relevance(0.0, [], reasons + [Reason("NO_REQUEST_TERMS", "request has no description/keywords", "info")])
    matched = sorted(want & {norm_tag(h) for h in have})
    score = round(len(matched) / len(want), 3)
    if score < MIN_RELEVANCE:
        reasons.append(Reason("LOW_RELEVANCE", f"only {len(matched)}/{len(want)} request terms matched", "reject"))
    return Relevance(score, matched, reasons)


def technical_compat(rec: AssetRecord, spec: Optional[TargetSpec]) -> Technical:
    """compatible only when at least one requirement was actually checked and every checked one
    passed and none was unrecorded; otherwise unknown (or incompatible)."""
    if spec is None:
        return Technical("unknown", ["no target specification given"])
    t, issues, unknown = rec.technical, [], []
    checked = 0

    def need(key, ok, label):
        nonlocal checked
        v = t.get(key)
        if v is None:
            unknown.append(f"{key} not recorded")
        else:
            checked += 1
            if not ok(v):
                issues.append(f"{label} (got {v})")

    if rec.media_type in (MediaType.VIDEO, MediaType.IMAGE):
        if spec.min_width:
            need("width", lambda v: v >= spec.min_width, f"width below {spec.min_width}")
        if spec.min_height:
            need("height", lambda v: v >= spec.min_height, f"height below {spec.min_height}")
    if rec.media_type is MediaType.AUDIO and spec.min_sample_rate:
        need("sample_rate", lambda v: v >= spec.min_sample_rate, f"sample rate below {spec.min_sample_rate}")
    if rec.media_type is not MediaType.IMAGE:
        if spec.min_duration_s:
            need("duration_s", lambda v: v >= spec.min_duration_s, f"shorter than {spec.min_duration_s}s")
        if spec.max_duration_s:
            need("duration_s", lambda v: v <= spec.max_duration_s, f"longer than {spec.max_duration_s}s")
    if spec.allowed_extensions:
        ext = Path(rec.file_path or "").suffix.lower()
        if not ext:
            unknown.append("file extension unknown")
        else:
            checked += 1
            if ext not in spec.allowed_extensions:
                issues.append(f"extension {ext} not in {list(spec.allowed_extensions)}")
    if issues:
        return Technical("incompatible", issues + unknown)
    if unknown:
        return Technical("unknown", unknown)
    if checked == 0:
        return Technical("unknown", ["no requirement in the target specification applies to this asset"])
    return Technical("compatible", [])


def quality_score(rec: AssetRecord) -> Optional[float]:
    """Mean of the caller-supplied scores that are present; None if none recorded."""
    vals = [rec.quality[k] for k in ("visual_score", "audio_score")
            if isinstance(rec.quality.get(k), (int, float)) and not isinstance(rec.quality.get(k), bool)]
    return round(sum(vals) / len(vals), 3) if vals else None


def find_duplicates(rec: AssetRecord, others: Iterable[AssetRecord]) -> list[str]:
    if not rec.checksum_sha256:
        return []
    return sorted(o.asset_id for o in others
                  if o.asset_id != rec.asset_id and o.checksum_sha256 == rec.checksum_sha256)


def evaluate(rec: AssetRecord, request: Optional[AssetRequest] = None, spec: Optional[TargetSpec] = None,
             ctx: Optional[UseContext] = None, others: Iterable[AssetRecord] = ()) -> AssetEvaluation:
    rights = evaluate_rights(rec, ctx)
    rel = relevance(rec, request) if request else Relevance(0.0, [], [Reason("NO_REQUEST", "no request given", "info")])
    tech = technical_compat(rec, spec)
    dups = find_duplicates(rec, others)
    reasons: list[Reason] = list(rights.reasons) + rel.reasons
    if not rec.checksum_sha256:
        reasons.append(Reason("NO_CHECKSUM", "no checksum; duplicates cannot be detected", "info"))
    if dups:
        reasons.append(Reason("DUPLICATE_CONTENT", f"identical content already indexed: {dups}", "info"))
    if tech.status == "incompatible":
        reasons.append(Reason("TECHNICAL_INCOMPATIBLE", "; ".join(tech.issues), "reject"))
    elif tech.status == "unknown" and spec is not None:
        reasons.append(Reason("TECHNICAL_UNKNOWN", "; ".join(tech.issues), "info"))

    if rights.verdict == REJECT:
        verdict = "reject"
    elif rights.verdict == QUARANTINE:
        verdict = "quarantine"
    elif dups:
        verdict = "duplicate"
    elif tech.status == "incompatible" or any(r.severity == "reject" for r in rel.reasons):
        verdict = "unsuitable"
    elif spec is not None and tech.status != "compatible":
        verdict = "review"          # technical suitability could not be established: not "usable"
    else:
        verdict = "usable"
    # The STORED state is authoritative: a REJECTED / QUARANTINED record (manual hold, integrity failure,
    # unmet rights) is never usable, whatever a fresh rights evaluation says. A bare, never-approved
    # record defaults to QUARANTINED, so it is not usable either.
    held = f" ({rec.state_reason})" if rec.state_reason else ""
    if rec.state is State.REJECTED:
        verdict = "reject"
        reasons.append(Reason("STORED_STATE_REJECTED", "stored state is rejected" + held, "reject"))
    elif rec.state is State.QUARANTINED:
        if verdict != "reject":
            verdict = "quarantine"
        reasons.append(Reason("STORED_STATE_QUARANTINED", "stored state is quarantined" + held, "quarantine"))
    return AssetEvaluation(rec.asset_id, verdict, rel, tech, quality_score(rec), rights, dups, reasons)
