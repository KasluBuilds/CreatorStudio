# Persistent Project State, Checkpoints and Handoff

**Status:** IMPLEMENTED and unit-tested (2026-11-08). Resolve is not involved; nothing here has been exercised against real footage or a real multi-provider session yet.
**Code:** `orchestrator/creator_state/` · **Tests:** `orchestrator/tests/` (71 tests: `test_state.py`, `test_claude_md.py`; standard library only)

## Purpose

A video production job must survive a Claude Code session ending, a Claude Pro usage limit, a switch to OmniRoute or another provider, a restart, or an interrupted stage. Nothing relies on a model's memory: **the files in the project folder are the memory.**

> This builds the persistent state foundation. It does **not** switch providers automatically. A human starts the new session and points it at `HANDOFF.md`; `switch-provider` only *records* that the switch happened.

## What exists per project (`Projects/<slug>/`)

| Path | Role |
|---|---|
| `project_state.json` | Authoritative state (schema v1): identity, status, stages, outputs, operation journal, decisions, constraints, tasks, errors, notes, source media, provider history |
| `checkpoints/NNNN_<reason>_<ts>.json` | Append-only snapshots after each completed/skipped/reopened stage. Created exclusively, never rewritten, each carries a SHA-256 of its state so tampering is detectable |
| `HANDOFF.md` | Regenerated after every state change from recorded facts only |
| `logs/activity.log` / `activity.jsonl` | Human-readable and machine-readable event log |
| `backups/` | Damaged state files preserved by `recover` |
| other folders | Working folders from `TECHNICAL_ARCHITECTURE.md` section 7 |

Stages follow `WORKFLOW.md`: `ingest, understand, research, creative_plan, asset_acquisition, plan_validation, resolve_assembly, review_iteration, long_form_delivery, short_form, completion`.

## Rules the code enforces

1. **A stage is completed only after verification.** At least one output must exist inside the project folder, be non-empty, parse if `.json`, and not be original source media. Size and SHA-256 are recorded. Otherwise the stage stays `in_progress`, an error is recorded, and nothing is checkpointed.
2. **Stage order** is enforced; completed stages are never silently restarted (`reopen-stage` needs a reason and invalidates later stages, keeping old outputs in history).
3. **Operation journal.** Record `begin-op` *before* acting and `finish-op` after. A completed operation returns `already_done` and must not be repeated. A **destructive** operation that was started but never confirmed blocks resumption (`verify_before_continuing`) until someone inspects reality and runs `resolve-op`.
4. **Atomic writes.** Temp file + fsync + `os.replace`, under a lock file (stale locks taken over after 120 s). A failed write leaves the previous state intact.
5. **Secrets are redacted** from everything persisted (state, logs, handoff, checkpoints): `sk-…`, bearer tokens, `key=`/`token=`/`password=` values, private-key blocks, common provider key shapes. This is a safety net; do not put credentials into state.
6. **Originals are only referenced** (path, size, mtime, optional hash). The package never writes to, moves or deletes them.
7. **Newer schema versions are refused** rather than guessed at.

## Recovery findings (`check` / `status` / `resume`)

`critical`: `STATE_UNUSABLE`, `OUTPUT_MISSING`, `OUTPUT_CHANGED`, `COMPLETED_WITHOUT_OUTPUT`, `ORDER_VIOLATION`, `MULTIPLE_ACTIVE_STAGES`, `UNCERTAIN_DESTRUCTIVE_OPERATION`, `STATE_BEHIND_CHECKPOINT`, `SOURCE_MEDIA_MISSING`, `SOURCE_MEDIA_CHANGED`.
`warning`: `INTERRUPTED_STAGE`, `UNFINISHED_OPERATION`, `FAILED_STAGE`, `CURRENT_STAGE_MISMATCH`.

Resume actions: `start`, `resume_interrupted`, `retry_failed`, `verify_before_continuing`, `reopen_and_redo`, `recover_state_first`, `manual_investigation`, `none`. Analysis is read-only. `check` and `resume` re-hash outputs by default; `status` and the automatic `HANDOFF.md` refresh compare sizes only unless `--deep` is given (`handoff` command always hashes). A same-size edit is therefore only caught by a deep check.

## Command reference

Run from `CreatorStudio\orchestrator`: `python -m creator_state <command> <project> ...`

`create`, `list`, `status [--json] [--deep]`, `check [--quick]`, `resume`, `handoff`, `checkpoints`, `recover [--seq N] --yes`, `start-stage`, `complete-stage --output PATH...`, `fail-stage`, `skip-stage`, `reopen-stage`, `begin-op [--destructive]`, `finish-op [--failed]`, `resolve-op --applied yes|no --note`, `decide`, `constraint`, `task`, `task-done`, `error`, `error-resolved`, `note`, `add-source`, `switch-provider`, `log`.

Tests: `cd orchestrator; python -I -m unittest discover -s tests -v`

## Ingestion on top of the state layer

`ingest` (see `INGESTION.md`) drives the existing `ingest` stage: every step is a journaled operation, originals are registered through `Project.register_sources` (one atomic update), the stage is completed through the normal verified `complete_stage` (manifest + report), and a checkpoint is saved. An interrupted ingest shows up as `INTERRUPTED_STAGE` and resumes from the probe cache; the handoff gains a "Media inventory" section.

## Session startup (CLAUDE.md)

The root `CLAUDE.md` is loaded by Claude Code when launched from `CreatorStudio` (with either `pro.ps1` or `backup.ps1`, which are unchanged). It tells the session to read `MASTER.md` first, explains the VERIFIED/DECIDED/PROPOSED/OPEN/NOT TESTED labels, and gives the resume procedure: identify the project (`list`), read `HANDOFF.md`, run `check` and `resume`, review interruption findings and the log, inspect the real files, then report to the user before continuing. It forbids repeating interrupted destructive operations and inventing decisions.

**This is instruction, not automation.** Nothing resumes a project on its own: a session follows `CLAUDE.md` only if it reads and obeys it, and a weaker model may follow it less reliably. `orchestrator/tests/test_claude_md.py` verifies that every file, CLI command, flag, stage name and finding code that `CLAUDE.md` mentions exists, but cannot verify that a model behaves accordingly.

## Known limitations

- Nothing calls these functions automatically yet: an agent (or the user) must record state. There is no orchestrator loop.
- Outputs must live inside the project folder; a Resolve export written elsewhere must be copied or referenced via a manifest file inside the project.
- No verification beyond "exists, non-empty, valid JSON, hashed". Stage-specific validation (media duration, render codec, edit-plan schema) belongs to later phases.
- A crash between the state write and checkpoint write can leave state one revision ahead of the newest checkpoint (harmless; the opposite case is detected).
- File locking is advisory and tested single-machine only.
- Not tested with real footage, a real provider switch, or concurrent sessions.
