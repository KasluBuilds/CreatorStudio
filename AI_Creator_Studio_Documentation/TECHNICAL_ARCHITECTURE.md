# Technical Architecture

**Status:** Target architecture is PROPOSED; local environment and Resolve MCP installation are VERIFIED as described.

## 1. Design goals

- Reliable autonomous workflow.
- Deterministic editing operations wherever possible.
- Context-aware creative reasoning.
- Low unnecessary token/API cost.
- Recoverability and non-destructive project handling.
- Clear observability: logs, status, inputs, outputs and errors.
- Extensibility for new agents and editing capabilities.

## 2. Recommended components

### Orchestrator
Python service coordinating state, agents, tools, retries and job lifecycle.

### LLM layer
Use Claude for high-context creative reasoning, planning, critique and complex decisions. Use lower-cost providers or deterministic code for simpler classification, transcription post-processing and routine tasks when quality is adequate. OmniRoute is available locally for provider routing and fallback; exact model routing should be benchmarked rather than assumed.

### Deterministic tools
Python and FFmpeg can handle media probing, file operations, waveform/silence analysis, frame extraction, image resizing, validation, metadata, checksums and repeatable transformations. Resolve remains the editing/rendering environment where its API is needed.

### Resolve integration
Two paths, for different callers (details: `RESOLVE_INTEGRATION.md` section 10):
- Interactive: Claude Code MCP client → local MCP server → in-app Resolve bridge → Resolve scripting API.
- Pipeline (implemented 2026-11-18, `T-RSV-002`; offline takeover review 2026-11-20): edit plan →
  `orchestrator/resolve_executor` (typed operations → capability gate (unknown, NOT_TESTED and UNSUPPORTED requests
  are refused first) → pure validation → idempotency journal (explicit key or `op:<op_id>`) → identity/precondition
  checks → fsynced journal `begin` → mutation → read-after-write verification → journal `finish`) → replaceable
  adapter (live: in-app bridge; offline: in-memory fake) → Resolve. Renders are verified from the file with ffprobe.
  The LLM never issues raw API calls on this path. Status: implemented and unit-tested offline; core operations were
  exercised live on synthetic media (`E-RSV-LIVE-0001`, previous session). M-RSV-02 is not VERIFIED (no real footage,
  no human review).

Responsibilities stay separate: Creative Director (story), Editor Agent (editing decisions and the structured plan),
validator (deterministic correctness and safety, no Resolve calls), Resolve executor (timeline operations and
readback), QC (inspection of the result). The executor depends on no other orchestrator package and on no LLM
provider.

### Storage
- Project manifests and state.
- Edit-plan JSON.
- Transcripts and time-aligned segments.
- Creator DNA profile and evidence.
- Editing profiles.
- Research briefs and sources.
- Asset manifests.
- QC reports and render metadata.
- Logs and backups.

## 3. Edit-plan contract

The LLM should not directly issue a long uncontrolled series of low-level editing commands. It should create a structured plan that deterministic validation can inspect.

Suggested top-level shape:
```json
{
  "schema_version": "0.1",
  "project_id": "example",
  "timeline": {
    "name": "Main Edit",
    "frame_rate": 25,
    "resolution": [1920, 1080]
  },
  "segments": [],
  "tracks": [],
  "assets": [],
  "operations": [],
  "platform_exports": [],
  "validation": {}
}
```

This is illustrative only; it is not yet a finalized schema. Use integer frame positions or rational time values, not ambiguous decimal seconds alone. Keep source in/out and timeline positions explicit.

## 4. Tool interface strategy

Prefer high-level, validated operations such as:
- `inspect_project()`
- `ingest_media()`
- `create_timeline_from_plan()`
- `append_source_segments()`
- `apply_clip_transform()`
- `add_caption_track()`
- `place_asset()`
- `render_review_copy()`
- `inspect_render()`
- `export_platform_variants()`

These are proposed wrapper functions, not confirmed existing MCP tools. Underlying MCP tools should be used only after their schema and behavior are inspected.

