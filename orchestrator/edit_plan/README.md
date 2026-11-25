# edit_plan — validated edit-plan schema and offline Editor planning (T-EDT-001, M-EDT-01)

**Evidence level: unit tests only, offline, synthetic data. Nothing here is VERIFIED.** No Resolve, no network, no real
media, no file is opened. Standard library only (Python 3.14). Contract version: `edit-plan/0.1`.

## What exists
| Module | Purpose |
|---|---|
| `schema.py`, `_codec.py` | frozen dataclasses; strict `to_dict`/`from_dict` (unknown field/version, missing field, wrong type -> `PlanSchemaError` with a `SCHEMA_*` code); `json_schema()` (draft 2020-12) generated from the same dataclasses |
| `validate.py` | `validate_plan(plan, export_allowlist=(), transcript=None)` and `validate_plan_dict(data, ...)` -> `ValidationReport`; never raises on bad data |
| `fidelity.py` | source-fidelity rules (hard errors) |
| `capability.py` | `build_requests`, `check_capabilities`, `to_executor_requests(plan)` (builds `resolve_executor` requests, executes nothing) |
| `canonical.py` | `canonical_json`, `plan_hash` (sha256; sorted keys, rates reduced, NFC text) |
| `planner.py` | `plan_from_brief(brief, inventory, transcript=None)` -> candidate plan + report |
| `frames.py`, `paths.py` | exact frame helpers (reuse `resolve_executor.timebase`); lexical Windows path comparison |

## Conventions
* Frame rates are exact rationals. `record_in` is relative to the timeline start (0 = first frame at `start_timecode`);
  `to_executor_requests` adds the start timecode to make the absolute `record_frame` the executor expects.
* Each segment states `range_convention`: `inclusive_out` (last included frame) or `exclusive_out` (first excluded
  frame). One frame = `in == out` inclusive, or `out == in + 1` exclusive. Never guessed.
* Timeline duration = floor(source length x timeline rate / source rate) (as Resolve does). A rate difference is
  warned (`RATE_MIXED`); an inexact conversion is warned too (`RATE_CONVERSION_INEXACT`).
* Transcript for caption checks: `source_id -> [{"start_frame", "end_frame", "text"}]`, source frames, half-open.
  Without a transcript a caption cannot be verified: warning `CAPTION_UNVERIFIED_NO_TRANSCRIPT`.
* Export allowlist is an argument of validation (`export_allowlist=`), never read from the environment. Empty = every
  export refused.

