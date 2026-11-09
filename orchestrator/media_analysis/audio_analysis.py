"""Deterministic audio measurements (stdlib `wave` only, PCM WAV input).

Everything produced here is a MEASUREMENT of signal level over time:
  windows        RMS / peak level per fixed window (dBFS)
  regions        runs above / below a level threshold  (energy, NOT speech: music, noise
                 and breathing also exceed a threshold - real speech detection needs a
                 `SpeechActivityProvider`, see below)
  gaps           quiet runs >= min_gap_s, with their position and exact duration
  energy_events  places where the level rises clearly above the recent baseline

No record says a pause is awkward, comedic or dead air, and none says a rise is
"emphasis": such readings are interpretations and belong in moment_index.Interpretation.
The thresholds used are stored in `params`, so every number can be reproduced.

Performance: pure Python; roughly a few seconds per hour of 16 kHz mono audio.
Input should be an analysis copy (proxies.AnalysisCopyRegistry), not an original container.
"""
from __future__ import annotations

import math
import statistics
import sys
import wave
from array import array
from dataclasses import dataclass, field
from operator import mul
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from .models import (CapabilityError, MediaAnalysisError, Provenance, atomic_write_json, check_range,
                     check_unit, read_json, stable_hash, t_key)

SCHEMA = "media_analysis.audio/1"
ENGINE = "energy-activity"
VERSION = "1"
FLOOR_DB = -120.0

DEFAULT_PARAMS = {
    "window_s": 0.1,              # analysis window (non-overlapping)
    "threshold_dbfs": "auto",     # number, or "auto" (see _auto_threshold)
    "min_gap_s": 0.3,             # quiet runs shorter than this are not reported as gaps
    "min_active_s": 0.15,         # active blips shorter than this inside quiet are treated as quiet
    "long_gap_s": 2.0,            # gaps at least this long carry long=True (a label of length only)
    "baseline_s": 3.0, "rise_db": 6.0, "event_refractory_s": 1.0,
}


def to_dbfs(x: float) -> float:
    return FLOOR_DB if x <= 0 else max(FLOOR_DB, 20.0 * math.log10(x))


@dataclass(frozen=True)
class AudioWindow:
    media_id: str
    start_s: float
    end_s: float
    rms_dbfs: float
    peak_dbfs: float

    def __post_init__(self) -> None:
        check_range(self.start_s, self.end_s)
        for name, v in (("rms_dbfs", self.rms_dbfs), ("peak_dbfs", self.peak_dbfs)):
            if not (FLOOR_DB - 1e-9 <= v <= 0.01):
                raise MediaAnalysisError(f"{name} {v} outside {FLOOR_DB}..0 dBFS")
        if self.rms_dbfs > self.peak_dbfs + 1e-6:
            raise MediaAnalysisError("rms cannot exceed peak")

    @property
    def record_id(self) -> str:
        return "win_" + stable_hash(self.media_id, t_key(self.start_s), t_key(self.end_s))


@dataclass(frozen=True)
class ActivityRegion:
    media_id: str
    start_s: float
    end_s: float
    state: str                    # "above_threshold" | "below_threshold"
    threshold_dbfs: float
    mean_rms_dbfs: float

    def __post_init__(self) -> None:
        check_range(self.start_s, self.end_s)
        if self.state not in ("above_threshold", "below_threshold"):
            raise MediaAnalysisError(f"unknown region state {self.state!r}")

    @property
    def record_id(self) -> str:
        return "reg_" + stable_hash(self.media_id, t_key(self.start_s), t_key(self.end_s), self.state)


@dataclass(frozen=True)
class QuietGap:
    media_id: str
    start_s: float
    end_s: float
    threshold_dbfs: float
    position: str                 # "between" (active on both sides) | "leading" | "trailing" | "whole"
    long: bool                    # duration >= long_gap_s; says nothing about whether it matters

    def __post_init__(self) -> None:
        check_range(self.start_s, self.end_s)
        if self.position not in ("between", "leading", "trailing", "whole"):
            raise MediaAnalysisError(f"unknown gap position {self.position!r}")

    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 6)

    @property
    def record_id(self) -> str:
        return "gap_" + stable_hash(self.media_id, t_key(self.start_s), t_key(self.end_s))


@dataclass(frozen=True)
class EnergyEvent:
    media_id: str
    start_s: float
    end_s: float
    kind: str                     # "energy_rise" only (a level change, not "emphasis")
    level_dbfs: float
    baseline_dbfs: float
    delta_db: float

    def __post_init__(self) -> None:
        check_range(self.start_s, self.end_s)
        if self.kind != "energy_rise":
            raise MediaAnalysisError(f"unknown energy event kind {self.kind!r}")

    @property
    def record_id(self) -> str:
        return "enr_" + stable_hash(self.media_id, t_key(self.start_s), self.kind)


