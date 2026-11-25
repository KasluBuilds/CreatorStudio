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

`unittest` and `node --test` requirements must show at least one executed test: exit 0 with zero tests, no summary line, or every test
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
| timeout (default 1800 s per requirement, `--timeout`) | `failed`, the child process tree is killed |
| the command changed the worktree, or the head moved | `failed`; the remaining commands are `not_run` |
| Ctrl+C | record state `interrupted`, the rest `not_run`; exit 130 |
| the process dies mid-run | record stays `running`; the next check sees a dead pid and reports it as interrupted |

The child gets a minimal environment (no tokens or API keys, no inherited supervisor directory: `CREATORSTUDIO_DEV_SUPERVISOR_DIR` points to a
throw-away directory that is deleted afterwards) and `PYTHONDONTWRITEBYTECODE=1`. Output is scrubbed for credential patterns before it is stored.

## The evidence record

One document per task and head in that subfolder, `tev_<task>_<head12>.json` (the state store's envelope, written atomically, with a `.bak` of the previous version). It holds the task, branch, the **head commit**, the **diff hash**
(the same 16-hex hash the review marker uses), a **hash of the requirement list**, the attempt number, state (`running`, `complete`, `interrupted`),
the runner pid, and per requirement: index, text, kind, argv, directory, start and end time, duration, exit code, test counts, status
(`passed failed skipped blocked not_run`), reason and output tail. A SHA-256 checksum over the whole record is stored in it.

It is accepted only if the checksum is intact and task, head, diff hash and requirement-list hash all equal what is being merged now.
A new commit, a merge of `main` into the branch, an edited requirement or a different diff therefore makes it unusable. Re-running writes a new
attempt for the same head and keeps the previous version as the state store's `.bak`. `--resume` reuses only results that passed for the same head,
diff and requirement list.

Limits, stated plainly: the checksum detects damage and casual edits, not a deliberate forger with access to the state directory (who could also
write a review marker). The record says what was run on this machine; it is not a CI substitute and it does not prove the tests are good tests.

## The `tests` check in `integrate`

`integrate` appends a ninth row, `tests`, after the eight gates. The eight gate functions, `evaluate()` and `cycle.py`'s eight-gate classification are unchanged.
The protocol text (section 18, reserved) still says eight gates; this check is a supervisor-side addition that only makes merging stricter, and the protocol
wording is for Lukas to amend.

| pull request | result |
|---|---|
| registry-only record | PASS: no code to test |
| head branch `wave/...` | PASS, stated as not applicable: lane tests are verified by the wave workflow |
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
- It does not run when nobody calls it. Nothing triggers `testreq run` automatically yet, and `cycle` still reports `ready_to_merge` on the eight gates; the merge is then refused by the ninth.
- `integrate --preflight` and `claim` do not check requirements yet (a claim with a prose requirement is accepted and then fails at merge).
- Wave pull requests are not covered. Full-suite requirements are expensive; write targeted ones.

## Observed use

OBSERVED on the pull request that introduced it (T-DEV-065): the task's own requirements were run with `testreq run` before merge; the result is in
the pull request and in `SESSION_HANDOFF.md`'s record of the task.
