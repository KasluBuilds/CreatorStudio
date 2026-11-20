# Lane L8 of wave w03-dev-supervisor: Supervisor CLI and one-command launch of Main (issue #44 integration)

Owner: `claude:dev-cli`  |  Task id: `T-DEV-008`  |  Branch: `lane/w03-dev-supervisor/L8`  |  Depends on lanes: L1, L2, L3, L4, L6

## You may edit only
- `orchestrator/dev_supervisor/cli.py`
- `orchestrator/dev_supervisor/__main__.py`
- `orchestrator/dev_supervisor/launch.py`
- `orchestrator/dev_supervisor/config.py`
- `orchestrator/tests/test_dev_cli.py`
- `waves/w03-dev-supervisor/lanes/L8/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- config.py: machine-local configuration (state directory outside the repo, thresholds, model table, OmniRoute base URL, GitHub repo and issue, paid-route flag default off) loaded from a machine-local file; a documented example is committed as a template with placeholders only
- cli.py and __main__.py: `python -B -m dev_supervisor` with subcommands start (launch Main under the supervisor), status, workers, assign, handoff, compact-check, replace, report (flush the GitHub log), snapshot, route (explain a routing decision), doctor (check claude CLI, git, gh, OmniRoute, Resolve discovery, state dir, repo clean); each is non-destructive by default and exits non-zero on a failed check; the documented single launch command starts Main and the supervisor loop
- launch.py: starts Main as a claude CLI session with the verified handoff and operating instructions as an appended prompt file, in the repository root, with a selectable profile that only passes flags (model, permission mode as configured) and NEVER reads, copies or modifies pro.ps1, backup.ps1 or .mcp.json; the Claude Pro versus OmniRoute choice is made by which local launcher the user runs and is documented, not automated by editing those launchers; the dangerous-permissions flag is only passed when the machine-local config explicitly enables it
- Integration test with fakes: start, assign two fake workers, simulate pressure, handoff verified, replacement, status reflects the new Main; nothing touches the real machine in tests
- At least 30 offline tests
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_cli.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L8/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
