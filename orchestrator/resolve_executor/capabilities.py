"""Capability inventory: what this package can do in Resolve Free 21.0.4.5, and how well that is proven.

Status meanings (the executor enforces them):

* VERIFIED     - exercised live by this package's live suite on Resolve Free 21.0.4.5 with synthetic media, with
                 readback (and, for renders, ffprobe) agreeing. Evidence id in `evidence`.
* IMPLEMENTED  - code and offline tests exist; no live evidence from this package yet. Executable.
* NOT_TESTED   - the API or MCP tool exists but this package has no code or test for it. Refused.
* UNSUPPORTED  - the API does not offer it on this build, or a known failure makes it unsafe. Refused.

Provenance of VERIFIED: `E-RSV-LIVE-0001` (PROJECT_STATUS.json) was observed by the previous T-RSV-002 session on
2026-11-18 at repository commit 7bddffe. Its evidence file is machine-local and was NOT re-observed in the takeover
review, which was offline. Offline changes made since (executor gate order, fail-closed checks) are IMPLEMENTED on
top of that and are not themselves live evidence.

Entries without an `OPERATION_CAPABILITY` mapping are descriptive (behaviours, limits, things with no request type);
the executor cannot be asked to do them, so they are refused by construction.

A third-party measurement (the MCP project's `api_truth` ledger, mostly Resolve Studio) is cited as context in
`limitations`; it never counts as VERIFIED here.

`OPERATION_CAPABILITY` maps every request kind to its capability. The executor refuses a request whose capability is
NOT_TESTED or UNSUPPORTED before anything is sent to Resolve.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Mapping, Tuple

VERIFIED, IMPLEMENTED, NOT_TESTED, UNSUPPORTED = "VERIFIED", "IMPLEMENTED", "NOT_TESTED", "UNSUPPORTED"
EXECUTABLE = (VERIFIED, IMPLEMENTED)
EDITION = "Resolve Free 21.0.4.5 (Windows) through the in-app bridge"


@dataclass(frozen=True)
class Capability:
    name: str
    area: str
    api: str                      # Resolve scripting calls used (or that would be needed)
    mcp: str                      # matching MCP tool/action, informational only
    inputs: str                   # request type and required parameters
    status: str
    edition: str
    limitations: str
    verification: str             # how the result is checked
    evidence: str = ""            # live evidence id (VERIFIED only)

    def to_dict(self) -> dict:
        return asdict(self)


def _c(name, area, api, mcp, inputs, status, limitations, verification, evidence="", edition=EDITION) -> Capability:
    return Capability(name, area, api, mcp, inputs, status, edition, limitations, verification, evidence)


CAPABILITIES: Tuple[Capability, ...] = (
    _c("inspect_project", "project inspection",
       "GetProjectManager, GetCurrentProject, GetName, GetTimelineCount/ByIndex, GetSetting", "project_manager get_current; project_settings get_setting",
       "InspectProject(op_id)", VERIFIED, "Read-only.", "Values read directly.", evidence="E-RSV-LIVE-0001"),
    _c("inspect_media_pool", "media pool inspection",
       "MediaPool.GetRootFolder, Folder.GetClipList/GetSubFolderList, MediaPoolItem.GetClipProperty/GetUniqueId",
       "media_pool get_root_folder; folder get_clips; media_pool_item get_clip_property",
       "part of InspectProject / preflight", VERIFIED, "Walks every folder; slow for very large pools.",
       "Values read directly.", evidence="E-RSV-LIVE-0001"),
    _c("import_media", "media pool", "MediaPool.ImportMedia([paths])", "media_pool import_media",
       "ImportMedia(op_id, paths)", VERIFIED,
       "Lands in the CURRENT media pool folder. Paths must exist and lie inside allowed media roots. Never copies or moves the file. Measured: a WAV reports Frames empty and FPS = the project rate at import; its frame count comes from the Duration timecode.",
       "Media pool re-read: each path present exactly once.", evidence="E-RSV-LIVE-0001"),
    _c("create_project", "project", "ProjectManager.CreateProject(name)", "project_manager create",
       "CreateProject(op_id, name)", VERIFIED, "Refused if the name exists; opens the new project.",
       "Current project name read back.", evidence="E-RSV-LIVE-0001"),
    _c("save_project", "project saving", "ProjectManager.SaveProject()", "project_manager save",
       "SaveProject(op_id)", VERIFIED, "Returns a bool only; Resolve has no save-state readback.",
       "Return value; journal checkpoint records the timeline structure alongside.", evidence="E-RSV-LIVE-0001"),
    _c("create_timeline", "timeline creation",
       "MediaPool.CreateEmptyTimeline, Timeline.SetSetting(useCustomSettings, timelineFrameRate, resolution), SetStartTimecode",
       "media_pool create_timeline; timeline set_setting", "CreateTimeline(op_id, name, frame_rate, width, height, start_timecode)",
       VERIFIED, "Measured: Timeline.SetSetting(timelineFrameRate) accepts integer rates only (24/25/30/48/50/60). NTSC rates (23.976/29.97/59.94) come only from the project rate, which can change only while the project has no timelines; the executor sets it for the first timeline and refuses a second NTSC rate.",
       "timeline_info read back: rate, resolution, start frame.", evidence="E-RSV-LIVE-0001"),
    _c("inspect_timeline", "timeline inspection",
       "Timeline.GetStartFrame/GetEndFrame/GetSetting/GetTrackCount/GetItemListInTrack, TimelineItem getters",
       "timeline get_items; timeline_item get_*", "InspectTimeline(op_id, timeline)", VERIFIED,
       "GetEnd/GetEndFrame are exclusive.", "Values read directly.", evidence="E-RSV-LIVE-0001"),
    _c("add_track", "track management", "Timeline.AddTrack(kind, subTrackType)", "timeline add_track",
       "AddTrack(op_id, timeline, track_kind, audio_format)", VERIFIED,
       "Video and audio only; subtitle tracks are refused (see add_subtitle).", "Track count +1 read back.", evidence="E-RSV-LIVE-0001"),
    _c("append_segment", "clip insertion, positioning and source trimming",
       "MediaPool.AppendToTimeline([{mediaPoolItem, startFrame, endFrame, recordFrame, trackIndex, mediaType}])",
       "media_pool append_to_timeline", "AppendSegment(op_id, timeline, media_path, source, track_index, record_frame, media, audio_track_index)",
       VERIFIED,
       "Measured: half-open [startFrame, endFrame) source frames (pixel-verified); mixed rates floor the duration; Resolve ACCEPTS a range past the file end and freezes the last frame, and REPORTS SUCCESS for an overlapping placement while placing nothing, so the executor refuses both before sending. GetSourceEndFrame readback is inconsistent and is not used for verification.",
       "Track re-read: an item exists at record_frame with the expected duration and source range; nothing else on the track changed.", evidence="E-RSV-LIVE-0001"),
    _c("set_transform", "transform and crop", "TimelineItem.SetProperty / GetProperty (ZoomX, Pan, Tilt, RotationAngle, Crop*, Opacity, Flip*)",
       "timeline_item set_property / get_transform / get_crop", "SetTransform(op_id, timeline, item, values)", VERIFIED,
       "Zoom is relative to the fitted image; Pan/Tilt are not raw pixels for clips narrower than the timeline. Inspector keyframes would override (none on fresh appends).",
       "GetProperty read back equals the request (1e-4).", evidence="E-RSV-LIVE-0001"),
    _c("add_marker", "markers", "Timeline.AddMarker(frame, color, name, note, duration, customData), GetMarkers",
       "timeline_markers add / get_all", "AddMarker(op_id, timeline, frame, color, name, note, duration)", VERIFIED,
       "Frame is relative to the timeline start; one marker per frame.", "GetMarkers read back at that frame.", evidence="E-RSV-LIVE-0001"),
    _c("place_title", "titles and text",
       "CreateEmptyTimeline + InsertFusionTitleIntoTimeline('Text+') + Fusion Template SetInput(StyledText, Size, Center) + AppendToTimeline of the nested timeline",
       "timeline insert_fusion_title; fusion_comp set_input", "PlaceTitle(op_id, timeline, text, track_index, record_frame, duration, size, center)",
       VERIFIED,
       "Insert*IntoTimeline cannot target a track, so the title lives on its own nested timeline (set to the target rate when integer) placed like a clip. Duration cannot exceed the Text+ default length (5 s). One nested timeline per title. Styling beyond Size/Center (font, colour) is not implemented.",
       "Placed item read back (track, start, duration) and the Text+ StyledText read back through the nested timeline.", evidence="E-RSV-LIVE-0001"),
    _c("render_timeline", "render configuration, monitoring and export verification",
       "SetCurrentRenderFormatAndCodec, SetRenderSettings, AddRenderJob, GetRenderJobList, StartRendering, GetRenderJobStatus, StopRendering, DeleteRenderJob",
       "render set_format_and_codec / set_settings / add_job / start / get_job_status", "RenderTimeline(op_id, timeline, spec)",
       VERIFIED,
       "Output only inside allowed output roots; never overwrites. Measured: {SelectAllFrames: False} alone is rejected, MarkIn/MarkOut alone set a range and MarkOut is INCLUSIVE; the queued job reads back marks, size, codec and audio flag. JobStatus text is localized: completion is CompletionPercentage 100 with no Error, then the FILE is verified with ffprobe. A timeout stops the render (Resolve left rendering state within the wait).",
       "Job list entry matches target dir/name; ffprobe: codec, dimensions, frame rate, frame count, audio stream.", evidence="E-RSV-LIVE-0001"),
    _c("verify_export", "export verification", "ffprobe (no Resolve call)", "render verify_output",
       "VerifyExport(op_id, path, expect)", VERIFIED, "Needs ffprobe; fails closed without it.", "ffprobe stream comparison.", evidence="E-RSV-LIVE-0001"),
    _c("delete_items", "recovery (remove wrong placements)", "Timeline.DeleteClips([items], False)", "timeline delete_clips",
       "DeleteItems(op_id, timeline, items)", VERIFIED, "Timeline items only; never media or files. Linked audio must be listed explicitly.",
       "Track re-read: the items are gone, the rest unchanged.", evidence="E-RSV-LIVE-0001"),
    # ── refused ──
    _c("set_clip_audio_level", "audio controls", "TimelineItem.SetProperty('Volume') (does not exist)", "none",
       "SetClipAudioLevel(...)", UNSUPPORTED,
       "Confirmed live (E-RSV-LIVE-0001): SetProperty('Volume'|'Level'|'AudioVolume') returns False on an audio item, and TimelineItem has no audio-level method. No API for clip or track volume, pan, EQ, fades or automation; only whole Fairlight presets can be applied.",
       "Refused before sending."),
    _c("audio_fades", "audio controls", "none", "none", "not a request type", UNSUPPORTED,
       "No fade-handle API for clips on 21.0.", "n/a"),
    _c("add_transition", "transitions and effects", "TimelineItem.AddTransition (21.1+ only)", "timeline_item add_transition",
       "AddTransition(...)", UNSUPPORTED,
       "Confirmed live: TimelineItem has no AddTransition on Free 21.0.4.5. Resolve 21.1 adds it, but Free 21.1 has no Python scripting at all.", "Refused before sending."),
    _c("add_subtitle", "captions/subtitles", "ImportMedia(srt) + AppendToTimeline (subtitle clipInfo)", "timeline_ai create_subtitles (Studio)",
       "AddSubtitle(...)", UNSUPPORTED,
       "Not tested here on purpose: subtitle clipInfo appends were reported to abort Resolve on 21.x (third-party), and there is no API to set caption text. Burn-in captions use place_title.",
       "Refused before sending."),
    _c("noise_reduction_eq", "audio controls", "none", "none", "not a request type", UNSUPPORTED,
       "No noise-reduction or EQ API; voice isolation state only.", "n/a"),
    _c("edit_modes", "clip insertion", "none (insert/overwrite/ripple)", "none", "not a request type", UNSUPPORTED,
       "Only AppendToTimeline with recordFrame exists; no insert/overwrite/ripple edit API (method inventory).", "n/a"),
    _c("project_recovery", "project saving and recovery", "none (no undo API)", "timeline_versioning (MCP-side snapshots)",
       "journal checkpoints + DeleteItems", VERIFIED,
       "Resolve has no scripted undo. Recovery = journal (pre-state, readback) + explicit removal of wrong items; never automatic repetition.",
       "Journal records and readback.", evidence="E-RSV-LIVE-0001"),
    # ── behaviours that are not request kinds: descriptive entries with their honest status ──
    _c("render_failure_reported_by_resolve", "render monitoring",
       "GetRenderJobStatus (Error text, CompletionPercentage)", "render get_job_status",
       "part of RenderTimeline", IMPLEMENTED,
       "A job whose Error field is set is classified render_failed and the output is not trusted. Resolve never failed a job on synthetic media, so this path was never observed live; it is covered with the offline fake only.",
       "Offline fake (render_behaviour='error')."),
    _c("interrupted_mutation_handling", "recovery",
       "none (journal logic)", "none", "every mutating request", IMPLEMENTED,
       "An unknown outcome (lost bridge after sending, unreadable readback, unexpected exception) leaves the journal 'begin' open and blocks the idempotency key (or 'op:<op_id>') until resolve_uncertain records an inspection; nothing is repeated automatically. Offline fault injection only; a real bridge restart in the middle of a mutation was not provoked live.",
       "Offline fault injection and journal state."),
    _c("create_timeline_high_frame_rates", "timeline creation", "Timeline.SetSetting(timelineFrameRate)", "timeline set_setting",
       "CreateTimeline(..., frame_rate in 30/50/59.94/60)", IMPLEMENTED,
       "Validation accepts 23.976, 24, 25, 29.97, 30, 50, 59.94 and 60. Live runs used 24, 25, 29.97 and the 23.976 source-rate conversion; 48/50/60 fps timelines were not exercised live.",
       "timeline_info read back: rate."),
    _c("drop_frame_timecode", "timecode", "none", "none", "not a request type", UNSUPPORTED,
       "timebase refuses drop-frame separators rather than miscount; only non-drop timecode is implemented.", "n/a"),
    _c("variable_frame_rate_sources", "media", "MediaPoolItem.GetClipProperty('FPS')", "media_pool_item get_clip_property",
       "AppendSegment of a VFR file", NOT_TESTED,
       "Resolve reports one FPS per clip; nothing here detects or conditions variable-frame-rate media. Not exercised; the frame arithmetic assumes a constant rate.",
       "Refused: no code path distinguishes VFR media; use constant-rate (or conformed) media only."),
    _c("link_video_audio_clips", "clip insertion", "TimelineItem link state (no scripting call known)", "none",
       "not a request type", NOT_TESTED,
       "A 'video_audio' append places both parts in sync on separate tracks but does not link them as one clip.",
       "n/a"),
    _c("text_plus_styling", "titles and text", "Fusion Text+ inputs (Font, Color, Background)", "fusion_comp set_input",
       "not a request type", NOT_TESTED,
       "Only StyledText, Size and Center are set; font, colour, background and safe-area styling are not implemented.",
       "n/a"),
    _c("proxy_relink_retime_multicam_compound", "advanced editing", "various", "various", "not a request type",
       NOT_TESTED, "No request type exists for proxies, relinking, retiming, multicam, compound clips or Fusion beyond Text+.",
       "n/a"),
)

CAPABILITY_BY_NAME: Dict[str, Capability] = {c.name: c for c in CAPABILITIES}

OPERATION_CAPABILITY: Mapping[str, str] = {
    "inspect_project": "inspect_project", "inspect_timeline": "inspect_timeline", "verify_export": "verify_export",
    "create_project": "create_project", "import_media": "import_media", "create_timeline": "create_timeline",
    "add_track": "add_track", "append_segment": "append_segment", "set_transform": "set_transform",
    "add_marker": "add_marker", "place_title": "place_title", "set_clip_audio_level": "set_clip_audio_level",
    "add_transition": "add_transition", "add_subtitle": "add_subtitle", "delete_items": "delete_items",
    "save_project": "save_project", "render_timeline": "render_timeline",
}


def capability_for(kind: str) -> Capability:
    return CAPABILITY_BY_NAME[OPERATION_CAPABILITY[kind]]


# The 37 tools of the installed Resolve MCP server (davinci-resolve-mcp 4.19.3), all with the input schema
# {"action": str, "params": object}. Action counts were extracted from the server source; a tool being listed is not
# evidence that any action works. This package does not call the MCP server: it is the LLM-facing alternative.
MCP_TOOLS: Mapping[str, int] = {
    "setup": 12, "resolve_control": 48, "layout_presets": 7, "render_presets": 8, "project_manager": 32,
    "project_manager_folders": 7, "project_manager_cloud": 4, "project_manager_database": 3, "project_settings": 20,
    "render": 40, "media_storage": 7, "media_pool": 52, "folder": 13, "media_pool_item": 34,
    "media_pool_item_markers": 16, "media_analysis": 75, "timeline_versioning": 12, "edit_engine": 30,
    "timeline": 103, "timeline_markers": 22, "timeline_frame": 2, "timeline_ai": 9, "timeline_item": 54,
    "timeline_item_markers": 14, "timeline_item_fusion": 12, "timeline_item_color": 39, "timeline_item_takes": 7,
    "gallery": 8, "gallery_stills": 7, "graph": 12, "color_group": 6, "fusion_comp": 42, "fuse_plugin": 8, "lut": 7,
    "dctl": 10, "script_plugin": 17, "knowledge": 5,
}
