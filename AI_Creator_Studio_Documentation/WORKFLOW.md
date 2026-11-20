# End-to-End Production Workflow

**Status:** PROPOSED target workflow. The Resolve connection is verified, but the autonomous pipeline is not yet implemented.

**Scope.** This workflow is the video-production pipeline. How the repository itself is developed (Main, workers, supervisor, GitHub log) is a separate system: see `DEVELOPMENT_OPERATING_SYSTEM.md`. Development agents do not run these stages on real footage; stage state is recorded by `creator_state`, never by the development supervisor.

## 1. Intake and ingest

1. Creator imports camera footage, phone footage, screen recordings, audio, images and project notes.
2. System creates a project record and copies or references media without altering originals.
3. Record file paths, durations, frame rates, dimensions, audio channels, timestamps and checksums where practical.
4. Generate proxies or analysis copies when useful.
5. Transcribe speech and align words/phrases to source timecodes.
6. Detect shots, scene changes, faces/subjects and audio events where useful.
7. Flag corrupt, missing or variable-frame-rate media for special handling.

**Output:** media inventory, transcript, shot/scene index and ingest report.

> **Implementation status (2026-11-08):** steps 1-4 and 7 are implemented for the inventory part only (hybrid ingestion, `INGESTION.md`): files are catalogued in place with path, duration, frame rate, codecs, checksums and warnings (including variable-frame-rate flags); proxies are available as an explicit operation. Tested on synthetic media only.
>
> Steps 4-6 have a **package-level foundation** in `orchestrator/media_analysis/` (see `INGESTION.md` “Media analysis (Phase 2)”): audio analysis copies (step 4), a transcript format with a replaceable provider interface and a human review layer (step 5), and an FFmpeg shot-boundary detector plus an energy-based audio analyser (step 6). **No transcription engine exists**, so step 5 cannot run on real speech; shot detection is tested only on a synthetic cut; nothing has run on real footage; the `media_analysis` package has no CLI commands.
>
> Step 6 also has a separate, CLI-driven shot/transition/candidate detector in `orchestrator/creator_state/shot_*.py` (`shots-plan`, `shots-detect`, `shots-status`, `shots-check`; see `SHOT_DETECTION.md`). It is verified on synthetic clips only and **not validated on real footage**; its confidence values are heuristic; variable-frame-rate sources are refused. How it relates to `media_analysis`'s boundary detector is open (Q35).

## 2. Understand the material

- Identify topic, subject, intended audience and likely format.
- Identify the strongest moments, demonstrations, explanations, jokes, reactions and narrative turns.
- Detect repeated explanations, false starts, dead air and technical issues, but do not automatically remove meaningful pauses.
- Compare the material with the creator’s learned Creator DNA.
- Identify gaps that may require a pickup shot, voiceover or external visual. Never fabricate spoken dialogue and attribute it to the creator.

**Output:** content map and evidence-linked moment index.

> **Implementation status (2026-11-08):** only the *evidence layer* of this stage exists. `media_analysis.pipeline` runs inside the `understand` stage (journaled operations; originals verified first) and produces audio measurements, shot boundaries, transcripts (when a provider exists), human transcript reviews, an **evidence-linked moment index** (`analysis/media/moments.json`, factual moment types plus separately attributed interpretations) and a Creator DNA handoff manifest. The content map, topic/audience identification, strongest-moment selection, comparison with Creator DNA and pickup-gap detection are **not implemented**. The pipeline deliberately does **not** complete `understand` (transcription is unavailable and a completed stage cannot be restarted without `reopen_stage`); when it may be completed is an open decision (`DECISIONS_AND_OPEN_QUESTIONS.md` Q33). Creator DNA learning is not triggered by this stage: the handoff is prepared, and ingesting it is an explicit call; its place in the stage sequence is open (Q32). Meaningful pauses are only *measured*, never removed or labelled. Before any of this can teach the system about the creator, a **speaker-attribution and eligibility step** decides, per record, whether it is evidence about the creator at all (sequence: media analysis → speaker attribution → eligibility decision → human review where required → Creator DNA handoff; `INGESTION.md`, `CREATOR_DNA.md` §8a). It is implemented as a library step with human-supplied attribution only (no provider, no CLI), tested on synthetic data; without attribution evidence nothing is eligible.

