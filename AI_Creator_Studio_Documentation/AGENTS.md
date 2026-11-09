# Agent Roles and Handoffs

**Status:** PROPOSED architecture. Names and responsibilities are design recommendations; these agents have not all been implemented.

## 1. Orchestrator

**Mission:** Coordinate the production job from intake to delivery.

Responsibilities:
- Maintain project/job state and task graph.
- Dispatch tasks to specialist agents and deterministic tools.
- Enforce dependencies, timeouts, retries and budgets.
- Persist decisions, evidence, versions and logs.
- Detect failed or contradictory handoffs.
- Ensure the correct project/timeline/media context is active.

It should not make every creative decision itself.

## 2. Research Agent

**Mission:** Find useful, reliable context and creative opportunities.

Responsibilities:
- Topic and trend discovery.
- Recent event and scientific claim verification.
- Credible source collection.
- Analysis of successful formats and references.
- Adaptation ideas for Croatian audiences where appropriate.
- Source dates, links, evidence quality and uncertainty.

Must distinguish reporting, evidence, speculation and personal opinion.

## 3. Creative Director

**Mission:** Shape the story from the creator’s actual material.

Responsibilities:
- Identify central idea, audience promise and narrative arc.
- Select hooks, setups, payoffs and section order.
- Find the strongest moments in the transcript/footage.
- Suggest filming pickups or missing context.
- Create long-form and short-form concepts.
- Ensure that structure does not distort the creator’s meaning.

Must not invent dialogue or attribute AI-written lines to the creator.

## 4. Editor Agent

**Mission:** Convert the story plan into a detailed, executable edit.

Responsibilities:
- Choose source ranges and transitions.
- Decide pacing, pauses, cuts, reaction holds and visual changes.
- Specify B-roll, graphics, captions, sound design and reframing.
- Preserve comedic timing and authentic delivery.
- Produce a structured edit plan with reasons and source references.
- Adapt editing style to content type and platform.

The Editor reasons about intent; deterministic code validates and executes.

## 5. Asset Agent

**Mission:** Find, generate and organize supporting assets.

Responsibilities:
- Search licensed/public-domain/user-owned material.
- Generate graphics, diagrams and illustrations where appropriate.
- Find sound effects/music within permitted rights.
- Validate source, license and restrictions.
- Store assets in the project asset library and write manifest metadata.

Must reject unknown-license assets and watermarked previews.

## 6. Resolve Executor

**Mission:** Perform validated editing operations in DaVinci Resolve.

Responsibilities:
- Translate validated edit-plan instructions into Resolve API/MCP calls.
- Create timelines, append clips, configure transforms, tracks, markers and exports.
- Save checkpoints and inspect results.
- Report actual API outcomes and errors.

This component should be deterministic where possible. It must not claim success merely because a tool call was attempted.

## 7. Quality Control Agent

**Mission:** Inspect the result and request specific corrections.

Responsibilities:
- Review rendered frames, audio, transcript and timeline metadata.
- Detect technical defects and likely editorial problems.
- Check long-form/short-form requirements.
- Produce actionable issue reports with timestamps and severity.
- Verify fixes after rerender.

QC should distinguish objective defects from subjective preferences.

## 8. Shared handoff contract

Every agent task should include:
- `project_id`
- `task_id`
- `input_refs`
- `goal`
- `constraints`
- `output_schema`
- `evidence_refs`
- `status`
- `warnings`
- `confidence`
- `created_at`

Every handoff should state what was actually done, what remains uncertain and where the output is stored.

## 9. Shared memory boundaries

- Project memory: facts about this particular video and its media.
- Creator DNA: stable or context-dependent creator traits.
- Editing profiles: proven editing approaches by content type/platform.
- Research knowledge: source-backed factual context and reference analyses.
- Operational memory: tool capabilities, Resolve version behavior, errors and workarounds.

Do not merge these stores indiscriminately. A one-off joke should not become a permanent Creator DNA rule; a project-specific fact should not be treated as general truth.

## 10. Suggested orchestration pattern

Use a central task graph, not an uncontrolled swarm. Agents can work in parallel when their inputs are independent (for example, research and transcript analysis). Editing cannot be finalized until the story plan and asset requirements are sufficiently stable. Resolve mutations should be serialized because the in-app bridge is single-threaded.
