"""Exact frame and time arithmetic for Resolve operations.

Everything here is integer or `fractions.Fraction`; no float is ever used to decide a frame. Conventions (the
whole package follows them, and RESOLVE_INTEGRATION.md section "Frame semantics" records the live evidence):

* A frame range is **half-open**: `[start, end)`. `end` is the first frame NOT included, so the length is
  `end - start` and a one-frame range is `[n, n + 1)`. The last included frame is `end - 1`.
* Resolve's `MediaPool.AppendToTimeline` clipInfo `startFrame`/`endFrame` are **source** frames in the same
  half-open convention, counted from the first frame of the media file (frame 0), not from its timecode.
* Timeline positions (`recordFrame`, `TimelineItem.GetStart()/GetEnd()`) are **absolute** timeline frames that
  include the timeline start frame (for example 90000 for a 01:00:00:00 start at 25 fps). `GetEnd()` is exclusive.
* When source and timeline rates differ, a source range of `n` frames occupies `floor(n * timeline_rate /
  source_rate)` timeline frames (observed behaviour, see `RESOLVE_INTEGRATION.md`; never a hard-coded correction).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Union

__all__ = [
    "FrameRate", "FrameRange", "TimebaseError", "parse_rate", "rescale_floor", "rescale_ceil",
    "source_frames_for_duration", "timeline_duration_for_source", "frames_to_seconds", "seconds_to_frames",
    "frames_to_timecode", "timecode_to_frames", "COMMON_RATES",
]


class TimebaseError(ValueError):
    """An invalid rate, range or conversion. Always explicit, never silently clamped."""


RateLike = Union["FrameRate", int, str, Fraction]

# Resolve's project/timeline frame-rate strings and the exact rational each stands for.
COMMON_RATES: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001), "24": Fraction(24), "25": Fraction(25), "29.97": Fraction(30000, 1001),
    "30": Fraction(30), "47.952": Fraction(48000, 1001), "48": Fraction(48), "50": Fraction(50),
    "59.94": Fraction(60000, 1001), "60": Fraction(60), "95.904": Fraction(96000, 1001), "96": Fraction(96),
    "100": Fraction(100), "119.88": Fraction(120000, 1001), "120": Fraction(120),
}

_RATE_TEXT = re.compile(r"^\s*(\d{1,4})(?:\.(\d{1,6}))?\s*$")
_RATIO_TEXT = re.compile(r"^\s*(\d{1,7})\s*/\s*(\d{1,7})\s*$")


@dataclass(frozen=True, order=True)
class FrameRate:
    """An exact frame rate in frames per second."""

    value: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.value, Fraction):
            raise TimebaseError("FrameRate.value must be a Fraction; use parse_rate()")
        if self.value <= 0 or self.value > 1000:
            raise TimebaseError(f"frame rate {self.value} is outside (0, 1000]")

    @property
    def is_integer(self) -> bool:
        return self.value.denominator == 1

    @property
    def nominal(self) -> int:
        """The integer timecode base (24 for 23.976, 30 for 29.97)."""
        return round(self.value)

    def resolve_text(self) -> str:
        """The string Resolve uses in project/timeline settings ("25", "29.97", "23.976")."""
        for text, exact in COMMON_RATES.items():
            if exact == self.value:
                return text
        if self.is_integer:
            return str(self.value.numerator)
        raise TimebaseError(f"frame rate {self.value} has no Resolve settings string")

    def __str__(self) -> str:
        return self.resolve_text() if (self.is_integer or self.value in COMMON_RATES.values()) else str(self.value)


def parse_rate(rate: RateLike) -> FrameRate:
    """Parse a rate without floats: 25, "25", "29.97", "30000/1001", Fraction(24000, 1001) or a FrameRate.

    Decimal NTSC strings map to the exact x/1001 rational; any other decimal is taken literally ("12.5" -> 25/2).
    A float is refused because its binary value is not the rate the caller meant.
    """
    if isinstance(rate, FrameRate):
        return rate
    if isinstance(rate, bool) or isinstance(rate, float):
        raise TimebaseError(f"frame rate must not be a {type(rate).__name__}: pass an int, string or Fraction")
    if isinstance(rate, int):
        return FrameRate(Fraction(rate))
    if isinstance(rate, Fraction):
        return FrameRate(rate)
    if isinstance(rate, str):
        text = rate.strip()
        if text in COMMON_RATES:
            return FrameRate(COMMON_RATES[text])
        m = _RATIO_TEXT.match(text)
        if m:
            den = int(m.group(2))
            if den == 0:
                raise TimebaseError("frame rate denominator is zero")
            return FrameRate(Fraction(int(m.group(1)), den))
        m = _RATE_TEXT.match(text)
        if m:
            whole, frac = m.group(1), m.group(2) or ""
            exact = Fraction(int(whole + frac), 10 ** len(frac))
            # Resolve and ffprobe report NTSC rates rounded; snap to the exact x/1001 rational when it is clearly meant.
            for nominal in (24, 30, 48, 60, 96, 120):
                ntsc = Fraction(nominal * 1000, 1001)
                if frac and abs(exact - ntsc) < Fraction(1, 200):
                    return FrameRate(ntsc)
            return FrameRate(exact)
    raise TimebaseError(f"unparseable frame rate {rate!r}")


@dataclass(frozen=True)
class FrameRange:
    """A half-open frame range `[start, end)` in one stated timebase."""

    start: int
    end: int

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int):
                raise TimebaseError(f"FrameRange.{name} must be an int, got {type(v).__name__}")
        if self.start < 0:
            raise TimebaseError(f"FrameRange.start {self.start} is negative")
        if self.end <= self.start:
            raise TimebaseError(f"empty or inverted range [{self.start}, {self.end}): end must be > start")

    @classmethod
    def from_inclusive(cls, first: int, last: int) -> "FrameRange":
        """Build from an inclusive first/last frame pair (as a human reads an In/Out mark)."""
        if isinstance(last, bool) or not isinstance(last, int):
            raise TimebaseError("last frame must be an int")
        return cls(first, last + 1)

    @classmethod
    def from_length(cls, start: int, length: int) -> "FrameRange":
        if isinstance(length, bool) or not isinstance(length, int) or length < 1:
            raise TimebaseError(f"length must be a positive int, got {length!r}")
        return cls(start, start + length)

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def last(self) -> int:
        """The last included frame."""
        return self.end - 1

    def contains(self, frame: int) -> bool:
        return self.start <= frame < self.end

    def overlaps(self, other: "FrameRange") -> bool:
        return self.start < other.end and other.start < self.end

    def shifted(self, offset: int) -> "FrameRange":
        return FrameRange(self.start + offset, self.end + offset)

    def within(self, total_frames: int) -> bool:
        """True when the range lies inside a media of `total_frames` frames (frames 0 .. total_frames - 1)."""
        return self.end <= total_frames


def _ratio(src: RateLike, dst: RateLike) -> Fraction:
    return parse_rate(dst).value / parse_rate(src).value


def rescale_floor(frames: int, src: RateLike, dst: RateLike) -> int:
    """`frames` in `src` rate expressed in whole `dst` frames, rounded down (exact rational arithmetic)."""
    _check_count(frames)
    q = frames * _ratio(src, dst)
    return q.numerator // q.denominator


def rescale_ceil(frames: int, src: RateLike, dst: RateLike) -> int:
    _check_count(frames)
    q = frames * _ratio(src, dst)
    return -((-q.numerator) // q.denominator)


def timeline_duration_for_source(source_frames: int, source_rate: RateLike, timeline_rate: RateLike) -> int:
    """Timeline frames a source range of `source_frames` occupies when appended (floor; see module docstring)."""
    return rescale_floor(source_frames, source_rate, timeline_rate)


def source_frames_for_duration(timeline_frames: int, source_rate: RateLike, timeline_rate: RateLike) -> int:
    """The smallest source length whose appended duration is at least `timeline_frames` timeline frames.

    With floor conversion, `ceil(t * src / tl)` source frames always give `>= t` timeline frames; the result is checked
    so a caller can rely on `timeline_duration_for_source(result) >= timeline_frames`.
    """
    _check_count(timeline_frames)
    n = rescale_ceil(timeline_frames, timeline_rate, source_rate)
    while timeline_duration_for_source(n, source_rate, timeline_rate) < timeline_frames:  # pragma: no cover - guard
        n += 1
    return n


def frames_to_seconds(frames: int, rate: RateLike) -> Fraction:
    if isinstance(frames, bool) or not isinstance(frames, int):
        raise TimebaseError("frames must be an int")
    return Fraction(frames) / parse_rate(rate).value


def seconds_to_frames(seconds: Union[Fraction, int, str], rate: RateLike, rounding: str = "exact") -> int:
    """Convert exact seconds to frames. `rounding` is "exact" (refuse a fractional result), "floor", "ceil" or
    "nearest" (half up). Floats are refused; pass a Fraction or a decimal string such as "1.04"."""
    if isinstance(seconds, (bool, float)):
        raise TimebaseError("seconds must be a Fraction, int or decimal string, not a float")
    sec = Fraction(seconds) if not isinstance(seconds, Fraction) else seconds
    q = sec * parse_rate(rate).value
    if rounding == "exact":
        if q.denominator != 1:
            raise TimebaseError(f"{seconds} s is not a whole number of frames at {parse_rate(rate)} fps")
        return q.numerator
    if rounding == "floor":
        return q.numerator // q.denominator
    if rounding == "ceil":
        return -((-q.numerator) // q.denominator)
    if rounding == "nearest":
        return (2 * q.numerator + q.denominator) // (2 * q.denominator)
    raise TimebaseError(f"unknown rounding {rounding!r}")


def _check_count(frames: int) -> None:
    if isinstance(frames, bool) or not isinstance(frames, int) or frames < 0:
        raise TimebaseError(f"frame count must be a non-negative int, got {frames!r}")


_TC = re.compile(r"^(\d{2}):(\d{2}):(\d{2})([:;])(\d{2})$")


def frames_to_timecode(frames: int, rate: RateLike) -> str:
    """Non-drop-frame timecode for an absolute frame count. Drop-frame is refused explicitly (not implemented)."""
    _check_count(frames)
    fps = parse_rate(rate).nominal
    ff = frames % fps
    total_seconds = frames // fps
    return f"{total_seconds // 3600:02d}:{total_seconds // 60 % 60:02d}:{total_seconds % 60:02d}:{ff:02d}"


def timecode_to_frames(tc: str, rate: RateLike) -> int:
    """Parse non-drop "HH:MM:SS:FF". A drop-frame separator (';') is refused rather than miscounted."""
    m = _TC.match(tc or "")
    if not m:
        raise TimebaseError(f"bad timecode {tc!r}; expected HH:MM:SS:FF")
    if m.group(4) == ";":
        raise TimebaseError("drop-frame timecode is not supported by this package")
    hh, mm, ss, ff = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(5))
    fps = parse_rate(rate).nominal
    if mm > 59 or ss > 59 or ff >= fps:
        raise TimebaseError(f"timecode {tc} is out of range at {fps} fps")
    return ((hh * 60 + mm) * 60 + ss) * fps + ff
