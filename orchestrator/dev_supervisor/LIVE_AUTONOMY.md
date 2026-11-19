# Live autonomy harness (T-DEV-026)

`live_autonomy.py` runs a real headless Claude Code **Main** (model haiku) unattended in a throwaway LOCAL sandbox
(bare origin, integration checkout, per-task worktrees, synthetic backlog L-001..L-006). Main works only through a
`ctl` command surface (`next, claim, dispatch, wait, status, verify, review, integrate, recover`); a separate harness
supervisor process spawns the real worker and reviewer `claude -p` processes; a chaos driver injects failures.
Every session gets the authority hook through `session_policy.install` (verified by executing it before launch).

Run: `cd orchestrator && python -B -m dev_supervisor.live_autonomy run --root <empty dir> --model haiku --yes`
(uses the configured Claude provider with the haiku model; no paid API; no real footage; nothing outside the sandbox).

## What the live runs showed (run3 = final code, OBSERVED 2026-11-19; run1 and run2 are earlier runs of the same harness)

| Capability | Label | Evidence |
|---|---|---|
| Main selects, claims, dispatches, verifies, reviews, integrates and re-selects tasks with no human input after the kickoff prompt | DEMONSTRATED UNATTENDED (local sandbox, haiku Main) | run3: 4 consecutive cycles in 421 s; all 48 Main Bash calls went through `ctl`; 0 tool errors. run1 completed the same loop but its Main hit 2 tool errors from a `$CS_CTL` quoting bug (fixed by a wrapper script before run2/run3); run2 completed 4 integrations too but its worker-kill and stray-file injections missed their window (fixed for run3) |
| Real worker implements the task; Main verifies tests + scope itself | LIVE-TESTED | real `claude -p` workers, tests re-run by `ctl verify` |
| Independent review before merge (separate reviewer session, different id, bound to the diff hash) | LIVE-TESTED, approve path only | 4 reviews, all APPROVE; a rejection was never produced live (unit-tested only) |
| Worker killed before it committed -> Main recovers by re-dispatch | LIVE-TESTED | run3: L-001 worker killed with identity proof, classified DEAD (exit 15), Main dispatched attempt 2 four seconds later, L-001 integrated. (run1 and run2 kills hit workers that had already committed or exited, so they did not exercise recovery) |
| Kill refused without identity proof | LIVE-TESTED | `authority.decide` refused a PID-only kill (`process-identity`) before each identity-proven kill |
| Out-of-scope change caught and repaired | LIVE-TESTED at the `verify` step | run3: `stray.txt` committed on L-003 by the harness; `verify` reported it; Main dispatched a repair worker; L-003 integrated |
| Main killed unexpectedly -> same session resumed and loop continues | LIVE-TESTED | run3: killed with identity proof (exit 15, classified not clean), resumed the same session id 3 s later, claimed L-003 next |
| Supervisor killed and restarted -> state recovered | LIVE-TESTED | run3: supervisor killed with identity proof, new supervisor served the queued L-004 dispatch from the request spool and ledger |
| Blocked task: attempts exhausted -> BLOCKED, dependants stay unstarted | LIVE-TESTED | run3: L-005 (contradictory test) BLOCKED after 2 attempts; L-006 never started; Main stopped because `work_remaining` was false |
| Local gate set (registered, tests on head, scope, review bound to diff hash and not the worker, secrets, forbidden files, up to date, merge lock) | INTEGRATION-TESTED (25 offline tests with real git, one failing case per gate) and used by the live runs | refusal of a merge by a gate was NOT observed live |
| Authority hook in every launched session | INSTALLED AND VERIFIED per session; no protected action was attempted in the runs, so no live block was observed in them | live blocking was observed separately by `session_policy live-probe` (T-DEV-025) |

