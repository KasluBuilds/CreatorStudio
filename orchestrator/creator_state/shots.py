"""Shot-detection orchestration: preflight, plan, detect, verify, publish, recover.

Wires the pure layers (shot_metrics: FFmpeg arguments + parsers; shot_classify: rules) into a safe,
resumable job, following the conventions of proxy.py:

* Originals are only ever read (opened by FFmpeg/ffprobe as input). Nothing is written next to or over
  them. Every pass re-checks the source's size and modified time against the ingestion manifest.
* Nothing runs unless asked (``detect``); ``plan`` executes no process and writes nothing.
* No shell: every process is an argument list, run one at a time, at below-normal priority, with
  bounded FFmpeg threads and a time limit.
* Output goes into a hidden ``.<run_id>.part`` folder inside the media's shots folder. It is validated,
  then published with ONE directory rename to ``<run_id>/``. A run folder that already exists is never
  touched or replaced. Interrupted, failed or invalid runs never reach ``<run_id>/``: a clean failure is
  preserved under ``_failed/`` for diagnosis, a crash leaves only the ``.part`` folder, which is never
  treated as a result (``list_runs`` reports it as incomplete; the next attempt discards it).
* ``latest.json`` is updated (atomically) only after a successful publish.

Layout::

    Projects/<slug>/analysis/shots/<media_id>/<run_id>/
        shots.json  candidates.csv  frame_scores.csv  run.json  report.md  raw/  frames/
    Projects/<slug>/analysis/shots/<media_id>/latest.json   (pointer to the newest completed run)
    Projects/<slug>/analysis/shots/<media_id>/_failed/<run_id>_<n>/   (diagnostics of failed runs)

Passes (sequential, each a full decode of the first video stream): (A) scdet + signalstats metadata,
(B) blackdetect, (C) tiny gray thumbnails used for before/after similarity, then a few single-frame JPEG
evidence extractions. Three decodes is the price of keeping the pure layers unchanged.

STATUS: verified only on tiny synthetic clips (see tests/test_shots.py). Classification thresholds are
untuned heuristics; nothing here has been validated on real footage, and confidence values are not
calibrated probabilities. A 1-2 frame shot between two cuts cannot be told from a flash by these
signals and is kept as an uncertain candidate rather than reported as two cuts.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import math
import os
import shutil
import subprocess
import time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import ingest, media, shot_classify as sc, shot_metrics as sm, util
from .project import Project, StateError
from .schema import IN_PROGRESS

SHOTS_DIR = "analysis/shots"
STAGE = "understand"
SETTINGS_FILE = "shots_settings.json"
SCHEMA_VERSION = 1
LATEST_FILE = "latest.json"
FAILED_DIR = "_failed"

DEFAULT_SETTINGS: dict = {
    "candidate_floor": 5.0,        # scdet score at/above which a frame becomes a candidate (recall-first)
    "group_max_gap": 1,            # below-floor frames allowed inside one candidate group (2-frame flashes need 1)
    "scale_height": 360,           # analysis height for the scdet pass (None = source size); changes scores
    "threads": 2,                  # FFmpeg decoder threads (hard cap 8)
    "context_before": 8,           # frames of score/luma context kept before an event
    "context_after": 8,
    "return_offset": 2,            # frames after an event used to test 'returns to the previous look'
    "black_min_duration": 0.08,
    "black_picture_ratio": 0.98,
    "black_pixel": 0.10,
    "ramp_min_frames": 4,
    "ramp_min_total": 10.0,
    "ramp_min_step": 0.5,
    "ramp_max_flat": 1,
    "thumb_width": 32,
    "thumb_height": 18,
    "max_evidence_events": 40,     # events that get before/after JPEGs (0 = none)
    "evidence_height": 240,
    "pass_timeout_s": 3600,
    "classifier": {},              # overrides for shot_classify.Thresholds fields
}
_INT = {"group_max_gap": (0, 10), "threads": (1, 8), "context_before": (1, 120), "context_after": (1, 120),
        "return_offset": (1, 20), "ramp_min_frames": (2, 1000), "ramp_max_flat": (0, 10),
        "thumb_width": (8, 128), "thumb_height": (8, 128), "max_evidence_events": (0, 500),
        "evidence_height": (16, 2160), "pass_timeout_s": (10, 86400)}
_NUM = {"candidate_floor": (0.0, 100.0), "black_min_duration": (0.0, 60.0), "black_picture_ratio": (0.0, 1.0),
        "black_pixel": (0.0, 1.0), "ramp_min_total": (0.0, 255.0), "ramp_min_step": (0.0, 255.0)}

CONFIDENCE_NOTE = sc.CONFIDENCE_NOTE
VALIDATION_NOTE = ("Verified on tiny synthetic clips only; thresholds are untuned heuristics. "
                   "Not validated on real footage.")
EXPECTED_OUTPUTS = ("shots.json", "candidates.csv", "frame_scores.csv", "run.json", "report.md", "raw/", "frames/")


class ShotsError(StateError):
    """Invalid request or an unsafe/unsupported situation (nothing was run)."""


class ShotsRunError(Exception):
    """A running job failed; the message is recorded with the failed run's diagnostics."""


# ---------------------------------------------------------------------------- settings
def load_settings(project: Project, overrides: Optional[dict] = None) -> dict:
    """Defaults < <project>/shots_settings.json < explicit overrides. Validated; unknown keys refused."""
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    f = project.dir / SETTINGS_FILE
    if f.exists():
        try:
            s.update(json.loads(f.read_text(encoding="utf-8")))
        except ValueError:
            raise ShotsError(f"{SETTINGS_FILE} is not valid JSON")
    s.update({k: v for k, v in (overrides or {}).items()})
    return validate_settings(s)


def validate_settings(s: dict) -> dict:
    if not isinstance(s, dict):
        raise ShotsError("settings must be an object")
    for k in s:
        if k not in DEFAULT_SETTINGS:
            raise ShotsError(f"unknown shots setting '{k}'")
    for k, (lo, hi) in _INT.items():
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ShotsError(f"shots setting '{k}' must be an int in [{lo}, {hi}]")
    for k, (lo, hi) in _NUM.items():
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            raise ShotsError(f"shots setting '{k}' must be a number in [{lo}, {hi}]")
        s[k] = float(v)
    sh = s["scale_height"]
    if sh is not None and (isinstance(sh, bool) or not isinstance(sh, int) or not 16 <= sh <= 4320):
        raise ShotsError("shots setting 'scale_height' must be null or an int in [16, 4320]")
    cl = s["classifier"]
    if not isinstance(cl, dict):
        raise ShotsError("shots setting 'classifier' must be an object")
    fields = {f.name for f in dataclasses.fields(sc.Thresholds)}
    for k, v in cl.items():
        if k not in fields:
            raise ShotsError(f"unknown classifier threshold '{k}'")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ShotsError(f"classifier threshold '{k}' must be a finite number")
    return s


