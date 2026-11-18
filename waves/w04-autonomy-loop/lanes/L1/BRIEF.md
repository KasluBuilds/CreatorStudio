# Lane L1 of wave w04-autonomy-loop: Autonomy loop: task selection, wake-up wait and Main Stop hook (issue #44, mission part 2-4)

Owner: `claude:repository-coordinator`  |  Task id: `T-DEV-013`  |  Branch: `lane/w04-autonomy-loop/L1`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/autonomy.py`
- `orchestrator/tests/test_dev_autonomy.py`
- `waves/w04-autonomy-loop/lanes/L1/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w04-autonomy-loop/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- New module orchestrator/dev_supervisor/autonomy.py with its own CLI `python -B -m dev_supervisor.autonomy <cmd>` (do NOT edit cli.py, launch.py, telemetry.py or any other existing module: they are claimed by open tasks T-DEV-010..012); standard library only; reuse existing modules read-only (state_store.StateStore, workers.WorkerRegistry, config.Config loading as cli.py does, evidence labels)
- `next [--json] [--repo <root>]`: builds the actionable list for Main from the repository records and the supervisor state, ordered by priority with a reason each: (a) worker results that are REPORTED and not yet verified (review_worker_result); (b) FAILED or LOST workers and whether a retry is eligible or blocked (bounded restarts, identical-failure stop); (c) wave lanes SUBMITTED but not verified, read from waves/*/wave.json plus `git` remote branch state via an injected runner; (d) registered tasks in ACTIVE_TASKS.json / DEVELOPMENT_TRACKER.json that are eligible: PLANNED or the next PLANNED step of an ACTIVE task whose dependencies are COMPLETED and whose owned_paths do not overlap another ACTIVE task; (e) blocked items with the exact blocker (dependency, lease held by another task, missing merge). When nothing is actionable it returns an explicit idle_reason (for example 'waiting for 2 running workers' or 'no eligible task: T-X waits for T-Y'). Never invents work; repository files win over supervisor cache
- `wait --timeout <s> [--poll <s>] [--cursor <file>]`: blocks until the actionable fingerprint changes or a new supervisor event (events.jsonl beyond a stored cursor) of kind worker_* / main_* / replacement arrives, prints a short JSON summary of what changed, exit 0 on change and exit 3 on timeout; meant to be run by Main as a background command so its exit re-invokes Main; bounded, low CPU, tolerant of missing/corrupt state
- `stop-hook`: implements the Claude Code Stop hook contract: reads the hook JSON from stdin (session_id, stop_hook_active, ...); only for the authoritative Main session id recorded in the supervisor state (any other session: allow, no output); if actionable items exist that Main has not yet been told about (fingerprint stored in the state directory), print {"decision": "block", "reason": "<one line naming the next item and the command to inspect it>"}; if stop_hook_active is true and the fingerprint is unchanged, allow; at most a configurable number of consecutive blocks (default 3) before allowing; any internal error fails OPEN (allow, exit 0) and is logged to the state directory; reasons never contain secrets or transcript text
- `main-settings --out <file> [--python <exe>] [--orchestrator <dir>]`: writes a Claude Code settings JSON containing only a hooks.Stop entry that runs the stop-hook command (argument-safe quoting on Windows), for use with `claude --settings <file>`; it never edits ~/.claude, the repository's .claude/ or any launcher; wiring it into launch.py is a later task (record that as PROPOSED)
- At least 40 offline tests with temp state directories, synthetic ACTIVE_TASKS/TRACKER/wave manifests, a fake git runner and a fake clock (eligibility rules, dependency and overlap blocking, idle reasons, wait change/timeout, stop-hook block/allow/loop-bound/fail-open/other-session, settings file shape)
- One OBSERVED live dry run of `next --json` against this repository and the real machine-local supervisor state directory, reported as counts and task ids only
- Synthetic data only: no real footage, no network in unit tests, no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any live observation is summarised as numbers and identifiers only (never prompts, transcript text or file contents), labelled OBSERVED with the exact command; anything not observed is NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, observed, proposed and open items
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) and the desktop node tests stay green; existing tests are not weakened

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_autonomy.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w04-autonomy-loop/lanes/L1/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w04-autonomy-loop`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
