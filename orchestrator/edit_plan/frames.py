"""Exact frame helpers shared by validation and the executor mapping. Integers and Fractions only.

Rate and rescale arithmetic is delegated to resolve_executor.timebase (the same rules the executor applies), so a
plan cannot be validated under one rule and executed under another.
"""
from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

try:  # the package is imported as `edit_plan` with orchestrator/ on sys.path (as the other packages are)
    from resolve_executor.timebase import (FrameRate, TimebaseError, seconds_to_frames, timecode_to_frames,
                                           timeline_duration_for_source)
except ImportError:  # pragma: no cover - direct execution from another working directory
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from resolve_executor.timebase import (FrameRate, TimebaseError, seconds_to_frames, timecode_to_frames,
                                           timeline_duration_for_source)

from .schema import Rate, Segment

__all__ = ["FrameRate", "TimebaseError", "seconds_to_frames", "to_rate", "start_frame", "half_open", "source_length",
           "timeline_duration", "is_exact", "seconds_text"]


def to_rate(r: Rate) -> FrameRate:
    """Exact FrameRate from a Rate; raises TimebaseError for zero/negative/absurd values."""
    if isinstance(r.num, bool) or isinstance(r.den, bool) or r.den == 0:
        raise TimebaseError("frame rate denominator is zero or not an int")
    return FrameRate(Fraction(r.num, r.den))


def start_frame(start_timecode: str, rate: FrameRate) -> int:
    return timecode_to_frames(start_timecode, rate)


def half_open(seg: Segment) -> tuple[int, int]:
    """Source range as half-open `[first, end)` according to the segment's stated convention."""
    if seg.source_in is None or seg.source_out is None or seg.range_convention is None:
        raise ValueError("segment has no complete source range")
    return seg.source_in, seg.source_out + (1 if seg.range_convention == "inclusive_out" else 0)


def source_length(seg: Segment) -> int:
    a, b = half_open(seg)
    return b - a


def timeline_duration(length: int, src: FrameRate, tl: FrameRate) -> int:
    """Timeline frames occupied by `length` source frames (floor, as Resolve does)."""
    return timeline_duration_for_source(length, src, tl)


def is_exact(length: int, src: FrameRate, tl: FrameRate) -> bool:
    return (length * tl.value / src.value).denominator == 1


def seconds_text(frames: int, rate: FrameRate) -> str:
    """Exact seconds as 'n' or 'n/d' (never a float)."""
    return str(Fraction(frames) / rate.value)