def settings_hash(s: dict) -> str:
    return util.sha256_text(util.canonical_json(s))[:8]


def thresholds_from(s: dict) -> sc.Thresholds:
    base = dataclasses.asdict(sc.DEFAULT_THRESHOLDS)
    for k, v in s["classifier"].items():
        base[k] = type(base[k])(v)
    return sc.Thresholds(**base)


# ---------------------------------------------------------------------------- identity and paths
def source_identity(item: dict) -> dict:
    f = item.get("file") or {}
    return {"path": item.get("path"), "size": f.get("size"), "mtime_ns": f.get("mtime_ns"),
            "sha256": f.get("sha256")}


def identity_hash(ident: dict) -> str:
    return util.sha256_text(util.canonical_json({k: ident[k] for k in ("size", "mtime_ns", "sha256")}))[:8]


def run_id_for(item: dict, settings: dict) -> str:
    """Deterministic: the same source identity and settings always map to the same run folder."""
    return f"r_{settings_hash(settings)}_{identity_hash(source_identity(item))}"


def media_dir(project: Project, media_id: str) -> Path:
    return project.dir / SHOTS_DIR / media_id


def _rel(project: Project, p: Path) -> str:
    return str(Path(p).relative_to(project.dir)).replace("\\", "/")


def _video_items(project: Project, media_ids=None) -> List[dict]:
    m = ingest.load_manifest(project)
    if m is None:
        raise ShotsError("no source_manifest.json; run ingest first")
    try:
        pid = project.load_state()["project_id"]
    except (StateError, KeyError) as e:
        raise ShotsError(f"project state is unusable: {e}")
    if m.get("project_id") != pid:
        raise ShotsError("source_manifest.json belongs to a different project (project id mismatch); refusing to analyse")
    items = [i for i in m["items"] if i.get("media_type") == "video"]
    if media_ids is not None:
        wanted = set(media_ids)
        known = {i["id"] for i in m["items"]}
        for w in wanted:
            if w not in known:
                raise ShotsError(f"media id '{w}' is not in the ingestion manifest")
        items = [i for i in items if i["id"] in wanted]
        missing = wanted - {i["id"] for i in items}
        if missing:
            raise ShotsError(f"media id(s) {sorted(missing)} are not videos")
    return items


# ---------------------------------------------------------------------------- preflight
def find_ffmpeg(explicit=None, which=shutil.which):
    cands = [c for c in (explicit, os.environ.get("CREATOR_FFMPEG"), which("ffmpeg")) if c]
    return cands[0] if cands else None


def preflight(project: Project, item: dict, settings: dict, prober: Optional[media.Prober], ffmpeg: Optional[str],
              *, probe: bool) -> dict:
    """Decide whether this video may be analysed. ``probe=False`` (planning) reads only the manifest and
    the file's stat; ``probe=True`` also re-probes the file with ffprobe. Never raises for a bad input:
    problems are returned so a plan can show them."""
    problems: List[str] = []
    warnings: List[str] = []
    out = {"ok": False, "problems": problems, "warnings": warnings, "fps": None, "expected_frames": None}
    path = item.get("path")
    if item.get("media_type") != "video":
        problems.append(f"not a video (media type '{item.get('media_type')}')")
    if item.get("status") != "ok":
        problems.append(f"manifest status is '{item.get('status')}'")
    if (item.get("probe") or {}).get("status") != "ok":
        problems.append("no successful ffprobe record in the manifest (re-run ingest with ffprobe available)")
    if not item.get("video"):
        problems.append("no probed video stream in the manifest")
    codes = {w.get("code") for w in item.get("warnings") or []}
    if "VARIABLE_FRAME_RATE" in codes:
        problems.append("variable frame rate suspected (VARIABLE_FRAME_RATE warning); frame-exact analysis refused")
    if "SYMLINK" in codes:
        warnings.append("source is a symbolic link")

    v = (item.get("video") or [None])[0]
    fps = None
    if v:
        fr = v.get("frame_rate") or {}
        if fr.get("kind") != "constant" or not fr.get("exact") or not fr.get("fps_rational"):
            problems.append(f"frame rate is not a verified constant rate (kind '{fr.get('kind')}')")
        else:
            try:
                fps = sm.parse_fps(fr["fps_rational"])
            except sm.MetricsError as e:
                problems.append(f"unusable frame rate: {e}")
        if len(item.get("video") or []) > 1:
            warnings.append("several video streams; only the first video stream is analysed")
        if v.get("rotation"):
            warnings.append(f"stream carries a {v['rotation']} degree rotation flag; analysis uses stored orientation")
    out["fps"] = fps

    f = item.get("file") or {}
    if f.get("size") is None or f.get("mtime_ns") is None:
        problems.append("manifest has no size/modified-time record for this file")
    if not isinstance(path, str) or not path or path.startswith("-") or not os.path.isabs(path):
        problems.append("source path is missing, relative or starts with '-'")
    else:
        try:
            st = os.stat(path)
            media.open_probe_read(path)
            if not os.path.isfile(path):
                problems.append("source is not a regular file")
            elif st.st_size != f.get("size") or st.st_mtime_ns != f.get("mtime_ns"):
                problems.append("source size or modified time differs from the ingestion manifest "
                                "(file changed or was replaced); re-run ingest and review before analysing")
        except FileNotFoundError:
            problems.append("source file is missing")
        except OSError as e:
            problems.append(f"source file is not accessible: {e.strerror or e}")

    if ffmpeg is None:
        problems.append("ffmpeg not found (install FFmpeg or set CREATOR_FFMPEG)")
    if prober is None or not prober.available:
        problems.append("ffprobe is not available")

    expected = None
    if v and fps:
        expected = v.get("nb_frames")
        if not expected:
            dur = v.get("duration_seconds") or (item.get("container") or {}).get("duration_seconds")
            if dur:
                expected = sm.expected_frame_count(dur, fps)
        if not expected:
            problems.append("duration and frame count unknown; cannot validate the analysis")
        elif expected < 2:
            problems.append("fewer than 2 frames; nothing to analyse")
    out["expected_frames"] = expected

    try:
        sm.escape_filter_path(str(media_dir(project, item.get("id", "x")) / "raw" / "analysis_metadata.txt"))
    except sm.MetricsError as e:
        problems.append(f"project path cannot be used in an FFmpeg filter: {e}")

    if probe and not problems:
        res = prober.probe(path)
        if res["status"] != "ok":
            problems.append(f"ffprobe could not read the source now: {res.get('error')}")
        else:
            parsed = media.parse_ffprobe(res["data"], "video")
            if not parsed["video"]:
                problems.append("ffprobe finds no video stream now")
            else:
                nv = parsed["video"][0]["frame_rate"]
                if nv["kind"] != "constant" or nv.get("fps_rational") != (v["frame_rate"]["fps_rational"]):
                    problems.append("frame rate now differs from the manifest or is not constant")
                if parsed["video"][0].get("nb_frames"):
                    out["expected_frames"] = parsed["video"][0]["nb_frames"]
    out["ok"] = not problems
    return out


