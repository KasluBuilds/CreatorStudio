# Lane L3 of wave w03-dev-supervisor: Safe handoff, compaction and session replacement (issue #44 part C)

Owner: `claude:dev-handoff`  |  Task id: `T-DEV-003`  |  Branch: `lane/w03-dev-supervisor/L3`  |  Depends on lanes: L1, L2

## You may edit only
- `orchestrator/dev_supervisor/handoff.py`
- `orchestrator/dev_supervisor/compaction.py`
- `orchestrator/dev_supervisor/replacement.py`
- `orchestrator/tests/test_dev_handoff.py`
- `waves/w03-dev-supervisor/lanes/L3/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- handoff.py: a handoff builder that captures objective, decisions, current plan, task graph, worker identities and status, branches/worktrees, latest commits and PRs, tests and evidence with labels, blockers, next actions and resource state (context and usage signals with their labels); written as machine-readable JSON plus a human Markdown file in the machine-local state store, with a content hash; a VERIFY step re-reads the files, validates the schema, checks that every referenced branch/worktree/commit/PR exists through injected checkers, and returns NOT_SAFE with reasons when anything is missing; contains no secrets or transcript text
- compaction.py: a policy that, given context pressure from the L1 telemetry (merge origin/wave/w03-dev-supervisor to use it) and configurable thresholds, decides NONE / WARN / COMPACT / REPLACE with a recorded reason; compaction is only requested when the session is idle and a handoff has just been verified; never claims exact usage when the signal is UNAVAILABLE; a compaction event is recorded through the state store event log
- replacement.py: the replace-a-session procedure: verify handoff, mark the old session retiring, launch a fresh Main or worker through the L2 launcher protocol with a prompt that points to the verified handoff, wait for the new session's first heartbeat, then retire the old; if the handoff is not verified the old session is never cleared; the module and its result state that a fresh session does NOT replenish account usage; bounded retries and rollback (the old session stays authoritative until the new one is confirmed)
- At least 40 offline tests (missing branch, corrupt handoff, hash mismatch, threshold edges, retiring and rollback, never-clear-without-verified-handoff, repeated failure stops)
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_handoff.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L3/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
