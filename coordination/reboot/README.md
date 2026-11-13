# Five-workstream reboot kit

Prepared from the five read-only Claude status reports dated 2026-11-13. This kit is a coordination artifact; it does not claim that local Claude sessions or ChatGPT project settings have been changed.

## Source of truth

- GitHub `main` is the canonical shared project state.
- `PROJECT_STATUS.json` is authoritative for milestone implementation and verification.
- `ACTIVE_TASKS.json` is authoritative for package ownership, task claims and leases.
- `DEVELOPMENT_TRACKER.json` is authoritative for step-level task progress.
- `SESSION_HANDOFF.md` is generated; do not edit it manually.
- The five reports under `handoffs/` are historical handoff evidence, not current task claims.

## Workstreams

Each folder contains:
- `CHATGPT_INSTRUCTIONS.md`: paste into the corresponding ChatGPT Project instructions.
- `CLAUDE_RESUME.md`: paste into the matching Claude Code session after its context is reset and its local repository state is reconciled.

Workstreams: transcription, creator-dna, research, assets, creative-director.

## Critical governance constraints

1. No implementation starts until a task is registered, its owned paths are clear, and the lease is claimed under `PROJECT_OPERATING_PROTOCOL.md`.
2. The four named package owners in `ACTIVE_TASKS.json` were marked assumed/unconfirmed in the reports. Confirm session identity before relying on those assignments.
3. `creator_state`, `media_analysis`, `speaker_attribution`, and `cross_package_tests` remain UNRESOLVED. No agent may edit them without a written integration agreement approved by Lukas or an explicit owner assignment from him.
4. Transcription's local experiments are machine-local and git-ignored. Preserve and back them up before any local cleanup or workspace migration. Do not rerun processing on real footage without fresh explicit permission.
5. `MVI_0879.MOV` is a friend's narration and must not be used as Creator DNA evidence.
6. The prior real-file shot run on `MVI_0890.MOV` produced suspicious false dissolve classifications. Do not tune thresholds based on that single clip or treat its output as accurate.
7. Q11 (transcription engine) is unresolved. Do not select an engine or upload audio to a hosted service without Lukas's decision and consent.
8. The user has delegated routine technical acceptance, task PR opening/merging and lease closeout to ChatGPT. Claude agents stop at READY_FOR_REVIEW and never self-approve, merge or release leases.

## ChatGPT Projects and GitHub access

ChatGPT Project instructions cannot be changed by this repository kit. The user must paste each `CHATGPT_INSTRUCTIONS.md` into the matching ChatGPT Project. Each ChatGPT workstream should use the connected GitHub repository tools to inspect current `main`; do not rely on old local reports as live status.

## Skill/tool decisions

- `portable-skills/lean-engineering/SKILL.md` combines Ponytail + Caveman.
- `portable-skills/github-sync/SKILL.md` is a separate reusable GitHub workflow skill.
- RTK is a separate CLI; see `portable-skills/RTK_SETUP.md`.
- claude-mem remains disabled; GitHub is the shared memory source.

## Current verified baseline

The five reports were each published as one commit on their own handoff branch, based on `81f38e03a675bfb0c2d53563a1f6176a9b360b73`. Each branch differs from that base only by adding its own `STATUS_REPORT.md`. None of those reports is merged into `main` yet.

This reboot kit is intended to be merged before agents resume, so all sessions can consume the same canonical instructions.
