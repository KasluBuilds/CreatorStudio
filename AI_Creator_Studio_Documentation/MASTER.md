# AI Creator Studio — Master Project Specification

**Document role:** Central source of truth for the project’s purpose, non-negotiable constraints, system architecture and subsystem documentation.

**Project root:** `C:\Users\Korisnik\CreatorStudio`  
**Resolve target:** DaVinci Resolve Free 21.0.4.5 on Windows  
**Status date:** 2026-11-08

---

## 1. Executive vision

AI Creator Studio is intended to become a highly autonomous, multi-agent video-production system that turns the creator’s real-world footage and recordings into polished long-form YouTube videos and multiple short-form videos for TikTok, Instagram Reels and YouTube Shorts.

It should behave like a small creative production studio: researching ideas, understanding the creator’s footage and personality, planning edits, selecting or creating supporting assets, editing in DaVinci Resolve, reviewing its own output, correcting problems and exporting finished deliverables.

The system should automate the production work without manufacturing a fake version of the creator.

## 2. Non-negotiable creative constraints

1. **Preserve the creator’s authenticity.** The creator’s real spoken dialogue, spontaneous jokes, reactions, delivery, opinions and personality are the source material. The AI may organize, select, tighten and support this material, but must not silently replace it with newly written dialogue or rewrite the creator’s words.
2. **No full-script requirement.** The creator does not want to write complete scripts. AI can prepare hooks, payoff ideas, outlines, talking points, filming prompts and structure suggestions. These are guidance, not replacement dialogue.
3. **Autonomy is the target.** The intended system should make routine creative and technical decisions and complete final exports without routine approval dialogs. Safety, licensing, destructive-operation and platform constraints still apply.
4. **Editing must have purpose.** Do not cut or add stimulation merely to make the video busy. Preserve pauses, timing, comprehension and meaningful reactions when they serve the content.
5. **Repurpose intelligently.** Long-form videos should yield multiple independently understandable short-form pieces. Some shorts may promote the full video, but not every short should be an advertisement.
6. **Evidence-informed craft.** Editing decisions should be informed by professional examples and credible research/education on attention, retention, comprehension, pacing, cognitive load, humor and visual communication. Treat evidence as guidance, not simplistic universal rules.
7. **Protect originals.** Source media must remain untouched. Work non-destructively in project/timeline copies and retain recovery paths.
8. **Respect rights.** Asset acquisition must track source and license. Unknown rights are not equivalent to permission.

## 3. Content scope

The creator’s content interests are deliberately broad and may include:
- Mycology and mushrooms
- DIY and practical projects
- Technology
- Biology and science
- Vlogs and day-to-day experiences
- Humor and spontaneous moments
- Personal opinions and worldviews
- Commentary on recent events

The system should not hard-code itself to one niche. It should identify the subject and adapt research, structure, visuals and tone to each video.

## 4. Deliverables

### Long-form
- YouTube-ready main video.
- Appropriate title/thumbnail/description support may be added as a later capability.
- Source-preserving edit and project organization.

### Short-form
- TikTok, Instagram Reels and YouTube Shorts variants.
- Multiple cutdowns when the source supports them.
- Each short should have its own hook, context and payoff; avoid extracting fragments that only make sense when the full video is watched.
- Some promotional cutdowns may direct viewers to the long-form video.

### Project artifacts
- Edit decision plan / structured timeline representation.
- Asset manifest and rights/provenance metadata.
- QC report and render metadata.
- Logs, backups and version history.

## 5. High-level architecture

The recommended design is a central Python orchestrator coordinating specialist agents and deterministic tools.

```text
Creator / Footage
       |
       v
Ingest & Media Understanding
       |
       +--> Creator DNA learning
       |
       +--> Research & topic context
       |
       v
Creative Director / Story Plan
       |
       v
Structured Edit Plan (validated JSON)
       |
       +--> Asset research / creation
       |
       v
Resolve Editing Executor
       |
       v
Review Render + Automated QC
       |
       +---- feedback / revision loop ----+
       |                                   |
       +-----------------------------------+
       |
       v
Long-form Export + Short-form Cutdowns
       |
       v
Final QC, delivery and project archive
```

