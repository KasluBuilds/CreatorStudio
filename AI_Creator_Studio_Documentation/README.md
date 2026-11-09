# AI Creator Studio — Documentation Pack

This folder contains the project’s central specification and focused subsystem documents.

> Claude Code sessions started in `CreatorStudio` load the root-level `CLAUDE.md`, which sets the reading order and the procedure for resuming a video project. It is not part of this folder; keep it consistent with these documents (`orchestrator/tests/test_claude_md.py` checks its file, command and stage references).

## How to use these files

1. Put the complete `AI_Creator_Studio_Documentation` folder somewhere inside the `CreatorStudio` project root, or copy its Markdown files into a `docs/` folder there.
2. Add the folder to the ChatGPT Project **video-editor** as project knowledge/files.
3. When working with Claude Code, ask Claude to read `MASTER.md` first, then only the relevant subsystem documents.
4. Update the relevant document whenever an architectural decision or implementation changes.

> Project governance lives at the repository root, outside this folder: `PROJECT_OPERATING_PROTOCOL.md` (rules for parallel work), `PROJECT_STATUS.json` (authoritative milestone status and progress), `ACTIVE_TASKS.json` (tasks, leases, package ownership), `SESSION_HANDOFF.md` (generated) and `CHANGELOG.md`.

## Document map

| File | Purpose |
|---|---|
| `MASTER.md` | Authoritative overview, goals, constraints, architecture and links |
| `WORKFLOW.md` | End-to-end production workflow |
| `AGENTS.md` | Agent roles, boundaries and handoffs |
| `EDITING_ENGINE.md` | Editing principles, storytelling, pacing and edit-plan model |
| `CREATOR_DNA.md` | Learning and preserving the creator’s authentic identity |
| `RESEARCH.md` | Topic, trend, format and evidence research |
| `ASSETS.md` | Asset discovery, creation, licensing and provenance |
| `RESOLVE_INTEGRATION.md` | DaVinci Resolve Free compatibility and MCP integration |
| `TECHNICAL_ARCHITECTURE.md` | Runtime, orchestration, data model, storage and reliability |
| `PROJECT_STATE.md` | Persistent project state, checkpoints, handoff and recovery (implemented) |
| `INGESTION.md` | Hybrid media ingestion (`Incoming/`, inventory, proxies) and media analysis (analysis copies, transcripts + review, audio/shot measurements, moment index, pipeline) — implemented, synthetic-tested |
| `SHOT_DETECTION.md` | Shot, transition and candidate detection with CLI (`shots-plan/detect/status/check`) — implemented, verified on synthetic clips only |
| `ENVIRONMENT.md` | Development environment, Python/test invocation, launcher roles, OmniRoute, Resolve/MCP and Git/GitHub setup (facts labelled VERIFIED/REPORTED; no secrets) |
| `FIRST_REAL_VIDEO_CHECKLIST.md` | PROPOSED guidance delivered to Lukas when the Phase 6 user-action gate fires: what to film, how to ingest it safely, how validation is judged |
| `TRANSCRIPTION_ENGINE_BRIEF.md` | Comparison of transcription approaches for decision Q11 (proposal only; no engine chosen) |
| `QUALITY_CONTROL.md` | Automated review, validation and export checks |
| `ROADMAP.md` | Phased implementation plan and current status |
| `DECISIONS_AND_OPEN_QUESTIONS.md` | Decision log, verified facts, uncertainties and questions |

## Status language

- **VERIFIED** — directly tested or explicitly confirmed in the project conversation.
- **DECIDED** — a user-approved design constraint or direction.
- **PROPOSED** — a recommended design that has not yet been implemented or explicitly approved in detail.
- **OPEN** — requires a future decision, experiment or implementation.
- **NOT TESTED** — do not assume it works merely because the API or tool exists.

These labels matter. Do not turn a proposal into a claim that a feature already exists.

## Project status at the time this pack was created

- Resolve Free 21.0.4.5: basic scripting, timeline creation, marker insertion, clip append, a source trim test, transform setting and automated MP4 export have been tested.
- The in-app Resolve bridge is running on loopback at `127.0.0.1:49632`.
- Claude Code reports the `davinci-resolve` project MCP as connected with 37 tools available.
- The 37 MCP tools have **not** all been individually tested.
- The two launcher scripts exist in the `CreatorStudio` root. The OmniRoute fallback launcher has not yet been end-to-end tested.
- The autonomous multi-agent editing system itself is a design goal, not a completed implementation.

Last consolidated: 2026-11-08.
