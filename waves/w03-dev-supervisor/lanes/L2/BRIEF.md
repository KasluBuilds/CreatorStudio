# Lane L2 of wave w03-dev-supervisor: Supervisor state store and worker lifecycle: spawn, assign, monitor, restart, stop (issue #44 part B)

Owner: `claude:dev-lifecycle`  |  Task id: `T-DEV-002`  |  Branch: `lane/w03-dev-supervisor/L2`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/state_store.py`
- `orchestrator/dev_supervisor/lifecycle.py`
- `orchestrator/dev_supervisor/workers.py`
- `orchestrator/tests/test_dev_lifecycle.py`
- `waves/w03-dev-supervisor/lanes/L2/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- state_store.py: a crash-safe machine-local store (atomic write, schema_version, fsync and rename, backup of the previous file, corruption detected and recovered from the backup, append-only JSON-lines event log) in a configurable directory that defaults OUTSIDE the repository; refuses a directory inside the repo working tree; no secrets ever written (scrub on write)
- workers.py: a Worker record (id, role, task_id, branch, worktree, owner stream, provider/model identity, status, pid, session id, started and last-heartbeat, restart count, last result reference) and a registry that survives process restarts
- lifecycle.py: spawn, assign, monitor, collect, restart and stop through an injected ProcessLauncher protocol (a fake launcher in tests; a real launcher that runs the claude CLI in print/stream-json mode with an explicit --model, --session-id and a task prompt file, argument lists only, no shell string interpolation, bounded runtime); heartbeat and stale detection; bounded restarts with backoff and a hard stop after N identical failures recorded as a blocker; graceful stop; duplicate-worker prevention for the same task/worktree; the worktree and branch must exist (injected checker) before a worker is assigned
- Result collection: a worker result is stored as REPORTED until an injected verifier marks it VERIFIED or REJECTED; contradictory reports from two workers on one task are detected and flagged
- Recovery: a fresh supervisor process reloads the registry, reconciles with live pids, marks vanished workers LOST and produces a recovery plan without relaunching anything automatically
- At least 50 offline tests (fake launcher, fake clock, corruption, crash mid-write, restart storms, duplicate assignment, contradiction detection)
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_lifecycle.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L2/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
