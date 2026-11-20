# Authority policy (T-DEV-024)

Status: **IMPLEMENTED, unit-tested offline (synthetic data).** Not installed into any session yet; not live-tested.

Source of authority: Lukas's mission message of 2026-11-19 (delegated autonomy). `authority.py` turns it into code.

## Autonomous (no approval)
read, run tests, edit files inside the task's owned paths, create branches and worktrees, dispatch/recover workers,
restart or resume processes the environment owns, routine git (commit, push of task/lane branches), create PRs,
merge **only** with every gate PASS plus an independent review and no admin bypass, update records.

## Protected
| Kind | Verdict |
|---|---|
| purchase, secret disclosure, original/`Incoming/` mutation, security-control bypass (`--no-verify`, `--admin`, hooks/gpg config), force push, history rewrite, `git add .`/`-A`, outside the project environment | DENY, no grant can lift it |
| paid API | DENY while `allow_paid_api` is false; even when true it needs an explicit grant |
| push to `main` (any refspec naming main, `--all`, `--mirror`) | DENY |
| Resolve mutation | needs explicit authorisation; allowed only for a marked synthetic fixture |
| deletion without a recovery path, editing launchers/`.mcp.json`/`tools/` | REQUIRE_AUTHORIZATION |
| terminate/restart a process | only with full identity proof (pid, exe, command line carrying the ownership marker, parent is an owned host, session id) and never a protected pid/session; **a PID alone is rejected** |
| any unknown action kind | DENY (default-deny) |
| any tool outside the closed tool table, another MCP server, a malformed tool name (`unknown_tool`) | DENY, no grant can lift it |
| approvals dir, policy settings dir, `authority_grants.json`, `.claude/settings*.json`, `... approve` in shell (`authority_change`) | DENY, no grant can lift it |
| publishing (Artifact, Docs writes, DesignSync, SendToProject*, PushNotification, RemoteTrigger, CronCreate/Delete) | REQUIRE_AUTHORIZATION (pure reads ALLOW) |
| `mcp__ide__executeCode` | REQUIRE_AUTHORIZATION |

## Standing autonomy (T-DEV-028)
See `STANDING_AUTONOMY.md`. In short: the tool table is closed-world (`classify_tool_call`); the hook runs for every
tool (matcher `.*`); Main's hook is `advisory`, workers' hook is `authoritative` (explicit allow only for the routine
allowlist, explicit deny otherwise); REQUIRE_AUTHORIZATION is queued as a pending approval in `<state>/approvals` and
denied; only a call a human approved in their own terminal gets `ask` (Main only), confirmed in Claude Code's native
prompt. New autonomous kinds: `spawn_subagent`, `web_read`. New protected kinds: `publish`, `ide_execute`,
`unknown_tool`, `authority_change`. `UNCONDITIONAL_KINDS` lists the protected kinds no grant can lift.

## Use
- In code: `authority.enforce(Action(kind, target, attrs), PolicyContext(...))` raises `AuthorityDenied`.
- Claude Code sessions: `python -B -m dev_supervisor.authority settings` prints a `hooks.PreToolUse` fragment (matcher
  `.*`) that runs `python -B <path>/authority.py hook`. Context via env: `CS_AUTHORITY_ROOTS`, `CS_AUTHORITY_REPO_ROOT`,
  `CS_AUTHORITY_OWNED_PATHS` (JSON list), `CS_AUTHORITY_GRANTS` (comma list; cannot contain unconditional denials),
  `CS_AUTHORITY_MODE` (`advisory` default / `authoritative`), `CS_AUTHORITY_ROLE`, `CS_AUTHORITY_APPROVALS_DIR`,
  `CS_AUTHORITY_BOUNDARY_PATHS` (hook script, live authority code, user settings; never written or named in shell),
  `CS_AUTHORITY_GH_REPO` (own `owner/name` for a worker's `gh --repo/-R`; default `KasluBuilds/CreatorStudio`).
- `python -B -m dev_supervisor.authority approvals [--dir D] [--config F] list|approve <id>`: `--dir` defaults to
  `$CS_AUTHORITY_APPROVALS_DIR`, else `<supervisor state dir>/approvals` (state dir from the same configuration
  loading as `python -m dev_supervisor`). list is read-only; approve is for a human terminal only (refused when
  `CS_AUTHORITY_MODE` is set or stdin is not a TTY).
- `python -B -m dev_supervisor.authority check --kind paid_api` evaluates one action (exit 2 unless ALLOW).

## Limits (read these)
- A bare `git push` or `git push origin HEAD` while on `main`, and `git push --repo=origin main`, cannot be recognised from command text; rely on branch protection and the
  merge gates for that case. `authority_grants.json` is only a path the policy refuses to write; nothing reads it yet.
- Commands wrapped by `&`, `cmd /c`, `bash -c`, `env X=1 ...` are not unwrapped (only `rtk <cmd>` and `rtk
  bypass|test|err|summary|run|proxy <cmd>` are, since review round 2, and since T-DEV-029 also behind rtk global
  flags such as `-v`, `-u`, `--skip-env`); quoted text containing
  `;` or `|` is split like a command; `git reset --hard`, `git branch -D` and `gh api --method DELETE` are not classified.
- Reads of secrets via `printenv KEY`, a bare `$env:KEY`, `grep` on `.env` or the `Grep` tool are not caught in
  advisory mode (in authoritative mode a worker shell segment naming a secret file is refused).
- Shell text is open-world: in advisory mode (Main) the hook is a deny-list over recognised patterns, not a sandbox
  (since review round 1 it also catches abbreviated `--no-verify`/`--no-gpg-sign`/`--force`/`--mirror`/`--delete`/
  `--all` and root pathspecs such as `./`, `:`, `:/`, `x/..` for `git add`). In authoritative mode (workers) it is a
  STRUCTURAL rule since review round 2: one simple Bash command with no shell metacharacters and no `cd`, then a
  closed allowlist of command, subcommand, exact options and path arguments (STANDING_AUTONOMY.md). Scripts that do the protected thing internally (for example test code run by the allowed `python -m
  unittest`) are not caught.
- A session that can write its own hook settings or env can switch the hook off; the hook protects against mistakes
  and shortcuts, and must be installed by the supervisor into settings the session does not control.
- Resolve MCP tools are classified by their NAME (`get_*/list_*/...` read, everything else mutation). A neutrally
  named tool whose `action`/`method` field names a read is silent in advisory mode and denied for workers. NOT TESTED
  against live tool names.
