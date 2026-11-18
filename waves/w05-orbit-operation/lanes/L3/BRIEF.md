# Lane L3 of wave w05-orbit-operation: Supervisor state repair: retire stale authoritative Main records

Owner: `claude:repository-coordinator`  |  Task id: `T-DEV-017`  |  Branch: `lane/w05-orbit-operation/L3`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/repair.py`
- `orchestrator/tests/test_dev_repair.py`
- `waves/w05-orbit-operation/lanes/L3/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w05-orbit-operation/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- New module dev_supervisor/repair.py with CLI `python -B -m dev_supervisor.repair [--yes]`: finds sessions-document records with role main, authoritative true, status ACTIVE/RETIRING that are NOT main.json's RUNNING session and whose process is not alive (pid from the record or ~/.claude/sessions files via telemetry.read_sessions, liveness probe injectable) and proposes retiring them (authoritative false, status RETIRED_STALE, note with reason and time)
- Dry run by default prints the plan; --yes applies it through StateStore atomic writes (backup kept), appends a 'state_repaired' event, and refuses when the supervisor lock is held by a live process unless --allow-while-running is given (then it only edits the sessions document, never main.json); never touches a session that is alive or is main.json's session; never deletes records
- At least 20 offline tests (stale vs alive vs current, lock held, corrupt document, idempotent re-run); one OBSERVED live DRY RUN against the real state (ids shortened to 8 chars); do not run --yes on the real state (Main decides)
- Synthetic data only: no real footage, no network in unit tests, no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any live observation is summarised as numbers and identifiers only, labelled OBSERVED with the exact command; anything not observed is NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, observed, proposed and open items
- Do NOT edit cli.py, launch.py, lifecycle.py, provider_env.py, telemetry.py or autonomy.py (claimed by open tasks or a merged lane); import them read-only
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) and the desktop node tests stay green; existing tests are not weakened

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_repair.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w05-orbit-operation/lanes/L3/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w05-orbit-operation`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
