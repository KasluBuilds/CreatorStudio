# Lane L3 of wave w06-acceptance-text-workers: Orbit polish: styles for operation sections and dated long resets

Owner: `claude:dev-orbit`  |  Task id: `T-DEV-020`  |  Branch: `lane/w06-acceptance-text-workers/L3`  |  Depends on lanes: none

## You may edit only
- `dashboard/desktop/`
- `orchestrator/tests/test_dev_orbit_view.py`
- `waves/w06-acceptance-text-workers/lanes/L3/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w06-acceptance-text-workers/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- dashboard/desktop styles give the operation sections (classes added by the previous wave, e.g. orbit-act) readable list styling consistent with the existing theme in light and dark mode; no new dependencies
- Account-usage reset rows show the date when the reset is more than 24 h away (e.g. 'Sat 21 Nov 23:00 UTC (in 3 d 9 h)'), deterministic from nowMs; existing behaviour for nearer resets unchanged
- Node tests for both; Orbit stays read-only; no renames of released artifacts
- Synthetic data only: no real footage, no network in unit tests, no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any live observation is summarised as numbers and identifiers only, labelled OBSERVED with the exact command; anything not observed is NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, observed, proposed and open items
- Do NOT edit cli.py, launch.py, lifecycle.py, provider_env.py, telemetry.py, autonomy.py, snapshot.py or repair.py (claimed by open tasks or merged lanes); import them read-only
- No paid API route is ever used; OmniRoute calls go only to the local gateway; never read, print or store any key
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) and the desktop node tests stay green; existing tests are not weakened

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_orbit_view.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w06-acceptance-text-workers/lanes/L3/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w06-acceptance-text-workers`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
