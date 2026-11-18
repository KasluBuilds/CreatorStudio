# Lane L2 of wave w07-autonomous-integration: Independent review packets and review-report validation (issue #44, full-autonomy policy)

Owner: `claude:repository-coordinator`  |  Task id: `T-DEV-022`  |  Branch: `lane/w07-autonomous-integration/L2`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/review.py`
- `orchestrator/tests/test_dev_review.py`
- `waves/w07-autonomous-integration/lanes/L2/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w07-autonomous-integration/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- New module dev_supervisor/review.py, CLI `python -B -m dev_supervisor.review packet <pr> --out <dir>` and `python -B -m dev_supervisor.review check <pr>`
- packet: fetches the PR (gh), computes the merge base and diff hash, writes diff.patch, files.txt, the registered task's acceptance criteria and owned paths, and a reviewer prompt (reviewer-prompt.md) instructing an independent reviewer worker to inspect correctness, tests, regressions, security, architecture and conventions, run the tests, classify BLOCKING/NON-BLOCKING findings and post exactly one comment in the dev-review v1 format; Review report format (posted as a PR comment by a reviewer worker): first line exactly '<!-- dev-review v1 pr=<n> diff_sha256=<16 hex> verdict=<APPROVE|CHANGES_REQUESTED> reviewer=<reviewer id> -->'; diff hash = first 16 hex of sha256 of `git diff --binary $(git merge-base <base> <head>) <head>`; the body lists BLOCKING and NON-BLOCKING findings and exact test result lines
- check: lists review markers on the PR, validates format, recomputes the diff hash, and reports CURRENT_APPROVED / STALE (hash changed: re-review needed) / CHANGES_REQUESTED / MISSING / INVALID; exposes a function used by integrate.py later (pure, injectable runners)
- At least 30 offline tests (marker parsing edge cases, forged or malformed markers, stale hash, multiple reviews latest-wins, author-equals-reviewer rejection); one OBSERVED live `check` against a real PR that has a review comment
- Synthetic data only; no network in unit tests (gh and git are injected runners with fakes); no secrets/tokens printed, stored or committed; no absolute user paths in tracked files
- Live observations summarised as numbers/identifiers, labelled OBSERVED with the exact command; anything not observed is NOT TESTED; nothing VERIFIED without command output
- Do NOT edit any existing module (cli.py, launch.py, lifecycle.py, provider_env.py, telemetry.py, autonomy.py, snapshot.py, repair.py, acceptance.py, text_worker.py are claimed or merged); import read-only
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) stays green

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_review.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w07-autonomous-integration/lanes/L2/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w07-autonomous-integration`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
