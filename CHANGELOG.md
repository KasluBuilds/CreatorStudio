# Changelog

Repository history of CreatorStudio, newest first. Entries cover what changed in **this repository**; design decisions and the
project's earlier history are in `AI_Creator_Studio_Documentation/DECISIONS_AND_OPEN_QUESTIONS.md` (section 6). The project had no version control
before `51fcd85`, so nothing earlier is listed here and nothing is reconstructed. Every merged task adds an entry (see `PROJECT_OPERATING_PROTOCOL.md`).

## Unreleased — branch `task/development-tracker` (task `T-GOV-002`, step 01: tracker and step protocol, pending review)

- Claim merged earlier as PR #4 (merge commit `27685bac2b0bc58c961e4e3d26b57af38f1bc3b2`), including a narrow fix so `test_leases_exit_codes` uses a fixture registry.
- Added `DEVELOPMENT_TRACKER.json` (step-level record linked to task ids; holds no milestone, weight, percentage or lease data), `DEVELOPMENT_STEP_PROTOCOL.md` (one reviewed step at a time; stop at `READY_FOR_REVIEW`; Lukas's explicit word completes a step and starts the next), `dashboard/tracker_check.py` (stdlib validator for structure, linkage, transitions and the completion gate) and `orchestrator/tests/test_development_tracker.py`.
- `CLAUDE.md` and `PROJECT_OPERATING_PROTOCOL.md` (new section 15) now require the step protocol. Removed the expired agreement `AGR-GOV-LEASE-TEST` from `ACTIVE_TASKS.json`.
- No milestone state, weight, computed progress, evidence or gate changed. The dashboard page and snapshot builder are later steps and do not exist yet.

## Unreleased — branch `task/governance-housekeeping` (governance wording only, pending review)

- `PROJECT_STATUS.json`: removed `next_actions` that referred to the now-merged PR #2 and to connecting/testing the ChatGPT connector; added the REPORTED connector retrieval and an action to confirm the weekly automation's access.
- Notification wording (`PROJECT_STATUS.json` gate, `PROJECT_OPERATING_PROTOCOL.md` section 14, `ROADMAP.md`, `FIRST_REAL_VIDEO_CHECKLIST.md`) now distinguishes the repository's own reminders from the REPORTED weekly ChatGPT "Phase 6 Readiness Check": scheduled existence is not evidence of successful private-repository access, which is NOT VERIFIED.
- No milestone state, evidence, weight or gate trigger changed; `SESSION_HANDOFF.md` regenerated.

## 2026-11-09 — `93a9a829e073595151666f99b0d1a57416db28c3` Governance layer (PR #1, task `T-GOV-001`, merged)

Merged with a merge commit; PR head `4eb229645dc054939dea1b1ee4a9e6089671df42`. Full suite on merged main: 1166 tests, 0 failures, 0 errors, 1 skipped. Evidence: `E-FULLSUITE-0003` in `PROJECT_STATUS.json`.

### Added
- `PROJECT_OPERATING_PROTOCOL.md`: rules for parallel Claude Code and ChatGPT work (branches, worktrees, ownership, task leases, integration, tests, security, ChatGPT procedure).
- `PROJECT_STATUS.json`: 28 weighted milestones with separate implementation and verification records and an evidence registry.
- `ACTIVE_TASKS.json`: package ownership registry (unresolved owners recorded as `UNRESOLVED`), shared-file list, integration agreements and the task registry.
- `SESSION_HANDOFF.md`: generated from the two JSON files.
- `orchestrator/governance/` and `orchestrator/tests/test_governance.py`: validation, a deterministic progress calculation (`python -B -m governance score`), lease checks and the handoff generator.
- `AI_Creator_Studio_Documentation/ENVIRONMENT.md`: development environment and setup facts, labelled VERIFIED or REPORTED.
- `CHANGELOG.md` (this file).

### Self-review corrections (same branch, after the first push)
- Scoring: `weights_status` is now an enum (`PROPOSED` or `APPROVED`); approval needs a human, a date and a `weights_fingerprint`, so any later weight change invalidates it. `human_review` evidence must name a human and carry `real_input: true`. `score` and the handoff say whether weights are proposed and how many milestones are verified on reviewed real input.
- Tamper resistance: `governance validate --against <ref>` rejects removed milestones, edited or removed evidence, verification upgrades without new evidence, and weight changes after approval.
- Ownership: directory claims without a trailing slash and case variants now overlap; branch prefixes must match the owner type; new `governance scope` compares a branch's changed files with its claimed paths.
- User-action gate `G-FIRST-REAL-VIDEO` (tell Lukas to start producing real videos when the first long-form pipeline is ready) in `PROJECT_STATUS.json`, `PROJECT_OPERATING_PROTOCOL.md` section 14, `CLAUDE.md`, `ROADMAP.md`, the generated handoff and the new `FIRST_REAL_VIDEO_CHECKLIST.md`. Validation fails once the trigger fires while the gate is still PENDING. No external notification exists.
- Real-input evidence from the local `Projects/my-first-test` recorded as machine-local observations (ingest and shot detection of one real file, transcription experiments). No verification level was raised and nothing is VERIFIED.
- Filming gate trigger corrected on Lukas's review: it now fires on a dedicated readiness milestone `M-LFE-03` (weight 4, PROPOSED, NOT_STARTED), not on long-form assembly (`M-LFE-01`). The milestone may be marked implemented only if its dependencies are implemented and each of five readiness criteria (RC-1 to RC-5: footage processed, validated plan and Resolve timeline, complete long-form render, initial QC and export verification, originals preserved with recoverable state) has `test_run` or `observation` evidence. Readiness to begin real-world validation, not proof of production-quality editing. The generated handoff shows the criteria while PENDING and the checklist when TRIGGERED. Adding the milestone moved the figures to 35.4% implementation and 12.2% verification (29 milestones, weight 164).
- 44 new governance tests (97 in total).

### Changed
- `CLAUDE.md`: new section 0 pointing sessions at the governance files and the branch/ownership rules.
- `ROADMAP.md`: status-authority note, milestone index, Phase 0 read-only MCP item ticked on the strength of the 2026-11-08 change-log record, notes that offline Research, Asset and Creative Director foundations exist.
- `DECISIONS_AND_OPEN_QUESTIONS.md`, `MASTER.md`, `README.md`, `AGENTS.md`, `RESEARCH.md`, `ASSETS.md`: small status corrections and pointers; no design decision changed.

## 2026-11-09 — `5f6925e` Treat machine-local files as optional in test_claude_md; validate .mcp.json.example

- A fresh clone failed `test_referenced_files_exist` because `CLAUDE.md` names `pro.ps1`, `backup.ps1`, `.mcp.json` and `tools/`, which are git-ignored. They are now optional (existence only, never read).
- New tests: the optional files must be git-ignored and still mentioned; `.mcp.json.example` must be valid, placeholder-only and free of personal paths.
- Full suite: 1069 tests, 0 failures, 0 errors, 1 skipped (symlink test), on the original project and on a clean clone.

## 2026-11-09 — `51fcd85` Initial import of CreatorStudio source and documentation

- 108 files: `orchestrator/` (creator_state, media_analysis, creator_dna, speaker_attribution, creative_director, research, assets, tests), `AI_Creator_Studio_Documentation/`, `CLAUDE.md`, `TOOLS.md`, `.claudeignore`, `.gitignore`, `.gitattributes`, `.mcp.json.example`, `Incoming/README.md`.
- Excluded on purpose: footage and derived per-project data, `tools/`, launchers, `.mcp.json`, local settings, media and model files.
- One change to existing source before import: the `FAKE_TOKEN` fixture in `orchestrator/tests/test_state.py` was replaced with a fully synthetic value (it had shared a prefix with a real credential).
- Pre-import baseline: 1067 tests, 0 failures, 0 errors, 1 skipped.