## 3. Research

Research Agent can:
- Find relevant current events, discoveries, background information and credible evidence.
- Investigate successful formats or English-language examples that can be adapted.
- Identify claims that need verification and distinguish facts from opinion.
- Suggest context, visual explanations and possible story angles.

Research should enrich the creator’s actual material, not force a trend onto unrelated footage.

**Output:** research brief with sources, dates, confidence and adaptation notes.

## 4. Creative plan

Creative Director proposes:
- Core promise and audience takeaway.
- Opening hook based on actual footage or a truthful framing.
- Narrative order, sections and transitions.
- Setup/payoff opportunities.
- Where context is missing.
- Potential long-form structure.
- Candidate short-form moments and their independent hooks/payoffs.
- Suggested filming pickups, if needed.

The plan is a guide for editing, not a full replacement script.

**Output:** story brief and structured edit plan.

## 5. Asset acquisition

Asset Agent receives specific visual/audio needs from the edit plan. It searches, creates or generates assets and records:
- Asset identity and local path.
- Source URL/provider and retrieval date.
- License/rights basis and restrictions.
- Attribution requirements.
- Intended use and time range.
- Whether it is generated, user-owned or externally sourced.

Unknown rights are rejected or quarantined. No watermark is not proof of permission.

**Output:** asset package and manifest.

## 6. Edit-plan validation

Before Resolve is changed, deterministic validation checks:
- Source media exists and is online.
- Source in/out points are valid and within media bounds.
- Frame rate and timebase conversions are explicit.
- Timeline tracks and overlaps are coherent.
- Audio/video references are valid.
- Assets are rights-cleared.
- All operations are supported by the tested Resolve API/MCP capability set.
- Destructive operations are avoided or have a backup/rollback path.

**Output:** validated, versioned JSON edit plan.

## 7. Resolve assembly

Resolve Executor:
1. Opens or creates the intended project/timeline.
2. Preserves the original timeline and source media.
3. Builds the edit from source segments and track instructions.
4. Applies tested transforms, titles, graphics, audio treatment and effects.
5. Saves checkpoints and logs each operation.
6. Verifies the resulting timeline against the edit plan.

Prefer constructing a new timeline from source segments rather than relying on undocumented mutation of existing timeline items. The initial trim test showed a frame-count discrepancy that must be resolved before precise automated trimming is trusted.

## 8. Review and iteration

1. Render a low-resolution review copy.
2. Extract representative frames and audio samples/transcripts.
3. QC checks continuity, black frames, audio, captions, framing, pacing, story logic and platform safety.
4. Creative/editor agents identify concrete defects and propose targeted changes.
5. Apply only changes tied to a specific issue.
6. Re-render and re-check.
7. Stop when quality criteria are met or a bounded retry limit is reached.

The system should not endlessly optimize tiny subjective details. Record unresolved trade-offs.

## 9. Long-form delivery

- Export the main video using an appropriate YouTube preset or validated custom settings.
- Verify output exists, duration, dimensions, frame rate, codec, audio stream and playback.
- Save project, edit plan, QC report, asset manifest and export metadata.

## 10. Short-form derivation

Generate multiple short-form candidates from the long-form/source material:
- Each has its own hook and payoff.
- Provide enough context to stand alone.
- Use vertical reframing and subject-aware crop.
- Reposition captions/graphics within platform-safe areas.
- Avoid arbitrary shortening that destroys timing or meaning.
- Include promotional cutdowns selectively.

Export separate platform variants as needed. Validate each export independently.

## 11. Completion and learning

- Archive project state and final deliverables.
- Record which edits were accepted, rejected or revised.
- Update Creator DNA only from reliable repeated evidence, not one-off behavior.
- Update editing profiles based on outcomes and explicit feedback.
- Keep source media unchanged and preserve rollback points.

## Autonomy and escalation

The target is no routine approval gate. The system should still stop or quarantine work when:
- Media is missing or corrupt.
- Rights are unclear.
- A tool reports unsupported behavior.
- A destructive action has no recovery path.
- A factual claim is uncertain and materially important.
- The render fails validation repeatedly.

These are safety/reliability boundaries, not routine creative approval dialogs.
