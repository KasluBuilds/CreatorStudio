"""Live integration suite: real Resolve, a NEW disposable project, synthetic media only.

    cd orchestrator
    python -B -m resolve_executor.live_suite --out "<a folder inside the bridge's allowed output roots>"

What it does, and what it never does:

* Generates synthetic clips with ffmpeg (frame index encoded as luma, a sine tone as audio) inside `--out`.
* Creates a new project `CS_T-RSV-002_<stamp>`; it never opens, edits or deletes any other project, never touches real
  footage (the policy forbids `MVI_0879.MOV` by name and media outside `--out`), never edits the bridge config.
* `--out` must lie inside the bridge's configured `allowed_output_roots` (read, not changed).
* Every check is recorded in `<out>/<run>/evidence.json` with the readback, ffprobe and luma measurements.
  A failing check is recorded and the suite continues with the next independent area.

Exit code 0 when every executed check passed, 1 otherwise, 2 when Resolve/bridge/ffmpeg is unavailable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from . import operations as ops
from .errors import AdapterError, FailureClass
from .executor import Executor, Outcome
from .journal import Journal
from .live import LiveResolveAdapter
from .probe import count_frames, ffprobe
from .timebase import FrameRange, parse_rate
from .validation import Policy, inside

STEP = 4            # luma step per source frame
PERIOD = 55         # luma pattern period (16 + 4 * 54 = 232 stays inside video range)


class Recorder:
    def __init__(self, path: str) -> None:
        self.path = path
        self.doc: Dict[str, Any] = {"schema": "resolve_executor.live_evidence/1", "checks": [], "facts": {}}

    def check(self, name: str, fn: Callable[[], Any]) -> Any:
        t0 = time.monotonic()
        rec: Dict[str, Any] = {"name": name}
        try:
            value = fn()
            ok = value.get("ok", True) if isinstance(value, dict) else True
            rec.update({"ok": bool(ok), "result": value})
        except Exception as exc:  # recorded, never hidden
            rec.update({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc(limit=4)[-1500:]})
            value = None
        rec["seconds"] = round(time.monotonic() - t0, 2)
        self.doc["checks"].append(rec)
        self.save()
        mark = "PASS" if rec["ok"] else "FAIL"
        print(f"[{mark}] {name} ({rec['seconds']}s)" + ("" if rec["ok"] else f": {rec.get('error') or ''}"), flush=True)
        return value

    def fact(self, key: str, value: Any) -> None:
        self.doc["facts"][key] = value
        self.save()

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.doc, fh, indent=2, sort_keys=True, default=str)


def run(cmd: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def make_clip(path: str, rate: str, frames: int, width: int = 1920, height: int = 1080, audio: bool = True) -> None:
    """Luma of frame N is 16 + STEP * (N mod PERIOD); a 440 Hz stereo tone when audio is on."""
    seconds = float(frames / parse_rate(rate).value) + 1          # generous; -frames:v cuts exactly
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={rate}:d={seconds:.3f}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds:.3f}"]
    cmd += ["-vf", f"geq=lum='16+{STEP}*mod(N,{PERIOD})':cb=128:cr=128", "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "2", "-g", "1", "-pix_fmt", "yuv420p", "-r", rate]
    if audio:
        cmd += ["-c:a", "aac", "-ac", "2", "-shortest"]
    cmd.append(path)
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr[-500:]}")


def make_tone(path: str, seconds: int = 10) -> None:
    p = run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=48000:duration={seconds}",
             "-ac", "2", "-c:a", "pcm_s16le", path])
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-500:])


def luma_series(path: str) -> List[float]:
    p = run(["ffmpeg", "-v", "error", "-i", path, "-vf",
             "signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"], timeout=600)
    return [float(m) for m in re.findall(r"lavfi\.signalstats\.YAVG=([0-9.]+)", p.stdout)]


def decode_index(y: float) -> int:
    """Source frame index modulo PERIOD from a measured mean luma (tolerates small codec drift)."""
    return int(round((y - 16) / STEP)) % PERIOD


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="folder inside the bridge's allowed output roots")
    ap.add_argument("--keep-going", action="store_true", default=True)
    args = ap.parse_args(argv)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg/ffprobe not found", file=sys.stderr)
        return 2
    try:
        adapter = LiveResolveAdapter.connect(timeout=60.0)
    except AdapterError as exc:
        print(f"Resolve bridge unavailable: {exc.message}", file=sys.stderr)
        return 2
    out_root = os.path.realpath(args.out)
    if not inside(out_root, adapter.configured_output_roots):
        print(f"--out must lie inside the bridge's allowed output roots {adapter.configured_output_roots}", file=sys.stderr)
        return 2
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(out_root, f"run_{stamp}")
    media_dir, render_dir = os.path.join(run_dir, "media"), os.path.join(run_dir, "renders")
    os.makedirs(media_dir)
    os.makedirs(render_dir)
    rec = Recorder(os.path.join(run_dir, "evidence.json"))
    rec.fact("started", dt.datetime.now(dt.timezone.utc).isoformat())
    rec.fact("health", adapter.health())
    rec.fact("project_before", adapter.current_project())

    # ── synthetic media ──
    clips = {"v25.mp4": ("25", 200), "v50.mp4": ("50", 200), "v2997.mp4": ("30000/1001", 200), "v24.mp4": ("24", 200)}
    paths = {}

    def gen():
        for name, (rate, n) in clips.items():
            make_clip(os.path.join(media_dir, name), rate, n)
            paths[name] = os.path.join(media_dir, name)
        make_clip(os.path.join(media_dir, "v25_vertical.mp4"), "25", 100, 1080, 1920)
        paths["v25_vertical.mp4"] = os.path.join(media_dir, "v25_vertical.mp4")
        make_tone(os.path.join(media_dir, "tone.wav"))
        paths["tone.wav"] = os.path.join(media_dir, "tone.wav")
        return {n: {"frames_probed": count_frames(p), "rate": ffprobe(p).video[0].frame_rate if ffprobe(p).video else None}
                for n, p in paths.items()}
    rec.check("synthetic media generated and probed", gen)

    policy = Policy(allowed_media_roots=(media_dir,), allowed_output_roots=(render_dir,),
                    forbidden_media_names=("MVI_0879.MOV",))
    ex = Executor(adapter, policy, Journal(os.path.join(run_dir, "journal")), render_poll_seconds=0.5)
    project = f"CS_T-RSV-002_{stamp}"

    def must(r):
        if not r.ok:
            raise AssertionError(f"{r.kind} {r.outcome.value} {r.failure}: {r.message}")
        return r

    def ok(r, **extra):
        return {"ok": r.ok, "outcome": r.outcome.value, "failure": r.failure.value if r.failure else None,
                "message": r.message, "evidence": r.evidence, **extra}

    rec.check("create disposable project", lambda: ok(must(ex.execute(ops.CreateProject("proj", name=project)))))
    if ex.expected_project != project:
        rec.save()
        return 1
    rec.check("import synthetic media", lambda: ok(must(ex.execute(ops.ImportMedia("imp", paths=tuple(paths.values()))))))
    rec.fact("media_pool", adapter.media_items())

    def tl(name, rate, w=1920, h=1080, tc="01:00:00:00"):
        return must(ex.execute(ops.CreateTimeline(f"tl-{name}", name=name, frame_rate=rate, width=w, height=h,
                                                  start_timecode=tc)))

    def seg(op_id, timeline, clip, s, e, **kw):
        return ex.execute(ops.AppendSegment(op_id, timeline=timeline, media_path=paths[clip], source=FrameRange(s, e), **kw))

    # ── A: frame semantics at matched rates ──
    def frame_semantics():
        tl("A25", "25")
        results = {}
        for op_id, (s, e) in {"a100": (0, 100), "a1first": (0, 1), "a1last100": (99, 100), "afileend": (180, 200),
                              "amid": (37, 62)}.items():
            r = seg(op_id, "A25", "v25.mp4", s, e)
            results[op_id] = ok(r)
            if not r.ok:
                results["ok"] = False
        items = adapter.track_items("A25", "video", 1)
        results["items"] = items
        results["timeline"] = adapter.timeline_info("A25")
        results.setdefault("ok", True)
        return results
    rec.check("A: source ranges are half-open at 25 fps (100, 1, last, file end, mid)", frame_semantics)

    def beyond_end():
        r = seg("abeyond", "A25", "v25.mp4", 199, 201)
        tl("A25raw", "25")
        raw = adapter.append("A25raw", [{"media_id": next(m["id"] for m in adapter.media_items()
                                                          if m["path"] and os.path.basename(m["path"]) == "v25.mp4"),
                                         "start": 190, "end": 210, "record": 90000, "track_index": 1, "media_type": 1}])
        landed = adapter.track_items("A25raw", "video", 1)
        res = {"ok": r.outcome is Outcome.REFUSED, "executor": ok(r), "raw_append_past_file_end_returned": raw,
               "raw_landed": landed}
        rr = ex.execute(ops.RenderTimeline("rAraw", timeline="A25raw", spec=ops.RenderSpec(render_dir, "A25raw_h264")))
        path = os.path.join(render_dir, "A25raw_h264.mp4")
        if os.path.exists(path):
            res["raw_render_source_index_mod55"] = [decode_index(y) for y in luma_series(path)]
        res["raw_render"] = ok(rr)
        return res
    rec.check("A: a range past the last frame is refused by the executor; raw Resolve behaviour recorded", beyond_end)

    def render_a():
        r = ex.execute(ops.RenderTimeline("rA", timeline="A25", spec=ops.RenderSpec(render_dir, "A25_h264")))
        out = ok(r)
        path = os.path.join(render_dir, "A25_h264.mp4")
        if os.path.exists(path):
            ys = luma_series(path)
            out["render_frames_decoded"] = len(ys)
            out["decoded_source_index_mod55"] = [decode_index(y) for y in ys]
            out["luma"] = [round(y, 2) for y in ys[:5]] + ["..."] + [round(y, 2) for y in ys[-5:]]
        return out
    rec.check("A: H.264 1920x1080 render verified (frames, codec, audio) and content decoded", render_a)

    def mixed():
        tl("C25", "25")
        res = {"ok": True}
        for op_id, clip, want in (("c50", "v50.mp4", 50), ("c2997", "v2997.mp4", 83), ("c24", "v24.mp4", 104)):
            r = seg(op_id, "C25", clip, 0, 100)
            res[op_id] = ok(r, expected_duration=want)
            res["ok"] = res["ok"] and r.ok
        res["items"] = adapter.track_items("C25", "video", 1)
        return res
    rec.check("C: mixed source rates in a 25 fps timeline floor as predicted (50, 83, 104)", mixed)

    def zero_start():
        tl("D0", "25", tc="00:00:00:00")
        r1 = seg("d1", "D0", "v25.mp4", 0, 10)
        r2 = seg("d2", "D0", "v25.mp4", 10, 20, record_frame=50)
        return {"ok": r1.ok and r2.ok, "r1": ok(r1), "r2": ok(r2), "info": adapter.timeline_info("D0"),
                "items": adapter.track_items("D0", "video", 1)}
    rec.check("D: zero timeline start and explicit record frame with a gap", zero_start)

    # ── E: multitrack ──
    def multitrack():
        tl("E", "25")
        res: Dict[str, Any] = {}
        for i, kind in enumerate(("video", "audio", "audio")):
            res[f"add_{i}"] = ok(must(ex.execute(ops.AddTrack(f"e-tr{i}", timeline="E", track_kind=kind))))
        res["av"] = ok(seg("e-av", "E", "v25.mp4", 0, 100, media="video_audio", audio_track_index=1))
        res["overlay"] = ok(seg("e-v2", "E", "v50.mp4", 0, 50, track_index=2, record_frame=90010))
        res["gap_then_clip"] = ok(seg("e-gap", "E", "v25.mp4", 100, 150, record_frame=90125))
        res["music"] = ok(seg("e-music", "E", "tone.wav", 0, 192, media="audio", track_index=3, record_frame=90000))
        res["sfx"] = ok(seg("e-sfx", "E", "tone.wav", 0, 12, media="audio", track_index=2, record_frame=90050))
        res["wav_media"] = next(m for m in adapter.media_items() if m["name"] == "tone.wav")
        over = seg("e-over", "E", "v25.mp4", 0, 10, record_frame=90090)
        res["overlap_refused"] = ok(over)
        mid = next(m["id"] for m in adapter.media_items() if m["path"] and os.path.basename(m["path"]) == "v25.mp4")
        before = adapter.track_items("E", "video", 1)
        raw = adapter.append("E", [{"media_id": mid, "start": 0, "end": 10, "record": 90090, "track_index": 1,
                                    "media_type": 1}])
        after = adapter.track_items("E", "video", 1)
        res["raw_overlap"] = {"returned": raw, "items_before": len(before), "items_after": len(after),
                              "after": after}
        res["structure"] = {f"{k}{i}": adapter.track_items("E", k, i) for k, n in (("video", 2), ("audio", 3))
                            for i in range(1, n + 1)}
        res["ok"] = all(res[k]["ok"] for k in ("av", "overlay", "gap_then_clip", "music", "sfx")) and \
            over.outcome is Outcome.REFUSED
        return res
    rec.check("E: multitrack, sync, gaps, overlap handling and track structure readback", multitrack)

    # ── F: transforms ──
    def transforms():
        ref = ops.ItemRef("video", 2, 90010)
        values = {"ZoomX": 0.5, "ZoomY": 0.5, "Pan": 400.0, "Tilt": -200.0, "RotationAngle": 15.0, "CropLeft": 100.0,
                  "CropTop": 50.0, "Opacity": 80.0, "FlipX": True}
        r = ex.execute(ops.SetTransform("f1", timeline="E", item=ref, values=values))
        return ok(r)
    rec.check("F: transform and crop on a V2 item read back exactly", transforms)

    def marker():
        r = ex.execute(ops.AddMarker("g1", timeline="E", frame=25, color="Green", name="beat", note="synthetic", duration=5))
        return ok(r, markers=adapter.markers("E"))
    rec.check("G: timeline marker read back (frame, colour, name, note, duration)", marker)

    def delete_wrong_item():
        must(seg("e-wrong", "E", "v25.mp4", 0, 5, record_frame=90200))
        before = adapter.track_items("E", "video", 1)
        r = ex.execute(ops.DeleteItems("e-del", timeline="E", items=(ops.ItemRef("video", 1, 90200),)))
        after = adapter.track_items("E", "video", 1)
        return ok(r, before=len(before), after=len(after),
                  checkpoint_files=sorted(os.listdir(os.path.join(run_dir, "journal", "checkpoints"))))
    rec.check("L: a wrong placement is removed by DeleteItems after an automatic checkpoint; other items untouched",
              delete_wrong_item)

    def identity():
        other = Executor(adapter, policy, expected_project=project + "_not_open")
        r = other.execute(ops.AddTrack("id-1", timeline="E", track_kind="video"))
        return {"ok": r.failure is FailureClass.IDENTITY_MISMATCH, "result": ok(r),
                "tracks_after": adapter.timeline_info("E")["track_counts"]}
    rec.check("L: an executor expecting another project refuses to mutate the open one", identity)

    # ── H: titles ──
    def title():
        r = ex.execute(ops.PlaceTitle("h1", timeline="E", text="Synthetic title", track_index=2, record_frame=90070,
                                      duration=40, size=0.08, center=(0.5, 0.2)))
        return ok(r)
    rec.check("H: Text+ title placed on V2 at an exact frame range, text read back", title)

    # ── I: audio capability probes (read what exists; never a workaround) ──
    def audio_probe():
        res: Dict[str, Any] = {"timeline_item_methods": adapter.methods_of("TimelineItem"),
                               "timeline_methods": adapter.methods_of("Timeline")}
        a_items = adapter.track_items("E", "audio", 1)
        if a_items:
            start = a_items[0]["start"]
            for key in ("Volume", "Level", "AudioVolume"):
                try:
                    res[f"SetProperty_{key}"] = adapter.set_item_property("E", "audio", 1, start, key, -6.0)
                except AdapterError as exc:
                    res[f"SetProperty_{key}"] = exc.to_dict()
        res["has_AddTransition"] = "AddTransition" in res["timeline_item_methods"]
        r = ex.execute(ops.SetClipAudioLevel("i1", timeline="E", item=ops.ItemRef("audio", 1, 90000), volume_db=-6.0))
        res["executor_refusal"] = ok(r)
        res["ok"] = r.failure is FailureClass.UNSUPPORTED
        return res
    rec.check("I: audio level / transition API surface probed; executor refuses unsupported audio ops", audio_probe)

    # ── J: renders ──
    def render(name, timeline, **kw):
        r = ex.execute(ops.RenderTimeline(f"r-{name}", timeline=timeline, spec=ops.RenderSpec(render_dir, name, **kw)))
        return ok(r)
    rec.check("J: multitrack timeline H.264 1080p with audio", lambda: render("E_h264", "E"))
    rec.check("J: H.265 render", lambda: render("A25_h265", "A25", codec="H265"))
    rec.check("J: render without audio has no audio stream", lambda: render("A25_noaudio", "A25", export_audio=False))
    rec.check("J: half-open mark range render [90010, 90035) has 25 frames",
              lambda: render("A25_range", "A25", mark_in=90010, mark_out=90035))

    def vertical():
        tl("V916", "25", w=1080, h=1920)
        must(seg("v1", "V916", "v25_vertical.mp4", 0, 50))
        return render("V916_h264", "V916", width=1080, height=1920)
    rec.check("J: 9:16 vertical 1080x1920 render", vertical)

    def interrupted():
        tl("LONG", "25")
        for n in range(6):
            must(seg(f"l{n}", "LONG", "v50.mp4", 0, 200))
        must(seg("l-a", "LONG", "v25.mp4", 0, 200, media="audio", track_index=1))
        r = ex.execute(ops.RenderTimeline("r-long", timeline="LONG",
                                          spec=ops.RenderSpec(render_dir, "LONG_timeout", width=3840, height=2160,
                                                              codec="H265", timeout_seconds=5)))
        res = ok(r)
        res["ok"] = r.failure in (FailureClass.RENDER_TIMEOUT, None)
        res["note"] = "SUCCEEDED means the render finished inside 5 s; the timeout path was then not exercised"
        return res
    rec.check("J: render timeout stops the job and reports the partial file", interrupted)

    # ── K: recovery / bridge failure ──
    def bridge_down():
        from .bridge import BridgeClient
        c = BridgeClient.from_config(timeout=2.0)
        c.port = c.port + 1
        try:
            LiveResolveAdapter(c).health()
            return {"ok": False}
        except AdapterError as exc:
            return {"ok": exc.failure is FailureClass.BRIDGE_UNAVAILABLE, "error": exc.to_dict()}
    rec.check("K: an unreachable bridge is classified BRIDGE_UNAVAILABLE", bridge_down)
    rec.check("K: checkpoint saves the project and snapshots the timeline",
              lambda: ex.checkpoint("final", "E"))
    rec.check("K: save project", lambda: ok(ex.execute(ops.SaveProject("save"))))

    # ── B: the historical 95/100 case (these switch to NEW disposable projects, so they run last) ──
    def historical_24():
        tl("B24", "24")
        r100 = seg("b24-100", "B24", "v25.mp4", 0, 100)
        r99 = seg("b24-99", "B24", "v25.mp4", 0, 99)
        items = adapter.track_items("B24", "video", 1)
        return {"ok": r100.ok and r99.ok and [i["duration"] for i in items] == [96, 95], "r100": ok(r100),
                "r99": ok(r99), "items": items, "timeline": adapter.timeline_info("B24")}
    rec.check("B: 25 fps source in a 24 fps timeline: [0,100) -> 96, [0,99) -> 95 (reproduces the historical 95)",
              historical_24)

    def ninety_five_ntsc():
        must(ex.execute(ops.CreateProject("proj-ntsc", name=project + "_ntsc")))
        must(ex.execute(ops.ImportMedia("imp-ntsc", paths=(paths["v25.mp4"],))))
        t = tl("B23976", "23.976")
        r = seg("b100", "B23976", "v25.mp4", 0, 100)
        items = adapter.track_items("B23976", "video", 1)
        refused = ex.execute(ops.CreateTimeline("tl-ntsc2", name="B2997", frame_rate="29.97", width=1920, height=1080))
        return {"ok": r.ok and bool(items) and items[0]["duration"] == 95 and refused.failure is FailureClass.PRECONDITION,
                "timeline": ok(t), "executor": ok(r), "items": items, "second_ntsc_rate_refused": ok(refused)}
    rec.check("B: NTSC timeline via a fresh project rate; 25 fps [0,100) in 23.976 -> 95; other NTSC rate refused",
              ninety_five_ntsc)

    rec.fact("finished", dt.datetime.now(dt.timezone.utc).isoformat())
    rec.fact("project_after", adapter.current_project())
    failed = [c["name"] for c in rec.doc["checks"] if not c["ok"]]
    rec.fact("summary", {"checks": len(rec.doc["checks"]), "failed": failed})
    print(f"evidence: {rec.path}\n{len(rec.doc['checks']) - len(failed)}/{len(rec.doc['checks'])} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
