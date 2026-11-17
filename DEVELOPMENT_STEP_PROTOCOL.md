# Development Step Protocol

Mandatory for every Claude Code session. Established by task `T-GOV-002`. It sits under `PROJECT_OPERATING_PROTOCOL.md` (which governs branches, claims, leases, merging) and adds one rule: **within each task, work advances one reviewed step at a time; Claude never approves its own step or starts the next one on its own authority.** Who may approve and advance is set out in section 3a.

Labels: **PROCEDURE** = a rule people follow; nothing but review enforces it. **CHECKED** = `dashboard/tracker_check.py` and the tests reject a tracker file that breaks it.

## 1. Where steps live

- `DEVELOPMENT_TRACKER.json` is the canonical record of steps. Each task is split into numbered steps (`S-<task>-00`, `-01`, ...). Step 00 is the claim pull request.
- A step has an id, title, status, acceptance criteria, evidence and a completion record. It links to its task by `task_id`.
- It does **not** repeat what other files own: tasks, owners, leases and paths are in `ACTIVE_TASKS.json`; milestones, weights and progress percentages are in `PROJECT_STATUS.json`. A tracker holding such keys is rejected (CHECKED). Finishing a step never changes progress percentages by itself.

## 2. Statuses and transitions

`PLANNED` -> `IN_PROGRESS` -> `READY_FOR_REVIEW` -> `COMPLETED`.
`READY_FOR_REVIEW` -> `IN_PROGRESS` when the reviewer asks for changes. Any unfinished step may become `BLOCKED` (with a `blocked_reason`); a blocked step returns to `PLANNED` or `IN_PROGRESS`. `COMPLETED` is terminal and its record is never edited. Steps are never deleted. The file alone is always checked for valid states. Whether a change from an earlier version is legal (for example `PLANNED` -> `COMPLETED`, an edit to a `COMPLETED` step, a removed step) is checked **only when a base is supplied**: `python -B dashboard/tracker_check.py --against origin/main` checks every committed `DEVELOPMENT_TRACKER.json` snapshot on the first-parent path from the branch fork point shared with that ref through `HEAD`, in order, then compares the working-tree file with the selected head. In GitHub Actions' pull-request checkout, pass the actual branch ref with `--head origin/<branch>` because the default checkout is a synthetic merge commit. The supplied base ref must be an ancestor of `HEAD` (the branch must be up to date). Finding the fork point avoids comparing an older task-branch snapshot against a newer main snapshot after main has been merged into the branch. This detects illegal intermediate transitions even when a later snapshot would otherwise hide them. On a merge commit whose second parent is already reachable from the supplied base, changes inherited unchanged from that parent are not re-counted as branch transitions; main's own history is already validated, while branch-side changes and merge resolutions remain checked. Without `--against` no history is compared (CHECKED when run with `--against`, PROCEDURE to run it).

## 3. The workflow

1. **One step at a time, per task.** Within each task at most one step is `IN_PROGRESS` or `READY_FOR_REVIEW`; steps run in order, and none starts while an earlier step of the same task is not `COMPLETED` (CHECKED per task). Different tasks may each have one working step at the same time. This changes only the step limit: one task branch and worktree per Claude task, claims and leases (`PROJECT_OPERATING_PROTOCOL.md` section 6), one writer to `main` and one pull request integrated at a time are unchanged.
2. **Start.** Set the step `IN_PROGRESS`. Only start it when the previous step is `COMPLETED` and an authorized party (section 3a) has told Claude to proceed (PROCEDURE).
3. **Stop at `READY_FOR_REVIEW`.** When the acceptance criteria are met, set `READY_FOR_REVIEW`, commit and push the branch, and **stop**. Claude does not start the next step, open a different task, merge anything or declare the step complete.
4. **Report.** The stop report contains: exact files changed; the exact commands run and their results; evidence (commit SHAs, test output); unresolved issues and anything not verified; the branch head and whether the working tree is clean; and whether the terminal is safe to clear.
5. **Review.** ChatGPT reviews the evidence in the repository and either asks for corrections (the step goes back to `IN_PROGRESS`) or gives technical approval (PROCEDURE).
6. **Complete.** Only after an explicit, written technical approval from ChatGPT that names the reviewed commit, a later commit sets `COMPLETED` and fills the completion record. Claude never infers approval from silence, from a green test run or from its own judgment (PROCEDURE).
7. **Continue.** The next step stays `PLANNED` until the approval in step 6 also says to proceed, or until Lukas says so. Without one of those, explicitly given, it stays blocked. There is no silent progression and no task switching: a session stays on its own task and step (PROCEDURE).

