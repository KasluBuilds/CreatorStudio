# Standing autonomy (T-DEV-028, issue #44)

Status: **IMPLEMENTED, unit-tested offline (synthetic data); security review round 1 and round 2 fixes applied.** Not live-tested with a supervisor-launched session.

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
| Workers / reviewers | `CS_AUTHORITY_MODE=authoritative` | Explicit `allow` only for non-shell ALLOW and for ONE simple Bash command that passes the structural worker shell rule below (the PowerShell tool is never allowed); explicit deny for everything else ("not on the worker routine allowlist"). Protected actions are always denied. The result does not depend on the permission mode. |
| Worker confinement | `ConfiguredSupervisor.build_spec` | `CS_AUTHORITY_REPO_ROOT` and `CS_AUTHORITY_ROOTS` are the worker's worktree, not the main checkout. Main keeps `main_roots`. |
| Authority boundary (review B5) | `touches_boundary_path` / `touches_boundary_command` | Approvals dir, policy dir, grants file, `.claude/settings*.json`, and now the live `authority.py` and `session_policy.py` (the hook script of the running hook), plus `CS_AUTHORITY_BOUNDARY_PATHS` (hook script, user settings file) set by `session_policy`. Write/Edit to them and shell commands naming them (absolute, or relative to the cwd, the repo root or any `cd` target in the command; `./` and `//` spellings normalised; a glob that could match one; a directory-level `git checkout`/`git restore` that contains one; review round 2 N3) are `authority_change` in both modes. Reading them with the Read tool stays allowed. Writes into any `.git` directory are denied (`git-internals`). |
| Read tools (review N2, N3) | `classify_tool_call` | `Read`/`NotebookRead` without a path are denied. Absolute `Glob` patterns are gated by their literal directory; `..`, `~`, drive-relative and provider patterns are outside the environment. A `Grep` `glob` that targets secret names (`.env*`, `*.pem`, `.*` ...) is secret disclosure. Drive-relative `C:x`, provider and `~` paths are denied everywhere. `SECRET_NAME` also covers `.git-credentials`, `.claude.json`, `gh/hosts.yml`. |
| Resolve (review N4) | `resolve_name_is_read` | Read status comes from the tool NAME. A neutrally named tool whose `action` names a read is silent in advisory mode and denied for workers; a mutating name is a mutation whatever the input says. |
| Project settings (review N6) | `session_policy.check_project_settings` | `install` and `argv_with_policy` refuse a launch cwd whose `.claude/settings.json` / `settings.local.json` holds `disableAllHooks` or hooks (or cannot be read). |
| Pending approvals | hook + `<state>/approvals/pending/<id>.json` | id = first 16 hex of sha256(tool name + canonical input); atomic write; dedupe; fields `id, call_hash, created_utc, session_id, role, tool, input_summary` (redacted, truncated)`, rule, reason, status`. A write failure still denies. |
| Human-only approve | `approvals approve <id>` | Refuses when `CS_AUTHORITY_MODE` is set (any hooked session) or stdin is not a TTY; asks to type `APPROVE`; records `APPROVED` + `approved_utc`. The hook denies Main's Write/Edit to the approvals dir, the policy settings dir, `authority_grants.json` and `.claude/settings*.json`, and any shell command that names them or runs `... approve`. |
| Settings | `session_policy.install` | Derived per-session file = copy of `hooks, autoMode, modelSettings, model, statusLine, permissions` from `~/.claude/settings.json` with every `rtk ...` hook removed, plus the authority hook first. `env`, `apiKeyHelper`, `awsAuthRefresh`, `otelHeadersHelper` and every other key are never copied. Missing/unreadable user file: authority hook only. Launch argv: `--settings <file> --setting-sources project,local`. |
| Persistence | `install` on every launch | Fresh, restart, replacement and re-dispatch all reinstall and re-verify (exact hook command executed: force push denied, malformed input denied, `git status` passes: silence in advisory, `allow` in authoritative). |
| Main prompt | `launch.write_main_prompt` | STANDING AUTHORITY section: routine list, protected list, never ask for routine work, protected actions queue, failing gate = stop and report. |

