# Lane L6 of wave w03-dev-supervisor: Automatic GitHub progress reporting: durable event log (issue #44 part 5)

Owner: `claude:dev-github-log`  |  Task id: `T-DEV-006`  |  Branch: `lane/w03-dev-supervisor/L6`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/github_log.py`
- `orchestrator/tests/test_dev_github_log.py`
- `waves/w03-dev-supervisor/lanes/L6/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- github_log.py: an event model (task_started, step_done, decision, test_run, failure, retry, provider_change, handoff, compaction, blocker, pr_opened) with evidence references and a source label; renders a concise Markdown comment; every event is scrubbed (tokens, key/value secrets, absolute user paths, long opaque strings) before it leaves the process
- Posting goes through an injected Runner protocol wrapping the gh CLI with argument lists (never shell strings): issue comment, issue create (sub-task), issue close; a real runner is provided but all tests use a fake; rate limiting and batching, bounded retries with backoff, an offline spool file (machine-local, atomic) so an event is never lost when GitHub or the network is down, and an idempotent flush (an event id marker prevents duplicate comments)
- Visibility safety: the repository is PUBLIC; the logger refuses to post content flagged as sensitive (secret pattern, transcript text marker, personal path) and records the refusal locally instead
- A helper creates a sub-task issue from a task record (id, owner, scope, dependencies, acceptance criteria, branch/worktree) with a back-reference to the parent issue number
- At least 40 offline tests (fake gh runner, outage plus spool plus flush, duplicate prevention, scrubbing, refusal)
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_github_log.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L6/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
