# Hybrid Media Ingestion

**Status:** IMPLEMENTED and unit-tested (2026-11-08) with generated dummy files, an injected fake ffprobe, and real ffmpeg/ffprobe on **synthetic** clips. **NOT TESTED on real footage.** Do not treat it as production-ready until a real sample has been ingested with the user's permission.
**Code:** `orchestrator/creator_state/{media,ingest,proxy,managed}.py` · **Tests:** `orchestrator/tests/test_ingest.py`

> **Scope of this file.** Sections up to "Not implemented / not tested" describe **ingestion** (inventory, proxies, managed copies). The section **"Media analysis (Phase 2)"** at the end describes the separate `orchestrator/media_analysis/` package that builds on the ingest manifest (analysis copies, transcripts and their review, audio measurements, shot boundaries, the moment index, the Creator DNA handoff and its connection to the `understand` stage).

## Design (DECIDED: hybrid ingestion)

- Originals are **never modified, renamed, moved or deleted**, and are **referenced in place** by absolute path (inside or outside CreatorStudio). Large originals are not duplicated by default.
- Everything generated (inventory, report, probe cache, proxies, managed copies) lives in `Projects/<slug>/`, never beside the originals.
- Working copies/proxies are separate, explicit, verified operations. Ingestion never creates them.
- Claude Code CLI is the interface; there is no dashboard.

## Where footage goes

```text
CreatorStudio\Incoming\<project-name>\      one folder per video project
    camera\  phone\  screen\  audio\  images\  notes\      (optional subfolders; other names = "unsorted")
```
Any other folder, on any drive, also works: `ingest "D:\Footage\trip"`. The folder name becomes the project slug unless `--project` is given. `Incoming\README.md` explains this for the user. Folders starting with `_` or `.` are ignored by `incoming-list`.

Supported: video (`.mov .mp4 .m4v .mkv .avi .mts .m2ts .mxf .webm .wmv .mpg .mpeg .3gp .ts .flv`), audio (`.wav .mp3 .aac .m4a .flac .ogg .opus .aif .aiff .wma .bwf`), images (`.jpg .jpeg .png .gif .webp .tif .tiff .bmp .heic .heif .avif`), notes (`.txt .md .rtf`), subtitles (`.srt .vtt .ass .ssa`). Unsupported extensions are **listed in the report and never touched**. Junk (hidden files, `Thumbs.db`, `~$*`), partial downloads (`.part .tmp .crdownload`) and camera sidecars (`.thm .lrv .xmp ...`) are listed as *ignored*.

## Pipeline (stage `ingest` of the persistent state)

1. **scan** the folder (read-only; unreadable folders and broken links are reported, not fatal).
2. **probe** each file: filesystem facts, SHA-256 per policy, ffprobe. Each result is cached atomically in `analysis/probe_cache/<id>.json`, so an interrupted ingest resumes without redoing finished files.
3. **write** `source_manifest.json` (the media inventory, atomic) and `analysis/ingest_report.md`.
4. **register** originals in project state by reference (one atomic update; already-registered paths are left exactly as first recorded, so later changes to an original remain detectable).
5. **complete** the stage through `complete_stage` (outputs verified, checkpoint saved). Each step is journaled as an operation (`scan@N`, `probe@N`, …); operations left open by a crash are marked failed on resume.

A project that already finished ingest refuses a second ingest unless `--reopen` is given (which also invalidates later completed stages). Item ids are derived from the normalized absolute path, so re-ingesting never duplicates an entry; identical *content* at different paths is kept and flagged `DUPLICATE_CONTENT`. Files that vanish later stay in the inventory as `missing` rather than being dropped.

## Inventory record (per file)

`id`, `name`, absolute `path` (and `real_path` for symlinks), `relative_to_source`, `source_root`, `source_category` (camera/phone/screen/audio/images/notes/unsorted), `extension`, `media_type` + `media_type_source` (`ffprobe` or `extension`), `status` (`ok`/`missing`/`inaccessible`), `file` {size, mtime, mtime_ns, created (only when the OS provides it), sha256, hash_status}, `probe` {status, tool, error}, `container` {format, duration, bit rate, container creation_time}, `video[]` {codec, profile, width, height, display size after rotation, rotation, pix_fmt, field order, frame count, `frame_rate`}, `audio[]` {codec, channels, layout, sample rate}, `image`, `warnings[]`, `first_seen`, `duplicate_of`.

