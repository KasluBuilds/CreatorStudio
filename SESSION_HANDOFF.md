<!-- GENERATED FILE. Do not edit by hand. Regenerate with:
     cd orchestrator && python -B -m governance handoff --write
     Source of truth: PROJECT_STATUS.json and ACTIVE_TASKS.json. -->

# Session handoff

Status record dated **2026-11-18**. Baseline commit **`93a9a829e073`** on `main`.

## User-action gates (things Lukas must be told at a milestone)

- **G-FIRST-REAL-VIDEO - Stop development-only work; start producing real videos** (PENDING; fires when any of M-LFE-03 is implemented; now: M-LFE-03 is NOT_STARTED).
  Fires only when ALL of these have test_run or observation evidence on M-LFE-03:
  - RC-1: Source footage is processed through the editing pipeline (ingest, understanding, plan) by the pipeline itself.
  - RC-2: A validated edit plan is produced and a Resolve timeline is built from it.
  - RC-3: A complete long-form output is rendered.
  - RC-4: Initial automated QC and export verification are completed on that output.
  - RC-5: Originals are preserved (verified unchanged) and the project state stays recoverable throughout.
  When it fires: The first end-to-end long-form pipeline is ready for real-world validation. Stop development-only work and start producing real videos: only real recordings can show whether the editing is good, and the finished long-form videos become the source material for Phase 7 short-form repurposing. This is readiness to begin validation, not proof that production-quality editing has been achieved.
  Notification: In the repository, `governance validate` (it fails once the trigger fires while the gate is PENDING) and the generated SESSION_HANDOFF.md are the reminders. REPORTED by Lukas, not verified by Claude: a weekly ChatGPT automation named "Phase 6 Readiness Check" is scheduled inside ChatGPT. NOT VERIFIED: that this automation can read the private repository or has completed any check; no completed run is recorded here. The automation does not trigger, replace or acknowledge the gate; only milestone evidence and validation trigger it and only Lukas acknowledges it.

## Verified baseline

