# Implementation Roadmap

**Status date:** 2026-11-08 (Phases 2 and 3 updated after the media-analysis / Creator DNA integration). This roadmap is a recommended sequence, not a claim that unlisted work has been completed.

**Status authority (2026-11-09):** current milestone status, evidence and progress percentages are recorded in `PROJECT_STATUS.json` (milestone ids `M-XXX-NN`, indexed at the end of this file). The checkboxes in this file are narrative and can lag it; if they disagree, the status file wins and the disagreement should be corrected here.

## Phase 0 — Foundation and compatibility

<badge>Mostly complete</badge>

- [x] Establish `CreatorStudio` root and initial folder structure.
- [x] Clone and install `samuelgursky/davinci-resolve-mcp`.
- [x] Install Resolve in-app bridge.
- [x] Verify Resolve Free 21.0.4.5 scripting access.
- [x] Test timeline creation, marker, clip append, transform and MP4 export.
- [x] Register root `.mcp.json`.
- [x] Launch Claude Code with project trust.
- [x] Confirm `/mcp` lists `davinci-resolve` with 37 tools.
- [x] Verify one harmless read-only MCP tool call end-to-end. (Recorded as succeeding in the 2026-11-08 change log of `DECISIONS_AND_OPEN_QUESTIONS.md`; M-RSV-01. Reported, not re-run.)
- [ ] Test OmniRoute fallback with MCP.
- [ ] Inspect bridge output-root allowlist before exporting to `CreatorStudio\Projects`.

## State foundation — persistent project state and checkpoints

<badge>Implemented, unit-tested 2026-11-08; not yet used on a real production</badge>

Requested by the user as "Phase 1" on 2026-11-08. The numbered phases below were written earlier, so this block is labeled by name to avoid renumbering them. Details: `PROJECT_STATE.md`.

- [x] Versioned per-project state file (`project_state.json`, schema v1).
- [x] Append-only, hash-checked checkpoints after completed stages.
- [x] Output verification before any stage can be marked complete.
- [x] Auto-generated `HANDOFF.md` containing recorded facts only (no hidden reasoning).
- [x] Human-readable and machine-readable activity logs with secret redaction.
- [x] Recovery analysis, resume-point detection and inconsistent-state detection.
- [x] Operation journal that prevents repeating completed destructive operations.
- [x] Atomic state writes with lock file.
- [x] CLI and 71 automated tests.
- [ ] Exercise on a real project and a real Claude Pro → OmniRoute session switch.
- [ ] Stage-specific output validators (media probe, render check, edit-plan schema).
- [ ] Orchestrator loop that records state automatically (today an agent/user must call it).
- [x] Root `CLAUDE.md` with reading order, status labels, safety rules and the resume procedure (instruction only; a session must follow it, nothing runs automatically). Consistency test added.
- [ ] Observe a real new session (Pro and OmniRoute) following `CLAUDE.md`; tighten the wording where a model skips steps.

## Phase 1 — Safe Resolve operations

- [ ] Build a small disposable test project and test MCP read operations.
- [ ] Validate exact source in/out frame semantics; explain the observed 95/100-frame result.
- [ ] Test multiple video/audio tracks and synchronization.
- [ ] Test transforms, crop, positioning and rotation.
- [ ] Test captions/titles and text styling.
- [ ] Test audio gain, fades, music and effects.
- [ ] Test render progress/status, failure handling and export verification.
- [ ] Test 9:16 vertical export.
- [ ] Document supported operations and known limitations.

## Phase 2 — Media ingest and analysis

Package-level foundation implemented 2026-11-08 in `orchestrator/media_analysis/` (details: `INGESTION.md` “Media analysis (Phase 2)”). Everything below was tested on **synthetic** data only; no real footage has been analysed. The only CLI commands in this phase are the shot-detection ones (below).

