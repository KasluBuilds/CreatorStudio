"""Shot-detection metrics layer: pure parsing, timing arithmetic and candidate grouping.

Nothing here runs a process or reads media. Callers run FFmpeg (using the argument lists built
here), then hand the captured text to the parsers. Parsers never guess: they return a status
that separates "no data" / "malformed" from a valid result that happens to contain no events.

FFmpeg 9.0.1 output formats (captured on tiny synthetic clips; fixtures live in tests/test_shots.py):

* ``metadata=mode=print:file=PATH`` writes, per frame::

      frame:10   pts:400     pts_time:0.4
      lavfi.scd.mafd=25.391
      lavfi.scd.score=25.391
      lavfi.scd.time=0.4          <- only on frames whose score >= the scdet threshold

  ``frame:N`` is the output frame counter (0-based, exact). ``pts`` is in stream time-base units
  and ``pts_time`` is that value in seconds, rounded to the stream time base (1 ms for MKV), so
  ``pts_time`` is NOT exact for rates such as 30000/1001. Frame numbers are therefore taken
  from ``frame:N`` and times are computed from the rational frame rate.
* scdet attributes a change to the first frame of the NEW content. With ``sc_pass=1`` only the
  detected frames are passed on (so metadata=print would see just those); keep it at 0.
* ``blackdetect`` (log level info) prints ``black_start:S black_end:E black_duration:D``; for
  black that runs to the end of the stream the log's end is the last frame's time. In metadata
  form only ``lavfi.black_start`` / ``lavfi.black_end`` appear and a trailing black run has no
  end key at all.
* ``signalstats`` adds ``lavfi.signalstats.YAVG``, ``YDIF`` (mean luma change from the previous
  frame), ``SATAVG``, ``HUEMED`` and others.

Status: parser formats verified on FFmpeg 9.0.1 only. Thresholds used elsewhere are untuned
heuristics; nothing here has been validated on real footage.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "MetricsError", "OK", "EMPTY_INPUT", "NO_FRAMES", "NO_METADATA", "INCOMPLETE",
    "parse_fps", "frame_to_seconds", "seconds_to_frame", "snap_seconds_to_frame",
    "format_timestamp", "expected_frame_count", "fraction_to_str",
    "escape_filter_path", "build_analysis_argv", "build_blackdetect_argv",
    "FrameMeta", "FrameMetaParse", "parse_frame_metadata",
    "ScdetFrame", "ScdetParse", "parse_scdet",
    "SignalstatsFrame", "SignalstatsParse", "parse_signalstats",
    "BlackInterval", "BlackParse", "parse_blackdetect_log", "parse_blackdetect_metadata",
    "CandidateGroup", "group_candidates", "context_window",
    "LumaRamp", "find_luma_ramps",
]

# ---------------------------------------------------------------------------- statuses
OK = "ok"                    # parsed; zero events is a valid result
EMPTY_INPUT = "empty_input"  # blank text: the tool produced nothing (distinct from "no events")
NO_FRAMES = "no_frames"      # text present but no frame headers
NO_METADATA = "no_metadata"  # frames present but none carry this filter's keys
INCOMPLETE = "incomplete"    # some frames / lines were unusable; see .problems


class MetricsError(ValueError):
    """Invalid argument to a metrics function (not a parse problem)."""


# ---------------------------------------------------------------------------- timing
_NTSC = (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001), Fraction(120000, 1001))


def parse_fps(value) -> Fraction:
    """Rational frame rate from '25', '25/1', '30000/1001', int, Fraction or (NTSC-snapped) float.

    Rejects zero, negative and non-finite values and '0/0' (what ffprobe reports for audio).
    """
    if isinstance(value, bool):
        raise MetricsError("frame rate cannot be a bool")
    if isinstance(value, Fraction):
        f = value
    elif isinstance(value, int):
        f = Fraction(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise MetricsError(f"frame rate is not finite: {value!r}")
        f = Fraction(value).limit_denominator(100000)
        for n in _NTSC:
            if abs(float(n) - value) < 0.002:
                f = n
    elif isinstance(value, str):
        s = value.strip()
        try:
            if s.count("/") > 1:
                raise MetricsError(f"cannot parse frame rate: {value!r}")
            if "/" in s:
                num, den = s.split("/", 1)
                if Fraction(den.strip()) == 0:
                    raise MetricsError(f"frame rate has zero denominator: {value!r}")
                f = Fraction(num.strip()) / Fraction(den.strip())
            else:
                f = Fraction(s)
        except (ValueError, ZeroDivisionError):
            raise MetricsError(f"cannot parse frame rate: {value!r}") from None
    else:
        raise MetricsError(f"unsupported frame-rate type: {type(value).__name__}")
    if f <= 0:
        raise MetricsError(f"frame rate must be positive: {value!r}")
    return f


def _to_fraction(seconds) -> Fraction:
    if isinstance(seconds, Fraction):
        return seconds
    if isinstance(seconds, bool):
        raise MetricsError("seconds cannot be a bool")
    if isinstance(seconds, int):
        return Fraction(seconds)
    if isinstance(seconds, float):
        if not math.isfinite(seconds):
            raise MetricsError(f"seconds is not finite: {seconds!r}")
        return Fraction(repr(seconds))  # shortest decimal text, no binary-float residue
    if isinstance(seconds, str):
        try:
            return Fraction(seconds.strip())
        except (ValueError, ZeroDivisionError):
            raise MetricsError(f"cannot parse seconds: {seconds!r}") from None
    raise MetricsError(f"unsupported seconds type: {type(seconds).__name__}")


def frame_to_seconds(frame: int, fps) -> Fraction:
    """Exact time of the start of 0-based frame ``frame`` at rational rate ``fps``."""
    if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
        raise MetricsError(f"frame must be a non-negative int: {frame!r}")
    return Fraction(frame) / parse_fps(fps)


def seconds_to_frame(seconds, fps, *, mode: str = "nearest") -> int:
    """Frame index for a time. ``mode``: nearest (ties up), floor (frame containing t), ceil."""
    x = _to_fraction(seconds) * parse_fps(fps)
    if x < 0:
        raise MetricsError("seconds must be non-negative")
    if mode == "floor":
        return math.floor(x)
    if mode == "ceil":
        return math.ceil(x)
    if mode == "nearest":
        return math.floor(x + Fraction(1, 2))
    raise MetricsError(f"unknown mode {mode!r}")


def snap_seconds_to_frame(seconds, fps) -> Tuple[int, Fraction]:
    """(nearest frame, residual seconds = given - exact frame time). Residual shows rounding."""
    f = seconds_to_frame(seconds, fps)
    return f, _to_fraction(seconds) - frame_to_seconds(f, fps)


def expected_frame_count(duration_seconds, fps) -> int:
    """Number of frames a stream of this duration should hold (nearest whole frame)."""
    n = seconds_to_frame(duration_seconds, fps)
    return n


def format_timestamp(seconds) -> str:
    """HH:MM:SS.mmm from exact seconds (rounded half-up to the millisecond)."""
    s = _to_fraction(seconds)
    if s < 0:
        raise MetricsError("seconds must be non-negative")
    ms = math.floor(s * 1000 + Fraction(1, 2))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    sec, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d}.{milli:03d}"


def fraction_to_str(f: Fraction) -> str:
    """Stable text for JSON: 'n/d' or 'n' for whole numbers."""
    return str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}"


# ---------------------------------------------------------------------------- FFmpeg argument lists
_FORBIDDEN_PATH_CHARS = set("'\";,[]=\n\r\0")


def escape_filter_path(path: str) -> str:
    """Make a filesystem path safe inside a filtergraph option value (no shell involved).

    Backslashes become '/', and each ':' becomes TWO backslashes + ':' (the filtergraph parser and the
    option parser each consume one level; verified with FFmpeg 9.0.1 on Windows: one backslash or plain
    single quotes fail with 'No option name near ...', two backslashes work, including directory names
    with spaces). Paths containing characters that would need further quoting (quotes, comma, semicolon,
    brackets, '=', control chars) are refused rather than guessed at.
    """
    if not isinstance(path, str) or not path.strip():
        raise MetricsError("path must be a non-empty string")
    p = path.replace("\\", "/")
    bad = sorted(c for c in set(p) if c in _FORBIDDEN_PATH_CHARS)
    if bad:
        raise MetricsError(f"path contains characters not allowed in a filtergraph: {bad!r}")
    return p.replace(":", chr(92) * 2 + ":")


def _check_exe_and_src(ffmpeg: str, src: str) -> None:
    for name, v in (("ffmpeg", ffmpeg), ("src", src)):
        if not isinstance(v, str) or not v.strip():
            raise MetricsError(f"{name} must be a non-empty string")
        if "\0" in v:
            raise MetricsError(f"{name} contains a NUL byte")
    if src.startswith("-"):
        raise MetricsError("src must not start with '-' (would be read as an option)")


def _check_int(name: str, v, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise MetricsError(f"{name} must be an int in [{lo}, {hi}]: {v!r}")
    return v


def _check_num(name: str, v, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise MetricsError(f"{name} must be a number in [{lo}, {hi}]: {v!r}")
    return float(v)


def build_analysis_argv(ffmpeg: str, src: str, metadata_path: str, *, threshold: float = 5.0,
                        scale_height: Optional[int] = 360, threads: int = 2,
                        signalstats: bool = True) -> List[str]:
    """argv for one read-only decode pass: scdet (+ signalstats) -> per-frame metadata file.

    ``threshold`` only controls scdet's own ``lavfi.scd.time`` flag; every frame's score is
    written regardless, so grouping/thresholding stays a later, reversible decision.
    ``scale_height`` (None = analyse at source size) trades speed for fidelity and changes scores;
    callers must record it with the results.
    """
    _check_exe_and_src(ffmpeg, src)
    thr = _check_num("threshold", threshold, 0.0, 100.0)
    _check_int("threads", threads, 1, 64)
    parts = []
    if scale_height is not None:
        parts.append(f"scale=-2:{_check_int('scale_height', scale_height, 16, 4320)}:flags=bilinear")
    parts.append(f"scdet=threshold={thr:g}")
    if signalstats:
        parts.append("signalstats")
    parts.append(f"metadata=mode=print:file={escape_filter_path(metadata_path)}")
    return [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-threads", str(threads),
            "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "1",
            "-vf", ",".join(parts), "-f", "null", "-"]


def build_blackdetect_argv(ffmpeg: str, src: str, *, min_duration: float = 0.08,
                           picture_black_ratio: float = 0.98, pixel_black: float = 0.10,
                           scale_height: Optional[int] = 360, threads: int = 2) -> List[str]:
    """argv for a blackdetect pass whose result is read from stderr (log level info)."""
    _check_exe_and_src(ffmpeg, src)
    d = _check_num("min_duration", min_duration, 0.0, 3600.0)
    pic = _check_num("picture_black_ratio", picture_black_ratio, 0.0, 1.0)
    pix = _check_num("pixel_black", pixel_black, 0.0, 1.0)
    _check_int("threads", threads, 1, 64)
    parts = []
    if scale_height is not None:
        parts.append(f"scale=-2:{_check_int('scale_height', scale_height, 16, 4320)}:flags=bilinear")
    parts.append(f"blackdetect=d={d:g}:pic_th={pic:g}:pix_th={pix:g}")
    return [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "info", "-threads", str(threads),
            "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "1",
            "-vf", ",".join(parts), "-f", "null", "-"]


# ---------------------------------------------------------------------------- generic frame metadata
_HDR = re.compile(r"^frame:(\d+)\s+pts:(\S+)\s+pts_time:(\S+)\s*$")
_KV = re.compile(r"^(lavfi\.[A-Za-z0-9_.]+)=(.*)$")


@dataclass(frozen=True)
class FrameMeta:
    frame: int
    pts: Optional[int]
    pts_time: str                    # raw text, preserved
    pts_time_s: Optional[Fraction]   # parsed (rounded to stream time base by FFmpeg); None if unparseable
    values: Dict[str, str]


@dataclass(frozen=True)
class FrameMetaParse:
    status: str
    frames: Tuple[FrameMeta, ...]
    problems: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == OK


def _dec(text: str) -> Optional[Fraction]:
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def parse_frame_metadata(text) -> FrameMetaParse:
    """Parse ``metadata=print`` output into frames. Never raises on bad text; reports problems."""
    if not isinstance(text, str):
        raise MetricsError("text must be a str")
    if not text.strip():
        return FrameMetaParse(EMPTY_INPUT, (), ())
    frames: List[FrameMeta] = []
    problems: List[str] = []
    cur: Optional[dict] = None

    def flush():
        if cur is not None:
            frames.append(FrameMeta(cur["frame"], cur["pts"], cur["pts_time"], cur["pts_time_s"], cur["values"]))

    for ln, line in enumerate(text.splitlines(), 1):
        line = line.rstrip()
        if not line:
            continue
        m = _HDR.match(line)
        if m:
            flush()
            n = int(m.group(1))
            if frames and n <= frames[-1].frame:
                problems.append(f"line {ln}: frame number {n} does not increase (previous {frames[-1].frame})")
            pts = int(m.group(2)) if re.fullmatch(r"-?\d+", m.group(2)) else None
            if pts is None:
                problems.append(f"line {ln}: pts is not an integer: {m.group(2)!r}")
            pt = _dec(m.group(3))
            if pt is None:
                problems.append(f"line {ln}: pts_time is not numeric: {m.group(3)!r}")
            cur = {"frame": n, "pts": pts, "pts_time": m.group(3), "pts_time_s": pt, "values": {}}
            continue
        k = _KV.match(line)
        if k:
            if cur is None:
                problems.append(f"line {ln}: metadata value before any frame header")
            elif k.group(1) in cur["values"]:
                problems.append(f"line {ln}: duplicate key {k.group(1)} in frame {cur['frame']}")
            else:
                cur["values"][k.group(1)] = k.group(2)
            continue
        problems.append(f"line {ln}: unrecognised line {line[:60]!r}")
    flush()
    if not frames:
        return FrameMetaParse(NO_FRAMES, (), tuple(problems))
    return FrameMetaParse(INCOMPLETE if problems else OK, tuple(frames), tuple(problems))


def _float(text: str) -> Optional[float]:
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _gaps(frames: Sequence[int]) -> List[str]:
    out = []
    for a, b in zip(frames, frames[1:]):
        if b != a + 1:
            out.append(f"frame gap between {a} and {b}")
    return out


# ---------------------------------------------------------------------------- scdet
@dataclass(frozen=True)
class ScdetFrame:
    frame: int
    pts: Optional[int]
    pts_time: str
    pts_time_s: Optional[Fraction]
    mafd: float
    score: float
    flagged_time: Optional[str]   # raw lavfi.scd.time (present only when score >= scdet threshold)


@dataclass(frozen=True)
class ScdetParse:
    status: str
    frames: Tuple[ScdetFrame, ...]
    problems: Tuple[str, ...]
    frames_seen: int

    @property
    def ok(self) -> bool:
        return self.status == OK


def parse_scdet(text) -> ScdetParse:
    """Per-frame scdet scores. OK with all-zero scores is a valid 'no changes' result;
    EMPTY_INPUT / NO_FRAMES / NO_METADATA mean the pass produced no usable data."""
    base = parse_frame_metadata(text)
    if base.status in (EMPTY_INPUT, NO_FRAMES):
        return ScdetParse(base.status, (), base.problems, 0)
    problems = list(base.problems)
    rows: List[ScdetFrame] = []
    any_key = False
    for fm in base.frames:
        has_mafd, has_score = "lavfi.scd.mafd" in fm.values, "lavfi.scd.score" in fm.values
        any_key = any_key or has_mafd or has_score or "lavfi.scd.time" in fm.values
        if not (has_mafd and has_score):
            if has_mafd or has_score:
                problems.append(f"frame {fm.frame}: incomplete scdet keys")
            continue
        mafd, score = _float(fm.values["lavfi.scd.mafd"]), _float(fm.values["lavfi.scd.score"])
        if mafd is None or score is None or mafd < 0 or score < 0:
            problems.append(f"frame {fm.frame}: scdet value not a finite non-negative number")
            continue
        rows.append(ScdetFrame(fm.frame, fm.pts, fm.pts_time, fm.pts_time_s, mafd, score,
                               fm.values.get("lavfi.scd.time")))
    if not any_key:
        return ScdetParse(NO_METADATA, (), tuple(problems), len(base.frames))
    problems += _gaps([r.frame for r in rows])
    if len(rows) != len(base.frames):
        problems.append(f"{len(base.frames) - len(rows)} of {len(base.frames)} frames lacked usable scdet data")
    return ScdetParse(INCOMPLETE if problems else OK, tuple(rows), tuple(problems), len(base.frames))


# ---------------------------------------------------------------------------- signalstats
DEFAULT_SIGNALSTATS_FIELDS = ("YAVG", "YDIF", "YMIN", "YMAX", "UAVG", "VAVG", "SATAVG", "HUEMED")


@dataclass(frozen=True)
class SignalstatsFrame:
    frame: int
    pts_time_s: Optional[Fraction]
    values: Dict[str, float]


@dataclass(frozen=True)
class SignalstatsParse:
    status: str
    frames: Tuple[SignalstatsFrame, ...]
    problems: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == OK


def parse_signalstats(text, *, fields: Iterable[str] = DEFAULT_SIGNALSTATS_FIELDS,
                      required: Iterable[str] = ("YAVG",)) -> SignalstatsParse:
    base = parse_frame_metadata(text)
    if base.status in (EMPTY_INPUT, NO_FRAMES):
        return SignalstatsParse(base.status, (), base.problems)
    fields, required = tuple(fields), tuple(required)
    problems = list(base.problems)
    rows: List[SignalstatsFrame] = []
    any_key = False
    for fm in base.frames:
        vals: Dict[str, float] = {}
        for f in fields:
            raw = fm.values.get(f"lavfi.signalstats.{f}")
            if raw is None:
                continue
            any_key = True
            v = _float(raw)
            if v is None:
                problems.append(f"frame {fm.frame}: {f} is not a finite number")
            else:
                vals[f] = v
        missing = [r for r in required if r not in vals]
        if missing:
            if any(k.startswith("lavfi.signalstats.") for k in fm.values):
                problems.append(f"frame {fm.frame}: missing required {missing}")
            continue
        rows.append(SignalstatsFrame(fm.frame, fm.pts_time_s, vals))
    if not any_key and not any(k.startswith("lavfi.signalstats.") for f in base.frames for k in f.values):
        return SignalstatsParse(NO_METADATA, (), tuple(problems))
    problems += _gaps([r.frame for r in rows])
    if len(rows) != len(base.frames):
        problems.append(f"{len(base.frames) - len(rows)} of {len(base.frames)} frames lacked usable signalstats data")
    return SignalstatsParse(INCOMPLETE if problems else OK, tuple(rows), tuple(problems))


# ---------------------------------------------------------------------------- blackdetect
@dataclass(frozen=True)
class BlackInterval:
    start: Fraction
    end: Optional[Fraction]          # None: black ran to the end of the stream (metadata form)
    duration: Optional[Fraction]
    open_ended: bool = False


@dataclass(frozen=True)
class BlackParse:
    status: str
    intervals: Tuple[BlackInterval, ...]
    problems: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == OK


_BLACK_LOG = re.compile(r"black_start:(\S+)\s+black_end:(\S+)\s+black_duration:(\S+)")
_COMPLETION = re.compile(r"\bvideo:\d+")


def parse_blackdetect_log(text, *, require_completion_marker: bool = True) -> BlackParse:
    """Intervals from the stderr log of a ``-v info`` run.

    Zero intervals is only a valid result when the log shows the run completed (ffmpeg's closing
    'video:...' summary line); otherwise the status is INCOMPLETE, not OK.
    """
    if not isinstance(text, str):
        raise MetricsError("text must be a str")
    if not text.strip():
        return BlackParse(EMPTY_INPUT, (), ())
    problems: List[str] = []
    out: List[BlackInterval] = []
    for m in _BLACK_LOG.finditer(text):
        s, e, d = _dec(m.group(1)), _dec(m.group(2)), _dec(m.group(3))
        if s is None or e is None or d is None:
            problems.append(f"unparseable black interval: {m.group(0)[:80]!r}")
            continue
        if e < s or d < 0 or abs((e - s) - d) > Fraction(2, 1000):
            problems.append(f"inconsistent black interval start={s} end={e} duration={d}")
            continue
        out.append(BlackInterval(s, e, d))
    if require_completion_marker and not _COMPLETION.search(text):
        problems.append("log has no completion summary; the run may have been cut short")
    return BlackParse(INCOMPLETE if problems else OK, tuple(out), tuple(problems))


def parse_blackdetect_metadata(text) -> BlackParse:
    """Intervals from ``lavfi.black_start`` / ``lavfi.black_end`` frame metadata.

    A start with no later end is returned as an open-ended interval (black to end of stream)."""
    base = parse_frame_metadata(text)
    if base.status in (EMPTY_INPUT, NO_FRAMES):
        return BlackParse(base.status, (), base.problems)
    problems = list(base.problems)
    out: List[BlackInterval] = []
    start: Optional[Fraction] = None
    for fm in base.frames:
        for key in ("lavfi.black_start", "lavfi.black_end"):
            if key not in fm.values:
                continue
            v = _dec(fm.values[key])
            if v is None:
                problems.append(f"frame {fm.frame}: {key} not numeric")
            elif key.endswith("start"):
                if start is not None:
                    problems.append(f"frame {fm.frame}: black_start while an interval is already open")
                start = v
            else:
                if start is None:
                    problems.append(f"frame {fm.frame}: black_end without a start")
                elif v < start:
                    problems.append(f"frame {fm.frame}: black_end before black_start")
                    start = None
                else:
                    out.append(BlackInterval(start, v, v - start))
                    start = None
    if start is not None:
        out.append(BlackInterval(start, None, None, open_ended=True))
    return BlackParse(INCOMPLETE if problems else OK, tuple(out), tuple(problems))


# ---------------------------------------------------------------------------- candidate grouping
@dataclass(frozen=True)
class CandidateGroup:
    """A run of adjacent frames whose score reached the floor. Every such frame belongs to exactly one group."""
    first_frame: int
    last_frame: int
    frames: Tuple[int, ...]
    scores: Tuple[float, ...]
    peak_frame: int
    peak_score: float

    @property
    def span(self) -> int:
        return self.last_frame - self.first_frame + 1


def group_candidates(frames: Sequence[ScdetFrame], floor: float, *, max_gap: int = 0) -> Tuple[CandidateGroup, ...]:
    """Cluster frames with ``score >= floor``; members may be separated by at most ``max_gap``
    below-floor frames. Single-frame events are kept as groups of one. Nothing is discarded."""
    fl = _check_num("floor", floor, 0.0, 100.0)
    _check_int("max_gap", max_gap, 0, 10_000)
    ordered = sorted(frames, key=lambda f: f.frame)
    for a, b in zip(ordered, ordered[1:]):
        if a.frame == b.frame:
            raise MetricsError(f"duplicate frame number {a.frame}")
    hits = [f for f in ordered if f.score >= fl]
    groups: List[List[ScdetFrame]] = []
    for f in hits:
        if groups and f.frame - groups[-1][-1].frame - 1 <= max_gap:
            groups[-1].append(f)
        else:
            groups.append([f])
    out = []
    for g in groups:
        peak = max(g, key=lambda x: (x.score, -x.frame))  # earliest frame wins ties
        out.append(CandidateGroup(g[0].frame, g[-1].frame, tuple(x.frame for x in g),
                                  tuple(x.score for x in g), peak.frame, peak.score))
    assert sum(len(g.frames) for g in out) == len(hits)
    return tuple(out)


def context_window(values: Sequence[float], first: int, last: int, before: int, after: int,
                   *, base_frame: int = 0) -> Tuple[int, Tuple[float, ...]]:
    """Slice of a per-frame series around [first, last]: returns (start_frame, values), clamped to the data.

    ``values[i]`` belongs to frame ``base_frame + i``."""
    _check_int("before", before, 0, 10_000)
    _check_int("after", after, 0, 10_000)
    lo = max(base_frame, first - before)
    hi = min(base_frame + len(values) - 1, last + after)
    if hi < lo:
        return lo, ()
    return lo, tuple(values[lo - base_frame:hi - base_frame + 1])


# ---------------------------------------------------------------------------- gradual luma ramps
@dataclass(frozen=True)
class LumaRamp:
    first_frame: int      # first frame whose luma differs from its predecessor in the ramp direction
    last_frame: int
    direction: int        # +1 brightening, -1 darkening
    start_luma: float     # luma of the frame just before the ramp
    end_luma: float
    steps: int
    monotonic_fraction: float

    @property
    def span(self) -> int:
        return self.last_frame - self.first_frame + 1

    @property
    def total_change(self) -> float:
        return self.end_luma - self.start_luma


def find_luma_ramps(luma: Sequence[float], *, base_frame: int = 0, min_frames: int = 4,
                    min_total: float = 8.0, min_step: float = 0.5, max_flat: int = 1) -> Tuple[LumaRamp, ...]:
    """Sustained, steady brightness drifts (candidate fades / dissolves).

    scdet scores stay tiny during a gradual change (observed 0.1-2.3 on tiny synthetic fades), so these
    cannot be found from scdet groups; the per-frame luma average is used instead. A ramp is a run of
    frame-to-frame changes of one sign (each at least ``min_step``), tolerating up to ``max_flat``
    consecutive near-flat frames. Reported only if it lasts ``min_frames`` and moves luma by ``min_total``.
    Camera moves and lighting changes can also ramp; telling them apart is the classifier's job."""
    _check_int("min_frames", min_frames, 2, 100_000)
    _check_int("max_flat", max_flat, 0, 100)
    _check_num("min_total", min_total, 0.0, 1e9)
    _check_num("min_step", min_step, 0.0, 1e9)
    deltas = [luma[i] - luma[i - 1] for i in range(1, len(luma))]
    out: List[LumaRamp] = []
    i = 0
    while i < len(deltas):
        d = deltas[i]
        if abs(d) < min_step:
            i += 1
            continue
        sign = 1 if d > 0 else -1
        j, last_good, flat = i, i, 0
        while j + 1 < len(deltas):
            nxt = deltas[j + 1]
            if nxt * sign >= min_step:
                j += 1
                last_good = j
                flat = 0
            elif abs(nxt) < min_step and flat < max_flat:
                j += 1
                flat += 1
            else:
                break
        run = deltas[i:last_good + 1]
        steps = len(run)
        good = sum(1 for x in run if x * sign >= min_step)
        start_idx, end_idx = i, last_good + 1       # luma indices: before-ramp frame, last ramp frame
        total = luma[end_idx] - luma[start_idx]
        if steps >= min_frames and abs(total) >= min_total:
            out.append(LumaRamp(base_frame + start_idx + 1, base_frame + end_idx, sign,
                                float(luma[start_idx]), float(luma[end_idx]), steps, good / steps))
        i = last_good + 1
    return tuple(out)