@dataclass(frozen=True)
class SpeechRegion:
    """Output of a real voice-activity engine (not the energy detector)."""
    media_id: str
    start_s: float
    end_s: float
    confidence: float | None = None

    def __post_init__(self) -> None:
        check_range(self.start_s, self.end_s)
        check_unit(self.confidence, "confidence")

    @property
    def record_id(self) -> str:
        return "spc_" + stable_hash(self.media_id, t_key(self.start_s), t_key(self.end_s))


@runtime_checkable
class SpeechActivityProvider(Protocol):
    name: str
    version: str
    def detect_speech(self, media_id: str, audio_path: str) -> Sequence[SpeechRegion]: ...


class UnavailableSpeechActivity:
    name, version = "none", "0"

    def detect_speech(self, media_id, audio_path):
        raise CapabilityError("no speech-activity (VAD) engine is installed; only energy thresholds are available")


@dataclass(frozen=True)
class AudioAnalysis:
    media_id: str
    duration_s: float
    sample_rate: int
    params: Mapping
    windows: tuple[AudioWindow, ...]
    regions: tuple[ActivityRegion, ...]
    gaps: tuple[QuietGap, ...]
    energy_events: tuple[EnergyEvent, ...]
    provenance: Provenance
    resolved_threshold_dbfs: float = FLOOR_DB
    speech_regions: tuple[SpeechRegion, ...] = ()     # only if a VAD provider was used
    analysis_copy_id: str | None = None               # copy that was analysed; times are on the original timeline

    def to_dict(self) -> dict:
        def rows(items):
            return [dict(vars(i)) for i in items]
        return {"schema": SCHEMA, "media_id": self.media_id, "duration_s": self.duration_s,
                "sample_rate": self.sample_rate, "params": dict(self.params),
                "resolved_threshold_dbfs": self.resolved_threshold_dbfs,
                "analysis_copy_id": self.analysis_copy_id, "provenance": self.provenance.to_dict(), "windows": rows(self.windows),
                "regions": rows(self.regions), "gaps": rows(self.gaps),
                "energy_events": rows(self.energy_events), "speech_regions": rows(self.speech_regions)}

    @classmethod
    def from_dict(cls, d: Mapping) -> "AudioAnalysis":
        if d.get("schema") != SCHEMA:
            raise MediaAnalysisError(f"unrecognised audio analysis schema {d.get('schema')!r}")
        return cls(d["media_id"], d["duration_s"], d["sample_rate"], d["params"],
                   tuple(AudioWindow(**w) for w in d["windows"]), tuple(ActivityRegion(**r) for r in d["regions"]),
                   tuple(QuietGap(**g) for g in d["gaps"]), tuple(EnergyEvent(**e) for e in d["energy_events"]),
                   Provenance.from_dict(d["provenance"]), d.get("resolved_threshold_dbfs", FLOOR_DB),
                   tuple(SpeechRegion(**s) for s in d.get("speech_regions", [])), d.get("analysis_copy_id"))


# ------------------------------------------------------------------ reading
def _read_windows(path: str, media_id: str, window_s: float):
    """Yield AudioWindow for consecutive windows. Returns (windows, sample_rate, duration_s)."""
    try:
        w = wave.open(str(path), "rb")
    except (wave.Error, EOFError, OSError) as exc:
        raise MediaAnalysisError(f"cannot read {path} as PCM WAV: {exc}") from exc
    with w:
        ch, sw, rate, total = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        if sw not in (1, 2, 4):
            raise MediaAnalysisError(f"unsupported sample width {sw * 8} bit (use 8/16/32-bit PCM)")
        if total == 0:
            raise MediaAnalysisError("audio file contains no samples")
        per = max(1, int(round(window_s * rate)))
        full_scale = float(2 ** (8 * sw - 1))
        typecode = {1: "B", 2: "h", 4: "i"}[sw]
        out, pos = [], 0
        while pos < total:
            raw = w.readframes(min(per, total - pos))
            n_frames = len(raw) // (sw * ch)
            if n_frames == 0:
                break
            a = array(typecode)
            a.frombytes(raw[: n_frames * sw * ch])
            if sw > 1 and sys.byteorder == "big":
                a.byteswap()
            vals = [x - 128 for x in a] if sw == 1 else a
            if ch > 1:
                vals = [sum(f) / ch for f in zip(*[vals[c::ch] for c in range(ch)])]
            n = len(vals)
            rms = math.sqrt(sum(map(mul, vals, vals)) / n) / full_scale
            peak = max(abs(min(vals)), abs(max(vals))) / full_scale
            start, end = pos / rate, (pos + n_frames) / rate
            out.append(AudioWindow(media_id, round(start, 6), round(end, 6), min(0.0, to_dbfs(rms)),
                                   min(0.0, to_dbfs(max(peak, rms)))))
            pos += n_frames
    return out, rate, total / rate


