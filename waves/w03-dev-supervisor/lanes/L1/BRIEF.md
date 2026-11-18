# Lane L1 of wave w03-dev-supervisor: Telemetry and evidence model: sessions, context pressure, usage signals (issue #44 part A)

Owner: `claude:dev-telemetry`  |  Task id: `T-DEV-001`  |  Branch: `lane/w03-dev-supervisor/L1`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/__init__.py`
- `orchestrator/dev_supervisor/telemetry.py`
- `orchestrator/dev_supervisor/evidence.py`
- `orchestrator/tests/test_dev_telemetry.py`
- `waves/w03-dev-supervisor/lanes/L1/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- evidence.py: a small Evidence model with a source label (OBSERVED, REPORTED, INFERRED, UNAVAILABLE), timestamp, staleness check and helpers; every telemetry value carries a label and never an exact number the source does not expose
- telemetry.py reads the Claude Code per-process session files (pid, sessionId, cwd, status busy/idle, name, startedAt, messaging socket path) from a configurable directory, tolerates missing/corrupt files and dead pids (liveness by pid probe, injectable), and reports staleness
- telemetry.py computes per-session context pressure from the JSONL transcript by reading only the last assistant usage record (input + cache_read + cache_creation tokens, model id), with a configurable per-model context window table and thresholds (warn, compact, replace) as configuration with documented defaults; unknown model window gives UNAVAILABLE pressure, never a guess; tail read so large transcripts are not loaded whole; no transcript text is ever returned
- Account usage and reset windows: a pluggable provider interface plus a statusline-JSON ingestor; with no source it returns UNAVAILABLE with the reason; the module documentation states plainly that nothing here is an account-usage reading unless a provider supplies it
- A single snapshot function returns a JSON-serialisable dict (schema_version, generated_at, sessions[], each with the labelled fields) used later by the supervisor and Orbit
- At least 40 offline tests with synthetic session files and synthetic transcripts (corrupt lines, huge line, missing usage, multiple models, dead pid, threshold edges); one real-machine OBSERVED run reported as numbers only
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_telemetry.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L1/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