**Frame rate honesty:** `frame_rate` holds `r_frame_rate`, `avg_frame_rate`, `fps`, `fps_rational`, `exact` and `kind`. `kind: constant, exact: true` (e.g. `30000/1001` = 29.97) only when the declared and average rates agree; `variable_suspected` (flagged `VARIABLE_FRAME_RATE`, `exact: false`, the average is reported) when they differ; otherwise `unknown` with an estimate or nothing.

**Checksums:** `--hash auto` (default) hashes files up to 4 GiB; larger files are flagged `HASH_SKIPPED_LARGE`; `always` and `never` are available.

**Warning codes:** `FFPROBE_UNAVAILABLE`, `PROBE_FAILED`, `FILE_INACCESSIBLE`, `SOURCE_MISSING`, `SOURCE_CHANGED_SINCE_LAST_INGEST`, `DUPLICATE_CONTENT`, `VARIABLE_FRAME_RATE`, `NO_AUDIO_STREAM`, `NO_MEDIA_STREAMS`, `ZERO_DURATION`, `EXTENSION_MISMATCH`, `HASH_SKIPPED_LARGE`, `HASH_FAILED`, `SYMLINK`.

## When ffprobe is unavailable

ffprobe is detected (explicit `--ffprobe`, `CREATOR_FFPROBE`, then `PATH`) and its `-version` output checked; it is never assumed. Without it, ingestion **still completes** and records only what the filesystem provides (path, size, times, checksum); media type falls back to the extension and is marked so; duration, dimensions, frame rate, codecs and audio streams stay `null`, **not guessed**. Every media file gets `FFPROBE_UNAVAILABLE`, the report and `HANDOFF.md` say so, a follow-up task is added, and proxies are blocked for unprobed media. After installing FFmpeg, `ingest <folder> --reopen` upgrades the records (stale "unavailable" cache entries are not reused). Image dimensions are not read without ffprobe.

## Proxies and managed copies (explicit, not part of ingestion)

- `proxy-plan <slug>` previews; `proxy-make <slug> --yes [--id M] [--max-height N --crf N --preset P]` generates. Settings: defaults in `proxy.DEFAULT_SETTINGS` (H.264/AAC mp4, 540 px max height, CRF 23), overridable per project in `proxy_settings.json` or on the command line; unknown or invalid settings are rejected.
- Output is `Projects/<slug>/proxies/<media-id>_<settings-hash>.mp4` (deterministic; different settings → different file). ffmpeg runs with `-n` (never overwrite) into a `.part` file; it is renamed into place only after verification (readable by ffprobe, has video, height within limit, audio present if the source has audio, duration within max(0.5 s, 2 %)). Failures leave no file. Relationships and checksums go to `analysis/proxy_manifest.json`. A source that changed since ingest is blocked.
- `managed-copy <slug> <media-id> --yes` copies **one** original to `assets/managed/` through a `.part` file, verifies size and SHA-256 (and that the original still matches its ingest checksum), refuses to overwrite and checks free space. The original stays the source of truth. Recorded in `analysis/managed_copies.json`.
- Proxy settings are validated only on tiny synthetic clips. Proxy and managed-copy operations are not stage-tracked and not yet part of the state journal; they log to the activity log and their own manifests.

## Commands

Run from `CreatorStudio\orchestrator` as `python -m creator_state <command>`:

| Command | Purpose |
|---|---|
| `incoming-list` | Folders under `Incoming/` with counts and ingest status |
| `incoming-inspect <name-or-path> [--json]` | Dry run: what would be ingested; changes nothing |
| `ingest <name-or-path> [--project S] [--title T] [--hash auto\|always\|never] [--ffprobe PATH] [--reopen]` | Create/reuse a project and catalogue the folder |
| `inventory <slug> [--type video\|audio\|image\|notes\|subtitle] [--json]` | Browse the inventory |
| `verify-sources <slug> [--deep]` | Are originals still present and unchanged? Exit 1 if not (read-only) |
| `ingest-status <slug>` | Stage state, inventory summary, ffprobe, quick source check |
| `proxy-plan <slug>` / `proxy-make <slug> --yes` | Preview / generate verified proxies |
| `managed-copy <slug> <media-id> --yes` | One verified managed copy |

## Safety summary

Originals are opened `rb` only (and read by ffprobe/ffmpeg as input). Tests snapshot every file hash, size, modification time and the folder listing before and after ingest, proxy generation and managed copy, and assert identity. Nothing deletes or overwrites anything it did not create itself (only its own `.part` leftovers and probe-cache files are replaced).

## Not implemented / not tested

