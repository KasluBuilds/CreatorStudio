"""Audio metric contract between media_analysis (producer) and Creator DNA (consumer).

`AudioWindow.metrics` may only contain the names below, each a finite real number inside its range.
Unknown names, booleans, strings, NaN/inf and out-of-range values are REJECTED at construction.
Every metric is OPTIONAL: a window carries only what was measured. Consumers must read metrics
with `AudioWindow.metric(name)` (returns None when absent) and decide explicitly what to do
without it; never assume a metric exists, never substitute a default.

Common assumptions
  * Times are seconds on the ORIGINAL media's timeline.
  * Levels are dBFS (0 = digital full scale, -120 = floor/digital silence) computed over the
    analysis window length recorded upstream (`Origin.settings["window_s"]`).
  * "Quiet"/"active" depends on the threshold in `Origin.settings["resolved_threshold_dbfs"]`
    (heuristic when upstream used "auto"). A pause is a MEASUREMENT of quiet duration: nothing here says
    the pause is awkward, comedic or dead air.

name               unit  scope     range       larger means                         produced for
pause_duration_s   s     duration  >= 0        a longer quiet run                   quiet-gap windows
energy_rise_db     dB    point     0 .. 120    a bigger jump above the baseline     energy-rise windows
level_dbfs         dBFS  window    -120 .. 0   louder (closer to 0)                 energy-rise / level windows
baseline_dbfs      dBFS  window    -120 .. 0   louder recent background level       energy-rise windows
rms_dbfs           dBFS  window    -120 .. 0   louder average level                 level windows
peak_dbfs          dBFS  window    -120 .. 0   louder peak sample                   level windows

Consistency rules (checked only when the involved metrics are present):
  peak_dbfs >= rms_dbfs - 0.01; |(level_dbfs - baseline_dbfs) - energy_rise_db| <= 0.1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .schema import DNAError

CONTRACT_VERSION = 1
LEVEL_MIN, LEVEL_MAX = -120.0, 0.0


@dataclass(frozen=True)
class MetricSpec:
    name: str
    unit: str
    scope: str            # "duration" | "window" | "point"
    minimum: float
    maximum: float | None
    meaning: str
    larger_means: str
    assumptions: str


METRICS: dict[str, MetricSpec] = {m.name: m for m in (
    MetricSpec("pause_duration_s", "s", "duration", 0.0, None,
               "length of one continuous quiet run (level at or below the analysis threshold)",
               "a longer quiet run",
               "only runs >= the upstream min_gap_s are reported; says nothing about editorial value"),
    MetricSpec("energy_rise_db", "dB", "point", 0.0, 120.0,
               "level of the event window minus the baseline, at the window start",
               "a bigger jump above the recent baseline",
               "only reported when the jump >= upstream rise_db; a level change, not 'emphasis'"),
    MetricSpec("level_dbfs", "dBFS", "window", LEVEL_MIN, LEVEL_MAX,
               "RMS level of this window", "louder", "-120 means digital silence"),
    MetricSpec("baseline_dbfs", "dBFS", "window", LEVEL_MIN, LEVEL_MAX,
               "median RMS level of the preceding active windows within upstream baseline_s",
               "a louder recent background", "needs enough preceding active windows upstream"),
    MetricSpec("rms_dbfs", "dBFS", "window", LEVEL_MIN, LEVEL_MAX,
               "RMS level of this window", "louder", "-120 means digital silence"),
    MetricSpec("peak_dbfs", "dBFS", "window", LEVEL_MIN, LEVEL_MAX,
               "highest absolute sample in this window", "a louder peak", "-120 means digital silence"),
)}


def validate_metrics(metrics: Mapping[str, float]) -> dict[str, float]:
    """Return a clean copy of `metrics` (floats) or raise DNAError. Empty mappings are rejected."""
    if not isinstance(metrics, Mapping) or not metrics:
        raise DNAError("audio window needs at least one metric")
    out: dict[str, float] = {}
    for name, value in metrics.items():
        spec = METRICS.get(name)
        if spec is None:
            raise DNAError(f"unknown audio metric {name!r}; contract v{CONTRACT_VERSION} defines {sorted(METRICS)}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DNAError(f"metric {name} must be a number, got {value!r}")
        v = float(value)
        if not math.isfinite(v):
            raise DNAError(f"metric {name} must be finite, got {value!r}")
        if v < spec.minimum or (spec.maximum is not None and v > spec.maximum):
            raise DNAError(f"metric {name}={v} outside {spec.minimum}..{spec.maximum} {spec.unit}")
        out[name] = v
    if "peak_dbfs" in out and "rms_dbfs" in out and out["peak_dbfs"] < out["rms_dbfs"] - 0.01:
        raise DNAError("peak_dbfs cannot be below rms_dbfs")
    if all(k in out for k in ("level_dbfs", "baseline_dbfs", "energy_rise_db")) and \
            abs((out["level_dbfs"] - out["baseline_dbfs"]) - out["energy_rise_db"]) > 0.1:
        raise DNAError("energy_rise_db is inconsistent with level_dbfs - baseline_dbfs")
    return out
