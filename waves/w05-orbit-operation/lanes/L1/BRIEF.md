# Lane L1 of wave w05-orbit-operation: Snapshot: operation picture (next items, idle reason), per-worker context, supervisor health, handoff state

Owner: `claude:repository-coordinator`  |  Task id: `T-DEV-015`  |  Branch: `lane/w05-orbit-operation/L1`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/snapshot.py`
- `orchestrator/tests/test_dev_snapshot.py`
- `waves/w05-orbit-operation/lanes/L1/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w05-orbit-operation/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- snapshot.build_snapshot keeps its current signature working and accepts optional inputs for the new sections; Snapshot additions (top-level keys, every value an evidence dict {label, value, timestamp, source, freshness} or a list of plain records): 'operation': {'actionable': [{id, kind, priority, summary, inspect}], 'others_waiting': [...same], 'blocked': [{id, kind, summary}], 'idle_reason': evidence, 'warnings': [str], 'fingerprint': str}; 'supervisor_health': {'lock_alive': evidence, 'last_tick_at': evidence, 'last_event_kind': evidence}; 'handoff': {'latest_id': evidence, 'verified': evidence, 'created_at': evidence}; each worker record gains 'context_pressure': {'tokens','window','ratio','level'} evidences (UNAVAILABLE with reason when its transcript is not found).
- The operation section comes from dev_supervisor.autonomy (read-only import of its next computation); per-worker context pressure uses telemetry.find_transcript/context_pressure for each worker's session id and cwd (worktree)
- Supervisor health from the state store lock and the events log (last tick or last event time); handoff state from the newest handoff file in the state directory and its verify status if recorded
- New entry point `python -B -m dev_supervisor.snapshot --out <file> [--repo <root>]` builds the FULL snapshot (telemetry, registry, events, operation, health, handoff, account usage via telemetry.RateLimitEventProvider if that class exists, else UNAVAILABLE) and writes it atomically; read-only otherwise
- At least 25 new offline tests; one OBSERVED live run of the entry point against the real state (counts only)
- Synthetic data only: no real footage, no network in unit tests, no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any live observation is summarised as numbers and identifiers only, labelled OBSERVED with the exact command; anything not observed is NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, observed, proposed and open items
- Do NOT edit cli.py, launch.py, lifecycle.py, provider_env.py, telemetry.py or autonomy.py (claimed by open tasks or a merged lane); import them read-only
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) and the desktop node tests stay green; existing tests are not weakened

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_snapshot.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w05-orbit-operation/lanes/L1/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w05-orbit-operation`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
