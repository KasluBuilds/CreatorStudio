# Launch sheet: wave w01-acquisition-providers-review

Three Claude Code agents work at the same time, each in its own worktree and branch. Nobody approves interim steps. The
Repository Coordinator integrates the whole wave in **one** pull request when all three lanes are `DONE`. Procedure and rules:
`WAVE_PROTOCOL.md`. Each lane's full assignment is its `BRIEF.md`.

| Lane | Task | Stream (ChatGPT project chat) | Delivers | Worktree (under `CreatorStudio-worktrees`) |
|---|---|---|---|---|
| L1 | T-AST-003 | asset-agent (`claude:asset-recovery`) | Asset acquisition pipeline, rights-evidence handling, local-folder and open-catalogue providers (M-AST-02, offline) | `w01-acquisition-providers-review-L1` |
| L2 | T-RSR-001 | research (`claude:research-stream`) | Four concrete research providers, cache, budgets, circuit breaker, pipeline stage adapter (M-RSR-02, offline) | `w01-acquisition-providers-review-L2` |
| L3 | T-DNA-002 | creator-dna (`claude:creator-dna-stream`) | Creator review queue, decision API, offline review sheet, import and CLI (M-DNA-03) | `w01-acquisition-providers-review-L3` |

Not in this wave: **Creative Director** (T-CRD-001 is active on its own branch and is never touched here) and
**transcription** (blocked on the engine decision, Q11). Packages with no owner (`creator_state`, `media_analysis`,
`speaker_attribution`, cross-package tests) are not touched by any lane.

## What each lane does, and does not do

Does: everything in its brief, autonomously, in its own worktree; commits explicit paths; writes
`waves/w01-acquisition-providers-review/lanes/<lane>/report.json`; runs `cd orchestrator && python -B -m governance wave submit`;
reports the `SUBMITTED` line.

Does not: ask for approval between steps, edit a shared registry (`ACTIVE_TASKS.json`, tracker, changelog, handoff,
`PROJECT_STATUS.json`), edit the manifest, touch another lane's paths, open a pull request, merge, or push to `main`. Synthetic data only: no
network, no API keys, no real footage or recordings, no Resolve. Nothing may be recorded as VERIFIED.

## Start prompt for the Claude Code session (replace the three lane values)

Open the lane's worktree folder, start Claude Code there with your usual launcher, and paste:

```
You are the Claude Code agent for lane <LANE> of wave w01-acquisition-providers-review (task <TASK>, owner <OWNER>).
Your working directory is your lane worktree; stay in it. Read, in order: CLAUDE.md, WAVE_PROTOCOL.md (sections 2, 4, 5 and 12 are
the ones that apply to you), your assignment waves/w01-acquisition-providers-review/lanes/<LANE>/BRIEF.md, then
AI_Creator_Studio_Documentation/MASTER.md and the subsystem document your brief names.
Do the whole lane autonomously and completely: implement every acceptance criterion, write the tests, update the document, run the
test modules the brief lists, then write your report.json (status DONE only if every criterion is met and the tests pass) and run
`cd orchestrator && python -B -m governance wave submit`. If submit refuses, fix what it names and rerun. If you hit a decision that
is not yours, record it under "unresolved" in report.json, do everything that is not blocked, and carry on. Never ask me to approve
a step. When submit prints SUBMITTED, reply with that line and a three-line summary, then stop.
```

| Lane | `<LANE>` | `<TASK>` | `<OWNER>` |
|---|---|---|---|
| L1 | L1 | T-AST-003 | claude:asset-recovery |
| L2 | L2 | T-RSR-001 | claude:research-stream |
| L3 | L3 | T-DNA-002 | claude:creator-dna-stream |

## Message for each ChatGPT project chat

```
Assignment for your stream in wave w01-acquisition-providers-review, lane <LANE>. Your Claude Code session works in the lane
worktree named in the launch sheet. Give it the start prompt above and the lane's BRIEF.md (waves/w01-acquisition-providers-review/lanes/<LANE>/BRIEF.md
on branch wave/w01-acquisition-providers-review). Do not approve or gate interim steps: the lane runs to completion and delivers
itself with `wave submit`. The Repository Coordinator verifies and integrates the whole wave; you may review the single wave pull
request when it opens. If the lane reports an `unresolved` decision, bring it to Lukas.
```

## While the wave runs

- Anyone can see progress: `cd orchestrator && python -B -m governance wave status` (after `git fetch origin`). A lane with no push for 48 hours
  shows STALE.
- `wave submit` may be run more than once; partial progress can be submitted with status `IN_PROGRESS`. Only `DONE` lanes count as finished.
- If `wave submit` says the wave branch moved, it merges again by itself and retries. If it reports a conflict, resolve it only inside the lane's own paths.

## When all three lanes are DONE (the Coordinator, no action from Lukas)

`wave verify --require-complete`, `wave close --approver repository_coordinator`, one pull request `wave/w01-acquisition-providers-review` into `main`,
the `validate` check, then merge with a merge commit if every gate in `WAVE_PROTOCOL.md` section 12 passes. Lukas is told the result and every
`unresolved` item from the three reports.