## 3a. Who decides what

Lukas has given ChatGPT a standing instruction to make technical acceptance decisions, move automatically between steps once a step is approved, and act as the default integrator: review, open and merge task pull requests and release task leases through follow-up pull requests. This section records how that works, so the protocol and the instruction do not contradict each other.

| Decision | Who |
|---|---|
| Declare a step finished and stop at `READY_FOR_REVIEW` | Claude, which must stop there |
| Technically approve a step and authorize the next one to start | ChatGPT (standing instruction from Lukas), or Lukas |
| Self-approve a step, set one's own step `COMPLETED`, or start the next step unprompted | **Nobody on the Claude side: forbidden** |
| Review, open or merge a task pull request; release its lease (follow-up pull request recording the merge commit) | **ChatGPT under Lukas's explicit standing delegation**; Claude never does these, with one exception: since 2026-11-17 the **Repository Coordinator** (a single named session) may, under the gates and exclusions in `PROJECT_OPERATING_PROTOCOL.md` section 18 (waves and routine pull requests, never governance machinery it wrote itself). Lukas retains final authority and may revoke or reserve any action |
| Change this protocol, milestone weights or progress | A pull request reviewed and merged by ChatGPT under Lukas's explicit delegation; Lukas retains final authority |

ChatGPT's delegated integration authority is not a review by Lukas. A completion record must say which of these it is: its `source` begins with `DIRECT` when Lukas said directly in the session that the step is complete, or `REPORTED` when the confirmation was relayed or comes from the standing instruction. A `REPORTED` source must not claim that Lukas reviewed the code, and must not claim that ChatGPT re-ran tests unless the evidence shows it did (CHECKED for the prefix, PROCEDURE for the honesty of the rest). If ChatGPT's approval cannot be seen in the session, record it as REPORTED and say it was not independently verified.

## 4. The completion gate (CHECKED)

