"""Human review layer for transcripts.

The ASR `Transcript` is never edited. A `TranscriptReview` is a separate, write-once record that
says, per segment: "confirmed" (the engine's wording is right) or "corrected" (here is what was
actually said), who reviewed it (`human:<name>`), and which exact transcript it refers to (id +
content hash). `apply_review()` builds a `ReviewedTranscript` VIEW; both the original text and the
reviewed text stay available, and segments nobody reviewed stay "unreviewed".

Review is cumulative by chaining: a new review names the one it supersedes and carries the full
item list (`extend_review` merges). Reviews are never rewritten; `latest_review` picks the newest
in the chain and refuses a forked chain.

Nothing here promotes a transcript by itself: only a TranscriptReview created by a named person does.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .models import MediaAnalysisError, atomic_write_json, read_json, stable_hash, utc_now
from .transcription import Transcript, TranscriptSegment

SCHEMA = "media_analysis.transcript_review/1"


def check_reviewer(name: str | None) -> str:
    if not name or not name.startswith("human:") or not name.split(":", 1)[1].strip():
        raise MediaAnalysisError("reviewer must look like 'human:<name>' (a named person)")
    return name


@dataclass(frozen=True)
class SegmentReview:
    segment_id: str                       # TranscriptSegment.record_id of the ORIGINAL segment
    status: str                           # "confirmed" | "corrected"
    corrected_text: str | None = None     # exactly what the reviewer says was spoken (kept verbatim)
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("confirmed", "corrected"):
            raise MediaAnalysisError("segment review status must be 'confirmed' or 'corrected'")
        if self.status == "corrected" and not (self.corrected_text and self.corrected_text.strip()):
            raise MediaAnalysisError("a correction needs the corrected text")
        if self.status == "confirmed" and self.corrected_text is not None:
            raise MediaAnalysisError("a confirmation carries no corrected text")

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class TranscriptReview:
    transcript_id: str
    original_content_hash: str
    reviewer: str
    items: tuple[SegmentReview, ...]
    previous_review_id: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        check_reviewer(self.reviewer)
        object.__setattr__(self, "items", tuple(self.items))
        if not self.items:
            raise MediaAnalysisError("a review must cover at least one segment")
        ids = [i.segment_id for i in self.items]
        if len(set(ids)) != len(ids):
            raise MediaAnalysisError("a review lists a segment more than once")
        if not self.created_at:
            object.__setattr__(self, "created_at", utc_now())

    @property
    def review_id(self) -> str:
        return "rev_" + stable_hash(self.transcript_id, self.original_content_hash, self.reviewer,
                                    [i.to_dict() for i in self.items], self.previous_review_id)

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "review_id": self.review_id, "transcript_id": self.transcript_id,
                "original_content_hash": self.original_content_hash, "reviewer": self.reviewer,
                "previous_review_id": self.previous_review_id, "created_at": self.created_at,
                "items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, d: Mapping) -> "TranscriptReview":
        if d.get("schema") != SCHEMA:
            raise MediaAnalysisError(f"unrecognised review schema {d.get('schema')!r}")
        r = cls(d["transcript_id"], d["original_content_hash"], d["reviewer"],
                tuple(SegmentReview(**i) for i in d["items"]), d.get("previous_review_id"), d.get("created_at", ""))
        if r.review_id != d.get("review_id"):
            raise MediaAnalysisError("review id mismatch: file edited or corrupt")
        return r


@dataclass(frozen=True)
class ReviewedSegment:
    original: TranscriptSegment           # exactly what the engine returned
    status: str                           # "confirmed" | "corrected" | "unreviewed"
    text: str                             # reviewed wording (== original.text unless corrected/unreviewed)
    note: str = ""

    @property
    def reviewed(self) -> bool:
        return self.status in ("confirmed", "corrected")


@dataclass(frozen=True)
class ReviewedTranscript:
    original: Transcript
    review: TranscriptReview | None
    segments: tuple[ReviewedSegment, ...]

    @property
    def reviewed_segments(self) -> list[ReviewedSegment]:
        return [s for s in self.segments if s.reviewed]


def make_review(transcript: Transcript, reviewer: str, items: Sequence[SegmentReview],
                previous: TranscriptReview | None = None) -> TranscriptReview:
    """Build a review for `transcript`, validating every item against the real segments."""
    by_id = {s.record_id: s for s in transcript.segments}
    for it in items:
        seg = by_id.get(it.segment_id)
        if seg is None:
            raise MediaAnalysisError(f"segment {it.segment_id} is not in transcript {transcript.transcript_id}")
        if it.status == "corrected" and it.corrected_text == seg.text:
            raise MediaAnalysisError(f"correction of {it.segment_id} equals the original text; use 'confirmed'")
    return TranscriptReview(transcript.transcript_id, transcript.content_hash, reviewer, tuple(items),
                            previous.review_id if previous else None)


def extend_review(transcript: Transcript, previous: TranscriptReview, reviewer: str,
                  new_items: Sequence[SegmentReview]) -> TranscriptReview:
    """A new review that keeps the previous items and lets `new_items` override them per segment."""
    if previous.transcript_id != transcript.transcript_id:
        raise MediaAnalysisError("previous review belongs to a different transcript")
    merged = {i.segment_id: i for i in previous.items}
    merged.update({i.segment_id: i for i in new_items})
    return make_review(transcript, reviewer, list(merged.values()), previous)


def apply_review(transcript: Transcript, review: TranscriptReview | None) -> ReviewedTranscript:
    """View of `transcript` with `review` applied. `review=None` -> everything unreviewed.
    The review must belong to exactly this transcript content."""
    if review is None:
        return ReviewedTranscript(transcript, None,
                                  tuple(ReviewedSegment(s, "unreviewed", s.text) for s in transcript.segments))
    if review.transcript_id != transcript.transcript_id:
        raise MediaAnalysisError("review belongs to a different transcript")
    if review.original_content_hash != transcript.content_hash:
        raise MediaAnalysisError("the transcript content differs from the one that was reviewed")
    items = {i.segment_id: i for i in review.items}
    unknown = set(items) - {s.record_id for s in transcript.segments}
    if unknown:
        raise MediaAnalysisError(f"review refers to unknown segments: {sorted(unknown)}")
    out = []
    for s in transcript.segments:
        it = items.get(s.record_id)
        if it is None:
            out.append(ReviewedSegment(s, "unreviewed", s.text))
        else:
            out.append(ReviewedSegment(s, it.status, s.text if it.status == "confirmed" else it.corrected_text,
                                       it.note))
    return ReviewedTranscript(transcript, review, tuple(out))


# ------------------------------------------------------------------- persistence
def save_review(directory: str | os.PathLike, review: TranscriptReview) -> Path:
    """Write-once. The file name carries the review id, so a different review never overwrites this one."""
    path = Path(directory) / f"{review.transcript_id}.review-{review.review_id}.json"
    if path.exists():
        if TranscriptReview.from_dict(read_json(path)) != review:
            raise MediaAnalysisError(f"{path.name} exists with different content; refusing to overwrite")
        return path
    atomic_write_json(path, review.to_dict())
    return path


def load_review(path: str | os.PathLike) -> TranscriptReview:
    return TranscriptReview.from_dict(read_json(path))


def latest_review(directory: str | os.PathLike, transcript_id: str) -> TranscriptReview | None:
    """The newest review in the chain for a transcript; None if there is none; error if the chain forks."""
    reviews = [load_review(p) for p in sorted(Path(directory).glob(f"{transcript_id}.review-*.json"))]
    if not reviews:
        return None
    superseded = {r.previous_review_id for r in reviews if r.previous_review_id}
    heads = [r for r in reviews if r.review_id not in superseded]
    if len(heads) != 1:
        raise MediaAnalysisError(f"review chain for {transcript_id} forks into {len(heads)} heads; resolve manually")
    return heads[0]
