# Session policy installer (T-DEV-025)

`session_policy.py` makes a launched or resumed Claude Code session actually run the authority hook (`AUTHORITY.md`).

## What is implemented and tested
- `install(state_dir, session_id, role, repo_root, ...)` writes `<state>/policy/<role>-<session uuid>.settings.json`
  atomically, reads it back, then **executes the exact hook command** against three probes: it must deny
  `git push --force`, stay silent on `git status`, and deny malformed input. Any failure raises `PolicyInstallError`.
- `argv_with_policy(argv, inst)` inserts `--settings <file>` and refuses an unverified installation, a missing file,
  or an argv that already has `--settings`. The path is deterministic per session, so fresh, resumed and replacement
  launches reuse or recreate the same file. `check_resume(inst, argv)` lets a launcher assert an argv carries it.
- Environment additions: `CS_AUTHORITY_REPO_ROOT`, `CS_AUTHORITY_ROOTS`, optional `CS_AUTHORITY_OWNED_PATHS`; grants
  only when passed explicitly.

## Evidence (labels follow the project convention)
- UNIT_ISOLATED: 16 offline tests; two of them run the real hook command in a local python subprocess.
- OBSERVED live (2026-11-19, claude 2.1.316, model haiku, throwaway git repo without a remote):
  - `python -B -m dev_supervisor.session_policy live-probe --yes`: `git status` ran, `git push --force` was blocked
    with `authority policy [force-push]`, `force_push_ran: false`.
  - A session started with the settings file, then **resumed by id with the same settings file**: force push blocked again.
- NOT TESTED: Write/Edit live behaviour through the installer (covered by the unit tests of `authority.py` only);
  a supervisor-launched interactive Main; replacement flow.

## Launch wiring (T-DEV-027, implemented, unit-tested offline)
- Workers: `ConfiguredSupervisor.build_spec` calls `install(..., "worker", repo_root, roots=[worktree])` and
  `argv_with_policy`; `LaunchSpec.env` carries `inst.env` and `ClaudeCliLauncher` merges it over `provider_env.child_env()`.
  A `PolicyInstallError` makes `_launch` fail the worker (status FAILED, signature `policy_install_failed`, normal
  backoff/BLOCKED rules); nothing is launched, there is no unhooked fallback.
- Main: `InteractiveMainLauncher.open_main` installs with role `main`, roots = exactly: repo root, the sibling
  `CreatorStudio-worktrees` folder, the supervisor store directory, and `~/.claude/projects/<slug>` (slug = repo root
  path with every non-alphanumeric character replaced by `-`, so Main can read supervisor state and its own memory), inserts `--settings`, and passes `inst.env` to `Terminal.open(..., env=)`
  (`NewConsoleTerminal` merges it over the child env). Installation failure raises `LaunchError`; Main is not opened.
- Both paths take an injectable installer (`Services.policy_install`) so tests never start a hook process.
- NOT TESTED live: a supervisor-launched interactive Main and a supervisor-launched worker actually running the hook.

## Review round 1 changes and remaining limits
- Verification now parses the hook output as JSON (`hookSpecificOutput.hookEventName == PreToolUse`, `permissionDecision`)
  instead of matching text. The hook command uses `python -I -B` and every path is double-quoted; paths containing
  shell-significant characters (`; $ ' & ` % ...`) are refused so cmd.exe (used for verification) and Git Bash agree.
- `argv_with_policy` only accepts an installation that `install()` verified in the same process and re-reads the
  settings file, so a forged `verified=True` or a tampered file is refused.
- Limits: the env additions are returned, not bound to the argv: a launcher that forgets to merge `inst.env` loses the
  owned-paths limit (the hook still enforces the unconditional rules). `--settings` is inserted after `argv[0]`, which
  assumes the executable is `claude` itself, not `node cli.js`. Verification runs in cmd.exe; Claude Code's own hook shell
  was observed to run the same command successfully (live probe) but that shell was not independently identified.
