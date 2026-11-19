# Live autonomy harness (T-DEV-026)

`live_autonomy.py` runs a real headless Claude Code **Main** (model haiku) unattended in a throwaway LOCAL sandbox
(bare origin, integration checkout, per-task worktrees, synthetic backlog L-001..L-006). Main works only through a
`ctl` command surface (`next, claim, dispatch, wait, status, verify, review, integrate, recover`); a separate harness
supervisor process spawns the real worker and reviewer `claude -p` processes; a chaos driver injects failures.
Every session gets the authority hook through `session_policy.install` (verified by executing it before launch).

Run: `cd orchestrator && python -B -m dev_supervisor.live_autonomy run --root <empty dir> --model haiku --yes`
(uses the configured Claude provider with the haiku model; no paid API; no real footage; nothing outside the sandbox).

## Which run is evidence for what
- **run4 (568 s, OBSERVED 2026-11-19) ran the final code of this PR** and is the evidence below.
- run3 (421 s) had the same outcome on the code before the review hardening (clean-tree test gate, merge of the tested sha,
  lock owner token, identity proof that does not trust ledger markers/parents). run1 and run2 were earlier runs: run1's Main hit
  2 tool errors from a `$CS_CTL` quoting bug, run2's worker-kill and stray-file injections missed their window; both fixed.
- The ledger and git history are the evidence. A Main's own final summary is not: in run4 it said "Five of six tasks are
  integrated" while the ledger and `git log` show four.

## What run4 showed

| Capability | Label | Evidence |
|---|---|---|
| Main selects, claims, dispatches, verifies, reviews, integrates and re-selects tasks with no human input after the kickoff prompt | DEMONSTRATED UNATTENDED (local sandbox, haiku Main) | 4 integrations (`git log --first-parent`: L-001..L-004 merge commits); all 50 Main Bash calls went through `ctl`; 0 tool errors |
| Main ends its own loop when the backlog is exhausted | LIVE-TESTED | Main's log ends with a `result` event after `next` reported `work_remaining: false`; the Stop hook never blocked (no `stop_blocks` counter), so Stop-hook continuation was NOT exercised |
| Real worker implements the task; Main verifies tests + scope itself | LIVE-TESTED | real `claude -p` workers; `ctl verify` re-runs the tests on the committed tree |
| Independent review before merge (separate reviewer session, different id, bound to the diff hash) | LIVE-TESTED, approve path only | 4 reviews, all APPROVE; a live rejection was never produced |
| Worker killed before it committed -> Main recovers by re-dispatch | LIVE-TESTED | L-001 worker killed with identity proof (exit 15, classified DEAD); Main dispatched attempt 2 (`w-l-001-2`) 11 s after the kill; L-001 integrated |
| Kill refused without identity proof | LIVE-TESTED | `authority.decide` refused a PID-only kill (`process-identity`) before each identity-proven kill |
| Out-of-scope change caught and repaired | LIVE-TESTED at the `verify` step | harness committed `stray.txt` on L-003; `verify` reported it; Main dispatched a repair worker; re-verify clean; L-003 integrated |
| Main killed unexpectedly -> same session resumed and loop continues | LIVE-TESTED | killed with identity proof (exit 15, recorded not clean); resumed 4 s later with the same session id (all 190 log lines carry one session id); Main claimed the next task |
| Supervisor killed and restarted -> state recovered | LIVE-TESTED | killed with identity proof; the new supervisor served the request queued meanwhile |
| Blocked task: attempts exhausted -> BLOCKED, dependants stay unstarted | LIVE-TESTED | L-005 (contradictory test) BLOCKED after 2 attempts; L-006 never started |
| Local gate set (registered, tests on the committed head, scope, review bound to the diff hash and not the worker, secrets, forbidden files, up to date, merge lock; merges exactly the tested sha) | INTEGRATION-TESTED (33 offline tests with real git; each gate has a failing case, including the dirty-worktree bypass the review found) and used by the live runs | a gate refusing a merge was NOT observed live |
| Authority hook in every launched session | INSTALLED AND VERIFIED per session by `session_policy.install`; no protected action was attempted in the runs, so no live block was observed in them | live blocking was observed separately by `session_policy live-probe` (T-DEV-025) |

