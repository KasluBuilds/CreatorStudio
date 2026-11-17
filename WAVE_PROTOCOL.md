# Wave Protocol

How many Claude Code agents work on CreatorStudio **at the same time** with the least administration. Added by task
`T-GOV-004` (2026-11-17). It sits under `PROJECT_OPERATING_PROTOCOL.md` (section 17 points here) and replaces, for waves,
the per-task claim, step-start, lease-renewal and closeout pull requests of sections 6 and 15. **Waves are the normal path
for substantial development work.** Only small, single-agent tasks that are not worth a wave still use those sections.

Labels: **VERIFIED** (observed), **CHECKED** (a tool enforces it), **PROCEDURE** (people follow it; nothing enforces it),
**UNKNOWN** (not tested).

## 1. What GitHub gives us (VERIFIED 2026-11-17)

The repository `KasluBuilds/CreatorStudio` is on a personal account. It was private until 2026-11-17, when Lukas made it
**public**. That changed what GitHub allows:

| Feature | Available? | Evidence |
|---|---|---|
| Branch protection and rulesets | **Yes, since the repository became public** (HTTP 403 while private) | `gh api .../branches/main/protection` answered `404 Branch not protected` (the feature exists, nothing was set) and `.../rulesets` answered `[]` on 2026-11-17 |
| Auto-merge | Off; not needed (the Coordinator merges when gates pass) | `allow_auto_merge: false` |
| GitHub Actions | **Enabled**; free for a public repository | `gh api .../actions/permissions`; `validate` ran green on pull request #28 |
| Secret scanning and push protection | **Enabled** | `gh api repos/KasluBuilds/CreatorStudio` → `security_and_analysis` |
| Merge methods | merge commit, squash and rebase are all allowed | `allow_merge_commit/squash_merge/rebase_merge: true`; "merge commits only" is a PROCEDURE |

**Protection configured on `main`** by the Repository Coordinator immediately after `T-GOV-004` merged (read it back with
`gh api repos/KasluBuilds/CreatorStudio/branches/main/protection`): a pull request is required with 0 approvals (the owner cannot
approve their own pull request); the `validate` check must pass and the branch must be up to date with `main`;
administrators are included; force-pushes and deletion of `main` are refused. `.githooks/pre-push` is an additional local safety net
(`git config core.hooksPath .githooks`; a hook can be skipped). What GitHub still cannot enforce: merge-commit-only, lane ownership
(the validators do), and who may merge (an administrator token can; the rule is that only the Repository Coordinator does, section 3).

**The repository is public.** Everything pushed to any branch, including `wave/` and `lane/` branches and every commit in
history, is world-readable and cannot be recalled. Lanes use synthetic data only, never paste credentials, and never commit media,
local paths or personal data. Secret scanning with push protection is on, and `wave submit` and CI also scan added lines.

## 2. The model in one page

```
main ──────────────────────────────────────────────●── (one merge commit per wave)
  \                                                /
   wave/w01-x  ──●────●──────●──────●─────●──────●   (integration branch; manifest + lane records)
                  \    / \    /      \   /
     lane/w01-x/L1 ●──●   \  /        \ /           each lane: its own worktree, branch, owned paths
     lane/w01-x/L2         ●●          ●
```

- A **wave** is one coordinated batch of large assignments. Its **manifest** `waves/<wave_id>/wave.json` lists the **lanes**.
- A **lane** is one agent's assignment: owner stream, task id, owned paths, test modules, acceptance criteria. Lanes own
  **disjoint** paths (CHECKED).
- Lanes work in **their own worktree and branch** `lane/<wave_id>/<lane_id>`, autonomously, with no approval step.
- A lane delivers with `governance wave submit`. The tool merges the wave branch into the lane, checks scope and tests, and
  pushes to `wave/<wave_id>` with a plain fast-forward push. **The push is the lock**: if another lane pushed first, git
  rejects it and the tool merges again and retries (VERIFIED by a test with a concurrent push).
- The Repository Coordinator verifies the whole wave, runs `wave close`, and opens **one pull request** `wave/<id>` into
  `main`. When every gate passes (section 12) it merges with a merge commit. Nothing else is pushed to `main` for the wave.

## 3. Roles

