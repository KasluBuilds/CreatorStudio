# Lane L2 of wave w04-autonomy-loop: Orbit: readable usage windows and explicit data-quality labels (issue #44, mission part 7)

Owner: `claude:dev-orbit`  |  Task id: `T-DEV-014`  |  Branch: `lane/w04-autonomy-loop/L2`  |  Depends on lanes: none

## You may edit only
- `dashboard/desktop/`
- `orchestrator/tests/test_dev_orbit_view.py`
- `waves/w04-autonomy-loop/lanes/L2/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w04-autonomy-loop/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- Orbit account-usage section (dashboard/desktop/src/orbit.js and its UI) shows the known keys produced by the supervisor's rate_limit_event provider in plain language: '5-hour window: used' -> '21%'; '5-hour window: resets' -> the UTC time plus a relative phrase ('in 1 h 3 min', or 'window already reset' when the time is past); the same for the 7-day window; 'rate-limit status'; unknown keys keep the existing generic rendering
- A provenance line states the source (for example 'from the last worker run's rate_limit_event') and its age; a value whose own timestamp is older than the stale threshold is shown as stale, never as live
- Every value shows one explicit data-quality badge from: live, cached, stale, estimated (label INFERRED), unavailable (with reason); REPORTED account usage stays UNAVAILABLE as today (the OBSERVED-only rule is unchanged)
- Orbit stays read-only (no new write paths; the existing read-only test still passes); released artifact names, package name and ids are not renamed
- Node tests (cd dashboard/desktop && node --test "tests/*.test.js") extended with fixtures for the new labels, resets in the future and in the past, stale values and unknown keys; existing tests updated only where the label text intentionally changes, with a note in the lane report
- orchestrator/tests/test_dev_orbit_view.py runs the Orbit node tests through subprocess when node is on PATH (skips with a clear reason otherwise) and checks the Python snapshot fixture for the usage keys renders through orbit.js without leaking REPORTED values
- Synthetic data only: no real footage, no network in unit tests, no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any live observation is summarised as numbers and identifiers only (never prompts, transcript text or file contents), labelled OBSERVED with the exact command; anything not observed is NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, observed, proposed and open items
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) and the desktop node tests stay green; existing tests are not weakened

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_orbit_view.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w04-autonomy-loop/lanes/L2/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w04-autonomy-loop`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
