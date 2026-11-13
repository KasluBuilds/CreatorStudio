# Claude Code resume brief — Research Agent

Start fresh. Do not assume previous session memory.

## Read first

Run `git fetch origin`. Read `CLAUDE.md`, `PROJECT_OPERATING_PROTOCOL.md`, `SESSION_HANDOFF.md`, `PROJECT_STATUS.json`, `ACTIVE_TASKS.json`, `AI_Creator_Studio_Documentation/RESEARCH.md`, and `handoffs/research/STATUS_REPORT.md`.

## Initial task: read-only interface audit

Inspect `orchestrator/research/` and its tests. Map:
- research request and result models;
- source provenance and attestation;
- provider protocol and fixture provider behavior;
- validation, scoring, risk and output packs;
- downstream asset-request and creative-director inputs.

Prepare a short integration contract and a deterministic test plan. Do not add live providers or make network calls yet. Confirm the package-owner identity with ChatGPT; ACTIVE_TASKS marks it assumed/unconfirmed.

Wait for a registered task and lease before editing. Keep all provider credentials out of the repository.
