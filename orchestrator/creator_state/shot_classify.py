"""Shot-detection classification layer: deterministic, explainable, pure.

Input is an ``Evidence`` record per candidate event (built later from shot_metrics output and
FFmpeg similarity measurements). Output is a ``Classification`` saying what the event most likely
is, how sure the rule set is, and WHY. Nothing is ever dropped: every Evidence yields exactly one
Classification, uncertain ones included, and rejected events (flash, camera motion, lighting
change) are kept with their reasons.

Shots and transitions are separate concepts:
* ``hard_cut``      -> a CUT boundary (shots touch; the new shot starts at ``first_frame``).
* ``fade_in`` / ``fade_out`` / ``dissolve`` -> a TRANSITION occupying a frame range of its own,
  belonging to neither neighbouring shot.
* ``flash`` / ``camera_motion`` / ``lighting_change`` -> not boundaries; kept as rejected events.
* ``uncertain``     -> unresolved; needs review. Never silently turned into a cut.

No minimum shot duration is applied anywhere (``build_shots`` keeps 1-frame shots).

CONFIDENCE: values are heuristic scores from the fixed formulas below (rule base + agreement
bonuses, capped). They are NOT calibrated probabilities and must not be presented as such.

Status: thresholds are untuned defaults chosen from tiny synthetic clips; nothing here has been
validated on real footage. They live in ``Thresholds`` so a later validation step can change them
without touching the rules.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "CONFIDENCE_NOTE", "CATEGORIES", "BOUNDARY_KIND", "ClassifyError", "Thresholds",
    "DEFAULT_THRESHOLDS", "Evidence", "Classification", "classify", "classify_all", "partition",
    "Shot", "Transition", "build_shots",
]

CONFIDENCE_NOTE = (
    "Confidence is a heuristic score produced by fixed rules (rule base plus agreement bonuses, capped); "
    "it is not a calibrated probability and has not been validated on real footage."
)

CATEGORIES = ("hard_cut", "flash", "camera_motion", "lighting_change",
              "fade_in", "fade_out", "dissolve", "uncertain")
# What each category means structurally. None = not a boundary.
BOUNDARY_KIND: Dict[str, Optional[str]] = {
    "hard_cut": "cut", "fade_in": "transition", "fade_out": "transition", "dissolve": "transition",
    "flash": None, "camera_motion": None, "lighting_change": None, "uncertain": None,
}


class ClassifyError(ValueError):
    """Invalid evidence or invalid boundary set (not a classification outcome)."""


@dataclass(frozen=True)
class Thresholds:
    """All tunables. Defaults are untuned starting points (see module docstring)."""
    cut_score: float = 10.0          # scdet score at/above which a spike counts as a strong cut signal
    weak_score: float = 5.0          # below cut_score but still worth confirming with similarity
    cut_max_len: int = 2             # a cut is a 1-2 frame spike (2 allows a blended frame)
    flash_max_len: int = 3           # longest event still considered a possible flash
    flash_return_sim: float = 0.85   # similarity of 'before' to 'after the event' that counts as a return
    flash_luma_spike: float = 25.0   # luma deviation during the event needed to call it a flash
    luma_return_tol: float = 6.0     # |luma_after - luma_before| treated as 'same look' without similarity data
    across_diff_sim: float = 0.60    # similarity across the event at or below this: different content
    across_same_sim: float = 0.75    # at or above this: same content
    motion_same_sim: float = 0.60    # sustained event with similarity at/above this: camera/subject motion
    lighting_luma_delta: float = 15.0
    plateau_min_len: int = 3         # events this long or longer are 'sustained'
    dissolve_min_len: int = 4
    dissolve_max_len: int = 75
    ramp_min_frames: int = 4
    ramp_min_total: float = 10.0     # luma change over a fade/dissolve ramp
    ramp_monotonic_fraction: float = 0.8
    ramp_min_step: float = 0.5
    steady_peak_ratio: float = 3.0   # max/median score for a 'steady' plateau
    luma_ref_frames: int = 3         # frames averaged for the before/after luma reference


DEFAULT_THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Evidence:
    """Everything the classifier may use about one candidate event. Optional items may be None/empty;
    the rules then say so in their reasons instead of guessing."""
    candidate_id: str
    first_frame: int
    last_frame: int
    peak_score: float
    scores: Tuple[float, ...] = ()       # scdet score per frame in the context window
    luma: Tuple[float, ...] = ()         # signalstats YAVG per frame in the same window
    window_start: int = 0                # frame number of scores[0] / luma[0]
    sim_across: Optional[float] = None   # similarity (0-1, 1 identical) of last-frame-before vs first-settled-frame-after
    sim_return: Optional[float] = None   # similarity of last-frame-before vs a frame a few frames after the event
    into_black: bool = False             # a black interval begins at/just after the event end
    out_of_black: bool = False           # a black interval ends at/just before the event start
    source: str = "scdet_group"          # or "luma_ramp": event found from luma drift, not scdet scores

    def __post_init__(self):
        if self.first_frame < 0 or self.last_frame < self.first_frame:
            raise ClassifyError(f"{self.candidate_id}: invalid frame range {self.first_frame}-{self.last_frame}")
        for name in ("sim_across", "sim_return"):
            v = getattr(self, name)
            if v is not None and not 0.0 <= v <= 1.0:
                raise ClassifyError(f"{self.candidate_id}: {name} must be within [0, 1]")
        if self.luma and self.scores and len(self.luma) != len(self.scores):
            raise ClassifyError(f"{self.candidate_id}: luma and scores windows differ in length")

    @property
    def length(self) -> int:
        return self.last_frame - self.first_frame + 1


@dataclass(frozen=True)
class Classification:
    candidate_id: str
    category: str
    boundary_kind: Optional[str]     # "cut" | "transition" | None
    rejected: bool                   # looked like a boundary candidate but is not one
    needs_review: bool               # uncertain
    confidence: float                # heuristic, see CONFIDENCE_NOTE
    confidence_basis: str
    rule: str                        # id of the rule that fired
    reasons: Tuple[str, ...]
    first_frame: int
    last_frame: int
    metrics: Dict[str, Optional[float]]

    def to_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "category": self.category,
                "boundary_kind": self.boundary_kind, "rejected": self.rejected,
                "needs_review": self.needs_review, "confidence": self.confidence,
                "confidence_basis": self.confidence_basis, "confidence_note": CONFIDENCE_NOTE,
                "rule": self.rule, "reasons": list(self.reasons),
                "first_frame": self.first_frame, "last_frame": self.last_frame,
                "metrics": dict(self.metrics)}


# ---------------------------------------------------------------------------- helpers
def _at(series: Sequence[float], window_start: int, frame: int) -> Optional[float]:
    i = frame - window_start
    return series[i] if 0 <= i < len(series) else None


def _ref_mean(ev: Evidence, frames: Iterable[int]) -> Optional[float]:
    vals = [v for v in (_at(ev.luma, ev.window_start, f) for f in frames) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _ramp(ev: Evidence, th: Thresholds):
    """(steps, total_change, monotonic_fraction, direction) over frames first-1..last, or None."""
    lo = ev.first_frame - 1
    vals = [_at(ev.luma, ev.window_start, f) for f in range(lo, ev.last_frame + 1)]
    if len(vals) < 2 or any(v is None for v in vals):
        return None
    deltas = [b - a for a, b in zip(vals, vals[1:])]
    total = vals[-1] - vals[0]
    if total == 0:
        return len(deltas), 0.0, 0.0, 0
    sign = 1 if total > 0 else -1
    good = sum(1 for d in deltas if d * sign >= th.ramp_min_step)
    return len(deltas), total, good / len(deltas), sign


def _cap(x: float, hi: float) -> float:
    return round(max(0.0, min(hi, x)), 2)


def _result(ev: Evidence, category: str, rule: str, reasons: List[str], conf: float, basis: str,
            metrics: Dict[str, Optional[float]]) -> Classification:
    kind = BOUNDARY_KIND[category]
    return Classification(ev.candidate_id, category, kind,
                          rejected=category in ("flash", "camera_motion", "lighting_change"),
                          needs_review=category == "uncertain", confidence=_cap(conf, 0.95),
                          confidence_basis=basis, rule=rule, reasons=tuple(reasons),
                          first_frame=ev.first_frame, last_frame=ev.last_frame, metrics=metrics)


# ---------------------------------------------------------------------------- classification
def classify(ev: Evidence, th: Thresholds = DEFAULT_THRESHOLDS) -> Classification:
    """Apply the ordered rule set. Order: fade -> flash -> lighting -> cut -> dissolve -> motion -> uncertain."""
    n = ev.length
    k = th.luma_ref_frames
    lb = _ref_mean(ev, range(ev.first_frame - k, ev.first_frame))
    la = _ref_mean(ev, range(ev.last_frame + 1, ev.last_frame + 1 + k))
    dluma = (la - lb) if (lb is not None and la is not None) else None
    dev = None
    if lb is not None:
        devs = [abs(v - lb) for v in (_at(ev.luma, ev.window_start, f) for f in range(ev.first_frame, ev.last_frame + 1))
                if v is not None]
        dev = max(devs) if devs else None
    ramp = _ramp(ev, th)
    ramp_ok = bool(ramp and ramp[0] >= th.ramp_min_frames and abs(ramp[1]) >= th.ramp_min_total
                   and ramp[2] >= th.ramp_monotonic_fraction)
    window = [s for s in (_at(ev.scores, ev.window_start, f) for f in range(ev.first_frame, ev.last_frame + 1))
              if s is not None]
    metrics: Dict[str, Optional[float]] = {
        "length_frames": float(n), "peak_score": ev.peak_score, "sim_across": ev.sim_across,
        "sim_return": ev.sim_return, "luma_before": lb, "luma_after": la, "luma_change": dluma,
        "luma_event_deviation": dev, "ramp_total": ramp[1] if ramp else None,
        "ramp_monotonic_fraction": ramp[2] if ramp else None,
    }
    diff = ev.sim_across is not None and ev.sim_across <= th.across_diff_sim
    same = ev.sim_across is not None and ev.sim_across >= th.across_same_sim

    # 1. fades (black adjacency + a real luma ramp)
    if (ev.into_black or ev.out_of_black) and ramp_ok:
        direction = ramp[3]
        if ev.into_black and direction < 0:
            return _result(ev, "fade_out", "R1_fade_out",
                           [f"black interval begins right after the event ({n} frames)",
                            f"luma falls steadily by {abs(ramp[1]):.1f} (monotonic fraction {ramp[2]:.2f})"],
                           0.6 + 0.25 * min(1.0, abs(ramp[1]) / 60.0) + 0.1 * min(1.0, ramp[2]),
                           "base 0.6 + ramp size + steadiness; capped 0.95", metrics)
        if ev.out_of_black and direction > 0:
            return _result(ev, "fade_in", "R1_fade_in",
                           [f"black interval ends right before the event ({n} frames)",
                            f"luma rises steadily by {ramp[1]:.1f} (monotonic fraction {ramp[2]:.2f})"],
                           0.6 + 0.25 * min(1.0, abs(ramp[1]) / 60.0) + 0.1 * min(1.0, ramp[2]),
                           "base 0.6 + ramp size + steadiness; capped 0.95", metrics)

    # 2. flash (short, bright departure that comes back)
    if n <= th.flash_max_len:
        # 'Returned' needs every available signal to agree: a same-content brightness step also has a high
        # before/after similarity, but its luma stays changed, so similarity alone must not decide.
        sim_ret = None if ev.sim_return is None else ev.sim_return >= th.flash_return_sim
        luma_ret = None if dluma is None else abs(dluma) <= th.luma_return_tol
        signals = [x for x in (sim_ret, luma_ret) if x is not None]
        returned = all(signals) if signals else None
        spike = dev is not None and dev >= th.flash_luma_spike
        if returned and spike:
            return _result(ev, "flash", "R2_flash",
                           [f"event lasts {n} frame(s)",
                            "picture returns to its earlier look after the event"
                            + (f" (similarity {ev.sim_return:.2f})" if ev.sim_return is not None
                               else f" (luma {lb:.1f} -> {la:.1f})"),
                            f"luma deviates by {dev:.1f} during the event"],
                           0.6 + (0.25 if ev.sim_return is not None else 0.1) + 0.1 * min(1.0, dev / 100.0),
                           "base 0.6 + return evidence + spike size; capped 0.95", metrics)
        if returned is None and ev.peak_score >= th.cut_score and ev.sim_across is None:
            return _result(ev, "uncertain", "R2_uncertain_no_return_evidence",
                           ["short score spike but neither similarity nor luma data are available",
                            "cannot tell a flash, a very short shot or a cut apart"], 0.2,
                           "no supporting evidence; fixed 0.2", metrics)
        if returned and dev is None:
            return _result(ev, "uncertain", "R2_uncertain_return_without_luma",
                           [f"event lasts {n} frame(s) and the picture returns to its earlier look",
                            "brightness data is unavailable, so a flash cannot be told from a very short shot",
                            "kept for review; not treated as a boundary or discarded"], 0.2,
                           "return seen but no brightness evidence; fixed 0.2", metrics)

    # 3. lighting change (same content, different brightness, not coming back)
    if same and dluma is not None and abs(dluma) >= th.lighting_luma_delta:
        return _result(ev, "lighting_change", "R3_lighting",
                       [f"content looks the same across the event (similarity {ev.sim_across:.2f})",
                        f"average luma changes by {dluma:+.1f} and stays changed"],
                       0.55 + 0.3 * min(1.0, (ev.sim_across - th.across_same_sim) / (1 - th.across_same_sim))
                       + 0.1 * min(1.0, abs(dluma) / 60.0),
                       "base 0.55 + similarity margin + luma change; capped 0.95", metrics)

    # 4. hard cut (short, strong, different before/after)
    if n <= th.cut_max_len:
        notes: List[str] = []
        if ev.peak_score >= th.cut_score and (ev.sim_across is None or diff):
            notes.append(f"{n}-frame score spike {ev.peak_score:.1f} >= {th.cut_score:g}")
            if diff:
                notes.append(f"frames before/after differ (similarity {ev.sim_across:.2f})")
            else:
                notes.append("no similarity evidence available; score alone")
            if ev.into_black or ev.out_of_black:
                notes.append("cut to/from black: no gradual luma ramp found")
            if ev.sim_return is not None and ev.sim_return >= th.flash_return_sim:
                notes.append("picture returns to its earlier look but there is no brightness spike: "
                             "possible very short shot; pair with the next event before trusting")
            margin = min(1.0, (ev.peak_score - th.cut_score) / th.cut_score)
            conf = 0.5 + 0.2 * margin + (0.2 if diff else 0.0)
            conf = min(conf, 0.7) if not diff else conf
            if ev.sim_return is not None and ev.sim_return >= th.flash_return_sim:
                conf = min(conf, 0.6)
            return _result(ev, "hard_cut", "R4_hard_cut", notes, conf,
                           "base 0.5 + score margin + similarity confirmation; capped 0.7 without similarity, 0.6 if it returns",
                           metrics)
        if th.weak_score <= ev.peak_score < th.cut_score and diff:
            return _result(ev, "hard_cut", "R4_hard_cut_weak",
                           [f"score {ev.peak_score:.1f} is below {th.cut_score:g} but frames before/after clearly differ "
                            f"(similarity {ev.sim_across:.2f})"], 0.45,
                           "weak score confirmed by similarity; fixed 0.45", metrics)

    # 5. dissolve (sustained, different content before/after, steady drift)
    if th.dissolve_min_len <= n <= th.dissolve_max_len and diff:
        steady = False
        if len(window) >= th.dissolve_min_len:
            med = statistics.median(window)
            steady = med > 0 and max(window) / med <= th.steady_peak_ratio
        if ramp_ok or steady:
            how = (f"luma drifts steadily by {ramp[1]:+.1f}" if ramp_ok
                   else f"scores form a steady plateau (max/median {max(window) / statistics.median(window):.1f})")
            return _result(ev, "dissolve", "R5_dissolve",
                           [f"gradual change over {n} frames", f"content differs before/after (similarity {ev.sim_across:.2f})", how],
                           0.5 + (0.2 if ramp_ok else 0.0) + 0.15 * min(1.0, (th.across_diff_sim - ev.sim_across) / th.across_diff_sim),
                           "base 0.5 + luma ramp + dissimilarity; capped 0.95", metrics)

    # 6. camera / subject motion (sustained, same content)
    if n >= th.plateau_min_len and ev.sim_across is not None and ev.sim_across >= th.motion_same_sim:
        return _result(ev, "camera_motion", "R6_motion",
                       [f"sustained event ({n} frames) but content is similar before/after (similarity {ev.sim_across:.2f})",
                        "elevated scores here do not indicate a cut"],
                       0.5 + 0.3 * min(1.0, (ev.sim_across - th.motion_same_sim) / (1 - th.motion_same_sim)),
                       "base 0.5 + similarity margin; capped 0.95", metrics)

    # 7. uncertain: say exactly which conditions failed
    why = [f"event length {n} frame(s), peak score {ev.peak_score:.1f}"]
    if ev.sim_across is None:
        why.append("no similarity evidence available")
    elif not (diff or same):
        why.append(f"similarity {ev.sim_across:.2f} is between the 'different' ({th.across_diff_sim:g}) and 'same' ({th.across_same_sim:g}) limits")
    if n >= th.plateau_min_len and ev.sim_across is None:
        why.append("sustained event: could be motion, a dissolve or a fade; similarity needed")
    if n > th.cut_max_len and n < th.dissolve_min_len:
        why.append(f"{n} frames is longer than a cut ({th.cut_max_len}) and shorter than a dissolve ({th.dissolve_min_len})")
    if (ev.into_black or ev.out_of_black) and not ramp_ok:
        why.append("black interval nearby but no steady luma ramp")
    if ev.peak_score >= th.cut_score and same and (dluma is None or abs(dluma) < th.lighting_luma_delta):
        why.append("score spike but picture looks the same before and after")
    why.append("kept for review; not treated as a boundary")
    return _result(ev, "uncertain", "R7_uncertain", why, 0.2 if ev.peak_score >= th.weak_score else 0.1,
                   "no rule fired; fixed low value", metrics)


def classify_all(evidence: Iterable[Evidence], th: Thresholds = DEFAULT_THRESHOLDS) -> Tuple[Classification, ...]:
    """One Classification per Evidence, same order. Nothing is dropped."""
    ev = list(evidence)
    ids = [e.candidate_id for e in ev]
    if len(set(ids)) != len(ids):
        raise ClassifyError("duplicate candidate_id in evidence")
    out = tuple(classify(e, th) for e in ev)
    assert len(out) == len(ev)
    return out


def partition(items: Sequence[Classification]) -> Dict[str, List[Classification]]:
    """Split into cuts / transitions / rejected / uncertain (every item lands in exactly one)."""
    parts: Dict[str, List[Classification]] = {"cuts": [], "transitions": [], "rejected": [], "uncertain": []}
    for c in items:
        if c.boundary_kind == "cut":
            parts["cuts"].append(c)
        elif c.boundary_kind == "transition":
            parts["transitions"].append(c)
        elif c.needs_review:
            parts["uncertain"].append(c)
        else:
            parts["rejected"].append(c)
    assert sum(len(v) for v in parts.values()) == len(items)
    return parts


# ---------------------------------------------------------------------------- shots vs transitions
@dataclass(frozen=True)
class Shot:
    index: int
    start_frame: int      # inclusive
    end_frame: int        # inclusive

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame + 1


@dataclass(frozen=True)
class Transition:
    kind: str             # fade_in | fade_out | dissolve
    start_frame: int      # inclusive
    end_frame: int        # inclusive
    candidate_id: str
    shot_before: Optional[int]
    shot_after: Optional[int]


def build_shots(total_frames: int, cuts: Iterable[int],
                transitions: Iterable[Tuple[str, int, int, str]] = ()) -> Tuple[Tuple[Shot, ...], Tuple[Transition, ...]]:
    """Turn accepted boundaries into shots and transitions. No minimum shot length is applied.

    ``cuts``: frame number of the first frame of the NEW shot (scdet attributes the change to that frame).
    ``transitions``: (kind, first_frame, last_frame, candidate_id); the frames of a transition belong to
    neither neighbouring shot. Overlapping or duplicate boundaries are an error, not silently merged.
    """
    if isinstance(total_frames, bool) or not isinstance(total_frames, int) or total_frames < 1:
        raise ClassifyError("total_frames must be a positive int")
    events: List[Tuple[int, int, str, Optional[Tuple[str, str]]]] = []
    for c in cuts:
        if isinstance(c, bool) or not isinstance(c, int) or not 1 <= c < total_frames:
            raise ClassifyError(f"cut frame {c!r} must be an int in [1, {total_frames - 1}]")
        events.append((c, c, "cut", None))
    for kind, s, e, cid in transitions:
        if kind not in ("fade_in", "fade_out", "dissolve"):
            raise ClassifyError(f"unknown transition kind {kind!r}")
        if not (isinstance(s, int) and isinstance(e, int) and 0 <= s <= e < total_frames):
            raise ClassifyError(f"transition {cid}: invalid range {s}-{e}")
        events.append((s, e, "transition", (kind, cid)))
    events.sort(key=lambda x: (x[0], x[1]))
    for a, b in zip(events, events[1:]):
        if b[0] <= a[1]:
            raise ClassifyError(f"boundaries overlap or repeat at frames {a[0]}-{a[1]} and {b[0]}-{b[1]}")

    shots: List[Shot] = []
    trans: List[Transition] = []
    pending: List[int] = []     # indexes into trans waiting for a shot_after
    cur = 0

    def add_shot(s: int, e: int) -> int:
        shots.append(Shot(len(shots), s, e))
        for pi in list(pending):
            if trans[pi].end_frame + 1 == s:
                t = trans[pi]
                trans[pi] = Transition(t.kind, t.start_frame, t.end_frame, t.candidate_id, t.shot_before, len(shots) - 1)
        pending.clear()
        return len(shots) - 1

    for s, e, typ, meta in events:
        if typ == "cut":
            if s - 1 < cur:
                raise ClassifyError(f"cut at frame {s} directly follows a transition and leaves no shot between them")
            add_shot(cur, s - 1)
            cur = s
        else:
            before = add_shot(cur, s - 1) if s - 1 >= cur else None
            trans.append(Transition(meta[0], s, e, meta[1], before, None))
            pending.append(len(trans) - 1)
            cur = e + 1
    if cur <= total_frames - 1:
        add_shot(cur, total_frames - 1)
    return tuple(shots), tuple(trans)