- [x] Create project manifest and media inventory (`source_manifest.json`, hybrid ingestion: originals referenced in place; `INGESTION.md`). Tested on synthetic media only.
- [~] Add proxy/analysis copy workflow. (Video proxies: explicit `proxy-make`/`managed-copy`, validated only on tiny synthetic clips. Audio analysis copies: registry with provenance, reuse and stale detection implemented in `media_analysis.proxies`, synthetic only.)
- [ ] Run ingestion on a real sample of the user's footage (with permission) and review the report.
- [~] Decide variable-frame-rate handling and image metadata fallback without ffprobe. (VFR is *detected and flagged* and analysis records use seconds, not frame numbers; the handling decision — conform, proxy or flag only — is still open, Q22.)
- [~] Add transcription with time alignment. (Segment/word timestamp format, replaceable provider interface, unavailable/fixture providers and the human review layer exist. **No transcription engine is installed or chosen** — Q11; a decision brief is in this phase’s report. Not usable on real speech yet.)
- [~] Add shot/scene detection. Two separate implementations exist and their relationship is undecided (Q35): (a) `media_analysis.scene_detection`: interface plus an FFmpeg scene-score boundary detector, tested on one synthetic hard cut; (b) `creator_state/shot_*.py` (details: `SHOT_DETECTION.md`):
  - [x] Shot-detection core (FFmpeg metrics parsing, candidate grouping, rule-based classification into cuts, transitions, rejected and uncertain events).
  - [x] Orchestration (preflight, dry-run plan, detection, verification, atomic publish, recovery of interrupted runs).
  - [x] CLI integration (`shots-plan`, `shots-detect`, `shots-status`, `shots-check`).
  - [x] Synthetic validation passed (141 tests on generated clips: cuts, short shots, flash, dissolve, fades, static footage, interruptions, malformed output, collisions).
  - [ ] **Real-footage validation pending** (needs the user's permission): thresholds are untuned and confidence is heuristic, not calibrated.
  - [ ] Constant-frame-rate only (variable-frame-rate sources are refused); only the first video stream is analysed; 1-2 frame shots stay uncertain.
- [~] Add speech and silence analysis. (Energy-based levels, quiet gaps and energy rises exist; **speech** detection needs a voice-activity engine that does not exist.)
- [x] Store evidence-linked moment index (`moments.json`; factual moment types, attributed interpretations kept separate). Synthetic only.
- [x] Connect analysis to the project state at package level (`media_analysis.pipeline`: originals verified first, journaled operations under the `understand` stage, outputs in `analysis/media/`). The stage is intentionally **not** auto-completed (Q33).
- [~] Add CLI commands for the analysis steps. (Only shot detection has them: `shots-plan/detect/status/check`. The `media_analysis` steps — audio analysis, transcription, moment index — still have none.)

## Phase 3 — Creator DNA

Implemented 2026-11-08 in `orchestrator/creator_dna/` (details: `CREATOR_DNA.md`). Synthetic data only; **no real recording has been analysed and no profile of the creator exists.**

- [x] Define evidence-backed profile schema (schema v2: layers, evidence methods, provenance, self-report).
- [ ] Analyze several representative recordings.
- [ ] Extract vocabulary, humor, delivery and visual-presence patterns. (Only a reference phrase analyzer exists; no humor/delivery/gesture analyzers.)
- [x] Store examples, confidence and counterexamples.
- [x] Add incremental learning and correction mechanism (library level: append-only log, idempotent ingest, confirm/reject/correct/reopen). 
- [x] Evidence contract with Phase 2: unverified ASR restricted, human-reviewed transcripts, optional visual confidence, audio metric contract, source provenance.
- [x] Speaker attribution and Creator DNA eligibility (Phase 2.3, `orchestrator/speaker_attribution/`, schema v3): identity, delivery context, attribution evidence, explicit eligibility decisions with reasons, human confirmation/correction, and a Creator DNA store that refuses footage evidence without an eligible-creator attribution. Library level, synthetic data only; **no attribution provider exists** (unknown stays unknown), no CLI, nothing run on real footage.
- [ ] Creator-facing way to review transcripts, confirm speakers and confirm/correct traits (Q30, Q39).
- [ ] Decide where DNA learning sits in the stage sequence and where the profile lives (Q31, Q32).
- [ ] Decide the attribution defaults and policies (Q37–Q43), and how the creator says "this is me" without recording special samples (Q39).

## Before Phase 4 (stabilization checklist)

- [ ] Decide the transcription engine (Q11) and test it on a short, permitted sample.
- [ ] Decide the open scoring/policy questions (Q26–Q29) and the `understand` completion rule (Q33).
- [ ] Reconcile `media_analysis.scene_detection` with `creator_state/shot_*.py` (Q35).
- [ ] Run Phase 2 → 3 end to end on one real, permitted recording **that contains the creator** (not `MVI_0879.MOV`, a friend's narration) and review the result, including the attribution decisions.

## Phase 4 — Research and asset services

Offline library foundations exist and are not wired into the pipeline, with no live providers: `orchestrator/research/` (M-RSR-01) and `orchestrator/assets/` (M-AST-01). The items below describe the full capability and stay open (M-RSR-02, M-AST-02).

- [ ] Implement Research Agent and source-quality rules.
- [ ] Add topic/trend discovery and research briefs.
- [ ] Implement Asset Agent.
- [ ] Add rights/provenance manifest and automated rejection rules.
- [ ] Create asset library indexing.

## Phase 5 — Creative planning and edit representation

`orchestrator/creative_director/` (M-CRD-01) is an offline story-brief foundation. It is not the Editor Agent and not the shared edit-plan schema (M-EDT-01).

- [ ] Define validated edit-plan JSON schema.
- [ ] Build Creative Director workflow.
- [ ] Build Editor planning workflow.
- [ ] Add source-range/frame validation.
- [ ] Add track/operation capability validation.
- [ ] Build high-level Resolve wrapper functions.

## Phase 6 — Long-form autonomous editing

- [ ] Build new timeline from validated plan.
- [ ] Add purposeful B-roll, graphics and captions.
- [ ] Add audio processing workflow.
- [ ] Add review render and QC feedback loop.
- [ ] Run on a real but backed-up project.
- [ ] Verify final export automatically.

## Phase 7 — Short-form repurposing

- [ ] Identify independent short-form moments.
- [ ] Create hook/context/payoff structure per cutdown.
- [ ] Add vertical reframing and safe-area checks.
- [ ] Export TikTok/Reels/Shorts variants.
- [ ] Verify every short stands alone.
- [ ] Add selective long-form promotional variants.

## Phase 8 — Production hardening

- [~] Add crash recovery and resumable jobs. (State, checkpoints and resume detection exist — see "State foundation"; automatic resumption by an orchestrator does not.)
- [~] Add versioned project backups and rollback. (State checkpoints exist; rollback of Resolve projects and media is not built.)
- [ ] Add job dashboard/status reporting.
- [ ] Add performance and cost measurement.
- [ ] Add regression tests for Resolve version changes.
- [ ] Run multiple complete productions and refine from results.

## Milestone index (details, weights and evidence: `PROJECT_STATUS.json`)

| Roadmap section | Milestones |
|---|---|
| Governance (added 2026-11-09) | M-INF-01, M-INF-02 |
| Phase 0 | M-RSV-01 |
| State foundation | M-STA-01 |
| Phase 1 | M-RSV-02 |
| Phase 2 | M-ING-01, M-MED-01, M-MED-02, M-SHT-01 |
| Phase 3 | M-DNA-01, M-SPK-01, M-DNA-02, M-DNA-03 |
| Before Phase 4 | M-INT-01, M-INT-02, M-INT-03 |
| Phase 4 | M-RSR-01, M-RSR-02, M-AST-01, M-AST-02 |
| Phase 5 | M-CRD-01, M-CRD-02, M-EDT-01, M-EDT-02 |
| Phase 6 | M-LFE-01, M-LFE-02 |
| Phase 7 | M-SFE-01 |
| Phase 8 | M-HRD-01 |

## Priority rule

Do not build sophisticated autonomous editing before the underlying Resolve operations, frame timing, project safety and export validation are reliable. A small, trustworthy end-to-end pipeline is more valuable than many untested agents.