| Who | Does |
|---|---|
| **Lukas** | Decides what waves exist and the assignments (or delegates planning). Keeps final authority; may reserve or revoke any action. Not asked to approve routine steps. |
| **Repository Coordinator** (one Claude Code session; `PROJECT_OPERATING_PROTOCOL.md` section 18) | Writes and validates the manifest, runs `wave init` and `wave worktrees`, watches `wave status`, runs `wave verify` and `wave close --approver repository_coordinator`, opens and merges the wave pull request when every gate passes, then updates the primary checkout. The only session that merges. |
| **ChatGPT** | Independent reviewer and planner under Lukas's standing delegation; may integrate instead when Lukas asks (`--approver chatgpt`). |
| **Lane agents (Claude Code)** | Do their lane, write `report.json`, run `wave submit`, stop. They never merge, never run `wave close`, never edit the manifest or a shared registry, never push to `main`. |

No lane approves its own work: `COMPLETED` records are written only by `wave close`, which refuses unless an `--approver`, the
standing-instruction flag and a `REPORTED`/`DIRECT` source are given (CHECKED that they are given; PROCEDURE that they are
true). A Coordinator approval is recorded as `approved_by: repository_coordinator` and never claims a ChatGPT or Lukas review.

## 4. Lifecycle

1. **Plan (integrator).** Copy `waves/TEMPLATE/wave.json`. One lane per independent assignment. Give each lane its streams'
   owned paths and test modules. Check it: `cd orchestrator && python -B -m governance wave validate <file>`.
   A lane may touch a package owned by someone else, or an `UNRESOLVED` one, only with an `authorizations` entry whose
   `granted_by` is `lukas` or `integrator` and whose `reference` says where the authorization is recorded.