## Worker shell rule (authoritative mode): structural
**Why structural.** Security review rounds 1 and 2 both found bypasses where the policy's model of the shell disagreed
with the real shell. Round 2 found three: a `cd` tracker desync (a failed `cd` before `;` or `||`, or a `cd` inside a
pipeline, does not change the real cwd, so `cd a/b; cd ../../CreatorStudio; git commit -m x` committed in the main
checkout with an explicit allow), PowerShell scriptblock arguments (`ls | sort {calc}` runs a program) and
`rtk test <cmd>` (which executes `<cmd>`). Since round 2 the worker allow path no longer models shell semantics at all.
A shell call gets an explicit `allow` only if ALL of the following hold; anything else is denied with "not on the
worker routine allowlist":
1. **Bash only.** The PowerShell tool is always denied for workers.
2. **One simple command.** After stripping one trailing ` 2>&1`, the text contains none of
   ``; & | < > ` $ ( ) { } [ ] * ? ! ~ #``, a backslash, a newline, a carriage return or another control character.
   Quotes are allowed only as one matched pair of single or double quotes around a whole token (`a"b"`, `"a"b` and
   unbalanced quotes are refused). So there are no pipelines, redirections, globs, variables or command lists:
   workers read and search with the Read, Grep and Glob tools.
3. **No `cd`, `pushd` or `popd`.** The payload cwd must be inside the roots, otherwise every shell command is denied
   (this also covers a `cd` that persisted from an earlier call).
4. **Closed allowlist,** at every level since round 1: command, then subcommand, then EXACT options (an unknown
   option or any abbreviation of a listed one is refused, because git and gh accept unambiguous prefixes), then path
   arguments; `--opt=value` tokens are checked the same way. `rtk` is unwrapped only in front of `git`, `gh`, `ls`,
   `grep`, `find`, `wc` or `diff`; every other rtk subcommand (`test`, `err`, `summary`, `run`, `proxy`, `bypass` ...)
   is denied. Source of truth: `GIT_SPECS`, `GH_SPECS`, `GOVERNANCE_SPECS`, `DEV_SUPERVISOR_SPECS`,
   `CREATOR_STATE_SPECS` and the per-command specs in `authority.py`.
5. **Paths inside the roots** (below).

For the deny-list used in both modes, `classify_command` unwraps `rtk test|err|summary|run|proxy|bypass <cmd>` to the
inner command, so Main's advisory hook also sees, for example, `rtk test git push origin main` as a push to main.

The detailed lists:
- **Paths.** Every path argument is resolved against that cwd and must stay inside the roots (the temp dir does NOT
  count for shell). Refused: `~`, drive-relative `C:x`, provider/URL forms (`env:X`, `HKLM:`), UNC/device paths, secret
  and launcher names, globs that start with a glob character (could expand into options), dot-globs (`.*`), brace
  expansion, and for writes (`mkdir`, `snapshot --out`) any `.git` component.
- **git** (no global option at all: `-C`, `--git-dir`, `--work-tree`, `-c`, `--no-pager` ... are refused):
  `status`, `diff`, `log`, `show` (no `--ext-diff`, `--textconv`, `--output`, `--no-index`), `rev-parse`, `ls-files`,
  `merge-base` (positionals checked against the roots); `fetch`/`pull` with a small flag list, a remote NAME and
  branch names only (no `--upload-pack`, `--receive-pack`, `-u`, `--exec`, `--config`, URLs, paths or refspecs with
  `:`/`+`); `branch` list or create (no `-f/-d/-D/-m/-M/-c/-C/-u`); `checkout -b <new> [<start>]` only; `switch <b>`
  or `switch -c <new> [<start>]`; `add` explicit paths (no `.`, `./`, `:`, `:/`, magic, git globs, `-A/-u/-f/-p`, no
  pathspec equal to a root or the cwd); `commit` with `-m/--message/-F/--file` (inside the roots)/`--author`/`-q/-v/-s`
  only, a message required, no pathspecs (never `--amend`, `--no-verify`, `-a`, `-n` or abbreviations); `worktree
  list`; `stash list|show`; `remote [-v]`, `remote get-url <name>`; `config --get|--get-all` of `user.*`,
  `remote.*.url|fetch`, `branch.*.remote|merge`, `init.defaultBranch`. `worktree add` is no longer allowed.
- **gh:** `pr view|list|checks|diff|create|comment`, `issue view|list|create|comment` with per-verb option lists;
  `--body-file`/`-F` must be inside the roots; `--repo` only on reads; `--jq`, `--template`, `--web` refused.
  `gh api <endpoint>` GET only (`-X GET`, `--paginate`, `-i`, `--silent`); no fields, headers, `--hostname`,
  `--jq` or `graphql`. Since round 2 (N2) the endpoint must be a relative path of plain name segments (no URL, host,
  `:` or `..`), and a `repos/...` endpoint must start with `repos/<owner>/<repo>`; a `pr`/`issue` operand is a number
  or a branch name, never a URL or a `.../pull/N` or `.../issues/N` path.
- **python** `[-B] [-I] -m`: `unittest` (discover `-s/-t` inside the roots), `governance validate [--against REF]`,
  `leases [--now]`, `handoff [--check]` (never `--write`), `wave submit [--attempts N]`, `wave status [id] [--json]`;
  `dev_supervisor status|workers|doctor|report|compact-check|snapshot [--out inside]|route` (never `--yes` or
  `--config`); `creator_state` read commands (`list check resume log checkpoints inventory ingest-status
  verify-sources incoming-list incoming-inspect shots-plan shots-status shots-check`, no `--root/--ffprobe/--ffmpeg`).
  Everything else (including `wave close`, `scope`, `score --write`, `start`, `assign`, `replace`) is denied.
- **Simple commands** with exact option lists: `ls dir pwd cat type head tail wc grep rg find sort uniq diff echo
  printf true test mkdir` and `sed -n` with a line-range print script only (`1,80p`; `$` is refused by the structural
  rule). `rtk git|gh|ls|grep|find|wc|diff ...` is checked as the inner command.
- Not on the list: `git push` (Main pushes), `gh pr merge`, anything else. Multi-line commit messages: several `-m`
  or `git commit -F <file inside the roots>`.

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

### Known limits accepted in review round 1 (left as they are)
- **N5, temp dir:** the temp directory stays a writable root for every session's file tools (not for worker shell
  path arguments). Claude scratchpads and dispatch prompt files live under `%TEMP%\claude\...`, so a worker could
  rewrite another session's prompt file (a cross-session injection path). Not changed in this round.
- **N8, approvals:** an APPROVED record is never consumed (every identical later call gets `ask` again). In advisory
  mode the shell boundary is a substring deny-list: Main could still forge a record by building the path inside
  `python -c` from `CS_AUTHORITY_APPROVALS_DIR`, or by calling `approve(..., isatty=True, env={})` directly. Impact is
  limited because APPROVED only ever yields `ask`, never `allow`, and the native prompt still reaches the human
  (INFERRED, not tested). `_APPROVE_CMD` also denies harmless text such as a commit message "authority: approve ...".

### Other limits after round 1
- Running code is running code: `python -m unittest`, `governance` and `dev_supervisor` import modules from the
  worker's own worktree, so a worker that writes a test or edits those packages can make an allowed command do
  anything. The allowlist constrains the command line, not what repository code does.
- The boundary check on shell text matches the boundary paths absolutely or relative to the payload cwd, the repo
  root or any `cd` target in the command, normalises `./` and `//`, and matches globs and directory-level `git
  checkout`/`git restore` (round 2, N3). A path built at runtime (variables, encoded strings, a script that writes the
  file) is still not recognised in advisory mode. Workers are covered separately because the live files are outside
  their roots.
- Advisory-mode (deny-list) parsing splits on `;`, `|`, `&&`, `||` and newlines even inside quotes; this only ever
  produces extra denials. The worker allow path does no splitting at all (structural rule).
- Accepted after round 2 (N4, unchanged): `python -m unittest` and `governance wave submit` run worker-written code;
  the temp dir is a writable root for file tools; `check_project_settings` does not inspect project `permissions`
  (they only matter where the hook is silent, which is Main's advisory mode).
- A recursive search inside the roots (`grep -r KEY .`, `rg`, the Grep tool without a glob) can still read a secret
  file that happens to sit inside the worktree; only targeted names and globs are refused. The repository holds no
  credentials by project rule.