# ----------------------------------------------------------------- analysis
def _auto_threshold(levels: list[float]) -> float:
    """Heuristic, recorded in the result. Between the noise floor (10th percentile) and the
    loud level (95th percentile); if the file has under 10 dB of spread there is nothing
    to separate and the threshold sits below everything (no gaps are invented)."""
    s = sorted(levels)
    p10, p95 = s[int(0.10 * (len(s) - 1))], s[int(0.95 * (len(s) - 1))]
    if p95 < -70.0:
        return -70.0                     # effectively digital silence
    if p95 - p10 < 10.0:
        return round(p10 - 1.0, 2)
    return round(p10 + 0.4 * (p95 - p10), 2)


def _runs(flags: list[bool]) -> list[list]:
    runs: list[list] = []
    for i, f in enumerate(flags):
        if runs and runs[-1][0] == f:
            runs[-1][2] = i + 1
        else:
            runs.append([f, i, i + 1])
    return runs


def analyze_wav(path: str | Path, media_id: str, params: Mapping | None = None,
                speech_provider: SpeechActivityProvider | None = None,
                analysis_copy_id: str | None = None) -> AudioAnalysis:
    p = {**DEFAULT_PARAMS, **(params or {})}
    unknown = set(p) - set(DEFAULT_PARAMS)
    if unknown:
        raise MediaAnalysisError(f"unknown audio analysis params: {sorted(unknown)}")
    if p["window_s"] <= 0 or p["min_gap_s"] <= 0:
        raise MediaAnalysisError("window_s and min_gap_s must be positive")
    windows, rate, duration = _read_windows(str(path), media_id, p["window_s"])
    levels = [w.rms_dbfs for w in windows]
    thr = _auto_threshold(levels) if p["threshold_dbfs"] == "auto" else float(p["threshold_dbfs"])
    ws = p["window_s"]
    active = [lv > thr for lv in levels]

    # 1. active blips shorter than min_active_s inside quiet become quiet; 2. quiet runs shorter
    #    than min_gap_s between active runs become active (they are not pauses).
    min_active = max(1, math.ceil(p["min_active_s"] / ws - 1e-9))
    min_gap = max(1, math.ceil(p["min_gap_s"] / ws - 1e-9))
    for f, a, b in _runs(active):
        if f and b - a < min_active:
            active[a:b] = [False] * (b - a)
    runs = _runs(active)
    for idx, (f, a, b) in enumerate(runs):
        if not f and 0 < idx < len(runs) - 1 and b - a < min_gap:
            active[a:b] = [True] * (b - a)
    runs = _runs(active)

    regions, gaps = [], []
    for idx, (f, a, b) in enumerate(runs):
        s, e = windows[a].start_s, windows[b - 1].end_s
        mean_rms = round(statistics.fmean(levels[a:b]), 2)
        regions.append(ActivityRegion(media_id, s, e, "above_threshold" if f else "below_threshold", thr, mean_rms))
        if not f and (e - s) >= p["min_gap_s"] - 1e-9:
            pos = "whole" if len(runs) == 1 else "leading" if idx == 0 else "trailing" if idx == len(runs) - 1 else "between"
            gaps.append(QuietGap(media_id, s, e, thr, pos, (e - s) >= p["long_gap_s"] - 1e-9))

    events, last_end = [], -1e9
    base_n = max(1, int(p["baseline_s"] / ws))
    for i, w in enumerate(windows):
        if not active[i] or w.start_s < last_end + p["event_refractory_s"]:
            continue
        prior = [levels[j] for j in range(max(0, i - base_n), i) if active[j]]
        if len(prior) < max(3, base_n // 3):
            continue
        base = statistics.median(prior)
        if levels[i] - base >= p["rise_db"]:
            events.append(EnergyEvent(media_id, w.start_s, w.end_s, "energy_rise", round(levels[i], 2),
                                      round(base, 2), round(levels[i] - base, 2)))
            last_end = w.end_s

    speech: tuple[SpeechRegion, ...] = ()
    prov_settings = dict(p)
    if speech_provider is not None:
        speech = tuple(speech_provider.detect_speech(media_id, str(path)))
        prov_settings["speech_provider"] = f"{speech_provider.name}@{speech_provider.version}"
    return AudioAnalysis(media_id, round(duration, 6), rate, p, tuple(windows), tuple(regions), tuple(gaps),
                         tuple(events), Provenance(ENGINE, VERSION, "deterministic", prov_settings),
                         thr, speech, analysis_copy_id)


def save_audio_analysis(directory: str | Path, a: AudioAnalysis) -> Path:
    path = Path(directory) / f"{a.media_id}.audio-{a.provenance.settings_hash}.json"
    if path.exists():
        old, new = read_json(path), a.to_dict()
        for d in (old, new):
            d["provenance"].pop("created_at", None)
        if old != new:
            raise MediaAnalysisError(f"{path.name} exists with different content; refusing to overwrite")
        return path
    atomic_write_json(path, a.to_dict())
    return path


def load_audio_analysis(path: str | Path) -> AudioAnalysis:
    return AudioAnalysis.from_dict(read_json(path))
