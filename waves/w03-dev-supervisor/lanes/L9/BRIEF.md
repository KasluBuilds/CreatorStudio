# Lane L9 of wave w03-dev-supervisor: Development operating system documentation and doc reconciliation (issue #44 part G)

Owner: `claude:dev-docs`  |  Task id: `T-DEV-009`  |  Branch: `lane/w03-dev-supervisor/L9`  |  Depends on lanes: L1, L2, L3, L4, L5, L6, L7, L8

## You may edit only
- `DEVELOPMENT_OPERATING_SYSTEM.md`
- `AI_Creator_Studio_Documentation/AGENTS.md`
- `AI_Creator_Studio_Documentation/WORKFLOW.md`
- `AI_Creator_Studio_Documentation/ENVIRONMENT.md`
- `orchestrator/tests/test_dev_docs.py`
- `waves/w03-dev-supervisor/lanes/L9/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- DEVELOPMENT_OPERATING_SYSTEM.md (project-independent, placeholders instead of machine values): how Main controls the operation; how specialist agents communicate (Claude Code messaging, GitHub as the durable log); the Python supervisor, session/context rotation and recovery; GitHub task/step/event logging, branches, worktrees, PRs and integration; Orbit and what it observes; Claude Pro versus OmniRoute (free Gemini/Groq, no paid API assumed) with quota realities and the statement that a fresh session does not replenish usage; Windows/VS Code setup and machine-local configuration; how to onboard a new repository; how Main proposes ideas, evaluates feasibility and trade-offs and asks essential questions before a major build; and the distinction between reusable development infrastructure and Creator Studio's video-production agents
- Every capability in the document is labelled with its real status (IMPLEMENTED, TESTED, OBSERVED, REPORTED, PROPOSED, NOT TESTED) taken from the lane reports and the merged code on the wave branch, with nothing promoted beyond its evidence
- AGENTS.md, WORKFLOW.md and ENVIRONMENT.md are reconciled (no duplicated or conflicting rules): development-time agents are clearly separated from video-production agents; MASTER.md, ROADMAP.md, DECISIONS_AND_OPEN_QUESTIONS.md, README.md, TOOLS.md, CLAUDE.md, PROJECT_OPERATING_PROTOCOL.md, RESOLVE_INTEGRATION.md and TECHNICAL_ARCHITECTURE.md are NOT edited by this lane (shared files, the integrator and T-RSV-002 own them); list the proposed edits to those files in the lane report instead
- test_dev_docs.py checks that DEVELOPMENT_OPERATING_SYSTEM.md contains every required section, a status label on each capability, no secret patterns, no absolute user paths and no launcher contents; the existing docs tests still pass
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_docs.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L9/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
