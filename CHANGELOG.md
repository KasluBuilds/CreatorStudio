# Changelog

Repository history of CreatorStudio, newest first. Entries cover what changed in **this repository**; design decisions and the
project's earlier history are in `AI_Creator_Studio_Documentation/DECISIONS_AND_OPEN_QUESTIONS.md` (section 6). The project had no version control
before `51fcd85`, so nothing earlier is listed here and nothing is reconstructed. Every merged task adds an entry (see `PROJECT_OPERATING_PROTOCOL.md`).

## Unreleased — task `T-AST-001` Step 1 (branch `task/asset-required-uses-fail-closed`, pending review)

- `S-AST-001-01` (`IN_PROGRESS` since PR #14, merge commit `34020d898cf720bd27731a1efd89f20420fc2f42`) implemented and set `READY_FOR_REVIEW`.
- `orchestrator/assets/rights.py`: `evaluate_rights` now quarantines (new reason code `USE_NOT_DECLARED`) when `UseContext.required_uses` is non-empty and the record's `allowed_uses` is empty. A required use missing from a non-empty `allowed_uses` is still `USE_NOT_ALLOWED` (quarantine); explicitly allowed uses still pass. No other rights-policy change.
- `orchestrator/tests/test_assets.py`: three synthetic regression tests, one per case.

## Unreleased — task `T-AST-001` start and registry agreement (branch `task/asset-required-uses-start`, pending review)

- Governance-only: records claim PR #13 merged (merge commit `12c5964781261fcc49ce6e62030636aad4b64fce`) with `S-AST-001-00` `COMPLETED`, and sets `S-AST-001-01` `IN_PROGRESS`. No Asset code or tests.
- Adds integration agreement `AGR-GOV-REGISTRY-01` (T-AST-001 and T-DNA-001 may each edit only their own records in `ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`, `SESSION_HANDOFF.md`; changelog append-only; handoff regenerated; merges serialized by ChatGPT) and lists it on T-AST-001 only. T-DNA-001's entry is not edited here.

## Unreleased — task `T-AST-001` claim (branch `task/asset-required-uses-fail-closed`, pending review)

- Claim only: registers `T-AST-001` (owner `claude:asset-stream`, 24-hour lease, restarted 2026-11-14 when the branch was refreshed with `origin/main` adc0f99) and its steps `S-AST-001-00` (claim, `READY_FOR_REVIEW`) and `S-AST-001-01` (implementation, `PLANNED`, not started). No source, test, status or milestone change.
- Planned fix, not yet made: `evaluate_rights` currently passes a required use silently when a record's `allowed_uses` is empty; Step 1 will quarantine that case. `SESSION_HANDOFF.md` regenerated with the official command.

## 2026-11-14 — T-GOV-003 Per-task development step limit (complete; lease and owned paths released)

- T-GOV-003 is complete. PR #11 was reviewed by ChatGPT and merged into `main` (merge commit `232172e62f3f7c7d1ba04d5a438d03689b9483d2`; reviewed head `e4dd01cd62c3e382e5fc79bffd1387402323b677`). Final closeout PR #12 moves S-GOV-003-01 to `COMPLETED` with a `REPORTED` completion record (ChatGPT reviewed and merged under Lukas's standing delegation; this does not claim Lukas reviewed the code), sets T-GOV-003 to `COMPLETED`, and releases its lease and owned paths.
- PR #10 reviewed by ChatGPT and merged into `main` (merge commit `cd6412bc4d207adc238b4f400c33870e2f3b1b9f`; reviewed head `c05fb31bd19c6fd97463407edfd397857285e4d2`). S-GOV-003-01 is now `READY_FOR_REVIEW`; a final closeout PR will record this PR's merge, complete the step and release the lease and owned paths.

- PR #9 merged by ChatGPT as delegated integrator (merge commit `67b70dc251a4708957f1952d6032b3a4e1735043`, reviewed head `c9b1a6010e894fe181cbd77060d4291913249250`). Pull request #10 records the merge evidence and completes S-GOV-003-00 with a REPORTED completion record. T-GOV-003 stays `ACTIVE` with its lease and owned paths and S-GOV-003-01 is now `READY_FOR_REVIEW`; a final closeout pull request after PR #11 is merged will complete the step and release the lease. The bootstrap exception (claim and implementation in one pull request) applied to T-GOV-003 only.
- `dashboard/tracker_check.py`: the limit of one `IN_PROGRESS`/`READY_FOR_REVIEW` step is now counted per task, so different tasks may each have one; steps stay sequential within each task, the error names the task, unregistered tasks are still rejected.
- `DEVELOPMENT_STEP_PROTOCOL.md` (section 3 item 1, history) and `PROJECT_OPERATING_PROTOCOL.md` (section 15, history) say the limit is per task. Claims, leases, one writer to `main`, one pull request integrated at a time and stopping at `READY_FOR_REVIEW` are unchanged.
- Repaired `test_protocol_authority_rules_do_not_contradict_the_standing_instruction` and `test_active_tasks_wording_matches_protocol_section_3a`, which still expected Lukas alone to merge pull requests; they now assert the delegated-integrator wording. Added per-task tests.
- Claim and implementation share one pull request as a **one-time bootstrap exception** approved by ChatGPT as delegated integrator; later tasks need their own claim pull request.

## 2026-11-13 — T-GOV-002 Development Tracker (PR #5 merged; follow-up lease release)

- Claim merged earlier as PR #4 (merge commit `27685bac2b0bc58c961e4e3d26b57af38f1bc3b2`), including a narrow fix so `test_leases_exit_codes` uses a fixture registry.
- Added `DEVELOPMENT_TRACKER.json` (step-level record linked to task ids; holds no milestone, weight, percentage or lease data), `DEVELOPMENT_STEP_PROTOCOL.md` (one reviewed step at a time; stop at `READY_FOR_REVIEW`; ChatGPT may approve and authorize progression and handles task PR integration and lease release under Lukas's explicit standing delegation; Claude never self-approves or merges), `dashboard/tracker_check.py` (stdlib validator for structure, linkage, transitions and the completion gate) and `orchestrator/tests/test_development_tracker.py`.
- `CLAUDE.md` and `PROJECT_OPERATING_PROTOCOL.md` (new section 15) now require the step protocol. Removed the expired agreement `AGR-GOV-LEASE-TEST` from `ACTIVE_TASKS.json`.
- Review correction (same branch): `tracker_check.py` now has `--against REF`, which compares the tracker with the one committed at REF and rejects illegal transitions, edits to completed steps and removed steps; tests drive it through the real CLI with a throwaway Git repository. Previously only helper functions did this and the CLI did not call them. Without `--against` no history is checked.
- Step 02: `dashboard/build_snapshot.py` (stdlib only) writes one JSON snapshot to an explicit `--out` path from `PROJECT_STATUS.json`, `ACTIVE_TASKS.json` and `DEVELOPMENT_TRACKER.json` (read-only; refuses invalid sources). It records the source commit, branch, UTC time and working-tree cleanliness (null if git is unavailable), copies progress figures unchanged, labels itself a snapshot that is not live GitHub data, and scrubs private/local paths, media file names and credential-looking strings. Tests added to `test_development_tracker.py`. A second review found that dictionary keys were not scrubbed: keys are now scrubbed like values, and two keys that become identical afterwards are refused (exit 2) instead of silently overwritten. After ChatGPT's review the scrubber was strengthened: Windows backslash paths for private folders, POSIX absolute paths (`/mnt`, `/tmp`, `/root`, `/workspace` and similar), `file:` URLs, `../`, `~/` and `$HOME` paths are removed whole, not just their media file names, with regression tests across many source fields; ordinary repository paths and GitHub URLs are unchanged. The module documentation no longer says an unavailable Git causes exit code 2 (the snapshot is still written with null Git metadata).
- Step 04 closeout: `DEVELOPMENT_STEP_PROTOCOL.md` section 3a resolves the contradiction between "only Lukas starts the next step" and Lukas's standing instruction to ChatGPT: ChatGPT may give technical approval and authorize the next step; Claude still stops at `READY_FOR_REVIEW` and never self-approves; ChatGPT handles task PR integration and lease release under Lukas's explicit standing delegation; Lukas retains final authority. `PROJECT_OPERATING_PROTOCOL.md` (table row, section 15) and the tracker's status descriptions were aligned. The completion gate was not weakened: `completion.source` must now begin with `DIRECT` or `REPORTED` (checked by `tracker_check.py`, with tests). `CLAUDE.md` was aligned with section 3a by ChatGPT in commit `ca6d802db725d2b8457fd81a10ae57ae5641095c` (Claude's own edit to it was not permitted in the session), and the changelog wording by `93b13349300341b6aa3372a3dee69688b0139e8d`.
- Further Step 04 corrections: the `lukas_confirmed` validation message now matches section 3a (the standing instruction is an allowed confirmation recorded with a `REPORTED` source), and the T-GOV-002 description and acceptance criterion in `ACTIVE_TASKS.json` were reworded to match; the lease and ownership are unchanged.
- Dashboard check in a real browser: the page was loaded in the already-installed Chrome (headless, driven through its DevTools protocol, nothing installed) with a generated snapshot: all sections rendered at 1280 and 390 px wide with no horizontal overflow, a bad file showed an error, no page errors and no non-file network requests. Not covered: drag-and-drop, dark mode, other browsers.
- Final implementation PR #5 merged into `main` by ChatGPT under Lukas's explicit delegation (merge commit `75b68d83aab5dbab57178f95b43012c7f5051209`). Step 04 was technically approved at `23953857ddeda5a02f7c991a32d5ea0af464b0f1`; the full-suite rerun at that exact head was reported as 1,244 tests in 193.4s, OK (1 skipped), not independently run by ChatGPT. The follow-up closeout PR records completion and releases the task lease and owned paths.
- No milestone state, weight, computed progress, evidence or gate changed. Step 03 added `dashboard/index.html`, a self-contained read-only viewer for a snapshot file (file picker and drag-and-drop, no network access, no credentials, text set only via `textContent`); it shows the snapshot warning, progress copied from the snapshot, the current step with its completion gate and terminal-clear status, task steps with criteria and evidence, milestones by phase, decisions, blockers, next actions and the recorded GitHub facts. Its pure logic is tested under Node when Node is installed; it has not been opened in a real browser by Claude.

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

