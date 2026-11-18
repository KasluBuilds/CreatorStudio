# Lane L1 of wave w07-autonomous-integration: Autonomous merge gate with serialization lock (issue #44, full-autonomy policy)

Owner: `claude:repository-coordinator`  |  Task id: `T-DEV-021`  |  Branch: `lane/w07-autonomous-integration/L1`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/integrate.py`
- `orchestrator/tests/test_dev_integrate.py`
- `waves/w07-autonomous-integration/lanes/L1/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w07-autonomous-integration/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- New module dev_supervisor/integrate.py, CLI `python -B -m dev_supervisor.integrate <pr> [--yes] [--repo <root>] [--base main]`: evaluates EVERY gate and prints a gate table; with --yes and all gates PASS it merges; dry run by default
- Gates: (1) PR open, not draft, base main, head branch prefix task/, wave/ or chatgpt/; (2) required check 'validate' SUCCESS on the exact current head SHA and no failing or pending required check; (3) GitHub mergeStateStatus CLEAN (up to date, no conflicts); (4) scope: every changed file lies inside the owned_paths of the ACTIVE/UNDER_REVIEW task registered for the head branch in ACTIVE_TASKS.json on origin/main, or for a wave/ branch inside the union of its lanes' owned paths plus waves/<id>/, ACTIVE_TASKS.json, DEVELOPMENT_TRACKER.json, CHANGELOG.md, SESSION_HANDOFF.md written by wave close; a registry-only PR (only ACTIVE_TASKS.json, DEVELOPMENT_TRACKER.json, SESSION_HANDOFF.md, CHANGELOG.md) passes scope as a record; (5) review: a PR comment whose first line is a valid dev-review v1 marker with verdict APPROVE whose diff_sha256 equals the hash recomputed now, posted by a reviewer that is not recorded as the author (author identity = the PR's head commits' Co-Authored-By/author lines and the task owner; the reviewer id must differ) — a newer CHANGES_REQUESTED for the same hash blocks; wave PRs may instead rely on wave close evidence only if a review marker exists for the wave diff; (6) secret scan of added lines (reuse orchestrator/governance's scanner read-only if importable, else an equivalent pattern list); (7) no tracked launcher/credential/media file added. Review report format (posted as a PR comment by a reviewer worker): first line exactly '<!-- dev-review v1 pr=<n> diff_sha256=<16 hex> verdict=<APPROVE|CHANGES_REQUESTED> reviewer=<reviewer id> -->'; diff hash = first 16 hex of sha256 of `git diff --binary $(git merge-base <base> <head>) <head>`; the body lists BLOCKING and NON-BLOCKING findings and exact test result lines
- Serialization: an exclusive integration lock file in the supervisor state directory (atomic create, holder pid/time, stale after a timeout) so two merges never run at once; the lock is held from final gate evaluation through merge and read-back
- Merge: `gh pr merge <n> --merge --match-head-commit <evaluated head>`; then read back `gh pr view <n> --json state,mergeCommit` and fail unless MERGED; post one PR comment with the gate table and result (marker '<!-- dev-integrate v1 pr=<n> head=<sha> result=<MERGED|REFUSED> -->'); append a supervisor event
- Never force, never admin-bypass, never retries a failed merge more than once, never merges when any gate is FAIL or UNKNOWN
- At least 40 offline tests with fake gh/git runners covering every gate pass/fail, lock contention and stale lock, head moved between evaluation and merge (match-head-commit failure), read-back failure; one OBSERVED live DRY RUN against a real open PR (gate table only)
- Synthetic data only; no network in unit tests (gh and git are injected runners with fakes); no secrets/tokens printed, stored or committed; no absolute user paths in tracked files
- Live observations summarised as numbers/identifiers, labelled OBSERVED with the exact command; anything not observed is NOT TESTED; nothing VERIFIED without command output
- Do NOT edit any existing module (cli.py, launch.py, lifecycle.py, provider_env.py, telemetry.py, autonomy.py, snapshot.py, repair.py, acceptance.py, text_worker.py are claimed or merged); import read-only
- Full suite (cd orchestrator && python -B -I -m unittest discover -s tests) stays green

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_integrate.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w07-autonomous-integration/lanes/L1/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w07-autonomous-integration`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
