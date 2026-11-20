# Lane L5 of wave w03-dev-supervisor: Resolve project discovery and bridge bootstrap, read-only (issue #44 part E)

Owner: `claude:dev-resolve-bootstrap`  |  Task id: `T-DEV-005`  |  Branch: `lane/w03-dev-supervisor/L5`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/resolve_discovery.py`
- `orchestrator/dev_supervisor/RESOLVE_BOOTSTRAP.md`
- `orchestrator/tests/test_dev_resolve_discovery.py`
- `waves/w03-dev-supervisor/lanes/L5/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- INVESTIGATE first and write the findings into RESOLVE_BOOTSTRAP.md (OBSERVED versus inferred): why an agent could launch DaVinci Resolve Free 21.0.4.5 yet not identify the intended project or start the in-app bridge; read-only inspection only (process list, the listening bridge port 49632 on 127.0.0.1, .mcp.json.example, the davinci-resolve MCP read tools, local Resolve project metadata); you must NOT open, create, rename, save, import into or modify any project or timeline and must not click in Resolve; if a read needs the bridge and it is unavailable, document exactly what the user must do
- resolve_discovery.py: a pure, injectable discovery module: is Resolve running (process check), is the bridge listening (TCP connect to 127.0.0.1 only), can the MCP answer a read-only call, current project name and stable identity (unique id where the API exposes one, else name plus database plus folder), current timeline name, project list; every field labelled OBSERVED or UNAVAILABLE; no guessing by name: it accepts a machine-local config of the expected project identity and reports MATCH, MISMATCH, UNCONFIGURED or UNAVAILABLE, and refuses to proceed on MISMATCH
- A small CLI entry (`python -B -m dev_supervisor.resolve_discovery`) prints the report as JSON, exit code non-zero on MISMATCH or UNAVAILABLE; it never performs a project mutation; bridge start is documented, and launched only on an explicit flag and only as far as it can be done without touching a project; otherwise it prints the exact manual fallback
- The real active project on this machine is DISCOVERED read-only and recorded (name, identity if available, timeline) in the lane report as OBSERVED; never written to a tracked file if it contains a personal path or a name not already public in the repository
- At least 30 offline tests with a fake process list, fake socket and fake MCP transport (MISMATCH, UNCONFIGURED, bridge down, Resolve closed, timeouts); coordination note: T-RSV-002 owns the resolve_executor package and the exclusive 'resolve' resource; this lane is read-only and does not touch that package
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_resolve_discovery.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L5/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