This is a target architecture, not a statement that all boxes are implemented.

### Main components
- **Orchestrator:** Owns job state, task sequencing, retries, budgets and handoffs.
- **Research Agent:** Finds relevant topics, trends, evidence and reference formats.
- **Creative Director:** Converts source material and goals into a story/edit plan.
- **Editor Agent:** Reasons about cuts, pacing, sound, visuals and platform framing.
- **Asset Agent:** Finds or creates supporting assets and validates their rights.
- **Resolve Executor:** Performs deterministic editing operations through the Resolve MCP/API.
- **QC Agent:** Reviews low-resolution renders and project metadata, then requests targeted corrections.
- **Creator DNA subsystem:** Learns the creator’s real style from footage and feedback.

See `AGENTS.md`, `WORKFLOW.md` and `TECHNICAL_ARCHITECTURE.md`.

## 6. Editing intelligence

The system should reason about:
- Story structure, context, stakes, progression and payoff.
- Hooks that arise from the actual footage and topic.
- Dead air versus meaningful pauses.
- Speech clarity, filler, repeated material and continuity.
- Humor setup, timing, reaction and release.
- Visual variety, purposeful B-roll, graphics and demonstrations.
- Audio intelligibility, music balance and sound effects.
- Long-form rhythm and short-form standalone structure.
- Platform-specific aspect ratios, framing, duration and safe areas.

It must not equate “fast editing” with “good editing.” See `EDITING_ENGINE.md`.

## 7. Creator DNA

Creator DNA is learned primarily from actual recorded material, not just a questionnaire. It may include:
- Vocabulary, recurring expressions and language habits.
- Humor style, timing, irony and spontaneity.
- Speech rhythm, pauses, emphasis and energy.
- Facial expression, gestures and camera presence.
- Preferred degree of informality and visual style.
- Topics, opinions and recurring narrative patterns.

The system should store observations with evidence and confidence, separate stable traits from context-dependent behavior, and update its understanding as more footage is analyzed. It must preserve the creator’s actual words. See `CREATOR_DNA.md`.

## 8. Research and asset acquisition

Research should support both original topic discovery and format adaptation. It can investigate current trends, recent events, scientific discoveries, credible educational sources and successful English-language formats that may be adapted for a Croatian-speaking audience.

Asset acquisition should find, generate or create useful visuals, diagrams, B-roll, sounds, music, memes and graphics. Every external asset must have source and rights metadata. Do not download or reuse material simply because it is publicly viewable. See `RESEARCH.md` and `ASSETS.md`.

## 9. DaVinci Resolve integration — verified baseline

Target installation: **DaVinci Resolve Free 21.0.4.5** on Windows.

Verified in this project:
- Resolve’s in-app Python scripting can access the Resolve object and identify the product/version.
- Project and media-pool inspection worked.
- Timeline creation and marker insertion worked.
- Appending a media clip to a timeline worked.
- A source-range append/trim test worked, although the observed duration was 95 frames for a nominal 100-frame range; frame-range semantics need further validation.
- `ZoomX` could be read, changed to 1.5 and read back.
- A minimal MP4/H.264 export could be configured, rendered and played successfully.
- The local in-app bridge is installed and running at `127.0.0.1:49632`.
- Claude Code displays `davinci-resolve` as connected with **37 tools** available.

Important limits:
- The 37 tools have not all been tested.
- Advanced effects, audio automation, captions, complex track operations, speed changes, multicam, Fusion and full render automation have not yet been validated end-to-end.
- Resolve Free 21.0.4.5 is the tested target. Do not infer compatibility with other Resolve versions. The repository source notes that Free 21.1 may no longer expose Python scripts in the Scripts menu; this is not a test of the user’s installation.

See `RESOLVE_INTEGRATION.md`.

## 10. Current technical environment