- Command: `cd orchestrator && python -B -I -m unittest discover -s tests`
- Result: 1166 tests, 0 failures, 0 errors, 1 skipped (2026-11-09; merged main in the primary folder after PR #1; the skip is test_symlink_source_refused (symlinks unavailable)).
- This is a point-in-time result tied to the commit above. Re-run before relying on it.

## Progress (computed from PROJECT_STATUS.json)

- Implementation: **43.0%**  
- Verification: **16.5%**  
- 29 milestones, total weight 164. Verification never exceeds implementation. Weights are PROPOSED: drafted by an AI session and not yet reviewed by Lukas, so these figures are provisional. 
- How to read them: *implementation* is the weighted share of milestones whose code exists (half credit if in progress); *verification* is the weighted share demonstrated at the level evidence supports. Neither measures effort, time left, or whether the product works on real footage.
- Milestones verified on reviewed real input: 0 of 29 (none).
- Milestones whose verification rests only on *reported* (not re-observed) evidence: none.

| Phase | Weight | Implemented | Verified |
|---|---:|---:|---:|
| Before Phase 4 | 10 | 0.0% | 0.0% |
| Governance | 6 | 100.0% | 37.5% |
| Phase 0 | 5 | 100.0% | 50.0% |
| Phase 1 | 10 | 100.0% | 50.0% |
| Phase 2 | 22 | 59.1% | 29.5% |
| Phase 3 | 21 | 47.6% | 16.7% |
| Phase 4 | 22 | 65.9% | 19.3% |
| Phase 5 | 26 | 23.1% | 5.8% |
| Phase 6 | 22 | 0.0% | 0.0% |
| Phase 7 | 8 | 0.0% | 0.0% |
| Phase 8 | 6 | 0.0% | 0.0% |
| State foundation | 6 | 100.0% | 25.0% |

## Milestones

| ID | Title | Weight | Implementation | Verification level |
|---|---|---:|---|---|
| M-AST-01 | Asset Agent offline foundation | 6 | IMPLEMENTED | UNIT_ISOLATED (IN_PROGRESS) |
| M-AST-02 | Asset acquisition, providers and licence verification | 5 | IN_PROGRESS | UNIT_ISOLATED (IN_PROGRESS) |
| M-CRD-01 | Creative Director offline foundation | 6 | IMPLEMENTED | UNIT_ISOLATED (IN_PROGRESS) |
| M-CRD-02 | Creative Director on real transcripts with Research and Creator DNA | 4 | NOT_STARTED | NONE (NOT_STARTED) |
| M-DNA-01 | Creator DNA evidence store and confidence model | 6 | IMPLEMENTED | UNIT_ISOLATED (IN_PROGRESS) |
| M-DNA-02 | Creator DNA learned from real recordings | 8 | NOT_STARTED | NONE (NOT_STARTED) |
| M-DNA-03 | Creator-facing review of transcripts, speakers and traits | 3 | NOT_STARTED | NONE (NOT_STARTED) |
| M-EDT-01 | Validated edit-plan schema and Editor planning workflow | 8 | NOT_STARTED | NONE (NOT_STARTED) |
| M-EDT-02 | High-level validated Resolve wrapper | 8 | NOT_STARTED | NONE (NOT_STARTED) |
| M-HRD-01 | Production hardening | 6 | NOT_STARTED | NONE (NOT_STARTED) |
| M-INF-01 | Canonical GitHub repository and safety configuration | 3 | IMPLEMENTED | INTEGRATION (IN_PROGRESS) |
| M-INF-02 | Governance layer and deterministic progress calculation | 3 | IMPLEMENTED | UNIT_ISOLATED (IN_PROGRESS) |
| M-ING-01 | Hybrid media ingestion and inventory | 5 | IMPLEMENTED | INTEGRATION (IN_PROGRESS) |
| M-INT-01 | End-to-end Phase 2 to 3 run on one real recording of the creator | 6 | NOT_STARTED | NONE (NOT_STARTED) |
| M-INT-02 | Resolve the media_analysis and speaker_attribution dependency cycle | 2 | NOT_STARTED | NONE (NOT_STARTED) |
| M-INT-03 | Reconcile the two shot detectors | 2 | NOT_STARTED | NONE (NOT_STARTED) |
| M-LFE-01 | Long-form autonomous assembly in Resolve | 10 | NOT_STARTED | NONE (NOT_STARTED) |
| M-LFE-02 | Review render, QC loop and export verification | 8 | NOT_STARTED | NONE (NOT_STARTED) |
| M-LFE-03 | First end-to-end long-form pipeline ready for real-world validation | 4 | NOT_STARTED | NONE (NOT_STARTED) |
| M-MED-01 | Media analysis foundation | 6 | IN_PROGRESS | UNIT_ISOLATED (IN_PROGRESS) |
| M-MED-02 | Transcription engine and time-aligned transcripts | 6 | BLOCKED | NONE (NOT_STARTED) |
| M-RSR-01 | Research Agent offline foundation | 6 | IMPLEMENTED | UNIT_ISOLATED (IN_PROGRESS) |
| M-RSR-02 | Live research providers and pipeline integration | 5 | NOT_STARTED | NONE (NOT_STARTED) |
| M-RSV-01 | Resolve Free scripting, bridge and MCP baseline | 5 | IMPLEMENTED | INTEGRATION (IN_PROGRESS) |
| M-RSV-02 | Safe, validated Resolve operations | 10 | IMPLEMENTED | INTEGRATION (IN_PROGRESS) |
| M-SFE-01 | Short-form repurposing and vertical export | 8 | NOT_STARTED | NONE (NOT_STARTED) |
| M-SHT-01 | Shot, transition and candidate detection with CLI | 5 | IMPLEMENTED | INTEGRATION (IN_PROGRESS) |
| M-SPK-01 | Speaker attribution and Creator DNA eligibility | 4 | IMPLEMENTED | INTEGRATION (IN_PROGRESS) |
| M-STA-01 | Persistent project state, checkpoints, handoff and recovery | 6 | IMPLEMENTED | UNIT_ISOLATED (IN_PROGRESS) |

## Active tasks

| Task | Owner | Branch | Status | Lease expires | Owns |
|---|---|---|---|---|---|
| T-RSV-002 Safe, validated DaVinci Resolve execution layer (M-RSV-02) | claude:repository-coordinator | `task/t-rsv-002` | ACTIVE | 2026-11-22T16:00:00+00:00 | orchestrator/resolve_executor/, orchestrator/tests/test_resolve_executor.py, AI_Creator_Studio_Documentation/RESOLVE_INTEGRATION.md, AI_Creator_Studio_Documentation/TECHNICAL_ARCHITECTURE.md, PROJECT_STATUS.json, ACTIVE_TASKS.json, DEVELOPMENT_TRACKER.json, SESSION_HANDOFF.md, CHANGELOG.md |
| T-EDT-001 Validated edit-plan schema and Editor planning workflow (M-EDT-01, offline) | claude:repository-coordinator | `task/t-edt-001-edit-plan` | ACTIVE | 2026-11-23T17:00:00+00:00 | orchestrator/edit_plan/, orchestrator/tests/test_edit_plan.py, AI_Creator_Studio_Documentation/EDITING_ENGINE.md |
| T-DEV-043 dev_supervisor: machine-readable independent flag on review allocations (first task run by a dispatched worker) | claude:main-coordinator | `task/t-dev-043-review-independence` | ACTIVE | 2026-11-25T06:36:49+00:00 | orchestrator/dev_supervisor/model_allocation.py, orchestrator/tests/test_dev_model_allocation.py |
| T-DEV-045 dev_supervisor: claim command that registers a task from a spec and applies the claim-PR rules | claude:main-coordinator | `task/t-dev-045-claim-command` | ACTIVE | 2026-11-25T07:06:24+00:00 | orchestrator/dev_supervisor/claim.py, orchestrator/tests/test_dev_claim.py |
| T-DEV-046 dev_supervisor: record the first dispatched worker run, its failure mode and the new loop tools | claude:main-coordinator | `task/t-dev-046-worker-run-record` | ACTIVE | 2026-11-25T07:21:32+00:00 | orchestrator/dev_supervisor/WORKER_DISPATCH.md, orchestrator/dev_supervisor/WORKER_GATEWAY.md |

Leases are not judged here (this file never reads the clock): run `cd orchestrator && python -B -m governance leases` to list expired ones.

## Package ownership

| Package | Owner | How established |
|---|---|---|
| `creator_state` | UNRESOLVED | UNRESOLVED - built by earlier sessions (state, ingestion, shot detection); Lukas has not named a current owner |
| `media_analysis` | UNRESOLVED | UNRESOLVED - shared between the Creator DNA and earlier media work; deliberately left open until subsystem boundaries are reviewed |
| `speaker_attribution` | UNRESOLVED | UNRESOLVED - written alongside Creator DNA on 2026-11-09 but no owner named; its import cycle with media_analysis (M-INT-02) needs one owner |
| `cross_package_tests` | UNRESOLVED | UNRESOLVED - test files spanning media_analysis, speaker_attribution and creator_dna |
| `creator_dna` | claude:creator-dna-stream | REPORTED - Lukas's brief names Creator DNA as one of four subsystem assignments; the stream identity is assumed, confirm it |
| `research` | claude:research-stream | ASSIGNED - Lukas explicitly authorized claude:research-stream to act as Research owner for Assignment 6 on 2026-11-17 (relayed in session; recorded by claim PR #32 and its start update) |
| `assets` | claude:asset-recovery | ASSIGNED - Lukas confirmed on 2026-11-17 that claude:asset-stream is abandoned and authorized the takeover of T-AST-001 by claude:asset-recovery (task T-AST-002) |
| `creative_director` | claude:creative-director-stream | REPORTED - Lukas's brief names the Creative Director as one of four subsystem assignments; the stream identity is assumed, confirm it |
| `governance` | claude:governance-session | ASSIGNED - Lukas assigned the governance migration to this session on 2026-11-09 |
| `resolve_executor` | claude:resolve-stream | ASSIGNED - Lukas assigned T-RSV-002 (safe Resolve execution layer, M-RSV-02) directly in session on 2026-11-17 |
| `dev_supervisor` | claude:repository-coordinator | ASSIGNED - Lukas authorized issue #44 (autonomous development supervisor) on 2026-11-18; built by wave w03-dev-supervisor; development-time infrastructure, not a video-production agent |

Unresolved ownership: `creator_state`, `media_analysis`, `speaker_attribution`, `cross_package_tests`. Touching these needs an integration agreement (see PROJECT_OPERATING_PROTOCOL.md).

## Blockers and open questions

- **M-MED-02** blocked by: Decision Q11 (transcription engine) has not been made.
- Weights and milestone granularity in this file need Lukas's review (weights_status).
- Ownership of the creator_state, media_analysis and speaker_attribution packages is UNRESOLVED (ACTIVE_TASKS.json package_owners).
- Q11: which transcription engine. No engine is chosen. Local experiments on one real recording exist (E-OBS-LOCAL-TRANSCRIPTION-EXP); the engine text and the creator's listening notes disagree and the discrepancy is unresolved.
- CLAUDE.md, INGESTION.md and SHOT_DETECTION.md still say ingestion and shot detection were tested on synthetic media only, but a real file was ingested (2026-11-08) and shot-detected (2026-11-09) in Projects/my-first-test. Lukas to confirm this was intended and to review the results; those documents belong to the creator_state owner (UNRESOLVED) and were not edited here.
- Q33 (when the understand stage may complete) and Q35 (two shot detectors) remain OPEN in DECISIONS_AND_OPEN_QUESTIONS.md.
- The repository became public on 2026-11-17. Branch protection on main is configured and read back (E-OBS-PROTECTION-0001): pull request required, `validate` required and up to date, administrators included, no force-push or deletion; merge-commit-only remains procedural. A public repository makes everything committed world-readable: Lukas to confirm that is intended for design documents and history.
- Worktree access to the git-ignored Projects/ is only partly tested: the read-only `creator_state --root <primary> list` worked from a worktree and changed nothing (observed 2026-11-09). Every other command and any write are NOT TESTED; the serialization rule stays in force.
- Milestone-triggered user action G-FIRST-REAL-VIDEO is PENDING (see user_action_gates).
- REPORTED by Lukas (not independently verified): ChatGPT retrieved all five canonical files from main through the GitHub connector. Whether it can create branches or pull requests is not recorded.

## Next actions

1. Lukas reviews the milestone weights and confirms or changes package owners.
2. Decide Q11 (transcription engine) so M-MED-02 can start; it unblocks real-input work for Creator DNA and Creative Director.
3. Lukas decides whether to approve the milestone weights (weights_status) and who owns the UNRESOLVED packages.
4. Lukas to confirm, in ChatGPT, that the weekly "Phase 6 Readiness Check" automation can read the repository (public since 2026-11-17) and to report the result of its first completed run; until then its successful access is NOT VERIFIED.
5. Deferred from wave w01 (waves/w01-acquisition-providers-review/DEFERRED.md): Research providers (M-RSR-02, offline and transport-injected) to be scheduled in a future wave after PR #32 lands or is withdrawn, and the Creator review workflow (M-DNA-03) after PR #34 lands or is withdrawn; Q30 and Q39 (review workflow shape, how the creator is identified) remain decisions for Lukas.
6. Lukas: decide whether Resolve renders may be written into CreatorStudio\Projects (bridge output allowlist; currently only the Movies folder). Needed before long-form renders inside project folders.

## Where things live

- Rules and procedures: `PROJECT_OPERATING_PROTOCOL.md`
- Status and evidence: `PROJECT_STATUS.json` (authoritative for milestone status)
- Tasks, leases, ownership: `ACTIVE_TASKS.json`
- Design and decisions: `AI_Creator_Studio_Documentation/` (MASTER, ROADMAP, DECISIONS_AND_OPEN_QUESTIONS)
- Environment and tools: `AI_Creator_Studio_Documentation/ENVIRONMENT.md`, `TOOLS.md`