## Issue codes
Schema: `SCHEMA_UNKNOWN_VERSION`, `SCHEMA_UNKNOWN_FIELD`, `SCHEMA_MISSING_FIELD`, `SCHEMA_BAD_VALUE`.
Structure: `META_INVALID`, `ID_INVALID`, `DUPLICATE_ID`, `TIMELINE_RATE_INVALID`, `TIMELINE_SIZE_INVALID`,
`START_TIMECODE_INVALID`, `SOURCE_RATE_INVALID`, `SOURCE_DURATION_INVALID`, `SOURCE_HASH_INVALID`, `SOURCE_PATH_INVALID`,
`SOURCE_NO_MEDIA`, `TRACK_KIND_INVALID`, `TRACK_INDEX_INVALID`, `TRACK_INDEX_DUPLICATE`, `EMPTY_PLAN`, `PURPOSE_INVALID`,
`MEDIA_INVALID`, `UNKNOWN_TRACK`, `TRACK_KIND_MISMATCH`, `AUDIO_TRACK_MISSING`, `AUDIO_TRACK_UNEXPECTED`,
`UNKNOWN_SOURCE`, `SOURCE_LACKS_MEDIA`, `PURPOSE_MEDIA_MISMATCH`, `TITLE_HAS_SOURCE`, `TITLE_FIELDS_INVALID`,
`TITLE_FIELDS_ON_NON_TITLE`, `RATIONALE_MISSING`, (warning) `RATIONALE_NOT_LINKED`.
Frames: `RANGE_CONVENTION_MISSING`, `SEGMENT_MISSING_RANGE`, `RANGE_NEGATIVE`, `RANGE_INVERTED`, `RANGE_EMPTY`,
`RANGE_OUT_OF_SOURCE`, `RECORD_NEGATIVE`, `ZERO_DURATION`, `TRACK_OVERLAP`, (warnings) `GAP`, `RATE_MIXED`,
`RATE_CONVERSION_INEXACT`. Gaps are always in `report.gaps`; only gaps on a base track (index 1) also warn.
Transitions/markers/captions/QC: `TRANSITION_UNKNOWN_SEGMENT`, `TRANSITION_NOT_ADJACENT`, `TRANSITION_DURATION_INVALID`,
`MARKER_OUT_OF_RANGE`, `MARKER_DUPLICATE_FRAME`, `MARKER_COLOR_INVALID`, `MARKER_DURATION_INVALID`,
`CAPTION_DURATION_INVALID`, `QC_EXPECTATION_INVALID`, `QC_EXPECTATION_UNMET`, `QC_DUPLICATE_ID`.
Fidelity: `FIDELITY_GENERATED_SPEECH`, `FIDELITY_DIALOGUE_NO_AUDIO`, `FIDELITY_DIALOGUE_NOT_RECORDED`,
`FIDELITY_DIALOGUE_NOT_CREATOR`, `FIDELITY_DIALOGUE_SPEAKER`, `FIDELITY_SPEAKER_ORIGIN_MISMATCH`, `CAPTION_TEXT_EMPTY`,
`CAPTION_NO_TRANSCRIPT_REF`, `CAPTION_SOURCE_UNKNOWN`, `CAPTION_REF_RANGE_INVALID`, `CAPTION_REF_OUT_OF_SOURCE`,
`CAPTION_NOT_QUOTED`, (warning) `CAPTION_UNVERIFIED_NO_TRANSCRIPT`, `EXPORT_PATH_INVALID`, `EXPORT_NAME_INVALID`,
`EXPORT_UNDER_PROJECTS`, `EXPORT_INSIDE_SOURCE_DIR`, `EXPORT_OVERWRITES_SOURCE`, `EXPORT_NOT_ALLOWLISTED`,
`EXPORT_DUPLICATE`.
Capability: `CAP_UNSUPPORTED` (executor capability not executable), `CAP_REQUEST_INVALID` (the executor's own validation
refuses the request), `CAP_UNMAPPABLE`. Internal: `VALIDATION_INTERNAL_ERROR`.
Planner: `PLANNER_BRIEF_UNUSABLE`, `PLANNER_NO_ARC`, `PLANNER_NO_SOURCES`, `PLANNER_UNKNOWN_SOURCE`,
`PLANNER_NOTHING_TO_PLACE`, `PLANNER_INPUT_INVALID`.

## Capability gate (against resolve_executor 21.0.4.5 capabilities)
Refused as hard errors, never dropped: `audio_gain_db` (set_clip_audio_level), `dissolve`/`dip_to_black` transitions
(add_transition), subtitle tracks and `subtitle_track` captions (add_subtitle). A `cut` needs no operation. Burn-in
captions and titles map to `place_title`. Requests are also run through `resolve_executor.validation.validate` with an
offline policy (media roots = the directories of the used sources; output roots = the allowlist; no file checks).

## Planner
`plan_from_brief` takes the brief's recommended arc (or `arc_id=`), places each section's ranges in order on V1/A1 as
`dialogue` (speaker `creator` only when every moment of the section is attributed to the creator, else `unknown`,
which validation then reports), adds one marker per section and standard QC expectations, with `captions=True` and a
transcript adds verbatim burn-in captions on V2. Seconds become frames exactly (start floors, end ceils; widened
ranges are listed in `notes`; ends past the file are clamped and noted). No pacing, B-roll, music, transitions or
styling: a CANDIDATE for human/QC review only.

## Known limits
* Offline and unit-tested only; never run against Resolve or real footage. Not VERIFIED.
* Paths are compared lexically (case-insensitive, `/` = `\`, `..` resolved). Symlinks, junctions, 8.3 short names and
  UNC/device paths cannot be resolved without the filesystem: give the allowlist as real long-form paths. Device and
  `\\?\` paths are refused as invalid.
* Caption quote check is a normalised substring test (NFC, case, whitespace) against the transcript entries that
  overlap the referenced range; punctuation differences count as a mismatch. Whether the transcript itself is right is
  not checked here.
* `speaker`/`origin` are declared by the inventory and the brief; this package cannot tell who actually speaks.
* The `place_title` Text+ route is limited to about 5 s per title by the executor; durations are not checked here.
* Variable frame rate media is not detected (inventory must say constant rate); drop-frame timecode is refused.
* Overlap is checked per track only; no cross-track rules (e.g. audio ducking) exist. No playback speed, effects,
  asset rights/provenance or platform targets yet.
* The planner reads the brief's seconds as decimal text of the float; it does not re-verify the brief against the
  transcript.
* `ExportTarget.format/codec` are limited to what the executor accepts (mp4/mov, H264/H265).

Tests: `python -B -I -m unittest discover -s orchestrator/tests -p test_edit_plan.py`.