## Not demonstrated (do not read the table above as covering these)
- Context-pressure handoff and replacement without a duplicate Main (only resume of the same session was exercised).
- The production path: `dev_supervisor start` / `main-host`, an interactive Main, GitHub PRs, `dev_supervisor.integrate` over `gh`.
  Production gate semantics are proven separately on real PRs; this harness uses a LOCAL gate implementation that mirrors them.
- Rejection by a live reviewer; an integrate-time gate refusal in a live run; Stop-hook continuation.
- The ledger does not record Main's end state (it still says RUNNING with the old exit code after the final resume).
- Clean-exit classification of an adopted process (no exit code is available, so it is recorded as not clean).
- More than one concurrent worker; any model other than haiku; runs longer than ~10 minutes; stalled-worker handling (implemented and unit-tested, no worker stalled).
- Offline tests do not cover `launch_request`, `start_main`, the supervisor tick beyond one malformed-request iteration, or the `run_live` cleanup; the process tests use injected identity/kill fakes, not real psutil.
- Chaos is injected by the harness: the failures are real (real kills, real commits) but chosen, not spontaneous.

## run4 timeline (UTC, from `state/events.jsonl`)
| time | event | detail |
|---|---|---|
| 18:53:06 | supervisor_started |  |
| 18:53:16 | main_started | resume=False |
| 18:53:45 | claimed | task=L-001 |
| 18:53:47 | dispatch_requested | task=L-001 worker=w-l-001-1 attempt=1 repair=False |
| 18:53:59 | chaos_pid_only_kill_refused | worker=w-l-001-1 rule=process-identity |
| 18:53:59 | chaos_worker_kill | task=L-001 worker=w-l-001-1 killed=True why=killed with identity proof |
| 18:54:10 | dispatch_requested | task=L-001 worker=w-l-001-2 attempt=2 repair=False |
| 18:54:46 | verified | task=L-001 tests_pass=True stray=[] |
| 18:55:33 | integrated | task=L-001 merge=f78310085b22 |
| 18:55:44 | claimed | task=L-002 |
| 18:55:46 | dispatch_requested | task=L-002 worker=w-l-002-1 attempt=1 repair=False |
| 18:56:21 | verified | task=L-002 tests_pass=True stray=[] |
| 18:57:00 | integrated | task=L-002 merge=79aa65c0860b |
| 18:57:02 | chaos_main_kill | killed=True why=killed with identity proof |
| 18:57:04 | main_exited | clean=False exit_code=15 |
| 18:57:08 | main_started | resume=True |
| 18:57:22 | claimed | task=L-003 |
| 18:57:23 | dispatch_requested | task=L-003 worker=w-l-003-1 attempt=1 repair=False |
| 18:57:54 | chaos_stray_file_committed | task=L-003 |
| 18:58:02 | verified | task=L-003 tests_pass=True stray=['stray.txt'] |
| 18:58:07 | dispatch_requested | task=L-003 worker=w-l-003-2 attempt=2 repair=True |
| 18:58:48 | verified | task=L-003 tests_pass=True stray=[] |
| 18:59:23 | integrated | task=L-003 merge=226dceb3e94a |
| 18:59:24 | chaos_supervisor_kill | killed=True why=killed with identity proof |
| 18:59:29 | supervisor_started |  |
| 18:59:35 | claimed | task=L-004 |
| 18:59:37 | dispatch_requested | task=L-004 worker=w-l-004-1 attempt=1 repair=False |
| 19:00:29 | verified | task=L-004 tests_pass=True stray=[] |
| 19:01:05 | integrated | task=L-004 merge=f12b7dc763a8 |
| 19:01:15 | claimed | task=L-005 |
| 19:01:16 | dispatch_requested | task=L-005 worker=w-l-005-1 attempt=1 repair=False |
| 19:01:54 | dispatch_requested | task=L-005 worker=w-l-005-2 attempt=2 repair=False |
| 19:02:29 | blocked | task=L-005 |
