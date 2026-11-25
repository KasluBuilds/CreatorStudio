# Test requirements, evidence and the `tests` check (T-DEV-065)

Status labels as in `WORKER_GATEWAY.md` section 7. Everything here is **IMPLEMENTED and unit-tested**. It has been used by hand
on its own task (see "Observed use" at the end); it has not run unattended.

## What problem it solves

A task in `ACTIVE_TASKS.json` lists `test_requirements`: free-text strings, for example
`cd orchestrator && python -B -I -m unittest discover -s tests -p test_dev_integrate.py`. Before this change nothing read them. Main
ran what it thought was right and said so in a comment, and `integrate` merged on CI, review and scope alone.

Now `dev_supervisor.testreq` reads the strings, runs the ones that are safe to run, stores the result against the exact commit,
and `integrate` refuses to merge a code pull request without a passing record. No new field was added to the registry.

## The requirement strings

Each string is classified (`python -B -m dev_supervisor.testreq plan` prints the result):

| kind | meaning | example |
|---|---|---|
| `command` | matches a fixed grammar; run **without a shell** from a parsed argv | `cd orchestrator && python -B -I -m unittest discover -s tests -p test_x.py` |
| `gate` | prose that an integration gate already decides; recorded as `skipped` ("decided by gate"), never as passed | `independent review of the exact diff`, `integrate --preflight passes on the branch`, `full per-file validation on the exact head` |
| `manual` | anything else; **blocks** the check until the requirement is rewritten as an exact command | `full suite before merge`, `affected suites: ...` |

The command grammar (anything outside it is `manual`):

- `python` with `-B` (and optionally `-I`) running `-m unittest discover -s tests|orchestrator/tests [-p <file>.py]`, `-m unittest tests.test_<name> ...`,
  `-m governance validate [--against origin/main] | handoff --check | scope --base origin/main`, or `dashboard/tracker_check.py --against origin/main`;
- `node --test tests/*.test.js`.
- The directory is `cd orchestrator &&`, `cd dashboard/desktop &&`, a trailing `from orchestrator/`, or `from the repository root`. An optional leading `focused: ` is ignored.
- Pipes, `&&` chains, redirects, `;`, `$`, backticks, parentheses, extra flags and other programs make the string `manual`.

