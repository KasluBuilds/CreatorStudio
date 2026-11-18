# Lane L2 of wave w05-orbit-operation: Orbit: operation, queued work, idle reason, worker context, supervisor health and handoff views

Owner: `claude:dev-orbit`  |  Task id: `T-DEV-016`  |  Branch: `lane/w05-orbit-operation/L2`  |  Depends on lanes: L1

## You may edit only
- `dashboard/desktop/`
- `orchestrator/tests/test_dev_orbit_view.py`
- `waves/w05-orbit-operation/lanes/L2/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w05-orbit-operation/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- Orbit renders the new snapshot sections (Snapshot additions (top-level keys, every value an evidence dict {label, value, timestamp, source, freshness} or a list of plain records): 'operation': {'actionable': [{id, kind, priority, summary, inspect}], 'others_waiting': [...same], 'blocked': [{id, kind, summary}], 'idle_reason': evidence, 'warnings': [str], 'fingerprint': str}; 'supervisor_health': {'lock_alive': evidence, 'last_tick_at': evidence, 'last_event_kind': evidence}; 'handoff': {'latest_id': evidence, 'verified': evidence, 'created_at': evidence}; each worker record gains 'context_pressure': {'tokens','window','ratio','level'} evidences (UNAVAILABLE with reason when its transcript is not found).): 'Next actions for Main' (actionable, ordered), 'Waiting on others' (informational), 'Blocked', an explicit idle reason line, warnings, per-worker context pressure, supervisor health and handoff state; a missing section renders as unavailable with a reason, never as an empty success
- A worker that is only registered (status ASSIGNED) is never shown as active; a blocked task is never shown as running
- Every value keeps its data-quality badge (live/cached/stale/estimated/unavailable); Orbit stays read-only; no renames of released artifacts
- Node tests with fixtures for each section and the missing-section case; test_dev_orbit_view.py extended to render the Python-built fixture from L1 after merging origin/wave/w05-orbit-operation
- Synthetic data only: no real footage, no network in unit tests, no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any live observation is summarised as numbers and identifiers only, labelled OBSERVED with the exact command; anything not observed is NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, observed, proposed and open items
- Do NOT edit cli.py, launch.py, lifecycle.py, provider_env.py, telemetry.py or autonomy.py (claimed by open tasks or a merged lane); import them read-only
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) and the desktop node tests stay green; existing tests are not weakened

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_orbit_view.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w05-orbit-operation/lanes/L2/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w05-orbit-operation`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