2. **Start.** `python -B -m governance wave init <file>` creates `wave/<id>` from `origin/main`, writes the manifest and a
   `BRIEF.md` per lane (the lane's full assignment and rules), and pushes the branch. No pull request, no claim.
3. **Fan out.** `python -B -m governance wave worktrees <wave_id>` creates one worktree per lane
   (`..\CreatorStudio-worktrees\<wave_id>-<lane>`). Start one Claude Code session per worktree with the text of its
   `BRIEF.md`. All lanes run concurrently.
4. **Work (lane).** Edit only your owned paths and `waves/<wave_id>/lanes/<lane>/`. Commit explicit paths. Write
   `report.json` (template: `waves/TEMPLATE/lane_report.json`). Commit early; every commit pushed is also your heartbeat.
5. **Deliver (lane).** `cd orchestrator && python -B -m governance wave submit`. It refuses (and pushes nothing) on: dirty
   tree, a merge conflict, a file outside your scope (including the manifest and any registry), a missing or invalid report,
   conflict markers, a credential pattern, or failing test modules. Fix and rerun. Partial progress may be submitted with
   status `IN_PROGRESS`; only `DONE` lanes count as finished.
6. **Watch.** `python -B -m governance wave status` shows each lane: not started, working, STALE, SUBMITTED, and its report status.
7. **Gate (integrator).** On the wave branch: `python -B -m governance wave verify <id> --require-complete`. CI runs the
   same on every push to `wave/**` and on the pull request.
8. **Close (Coordinator).** On a clean wave branch: `python -B -m governance wave close <id> --approver repository_coordinator
   --standing-instruction --date YYYY-MM-DD --source "REPORTED: ..."`. It writes one COMPLETED task and one COMPLETED
   step per lane, a changelog entry, and the regenerated handoff, validates them with the real validators, and commits
   (it never pushes). The source states where the approval came from and must not claim Lukas read the code.
9. **Merge (Coordinator).** Push the wave branch, open one pull request into `main`, wait for `validate` on that exact head,
   run the gates in section 12, then `gh pr merge <n> --merge --match-head-commit <head>`. Fast-forward the primary checkout and remove the
   finished lane worktrees (`git worktree remove`, never `--force` while they hold uncommitted work). Remote branches may be
   deleted later; nothing depends on them once merged.

## 5. Ownership and conflicts

- **Disjoint lanes** (CHECKED at `wave validate`, at `wave init`, and in CI). Two lanes may not own overlapping paths.
- **No shared files.** A lane may not own `ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
  `SESSION_HANDOFF.md`, `PROJECT_STATUS.json`, `CLAUDE.md`, the two protocols, `.github/`, `.githooks/`, `waves/`, `tools/`,
  `Incoming/`, `Projects/`, `test_media/`, launchers or `.mcp.json`. Those records are written only by `wave close`
  (or by a separate integrator change). Lanes never conflict on registries because they never write them.
- **No scope widening.** The manifest is always read from the remote wave branch, never from the lane's copy, and a lane
  may not change it (an edit to `wave.json` by a lane fails `wave submit`).
- **Other work in flight.** `wave init` refuses a lane whose path overlaps an ACTIVE task in `ACTIVE_TASKS.json` on `main`.
  Overlap with another **wave** is not checked automatically (UNKNOWN until two waves run at once); the integrator checks
  `git branch -r --list 'origin/wave/*'` and `wave status` before starting one. Run waves with disjoint paths.
- **Dependencies.** `depends_on` lists lanes. A dependent lane starts from the same wave tip but should wait until its
  dependencies show SUBMITTED, then run `git merge origin/wave/<id>` before building on them. `wave submit` always merges
  the wave tip, so a late dependency is picked up on the next submit.
- **Existing packages** keep their owners in `ACTIVE_TASKS.json` → `package_owners`; a lane for a different owner needs an
  authorization (above). Never claim another stream's package without it.

## 6. Leases and liveness (replaces lease renewal pull requests)

A wave has no lease and needs no renewal. Liveness is read from git: a lane whose branch has had no push for
`inactivity_hours` (default 48) and is not submitted shows **STALE** in `wave status`. A stale lane is handled by the
integrator, with no pull request: reassign it by editing `owner` in the manifest **on the wave branch** (one commit,
visible in history), or drop it by removing the lane before `close`. Never overwrite the previous owner's branch or
worktree: fetch it and continue from it.

## 7. Recoverability and safety

- Nothing is rewritten. No force-push, no reset of shared branches, no deletion of unmerged branches. A lane's commits stay
  on its lane branch even after submit.
- **Bad lane:** the wave branch history shows exactly which merge brought it in. Revert that merge on the wave branch
  (`git revert -m 1 <merge>`) or drop the lane before `close`. Other lanes are unaffected because they own other paths.
- **Bad wave after merge:** revert the single wave merge commit on `main` in one pull request. Evidence SHAs stay reachable
  because merge commits are never squashed.
- **Abandoned wave:** leave `wave/<id>` unmerged and tell Lukas; no registry on `main` was touched, so there is nothing to release.
- **Originals and secrets:** unchanged rules. Lanes use synthetic data only unless Lukas authorizes real footage in the
  manifest's lane brief. `wave submit` and CI scan added lines for credential patterns. The repository still holds no footage.
- **Resolve** stays serialized: at most one lane per wave, and no other active task, may do Resolve mutations; the integrator
  names that lane in its brief. (Not mechanically enforced for waves: PROCEDURE.)

## 8. What this removes, and what stays

| Old step | In a wave |
|---|---|
| Claim pull request per task | none: the manifest is the claim, checked by `wave validate` |
| Step-start pull request per task | none: lanes start from the brief |
| Lease renewal pull requests | none: liveness is read from git (section 6) |
| Closeout pull request per task | none: `wave close` records every lane in the wave pull request |
| ChatGPT review at every step | one review, of the whole wave |
| Merge order and shared-registry conflicts | none: lanes never write shared records |

Stays, because it protects the code: one writer to `main`, exclusive ownership and scope validation, automated tests,
secret scanning, merge commits only, evidence for every claim, Lukas's final authority, and the rule that nothing is VERIFIED
without real-input evidence. A **small, single** task that is not worth a wave may still use sections 6 and 15 of the
operating protocol.

## 9. Test and verification status

- `orchestrator/tests/test_waves.py`: manifest rules, scope, report rules, generated records checked by the real
  `governance` and `tracker_check` validators, and git flows against a throwaway bare origin (init, worktrees, submit,
  concurrent push retry, scope violation, manifest tampering, status/stale, verify, close, pre-push hook).
- `.github/workflows/validate.yml` (VERIFIED 2026-11-17 on pull request #28): the first run failed on four existing ingest tests that compare an 8.3 short temp path (`RUNNER~1`) with the long form; the workflow now sets `TEMP`/`TMP` to the runner's long temp directory and the second run passed (1306 tests, 54 seconds, about 1.5 minutes for the whole job on `windows-latest` with Python 3.14). CI skips 49 tests that run locally (the local run skips 1), so CI does not exercise whatever those need (probably real media tools). GitHub warns that `actions/checkout@v4` and `actions/setup-python@v5` use the deprecated Node 20 and are forced onto Node 24.
- UNKNOWN: the wave gate step in CI (it only runs on `wave/**` branches, so it is first exercised by the first real wave) and whether the suite passes on a Linux runner. Actions minutes are no longer a concern now that the repository is public.
- NOT TESTED: more than two lanes at once, two waves at once, and a lane agent following the brief without human help.
  The first real wave is the real test; start with two or three lanes.

## 10. Quick reference

```powershell
cd <primary checkout>\orchestrator
python -B -m governance wave validate ..\waves\w01-x\draft.json   # integrator: check a manifest
python -B -m governance wave init     ..\waves\w01-x\draft.json   # integrator: create wave/<id>
python -B -m governance wave worktrees w01-x                      # integrator: one worktree per lane
python -B -m governance wave submit                               # lane: deliver (run in the lane worktree)
python -B -m governance wave status                               # anyone: who is where
python -B -m governance wave verify w01-x --require-complete      # integrator: gate before close
python -B -m governance wave close  w01-x --approver repository_coordinator --standing-instruction --date 2026-11-20 --source "REPORTED: ..."
```

## 11. Decisions for Lukas

None of these blocks work; each has a default the Coordinator follows until Lukas says otherwise.

- **D1. Merge methods.** GitHub still allows squash and rebase merges; "merge commit only" is procedure because evidence cites commit
  SHAs. Default: the Coordinator uses merge commits only. Lukas may turn the other two off in the repository settings.
- **D2. Is one wave review enough?** Default: the Coordinator's single verification of a wave approves every lane in it, recorded
  as `approved_by: repository_coordinator`, REPORTED under the standing delegation. If Lukas wants a lane reviewed separately, say so in
  the manifest and split it into its own wave.
- **D3. Reviewer independence.** With the Coordinator as integrator, a wave is reviewed by the same tooling that created it. The
  gates (disjoint ownership, scope, tests, CI, secret scan) are mechanical; there is no second human or ChatGPT read unless Lukas
  asks for one. Default: Coordinator merges when gates pass; Lukas or ChatGPT may review any wave pull request before it merges by
  commenting or reserving it.
- **D4. Admin enforcement.** Protection includes administrators, so even Lukas's own token cannot push to `main` directly; he can
  lift it in the settings when he must.
- **D5. Local enablement.** Each clone runs `git config core.hooksPath .githooks` once (it covers every worktree).
- **D6. Unresolved package owners.** `creator_state`, `media_analysis`, `speaker_attribution` and the cross-package tests stay
  UNRESOLVED; no wave lane may touch them without Lukas's authorization in the manifest.

## 12. Coordinator gate checklist (every merge)

1. `git fetch origin`; `gh pr view <n> --json state,isDraft,mergeable,mergeStateStatus,headRefOid,statusCheckRollup`.
2. State OPEN, not a draft, `MERGEABLE`, `validate` conclusion SUCCESS for `headRefOid`.
3. For a wave: on the wave branch `wave verify <id> --require-complete` clean before `close`, `wave verify <id> --allow-closeout` clean after,
   every lane `DONE`, every `unresolved` entry recorded or sent to Lukas.
4. The changed files are only the lanes' paths, `waves/<id>/`, and the four closeout records (the verify step proves it).
5. Not on the exclusion list of `PROJECT_OPERATING_PROTOCOL.md` section 18 (governance machinery, own implementation, UNRESOLVED
   packages, VERIFIED upgrades, real footage, another session's task in progress).
6. `gh pr merge <n> --merge --match-head-commit <headRefOid>`; confirm `state: MERGED` and the merge commit; fast-forward the primary checkout.
7. Record anything surprising in the next pull request's description or the wave's changelog entry; never silently skip a gate.
