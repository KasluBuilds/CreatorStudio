---
name: github-sync
description: Safely inspect and synchronize a local Git worktree with its GitHub repository, diagnose branch drift, and prepare task branches and pull requests. Use when asked to sync, resume, hand off, or coordinate GitHub work.
---

# GitHub Sync

Use this workflow whenever synchronizing a local checkout with GitHub or preparing work for review.

## 1. Inspect before changing anything

Run and read:
- `git status --short --branch`
- `git branch --show-current`
- `git branch -vv`
- `git log -5 --oneline --decorate`
- `git worktree list`
- `git stash list`

Identify tracked, untracked, ignored, staged, unstaged, ahead, and behind state. Do not assume remote-tracking refs are current.

## 2. Fetch safely

Run `git fetch origin --prune` only when appropriate. Fetch updates remote-tracking metadata; it does not integrate changes into the current branch.

After fetching, compare the current branch with its upstream and inspect divergence. Never equate "behind" with "safe to pull."

## 3. Protect local work

- Never discard, reset, clean, overwrite, or move user data to make synchronization easier.
- Never delete a worktree or branch until its state and purpose are understood.
- Never run `git reset --hard`, `git clean -fd`, force-push, or mass-staging commands as routine sync steps.
- If local changes exist, preserve them and decide whether they belong in the task branch before proceeding.
- Treat ignored directories as potentially important local data; inspect before any cleanup.
- Do not expose credentials, tokens, private URLs with embedded credentials, or machine-specific secrets.

## 4. Use one task branch per task

- Start from the latest intended base commit, normally `origin/main`.
- Use one branch and one worktree per concurrent task.
- Do not commit implementation work directly to `main`.
- Stage explicit file paths, not `git add .` or `git add -A`.
- Keep each task's diff bounded to its owned paths and agreed shared files.

## 5. Review and integrate

Before opening a PR:
- inspect the full diff and changed-file list;
- run the task's targeted tests;
- run broader tests when risk or project policy requires them;
- inspect secrets, generated files, media, local configuration, and unrelated changes;
- record exact commands, results, and limitations.

A PR is not approved merely because CI is green. Review behavior, compatibility, ownership, documentation, and recovery implications.

Merge only when the user has authorized integration or a standing delegation explicitly covers it. Follow the repository's merge policy. Release task leases only through the prescribed closeout workflow.

## 6. Report honestly

State:
- exact base and head commits;
- whether local and remote state are synchronized;
- what changed;
- tests actually run;
- unverified assumptions and blockers;
- whether a PR was opened or merged.

Never claim local access if you only inspected GitHub. Never claim a push, merge, or test succeeded without tool or command evidence.
