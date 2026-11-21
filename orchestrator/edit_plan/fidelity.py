"""Source-fidelity rules. Every finding here is a hard error: they protect the creator's real words and originals.

* Dialogue comes only from recorded source ranges of the creator; nothing synthetic is ever heard.
* A caption must quote a transcript range of a recorded source (text is checked against the transcript when given).
* Sources are read-only: no output path may equal or lie inside a source directory.
* Exports go only into an explicitly configured allowlist (empty allowlist = every export refused), and never
  under CreatorStudio\\Projects, which Lukas has not approved as an output location.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from . import paths
from .issues import PlanIssue, error, warning
from .schema import EditPlan

PROJECTS_SEQUENCE = ("creatorstudio", "projects")
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


def _norm_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def check_dialogue(plan: EditPlan) -> list[PlanIssue]:
    out: list[PlanIssue] = []
    sources = {s.source_id: s for s in plan.sources}
    for i, seg in enumerate(plan.segments):
        p = f"/segments/{i}"
        src = sources.get(seg.source_id) if seg.source_id is not None else None
        has_audio = seg.media in ("audio", "video_audio")
        if src is not None and src.origin == "generated_speech":
            out.append(error("FIDELITY_GENERATED_SPEECH", f"{p}/source_id",
                             f"source {src.source_id!r} is generated/synthetic speech; it may never appear in a plan"))
        if seg.purpose == "dialogue":
            if not has_audio:
                out.append(error("FIDELITY_DIALOGUE_NO_AUDIO", f"{p}/media",
                                 "a dialogue segment must carry the recorded audio"))
            if src is None:
                out.append(error("FIDELITY_DIALOGUE_NOT_RECORDED", f"{p}/source_id",
                                 "dialogue needs a recorded source range"))
            elif src.origin != "creator_recording":
                out.append(error("FIDELITY_DIALOGUE_NOT_CREATOR", f"{p}/source_id",
                                 f"dialogue must come from a creator recording, source {src.source_id!r} is "
                                 f"{src.origin!r}"))
            if seg.speaker != "creator":
                out.append(error("FIDELITY_DIALOGUE_SPEAKER", f"{p}/speaker",
                                 f"dialogue speaker must be 'creator', got {seg.speaker!r}"))
        elif seg.speaker == "creator" and (src is None or src.origin != "creator_recording"):
            out.append(error("FIDELITY_SPEAKER_ORIGIN_MISMATCH", f"{p}/speaker",
                             "speaker 'creator' is only valid for a creator recording"))
    return out


def check_captions(plan: EditPlan, transcript: Mapping[str, Sequence[Mapping[str, Any]]] | None) -> list[PlanIssue]:
    """`transcript`: source_id -> entries {"start_frame", "end_frame" (half-open source frames), "text"}."""
    out: list[PlanIssue] = []
    sources = {s.source_id: s for s in plan.sources}
    for i, cap in enumerate(plan.captions):
        p = f"/captions/{i}"
        ref = cap.transcript_ref
        if not isinstance(cap.text, str) or not cap.text.strip():
            out.append(error("CAPTION_TEXT_EMPTY", f"{p}/text", "caption text is empty"))
            continue
        if ref is None:
            out.append(error("CAPTION_NO_TRANSCRIPT_REF", f"{p}/transcript_ref",
                             "caption text must quote a transcript range; free text is never allowed"))
            continue
        src = sources.get(ref.source_id)
        if src is None:
            out.append(error("CAPTION_SOURCE_UNKNOWN", f"{p}/transcript_ref/source_id",
                             f"unknown source {ref.source_id!r}"))
            continue
        if src.origin == "generated_speech":
            out.append(error("FIDELITY_GENERATED_SPEECH", f"{p}/transcript_ref/source_id",
                             "a caption may not quote generated speech"))
        if ref.start_frame < 0 or ref.end_frame <= ref.start_frame:
            out.append(error("CAPTION_REF_RANGE_INVALID", f"{p}/transcript_ref",
                             f"transcript range [{ref.start_frame}, {ref.end_frame}) is negative, empty or inverted"))
            continue
        if ref.end_frame > src.duration_frames:
            out.append(error("CAPTION_REF_OUT_OF_SOURCE", f"{p}/transcript_ref",
                             f"transcript range ends at {ref.end_frame}, source has {src.duration_frames} frames"))
            continue
        if transcript is None or ref.source_id not in transcript:
            out.append(warning("CAPTION_UNVERIFIED_NO_TRANSCRIPT", f"{p}/text",
                               "no transcript was supplied for this source: the quote could not be checked"))
            continue
        spoken = _norm_text(" ".join(str(e.get("text", "")) for e in transcript[ref.source_id]
                                     if _overlaps(e, ref.start_frame, ref.end_frame)))
        if _norm_text(cap.text) not in spoken or not spoken:
            out.append(error("CAPTION_NOT_QUOTED", f"{p}/text",
                             "caption text is not a verbatim quote of the referenced transcript range"))
    return out


def _overlaps(entry: Mapping[str, Any], start: int, end: int) -> bool:
    a, b = entry.get("start_frame"), entry.get("end_frame")
    return isinstance(a, int) and isinstance(b, int) and a < end and start < b


def check_exports(plan: EditPlan, allowlist: Iterable[str]) -> list[PlanIssue]:
    out: list[PlanIssue] = []
    roots = [r for r in (paths.parts_of(a) for a in allowlist) if r is not None]
    source_parts = [(s, paths.parts_of(s.path)) for s in plan.sources]
    source_dirs = [(s.source_id, (sp[0], sp[1][:-1])) for s, sp in source_parts if sp is not None and sp[1]]
    seen: set[tuple] = set()
    for i, ex in enumerate(plan.exports):
        p = f"/exports/{i}"
        parts = paths.parts_of(ex.directory)
        if parts is None:
            out.append(error("EXPORT_PATH_INVALID", f"{p}/directory",
                             "the export directory must be an absolute, plain Windows path"))
            continue
        if not isinstance(ex.file_name, str) or not _FILE_NAME.match(ex.file_name) or ".." in ex.file_name \
                or ex.file_name.endswith("."):
            out.append(error("EXPORT_NAME_INVALID", f"{p}/file_name",
                             "file name must be 1-120 characters of letters, digits, _ . - (no path, no '..')"))
        if paths.contains_sequence(parts, PROJECTS_SEQUENCE):
            out.append(error("EXPORT_UNDER_PROJECTS", f"{p}/directory",
                             "exports under CreatorStudio\\Projects are not approved"))
        for sid, sdir in source_dirs:
            if paths.is_inside(parts, sdir):
                out.append(error("EXPORT_INSIDE_SOURCE_DIR", f"{p}/directory",
                                 f"the export directory equals or lies inside the directory of source {sid!r}; "
                                 f"sources are read-only"))
                break
        target = (parts[0], parts[1] + (f"{ex.file_name}.{ex.format}".casefold(),))
        for s, sp in source_parts:
            if sp is not None and sp == target:
                out.append(error("EXPORT_OVERWRITES_SOURCE", f"{p}/file_name",
                                 f"the export would overwrite source {s.source_id!r}"))
        if not roots:
            out.append(error("EXPORT_NOT_ALLOWLISTED", f"{p}/directory",
                             "no export allowlist is configured; every export is refused"))
        elif not any(paths.is_inside(parts, r) for r in roots):
            out.append(error("EXPORT_NOT_ALLOWLISTED", f"{p}/directory",
                             "the export directory is not inside the configured allowlist"))
        key = (parts, ex.file_name.casefold() if isinstance(ex.file_name, str) else None, ex.format)
        if key in seen:
            out.append(error("EXPORT_DUPLICATE", p, "two exports write the same file"))
        seen.add(key)
    return out
