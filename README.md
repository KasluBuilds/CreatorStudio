# AI Creator Studio

A design-first, work-in-progress project to build a mostly autonomous **video-production system** (long-form videos plus shorts) on top of **DaVinci Resolve Free**. It is meant to turn a creator's real footage into edited videos **without writing replacement dialogue**: the creator's own words, humour and personality stay the source material.

> **Status: early development. Nothing here is a finished product.** Most of the system is still design. Only the offline building blocks listed below exist as code, and they have been exercised on **synthetic data only**. The end-to-end pipeline (assembly in Resolve, quality control, export) is not built yet.

## Honest progress

Progress is tracked deterministically in [`PROJECT_STATUS.json`](PROJECT_STATUS.json). At the last recorded update:

| Measure | Value |
|---|---|
| Implementation | ~35 % of weighted milestones |
| Verification | ~13 % (nothing is "verified" without real-input evidence) |

Status labels used across the docs: **VERIFIED** (tested), **DECIDED** (owner-approved), **PROPOSED** (design only), **OPEN** (needs a decision), **NOT TESTED**.

## What exists in code (`orchestrator/`)

Standard-library Python, offline, no network access, tested on synthetic media:

| Package | Purpose |
|---|---|
| `creator_state` | Persistent project state, checkpoints, handoff, recovery and media ingestion CLI |
| `media_analysis` | Analysis foundations: measurements, moment index (no transcription engine yet) |
| `speaker_attribution` | Speaker attribution contracts (library only) |
| `creator_dna` | Evidence store and confidence model for learning the creator's authentic style |
| `research` | Offline research agent foundation (fixture providers only) |
| `assets` | Offline asset and licence/rights checking foundation |
| `creative_director` | Offline creative planning foundation |
| `governance` | Tooling that validates the project status and task registry |

Run the tests (Python 3, no third-party dependencies):

```powershell
cd orchestrator
python -B -I -m unittest discover -s tests
```

## Documentation

Start with [`AI_Creator_Studio_Documentation/MASTER.md`](AI_Creator_Studio_Documentation/MASTER.md) (goals, non-negotiables, architecture), then the [document map](AI_Creator_Studio_Documentation/README.md) for workflow, agents, editing engine, Resolve integration, quality control and the roadmap.

## Principles

- The creator's real words and personality are the source material; the system never invents dialogue.
- Original media is immutable: it is catalogued in place and never modified, moved or deleted.
- Resolve operations need a validated plan and a recovery path.
- Rights and provenance are tracked for every asset; unknown rights are not permission.
- Nothing is called working until it is verified.

## How the project is run

Several AI sessions (Claude Code and ChatGPT) work in parallel under a written protocol: see [`PROJECT_OPERATING_PROTOCOL.md`](PROJECT_OPERATING_PROTOCOL.md), [`CLAUDE.md`](CLAUDE.md), [`ACTIVE_TASKS.json`](ACTIVE_TASKS.json) and [`CHANGELOG.md`](CHANGELOG.md). The owner keeps final authority.

## What is deliberately not in this repository

Footage, project data, launcher scripts, API keys, local MCP configuration and third-party tools. See [`.gitignore`](.gitignore) and the template [`.mcp.json.example`](.mcp.json.example). Some documents mention the author's local Windows paths; they are descriptive only.

## Requirements (for the parts that run)

Windows with PowerShell, Python 3, and FFmpeg/ffprobe for ingestion and shot detection. DaVinci Resolve Free 21.0.4.5 is the integration target; compatibility with other versions is not assumed. See [`TOOLS.md`](TOOLS.md).

## Contributing and license

This is a personal project in active development and is **not accepting contributions yet**. **No license is granted**: all rights are reserved, so the code is visible but may not be reused or redistributed without permission.
