# AI Creator Studio — instructions for every Claude Code session

Goal: a mostly autonomous video-production system (long-form + shorts) built on DaVinci Resolve Free 21.0.4.5. Most of it is **design, not code yet**. Do not describe anything as working unless it is VERIFIED.

## 0. Multi-agent governance (read before changing any file)
Several Claude Code and ChatGPT sessions work in parallel. Coordination lives in the repository, not in anyone's memory:
- Read `PROJECT_OPERATING_PROTOCOL.md` and `SESSION_HANDOFF.md` first. `PROJECT_STATUS.json` is authoritative for milestone status and progress; `ACTIVE_TASKS.json` for who owns what.
- Work on a task branch in your own Git worktree and claim your files in `ACTIVE_TASKS.json` first. Never push to `main`, never force-push, never stage with `git add .` or `git add -A`.
- Do not edit a package owned by someone else, or marked UNRESOLVED, without an integration agreement. Only one task may hold Resolve at a time.
- Milestone-triggered reminder for Lukas: when Phase 6 reaches its first end-to-end long-form pipeline ready for real-world validation, tell him to stop development-only work and start producing real videos (gate `G-FIRST-REAL-VIDEO` in `PROJECT_STATUS.json` → `user_action_gates`, `AI_Creator_Studio_Documentation/FIRST_REAL_VIDEO_CHECKLIST.md`). Check `SESSION_HANDOFF.md` for an "ACTION REQUIRED" gate.
- Code plus tests is at most "implemented": never record anything as VERIFIED without evidence. The repository holds no footage, launchers or credentials; keep it that way.

## 1. Read, in this order
1. `AI_Creator_Studio_Documentation/MASTER.md` — always, first (goals, non-negotiables, environment).
2. Only the subsystem docs your task needs: `WORKFLOW.md` (stages), `PROJECT_STATE.md` (state/handoff/recovery), `INGESTION.md` (new footage, inventory, proxies), `TECHNICAL_ARCHITECTURE.md`, `RESOLVE_INTEGRATION.md`, `AGENTS.md`, `EDITING_ENGINE.md`, `QUALITY_CONTROL.md`, `ASSETS.md`, `RESEARCH.md`, `CREATOR_DNA.md`.
3. `DECISIONS_AND_OPEN_QUESTIONS.md` and `ROADMAP.md` for current status before proposing work.

Status labels (docs README): **VERIFIED** tested/confirmed · **DECIDED** user-approved · **PROPOSED** design only · **OPEN** needs decision/experiment · **NOT TESTED** exists but unproven. Never promote a proposal to a fact. If docs conflict, prefer the more specific verified fact, flag the conflict, update the docs. Never overwrite a user decision silently.

## 2. Hard rules
- The creator's real words and personality are the source material. Never write replacement dialogue.
- Originals are immutable: never modify, move or delete source media.
- Do not execute editing operations in Resolve without a validated plan and a recovery path. Read-only inspection is fine. Serialize Resolve mutations.
- Do not edit `pro.ps1`, `backup.ps1`, `.mcp.json`, or anything under `tools/` without a clear reason explained to the user first. Keep the two launchers (`pro.ps1` = Claude Pro, `backup.ps1` = OmniRoute); do not add a third.
- Never move, rename, delete or overwrite anything under `Incoming/` or any registered original. Ingestion only references files in place.
- Never ask for, print or store API keys/tokens. Do not copy launcher contents into project files.
- Don't invent missing project decisions, and don't assume work succeeded because a command was run. Verify by reading files/state back.

## 3. Starting or resuming a video project
Nothing resumes automatically: **you** must do this at the start of any session that touches an existing project.

1. **Identify the project.** `cd orchestrator; python -m creator_state list` (projects live in `Projects/<slug>/`). If the user didn't say which one and more than one is plausible, ask. If none exist, don't invent one.
2. **Read** `Projects/<slug>/HANDOFF.md` fully. It is a recorded-facts summary; it contains no hidden reasoning from the previous session.
3. **Audit:** `python -m creator_state check <slug>` (hashes outputs; exit 1 = critical findings) and `python -m creator_state resume <slug>`.
4. **Check for interruptions:** look for `INTERRUPTED_STAGE`, `UNFINISHED_OPERATION`, `UNCERTAIN_DESTRUCTIVE_OPERATION`, `OUTPUT_MISSING/CHANGED`, `STATE_UNUSABLE`, `STATE_BEHIND_CHECKPOINT`, `SOURCE_MEDIA_*`. Also `python -m creator_state log <slug> -n 30`.
5. **Inspect the real files** the handoff names (outputs, edit plans, etc.) before continuing. For a Resolve-related stage, read the actual Resolve state first (read-only tools).
6. **Report to the user** what stage you will resume, any critical findings, and anything you couldn't verify. Then proceed.