- Windows, VS Code integrated PowerShell.
- Main project root: `C:\Users\Korisnik\CreatorStudio`.
- MCP repository: `C:\Users\Korisnik\CreatorStudio\tools\davinci-resolve-mcp`.
- Claude Code CLI, authenticated through the user’s Claude Pro account.
- Local OmniRoute gateway: `http://localhost:20128`.
- Free Gemini and Groq provider keys are routed through OmniRoute.
- The user uses two launcher profiles: `pro.ps1` (direct Anthropic login) and `backup.ps1` (OmniRoute fallback).
- The MCP server uses a Python virtual environment under the MCP repository and is configured in the CreatorStudio root `.mcp.json`.
- Resolve bridge listens on loopback only. Keep it local; do not expose it to the network.

Never ask the user to paste API keys, auth tokens or private SSH keys into chat.

## 11. Current project tree

```text
CreatorStudio/
├── .mcp.json
├── .claudeignore
├── pro.ps1
├── backup.ps1
├── AI/
│   ├── Agents/
│   ├── CreatorDNA/
│   ├── EditingProfiles/
│   └── Knowledge/
├── Assets/
│   ├── Images/
│   ├── Videos/
│   ├── SoundEffects/
│   ├── Music/
│   ├── Graphics/
│   └── Memes/
├── Incoming/                # drop new footage here, one folder per video project (see Incoming/README.md)
├── CLAUDE.md                # per-session instructions: reading order, safety rules, resume procedure
├── orchestrator/            # creator_state (state, ingest; PROJECT_STATE.md, INGESTION.md), media_analysis (INGESTION.md), creator_dna (CREATOR_DNA.md); other packages from separate work streams may be present
├── Projects/
├── Research/
└── tools/
    └── davinci-resolve-mcp/
```

Added 2026-11-09 and not shown in the tree: repository-root governance files (`PROJECT_OPERATING_PROTOCOL.md`, `PROJECT_STATUS.json`, `ACTIVE_TASKS.json`, `SESSION_HANDOFF.md`, `CHANGELOG.md`, `TOOLS.md`, `.mcp.json.example`) and the `orchestrator/` packages research, assets, creative_director, speaker_attribution and governance.

The repository also has its own `.mcp.json`. The root-level `.mcp.json` is the one intended to be discovered when Claude Code is launched from `CreatorStudio`.

## 12. Operating principles for Claude Code

When working in this project:
1. Read this file first.
2. Read only the subsystem documents relevant to the current task.
3. Distinguish verified facts, decisions, proposals and open questions.
4. Do not modify originals or perform destructive edits without a recovery plan.
5. Do not claim a feature works until it has been tested in the actual Resolve installation.
6. Prefer small, verifiable steps and explain PowerShell commands simply.
7. Keep the user’s two-launcher setup. Do not create a third launcher unless explicitly requested.
8. Do not force `DAVINCI_RESOLVE_BRIDGE=1` by default. The MCP repository source says this forces bridge-only transport and disables fallback; the server is designed to fall back automatically when the direct transport is unavailable.
9. Do not change `pro.ps1`, `backup.ps1`, `.mcp.json`, bridge configuration or MCP installation without a clear reason and an explanation.
10. Do not start editing real footage during connection/compatibility tests. Use disposable test projects and media.
11. Development-time agents (Main, workers, the Python supervisor, Orbit) are reusable development infrastructure, separate from the video-production agents in AGENTS.md. Their rules are in PROJECT_OPERATING_PROTOCOL.md section 19 and their guide is DEVELOPMENT_OPERATING_SYSTEM.md (added by wave w03-dev-supervisor, issue #44); implementation status is recorded there and in PROJECT_STATUS.json, and until that wave is merged treat it as PROPOSED.

## 13. Documentation authority

- `MASTER.md`: overall goals, constraints and architecture.
- Each subsystem file: detailed specification for that subsystem.
- `DECISIONS_AND_OPEN_QUESTIONS.md`: record of what is confirmed, decided, proposed and unresolved.
- `ROADMAP.md`: implementation order and completion narrative.
- `PROJECT_STATUS.json` (repository root): current milestone status, evidence and progress; authoritative for status. Rules for parallel work: `PROJECT_OPERATING_PROTOCOL.md`.
- `ACTIVE_TASKS.json` (repository root): task claims, leases and package ownership.

If documents conflict, preserve the more specific verified fact, flag the conflict and update the documents. Never silently overwrite a user decision.