A step may be `COMPLETED` only with: at least one evidence record; a `completion` record with `reviewed_by_chatgpt: true` (or, for work approved by the Repository Coordinator or directly by Lukas, `approved_by` set to `repository_coordinator` or `lukas` and `reviewed_by_chatgpt` false; the record must never claim a ChatGPT review that did not happen), `lukas_confirmed: true` (Lukas's confirmation, which may be his standing instruction, see section 3a), a date and a `source` that begins with `DIRECT` or `REPORTED` and says where the confirmation came from; and every earlier step `COMPLETED`. A step that is not `COMPLETED` may not carry a completion record.

Evidence types: `commit`, `pull_request` (both need a full 40-character SHA), `test_run` (command and result), `observation`, `document`. Tests passing is evidence for review, not completion.

## 5. Terminal safety

"Safe to clear" means: working tree clean, nothing unpushed that matters, no background process, no Resolve operation in flight. State the facts that make it so; if any is false, say "NOT safe to clear" and why.

## 6. Enforcement, honestly

Nothing in GitHub blocks a session from ignoring this protocol: the repository has no branch protection (see `PROJECT_OPERATING_PROTOCOL.md` section 7), although GitHub Actions runs the `validate` workflow and reports its result. What exists: `python -B dashboard/tracker_check.py` validates the current tracker file, and `python -B dashboard/tracker_check.py --against origin/main` also checks every committed tracker snapshot on the first-parent path from the branch fork point shared with `origin/main` through the actual PR head (`--head origin/$HEAD_REF`), in order, plus any working-tree change after the selected head (read-only `git`; exit 0 ok, 1 problems, 2 unusable file, ref or git; if the ref has no tracker file yet it says so and compares nothing, so run `git fetch origin` first and read the line that says what was compared). The history check catches intermediate illegal transitions, not just a base-to-final difference. The workflow passes `--head origin/$HEAD_REF` so the checker inspects the PR branch rather than GitHub's synthetic merge checkout. It first verifies that `origin/main` is an ancestor of that head, then finds the nearest first-parent commit also reachable from `origin/main` as the fork point; this handles a task branch that merged newer main commits after work began. For such merge commits, main-parent changes already validated on `origin/main` are excluded from the branch-side comparison, while branch changes and conflict resolutions are still checked. The `validate` workflow runs this check before the full test suite and secret scan; the reviewer still reads the pull request because the workflow reports status but cannot block a merge. `orchestrator/tests/test_development_tracker.py` tests the CLI and intermediate history behavior and checks that `CLAUDE.md` and `PROJECT_OPERATING_PROTOCOL.md` name this protocol. The review and approval gate in sections 3.5 to 3.7 and 3a is a PROCEDURE.

## 6a. Waves

A wave lane (`WAVE_PROTOCOL.md`) is one task with one tracker step. Its internal progress is the agent's own and is not tracked step by step, so no step-start pull request and no per-step stop for approval apply inside a lane. The agent still never approves itself: a lane's step becomes `COMPLETED` only when the integrator runs `governance wave close` after ChatGPT's single review of the wave pull request, with a `completion` record whose `source` begins `REPORTED` or `DIRECT` (section 3a still applies). The completion gate in section 4 is checked on the generated records by the same validator.

## 7. Change history

2026-11-12: created by `T-GOV-002` step 01. Same day, after review: historical transition checking added to the CLI as `--against REF`; this document now states that it is only checked when a base is supplied.
2026-11-12: `T-GOV-002` step 04 resolved a contradiction. The protocol reserved starting the next step to Lukas alone, while Lukas had given ChatGPT a standing instruction to approve technical acceptance and advance between steps. Section 3a now states who decides what; Claude still stops at `READY_FOR_REVIEW` and never self-approves. The completion gate was not weakened: it additionally requires the `source` to begin with `DIRECT` or `REPORTED`.
2026-11-13: Lukas explicitly delegated routine technical review, task PR opening/merging and lease release to ChatGPT as the default integrator. Section 3a records this standing delegation; Claude remains prohibited from merging PRs or releasing leases, and Lukas retains final authority.
2026-11-13: `T-GOV-003` changed the working-step limit from one per tracker to one per task. `tracker_check.py` now counts `IN_PROGRESS`/`READY_FOR_REVIEW` steps inside each task, so different tasks may each have one; ordering stays sequential within each task. Claims, leases, one writer to `main`, one pull request integrated at a time and Claude stopping at `READY_FOR_REVIEW` are unchanged. T-GOV-003 was claimed and implemented in one pull request as a **one-time bootstrap exception** approved by ChatGPT as delegated integrator (the global limit and two stale authority tests blocked a separate claim pull request); this is not a general workflow and every later task needs its own claim pull request.
2026-11-17: `T-GOV-004` added section 6a: a wave lane is one task with one tracker step, completed by `governance wave close` (`--approver chatgpt|repository_coordinator|lukas`) after one review of the wave pull request (`WAVE_PROTOCOL.md`).

2026-11-17: tracker history validation now walks every committed tracker snapshot on the first-parent path from the supplied base to HEAD. This preserves the transition rules while allowing a one-PR implementation to retain its IN_PROGRESS checkpoint in branch history; no transition is waived.