## Not demonstrated (do not read the table above as covering these)
- Context-pressure handoff and replacement without a duplicate Main (only resume of the same session was exercised).
- The production path: `dev_supervisor start` / `main-host`, an interactive Main, GitHub PRs, `dev_supervisor.integrate` over `gh`.
  Production gate semantics are proven separately on real PRs; this harness uses a LOCAL gate implementation that mirrors them.
- Rejection by a live reviewer, and an integrate-time gate refusal, in a live run.
- Clean-exit classification of Main after a supervisor restart: an adopted process has no exit code, so it is recorded as not clean.
- More than one concurrent worker; any model other than haiku; runs longer than ~7 minutes.
- Chaos is injected by the harness; the failures are real (real kills, real commits) but chosen, not spontaneous.

## Run3 timeline (UTC, from `state/events.jsonl`)
| time | event | detail |
|---|---|---|
| 18:26:17 | supervisor_started |  |
| 18:26:27 | main_started | resume=False session=4ed49682-b824-4657-a5fb-ddf739845e3c |
| 18:26:44 | claimed | task=L-001 |
| 18:26:45 | dispatch_requested | task=L-001 worker=w-l-001-1 attempt=1 repair=False |
| 18:26:52 | chaos_pid_only_kill_refused | worker=w-l-001-1 verdict=DENY rule=process-identity |
| 18:26:52 | chaos_worker_kill | task=L-001 worker=w-l-001-1 killed=True why=killed with identity proof |
| 18:26:57 | dispatch_requested | task=L-001 worker=w-l-001-2 attempt=2 repair=False |
| 18:27:19 | verified | task=L-001 tests_pass=True stray=[] |
| 18:27:45 | integrated | task=L-001 merge=07ed68d983b5 |
| 18:27:49 | claimed | task=L-002 |
| 18:27:49 | dispatch_requested | task=L-002 worker=w-l-002-1 attempt=1 repair=False |
| 18:28:13 | verified | task=L-002 tests_pass=True stray=[] |
| 18:28:47 | integrated | task=L-002 merge=560bc7a99635 |
| 18:28:49 | chaos_main_kill | killed=True why=killed with identity proof session=4ed49682-b824-4657-a5fb-ddf739845e3c |
| 18:28:51 | main_exited | clean=False exit_code=15 session=4ed49682-b824-4657-a5fb-ddf739845e3c |
| 18:28:52 | main_started | resume=True session=4ed49682-b824-4657-a5fb-ddf739845e3c |
| 18:28:59 | claimed | task=L-003 |
| 18:28:59 | dispatch_requested | task=L-003 worker=w-l-003-1 attempt=1 repair=False |
| 18:29:17 | chaos_stray_file_committed | task=L-003 |
| 18:29:19 | verified | task=L-003 tests_pass=True stray=['stray.txt'] |
| 18:29:22 | dispatch_requested | task=L-003 worker=w-l-003-2 attempt=2 repair=True |
| 18:29:47 | verified | task=L-003 tests_pass=True stray=[] |
| 18:30:14 | integrated | task=L-003 merge=72ce5fafacc9 |
| 18:30:17 | chaos_supervisor_kill | killed=True why=killed with identity proof |
| 18:30:18 | claimed | task=L-004 |
| 18:30:19 | dispatch_requested | task=L-004 worker=w-l-004-1 attempt=1 repair=False |
| 18:30:21 | supervisor_started |  |
| 18:31:12 | verified | task=L-004 tests_pass=True stray=[] |
| 18:31:51 | integrated | task=L-004 merge=456cdfa9a0c0 |
| 18:32:01 | claimed | task=L-005 |
| 18:32:03 | dispatch_requested | task=L-005 worker=w-l-005-1 attempt=1 repair=False |
| 18:32:40 | dispatch_requested | task=L-005 worker=w-l-005-2 attempt=2 repair=False |
| 18:33:14 | blocked | task=L-005 |

Known harness limits: stall detection (`STALL_SECONDS`) is implemented and unit-tested but no worker stalled; the
supervisor cleanup after a run relies on identity-proven kills; the ledger is a single JSON file under a lock file.