- Real footage; very large files and long folders (performance unknown); network drives and removable-media disconnects.
- Real transcription, real speech-activity detection and any vision model: no engine is installed or chosen (see "Media analysis (Phase 2)"). The analysis interfaces, an energy-based audio analyser and an FFmpeg shot-boundary detector exist and are tested on synthetic media only.
- Image dimensions or any metadata without ffprobe; HEIC/RAW support depends on the installed FFmpeg.
- Cross-process concurrency for simultaneous ingests into one project (the state lock covers state writes, not the whole pipeline).
- Content-based relocation of moved originals (`verify-sources` reports missing files; it does not search for them).
- Proxy tuning, audio-only proxies, and Resolve-side linking of proxies.
- Symlink creation was not exercised in tests (Windows needs privileges); the code path records a `SYMLINK` warning.

---

# Media analysis (Phase 2)

**Status:** IMPLEMENTED at package level, unit-tested on **synthetic** data only (`orchestrator/media_analysis/`; 67 tests in `tests/test_media_analysis.py`, 38 pipeline/handoff tests in `tests/test_integration_phase2_phase3.py`). Real FFmpeg/ffprobe were exercised only on a generated 4-second clip. **NOT TESTED on real footage. No transcription engine exists.** The `media_analysis` package has no CLI commands; everything in it is a Python API (the separate shot-detection commands are in `SHOT_DETECTION.md`). Code overview: `inventory.py`, `proxies.py`, `transcription.py`, `review.py`, `audio_analysis.py`, `scene_detection.py`, `moment_index.py`, `adapters.py`, `pipeline.py`; a short contract note is in `orchestrator/media_analysis/INTEGRATION_CREATOR_DNA.md`.

Four kinds of record are kept apart and never merged: **measurement** (deterministic numbers), **transcription** (an engine’s recognised wording, not ground truth), **detection** (a detector’s output) and **interpretation** (a subjective label by an AI or a human; never produced automatically).

## Reuse of the existing manifest and media identity

- `source_manifest.json` (written by ingest) remains the **single source of truth** for originals. `media_analysis` does not create a competing project manifest.
- Media ids are `creator_state.media.item_id` (derived from the normalised absolute path, `m_<10 hex>`): the same file has the same id in both packages, and the id stays the same if the file’s content changes.
- `MediaRef` pins an original by path, size, mtime, sha256 and duration. `media_analysis.inventory` can also build records of the *same shape* for a folder outside a project (used by tests); it imports `creator_state`’s probe/parse/checksum helpers read-only instead of duplicating them. Extra statuses there: `invalid` (ffprobe failed) and `unsupported`.
- Before an analysis step relies on an original, the pipeline calls `ingest.verify_sources`. Analysis copies, audio analysis, scene detection and the copy used for transcription use the **deep** (checksum) check; the moment index and DNA handoff use the quick check (size and modified time) by default. Any original that is not `ok` aborts the step with a clear error.

## Analysis copies: lifecycle and provenance (`proxies.py`)

- Only **audio** analysis copies are generated (`audio_wav`, default 16 kHz mono PCM). Video proxies stay with `creator_state.proxy`; `register_existing` can *record* such a file’s relationship to its original but never generates one.
- Nothing is created automatically: a copy exists only after an explicit `ensure()` call (the pipeline calls it for audio analysis and transcription).
- Creation: `ffmpeg -n` into a `.part` file in the registry folder (never beside the original, never over an existing file), then checks — valid WAV, requested rate/channels, duration within max(0.5 s, 2 %) of the source, original’s size/mtime unchanged — and only then a rename. Failures leave no file and no record. An existing but unregistered output file is refused, not overwritten. If `ffmpeg` is missing a `CapabilityError` is raised and nothing is created.
- Registry `analysis_copies.json` records per copy: `copy_id` (`<media_id>_<kind>_<settings hash>_<source fingerprint>`), the source identity (path, size, mtime, sha256, duration), settings, tool and argument template, output path/size/sha256, creation time.
- A valid copy is reused. Audit states: `valid`, `source_missing`, `stale_source` (source size/mtime, or with `deep` sha256, differs), `output_missing`, `output_changed` (deep). A stale copy is **never deleted or overwritten**; the changed source simply yields a new `copy_id` and file.
- Times in records are seconds on the **original** timeline; records say which copy was analysed (`analysis_copy_id`). Only the copy’s total duration is verified against the original; whether container start-time offsets could shift a copy’s timeline on real media was **not tested**.

## Transcripts and review (`transcription.py`, `review.py`)