# ---------------------------------------------------------------------------- plan (read-only)
def plan(project: Project, settings: dict, prober: Optional[media.Prober] = None, ffmpeg: Optional[str] = None,
         media_ids=None) -> List[dict]:
    """What ``detect`` would do, per video. Executes no process and writes nothing."""
    rows = []
    for it in _video_items(project, media_ids):
        pf = preflight(project, it, settings, prober, ffmpeg, probe=False)
        rid = run_id_for(it, settings) if (it.get("file") or {}).get("size") is not None else None
        mdir = media_dir(project, it["id"])
        row = {"id": it["id"], "name": it["name"], "source": it.get("path"), "source_identity": source_identity(it),
               "settings_hash": settings_hash(settings), "run_id": rid,
               "run_dir": _rel(project, mdir / rid) if rid else None, "action": "detect", "reason": "",
               "warnings": list(pf["warnings"]), "expected_frames": pf["expected_frames"],
               "fps": sm.fraction_to_str(pf["fps"]) if pf["fps"] else None,
               "passes": ["scdet+signalstats", "blackdetect", "thumbnails", "evidence_frames"],
               "expected_outputs": [f"{_rel(project, mdir / rid)}/{o}" for o in EXPECTED_OUTPUTS] if rid else []}
        if pf["problems"]:
            row.update(action="blocked", reason="; ".join(pf["problems"]))
        elif (mdir / rid).exists():
            row.update(action="exists", reason="a run with these settings and this source already exists; it will not be replaced")
        elif (mdir / f".{rid}.part").exists():
            row["warnings"].append("an incomplete earlier attempt (.part folder) exists and will be discarded when detection starts")
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------- process running
def default_runner(argv: Sequence[str], timeout: float, stdout_path: Optional[Path] = None):
    """Run one process: no shell, bounded time, below-normal priority. Returns (returncode, stderr bytes).
    ``stdout_path`` (optional) receives the process's stdout; otherwise stdout is discarded."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    else:
        kw["preexec_fn"] = lambda: os.nice(10)
    out = open(stdout_path, "xb") if stdout_path else None
    try:
        p = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=out if out else subprocess.DEVNULL,
                             stderr=subprocess.PIPE, **kw)
        try:
            _, err = p.communicate(timeout=timeout)
        except BaseException:           # timeout, Ctrl+C, anything: never leave the process running
            p.kill()
            p.communicate()
            raise
        return p.returncode, err
    finally:
        if out:
            out.close()


def _tail(b: bytes, n: int = 1500) -> str:
    return b.decode("utf-8", "replace")[-n:]


def frame_similarity(a: bytes, b: bytes) -> float:
    """Brightness-independent structural similarity of two equal-size gray thumbnails, 0..1.

    Pearson correlation of the pixels (so a pure brightness change keeps similarity high), floored at 0.
    Two flat frames count as the same only if their means are close; a flat and a textured frame differ.
    Flat content therefore cannot show a lighting change (documented limitation)."""
    if len(a) != len(b) or not a:
        raise ValueError("thumbnails must be equal-size and non-empty")
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a) / n
    vb = sum((x - mb) ** 2 for x in b) / n
    flat_a, flat_b = va < 4.0, vb < 4.0     # std < 2 gray levels
    if flat_a and flat_b:
        return 1.0 if abs(ma - mb) < 8.0 else 0.0
    if flat_a or flat_b:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / n
    return max(0.0, min(1.0, cov / math.sqrt(va * vb)))


class Thumbs:
    """Random access to the raw gray thumbnail file written by pass C."""

    def __init__(self, path: Path, width: int, height: int):
        self.path, self.size = Path(path), width * height
        self.count = self.path.stat().st_size // self.size if self.size else 0
        self.exact = self.path.stat().st_size == self.count * self.size

    def get(self, i: int) -> Optional[bytes]:
        if not 0 <= i < self.count:
            return None
        with open(self.path, "rb") as f:
            f.seek(i * self.size)
            return f.read(self.size)


def _dec6(x: Fraction) -> str:
    return format(Decimal(x.numerator) / Decimal(x.denominator), ".6f")


# ---------------------------------------------------------------------------- pure derivation
def _black_frames(intervals, fps) -> List[Tuple[int, Optional[int]]]:
    out = []
    for b in intervals:
        s = sm.seconds_to_frame(b.start, fps)
        e = sm.seconds_to_frame(b.end, fps) if b.end is not None else None
        out.append((s, e))
    return out


def derive(scd_frames, sig_frames, black_intervals, thumbs, settings: dict, fps: Fraction) -> dict:
    """Candidates -> evidence -> classifications -> shots/transitions. Pure and deterministic: no process
    and no clock; reads thumbnails only through ``thumbs.get``."""
    total = len(scd_frames)
    th = thresholds_from(settings)
    score = [f.score for f in scd_frames]
    luma = [f.values["YAVG"] for f in sig_frames]
    groups = sm.group_candidates(scd_frames, settings["candidate_floor"], max_gap=settings["group_max_gap"])
    ramps = sm.find_luma_ramps(luma, base_frame=0, min_frames=settings["ramp_min_frames"],
                               min_total=settings["ramp_min_total"], min_step=settings["ramp_min_step"],
                               max_flat=settings["ramp_max_flat"])
    spans = [(g.first_frame, g.last_frame, "scdet_group") for g in groups] + \
            [(r.first_frame, r.last_frame, "luma_ramp") for r in ramps]
    spans.sort()
    events: List[dict] = []
    for first, last, kind in spans:               # merge overlapping spans; keep every constituent
        if events and first <= events[-1]["last"]:
            e = events[-1]
            e["last"] = max(e["last"], last)
            e["members"].append({"kind": kind, "first": first, "last": last})
        else:
            events.append({"first": first, "last": last, "members": [{"kind": kind, "first": first, "last": last}]})
    blacks = _black_frames(black_intervals, fps)
    evidence: List[sc.Evidence] = []
    meta: Dict[str, dict] = {}
    for n, e in enumerate(events, 1):
        cid = f"c{n:04d}"
        first, last = e["first"], e["last"]
        ws, win_luma = sm.context_window(luma, first, last, settings["context_before"], settings["context_after"])
        _, win_score = sm.context_window(score, first, last, settings["context_before"], settings["context_after"])
        peak = max(score[first:last + 1])
        before, after = first - 1, last + 1
        ret = after + settings["return_offset"]
        sim_across = sim_return = None
        tb = thumbs.get(before) if before >= 0 else None
        ta = thumbs.get(after) if after < total else None
        tr = thumbs.get(ret) if ret < total else None
        if tb is not None and ta is not None:
            sim_across = round(frame_similarity(tb, ta), 4)
        if tb is not None and tr is not None:
            sim_return = round(frame_similarity(tb, tr), 4)
        into_black = any(first <= s <= last + 3 for s, _ in blacks)
        out_of_black = any(en is not None and first - 3 <= en <= last for _, en in blacks)
        kinds = sorted({m["kind"] for m in e["members"]})
        evidence.append(sc.Evidence(cid, first, last, peak, scores=win_score, luma=win_luma, window_start=ws,
                                    sim_across=sim_across, sim_return=sim_return, into_black=into_black,
                                    out_of_black=out_of_black, source="+".join(kinds)))
        meta[cid] = {"members": e["members"], "peak_score": peak, "sim_across": sim_across,
                     "sim_return": sim_return, "into_black": into_black, "out_of_black": out_of_black}
    classified = list(sc.classify_all(evidence, th))
    classified = _resolve_conflicts(classified, total)
    cuts = sorted(c.first_frame for c in classified if c.boundary_kind == "cut")
    trans_in = [(c.category, c.first_frame, c.last_frame, c.candidate_id) for c in classified
                if c.boundary_kind == "transition"]
    shots, transitions = sc.build_shots(total, cuts, trans_in)
    return {"total_frames": total, "classifications": classified, "meta": meta, "shots": shots,
            "transitions": transitions, "ramps": len(ramps), "groups": len(groups)}


def _demote(c: sc.Classification, why: str) -> sc.Classification:
    return dataclasses.replace(
        c, category="uncertain", boundary_kind=None, rejected=False, needs_review=True,
        confidence=min(c.confidence, 0.2), confidence_basis="demoted by orchestration because boundaries conflict; fixed cap 0.2",
        rule="ORCH_conflict", reasons=c.reasons + (f"originally classified {c.category} (confidence {c.confidence}); {why}",))


def _resolve_conflicts(items: List[sc.Classification], total: int) -> List[sc.Classification]:
    """Make the accepted boundaries mutually consistent without discarding anything: a conflicting
    candidate becomes 'uncertain' with the reason recorded."""
    out = list(items)
    kept: List[sc.Classification] = []
    for i, c in sorted(((i, c) for i, c in enumerate(out) if c.boundary_kind == "transition"),
                       key=lambda t: (t[1].first_frame, t[1].last_frame)):
        if kept and c.first_frame <= kept[-1].last_frame:
            out[i] = _demote(c, f"overlaps transition {kept[-1].candidate_id}")
        else:
            kept.append(c)
    for i, c in enumerate(out):
        if c.boundary_kind != "cut":
            continue
        if not 1 <= c.first_frame < total:
            out[i] = _demote(c, "cut position outside the stream")
            continue
        for t in kept:
            if t.first_frame <= c.first_frame <= t.last_frame + 1:
                out[i] = _demote(c, f"falls inside or directly after transition {t.candidate_id}")
                break
    return out


# ---------------------------------------------------------------------------- file writers
def _csv_text(header: Sequence[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _frame_scores_csv(scd_frames, sig_frames, fps: Fraction) -> str:
    rows = []
    for s, g in zip(scd_frames, sig_frames):
        rows.append([s.frame, _dec6(sm.frame_to_seconds(s.frame, fps)), s.pts_time, s.mafd, s.score,
                     g.values.get("YAVG", ""), g.values.get("YDIF", "")])
    return _csv_text(["frame", "time_seconds", "pts_time_raw", "scdet_mafd", "scdet_score", "yavg", "ydif"], rows)


def _candidates_csv(derived: dict, fps: Fraction) -> str:
    rows = []
    for c in derived["classifications"]:
        m = derived["meta"][c.candidate_id]
        rows.append([c.candidate_id, c.first_frame, c.last_frame, _dec6(sm.frame_to_seconds(c.first_frame, fps)),
                     _dec6(sm.frame_to_seconds(c.last_frame + 1, fps)), m["peak_score"], "+".join(sorted({x["kind"] for x in m["members"]})),
                     c.category, c.boundary_kind or "", c.rejected, c.needs_review, c.confidence, c.rule,
                     m["sim_across"] if m["sim_across"] is not None else "", m["sim_return"] if m["sim_return"] is not None else "",
                     m["into_black"], m["out_of_black"], " | ".join(c.reasons)])
    return _csv_text(["candidate_id", "first_frame", "last_frame", "start_time_seconds", "end_time_seconds", "peak_score",
                      "source", "category", "boundary_kind", "rejected", "needs_review", "confidence", "rule", "sim_across",
                      "sim_return", "into_black", "out_of_black", "reasons"], rows)


def _cand_json(c: sc.Classification, derived: dict, fps: Fraction, evidence: Dict[str, List[str]]) -> dict:
    m = derived["meta"][c.candidate_id]
    d = c.to_dict()
    d.pop("confidence_note", None)
    d.update(start_time=_dec6(sm.frame_to_seconds(c.first_frame, fps)), end_time=_dec6(sm.frame_to_seconds(c.last_frame + 1, fps)),
             detection_sources=sorted({x["kind"] for x in m["members"]}), peak_score=m["peak_score"],
             sim_across=m["sim_across"], sim_return=m["sim_return"], into_black=m["into_black"], out_of_black=m["out_of_black"],
             evidence_frames=evidence.get(c.candidate_id, []))
    return d


def _shots_json(item: dict, ident: dict, fps: Fraction, derived: dict, settings: dict, tools: dict,
                evidence: Dict[str, List[str]], warnings: List[str]) -> dict:
    cl = {c.candidate_id: c for c in derived["classifications"]}
    by_cut = {c.first_frame: c for c in derived["classifications"] if c.boundary_kind == "cut"}
    tr_by_start = {t.start_frame: t for t in derived["transitions"]}
    tr_by_end = {t.end_frame + 1: t for t in derived["transitions"]}
    total = derived["total_frames"]
    shots = []
    for s in derived["shots"]:
        start_b = by_cut.get(s.start_frame)
        end_b = by_cut.get(s.end_frame + 1)
        start_t = tr_by_end.get(s.start_frame)
        end_t = tr_by_start.get(s.end_frame + 1)
        refs = []
        for kind, ref in (("start", start_b or (cl.get(start_t.candidate_id) if start_t else None)),
                          ("end", end_b or (cl.get(end_t.candidate_id) if end_t else None))):
            refs.append(ref)
        confs = [r.confidence for r in refs if r is not None]
        shots.append({
            "shot_id": f"s{s.index + 1:04d}", "start_frame": s.start_frame, "end_frame": s.end_frame,
            "start_time": _dec6(sm.frame_to_seconds(s.start_frame, fps)),
            "end_time": _dec6(sm.frame_to_seconds(s.end_frame + 1, fps)),
            "duration": _dec6(sm.frame_to_seconds(s.length, fps)), "frames": s.length,
            "detection_method": "scdet+signalstats+thumbnail_similarity+blackdetect",
            "start_boundary": (refs[0].candidate_id if refs[0] else ("stream_start" if s.start_frame == 0 else None)),
            "end_boundary": (refs[1].candidate_id if refs[1] else ("stream_end" if s.end_frame == total - 1 else None)),
            "confidence": round(min(confs), 2) if confs else None,
            "confidence_basis": ("weakest bounding boundary (heuristic)" if confs else
                                 "no bounding boundary was detected; the shot spans the whole analysed stream or has only stream edges"),
            "evidence": sorted({p for r in refs if r for p in evidence.get(r.candidate_id, [])}),
        })
    parts = sc.partition(derived["classifications"])
    return {
        "schema_version": SCHEMA_VERSION, "media_id": item["id"], "source": {**ident, "name": item["name"]},
        "fps": sm.fraction_to_str(fps), "fps_float": float(fps), "total_frames": total,
        "duration": _dec6(sm.frame_to_seconds(total, fps)),
        "time_convention": "times are exact frame boundaries computed from the rational frame rate; end_time and end_frame+1 are exclusive",
        "detector": {"tools": tools, "settings": settings, "settings_hash": settings_hash(settings)},
        "confidence_note": CONFIDENCE_NOTE, "validation_note": VALIDATION_NOTE,
        "shots": shots,
        "transitions": [{"transition_id": f"t{i + 1:04d}", "candidate_id": t.candidate_id, "kind": t.kind,
                         "start_frame": t.start_frame, "end_frame": t.end_frame,
                         "start_time": _dec6(sm.frame_to_seconds(t.start_frame, fps)),
                         "end_time": _dec6(sm.frame_to_seconds(t.end_frame + 1, fps)),
                         "shot_before": (f"s{t.shot_before + 1:04d}" if t.shot_before is not None else None),
                         "shot_after": (f"s{t.shot_after + 1:04d}" if t.shot_after is not None else None),
                         "confidence": cl[t.candidate_id].confidence, "reasons": list(cl[t.candidate_id].reasons),
                         "evidence_frames": evidence.get(t.candidate_id, [])}
                        for i, t in enumerate(derived["transitions"])],
        "cuts": [_cand_json(c, derived, fps, evidence) for c in parts["cuts"]],
        "rejected_candidates": [_cand_json(c, derived, fps, evidence) for c in parts["rejected"]],
        "uncertain_candidates": [_cand_json(c, derived, fps, evidence) for c in parts["uncertain"]],
        "summary": {"shots": len(derived["shots"]), "cuts": len(parts["cuts"]), "transitions": len(parts["transitions"]),
                    "rejected": len(parts["rejected"]), "uncertain": len(parts["uncertain"]),
                    "candidates_total": len(derived["classifications"]), "candidate_groups": derived["groups"],
                    "luma_ramps": derived["ramps"]},
        "warnings": warnings,
    }


def _report_md(item: dict, run_id: str, doc: dict, run_info: dict) -> str:
    s = doc["summary"]
    lines = [f"# Shot detection report: {item['name']}", "",
             f"Run `{run_id}` for media `{item['id']}`. {VALIDATION_NOTE}", "",
             f"- Frames analysed: {doc['total_frames']} at {doc['fps']} fps ({doc['duration']} s)",
             f"- Shots: {s['shots']}; hard cuts: {s['cuts']}; transitions: {s['transitions']}; "
             f"rejected events: {s['rejected']}; uncertain candidates: {s['uncertain']}", "",
             f"{CONFIDENCE_NOTE}", ""]
    if doc["transitions"]:
        lines += ["## Transitions"] + [f"- {t['kind']} frames {t['start_frame']}-{t['end_frame']} "
                                       f"({t['start_time']}-{t['end_time']} s)" for t in doc["transitions"]] + [""]
    if doc["cuts"]:
        lines += ["## Hard cuts"] + [f"- frame {c['first_frame']} ({c['start_time']} s), confidence {c['confidence']}" for c in doc["cuts"]] + [""]
    if doc["rejected_candidates"]:
        lines += ["## Rejected events (kept for audit)"] + [f"- frames {c['first_frame']}-{c['last_frame']}: {c['category']} "
                                                          f"({'; '.join(c['reasons'])})" for c in doc["rejected_candidates"]] + [""]
    if doc["uncertain_candidates"]:
        lines += ["## Uncertain candidates (need review)"] + [f"- frames {c['first_frame']}-{c['last_frame']}: "
                                                              f"{'; '.join(c['reasons'])}" for c in doc["uncertain_candidates"]] + [""]
    if doc["warnings"]:
        lines += ["## Warnings"] + [f"- {w}" for w in doc["warnings"]] + [""]
    lines += ["## Processes"] + [f"- {p['name']}: exit {p['returncode']}, {p['seconds']:.1f} s" for p in run_info["processes"]] + [""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- verification
def _sha_tree(root: Path, skip=("run.json",)) -> Dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in skip and not p.name.endswith(".tmp"):
            out[p.relative_to(root).as_posix()] = util.sha256_file(p)
    return out


def verify_run_dir(root: Path, check_hashes: bool = True, require_run: bool = True) -> List[str]:
    """Structural checks on a run folder (part or published). Returns problems; empty = valid.
    ``require_run=False`` is used before run.json exists (it is written last, and records the hashes)."""
    probs: List[str] = []
    root = Path(root)
    for name in ("shots.json", "candidates.csv", "frame_scores.csv", "report.md") + (("run.json",) if require_run else ()):
        p = root / name
        if not p.is_file() or p.stat().st_size == 0:
            probs.append(f"{name} missing or empty")
    for d in ("raw", "frames"):
        if not (root / d).is_dir():
            probs.append(f"{d}/ missing")
    if probs:
        return probs
    try:
        doc = json.loads((root / "shots.json").read_text(encoding="utf-8"))
        run = json.loads((root / "run.json").read_text(encoding="utf-8")) if require_run else None
    except ValueError as e:
        return [f"invalid JSON: {e}"]
    total = doc.get("total_frames")
    segs = [(s["start_frame"], s["end_frame"], "shot " + s["shot_id"]) for s in doc.get("shots", [])] + \
           [(t["start_frame"], t["end_frame"], "transition " + t["transition_id"]) for t in doc.get("transitions", [])]
    segs.sort()
    if not isinstance(total, int) or total < 1:
        probs.append("total_frames invalid")
    else:
        expect = 0
        for a, b, who in segs:
            if a != expect or b < a:
                probs.append(f"{who} frames {a}-{b} do not continue from frame {expect} (gap or overlap)")
                break
            expect = b + 1
        else:
            if expect != total:
                probs.append(f"shots and transitions cover {expect} of {total} frames")
    ids = [s["shot_id"] for s in doc.get("shots", [])]
    if len(ids) != len(set(ids)):
        probs.append("duplicate shot ids")
    n_c = sum(len(doc.get(k, [])) for k in ("cuts", "rejected_candidates", "uncertain_candidates")) + len(doc.get("transitions", []))
    rows = (root / "candidates.csv").read_text(encoding="utf-8").splitlines()
    if len(rows) - 1 != n_c:
        probs.append(f"candidates.csv has {len(rows) - 1} rows but shots.json lists {n_c} candidates")
    fr = (root / "frame_scores.csv").read_text(encoding="utf-8").splitlines()
    if isinstance(total, int) and len(fr) - 1 != total:
        probs.append(f"frame_scores.csv has {len(fr) - 1} rows, expected {total}")
    if run is not None and run.get("status") != "completed":
        probs.append(f"run.json status is '{run.get('status')}'")
    if check_hashes and run is not None:
        listed = run.get("output_sha256") or {}
        actual = _sha_tree(root)
        if listed != actual:
            diff = sorted(set(listed) ^ set(actual)) or sorted(k for k in listed if listed[k] != actual.get(k))
            probs.append(f"file hashes differ from run.json: {diff[:5]}")
    return probs


# ---------------------------------------------------------------------------- detect
def _source_unchanged(item: dict) -> Optional[str]:
    f = item["file"]
    try:
        st = os.stat(item["path"])
    except OSError as e:
        return f"source no longer accessible: {e.strerror or e}"
    if st.st_size != f["size"] or st.st_mtime_ns != f["mtime_ns"]:
        return "source changed while being processed (size or modified time differs from the manifest)"
    return None


def _journal_active(project: Project) -> bool:
    try:
        return project.load_state()["stages"][STAGE]["status"] == IN_PROGRESS
    except (KeyError, StateError):
        return False


def _discard_own_part(mdir: Path, part: Path) -> None:
    """Remove our own leftover .part folder (named exactly .<run_id>.part inside this media's folder)."""
    if part.parent != mdir or not (part.name.startswith(".r_") and part.name.endswith(".part")):
        raise ShotsError(f"refusing to delete unexpected path {part}")
    if part.exists():
        shutil.rmtree(part)


def _preserve_failure(mdir: Path, part: Path, run_id: str, reason: str, processes: list) -> Optional[Path]:
    """Move a failed attempt's folder to _failed/ with a failure.json. Never raises; returns the new path."""
    try:
        fdir = mdir / FAILED_DIR
        fdir.mkdir(exist_ok=True)
        n = 1
        while (fdir / f"{run_id}_{n}").exists():
            n += 1
        dest = fdir / f"{run_id}_{n}"
        os.rename(part, dest)
        util.atomic_write_text(dest / "failure.json", util.pretty_json({
            "status": "failed", "run_id": run_id, "reason": reason, "processes": processes,
            "note": "NOT A VALID RESULT. Kept for diagnosis only; nothing here was published."}))
        return dest
    except OSError:
        return None


def detect(project: Project, settings: dict, prober: media.Prober, ffmpeg: Optional[str], media_ids=None,
           runner: Optional[Callable] = None, progress: Optional[Callable] = None, journal: str = "auto") -> List[dict]:
    """Analyse each selected video, one at a time. Explicit call only. Returns one result per video:
    status completed | skipped | blocked | failed."""
    runner = runner or default_runner
    progress = progress or (lambda m: None)
    results = []
    for item in _video_items(project, media_ids):
        res = {"id": item["id"], "name": item["name"], "run_id": None, "status": "failed", "reason": "", "run_dir": None}
        results.append(res)
        pf = preflight(project, item, settings, prober, ffmpeg, probe=True)
        if not pf["ok"]:
            res.update(status="blocked", reason="; ".join(pf["problems"]))
            project.log.event("shots.blocked", f"{item['name']}: {res['reason']}", "WARN", stage=STAGE)
            continue
        rid = run_id_for(item, settings)
        mdir = media_dir(project, item["id"])
        final, part = mdir / rid, mdir / f".{rid}.part"
        res.update(run_id=rid, run_dir=_rel(project, final))
        if final.exists():
            res.update(status="skipped", reason="a run with these settings and this source already exists; not replaced")
            continue
        use_journal = journal == "always" or (journal == "auto" and _journal_active(project))
        op_id = f"shots-{item['id']}-{rid}"
        try:
            mdir.mkdir(parents=True, exist_ok=True)
            if part.exists():
                project.log.event("shots.discard_part", f"Discarding incomplete earlier attempt {part.name}", "WARN", stage=STAGE)
                _discard_own_part(mdir, part)
            if use_journal and project.begin_operation(STAGE, op_id, f"shot detection for {item['name']} ({rid})") == "already_done":
                raise ShotsError("the operation journal says this run completed but its folder is missing; investigate before re-running")
        except ShotsError as e:
            res.update(status="blocked", reason=str(e))
            continue
        except OSError as e:
            res.update(status="failed", reason=f"cannot prepare output folder: {e}")
            continue
        processes: List[dict] = []
        project.log.event("shots.started", f"Shot detection for {item['name']} -> {_rel(project, final)}", stage=STAGE)
        try:
            part.mkdir()
            (part / "raw").mkdir()
            (part / "frames").mkdir()
            _execute(project, item, settings, pf, rid, part, final, mdir, ffmpeg, prober, runner, progress, processes, res)
            res.update(status="completed", reason="")
            if use_journal:
                project.complete_operation(STAGE, op_id, f"published {_rel(project, final)}")
            project.log.event("shots.completed", f"Shot detection finished: {_rel(project, final)} ({res.get('summary')})", stage=STAGE)
        except BaseException as e:           # includes KeyboardInterrupt: never publish, always preserve diagnostics
            reason = (f"interrupted: {type(e).__name__}" if not isinstance(e, Exception) else
                      (str(e) if isinstance(e, (ShotsRunError, ShotsError)) else f"{type(e).__name__}: {e}"))
            kept = _preserve_failure(mdir, part, rid, reason, processes) if part.exists() else None
            res.update(status="failed", reason=reason, run_dir=None, diagnostics=_rel(project, kept) if kept else None)
            if use_journal:
                try:
                    project.fail_operation(STAGE, op_id, reason)
                except StateError:
                    pass
            project.log.event("shots.failed", f"{item['name']}: {reason}", "ERROR", stage=STAGE)
            if not isinstance(e, Exception):
                raise
    return results


def _run_pass(name: str, argv: List[str], timeout: int, runner, processes: list, part: Path,
              stdout_path: Optional[Path] = None, stderr_name: Optional[str] = None) -> bytes:
    t0 = time.monotonic()
    rec = {"name": name, "argv": argv, "returncode": None, "seconds": 0.0}
    processes.append(rec)
    try:
        rc, err = runner(argv, timeout, stdout_path) if stdout_path else runner(argv, timeout)
    except subprocess.TimeoutExpired:
        rec.update(returncode="timeout", seconds=time.monotonic() - t0)
        raise ShotsRunError(f"{name}: FFmpeg did not finish within {timeout} s and was stopped")
    except OSError as e:
        rec.update(returncode="not_started", seconds=time.monotonic() - t0)
        raise ShotsRunError(f"{name}: could not start FFmpeg: {e}")
    rec.update(returncode=rc, seconds=time.monotonic() - t0, stderr_tail=_tail(err, 600))
    if stderr_name:
        util.write_new_text(part / "raw" / stderr_name, err.decode("utf-8", "replace"))
    if rc != 0:
        raise ShotsRunError(f"{name}: FFmpeg exited with code {rc}: {_tail(err, 300).strip()}")
    return err


def _extract_frame(ffmpeg: str, src: str, frame: int, fps: Fraction, out: Path, height: int, timeout: int, runner) -> Optional[str]:
    """One single-frame JPEG (mid-frame time so the intended frame is chosen). Returns an error text or None."""
    t = _dec6((Fraction(frame) + Fraction(1, 2)) / fps)
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-n", "-ss", t, "-i", src, "-map", "0:v:0",
            "-frames:v", "1", "-an", "-vf", f"scale=-2:'min({height},ih)'", "-q:v", "3", str(out)]
    try:
        rc, err = runner(argv, timeout)
    except subprocess.TimeoutExpired:
        return "timed out"
    except OSError as e:
        return f"could not start: {e}"
    if rc != 0:
        return f"exit {rc}: {_tail(err, 200).strip()}"
    if not out.is_file() or out.stat().st_size == 0:
        return "no image produced"
    return None


