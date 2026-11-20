# Standing autonomy (T-DEV-028, issue #44)

Status: **IMPLEMENTED, unit-tested offline (synthetic data).** Not live-tested with a supervisor-launched session.

Source: Lukas's standing authority of 2026-11-20. Routine development needs no approval; a short list stays protected.
This extends the existing authority architecture (`authority.py`, `session_policy.py`, `AUTHORITY.md`,
`SESSION_POLICY.md`); it is not a second system.

## The policy
- **Routine (never ask Lukas):** register tasks, dispatch and replace workers, branches, commits, push task/lane
  branches, open PRs, reviews, run the 8 integration gates, merge only when every gate passes, safe recovery, Main's
  own supervisor maintenance.
- **Protected:** paid API, spending, destructive data operations, irreversible production changes (Resolve production
  mutation), disabling safety controls, changing or expanding the authority boundaries (grants, hook settings,
  approvals, the approve command), launchers / `tools/` / `.mcp.json`, publishing outward-facing content.
  No existing denial was weakened.

## What is enforced where
| Layer | Enforced by | What |
|---|---|---|
| Tool classification | `authority.classify_tool_call` | Closed world: shell, read (`Read`, `Glob`, `Grep`, `NotebookRead`, `LS`, path-gated; no path = cwd), write (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`), session-internal (ALLOW), subagent `Agent`/`Task` (`spawn_subagent`, ALLOW), web `WebFetch`/`WebSearch` (`web_read`, ALLOW; paid-API hosts = `paid_api`), publishing (`publish`, REQUIRE_AUTHORIZATION; pure read actions ALLOW), Resolve MCP (as before), `mcp__ide__getDiagnostics` ALLOW / `executeCode` (`ide_execute`, REQUIRE_AUTHORIZATION). Anything else: `unknown_tool`, DENY. |
| Kinds | `AUTONOMOUS_KINDS`, `PROTECTED_KINDS`, `UNCONDITIONAL_KINDS` | Single source of truth. `unknown_tool` and `authority_change` can never be granted. |
| Hook matcher | `session_policy.MATCHER = ".*"` | Every tool call reaches the hook (subagent tool calls run under the same settings). |
| Main | `CS_AUTHORITY_MODE=advisory` | Silence for ALLOW (the normal permission mode decides); deny for DENY; REQUIRE_AUTHORIZATION is queued and denied; an approved identical call gets `ask`. |
| Workers / reviewers | `CS_AUTHORITY_MODE=authoritative` | Explicit `allow` only for non-shell ALLOW and for shell commands whose every segment head is on the routine allowlist; explicit deny for everything else ("not on the worker routine allowlist"). Protected actions are always denied. The result does not depend on the permission mode. |
| Worker confinement | `ConfiguredSupervisor.build_spec` | `CS_AUTHORITY_REPO_ROOT` and `CS_AUTHORITY_ROOTS` are the worker's worktree, not the main checkout. Main keeps `main_roots`. |
| Pending approvals | hook + `<state>/approvals/pending/<id>.json` | id = first 16 hex of sha256(tool name + canonical input); atomic write; dedupe; fields `id, call_hash, created_utc, session_id, role, tool, input_summary` (redacted, truncated)`, rule, reason, status`. A write failure still denies. |
| Human-only approve | `approvals approve <id>` | Refuses when `CS_AUTHORITY_MODE` is set (any hooked session) or stdin is not a TTY; asks to type `APPROVE`; records `APPROVED` + `approved_utc`. The hook denies Main's Write/Edit to the approvals dir, the policy settings dir, `authority_grants.json` and `.claude/settings*.json`, and any shell command that names them or runs `... approve`. |
| Settings | `session_policy.install` | Derived per-session file = copy of `hooks, autoMode, modelSettings, model, statusLine, permissions` from `~/.claude/settings.json` with every `rtk ...` hook removed, plus the authority hook first. `env`, `apiKeyHelper`, `awsAuthRefresh`, `otelHeadersHelper` and every other key are never copied. Missing/unreadable user file: authority hook only. Launch argv: `--settings <file> --setting-sources project,local`. |
| Persistence | `install` on every launch | Fresh, restart, replacement and re-dispatch all reinstall and re-verify (exact hook command executed: force push denied, malformed input denied, `git status` passes: silence in advisory, `allow` in authoritative). |
| Main prompt | `launch.write_main_prompt` | STANDING AUTHORITY section: routine list, protected list, never ask for routine work, protected actions queue, failing gate = stop and report. |

## Worker routine allowlist (authoritative mode)
`git` status/diff/log/show/fetch/pull/rev-parse/ls-files/merge-base, branch (no delete/move/copy/force),
checkout/switch (no force, `-B`, `--`, `.`), add (explicit paths only), commit (no `-a`, `--amend`, `--no-verify`),
worktree list/add, stash list/show/push/apply, remote (`-v`, get-url), config `--get`; global options only `-C` and
`--no-pager`. `gh pr|issue view/list/create/comment` (+ `pr checks/diff`), `gh api` GET only (no `-X` other than GET,
no `-f/-F/--field/--input`). `python [-B] [-I] -m unittest|governance|dev_supervisor[.x]` (not `session_policy`,
`live_autonomy` or `--yes`) and `creator_state` read commands. `ls dir pwd cd cat type head tail wc grep rg`
(no `--pre`) `find` (no `-delete/-exec...`) `sort` (no `-o`) `uniq` (one operand) `diff echo mkdir` (inside the
environment) `test true printf`, `sed -n` (no `-i`, no `w`/`e` commands). `rtk <allowed>` and `rtk bypass <allowed>`.
Any redirection other than `2>&1` / `>/dev/null` / `>$null`, `&`, backticks, `$(`, `${`, `<(`, `>(` is refused.
Secret files and launchers named in any segment are refused. Not on the list: `git push` (Main pushes), `gh pr merge`.
Multi-line commit messages: use several `-m` or `git commit -F <file>`; `$(cat <<EOF ...)` is refused.

## How Lukas approves
1. `python -B -m dev_supervisor.authority approvals --dir <state>/approvals list` in his own terminal (not inside a
   Claude session).
2. `python -B -m dev_supervisor.authority approvals --dir <state>/approvals approve <id>`, type `APPROVE`.
3. When Main repeats the identical call, the hook answers `ask` and Claude Code's native permission prompt asks him to
   confirm. Workers are never offered approved actions.

## NOT verified / limits
- NOT TESTED live: a supervisor-launched Main or worker with the derived settings, `--setting-sources project,local`
  together with `--settings` (flag names taken from the task; not checked against `claude --help` here), the
  `allow` decision skipping prompts in headless worker sessions, and whether hooks fire for subagent tool calls.
- If `--setting-sources project,local` is not honoured as intended, user hooks could run twice (including the RTK
  hook); the derived file would still carry the authority hook.
- In advisory mode Main's shell is still a deny-list over recognised patterns. The boundary check on shell text is a
  substring/regex match: an obfuscated path (variables, encoded strings, a script that writes the file) is not caught.
  The approve CLI's own checks (`CS_AUTHORITY_MODE` set, no TTY in the Bash tool) remain as further barriers.
- The approve path trusts that a process without `CS_AUTHORITY_MODE` and with a TTY is a human. A session that can
  unset its environment and obtain a TTY could bypass it; the native permission prompt is still the final step.
- Dedupe has a small race: two hooks writing the same id at the same moment both write a PENDING record (same content
  apart from time). An APPROVED record is only rewritten if it disappears first.
- The temp directory stays inside every session's environment (pre-existing behaviour), so a worker may write there.
- Tools present in some Claude Code builds but not in the closed table (for example `EnterWorktree`, `ExitWorktree`,
  `BashOutput`, `KillShell`, `ReportFindings`) are denied until they are added to the table deliberately.
- Approval records are reused: once APPROVED, every identical call gets `ask` (still confirmed natively each time).