- `TranscriptSegment`: media id, start/end (seconds), recognised `text`, optional `language` (BCP-47-style, as reported by the engine), `variety` (e.g. `kajkavian`, only if the engine reports one), `language_confidence`, `confidence`, `speaker_id`, `words` and `language_spans` (character ranges for code-switching). Text is stored exactly as returned (no normalisation or correction; diacritics, slang, repetitions and false starts are kept, including decomposed combining characters). `words` is `None` unless the engine supplied word timings; nothing derives words from phrase timings.
- A `Transcript` is engine output: **always `unverified`**, never edited, saved write-once as `<media>.<engine>-<settings hash>.transcript.json`. It records the engine/provenance, the requested language, source duration and the audio copy that was transcribed.
- Providers implement `TranscriptionProvider` (`capabilities()`, `transcribe()`). The run rejects results from providers that return word timings, speaker ids or language varieties they did not declare. Only `UnavailableProvider` (raises `CapabilityError`) and `FixtureProvider` (synthetic test data, marked `synthetic_fixture`) exist.
- **Review** (`review.py`) is a separate write-once record: a named reviewer (`human:<name>`), the exact transcript (id + content hash), and per segment `confirmed` or `corrected(text)`. `apply_review()` returns a view holding both texts; unreviewed segments stay unreviewed. Reviews chain (`previous_review_id`, `extend_review`); a forked chain, a review applied to changed transcript content, a correction equal to the original, and a tampered review file are all rejected. Nothing promotes a transcript to “reviewed” except such a review.

## Audio measurements (`audio_analysis.py`)

PCM WAV only (8/16/32-bit, any channel count, pure Python). Output per recording: per-window RMS/peak (dBFS); **regions** above/below a level threshold (this is *energy*, not speech detection); **quiet gaps** (position `between`/`leading`/`trailing`/`whole`, exact duration, a `long` flag that only reflects length); **energy rises** over the recent baseline. All parameters are stored with the result; defaults: window 0.1 s, threshold `auto`, min gap 0.3 s, min active blip 0.15 s, long gap 2.0 s, baseline 3.0 s, rise 6 dB, refractory 1.0 s. `auto` places the threshold at the 10th percentile + 40 % of the way to the 95th percentile (or just below everything if the spread is under 10 dB, or at −70 dBFS for near-silent files) — a heuristic, not validated on real recordings. Speech regions exist only if a `SpeechActivityProvider` (voice-activity engine) is supplied; none is. No pause is called awkward, comedic or dead air. The metric names and definitions handed to Creator DNA are in `CREATOR_DNA.md` §7.3.

## Shot boundaries (`scene_detection.py`) and limitations

`ShotBoundary` (boundary time, method, raw `score`, `confidence` left `None` unless an engine supplies one, `FrameRef` timestamps) and `VisualEvent` are detections. `FfmpegSceneDetector` uses FFmpeg’s `select='gt(scene,T)'` (default T = 0.4) and reads the video without writing. Limitations: tested only on a synthetic hard cut (red→blue); the raw score is not a probability; a boundary is a *visual event*, not a change of subject or topic; flashes, lighting changes, camera motion and screen-recording window switches may trigger it; behaviour on fades/dissolves, variable-frame-rate media and real footage is unknown; it reports times, not frame numbers; it detects no faces, subjects or gestures. Without FFmpeg it raises `CapabilityError` and invents nothing. **Note:** a separate shot-detection work stream exists in `orchestrator/creator_state/shot_metrics.py`, `shot_classify.py` and `tests/test_shots.py` (its own tests pass); it is documented in `SHOT_DETECTION.md` (with CLI commands `shots-plan`, `shots-detect`, `shots-status`, `shots-check`; verified on synthetic clips only), and how it relates to `FfmpegSceneDetector` is undecided (Q35).

## Moment index and interpretation model (`moment_index.py`)

- **Evidence records** (transcript segment, audio gap, energy event, speech region, shot boundary, visual event) each keep their engine/provenance and payload.
- A **moment** is a time range on one recording pointing at ≥ 1 evidence records. Its type is *factual only* (`spoken_statement`, `pause`, `energy_rise`, `scene_change`, `visual_event`, `speech_region`, `composite`, `candidate`); creating a moment typed “joke”, “reaction” or similar is rejected. `composite` means “these records co-occur within a time window” (`correlate()`), nothing more. Moment ids derive from media, type, range and evidence ids, so an identical moment is never added twice.
- An **interpretation** (free-vocabulary label, author `ai:<model>` or `human:<name>`, rationale, optional confidence) is stored beside a moment, never as its type, and requires cited evidence. Nothing in the package creates one automatically. Interpretations are not handed to Creator DNA by the adapters.
- Also: queries by media/type/time/evidence kind/label, `repeated_statements()` (identical text at different times, e.g. retakes), save/load with tamper detection.

## Output storage

Inside the project folder (so `complete_stage` can verify and hash outputs):