def _evidence_targets(derived: dict, limit: int) -> List[Tuple[str, str, int]]:
    """(candidate_id, label, frame) in a fixed priority order: cuts, transitions, then uncertain; capped."""
    n = derived["total_frames"]
    order = {"cut": 0, "transition": 1}
    cls = sorted([c for c in derived["classifications"] if c.boundary_kind or c.needs_review],
                 key=lambda c: (order.get(c.boundary_kind, 2), c.first_frame))[:limit]
    out = []
    for c in cls:
        if c.first_frame - 1 >= 0:
            out.append((c.candidate_id, "before", c.first_frame - 1))
        if c.boundary_kind == "transition":
            out.append((c.candidate_id, "mid", (c.first_frame + c.last_frame) // 2))
        if c.last_frame + 1 < n:
            out.append((c.candidate_id, "after", c.last_frame + 1))
    return out


def _execute(project: Project, item: dict, settings: dict, pf: dict, rid: str, part: Path, final: Path, mdir: Path,
             ffmpeg: str, prober: media.Prober, runner, progress, processes: list, res: dict) -> None:
    fps: Fraction = pf["fps"]
    src = item["path"]
    ident = source_identity(item)
    raw = part / "raw"
    to = settings["pass_timeout_s"]
    warnings: List[str] = list(pf["warnings"])

    def guard():
        bad = _source_unchanged(item)
        if bad:
            raise ShotsRunError(bad)

    guard()
    _run_pass("ffmpeg_version", [ffmpeg, "-version"], 30, runner, processes, part, stdout_path=raw / "ffmpeg_version.txt")
    vfile = raw / "ffmpeg_version.txt"
    version = vfile.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip() if vfile.is_file() and vfile.stat().st_size else "unknown"
    tools = {"ffmpeg": version, "ffprobe": prober.version}

    # Pass A: scdet + signalstats -> per-frame metadata
    progress(f"{item['name']}: pass A (scdet + signalstats)")
    meta_path = raw / "analysis_metadata.txt"
    argv = sm.build_analysis_argv(ffmpeg, src, str(meta_path), threshold=settings["candidate_floor"],
                                  scale_height=settings["scale_height"], threads=settings["threads"], signalstats=True)
    _run_pass("pass_a_scdet_signalstats", argv, to, runner, processes, part, stderr_name="pass_a_stderr.txt")
    guard()
    if not meta_path.is_file():
        raise ShotsRunError("pass A produced no metadata file")
    text = meta_path.read_text(encoding="ascii", errors="replace")
    scd, sig = sm.parse_scdet(text), sm.parse_signalstats(text)
    if not scd.ok:
        raise ShotsRunError(f"scdet output unusable ({scd.status}): {'; '.join(scd.problems[:3])}")
    if not sig.ok:
        raise ShotsRunError(f"signalstats output unusable ({sig.status}): {'; '.join(sig.problems[:3])}")
    n = len(scd.frames)
    if scd.frames[0].frame != 0 or [f.frame for f in sig.frames] != [f.frame for f in scd.frames]:
        raise ShotsRunError("scdet and signalstats frame numbering disagree or does not start at 0")
    expected = pf["expected_frames"]
    tol = max(2, math.ceil(0.002 * expected))
    if abs(n - expected) > tol:
        raise ShotsRunError(f"analysed {n} frames but the source should have about {expected}; the decode may be incomplete")

    # Pass B: black intervals
    progress(f"{item['name']}: pass B (blackdetect)")
    argv = sm.build_blackdetect_argv(ffmpeg, src, min_duration=settings["black_min_duration"],
                                     picture_black_ratio=settings["black_picture_ratio"], pixel_black=settings["black_pixel"],
                                     scale_height=settings["scale_height"], threads=settings["threads"])
    err = _run_pass("pass_b_blackdetect", argv, to, runner, processes, part, stderr_name="pass_b_blackdetect_log.txt")
    guard()
    black = sm.parse_blackdetect_log(err.decode("utf-8", "replace"))
    if not black.ok:
        raise ShotsRunError(f"blackdetect output unusable ({black.status}): {'; '.join(black.problems[:3])}")

    # Pass C: tiny gray thumbnails for before/after similarity
    progress(f"{item['name']}: pass C (thumbnails)")
    w, h = settings["thumb_width"], settings["thumb_height"]
    tpath = raw / "thumbs_gray.bin"
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-threads", str(settings["threads"]), "-i", src,
            "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "1", "-vf", f"scale={w}:{h}:flags=area,format=gray",
            "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"]
    _run_pass("pass_c_thumbnails", argv, to, runner, processes, part, stdout_path=tpath, stderr_name="pass_c_stderr.txt")
    guard()
    if not tpath.is_file():
        raise ShotsRunError("pass C produced no thumbnail file")
    thumbs = Thumbs(tpath, w, h)
    if not thumbs.exact or thumbs.count != n:
        raise ShotsRunError(f"thumbnail data holds {tpath.stat().st_size} bytes; expected {n} frames of {w * h} bytes")

    try:
        derived = derive(scd.frames, sig.frames, black.intervals, thumbs, settings, fps)
    except (sc.ClassifyError, sm.MetricsError, ValueError) as e:
        raise ShotsRunError(f"classification failed: {e}")

    # Evidence frames (non-fatal: a missing image is a warning, not a failed run)
    progress(f"{item['name']}: evidence frames")
    evidence: Dict[str, List[str]] = {}
    failed_imgs = 0
    t0 = time.monotonic()
    targets = _evidence_targets(derived, settings["max_evidence_events"])
    for cid, label, frame in targets:
        rel = f"frames/{cid}_{label}_f{frame:06d}.jpg"
        e = _extract_frame(ffmpeg, src, frame, fps, part / rel, settings["evidence_height"], to, runner)
        if e:
            failed_imgs += 1
            warnings.append(f"evidence frame {rel} not produced: {e}")
        else:
            evidence.setdefault(cid, []).append(rel)
    if targets:
        processes.append({"name": "evidence_frames", "argv": "[ffmpeg ... -frames:v 1 ...] x%d" % len(targets), "returncode": 0 if not failed_imgs else "partial",
                          "seconds": time.monotonic() - t0, "failed": failed_imgs})
    guard()

    # Write results (exclusive creates), verify, then record and re-verify
    doc = _shots_json(item, ident, fps, derived, settings, tools, evidence, warnings)
    util.write_new_text(part / "shots.json", util.pretty_json(doc))
    util.write_new_text(part / "candidates.csv", _candidates_csv(derived, fps))
    util.write_new_text(part / "frame_scores.csv", _frame_scores_csv(scd.frames, sig.frames, fps))
    run_info = {"processes": processes}
    util.write_new_text(part / "report.md", _report_md(item, rid, doc, run_info))
    probs = verify_run_dir(part, check_hashes=False, require_run=False)
    if probs:
        raise ShotsRunError("output verification failed: " + "; ".join(probs))
    run = {"schema_version": SCHEMA_VERSION, "run_id": rid, "status": "completed", "media_id": item["id"],
           "created_at": util.utcnow(), "settings": settings, "settings_hash": settings_hash(settings),
           "source_before": ident, "source_after": source_identity(item), "tools": tools,
           "frames_analysed": n, "expected_frames": expected, "fps": sm.fraction_to_str(fps),
           "processes": processes, "warnings": warnings, "summary": doc["summary"],
           "verification": {"structure_checked_before_hashing": True},
           "output_sha256": _sha_tree(part), "confidence_note": CONFIDENCE_NOTE, "validation_note": VALIDATION_NOTE}
    util.write_new_text(part / "run.json", util.pretty_json(run))
    probs = verify_run_dir(part, check_hashes=True, require_run=True)
    if probs:
        raise ShotsRunError("final verification failed: " + "; ".join(probs))
    guard()
    if final.exists():
        raise ShotsRunError("the run folder appeared while processing; it was not replaced")
    os.rename(part, final)                      # the single atomic publish step
    util.atomic_write_text(mdir / LATEST_FILE, util.pretty_json(
        {"run_id": rid, "run_dir": _rel(project, final), "published_at": util.utcnow(), "settings_hash": settings_hash(settings)}))
    res["summary"] = doc["summary"]


# ---------------------------------------------------------------------------- inspection and recovery
def list_runs(project: Project, media_id: Optional[str] = None) -> List[dict]:
    """Every folder under analysis/shots, classified. Read-only. Only state 'complete' is a result."""
    root = project.dir / SHOTS_DIR
    rows: List[dict] = []
    if not root.is_dir():
        return rows
    for mdir in sorted(p for p in root.iterdir() if p.is_dir()):
        if media_id and mdir.name != media_id:
            continue
        latest = None
        lf = mdir / LATEST_FILE
        if lf.is_file():
            try:
                latest = json.loads(lf.read_text(encoding="utf-8")).get("run_id")
            except ValueError:
                latest = None
        for p in sorted(mdir.iterdir()):
            base = {"media_id": mdir.name, "path": _rel(project, p)}
            if p.is_dir() and p.name.startswith(".r_") and p.name.endswith(".part"):
                rows.append({**base, "run_id": p.name[1:-5], "state": "incomplete",
                             "note": "interrupted or crashed run; never a result; discarded by the next attempt"})
            elif p.is_dir() and p.name == FAILED_DIR:
                for f in sorted(x for x in p.iterdir() if x.is_dir()):
                    rows.append({"media_id": mdir.name, "path": _rel(project, f), "run_id": f.name.rsplit("_", 1)[0],
                                 "state": "failed", "note": "diagnostics only; not a result"})
            elif p.is_dir() and p.name.startswith("r_"):
                row = {**base, "run_id": p.name, "state": "invalid", "latest": p.name == latest}
                if (p / "run.json").is_file():
                    try:
                        run = json.loads((p / "run.json").read_text(encoding="utf-8"))
                        if run.get("status") == "completed":
                            row.update(state="complete", summary=run.get("summary"), created_at=run.get("created_at"))
                    except ValueError:
                        pass
                rows.append(row)
    return rows


def check_runs(project: Project, media_id: Optional[str] = None) -> List[dict]:
    """Re-verify completed runs: structure, file hashes, and the source against the manifest. Read-only."""
    findings: List[dict] = []
    m = ingest.load_manifest(project)
    items = {i["id"]: i for i in (m or {}).get("items", [])}

    def add(sev, code, msg, path):
        findings.append({"severity": sev, "code": code, "message": msg, "path": path})

    for r in list_runs(project, media_id):
        if r["state"] == "incomplete":
            add("warning", "RUN_INCOMPLETE", "an interrupted run left a .part folder (not a result)", r["path"])
        elif r["state"] == "failed":
            add("info", "RUN_FAILED_DIAGNOSTICS", "a failed run's diagnostics are kept here", r["path"])
        elif r["state"] == "invalid":
            add("critical", "RUN_INVALID", "run folder has no valid completed run.json", r["path"])
        else:
            d = project.dir / r["path"]
            for p in verify_run_dir(d, check_hashes=True):
                add("critical", "RUN_OUTPUT_PROBLEM", p, r["path"])
            try:
                run = json.loads((d / "run.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            it = items.get(r["media_id"])
            if it is None:
                add("warning", "MEDIA_NOT_IN_MANIFEST", "the run's media id is no longer in the manifest", r["path"])
            else:
                if source_identity(it) != run.get("source_before"):
                    add("warning", "SOURCE_IDENTITY_CHANGED_SINCE_RUN", "the manifest's record of the source differs from the one analysed", r["path"])
                bad = _source_unchanged(it) if it.get("file") else None
                if bad:
                    add("critical", "SOURCE_CHANGED", bad, r["path"])
    return findings
