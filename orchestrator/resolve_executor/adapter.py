"""The adapter boundary: the narrow, data-only surface the executor needs from Resolve.

Adapters return plain JSON-like data (dicts, lists, strings, ints, bools), never live Resolve objects, so nothing
above this layer can reach the raw scripting API. Two implementations exist:

* `fake.FakeResolve`: an in-memory simulation for offline tests (CI has no Resolve).
* `live.LiveResolveAdapter`: the local in-app bridge (127.0.0.1) of Resolve Free.

Conventions every adapter must keep:

* A plain `False`/`None` from Resolve is returned as `False`/`None`, never raised: the executor decides from a
  readback what actually changed.
* Transport problems raise `errors.AdapterError` with a `FailureClass` of BRIDGE_UNAVAILABLE (nothing sent),
  BRIDGE_TIMEOUT (sent, outcome unknown) or BRIDGE_PROTOCOL.
* Timeline item records are dicts with at least: `start`, `end` (exclusive), `duration`, `name`, `media_id`
  (None for generators/titles/transitions), `track_kind`, `track_index`, and where readable `source_start`,
  `source_end` (exclusive, source frames) and `id`.
* Clip infos for `append` are dicts: `media_id`, `start`, `end` (source frames, half-open), `record` (absolute
  timeline frame), `track_index`, `media_type` (1 video, 2 audio, or None for both).
"""

from __future__ import annotations

import abc
from typing import Any, Dict, List, Mapping, Optional, Sequence


class ResolveAdapter(abc.ABC):
    # ── identity ──
    @abc.abstractmethod
    def health(self) -> Dict[str, Any]:
        """{"product", "version", "edition"}; raises AdapterError when Resolve cannot be reached."""

    @abc.abstractmethod
    def current_project(self) -> Optional[str]: ...

    @abc.abstractmethod
    def project_names(self) -> List[str]: ...

    @abc.abstractmethod
    def create_project(self, name: str) -> bool: ...

    @abc.abstractmethod
    def save_project(self) -> bool: ...

    @abc.abstractmethod
    def project_setting(self, key: str) -> Any: ...

    @abc.abstractmethod
    def set_project_setting(self, key: str, value: str) -> bool: ...

    # ── media pool ──
    @abc.abstractmethod
    def media_items(self) -> List[Dict[str, Any]]:
        """Every clip in the media pool: {"id", "path", "name", "frames", "fps", "type", ...}."""

    @abc.abstractmethod
    def import_media(self, paths: Sequence[str]) -> List[str]: ...

    # ── timelines ──
    @abc.abstractmethod
    def timeline_names(self) -> List[str]: ...

    @abc.abstractmethod
    def current_timeline(self) -> Optional[str]: ...

    @abc.abstractmethod
    def set_current_timeline(self, name: str) -> bool: ...

    @abc.abstractmethod
    def create_timeline(self, name: str) -> bool: ...

    @abc.abstractmethod
    def timeline_info(self, name: str) -> Optional[Dict[str, Any]]:
        """{"name", "start_frame", "end_frame" (exclusive), "frame_rate" (Resolve string), "width", "height",
        "start_timecode", "track_counts": {"video", "audio", "subtitle"}} or None if absent."""

    @abc.abstractmethod
    def set_timeline_setting(self, name: str, key: str, value: str) -> bool: ...

    @abc.abstractmethod
    def set_timeline_start_timecode(self, name: str, timecode: str) -> bool: ...

    @abc.abstractmethod
    def add_track(self, name: str, kind: str, subtype: Optional[str]) -> bool: ...

    @abc.abstractmethod
    def track_items(self, name: str, kind: str, index: int) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def append(self, name: str, clip_infos: Sequence[Mapping[str, Any]]) -> int:
        """AppendToTimeline; returns how many items Resolve reported (NOT proof: the executor reads back)."""

    @abc.abstractmethod
    def item_properties(self, name: str, kind: str, index: int, start: int, keys: Sequence[str]) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def set_item_property(self, name: str, kind: str, index: int, start: int, key: str, value: Any) -> bool: ...

    @abc.abstractmethod
    def delete_items(self, name: str, refs: Sequence[Mapping[str, int]]) -> bool:
        """refs: [{"track_kind", "track_index", "start_frame"}]."""

    @abc.abstractmethod
    def add_marker(self, name: str, frame: int, color: str, title: str, note: str, duration: int) -> bool: ...

    @abc.abstractmethod
    def markers(self, name: str) -> Dict[int, Dict[str, Any]]: ...

    @abc.abstractmethod
    def has_method(self, object_kind: str, method: str) -> bool:
        """Capability probe on a live object kind ("Timeline", "TimelineItem", "Project", ...)."""

    @abc.abstractmethod
    def create_title_clip(self, title_timeline: str, text: str, duration: int, size: Optional[float],
                          center: Optional[Sequence[float]], frame_rate: Optional[str] = None) -> Optional[str]:
        """Build a Text+ title on its own timeline `title_timeline` (at `frame_rate` when given and settable);
        return that timeline's media pool item id."""

    @abc.abstractmethod
    def title_text(self, name: str, track_index: int, start: int) -> Optional[str]:
        """The Text+ text of a placed nested title, read through the nested timeline (None if unreadable)."""

    # ── pages ──
    @abc.abstractmethod
    def current_page(self) -> Optional[str]: ...

    @abc.abstractmethod
    def open_page(self, page: str) -> bool: ...

    # ── render ──
    @abc.abstractmethod
    def render_formats(self) -> Dict[str, str]: ...

    @abc.abstractmethod
    def render_codecs(self, fmt: str) -> Dict[str, str]: ...

    @abc.abstractmethod
    def set_render_format_and_codec(self, fmt: str, codec: str) -> bool: ...

    @abc.abstractmethod
    def set_render_settings(self, settings: Mapping[str, Any]) -> bool: ...

    @abc.abstractmethod
    def render_presets(self) -> List[str]: ...

    @abc.abstractmethod
    def load_render_preset(self, name: str) -> bool: ...

    @abc.abstractmethod
    def add_render_job(self) -> Optional[str]: ...

    @abc.abstractmethod
    def render_jobs(self) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def start_render(self, job_ids: Sequence[str]) -> bool: ...

    @abc.abstractmethod
    def render_status(self, job_id: str) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def is_rendering(self) -> bool: ...

    @abc.abstractmethod
    def stop_render(self) -> None: ...

    @abc.abstractmethod
    def delete_render_job(self, job_id: str) -> bool: ...
