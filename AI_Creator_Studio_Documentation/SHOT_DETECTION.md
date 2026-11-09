# Shot Detection

**Status:** IMPLEMENTED (core, orchestration and CLI) and **VERIFIED on synthetic clips only** (generated 64x48, 25 fps test clips; 141 tests in `orchestrator/tests/test_shots.py`). **NOT TESTED on real footage.** Real-footage accuracy is unknown; do not rely on the results for editing decisions until a real sample has been run with the user's permission and reviewed.
**Code:** `orchestrator/creator_state/shot_metrics.py` (FFmpeg arguments, parsers, timing), `shot_classify.py` (rules), `shots.py` (orchestration), `cli.py` (commands). **Tests:** `orchestrator/tests/test_shots.py`.

## What it does

For one video it finds where the picture changes and says, per event, what it most likely is. It does **not** understand content: a "shot" here means a run of frames between detected changes, not a change of topic or scene in the story sense. Scene-level understanding is a separate, later subsystem and is deliberately not part of this one.

Relationship to `orchestrator/media_analysis/scene_detection.py` (an FFmpeg scene-score boundary detector in the Phase 2 package): both exist and neither feeds the other. How they should be reconciled is **OPEN** (referred to as Q35 in `ROADMAP.md` and `INGESTION.md`).

## Candidates, shots and transitions

| Term | Meaning |
|---|---|
| **Candidate** | Any event that might be a boundary: a run of adjacent frames whose scdet score reaches the floor (default 5, deliberately low so short events are not missed), or a steady brightness ramp. Every candidate is classified and **kept**, including rejected and uncertain ones. |
| **Shot** | A run of consecutive frames between boundaries. Frame numbers are exact; `end_frame` is inclusive and `end_time` is exclusive. No minimum shot length is applied. |
| **Transition** | A fade or dissolve: a frame range that belongs to **neither** neighbouring shot. |

Classification (ordered rules, each result lists its reasons): `hard_cut` (a cut, shots touch), `fade_in` / `fade_out` / `dissolve` (transitions), `flash`, `camera_motion` and `lighting_change` (looked like a boundary but are not; kept in `rejected_candidates`), and `uncertain` (kept in `uncertain_candidates`, needs review, never treated as a boundary). A candidate that conflicts with another boundary is demoted to `uncertain` with the reason, never dropped.

**Confidence is a heuristic score from fixed rules, not a calibrated probability.** It has not been validated on real footage. A shot with no detected boundary has `confidence: null`.

## How a run works

1. **Preflight** (below), then a deterministic run id `r_<settings hash>_<source identity hash>`.
2. **Pass A** `scdet` + `signalstats` per-frame metadata. **Pass B** `blackdetect`. **Pass C** tiny gray thumbnails for before/after similarity. Then a few single-frame JPEG evidence extractions (a failed image is a warning, not a failed run). Three full decodes of the first video stream, one process at a time, at below-normal priority, with bounded FFmpeg threads and a time limit per pass. No shell is used. By default the analysis runs at 360 px height (changes scores; `--full-size` analyses at source size).
3. **Derive**: candidates, evidence, classification, shots and transitions.
4. **Verify and publish**: written into a hidden `.<run_id>.part` folder, checked (shots and transitions cover every frame exactly once, row counts match, hashes match `run.json`), then published with one directory rename. `latest.json` is updated only afterwards.

## Commands

Run from `CreatorStudio\orchestrator` as `python -m creator_state <command>`:

| Command | Purpose | Exit code |
|---|---|---|
| `shots-plan <slug> [--id M]... [--json] [settings]` | Dry run per video: action (`DETECT` / `EXISTS` / `BLOCKED` with the reason), run id, expected frames, output folder. Starts no process and writes nothing (tools are located, not run). | 0 ok; 1 if any video is blocked; 2 error or no videos |
| `shots-detect <slug> --yes [--id M]... [--ffmpeg P] [--ffprobe P] [settings]` | Run detection. Without `--yes` it only prints the plan. Output verification is re-run by the command; a run is reported `COMPLETED` only if it passes. Statuses: COMPLETED, SKIPPED (a run already exists; never replaced), BLOCKED, FAILED. | 0 all completed or skipped; 1 any blocked or failed; 2 invalid project, media id or settings; 130 interrupted |
| `shots-status <slug> [--id M] [--json]` | Lists runs as COMPLETE (with counts and `(latest)`), INCOMPLETE or FAILED. Only COMPLETE runs are results. Read-only. | 0 (2 for an invalid project or media id) |
| `shots-check <slug> [--id M] [--json]` | Re-verifies complete runs (structure, file hashes) and each source against the inventory. Warnings: incomplete runs. Info: failed-run diagnostics. | 1 if any critical finding, else 0 |

