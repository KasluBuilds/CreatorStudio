"""In-memory Resolve simulation for offline tests.

It models the behaviours the executor depends on, as established live (see RESOLVE_INTEGRATION.md):
half-open source ranges, exclusive item/timeline ends, floor conversion for mixed source/timeline rates, silent
rejection of overlapping placements, False (not an exception) for rejected calls, and a render queue whose output
file is written on completion. Fault injection (`faults`) reproduces transport failures before or after a change
is applied. It is a test double, not evidence about Resolve.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .adapter import ResolveAdapter
from .errors import AdapterError, FailureClass
from .probe import MediaInfo, ProbeError, StreamInfo
from .timebase import parse_rate, timecode_to_frames, timeline_duration_for_source

TRANSFORM_DEFAULTS = {"ZoomX": 1.0, "ZoomY": 1.0, "Pan": 0.0, "Tilt": 0.0, "RotationAngle": 0.0, "AnchorPointX": 0.0,
                      "AnchorPointY": 0.0, "Pitch": 0.0, "Yaw": 0.0, "CropLeft": 0.0, "CropRight": 0.0,
                      "CropTop": 0.0, "CropBottom": 0.0, "CropSoftness": 0.0, "Opacity": 100.0, "FlipX": False,
                      "FlipY": False, "CropRetain": False}


@dataclass
class FakeMedia:
    id: str
    path: str
    frames: int
    fps: str
    video: bool = True
    audio: bool = True
    width: int = 1920
    height: int = 1080
    type: str = "Video + Audio"
    timeline: Optional[str] = None        # set for nested-timeline items (titles)


@dataclass
class FakeTimeline:
    name: str
    rate: str = "25"
    width: int = 1920
    height: int = 1080
    start: int = 90000
    tracks: Dict[str, int] = field(default_factory=lambda: {"video": 1, "audio": 1, "subtitle": 0})
    items: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    markers: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    text: Optional[str] = None            # Text+ content for title timelines

    def end(self) -> int:
        ends = [it["end"] for its in self.items.values() for it in its]
        return max(ends) if ends else self.start


class FakeResolve(ResolveAdapter):
    def __init__(self, *, media_files: Optional[Mapping[str, Dict[str, Any]]] = None, version: str = "21.0.4.5",
                 render_steps: int = 3, codecs: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self.version = version
        self.projects: Dict[str, Dict[str, Any]] = {"Untitled Project": self._new_project()}
        self.current: Optional[str] = "Untitled Project"
        self.media_files = dict(media_files or {})     # path -> {frames, fps, video, audio, width, height}
        self.page = "edit"
        self.render_steps = render_steps
        self.codecs = codecs or {"mp4": {"H.264": "H264", "H.265": "H265"}, "mov": {"H.264": "H264", "H.265": "H265"}}
        self.calls: List[str] = []
        # faults: method name -> list of actions consumed in order:
        #   "raise_before" (transport error, nothing applied), "raise_after" (applied, then timeout),
        #   "false" (Resolve returns False), "unavailable" (connect refused), "ignore" (returns True, does nothing)
        self.faults: Dict[str, List[str]] = {}
        self.render_behaviour = "ok"   # ok | error | stall | stub | audio_only | wrong_size | no_file
        self._next_id = 1
        self.sets_ignored: set = set()  # property keys whose SetProperty silently does nothing
        self.title_rate_ignored = False  # simulate a title timeline left at the project rate
        self.job_overrides: Dict[str, Any] = {}  # fields a queued job reports differently from what was set

    # ── plumbing ──
    @staticmethod
    def _new_project() -> Dict[str, Any]:
        return {"media": [], "timelines": {}, "current_timeline": None, "settings": {"timelineFrameRate": "25"},
                "jobs": {}, "rendering": None, "saved": 0}

    def _id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id:04d}"

    def _fault(self, method: str) -> Optional[str]:
        self.calls.append(method)
        queue = self.faults.get(method)
        if queue:
            action = queue.pop(0)
            if action == "raise_before":
                raise AdapterError(FailureClass.BRIDGE_TIMEOUT, f"simulated timeout before {method}")
            if action == "unavailable":
                raise AdapterError(FailureClass.BRIDGE_UNAVAILABLE, f"simulated unreachable bridge ({method})")
            return action
        return None

    def _after(self, action: Optional[str], method: str) -> None:
        if action == "raise_after":
            raise AdapterError(FailureClass.BRIDGE_TIMEOUT, f"simulated timeout after {method}")

    @property
    def p(self) -> Dict[str, Any]:
        if self.current is None:
            raise AdapterError(FailureClass.PRECONDITION, "no project is open")
        return self.projects[self.current]

    def _tl(self, name: str) -> FakeTimeline:
        tl = self.p["timelines"].get(name)
        if tl is None:
            raise AdapterError(FailureClass.PRECONDITION, "timeline not found", {"timeline": name})
        return tl

    # ── identity ──
    def health(self) -> Dict[str, Any]:
        self._fault("health")
        return {"product": "DaVinci Resolve", "version": self.version, "bridge_edition": "free"}

    def current_project(self) -> Optional[str]:
        self._fault("current_project")
        return self.current

    def project_names(self) -> List[str]:
        self._fault("project_names")
        return sorted(self.projects)

    def create_project(self, name: str) -> bool:
        a = self._fault("create_project")
        if a in ("false", "ignore") or name in self.projects:
            return a == "ignore"
        self.projects[name] = self._new_project()
        self.current = name
        self._after(a, "create_project")
        return True

    def save_project(self) -> bool:
        a = self._fault("save_project")
        if a == "false":
            return False
        self.p["saved"] += 1
        return True

    def project_setting(self, key: str) -> Any:
        return self.p["settings"].get(key)

    def set_project_setting(self, key: str, value: str) -> bool:
        if key == "timelineFrameRate" and self.p["timelines"]:
            return False             # measured: locked once the project has timelines
        self.p["settings"][key] = value
        return True

    # ── media ──
    def media_items(self) -> List[Dict[str, Any]]:
        self._fault("media_items")
        return [{"id": m.id, "path": m.path, "name": os.path.basename(m.path) if m.path else m.id, "frames": m.frames,
                 "fps": m.fps, "type": m.type} for m in self.p["media"]]

    def import_media(self, paths: Sequence[str]) -> List[str]:
        a = self._fault("import_media")
        if a in ("false", "ignore"):
            return []
        ids = []
        for path in paths:
            spec = self.media_files.get(path)
            if spec is None:
                continue
            video, audio = spec.get("video", True), spec.get("audio", True)
            mtype = "Video + Audio" if video and audio else ("Video" if video else "Audio")
            m = FakeMedia(self._id("mpi"), path, int(spec["frames"]), str(spec["fps"]), video, audio,
                          spec.get("width", 1920), spec.get("height", 1080), mtype)
            self.p["media"].append(m)
            ids.append(m.id)
        self._after(a, "import_media")
        return ids

    # ── timelines ──
    def timeline_names(self) -> List[str]:
        self._fault("timeline_names")
        return list(self.p["timelines"])

    def current_timeline(self) -> Optional[str]:
        return self.p["current_timeline"]

    def set_current_timeline(self, name: str) -> bool:
        if name not in self.p["timelines"]:
            return False
        self.p["current_timeline"] = name
        return True

    def create_timeline(self, name: str) -> bool:
        a = self._fault("create_timeline")
        if a in ("false", "ignore") or name in self.p["timelines"]:
            return False
        self.p["timelines"][name] = FakeTimeline(name, rate=self.p["settings"]["timelineFrameRate"])
        self.p["current_timeline"] = name
        self._after(a, "create_timeline")
        return True

    def timeline_info(self, name: str) -> Optional[Dict[str, Any]]:
        self._fault("timeline_info")
        tl = self.p["timelines"].get(name)
        if tl is None:
            return None
        nominal = parse_rate(tl.rate).nominal
        s = tl.start
        tc = f"{s // nominal // 3600:02d}:{s // nominal // 60 % 60:02d}:{s // nominal % 60:02d}:{s % nominal:02d}"
        return {"name": name, "start_frame": tl.start, "end_frame": tl.end(), "frame_rate": tl.rate,
                "width": tl.width, "height": tl.height, "start_timecode": tc, "track_counts": dict(tl.tracks)}

    def set_timeline_setting(self, name: str, key: str, value: str) -> bool:
        a = self._fault("set_timeline_setting")
        tl = self._tl(name)
        if a == "false":
            return False
        if key == "timelineFrameRate":
            if any(tl.items.values()) or not parse_rate(value).is_integer:
                return False           # locked once it has items; NTSC rates only via the project (measured)
            tl.rate = value
        elif key == "timelineResolutionWidth":
            tl.width = int(value)
        elif key == "timelineResolutionHeight":
            tl.height = int(value)
        return True

    def set_timeline_start_timecode(self, name: str, timecode: str) -> bool:
        tl = self._tl(name)
        tl.start = timecode_to_frames(timecode, tl.rate)
        return True

    def add_track(self, name: str, kind: str, subtype: Optional[str]) -> bool:
        a = self._fault("add_track")
        tl = self._tl(name)
        if a in ("false", "ignore"):
            return a == "ignore"
        tl.tracks[kind] = tl.tracks.get(kind, 0) + 1
        self._after(a, "add_track")
        return True

    def track_items(self, name: str, kind: str, index: int) -> List[Dict[str, Any]]:
        self._fault("track_items")
        tl = self._tl(name)
        return [dict(x) for x in sorted(tl.items.get(f"{kind}{index}", []), key=lambda i: i["start"])]

    def append(self, name: str, clip_infos: Sequence[Mapping[str, Any]]) -> int:
        a = self._fault("append")
        tl = self._tl(name)
        if a in ("false", "ignore"):
            return 0
        placed = 0
        for ci in clip_infos:
            m = next((x for x in self.p["media"] if x.id == ci["media_id"]), None)
            if m is None:
                continue
            start, end = int(ci["start"]), int(ci["end"])
            if end <= start or end > m.frames or start < 0:
                continue
            duration = timeline_duration_for_source(end - start, m.fps, tl.rate)
            record = int(ci["record"])
            parts = []
            mt = ci.get("media_type")
            if m.video and mt in (None, 1):
                parts.append(("video", int(ci["track_index"])))
            if m.audio and mt in (None, 2):
                parts.append(("audio", int(ci["track_index"])))
            for kind, idx in parts:
                if idx > tl.tracks.get(kind, 0):
                    continue
                track = tl.items.setdefault(f"{kind}{idx}", [])
                if any(record < it["end"] and it["start"] < record + duration for it in track):
                    continue        # Resolve silently rejects an overlapping placement
                track.append({"id": self._id("tli"), "name": os.path.basename(m.path) if m.path else m.id,
                              "track_kind": kind, "track_index": idx, "start": record, "end": record + duration,
                              "duration": duration, "media_id": m.id, "source_start": start, "source_end": end,
                              "props": dict(TRANSFORM_DEFAULTS)})
                placed += 1
        self._after(a, "append")
        return placed

    def _find(self, name: str, kind: str, index: int, start: int) -> Dict[str, Any]:
        for it in self._tl(name).items.get(f"{kind}{index}", []):
            if it["start"] == start:
                return it
        raise AdapterError(FailureClass.PRECONDITION, "timeline item not found",
                           {"track_kind": kind, "track_index": index, "start_frame": start})

    def item_properties(self, name: str, kind: str, index: int, start: int, keys: Sequence[str]) -> Dict[str, Any]:
        self._fault("item_properties")
        it = self._find(name, kind, index, start)
        return {k: it["props"].get(k) for k in keys}

    def set_item_property(self, name: str, kind: str, index: int, start: int, key: str, value: Any) -> bool:
        a = self._fault("set_item_property")
        it = self._find(name, kind, index, start)
        if a == "false" or key not in TRANSFORM_DEFAULTS:
            return False
        if key in self.sets_ignored or a == "ignore":
            return True                # reports success but changes nothing (a "silent lie")
        it["props"][key] = value
        self._after(a, "set_item_property")
        return True

    def delete_items(self, name: str, refs: Sequence[Mapping[str, int]]) -> bool:
        a = self._fault("delete_items")
        tl = self._tl(name)
        if a in ("false", "ignore"):
            return a == "ignore"
        for r in refs:
            key = f"{r['track_kind']}{r['track_index']}"
            tl.items[key] = [it for it in tl.items.get(key, []) if it["start"] != r["start_frame"]]
        self._after(a, "delete_items")
        return True

    def add_marker(self, name: str, frame: int, color: str, title: str, note: str, duration: int) -> bool:
        a = self._fault("add_marker")
        tl = self._tl(name)
        if a == "false" or frame in tl.markers:
            return False
        tl.markers[frame] = {"color": color, "name": title, "note": note, "duration": duration, "customData": ""}
        self._after(a, "add_marker")
        return True

    def markers(self, name: str) -> Dict[int, Dict[str, Any]]:
        return {f: dict(m) for f, m in self._tl(name).markers.items()}

    def has_method(self, object_kind: str, method: str) -> bool:
        return not (object_kind == "TimelineItem" and method == "AddTransition")

    def create_title_clip(self, title_timeline: str, text: str, duration: int, size: Optional[float],
                          center: Optional[Sequence[float]], frame_rate: Optional[str] = None) -> Optional[str]:
        a = self._fault("create_title_clip")
        if a == "false" or title_timeline in self.p["timelines"]:
            return None
        rate = (None if self.title_rate_ignored else frame_rate) or self.p["settings"]["timelineFrameRate"]
        tl = FakeTimeline(title_timeline, rate=rate)
        default_len = 5 * parse_rate(rate).nominal          # a Text+ title is 5 seconds by default
        tl.items["video1"] = [{"id": self._id("tli"), "name": "Text+", "start": tl.start, "end": tl.start + default_len,
                               "duration": default_len, "media_id": None}]
        tl.text = text
        self.p["timelines"][title_timeline] = tl
        m = FakeMedia(self._id("mpi"), "", default_len, rate, video=True, audio=False, type="Timeline",
                      timeline=title_timeline)
        self.p["media"].append(m)
        return m.id

    def title_text(self, name: str, track_index: int, start: int) -> Optional[str]:
        it = self._find(name, "video", track_index, start)
        m = next((x for x in self.p["media"] if x.id == it.get("media_id")), None)
        if m is None or not m.timeline:
            return None
        return self.p["timelines"][m.timeline].text

    # ── pages ──
    def current_page(self) -> Optional[str]:
        return self.page

    def open_page(self, page: str) -> bool:
        self.page = page
        return True

    # ── render ──
    def render_formats(self) -> Dict[str, str]:
        return {k: k for k in self.codecs}

    def render_codecs(self, fmt: str) -> Dict[str, str]:
        return dict(self.codecs.get(fmt, {}))

    def set_render_format_and_codec(self, fmt: str, codec: str) -> bool:
        a = self._fault("set_render_format_and_codec")
        self.page = "deliver"
        if a == "false" or codec not in self.codecs.get(fmt, {}).values():
            return False
        self.p["render_fmt"] = (fmt, codec)
        return True

    def set_render_settings(self, settings: Mapping[str, Any]) -> bool:
        a = self._fault("set_render_settings")
        self.page = "deliver"
        if a == "false":
            return False
        if settings.get("CustomName") == "" or settings == {"SelectAllFrames": False}:
            return False                 # measured: SelectAllFrames False on its own is rejected
        s = self.p.setdefault("render_settings", {})
        s.update(settings)
        if settings.get("SelectAllFrames") is True:
            s.pop("MarkIn", None)
            s.pop("MarkOut", None)
        if "MarkIn" in settings or "MarkOut" in settings:
            s["SelectAllFrames"] = False
        return True

    def render_presets(self) -> List[str]:
        return ["H.264 Master"]

    def load_render_preset(self, name: str) -> bool:
        return name in self.render_presets()

    def add_render_job(self) -> Optional[str]:
        a = self._fault("add_render_job")
        s = self.p.get("render_settings", {})
        if a == "false" or not s.get("TargetDir"):
            return None
        job = self._id("job")
        fmt, codec = self.p.get("render_fmt", ("mp4", "H264"))
        tl = self._tl(self.p["current_timeline"])
        if s.get("SelectAllFrames", True) is not False:
            mark_in, mark_out = tl.start, tl.end() - 1     # MarkOut is inclusive in Resolve
        else:
            mark_in, mark_out = s["MarkIn"], s["MarkOut"]
        name = s.get("CustomName") or tl.name
        self.p["jobs"][job] = {"JobId": job, "TargetDir": s["TargetDir"], "OutputFilename": f"{name}.{fmt}",
                               "VideoFormat": fmt, "VideoCodec": codec, "MarkIn": mark_in, "MarkOut": mark_out,
                               "FormatWidth": s.get("FormatWidth", tl.width), "FormatHeight": s.get("FormatHeight", tl.height),
                               "IsExportVideo": s.get("ExportVideo", True), "IsExportAudio": s.get("ExportAudio", True),
                               "TimelineName": tl.name,
                               "rate": tl.rate, "has_audio": any(tl.items.get(f"audio{i}") for i in range(1, 9)),
                               "_progress": 0, "_status": "Ready", **self.job_overrides}
        return job

    def render_jobs(self) -> List[Dict[str, Any]]:
        return [{k: v for k, v in j.items() if not k.startswith("_")} for j in self.p["jobs"].values()]

    def start_render(self, job_ids: Sequence[str]) -> bool:
        a = self._fault("start_render")
        if a == "false":
            return False
        for j in job_ids:
            if j in self.p["jobs"]:
                self.p["jobs"][j]["_status"] = "Rendering"
                self.p["rendering"] = j
        return True

    def render_status(self, job_id: str) -> Dict[str, Any]:
        self._fault("render_status")
        j = self.p["jobs"].get(job_id)
        if j is None:
            return {}
        if j["_status"] == "Rendering":
            if self.render_behaviour == "error" and j["_progress"] >= 1:
                j["_status"], self.p["rendering"] = "Failed", None
                return {"JobStatus": "Failed", "CompletionPercentage": j["_progress"], "Error": "simulated codec error"}
            if self.render_behaviour != "stall":
                j["_progress"] = min(100, j["_progress"] + max(1, 100 // max(1, self.render_steps)))
            if j["_progress"] >= 100:
                j["_status"], self.p["rendering"] = "Complete", None
                self._write_output(j)
        return {"JobStatus": j["_status"], "CompletionPercentage": j["_progress"], "Error": ""}

    def _write_output(self, j: Dict[str, Any]) -> None:
        if self.render_behaviour == "no_file":
            return
        path = os.path.join(j["TargetDir"], j["OutputFilename"])
        frames = j["MarkOut"] - j["MarkIn"] + 1
        desc = {"fake_render": True, "codec": {"H264": "h264", "H265": "hevc"}[j["VideoCodec"]],
                "width": j["FormatWidth"], "height": j["FormatHeight"], "rate": j["rate"], "frames": frames,
                "video": bool(j["IsExportVideo"]), "audio": bool(j["IsExportAudio"])}
        if self.render_behaviour == "audio_only":
            desc["video"] = False
        if self.render_behaviour == "wrong_size":
            desc["width"] = 1280
        data = b"" if self.render_behaviour == "stub" else json.dumps(desc).encode()
        with open(path, "xb") as fh:
            fh.write(data)

    def is_rendering(self) -> bool:
        return self.p["rendering"] is not None

    def stop_render(self) -> None:
        j = self.p["jobs"].get(self.p["rendering"] or "")
        if j:
            j["_status"] = "Cancelled"
        self.p["rendering"] = None

    def delete_render_job(self, job_id: str) -> bool:
        return self.p["jobs"].pop(job_id, None) is not None


def fake_probe(path: str) -> MediaInfo:
    """Prober for files written by FakeResolve renders (JSON descriptors, not media)."""
    if not os.path.isfile(path):
        raise ProbeError("missing", f"no file at {path}")
    size = os.path.getsize(path)
    try:
        with open(path, "rb") as fh:
            desc = json.loads(fh.read().decode() or "null")
    except ValueError:
        raise ProbeError("unreadable", "not a fake render descriptor")
    if not isinstance(desc, dict):
        raise ProbeError("unreadable", "empty or invalid file")
    rate = parse_rate(desc["rate"]).value
    streams = []
    if desc["video"]:
        streams.append(StreamInfo("video", desc["codec"], desc["width"], desc["height"],
                                  f"{rate.numerator}/{rate.denominator}", desc["frames"]))
    if desc["audio"]:
        streams.append(StreamInfo("audio", "aac", sample_rate=48000, channels=2))
    dur = Fraction(desc["frames"]) / rate
    return MediaInfo(path, size, "mov,mp4,m4a,3gp,3g2,mj2", str(float(dur)), tuple(streams))