Implemented so far (`resolve_executor`, live-exercised on Resolve Free 21.0.4.5 with synthetic media): inspect
project/timeline, create project/timeline, import media, add track, append source segments (positioned, half-open
source ranges, mixed rates), transforms and crop, markers, Text+ titles, remove items, save/checkpoint, render with
monitoring and export verification. Explicitly refused: audio levels/fades/EQ, transitions, subtitle tracks,
insert/overwrite edits (no safe API on 21.0). Capability statuses (VERIFIED / IMPLEMENTED / NOT_TESTED / UNSUPPORTED)
live in `resolve_executor/capabilities.py` and are mirrored in its README; IMPLEMENTED-only items include Resolve-reported
render failures and interrupted-mutation handling (offline fault injection, never provoked live). `create_timeline_from_plan()` waits for the edit-plan schema (`M-EDT-01`).

Batch operations where safe. Avoid hundreds of tiny calls when a single validated high-level operation can do the job. Serialize Resolve mutations because the bridge is single-threaded.

## 5. Reliability and safety

- Keep originals immutable.
- Use unique project/timeline IDs.
- Save before risky operations.
- Create checkpoints after logical stages.
- Validate paths against allowlists.
- Never delete or overwrite final deliverables without a backup.
- Record every Resolve mutation and returned result.
- Verify outcomes by reading state back.
- Use bounded retries and idempotency keys where practical.
- Avoid concurrent writes to one Resolve session.
- Keep bridge on loopback only.

## 6. Token and cost efficiency

- Use deterministic tools for exact operations and repetitive media analysis.
- Send the LLM concise evidence (transcript spans, representative frames, metadata) rather than entire raw media when possible.
- Cache transcripts, embeddings, shot boundaries and research.
- Use smaller/cheaper models for low-risk classification only after benchmarking.
- Reserve Claude Pro/high-reasoning usage for story planning, nuanced style decisions and QC reasoning.
- Maintain task budgets and stop conditions.

## 7. Suggested project data layout

```text
Projects/
  <project-slug>/
    source_manifest.json
    transcripts/
    analysis/
    research/
    edit_plans/
    assets/
    resolve/
    review_renders/
    final_exports/
    qc/
    logs/
    backups/
```

Ingestion (see `INGESTION.md`) uses `source_manifest.json` (the media inventory named above) at the project root, plus `analysis/ingest_report.md`, `analysis/probe_cache/`, `analysis/proxy_manifest.json`, `analysis/managed_copies.json`, `proxies/` and `assets/managed/`. Original footage is **not** stored in the project: it is referenced in place by absolute path, usually from `Incoming/<project>/` at the studio root or an external folder.

Media analysis (see `INGESTION.md` “Media analysis (Phase 2)”) writes **inside `analysis/media/`**, not to a top-level `transcripts/` folder: `copies/` (audio analysis copies + registry), `audio/`, `scenes/`, `transcripts/` (ASR output and, separately, human reviews), `moments.json`, `attribution/` (speaker-attribution evidence and persisted eligibility decisions), `eligibility_report.json` and `dna_handoff.json`. The top-level `transcripts/` entry in the tree above is the original proposal and is not used by the code.

Each project folder additionally holds `project_state.json`, `checkpoints/`, `HANDOFF.md` and `logs/activity.*`, implemented by `orchestrator/creator_state/`; see `PROJECT_STATE.md`.

Keep reusable global assets under `Assets/`; project-specific assets should be copied or referenced under the project folder with provenance.

## 8. Observability

Every task should log:
- Timestamp and task ID.
- Agent/tool and version.
- Input references.
- Output references.
- Status and duration.
- Warnings/errors.
- Retry count.
- Resolve project/timeline identity.
- Any non-idempotent changes.

Provide a concise job status report that explains what completed and what failed.

## 9. Environment

Current environment details are recorded in `MASTER.md` and `RESOLVE_INTEGRATION.md`. Do not hard-code secrets into source files. Keep provider credentials in environment variables or local secret storage. Never commit `.env`, tokens, SSH private keys or credentials.