```text
Projects/<slug>/analysis/media/
  copies/analysis_copies.json        analysis-copy registry
  copies/copies/<copy_id>.wav        analysis copies
  audio/<media_id>.audio-<hash>.json
  scenes/<media_id>.shots-<hash>.json
  transcripts/<media_id>.<engine>-<hash>.transcript.json      ASR output, never edited
  transcripts/<transcript_id>.review-<review_id>.json         human reviews (separate files)
  moments.json
  attribution/events.jsonl           speaker-attribution evidence, eligibility decisions, provider runs (append-only)
  eligibility_report.json            every eligibility decision with its reason (ineligible ones included)
  dna_handoff.json                   what was/was not handed to Creator DNA, with record ids and reasons
```

## Pipeline connection (`pipeline.py`)

It uses only `creator_state`’s public API (no state-format change, no edit to `creator_state`) and runs inside the existing **`understand`** stage:

| Step | Operation id (journaled) | Notes |
|---|---|---|
| start | — | `begin()` starts/resumes `understand`; every step requires it to be `in_progress` |
| originals verified | — | `ingest.verify_sources` first; a changed/missing original aborts the step |
| analysis copy | `copy:<copy_id>` | reused if valid |
| audio measurements | `audio:<media_id>:<hash>` | |
| shot boundaries | `scenes:<media_id>:<detector>` | needs FFmpeg or an injected detector |
| transcription | `transcribe:<media_id>:<provider>` | with no engine it fails the operation with `CapabilityError`, visible in project state |
| review stored | `review:<review_id>` | next to, never over, the ASR transcript |
| moment index | `moments:…` | |
| eligibility decisions | `eligibility:…` | speaker attribution -> eligibility for Creator DNA; with no attribution evidence everything is ineligible (`speaker_unknown`) |
| DNA handoff manifest | `dna_handoff:…` | only records an eligibility decision found eligible as CREATOR evidence |

Operations are non-destructive and idempotent: an already-completed operation is re-evaluated (reusing existing outputs) and not re-journaled; a failed one is journaled as failed.

**Speaker attribution and eligibility (Phase 2.3).** Before anything reaches Creator DNA, each candidate (transcript segment, pause, energy rise, visual event) gets an explicit **eligibility decision** from `orchestrator/speaker_attribution/` (see `CREATOR_DNA.md` §8a). Who speaks / who is visible comes only from a human confirmation or correction, explicit project metadata, or future supporting providers; nobody is identified by software, a diarisation cluster is a grouping and not an identity, and unknown stays unknown. Decisions are persisted with reasons in `analysis/media/attribution/` (inspectable and re-computable) and summarised in `eligibility_report.json`. Pipeline calls: `attribution_store(project)` (record human decisions with `human_confirm_span` / `human_correct_span` / `project_metadata` / `map_cluster`), `evaluate_eligibility(project)`, then `prepare_dna_handoff`. Not implemented: any attribution provider, any CLI, any creator-facing review tool, any run on real footage.

**DNA handoff.** `prepare_dna_handoff` builds the Creator DNA inputs allowed by an explicit `DnaEligibility` policy **and found eligible by the attribution step**, stamps them with an `EvidenceAttribution`, and writes `dna_handoff.json`. Defaults are conservative: **no unreviewed ASR** (opt-in `include_unverified_transcripts`, optionally filtered by `min_asr_confidence`), audio gaps and energy rises included, level windows excluded, visual detections included, source paths included. Everything excluded is listed with its reason (e.g. `attribution:ineligible:other_speaker`). Shot boundaries are never handed over (they are not person behaviour). It learns nothing. **DNA ingestion stays an explicit, separate call** (`ingest_handoff_into_dna(store, handoff, analyzers)`): which analyzers to run, which eligibility policy to use and whether the transcripts have been reviewed are decisions, not routine steps; a measurement is not a trait, a scene boundary is not a topic change, and a one-off remains `one_off`. Re-running is idempotent (identical evidence is skipped).

**`understand` is not completed automatically.** The stage also covers transcription, the content map and the evidence-linked moment index of `WORKFLOW.md` §2; with no transcription engine none of that can be complete, and `complete_stage` only needs *some* verified output, so an automatic completion would mark an unfinished stage done (a completed stage cannot be restarted without `reopen_stage`). `pipeline.outputs()` lists the files for `complete_stage`; when the stage may legitimately be completed is an open decision (Q33).

## Not implemented / not tested (media analysis)

Real transcription, speaker diarisation and any attribution provider, speech-activity detection, face/subject/gesture detection, multi-camera sync, any CLI command, automatic DNA learning, a creator-facing transcript review tool, and any run on real footage.
