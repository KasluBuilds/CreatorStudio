# Claude Code resume brief — Asset Agent

Start fresh. Do not assume previous session memory.

## Read first

Run `git fetch origin`. Read `CLAUDE.md`, `PROJECT_OPERATING_PROTOCOL.md`, `SESSION_HANDOFF.md`, `PROJECT_STATUS.json`, `ACTIVE_TASKS.json`, `AI_Creator_Studio_Documentation/ASSETS.md`, and `handoffs/asset-agent/STATUS_REPORT.md`.

## Initial task: read-only rights and interface audit

Inspect `orchestrator/assets/` and `orchestrator/tests/test_assets.py`. Map metadata, rights evaluation, fail-closed decisions, library storage, provider interfaces and all relevant negative cases. Define the asset-request contract expected from Research and the asset needs coming from Creative Director.

Do not add providers, downloaders, licence automation or external calls during this audit. Confirm that this session is the intended asset stream; the package-owner identity is marked assumed/unconfirmed.

Wait for a registered task and lease before editing. Never weaken fail-closed rights behavior.