Settings flags (only those `shots.py` supports): `--floor` (candidate score floor, default 5), `--max-gap` (default 1), `--threads` (1-8, default 2), `--evidence` (events that get JPEG frames, default 40, 0 = none), `--timeout` (seconds per pass, default 3600), and either `--scale-height N` (default 360) or `--full-size`. Further settings, including the classifier thresholds, go in `Projects/<slug>/shots_settings.json`; unknown or invalid settings are refused.

```powershell
cd C:\Users\Korisnik\CreatorStudio\orchestrator
python -m creator_state shots-plan <slug>                    # preview (dry)
python -m creator_state shots-detect <slug> --yes --id <media-id>
python -m creator_state shots-status <slug>
python -m creator_state shots-check <slug>
```
Do not run `shots-detect` on the user's real footage without their permission. If the `understand` stage is in progress (`start-stage <slug> understand`), each run is journaled as an operation; otherwise nothing is written to project state and the command says so.

## Output layout

```text
Projects/<slug>/analysis/shots/<media_id>/
  latest.json                     pointer to the newest completed run
  <run_id>/                       a published, verified run (never modified later)
    shots.json                    shots, transitions, cuts, rejected_candidates, uncertain_candidates, summary, settings, warnings
    candidates.csv                one row per candidate: frames, times, category, confidence, rule, similarities, reasons
    frame_scores.csv              per frame: scdet mafd/score, mean luma, luma change
    run.json                      settings, tool versions, source identity before/after, every process (argv, exit code, seconds), warnings, SHA-256 of every file
    report.md                     human summary
    raw/                          raw FFmpeg output (metadata, stderr logs, blackdetect log, thumbnail data)
    frames/                       before/mid/after JPEGs for boundaries and uncertain events (capped)
  .<run_id>.part/                 work in progress or the leftover of a crash: NEVER a result
  _failed/<run_id>_<n>/           diagnostics of a failed run (with failure.json): NEVER a result
```
In `shots.json` each shot has `shot_id`, `start_frame`, `end_frame`, `start_time`, `end_time`, `duration`, `detection_method`, `confidence` (heuristic), `start_boundary`, `end_boundary` and `evidence` (JPEG paths). Times are exact frame boundaries computed from the rational frame rate.

## Preflight: what is refused (reported as BLOCKED with the reason)

- Not a video, not `ok` in the inventory, or no successful ffprobe record.
- **Variable frame rate** (`VARIABLE_FRAME_RATE` warning) or any frame rate that is not a verified constant rate. Frame-exact analysis is not attempted.
- Source size or modified time differs from the ingestion manifest, or the file is missing or unreadable.
- A manifest that belongs to another project; an unknown media id.
- Missing ffmpeg or ffprobe; a project path containing characters that cannot be used inside an FFmpeg filter.
- The frame rate re-probed now differs from the manifest.

**Only the first video stream is analysed**; a warning is recorded when there are several. Audio is not read.

## Safety, failure and recovery

- Originals are only read (opened by FFmpeg/ffprobe as input). Size and modified time are re-checked after every pass; a change during processing fails the run.
- A run folder that already exists is never touched or replaced; a failed or later run cannot disturb a completed one or `latest.json`.
- A failed, interrupted (including Ctrl+C), timed-out or invalid run never produces a result folder. A clean failure is kept under `_failed/` as diagnostics. A crash leaves only a `.part` folder; `shots-status` shows it as INCOMPLETE and the next attempt discards it.
- Malformed, empty, truncated or inconsistent FFmpeg output fails the run; zero events is a valid result only when the data is complete.
- Re-running after a failure is safe and idempotent: the same source and settings map to the same run id.

## Known limitations

- **Real footage: not validated.** Thresholds are untuned heuristics chosen from tiny synthetic clips. Expect false positives and misses on handheld, noisy, compressed or screen-recorded material.
- Cost: three decodes plus evidence frames; long 1080p footage will be slow on this machine (performance not measured).
- Shots of 1-2 frames cannot be told from a flash with these signals and stay `uncertain`. Flashes longer than about 2 frames also stay `uncertain`. Shots of 3 or more frames are detected on synthetic clips.
- Black lead-in or tail frames can appear as tiny "shots". A fade that runs to the very last frame (no black after it) is kept as `uncertain`.
- Similarity uses 32x18 gray thumbnails: it cannot show a lighting change on perfectly flat frames, and a colour-only change between flat frames may score below the threshold.
- Only constant-frame-rate sources, only the first video stream, no rotation handling beyond a warning.
- Faces, subjects, topics and "scenes" in the story sense are not detected.
