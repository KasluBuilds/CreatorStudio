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
| push to `main` | DENY |
| Resolve mutation | needs explicit authorisation; allowed only for a marked synthetic fixture |
| deletion without a recovery path, editing launchers/`.mcp.json`/`tools/` | REQUIRE_AUTHORIZATION |
| terminate/restart a process | only with full identity proof (pid, exe, command line carrying the ownership marker, parent is an owned host, session id) and never a protected pid/session; **a PID alone is rejected** |
| any unknown action kind | DENY (default-deny) |

## Use
- In code: `authority.enforce(Action(kind, target, attrs), PolicyContext(...))` raises `AuthorityDenied`.
- Claude Code sessions: `python -B -m dev_supervisor.authority settings` prints a `hooks.PreToolUse` fragment that runs
  `python -B <path>/authority.py hook`. Context via env: `CS_AUTHORITY_ROOTS`, `CS_AUTHORITY_REPO_ROOT`,
  `CS_AUTHORITY_OWNED_PATHS` (JSON list), `CS_AUTHORITY_GRANTS` (comma list; cannot contain unconditional denials).
- `python -B -m dev_supervisor.authority check --kind paid_api` evaluates one action (exit 2 unless ALLOW).

## Limits (read these)
- Shell text is open-world: the hook is a deny-list over recognised patterns, not a sandbox. Obfuscated commands,
  scripts that do the protected thing internally, and tools it does not classify are not caught.
- A session that can write its own hook settings or env can switch the hook off; the hook protects against mistakes
  and shortcuts, and must be installed by the supervisor into settings the session does not control.
- Resolve MCP verbs are classified by name prefix (`get/list/...` read, everything else mutation): NOT TESTED against live tool names.
