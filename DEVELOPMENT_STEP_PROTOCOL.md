# Development Step Protocol

Mandatory for every Claude Code session. Established by task `T-GOV-002`. It sits under `PROJECT_OPERATING_PROTOCOL.md` (which governs branches, claims, leases, merging) and adds one rule: **work advances one reviewed step at a time, and only Lukas starts the next step.**

Labels: **PROCEDURE** = a rule people follow; nothing but review enforces it. **CHECKED** = `dashboard/tracker_check.py` and the tests reject a tracker file that breaks it.

## 1. Where steps live

- `DEVELOPMENT_TRACKER.json` is the canonical record of steps. Each task is split into numbered steps (`S-<task>-00`, `-01`, ...). Step 00 is the claim pull request.
- A step has an id, title, status, acceptance criteria, evidence and a completion record. It links to its task by `task_id`.
- It does **not** repeat what other files own: tasks, owners, leases and paths are in `ACTIVE_TASKS.json`; milestones, weights and progress percentages are in `PROJECT_STATUS.json`. A tracker holding such keys is rejected (CHECKED). Finishing a step never changes progress percentages by itself.

## 2. Statuses and transitions

`PLANNED` -> `IN_PROGRESS` -> `READY_FOR_REVIEW` -> `COMPLETED`.
`READY_FOR_REVIEW` -> `IN_PROGRESS` when the reviewer asks for changes. Any unfinished step may become `BLOCKED` (with a `blocked_reason`); a blocked step returns to `PLANNED` or `IN_PROGRESS`. `COMPLETED` is terminal and its record is never edited. Steps are never deleted. The file alone is always checked for valid states. Whether a change from an earlier version is legal (for example `PLANNED` -> `COMPLETED`, an edit to a `COMPLETED` step, a removed step) is checked **only when a base is supplied**: `python -B dashboard/tracker_check.py --against origin/main` compares the file with the one committed at that ref. Without `--against` no history is compared (CHECKED when run with `--against`, PROCEDURE to run it).

## 3. The workflow

1. **One step at a time.** At most one step is `IN_PROGRESS` or `READY_FOR_REVIEW`; steps run in order, and none starts while an earlier one is not `COMPLETED` (CHECKED).
2. **Start.** Set the step `IN_PROGRESS`. Only start it when Lukas has said to (PROCEDURE).
3. **Stop at `READY_FOR_REVIEW`.** When the acceptance criteria are met, set `READY_FOR_REVIEW`, commit and push the branch, and **stop**. Claude does not start the next step, open a different task, merge anything or declare the step complete.
4. **Report.** The stop report contains: exact files changed; the exact commands run and their results; evidence (commit SHAs, test output); unresolved issues and anything not verified; the branch head and whether the working tree is clean; and whether the terminal is safe to clear.
5. **Review.** ChatGPT reviews the evidence in the repository and tells Lukas whether the step is complete (PROCEDURE).
6. **Complete.** Only after Lukas explicitly says the step is complete or to continue, a later commit sets `COMPLETED` and fills the completion record. Claude never infers approval from silence, from a green test run or from ChatGPT's opinion alone (PROCEDURE).
7. **Continue.** The next step stays `PLANNED` and blocked until Lukas explicitly says to continue. There is no automatic task switching and no silent progression (PROCEDURE).

## 4. The completion gate (CHECKED)

A step may be `COMPLETED` only with: at least one evidence record; a `completion` record with `reviewed_by_chatgpt: true`, `lukas_confirmed: true`, a date and a `source` that says where Lukas's confirmation came from (mark it REPORTED if it was relayed rather than seen); and every earlier step `COMPLETED`. A step that is not `COMPLETED` may not carry a completion record.

Evidence types: `commit`, `pull_request` (both need a full 40-character SHA), `test_run` (command and result), `observation`, `document`. Tests passing is evidence for review, not completion.

## 5. Terminal safety

"Safe to clear" means: working tree clean, nothing unpushed that matters, no background process, no Resolve operation in flight. State the facts that make it so; if any is false, say "NOT safe to clear" and why.

## 6. Enforcement, honestly

Nothing in GitHub blocks a session from ignoring this protocol (the repository has no branch protection or CI, see `PROJECT_OPERATING_PROTOCOL.md` section 7). What exists: `python -B dashboard/tracker_check.py` validates the tracker file, and `python -B dashboard/tracker_check.py --against origin/main` also checks the history against the tracker committed at that ref (read-only `git`; exit 0 ok, 1 problems, 2 unusable file, ref or git; if the ref has no tracker file yet it says so and compares nothing, so run `git fetch origin` first and read the line that says what was compared). Nothing runs it automatically before a merge, and it can only compare against a ref that already holds the earlier state, so a completion record that was written without Lukas's word is caught by review, not by the tool; `orchestrator/tests/test_development_tracker.py` runs it on every full-suite run and checks that `CLAUDE.md` and `PROJECT_OPERATING_PROTOCOL.md` name this protocol; the reviewer reads the pull request. The human gate in sections 3.5 to 3.7 is a PROCEDURE.

## 7. Change history

2026-11-12: created by `T-GOV-002` step 01. Same day, after review: historical transition checking added to the CLI as `--against REF`; this document now states that it is only checked when a base is supplied.
