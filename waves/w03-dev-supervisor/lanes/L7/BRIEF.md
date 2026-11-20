# Lane L7 of wave w03-dev-supervisor: Orbit observability: supervisor snapshot and dashboard panel (issue #44 part F)

Owner: `claude:dev-orbit`  |  Task id: `T-DEV-007`  |  Branch: `lane/w03-dev-supervisor/L7`  |  Depends on lanes: L1, L2

## You may edit only
- `orchestrator/dev_supervisor/snapshot.py`
- `orchestrator/tests/test_dev_snapshot.py`
- `dashboard/desktop/`
- `dashboard/build_snapshot.py`
- `dashboard/index.html`
- `waves/w03-dev-supervisor/lanes/L7/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- snapshot.py builds one read-only JSON for Orbit from the L1 telemetry and the L2 registry (Main, workers, tasks, model/provider, context pressure with labels, account usage only when OBSERVED, branch/PR, last durable GitHub checkpoint, blockers, failures, retries, next actions); each value says whether it is live or cached and its age; stale and offline are first-class states
- The existing dashboard (read-only) gains an Operation/Supervisor view that renders that snapshot (from a local file path or setting, never a network write); a missing or stale file shows an explicit offline/stale state, never an empty success view; the UI distinguishes live from cached data; the product identity is shown as Orbit in the window title, headings of the NEW view and the About text, while released artifact names, file names and existing ids are NOT renamed; record the naming decision in the lane report
- The dashboard stays read-only: no code path writes to GitHub, the repository or the supervisor state; add a test asserting this where the existing test style allows
- Existing dashboard node tests (dashboard/desktop/tests) and Python snapshot tests still pass and are not weakened; new tests cover live, stale and offline rendering with fixtures
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_snapshot.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L7/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
