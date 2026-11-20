"""`ResolveAdapter` over the local in-app bridge (Resolve Free 21.0.x).

Every public method maps to a short, fixed sequence of documented Resolve scripting calls. Nothing here accepts a
method name from the caller. `False`/`None` from Resolve is passed through as data; transport problems raise
`AdapterError` (see `bridge.BridgeClient`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .adapter import ResolveAdapter
from .bridge import BridgeClient, Handle
from .errors import AdapterError, FailureClass
from .timebase import TimebaseError, timecode_to_frames

ROOT = "resolve"
_ITEM_KINDS = ("video", "audio", "subtitle")
# Object kinds a capability probe may ask about, and how to reach a live instance of each.
_PROBE_KINDS = ("Resolve", "ProjectManager", "Project", "MediaPool", "Timeline", "TimelineItem", "MediaPoolItem")


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def clip_frames(props: Dict[str, Any]) -> tuple:
    """(frame count, where it came from) for a media pool clip's GetClipProperty() dict.

    Measured on Free 21.0.4.5: video clips report "Frames"; an audio-only clip (WAV) reports Frames '' and FPS = the
    project rate at import, and its Duration timecode at that rate is the frame count AppendToTimeline uses.
    """
    frames = _int(props.get("Frames"))
    if frames is not None:
        return frames, "Frames"
    if props.get("Type") == "Audio" and props.get("Duration") and props.get("FPS"):
        try:
            return timecode_to_frames(str(props["Duration"]), str(props["FPS"])), "Duration"
        except TimebaseError:
            pass
    return None, None


class LiveResolveAdapter(ResolveAdapter):
    def __init__(self, client: BridgeClient) -> None:
        self.c = client

    @classmethod
    def connect(cls, timeout: float = 30.0) -> "LiveResolveAdapter":
        adapter = cls(BridgeClient.from_config(timeout=timeout))
        adapter.health()
        return adapter

    @property
    def configured_output_roots(self) -> tuple:
        return self.c.allowed_output_roots

    @property
    def configured_media_roots(self) -> tuple:
        return self.c.allowed_media_roots

    # ── object helpers ──
    def _call(self, target: Any, method: str, *args: Any, timeout: Optional[float] = None) -> Any:
        return self.c.call(target, method, *args, timeout=timeout)

    def _pm(self) -> Handle:
        pm = self._call(ROOT, "GetProjectManager")
        if not isinstance(pm, Handle):
            raise AdapterError(FailureClass.BRIDGE_PROTOCOL, "GetProjectManager returned no object")
        return pm

    def _project(self, required: bool = True) -> Optional[Handle]:
        p = self._call(self._pm(), "GetCurrentProject")
        if not isinstance(p, Handle):
            if required:
                raise AdapterError(FailureClass.PRECONDITION, "no project is open in Resolve")
            return None
        return p

    def _media_pool(self) -> Handle:
        return self._call(self._project(), "GetMediaPool")

    def _timeline(self, name: str) -> Optional[Handle]:
        project = self._project()
        count = _int(self._call(project, "GetTimelineCount")) or 0
        for i in range(1, count + 1):
            tl = self._call(project, "GetTimelineByIndex", i)
            if isinstance(tl, Handle) and self._call(tl, "GetName") == name:
                return tl
        return None

    def _require_timeline(self, name: str) -> Handle:
        tl = self._timeline(name)
        if tl is None:
            raise AdapterError(FailureClass.PRECONDITION, "timeline not found", {"timeline": name})
        return tl

    def _all_clips(self) -> List[Handle]:
        out: List[Handle] = []
        stack = [self._call(self._media_pool(), "GetRootFolder")]
        seen = 0
        while stack and seen < 10000:
            folder = stack.pop()
            seen += 1
            for clip in self._call(folder, "GetClipList") or []:
                if isinstance(clip, Handle):
                    out.append(clip)
            for sub in self._call(folder, "GetSubFolderList") or []:
                if isinstance(sub, Handle):
                    stack.append(sub)
        return out

    def _clip_by_id(self, media_id: str) -> Optional[Handle]:
        for clip in self._all_clips():
            if self._call(clip, "GetUniqueId") == media_id:
                return clip
        return None

    def _item_record(self, item: Handle, kind: str, index: int) -> Dict[str, Any]:
        start, end = _int(self._call(item, "GetStart")), _int(self._call(item, "GetEnd"))
        rec: Dict[str, Any] = {"track_kind": kind, "track_index": index, "start": start, "end": end,
                               "duration": _int(self._call(item, "GetDuration")),
                               "name": self._call(item, "GetName")}
        try:
            rec["id"] = self._call(item, "GetUniqueId")
        except AdapterError:
            rec["id"] = None
        mpi = self._call(item, "GetMediaPoolItem")
        rec["media_id"] = self._call(mpi, "GetUniqueId") if isinstance(mpi, Handle) else None
        for key, method in (("source_start", "GetSourceStartFrame"), ("source_end", "GetSourceEndFrame")):
            try:
                rec[key] = _int(self._call(item, method))
            except AdapterError:
                rec[key] = None
        return rec

    def _item_at(self, name: str, kind: str, index: int, start: int) -> Optional[Handle]:
        tl = self._require_timeline(name)
        found = None
        for item in self._call(tl, "GetItemListInTrack", kind, index) or []:
            if isinstance(item, Handle) and _int(self._call(item, "GetStart")) == start:
                found = item
        return found

    # ── identity ──
    def health(self) -> Dict[str, Any]:
        h = self.c.health()
        product = self._call(ROOT, "GetProductName")
        version = self._call(ROOT, "GetVersionString")
        return {"product": product, "version": version, "bridge_edition": h.get("edition"),
                "bridge_version": h.get("version")}

    def current_project(self) -> Optional[str]:
        p = self._project(required=False)
        return self._call(p, "GetName") if p else None

    def project_names(self) -> List[str]:
        return [str(x) for x in (self._call(self._pm(), "GetProjectListInCurrentFolder") or [])]

    def create_project(self, name: str) -> bool:
        return isinstance(self._call(self._pm(), "CreateProject", name), Handle)

    def save_project(self) -> bool:
        return bool(self._call(self._pm(), "SaveProject"))

    def project_setting(self, key: str) -> Any:
        return self._call(self._project(), "GetSetting", key)

    def set_project_setting(self, key: str, value: str) -> bool:
        return bool(self._call(self._project(), "SetSetting", key, value))

    # ── media pool ──
    def media_items(self) -> List[Dict[str, Any]]:
        out = []
        for clip in self._all_clips():
            props = self._call(clip, "GetClipProperty") or {}
            if not isinstance(props, dict):
                props = {}
            frames, frames_from = clip_frames(props)
            out.append({"id": self._call(clip, "GetUniqueId"), "path": props.get("File Path") or "",
                        "name": props.get("Clip Name") or props.get("File Name") or "",
                        "frames": frames, "frames_from": frames_from, "fps": str(props.get("FPS") or ""),
                        "type": props.get("Type") or "", "resolution": props.get("Resolution") or "",
                        "start_tc": props.get("Start TC") or "", "audio_channels": props.get("Audio Ch") or "",
                        "duration_tc": props.get("Duration") or ""})
        return out

    def import_media(self, paths: Sequence[str]) -> List[str]:
        items = self._call(self._media_pool(), "ImportMedia", list(paths)) or []
        return [self._call(i, "GetUniqueId") for i in items if isinstance(i, Handle)]

    # ── timelines ──
    def timeline_names(self) -> List[str]:
        project = self._project()
        count = _int(self._call(project, "GetTimelineCount")) or 0
        return [self._call(self._call(project, "GetTimelineByIndex", i), "GetName") for i in range(1, count + 1)]

    def current_timeline(self) -> Optional[str]:
        tl = self._call(self._project(), "GetCurrentTimeline")
        return self._call(tl, "GetName") if isinstance(tl, Handle) else None

    def set_current_timeline(self, name: str) -> bool:
        tl = self._timeline(name)
        return bool(tl is not None and self._call(self._project(), "SetCurrentTimeline", tl))

    def create_timeline(self, name: str) -> bool:
        return isinstance(self._call(self._media_pool(), "CreateEmptyTimeline", name), Handle)

    def timeline_info(self, name: str) -> Optional[Dict[str, Any]]:
        tl = self._timeline(name)
        if tl is None:
            return None
        return {"name": name, "start_frame": _int(self._call(tl, "GetStartFrame")),
                "end_frame": _int(self._call(tl, "GetEndFrame")),
                "frame_rate": str(self._call(tl, "GetSetting", "timelineFrameRate") or ""),
                "width": _int(self._call(tl, "GetSetting", "timelineResolutionWidth")),
                "height": _int(self._call(tl, "GetSetting", "timelineResolutionHeight")),
                "start_timecode": self._call(tl, "GetStartTimecode"),
                "track_counts": {k: _int(self._call(tl, "GetTrackCount", k)) or 0 for k in _ITEM_KINDS}}

    def set_timeline_setting(self, name: str, key: str, value: str) -> bool:
        return bool(self._call(self._require_timeline(name), "SetSetting", key, value))

    def set_timeline_start_timecode(self, name: str, timecode: str) -> bool:
        return bool(self._call(self._require_timeline(name), "SetStartTimecode", timecode))

    def add_track(self, name: str, kind: str, subtype: Optional[str]) -> bool:
        tl = self._require_timeline(name)
        args = (kind, subtype) if subtype else (kind,)
        return bool(self._call(tl, "AddTrack", *args))

    def track_items(self, name: str, kind: str, index: int) -> List[Dict[str, Any]]:
        tl = self._require_timeline(name)
        items = self._call(tl, "GetItemListInTrack", kind, index) or []
        return [self._item_record(i, kind, index) for i in items if isinstance(i, Handle)]

    def append(self, name: str, clip_infos: Sequence[Mapping[str, Any]]) -> int:
        tl = self._require_timeline(name)
        if not self._call(self._project(), "SetCurrentTimeline", tl):
            return 0
        infos = []
        for ci in clip_infos:
            clip = self._clip_by_id(str(ci["media_id"]))
            if clip is None:
                raise AdapterError(FailureClass.PRECONDITION, "media pool item not found", {"media_id": ci["media_id"]})
            info: Dict[str, Any] = {"mediaPoolItem": clip, "startFrame": int(ci["start"]), "endFrame": int(ci["end"]),
                                    "recordFrame": int(ci["record"]), "trackIndex": int(ci["track_index"])}
            if ci.get("media_type") is not None:
                info["mediaType"] = int(ci["media_type"])
            infos.append(info)
        result = self._call(self._media_pool(), "AppendToTimeline", infos)
        return len([x for x in (result or []) if isinstance(x, Handle)])

    def item_properties(self, name: str, kind: str, index: int, start: int, keys: Sequence[str]) -> Dict[str, Any]:
        item = self._item_at(name, kind, index, start)
        if item is None:
            raise AdapterError(FailureClass.PRECONDITION, "timeline item not found",
                               {"track_kind": kind, "track_index": index, "start_frame": start})
        return {k: self._call(item, "GetProperty", k) for k in keys}

    def set_item_property(self, name: str, kind: str, index: int, start: int, key: str, value: Any) -> bool:
        item = self._item_at(name, kind, index, start)
        if item is None:
            raise AdapterError(FailureClass.PRECONDITION, "timeline item not found",
                               {"track_kind": kind, "track_index": index, "start_frame": start})
        return bool(self._call(item, "SetProperty", key, value))

    def delete_items(self, name: str, refs: Sequence[Mapping[str, int]]) -> bool:
        tl = self._require_timeline(name)
        handles = []
        for r in refs:
            h = self._item_at(name, str(r["track_kind"]), int(r["track_index"]), int(r["start_frame"]))
            if h is None:
                raise AdapterError(FailureClass.PRECONDITION, "timeline item not found", dict(r))
            handles.append(h)
        return bool(self._call(tl, "DeleteClips", handles, False))

    def add_marker(self, name: str, frame: int, color: str, title: str, note: str, duration: int) -> bool:
        return bool(self._call(self._require_timeline(name), "AddMarker", frame, color, title, note, duration, ""))

    def markers(self, name: str) -> Dict[int, Dict[str, Any]]:
        raw = self._call(self._require_timeline(name), "GetMarkers") or {}
        out = {}
        for k, v in raw.items() if isinstance(raw, dict) else ():
            f = _int(k) if not isinstance(k, float) else int(k)
            if f is not None:
                out[f] = dict(v) if isinstance(v, dict) else {}
        return out

    def has_method(self, object_kind: str, method: str) -> bool:
        target = self._probe_target(object_kind)
        return target is not None and method in self.c.methods(target)

    def methods_of(self, object_kind: str) -> List[str]:
        target = self._probe_target(object_kind)
        return self.c.methods(target) if target is not None else []

    def _probe_target(self, object_kind: str) -> Optional[Any]:
        if object_kind not in _PROBE_KINDS:
            raise AdapterError(FailureClass.VALIDATION, "unknown object kind", {"kind": object_kind})
        if object_kind == "Resolve":
            return ROOT
        if object_kind == "ProjectManager":
            return self._pm()
        project = self._project(required=False)
        if project is None:
            return None
        if object_kind == "Project":
            return project
        if object_kind == "MediaPool":
            return self._call(project, "GetMediaPool")
        if object_kind == "MediaPoolItem":
            clips = self._all_clips()
            return clips[0] if clips else None
        tl = self._call(project, "GetCurrentTimeline")
        if not isinstance(tl, Handle):
            return None
        if object_kind == "Timeline":
            return tl
        for kind in ("video", "audio"):
            for idx in range(1, (_int(self._call(tl, "GetTrackCount", kind)) or 0) + 1):
                items = self._call(tl, "GetItemListInTrack", kind, idx) or []
                if items:
                    return items[0]
        return None

    def create_title_clip(self, title_timeline: str, text: str, duration: int, size: Optional[float],
                          center: Optional[Sequence[float]], frame_rate: Optional[str] = None) -> Optional[str]:
        """Text+ on its own timeline (lands on V1 there), text set through the Fusion comp; returns the nested
        timeline's media pool item id. The caller places that item with `append`."""
        project = self._project()
        previous = self._call(project, "GetCurrentTimeline")
        tl = self._call(self._media_pool(), "CreateEmptyTimeline", title_timeline)
        if not isinstance(tl, Handle):
            return None
        if frame_rate:
            # Integer rates only (measured); otherwise the title timeline keeps the project rate and the
            # executor converts its length with the floor rule.
            self._call(tl, "SetSetting", "useCustomSettings", "1")
            self._call(tl, "SetSetting", "timelineFrameRate", frame_rate)
        try:
            if not self._call(project, "SetCurrentTimeline", tl):
                return None
            item = self._call(tl, "InsertFusionTitleIntoTimeline", "Text+")
            if not isinstance(item, Handle):
                return None
            comp = self._call(item, "GetFusionCompByIndex", 1)
            if not isinstance(comp, Handle):
                return None
            tool = self._call(comp, "FindTool", "Template")
            if not isinstance(tool, Handle):
                return None
            self._call(tool, "SetInput", "StyledText", text)
            if size is not None:
                self._call(tool, "SetInput", "Size", float(size))
            if center is not None:
                self._call(tool, "SetInput", "Center", {"1": float(center[0]), "2": float(center[1])})
            mpi = self._call(tl, "GetMediaPoolItem")
            return self._call(mpi, "GetUniqueId") if isinstance(mpi, Handle) else None
        finally:
            if isinstance(previous, Handle):
                self._call(project, "SetCurrentTimeline", previous)

    def title_text(self, name: str, track_index: int, start: int) -> Optional[str]:
        """Read back the Text+ StyledText of a placed nested title (for verification)."""
        item = self._item_at(name, "video", track_index, start)
        if item is None:
            return None
        mpi = self._call(item, "GetMediaPoolItem")
        inner = self._call(mpi, "GetTimeline") if isinstance(mpi, Handle) else None
        if not isinstance(inner, Handle):
            return None
        for t in self._call(inner, "GetItemListInTrack", "video", 1) or []:
            comp = self._call(t, "GetFusionCompByIndex", 1)
            tool = self._call(comp, "FindTool", "Template") if isinstance(comp, Handle) else None
            if isinstance(tool, Handle):
                return self._call(tool, "GetInput", "StyledText")
        return None

    # ── pages ──
    def current_page(self) -> Optional[str]:
        return self._call(ROOT, "GetCurrentPage")

    def open_page(self, page: str) -> bool:
        return bool(self._call(ROOT, "OpenPage", page))

    # ── render ──
    def render_formats(self) -> Dict[str, str]:
        return dict(self._call(self._project(), "GetRenderFormats") or {})

    def render_codecs(self, fmt: str) -> Dict[str, str]:
        return dict(self._call(self._project(), "GetRenderCodecs", fmt) or {})

    def set_render_format_and_codec(self, fmt: str, codec: str) -> bool:
        return bool(self._call(self._project(), "SetCurrentRenderFormatAndCodec", fmt, codec))

    def set_render_settings(self, settings: Mapping[str, Any]) -> bool:
        return bool(self._call(self._project(), "SetRenderSettings", dict(settings)))

    def render_presets(self) -> List[str]:
        return [str(x) for x in (self._call(self._project(), "GetRenderPresetList") or [])]

    def load_render_preset(self, name: str) -> bool:
        return bool(self._call(self._project(), "LoadRenderPreset", name))

    def add_render_job(self) -> Optional[str]:
        job = self._call(self._project(), "AddRenderJob")
        return str(job) if job else None

    def render_jobs(self) -> List[Dict[str, Any]]:
        return [dict(j) for j in (self._call(self._project(), "GetRenderJobList") or []) if isinstance(j, dict)]

    def start_render(self, job_ids: Sequence[str]) -> bool:
        return bool(self._call(self._project(), "StartRendering", list(job_ids), False))

    def render_status(self, job_id: str) -> Dict[str, Any]:
        return dict(self._call(self._project(), "GetRenderJobStatus", job_id) or {})

    def is_rendering(self) -> bool:
        return bool(self._call(self._project(), "IsRenderingInProgress"))

    def stop_render(self) -> None:
        self._call(self._project(), "StopRendering")

    def delete_render_job(self, job_id: str) -> bool:
        return bool(self._call(self._project(), "DeleteRenderJob", job_id))
