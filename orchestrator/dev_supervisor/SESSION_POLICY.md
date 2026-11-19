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

## Not done (needs the package owner)
`launch.py` does not call this module yet. Until it does, **no supervisor-launched Main or worker has the hook**.
The integration is three lines per launch path (`install`, `argv_with_policy`, merge `env`), plus a startup check that
refuses to launch when `PolicyInstallError` is raised. `launch.py`, `cli.py` and `config.py` currently carry another
session's uncommitted `main-host` changes (worktree `dev-supervisor-main-terminal-host`), so editing them now would collide.
