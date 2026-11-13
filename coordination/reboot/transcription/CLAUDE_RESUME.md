# Claude Code resume brief — Transcription and media evidence

You are the implementation agent for the Transcription workstream. This is a fresh session; do not assume any previous conversation memory.

## First: read current repository state

1. Run `git fetch origin`.
2. Read `CLAUDE.md`, `PROJECT_OPERATING_PROTOCOL.md`, `SESSION_HANDOFF.md`, `PROJECT_STATUS.json`, `ACTIVE_TASKS.json`.
3. Read `AI_Creator_Studio_Documentation/DECISIONS_AND_OPEN_QUESTIONS.md`, `INGESTION.md`, `SHOT_DETECTION.md`, and the transcription engine brief if present.
4. Inspect the report `handoffs/transcription/STATUS_REPORT.md`.
5. Inspect local experiment evidence under `Projects/my-first-test/analysis/transcription/` only if it is present in the primary checkout. These files are ignored by Git; do not move, rename, delete, rewrite or stage them.

## Initial task is READ-ONLY

Prepare:
- an inventory of the local transcription runs and human-reference files;
- a concise explanation of the mismatch between engine text and listening notes, with file evidence;
- a Q11 comparison brief, without selecting an engine;
- an M-INT-03 architecture map of both shot detectors and the false-positive hypothesis from the 2026-11-09 run.

Do not edit source code or shared docs yet. The `creator_state`, `media_analysis`, `speaker_attribution`, and cross-package test ownership entries are UNRESOLVED. Ask ChatGPT to obtain an integration agreement or explicit owner assignment before touching them.

## Safety

- No new real-footage processing without fresh explicit permission from Lukas.
- No hosted audio upload without explicit consent.
- Preserve all local experiments and scratch evidence.
- Do not tune thresholds against MVI_0890.MOV alone.
- Do not use MVI_0879.MOV for Creator DNA.

After the read-only audit, report exact findings and stop. Wait for a registered task and lease before implementation.
