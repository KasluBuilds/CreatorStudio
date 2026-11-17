# Wave Protocol

How many Claude Code agents work on CreatorStudio **at the same time** with the least administration. Added by task
`T-GOV-004` (2026-11-17). It sits under `PROJECT_OPERATING_PROTOCOL.md` (section 17 points here) and replaces, for waves,
the per-task claim, step-start, lease-renewal and closeout pull requests of sections 6 and 15. Tasks that are not part of
a wave still use those sections unchanged.

Labels: **VERIFIED** (observed), **CHECKED** (a tool enforces it), **PROCEDURE** (people follow it; nothing enforces it),
**UNKNOWN** (not tested).

## 1. What GitHub gives us (VERIFIED 2026-11-17)

The repository is private and belongs to a personal account (`KasluBuilds`) without a paid plan.

| Feature | Available? | Evidence |
|---|---|---|
| Branch protection, rulesets, required reviews or checks, merge queue | **No** | `gh api .../branches/main/protection` and `.../rulesets` answer HTTP 403 "Upgrade to GitHub Pro or make this repository public" |
| Auto-merge | Off, and not useful without required checks | `allow_auto_merge: false` |
| GitHub Actions | **Enabled** (`allowed_actions: all`); no workflow existed before this task | `gh api .../actions/permissions` |
| Pull requests, merge commits, status shown on a PR | Yes | in daily use |

Consequences, stated honestly: nothing on GitHub can stop a push to `main` or a merge with a red check. The single
writer to `main` stays a PROCEDURE. What this system adds is (a) a CI workflow that reports a status on every pull request
(`.github/workflows/validate.yml`), (b) a local `pre-push` hook that refuses pushes to `main` and force-updates or
deletions of `wave/` and `lane/` branches (`.githooks/pre-push`, enabled with `git config core.hooksPath .githooks`; a
hook can be skipped, so it is a safety net), and (c) a validator that makes ownership conflicts visible before work starts.
Lukas can upgrade the plan (Decision D1, section 11) to make (a) blocking.

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
- The integrator verifies the whole wave, runs `wave close`, and opens **one pull request** `wave/<id>` into `main`.
  ChatGPT reviews once and merges with a merge commit. Nothing else is pushed to `main` for the wave.

## 3. Roles

| Who | Does |
|---|---|
| **Lukas** | Decides what waves exist and the assignments (or delegates planning to ChatGPT). Keeps final authority; may reserve any merge. |
| **ChatGPT (integrator)** | Writes the manifest, runs `wave init`, reviews the wave pull request once, runs `wave close` with the approval, merges. Under Lukas's standing delegation (section 3a of `DEVELOPMENT_STEP_PROTOCOL.md`). |
| **Lane agents (Claude Code)** | Do their lane, write `report.json`, run `wave submit`, stop. They never merge, never run `wave close`, never edit the manifest or a shared registry. |

Claude never approves its own work: `COMPLETED` records are written only by `wave close`, which the integrator runs after
approving, and which refuses to run unless the approval flags and a `REPORTED`/`DIRECT` source are given (CHECKED that
they are given; PROCEDURE that they are true).

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
8. **Close (integrator).** On a clean wave branch: `python -B -m governance wave close <id> --chatgpt-approved
   --standing-instruction --date YYYY-MM-DD --source "REPORTED: ..."`. It writes one COMPLETED task and one COMPLETED
   step per lane, a changelog entry, and the regenerated handoff, validates them with the real validators, and commits
   (it never pushes). The source states where the approval came from and must not claim Lukas read the code.
9. **Merge.** Push the wave branch, open one pull request into `main`, wait for the `validate` check, merge with a merge
   commit (`gh pr merge <n> --merge`). Delete the merged lane and wave branches afterwards (Lukas, or the integrator).

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

Stays: one writer to `main`, merge commits only, tests green before merge, evidence for every claim, Lukas's final
authority, and the rule that nothing is VERIFIED without real-input evidence. A **small, single** task that is not worth a
wave may still use sections 6 and 15 of the operating protocol.

## 9. Test and verification status

- `orchestrator/tests/test_waves.py`: manifest rules, scope, report rules, generated records checked by the real
  `governance` and `tracker_check` validators, and git flows against a throwaway bare origin (init, worktrees, submit,
  concurrent push retry, scope violation, manifest tampering, status/stale, verify, close, pre-push hook).
- `.github/workflows/validate.yml` (VERIFIED 2026-11-17 on pull request #28): the first run failed on four existing ingest tests that compare an 8.3 short temp path (`RUNNER~1`) with the long form; the workflow now sets `TEMP`/`TMP` to the runner's long temp directory and the second run passed (1306 tests, 54 seconds, about 1.5 minutes for the whole job on `windows-latest` with Python 3.14). CI skips 49 tests that run locally (the local run skips 1), so CI does not exercise whatever those need (probably real media tools). GitHub warns that `actions/checkout@v4` and `actions/setup-python@v5` use the deprecated Node 20 and are forced onto Node 24.
- UNKNOWN: the wave gate step in CI (it only runs on `wave/**` branches, so it is first exercised by the first real wave), whether the suite passes on a Linux runner, and the monthly Actions minutes it consumes on the free plan (Windows runners are billed at a higher multiplier).
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
python -B -m governance wave close  w01-x --chatgpt-approved --standing-instruction --date 2026-11-20 --source "REPORTED: ..."
```

## 11. Decisions for Lukas

- **D1. Plan upgrade.** GitHub Pro (about 4 USD/month at the time of writing; check the current price) would give branch
  protection with required status checks. Until then the single writer to `main` and the green-check rule are procedure.
  Making the repository public would also unlock protection but is **not recommended** (it holds private design).
- **D2. Is one wave review enough?** This protocol lets ChatGPT's single review of a wave pull request approve every lane
  in it (as REPORTED, under the standing delegation). If Lukas wants any lane reviewed separately, say so in the manifest brief
  and split it into its own wave.
- **D3. Bootstrap.** `T-GOV-004` itself had to be claimed and delivered the old way, in one pull request (the same
  one-time exception `T-GOV-003` used). It needs the integrator's approval of that exception.
- **D4. Shared-registry agreement.** `AGR-GOV-REGISTRY-01` was extended to `T-GOV-004` for this pull request only.
- **D5. First wave.** Which assignments become lanes, and who plays integrator, is Lukas's call.
- **D6. Local enablement.** Lukas (or each session once) runs `git config core.hooksPath .githooks`.