`unittest` and `node --test` requirements must show at least one executed test (node's TAP and default spec reporter lines are both read; OBSERVED on node v24): exit 0 with zero tests, no summary line, or every test
skipped is a **failure**. In the registry at the time of writing, 212 of 269 strings are commands, 45 are gate prose and 12 are manual
(OBSERVED by running the classifier over `ACTIVE_TASKS.json`); the 12 are historical, completed tasks.

## Running

```powershell
cd <task worktree>\orchestrator
python -B -m dev_supervisor.testreq plan   --task T-DEV-065        # classification only
python -B -m dev_supervisor.testreq run    --task T-DEV-065        # run and store the evidence
python -B -m dev_supervisor.testreq run    --task T-DEV-065 --resume   # keep results that already passed for this exact head
python -B -m dev_supervisor.testreq show   --task T-DEV-065        # the record, with output tails of failures
python -B -m dev_supervisor.testreq check  --task T-DEV-065        # the verdict the merge check would give for the current head
```

`--task` defaults to the task whose branch is checked out. The requirement list is read from `origin/main`, the same source `integrate`
uses, not from the worktree. Evidence is stored in the `test_evidence` subfolder of the supervisor state directory (`--state-dir`, default as for the Supervisor; a subfolder so that the Supervisor's own files are never touched), never in the repository.
Exit codes: 0 passed, 1 not passed, 2 refused (usage, unreadable registry, run already in progress), 130 interrupted.

A run refuses or fails as follows:

| situation | result |
|---|---|
| worktree has uncommitted changes | every command `blocked` (the evidence would not describe the commit) |
| runner (`node`) not found, process cannot start, directory missing | `blocked`, with the reason |
| non-zero exit | `failed`; the record keeps the exit code, the failing test names and the last 4000 characters of output |
| no tests ran, or all skipped | `failed` |
| timeout (default 1800 s per requirement, `--timeout`) | `failed`, the child process tree is killed (`taskkill /T` on Windows, OBSERVED to work; a process group kill on POSIX, written but NOT TESTED) |
| the command changed the worktree, or the head moved | `failed`; the remaining commands are `not_run` |
| Ctrl+C | record state `interrupted`, the rest `not_run`; exit 130 |
| the process dies mid-run | record stays `running`; the next check sees a dead pid and reports it as interrupted |
| a second `run` while the record says `running` and that pid is alive | refused; a reused process id looks the same, so `--ignore-running` overrides it when you know the old run is dead (there is no lock: two simultaneous starts can both begin, and both outcomes fail closed) |

The child gets a minimal environment (no tokens or API keys, no inherited supervisor directory: `CREATORSTUDIO_DEV_SUPERVISOR_DIR` points to a
throw-away directory that is deleted afterwards) and `PYTHONDONTWRITEBYTECODE=1`. Output is scrubbed for credential patterns before it is stored.

## The evidence record

One document per task and head in that subfolder, `tev_<task>_<head12>.json` (the state store's envelope, written atomically, with a `.bak` of the previous version). It holds the task, branch, the **head commit**, the **diff hash**
(the same 16-hex hash the review marker uses), a **hash of the requirement list**, the attempt number, state (`running`, `complete`, `interrupted`),
the runner pid, and per requirement: index, text, kind, argv, directory, start and end time, duration, exit code, test counts, status
(`passed failed skipped blocked not_run`), reason and output tail. A SHA-256 checksum over the whole record is stored in it.

It is accepted only if the checksum is intact and task, head, diff hash and requirement-list hash all equal what is being merged now.
A new commit, a merge of `main` into the branch, an edited requirement or a different diff therefore makes it unusable. Reading is pure: nothing is created, no temp file is removed and a damaged file is neither quarantined nor rewritten (the backup is read instead).
Re-running writes a new
attempt for the same head and keeps the previous version as the state store's `.bak`. `--resume` reuses only results that passed for the same head,
diff and requirement list.

Limits, stated plainly: the checksum is not keyed. It detects damage and casual edits, not a deliberate forger with access to the state directory (who could also
write a review marker). The tests being run are the branch's own code and keep `LOCALAPPDATA`/`USERPROFILE`, so a malicious test could compute the evidence path and write a valid-looking record; the independent review of the diff is what guards against that. The record says what was run on this machine; it is not a CI substitute and it does not prove the tests are good tests.

## The `tests` check in `integrate`

`integrate` appends a ninth row, `tests`, after the eight gates. The eight gate functions, `evaluate()` and `cycle.py`'s eight-gate classification are unchanged.
The protocol text (section 18, reserved) still says eight gates; this check is a supervisor-side addition that only makes merging stricter, and the protocol
wording is for Lukas to amend.

| pull request | result |
|---|---|
| registry-only record | PASS: no code to test |
| head branch `wave/...` | **FAIL**: lane test evidence is not supported yet, so a wave merge through `integrate` needs an explicit decision (a branch name alone never earns a pass) |
| code | needs exactly one active task for the branch; every requirement must be a command or gate prose; every command needs a passed result in a complete record for this exact head and diff |
| anything unreadable | UNKNOWN (blocks) |

A failing row names the requirement and the command to run, for example: `no test evidence for head 0209709abcde (missing); run: python -B -m dev_supervisor.testreq run --task T-X-001`.
The row is evaluated before the lock and again under it, so evidence that disappears or goes stale in between refuses the merge. It is recorded in the
integration comment's gate table and in the `integrate` event. `--yes` merges nothing on a failing or unknown row.

## Workflow for Main

1. Claim and implement as before; commit (the worktree must be clean).
2. `testreq run` in the worktree. Fix and re-commit if it fails; any new commit needs a new run (`--resume` skips what already passed for the same head only).
3. Publish with `intake`, get the independent review, then `integrate <pr>` (dry run) shows the `tests` row; `integrate <pr> --yes` merges when all nine rows pass.

If a task's requirement is prose that is not in the table, rewrite it in the claim as an exact command before the work starts (`plan` shows `MANUAL`).

## What this does not do

- It does not choose which tests a task needs: the requirement list is the task author's decision. A task that lists only `governance validate` is satisfied by that.
- It runs by itself only for a finished worker and only in a Supervisor process that loaded the code of T-DEV-073 and T-DEV-074 (next section); every other run is a person or Main calling it. `cycle` reports `needs_tests` (T-DEV-067) when the eight gates pass and this check does not.
- `claim` refuses prose requirements (T-DEV-066, produced by a dispatched worker). `integrate --preflight` does not run the check; use `testreq check`.
- Wave pull requests are refused by this check, not covered by it (no wave is open today). Full-suite requirements are expensive; write targeted ones.

## Observed use

OBSERVED on 2026-11-25 (UTC), recorded in `WORKER_GATEWAY.md` section 13: the runner decided the merge of PRs #225, #230, #231 (a worker's change), #235 and #236, each with nine passing rows and a row 9 naming the head and diff. While it was built the classifier found 212 commands, 45 gate prose and 12 manual requirements in the registry; running a task's requirements from `orchestrator/` exposed a wrong working directory; the full `test_dev_*` run and CI exposed one launch-path audit test (T-DEV-068); and independent review of the first head found a wave exemption, a node reporter mismatch and an impure read path, all fixed before merge. It has not run unattended, and a single machine has produced all the evidence.

## Supervisor-triggered runs (T-DEV-073, T-DEV-074; IMPLEMENTED and unit-tested, NOT DEMONSTRATED in the live Supervisor)

`dev_supervisor/worker_tests.py` (`WorkerTestScanner.scan`) is called once per Supervisor tick, after the worker monitor pass (`CREATORSTUDIO_WORKER_TESTS=0` turns it off). It never runs a test command itself: it starts the existing `testreq run` as a child process (`testreq.spawn_run`: fixed argument list, minimal environment, own process group, no window, output to the null device) and reads the result from the sealed evidence record.

**Who is tested.** A worker with role `worker`, status EXITED, exit code 0, a `task/` branch, that ended after the feature was first enabled (`enabled_since` in the state document, so none of the historical workers is touched) and within 48 hours, whose task is ACTIVE or UNDER_REVIEW on `origin/main` (as of the last fetch; the scanner never fetches).

**Checks before a run, repeated on every pass.** The worktree exists, is clean and on the registered branch; the branch has a commit ahead of `origin/main`; the requirement list is not empty, has no manual requirement and has at least one command (a task with gate prose only is BLOCKED, never PASSED). A cause that clears is picked up by the next pass.

**States** (document `worker_tests` in the Supervisor state directory, one entry per worker): `PENDING` (another run is live, or free memory is below the floor), `RUNNING`, `PASSED`, `FAILED`, `BLOCKED` (with the reason). `PASSED` and `FAILED` come only from `testreq.check_evidence` for the exact head, diff hash and requirement list; a record for another head or diff, or a damaged one, never counts, a stored verdict is evaluated again when the record's checksum changes (a manual passing run replaces a recorded failure), and `skipped` is never a pass. Events `worker_tests_started|passed|failed|blocked|interrupted` are appended when state or detail changes; a pass produces no event on later ticks.

**Safe against duplicates and restarts.** The launch is written to the document before the process starts and the pid right after. A live runner of any entry holds the single slot whatever state the entry is in (a moved head or a dirty worktree does not free it); a runner whose evidence record is complete is finished even if its pid now belongs to another process; a runner older than `timeout x commands + 300 s per command + 900 s` is not trusted (it is never killed). An unrecorded launch counts as live for 60 s and afterwards only if the evidence store shows a running record with a live pid. A dead runner with an incomplete record is relaunched with `--resume`; at most three launches in all (attempts, the first included) are made per task and head (counted per task and head, not per worker), then BLOCKED with the manual command (`--ignore-running` when the old process is known to be dead). A failed start is not retried for 300 s and uses no attempt. An exception in one worker's evaluation is recorded as BLOCKED for that worker only.

**What shows it.** `autonomy next` offers `worker_tests_failed` (FAILED and BLOCKED entries, before `worker_failed`) and `worker_tests_passed` (next step: `intake`, until `origin/<branch>` exists), and notes RUNNING and PENDING entries as waiting; entries of a COMPLETED or RELEASED task or of a head the worktree has left are not offered. `cycle` reports `worker_testing` and `worker_tests_failed` before a pull request exists.

**Observed.** Offline end-to-end run on 2026-11-25 (OBSERVED, script not in the repository): a real temporary git repository with an origin, a registry with one task, a worker record EXITED 0, the real scanner and the real `testreq` child process. The first pass started the runner; 3.4 s later the entry was PASSED for the exact head with a sealed record; repeated passes started no second runner and appended no event (events: started, passed once each); a later failing commit was tested as its own head and gave FAILED with the failing test, the command and the `testreq show` hint. **This is not the live Supervisor.** The Supervisor process running on the owner's machine loaded its code before these tasks merged: until its owner restarts it, no scan happens (`autonomy next` and `cycle` already show the document when it exists). Nothing is published, reviewed or merged by any of this.

**Known limits.** The runner's output goes to the null device, so when a runner refuses before writing a record the reason is lost (T-DEV-077 prepared). The wall-clock budget also applies to a runner this process started (after a long machine sleep a healthy run can look dead; a second run is then refused by the first one's pid check or waits). A start that keeps failing appends one BLOCKED event per 5 minutes for up to 48 hours. Successful runs count towards the three attempts, so three requirement edits on one head end BLOCKED with an "interrupted" wording. The POSIX process-group handling of the child is NOT TESTED.
