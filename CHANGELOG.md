# Changelog

Repository history of CreatorStudio, newest first. Entries cover what changed in **this repository**; design decisions and the
project's earlier history are in `AI_Creator_Studio_Documentation/DECISIONS_AND_OPEN_QUESTIONS.md` (section 6). The project had no version control
before `51fcd85`, so nothing earlier is listed here and nothing is reconstructed. Every merged task adds an entry (see `PROJECT_OPERATING_PROTOCOL.md`).

## Unreleased — branch `task/governance-foundation` (task `T-GOV-001`, under review, not merged)

The commit SHA is recorded in `PROJECT_STATUS.json` and in this entry after merge.

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
- 38 new governance tests (91 in total).

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