If the resume action is blocking (`verify_before_continuing`, `reopen_and_redo`, `recover_state_first`, `manual_investigation`), stop and handle that first; don't work around it.

### Interrupted or risky operations
- **Never repeat an interrupted destructive/risky operation.** Inspect the real result first, then record it: `resolve-op <slug> <stage> <op_id> --applied yes|no --note "what you checked"`.
- Operations already `completed` are not repeated (`begin-op` returns `already_done`).
- To redo a finished stage, use `reopen-stage` with a reason; don't edit state files by hand. If `project_state.json` is damaged: `recover <slug>` (preview), then `recover <slug> --yes`; the damaged file is kept in `backups/` and later work must be re-verified.

## 3a. New footage (ingestion) — details in `INGESTION.md`
Footage goes in `Incoming/<project-name>/` (optional subfolders `camera`, `phone`, `screen`, `audio`, `images`, `notes`; see `Incoming/README.md`) or any other folder, even outside CreatorStudio. Originals are catalogued **in place**: never copied, moved or modified.
1. `python -m creator_state incoming-list`, then `incoming-inspect <name>` (dry run, changes nothing).
2. `python -m creator_state ingest <name>` (creates the project, completes the `ingest` stage, writes `source_manifest.json` and `analysis/ingest_report.md`). Re-running on a finished project needs `--reopen`, which invalidates later stages: ask first.
3. `python -m creator_state inventory <slug>`, `ingest-status <slug>`, and `verify-sources <slug> --deep` (exit 1 = originals missing or changed). Run `verify-sources` when resuming before relying on any original.
- If ffprobe is missing, metadata stays blank (`null`). Never guess durations, frame rates or codecs; follow the recorded task. `FFPROBE_UNAVAILABLE`, `PROBE_FAILED`, `VARIABLE_FRAME_RATE` and `DUPLICATE_CONTENT` warnings need attention before editing.
- `proxy-make` and `managed-copy` are explicit and need `--yes`; run them only when the user asks. Ingestion has been tested on synthetic media only: do not run it on the user's real footage without their permission. Transcription does not exist yet (no engine is chosen).

## 3b. Shot detection — details in `SHOT_DETECTION.md`
- `python -m creator_state shots-plan <slug>` is a dry run (starts nothing, writes nothing). `shots-detect <slug> --yes` runs it: the originals are only read and results go under `analysis/shots/`. `shots-status <slug>` lists runs (only COMPLETE ones are results; INCOMPLETE and FAILED are not). `shots-check <slug>` re-verifies them (exit 1 = critical).
- Verified on synthetic clips only: do not run `shots-detect` on the user's real footage without their permission. A BLOCKED video is reported with its reason (variable frame rate is refused, only the first video stream is analysed): do not work around it. Confidence is heuristic, not calibrated.

## 4. Recording state (do this as you work, not at the end)
Run from `orchestrator/` as `python -m creator_state <command> <slug> ...`. Full list: `python -m creator_state --help`; details in `PROJECT_STATE.md`.

| When | Command |
|---|---|
| New project | `create "<title>" --source <original file>` (reference only, never copied) |
| Begin a stage | `start-stage <slug> <stage>` |
| Before a risky step | `begin-op <slug> <stage> <op_id> "<what>" [--destructive]`; after: `finish-op <slug> <stage> <op_id> [--failed] [--result ...]` |
| User or you decide something | `decide <slug> "<text>" --why "<reason>" [--stage S] [--by user or claude]` — only record decisions that were actually made |
| Constraint / task / problem | `constraint`, `task` / `task-done`, `error` / `error-resolved`, `note` |
| Stage finished | write the output file inside the project folder, then `complete-stage <slug> <stage> --output <path> [--output ...] --summary "..."` — it refuses unless outputs exist, are non-empty and valid; a checkpoint is saved automatically |
| Stage failed / not applicable | `fail-stage <slug> <stage> "<msg>"` / `skip-stage <slug> <stage> "<reason>"` |
| Provider switch (user's action) | `switch-provider <slug> "<from>" "<to>" --reason "..."` — records only; it does not switch anything |
| Hand over | `handoff <slug>` (regenerates `HANDOFF.md` with full verification); it is also refreshed after every state change |

Stages, in order: `ingest, understand, research, creative_plan, asset_acquisition, plan_validation, resolve_assembly, review_iteration, long_form_delivery, short_form, completion`. Checkpoints are append-only; list them with `python -m creator_state checkpoints <slug>`. Never put secrets in state, notes or logs.

## 5. Working style
Small verifiable steps; explain PowerShell commands simply (user's shell is PowerShell on Windows). Update the relevant doc whenever behavior or decisions change. Tests for the state layer: `cd orchestrator; python -I -m unittest discover -s tests`.
