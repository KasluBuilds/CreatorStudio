# Lane L1 of wave w06-acceptance-text-workers: End-to-end autonomy acceptance harness (issue #44, mission part 11)

Owner: `claude:repository-coordinator`  |  Task id: `T-DEV-018`  |  Branch: `lane/w06-acceptance-text-workers/L1`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/acceptance.py`
- `orchestrator/tests/test_dev_acceptance.py`
- `waves/w06-acceptance-text-workers/lanes/L1/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w06-acceptance-text-workers/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- New module dev_supervisor/acceptance.py with CLI `python -B -m dev_supervisor.acceptance --out <report.json> [--real-worker] [--keep]` that runs a scripted scenario in an ISOLATED temp state directory and a temp git repository (never the real supervisor state, never this repository's registry): (1) a synthetic ACTIVE_TASKS.json/DEVELOPMENT_TRACKER.json with two eligible tasks and one blocked task; (2) autonomy next selects the first eligible task; (3) a worker is assigned and spawned through the existing lifecycle (ConfiguredSupervisor / WorkerSupervisor) with a fake launcher by default, or with --real-worker a REAL bounded `claude -p --model haiku` run (child env without parent-session markers and without ANTHROPIC_* overrides, prompt: write one file in its temp worktree) whose exit and output file are checked; (4) the result is recorded REPORTED, independently verified by the harness (file content check), marked VERIFIED; (5) the task is marked COMPLETED in the synthetic registry and autonomy next selects the second task without any human input; (6) a context threshold is SIMULATED with a synthetic transcript above the compact threshold, compaction decides, a handoff is built, written and verified (handoff module, injected checkers), and replacement runs with a fake Main launcher and a simulated heartbeat; (7) the replacement Main's view (autonomy next) still shows the second task and not the completed first one (no duplication); (8) the stop-hook blocks the new Main while work remains and allows after it is told
- The report lists every step with outcome PASS/FAIL and a mode label REAL, SIMULATED or MOCKED, plus timings; exit 0 only if all steps pass; nothing is labelled REAL unless a real process ran
- At least 25 offline tests (default mocked mode, failure injection per step, report shape, isolation: the real state dir and repo registry are never touched); one OBSERVED live run in default mode and one with --real-worker (counts and step outcomes only)
- Synthetic data only: no real footage, no network in unit tests, no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any live observation is summarised as numbers and identifiers only, labelled OBSERVED with the exact command; anything not observed is NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, observed, proposed and open items
- Do NOT edit cli.py, launch.py, lifecycle.py, provider_env.py, telemetry.py, autonomy.py, snapshot.py or repair.py (claimed by open tasks or merged lanes); import them read-only
- No paid API route is ever used; OmniRoute calls go only to the local gateway; never read, print or store any key
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) and the desktop node tests stay green; existing tests are not weakened

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_acceptance.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w06-acceptance-text-workers/lanes/L1/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w06-acceptance-text-workers`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
