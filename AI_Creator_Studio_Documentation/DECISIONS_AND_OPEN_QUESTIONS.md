# Decisions, Verified Facts and Open Questions

This document prevents proposals and assumptions from being mistaken for confirmed project requirements or working features.

## 1. User decisions / constraints

| Item | Status | Record |
|---|---|---|
| Product vision | DECIDED | Autonomous multi-agent AI Creator Studio |
| Target editor | DECIDED | DaVinci Resolve Free 21.0.4.5 on Windows |
| Avoid Studio purchase/downgrade | DECIDED preference | Keep Free unless a specific required feature is proven unavailable |
| Output formats | DECIDED | Long-form YouTube and short-form TikTok, Instagram Reels, YouTube Shorts |
| Content niches | DECIDED | Broad: mycology, DIY, tech, humor/day-to-day, vlogs, biology, opinions/current events |
| Authenticity | NON-NEGOTIABLE | Do not replace creator’s real dialogue, jokes, spontaneous reactions or personality |
| Scripting | DECIDED | Creator does not write full scripts; AI can prepare hooks, outlines, talking points and filming guidance |
| Autonomy | DECIDED | Target fully autonomous production and final export; no routine approval dialogs |
| Short-form strategy | DECIDED | Multiple standalone cutdowns; some promotional, not all |
| Research service | DECIDED | Separate research capability |
| Asset acquisition | DECIDED | Separate asset search/creation capability |
| Rights handling | DECIDED | Known permitted rights required; unknown rights rejected |
| Editing philosophy | DECIDED | Purposeful, evidence-informed editing; do not overcut or over-stimulate |
| Documentation | DECIDED | Master document plus specialized subsystem files |
| Creator DNA source (2026-11-09, from the task brief) | DECIDED | Creator DNA is learned from the actual footage being edited. The creator is **not** asked to record special voice samples, talking clips or training material, and no separate recording session is required. |
| Speaker attribution (2026-11-09, from the task brief) | DECIDED | Unknown attribution must never silently become creator attribution; a friend speaking the same dialect, a visible person, or a narrator is not automatically (or automatically not) the creator. Uncertainty is preserved; no voice biometrics or face recognition is required. |
| Test file `MVI_0879.MOV` (2026-11-09) | DECIDED | A friend's narration in the creator's dialect. Allowed for generic transcription-engine tests; **must not** be used as Creator DNA evidence. |
| Media ingestion | DECIDED (2026-11-08) | Hybrid: originals never modified and referenced in place by absolute path; catalogue and organize; proxies/working copies only as explicit, verified, separate operations; analysis and generated files kept apart from originals; Claude Code CLI first, no dashboard yet |
| Repository and governance (2026-11-09, from Lukas's task briefs) | DECIDED | A private GitHub repository is the canonical source of truth; several Claude Code and ChatGPT workstreams run in parallel under shared governance (task branches, worktrees, explicit ownership, task leases); progress must distinguish implementation from verification and be computed from weighted milestone records, not invented |

## 2. Verified technical facts

- Resolve version: DaVinci Resolve 21.0.4.5 Free.
- In-app scripting can access Resolve object.
- Project/media pool inspection worked; 41 media pool clips observed in one project.
- Timeline inspection worked; one observed timeline was 25 fps with start frame 90000.
- Timeline creation and marker insertion worked.
- Clip append worked.
- Source range test produced a 95-frame item for a nominal 100-frame range; unresolved.
- ZoomX read/write worked.
- Minimal MP4/H.264 render completed and played.
- Resolve bridge running on `127.0.0.1:49632`.
- MCP repository installed under CreatorStudio tools.
- Claude Code `/mcp` showed `davinci-resolve` connected with 37 tools.
- Claude Docs MCP also appeared with 8 tools.

## 2a. Implemented (tested in isolation only)

- Persistent project state, checkpoints, handoff generation, logs and recovery analysis: `orchestrator/creator_state/`, documented in `PROJECT_STATE.md`. 71 unit tests pass (2026-11-08). Tested with temporary directories and one disposable demo project (`Projects/demo-state-test`, dummy media); not tested with real footage, Resolve, or an actual provider switch.
- Hybrid media ingestion (`orchestrator/creator_state/{media,ingest,proxy,managed}.py`, `INGESTION.md`, `Incoming/README.md`): scan, ffprobe inventory with exact-vs-estimated frame rate, checksums, resumable probe cache, `source_manifest.json`, report, state/checkpoint integration, source verification, explicit proxy and managed-copy operations. Tested with generated dummy files, a fake ffprobe, and real ffmpeg/ffprobe on synthetic clips. **Not tested on real footage.**
- Media analysis foundation (`orchestrator/media_analysis/`, `INGESTION.md` “Media analysis (Phase 2)”): reuse of the ingest manifest and media ids, audio analysis-copy registry with provenance and stale detection, transcript format + replaceable provider interface (no engine) + human review layer, energy-based audio measurements, FFmpeg shot-boundary detector, evidence-linked moment index with separately attributed interpretations, and a pipeline connection to the `understand` stage (journaled operations, originals verified first). 67 + 38 tests (2026-11-08). Synthetic media only; real FFmpeg exercised on one generated 4-second clip. **Not tested on real footage; no CLI.**
- Creator DNA foundation (`orchestrator/creator_dna/`, `CREATOR_DNA.md`): append-only evidence store, layers (observed / interpretation / hypothesis / self-report), derived confidence and scope, creator confirm/reject/correct, optional evidence confidence, audio metric contract v1, source provenance, schema v2. 69 tests. Synthetic data only. **No real recording analysed; no creator profile exists; no creator-facing tool.**
- Test status (2026-11-09, from `orchestrator/`, `python -I -m unittest discover -s tests -p "<file>"`): `test_media_analysis.py` 67, `test_creator_dna.py` 69, `test_integration_phase2_phase3.py` 38, `test_speaker_attribution.py` 56 — all passing. The full suite (`python -I -m unittest discover -s tests`, 579 tests) also covers the separately owned `research` package (`test_research.py`, 186 tests), which was being edited concurrently: it showed 0 failures in one run and 45 failures/errors (all in `test_research.py`) minutes later. Quote a full-suite result only together with its time and re-run it; do not assume it.
- Speaker attribution and eligibility (`orchestrator/speaker_attribution/`, `CREATOR_DNA.md` §8a, 2026-11-09): identity, delivery context, attribution evidence (human / project metadata authoritative; supporting evidence can only propose), explicit persisted eligibility decisions with reasons, human confirmation/correction/cluster mapping, revocation, and Creator DNA schema v3 whose store refuses footage evidence without an eligible-creator stamp. 56 tests. Synthetic data only; **no attribution provider exists, no CLI, nothing run on real footage.**
- Design rules derived from the *non-negotiable authenticity constraint* (not new product decisions): an engine’s transcript is never treated as verified speech; only a named human review can make text “verbatim” evidence; creator statements are never counted as footage evidence; missing confidence is never replaced by a default. Their *parameters* (weights, thresholds) are implementer defaults awaiting the creator — see Q26–Q36.
- Research Agent, Asset Agent and Creative Director foundations (`orchestrator/research/`, `orchestrator/assets/`, `orchestrator/creative_director/`): offline libraries with fixture providers only; no network, no concrete provider, not wired into the pipeline or `creator_state`; unit-tested on synthetic data (284, 146 and 132 tests collected on 2026-11-09). Recorded as M-RSR-01, M-AST-01 and M-CRD-01 in `PROJECT_STATUS.json`.
- Test status (2026-11-09, commit `5f6925e`): the full suite ran 1069 tests with 0 failures, 0 errors and 1 skipped (a symlink test) on the original project and on a clean clone. This supersedes the 579-test and 45-failure remarks above, which describe earlier moments (the 45 failures in `test_research.py` did not reproduce). Current figures are always in `PROJECT_STATUS.json` → `baseline`.
- Provider switching is **not** automatic. The state layer lets a replacement session continue from files; a human starts that session.

## 3. Proposals, not implemented facts

- Python orchestrator loop (only its state/recovery foundation exists).
- Multi-agent roles and handoff contracts.
- Structured edit-plan JSON schema.
- High-level validated Resolve wrapper functions.
- Creator DNA *learning on real footage*, humor/delivery/gesture analyzers, and use of the profile by other agents (only the evidence store and a reference phrase analyzer exist — §2a).
- Research and Asset Agent *providers, pipeline wiring and runs on real input* (offline library foundations exist, see section 2a).
- Automatic transcription (no engine), speaker identification, speech-activity detection and scene *understanding* (only measurements, a boundary detector and the interfaces exist — §2a).
- Automated review render/QC correction loop.
- Fully autonomous long-form/short-form production.
- Job dashboard and production analytics.
- Automatic resumption of jobs (resume *detection* exists; nothing acts on it automatically).

## 4. Open questions

### Resolve/MCP
1. Which of the 37 MCP tools work reliably on this exact Free Resolve installation?
2. Why did the nominal 100-frame source range produce 95 frames?
3. Can the API reliably manage multiple tracks, clip placement and exact trims?
4. Which title/caption/effect/Fusion operations are available through MCP?
5. What audio operations are reliable?
6. How should render progress and failure states be monitored?
7. Does vertical export work with correct framing and safe areas?
8. Does the OmniRoute launcher preserve MCP connectivity?
9. Should the bridge allowed-output root be expanded to `CreatorStudio`? Inspect before changing.

### Product and creative
10. What should the default long-form and short-form deliverable profiles be?
11. Which Croatian transcription engine and alignment method should be used? **OPEN.** The provider interface and review layer exist and are engine-neutral; a comparison and recommendation (not a decision) are in `TRANSCRIPTION_ENGINE_BRIEF.md`. Local facts: laptop CPU only (Ryzen 3 7320U, 13.8 GB RAM, integrated GPU, no CUDA), no ASR package installed, FFmpeg 9.0.1 includes a `whisper` filter (whisper.cpp); whether any whisper.cpp model file exists locally was **not established** (the search was cut short), and none was installed or used.
12. How should Creator DNA observations be scored and updated? **Implemented as a default, not validated:** see `CREATOR_DNA.md` §4.3 (recording-level independence, saturating support score, layer caps, derived scope). The creator has not reviewed these rules; see Q26.
13. What is the source/research budget per video?
14. What should be the default retry limits and QC thresholds?
15. Which platforms/metadata/publishing integrations are in scope later?

### State and handoff (new)
16. ~~Should a project-level `CLAUDE.md` tell every new session to read the active project's `HANDOFF.md`?~~ Done 2026-11-08 (root `CLAUDE.md`). Still OPEN: how reliably Claude Pro and OmniRoute models actually follow it.
17. Which stage-specific output validators should gate completion first (media probe, render check, edit-plan schema)?
18. `backup.ps1` contains a plaintext OmniRoute auth token. Consider moving it to an environment variable or local secret store (not changed here; the file was out of scope). If the file was ever shared or synced, consider rotating the token.
19. The roadmap's earlier "Phase 1" (safe Resolve operations) and the user's "Phase 1" (state foundation) collided; the state work is recorded as a named block. Renumber if preferred.

### Ingestion (new)
20. Run ingestion on a real sample (user permission needed): how long do checksums/probes take on the real files, and are the reports useful?
21. Is the default checksum limit (4 GiB, `--hash auto`) right, and should hashing be optional for very large camera files?
22. How should variable-frame-rate phone/screen recordings be handled before editing (conform to constant rate, proxy, or flag only)?
23. Are the default proxy settings (H.264/AAC, 540 px, CRF 23) what DaVinci Resolve Free handles well? Not yet tested in Resolve.
24. Where should long-term originals live (Incoming/ vs. dedicated footage drive)? `verify-sources` detects moved files but does not relocate them.
25. `ffprobe -version` shows a full build is installed at `C:\Users\Korisnik\AppData\Local\Microsoft\WinGet\Links` (FFmpeg 9.0.1 gyan.dev); this is an installation fact on this machine, not guaranteed elsewhere.

### Media analysis and Creator DNA (new)

These are product-level choices the implementation had to make a *default* for. None is decided by the creator.

26. **Scoring constants, including the `ASR_UNVERIFIED` weight of 0.6.** Chosen by the implementer to sit between verified/measured evidence (1.0) and AI estimates (0.5); not derived from data. Other defaults: saturation k = 2, same-recording extra weight 0.3, 3 recordings and consistency 0.8 for `stable`, creator-confirmation floor 0.95, layer caps. *Concrete task:* after the first permitted sample has been transcribed *and* human-reviewed, measure how often ASR text matches the reviewed text and set the weight from that; then ask the creator whether the resulting behaviour feels right.
27. **Kajkavian (and other variety) tagging.** Now: a BCP-47-style `language` (e.g. `hr`, `en`) plus a free-text `variety` (e.g. `kajkavian`) that a provider may report only if it declares it supports it. There is no standard language code for Kajkavian and no known engine that reports it. Open: the tag vocabulary (a private-use subtag such as `hr-x-kajkavian` vs a separate field), and whether the variety is set by the engine, by the human reviewer, or both.
28. **Transcript review at scale.** The review layer supports partial review (any subset of segments; unreviewed segments stay `unverified` and, by default, never reach Creator DNA). Reviewing every segment of every video is unrealistic. Open: a policy (e.g. review only segments that feed DNA quotes or edit cuts; sampled review; confidence-gated review) and what “enough reviewed” means before DNA learning from a recording is allowed.
29. **Absolute source paths in DNA exports.** Now included by default (`DnaEligibility.include_source_paths=True`) so evidence can be traced to the exact file; they expose local folder names and can be switched off. Open: which default is right if profiles are ever shared or synced.
30. **Creator-facing review and correction.** Transcript review (`review.py`) and trait confirm/reject/correct (`DNAStore`) are library calls only; no CLI or UI exists. Open: the interface (CLI command, generated review sheet, in-chat flow) and who is the reviewer in practice.
31. **Where the Creator DNA profile lives.** The store takes any directory; `AI/CreatorDNA/` from earlier planning is *not* implemented. Open: one profile per creator in `AI/CreatorDNA/` vs per project; backup/versioning of the event log.
32. **Where DNA learning belongs in the stage sequence.** The handoff is prepared during `understand` but ingestion is an explicit call. Open: whether learning is its own step after `understand`, a part of `completion`, or something the creator triggers; and which analyzers are allowed to run.
33. **When `understand` may be marked complete.** It is not completed automatically (no transcription engine; a completed stage needs `reopen_stage` to restart). Open: the completion criteria (e.g. transcript present and reviewed enough, moment index built, content map written) and who decides.
34. **Interpretation vocabulary and review.** Moment-index interpretations (“joke”, “reaction”, …) use a free vocabulary, an author (`ai:`/`human:`) and a rationale; there is no controlled vocabulary or approval workflow.
35. **Two shot-detection efforts: relationship and contract.** **OPEN; nothing decided.** Both exist and neither calls or reads the other. `media_analysis.scene_detection` (`FfmpegSceneDetector`: FFmpeg scene-score boundaries, Python API only, tested on one synthetic cut; see `INGESTION.md`) and `creator_state/shot_*.py` (CLI `shots-plan`, `shots-detect`, `shots-status`, `shots-check`; frame-exact, constant-frame-rate only, classifies cuts, transitions, rejected and uncertain events; outputs under `analysis/shots/`; verified on synthetic clips only; see `SHOT_DETECTION.md`). Open: (a) which one should be canonical for shot boundaries; (b) whether one should eventually call or reuse the other; (c) whether they should instead stay separate because they serve different purposes; (d) if they are integrated, what contract is needed, e.g. how shots, transitions and uncertain candidates map to moment-index evidence and its factual types, time versus frame units, variable-frame-rate handling, and what `confidence` means. Shot-boundary detection (a visual change) is distinct from higher-level scene understanding (story or topic segments); neither effort implements the latter.
36. **Audio analysis validity.** The `auto` threshold, window/gap defaults and the metric contract v1 are tested on synthetic tones only; whether they behave on real recordings (noise, music, breathing) is unknown. Also untested: whether analysis copies keep the original timeline exactly on real containers with start-time offsets. *Concrete task:* check both on one permitted real recording; any change to the metric contract needs a version bump in `creator_dna/metrics.py`.

### Speaker attribution and eligibility (new, 2026-11-09)

Implementation defaults awaiting the creator; none is a product decision.

37. **Attribution numbers.** Human decision confidence 0.95, project-metadata 0.85, supporting evidence capped at 0.70, minimum 0.80 for `eligible`, coverage tolerance 0.25 s, pause probe 0.2 s. *Concrete task:* after a first permitted real sample, check whether the decisions they produce match the creator's own judgement of which moments are theirs.
38. **Unknown delivery needs review.** A confirmed creator span with `delivery=unknown` is `requires_review` (it could be playback). This is safe but adds a review burden. Options: record delivery once per clip; derive it from the folder/metadata convention (e.g. `camera/` = on camera) only if the creator approves that convention.
39. **How the creator establishes "this is me" without special samples.** Today: a person confirms a span, maps a provider's cluster in a recording, or records project metadata. Open: the convention (per clip, per project), the interface (CLI command, review sheet, in-chat flow), and whether cross-clip continuity may ever help (now it can only propose). No creator-facing tool exists.
40. **Overlapping speech and isolation.** The unit is the transcript segment. A mixed segment is ineligible unless a human asserts the creator's contribution is `isolated`. Open: whether word-level attribution (needs word timestamps and a diarisation provider) is worth building, and what counts as isolated.
41. **Pauses and energy as creator behaviour.** A pause is the creator's delivery only between creator-attributed speech on both sides; edge pauses and pauses next to another speaker are ineligible. Open: whether a pause before the creator's reaction to someone else counts.
42. **Visual attribution.** No provider; a visible person is not the speaker. Shot boundaries are never person behaviour. Open: how "the creator is on screen" is tagged at scale, and whether framing/camera-presence evidence needs its own rules.
43. **Strictness and compatibility.** The Creator DNA store now refuses unattributed footage evidence and has no bypass flag (a test-only fixture stamps synthetic attributions). Consumers had to add stamps (the Creative Director test did). Older v2 logs load, but their footage evidence is never counted. Open: whether any migration or explicit legacy path should exist (none is planned).
44. **Third-party identifiers.** `other_identified` uses a project-local `person_id` (no real name needed) and is stored in the project. Open: retention and sharing rules for such ids, and whether a friend's attribution data should be minimised or purged.

## 5. Operational facts

Project root:
`C:\Users\Korisnik\CreatorStudio`

MCP repo:
`C:\Users\Korisnik\CreatorStudio\tools\davinci-resolve-mcp`

Bridge config:
`C:\Users\Korisnik\.config\davinci-resolve-mcp\bridge.json`

Bridge address:
`127.0.0.1:49632`

OmniRoute:
`http://localhost:20128`

## 6. Change log

### 2026-11-09
- GitHub migration and governance: created the private repository `KasluBuilds/CreatorStudio` (initial commit `51fcd85`; `5f6925e` made `test_claude_md.py` tolerate the git-ignored launchers, `.mcp.json` and `tools/`, and added a check of `.mcp.json.example`). Added `PROJECT_OPERATING_PROTOCOL.md`, `PROJECT_STATUS.json`, `ACTIVE_TASKS.json`, a generated `SESSION_HANDOFF.md`, `CHANGELOG.md`, `ENVIRONMENT.md` and the `orchestrator/governance/` package on branch `task/governance-foundation` (pending review). Corrected stale status text only where evidence supports it: the read-only MCP test in `ROADMAP.md`, and the Research/Asset/Creative Director status. No design decision was changed; milestone weights are proposals awaiting Lukas.
- Added speaker attribution and Creator DNA eligibility (`orchestrator/speaker_attribution/`, Creator DNA schema v3, pipeline steps `evaluate_eligibility` and an attributed `prepare_dna_handoff`). Docs updated (`CREATOR_DNA.md` §8a, `INGESTION.md`, `WORKFLOW.md`, `ROADMAP.md`); open choices recorded as Q37–Q44. Synthetic data only; no provider, no CLI; `MVI_0879.MOV` is excluded from Creator DNA evidence.
- Documentation consistency cleanup: Q35 rewritten in place (same number, still OPEN) to reflect that the `creator_state` shot detector is now documented and has CLI commands. No question was renumbered and no decision was made.

### 2026-11-08
- Consolidated AI Creator Studio vision and architecture.
- Confirmed Resolve Free 21.0.4.5 core scripting/render tests.
- Installed in-app bridge and MCP server.
- Registered MCP in CreatorStudio root.
- Claude Code connected to `davinci-resolve` with 37 tools.
- Created initial documentation pack.
- Read-only MCP connection test succeeded (project `MCP_COMPATIBILITY_TEST`, timeline `MCP_TRIM_TEST`, 24 fps as reported by the setting, 3 Master-folder items of which 1 is source media).
- Built persistent state foundation (`orchestrator/creator_state/`, `PROJECT_STATE.md`); 62 tests pass. Provider switching remains manual.
- Built hybrid media ingestion (step 3): `Incoming/` convention, inventory manifest, ffprobe integration with graceful fallback, proxy and managed-copy operations, 9 new CLI commands, `INGESTION.md`, 63 new tests. Synthetic media only.
- Built the media-analysis foundation (`orchestrator/media_analysis/`) and the Creator DNA evidence store (`orchestrator/creator_dna/`), then hardened their shared evidence contract (unverified ASR vs reviewed speech, optional visual confidence, creator self-report vs footage, audio metric contract v1, source provenance) and connected analysis to the `understand` stage at package level. Documentation updated to match (Phase 2.2); open choices recorded as Q26–Q36. Synthetic data only; no CLI; no transcription engine; nothing run on real footage.
- Added root `CLAUDE.md` (none existed): documentation reading order starting with `MASTER.md`, status labels, hard rules, resume procedure, state CLI usage. Added `test_claude_md.py` (71 tests total, all pass). Resuming is still manual and instruction-driven, not automatic. Launchers, `.mcp.json` and `tools/` untouched.
