# Lane L2 of wave w06-acceptance-text-workers: OmniRoute text-only workers on free routes (issue #44, mission part 5)

Owner: `claude:repository-coordinator`  |  Task id: `T-DEV-019`  |  Branch: `lane/w06-acceptance-text-workers/L2`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/text_worker.py`
- `orchestrator/tests/test_dev_text_worker.py`
- `waves/w06-acceptance-text-workers/lanes/L2/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w06-acceptance-text-workers/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- New module dev_supervisor/text_worker.py with CLI `python -B -m dev_supervisor.text_worker --task <task.json> --out <result.json> [--routes r1,r2] [--benchmark <results.json>]`: runs one bounded TEXT-ONLY task (kinds: summarise, classify, extract_json, review_notes) through the existing dev_supervisor.omniroute client against the local gateway; route order from a benchmark results file when given (pass rate, then latency), else a configured default list; bounded retries and fallback to the next route on rate_limited/timeout/not_found/auth_error; a deterministic output checker per kind (length bounds, label in allowed set, valid JSON with required keys); the result JSON records route, attempts, latencies, failure classes, checker verdict, and labels the output REPORTED until a checker passes (then CHECKED) — never VERIFIED
- Refuses non-local base URLs unless explicitly allowed; never uses a paid route; no tool use (Claude Code through OmniRoute currently fails tool calls with a Gemini thought_signature error, OBSERVED 2026-11-18: record that limitation in the module docstring)
- At least 30 offline tests with a fake transport (fallback order, retry bounds, each checker, refusal of remote URLs, no key in output); one OBSERVED live run of each task kind on the free gemini/gemini-3.1-flash-lite route (route, pass/fail, latency only)
- Synthetic data only: no real footage, no network in unit tests, no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any live observation is summarised as numbers and identifiers only, labelled OBSERVED with the exact command; anything not observed is NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, observed, proposed and open items
- Do NOT edit cli.py, launch.py, lifecycle.py, provider_env.py, telemetry.py, autonomy.py, snapshot.py or repair.py (claimed by open tasks or merged lanes); import them read-only
- No paid API route is ever used; OmniRoute calls go only to the local gateway; never read, print or store any key
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) and the desktop node tests stay green; existing tests are not weakened

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_text_worker.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w06-acceptance-text-workers/lanes/L2/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w06-acceptance-text-workers`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
