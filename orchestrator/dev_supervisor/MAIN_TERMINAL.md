# Main in a VS Code terminal tab (attached launch mode)

Status: IMPLEMENTED and TESTED offline (fake claude, fake session files, fake clock, plus real Python sleeper processes for
lock and termination behaviour). **NOT TESTED live**: no real `claude` session, no real VS Code terminal and no real
`claude --resume` was used. Every label below follows that rule. Nothing here is VERIFIED.

## Why

REPORTED 2026-11-19: a Main started by the supervisor with `CREATE_NEW_CONSOLE` ran as a process but had no visible window
(`MainWindowHandle = 0`), so the user could not talk to it. The supervisor cannot type into an interactive session, and nothing
outside VS Code can create an integrated-terminal tab (tabs come from the user, from a VS Code task, or from an extension; a
Python process has no supported interface to them). So the tab is opened by the user and the interactive `claude` runs **inside
it**, as the child of a small host process.

## Architecture

```
VS Code terminal tab 1 "Main"                     VS Code terminal tab 2 "Supervisor"
python -B -m dev_supervisor main-host --yes       python -B -m dev_supervisor start --yes
  MainHost (main_host.py)                           SupervisorLoop (cli.py), main_launch_mode "attached"
   |- owns the interactive claude child               |- never launches Main
   |- you type into claude directly                   |- adopts the Main the host runs (session file = identity)
   |- executes supervisor commands                    |- monitors context pressure, workers, requests
   `- writes main_host.json (heartbeat)               `- replaces Main through the host (handoff first)
            \___ machine-local files in <STATE_DIR>, one writer per file, atomic writes ___/
```

Coordination files (all under `<STATE_DIR>`, outside Git, no credentials; `state_store.scrub` is applied):

| File | Writer | Content |
|---|---|---|
| `main_host.json` | host only | host id and pid, state, provider label, heartbeat, running child (pid, session id, confirmed), the transition in progress, ended children (last 8), recoverable session |
| `main_host.lock` | host only | an **OS lock** held by the host process on an open handle (`msvcrt.locking` / `fcntl.flock`); the file body is only an owner record for messages |
| `main_commands/<id>.json` | supervisor only | `handover`, `stop_child`, `resume`, `settle`; each has an expiry and the supervisor pid |
| `main_commands/<id>.cancel.json` | supervisor only | a request not to start (or continue) command `<id>` |
| `main_commands/<id>.ack.json` | host only | `ACCEPTED`, `STARTED`, `DONE`, `REJECTED`, `EXPIRED`, `FAILED`, `CANCELLED` |
| `main.json`, `sessions.json` | supervisor only | unchanged meaning; a Main is recorded only after it is observed |

`main_launch_mode` selects the behaviour. `"console"` (default) is the previous behaviour, unchanged. `"attached"` is this
document. Provider environment is unchanged in both: the child gets `provider_env.child_env()` (the OmniRoute/gateway
overrides removed, Claude Pro default) and the permission flags come from the same `build_main_argv`
(`main_permission_mode`, `dangerously_skip_permissions` only when configured). `allow_paid_api` is not touched.

Optional key `main_initial_prompt` (one short plain line, not starting with `-`) replaces the default first prompt. It exists
for smoke tests; leave it out for real work. Like the other new keys it must not be added to a config that an older checkout
still reads (older code refuses unknown keys).

## Startup (two tabs)

One-time: add to the machine-local config (never into Git), see `python -B -m dev_supervisor config --example`:

```json
"main_launch_mode": "attached"
```

Do not add the key while any older checkout still reads that config. Until this change is merged, use a separate config file
with `--config` (see "Safe live smoke test").

Open the first tab (Terminal > New Terminal, or Ctrl+Shift+`) with your usual launcher for the provider you want, then:

```powershell
cd <REPO_ROOT>\orchestrator
python -B -m dev_supervisor main-host            # dry run: shows argv, isolation facts and the duplicate check; starts nothing
python -B -m dev_supervisor main-host --yes      # tab 1: Main runs here; type into it
```

Open the second tab, same launcher, then:

```powershell
cd <REPO_ROOT>\orchestrator
python -B -m dev_supervisor start --yes          # tab 2: supervisor; adopts the Main of tab 1
python -B -m dev_supervisor status               # any tab: shows main_host, session id, heartbeat
```

Order does not matter. Supervisor first: it prints `Waiting for the Main terminal host`, records `AWAITING_HOST` and launches
nothing. Host first: the supervisor adopts the Main as soon as the session file shows the expected session id. The tab you
open with a launcher decides the provider of what it starts: the host's child inherits tab 1's environment, workers inherit
tab 2's. The host records a provider label (`pro`/`inherit`, never a value) in `main_host.json`; use the same launcher in both
tabs.

Optional, NOT TESTED: a VS Code task can open the tabs for you (`.vscode/tasks.json` is local to the checkout and is not part
of this change). Automatic tasks need VS Code's "Allow Automatic Tasks" consent:

```json
{ "version": "2.0.0", "tasks": [
  { "label": "CreatorStudio Main", "type": "shell", "command": "python -B -m dev_supervisor main-host --yes",
    "options": { "cwd": "${workspaceFolder}/orchestrator" },
    "presentation": { "panel": "dedicated", "group": "creatorstudio", "reveal": "always" }, "problemMatcher": [] },
  { "label": "CreatorStudio Supervisor", "type": "shell", "command": "python -B -m dev_supervisor start --yes",
    "options": { "cwd": "${workspaceFolder}/orchestrator" },
    "presentation": { "panel": "dedicated", "group": "creatorstudio", "reveal": "always" }, "problemMatcher": [] } ] }
```

## Duplicate prevention

`main-host --yes` refuses (exit 1, nothing started) when any of these holds:

- another host holds the OS lock (the OS releases it when that process dies: no stale-lock takeover race, no pid reuse
  ambiguity);
- `main.json` records a Main whose pid is **alive, whatever the record's status** (this is how the old invisible console Main
  is detected);
- an earlier host's child is still alive;
- a **live Claude Code session with this Main's name runs in this repository** (`~/.claude/sessions` is machine-wide, so this
  also catches a Main recorded in a different state directory, or in none). A session with another name, or in another
  directory, does not block: your own Claude sessions and a throwaway test repo are unaffected;
- `main_launch_mode` is not `attached`;
- the Main record or the host record cannot be read (fail closed: a duplicate cannot be ruled out).

`--ignore-recorded-main` bypasses the recorded-pid and session checks only, after you have verified that a recorded pid is
stale. The supervisor in attached mode never launches Main at all, never overwrites a live recorded pid with
`AWAITING_HOST`, and console mode refuses to launch while a host child is alive. Limit: the session scan sees only what
Claude Code writes to its session directory; a Main in another repository, or one that wrote no session file, is invisible
to it.

## Replacement (automatic or `replace --handoff-id ... --yes`)

One terminal cannot hold two interactive programs, so the old Main cannot keep running while the new one starts in the same tab.
The sequence below is the safest recoverable one; the unavoidable window is "old stopped, new not yet confirmed", and it always
ends in a usable Main or an explicit `NEEDS_ATTENTION`.

1. The supervisor verifies the handoff by read-back (unchanged). An unsafe handoff sends the host nothing.
2. The supervisor checks the host heartbeat and that the host runs exactly the old session; then sends `handover`.
3. **Preflight, no side effect:** the host checks the command is fresh and not cancelled, the issuing supervisor is alive, the
   old child is its own, the prompt file is inside `<STATE_DIR>`, `claude` resolves (no `.cmd` shim) and the argv builds. Any
   failure is `REJECTED`/`EXPIRED`/`CANCELLED`, and the old Main is untouched.
4. **Write-ahead:** the host publishes `recover_session_id` and the transition stage to `main_host.json` BEFORE it touches the
   old child. If that cannot be saved, nothing is stopped (`FAILED`).
5. Only then the host terminates (then kills) the old child. If it cannot be stopped, the write-ahead is undone: `FAILED`, the
   old Main is still running and nothing else was started.
6. The host starts the new Main in the same tab (`STARTED`). The supervisor waits for the first heartbeat: a session file that
   shows the **new** session id with a live pid. The old exit must be recorded by the host before the old session is marked
   retired.
7. No heartbeat or an early exit: the host stops the new child, the supervisor retries once (a second `handover`).
8. Both attempts failed: the supervisor sends `resume`; the host runs `claude --resume <old session id>` in the tab with the
   same model, permission mode and appended prompt. The result is recorded only from what the session file then shows:
   `RESUMED_SAME_ID`, `RESUMED_NEW_ID` (the observed id is recorded) or `RESUME_UNCONFIRMED`. Limitation: this restores the
   conversation from Claude Code's own transcript. The supervisor does not verify that the exact previous state (context
   contents, pending tool state) returned. `claude --resume` was never run live here.
9. Success: `settle` tells the host the recovery information can be dropped. The host must **acknowledge** it (`DONE`); an
   unacknowledged settle is reported as `settle: UNCONFIRMED` and retried on later ticks.

**Timeout races.** If the supervisor gets no final answer in time it writes `<id>.cancel.json` and waits for the host's *final*
answer instead of guessing. The host honours the cancel only before step 5. If the handover completed anyway, the host's Main
is adopted from its session file and the result is `REPLACED_LATE_HANDOVER` with `authoritative_session_id`; it is never
reported as `ROLLED_BACK` while `main.json` points at the new Main. That status is a non-success exit for `replace` because the
replacement heartbeat did not confirm it.

**Never resurrect a replaced session.** The host itself watches for the new Main's session file. A new Main that was seen
running is "confirmed"; if it exits later (you quit it), the host ends too and does **not** resume the old session, even if a
`settle` was lost. Only a new Main that never showed up is treated as a failed start and recovered.

| Failure | Result |
|---|---|
| handoff NOT_SAFE or checkers fail | refused before any command; old Main untouched |
| host heartbeat stale / no host | launch error; `ROLLED_BACK`, `OLD_MAIN_INTACT`, nothing stopped |
| host preflight fails (claude missing, bad prompt path, identity mismatch) | `REJECTED`; old Main untouched |
| recovery record cannot be saved | `FAILED`; old Main untouched |
| Ctrl+C or interruption during preflight / while saving / while stopping | write-ahead undone, old Main still running, command `FAILED` |
| Ctrl+C after the old Main stopped (before or during the start of the new one) | recovery record already durable: the host resumes the old session after `host_recovery_wait_s`, or about 5 s if the supervisor is dead |
| old Main cannot be stopped | `FAILED`; old Main still running; no second Main |
| new Main cannot start / exits / no heartbeat | retry once, then `resume` of the old session |
| supervisor times out while the host is mid-handover | cancel requested; host's final answer decides; a completed handover is adopted as `REPLACED_LATE_HANDOVER` |
| host dies mid-handover | no heartbeat, no host: `NEEDS_ATTENTION`; never recorded as success; old session marked failed, not authoritative |
| supervisor dies after the host stopped the old Main | the host resumes the old session itself after 5 s (supervisor dead) or `host_recovery_wait_s` |
| supervisor restarts after a completed handover | `reconcile` adopts the host's child once the session file shows its id |
| resume also fails | `NEEDS_ATTENTION` (supervisor) or host exit 1 (host self-recovery); it is tried once, never looped |
| session file shows another id on the host's pid | `IDENTITY_MISMATCH`, not adopted (except after `resume`, where the observed id is recorded) |
| host record unreadable after a failed replacement | `HOST_STATE_UNREADABLE`; the old session is NOT marked failed; retried by the loop |

An old session is marked `RETIRED` only with host-recorded evidence that its process ended. Without it (for example an
adoption while the old Main is still alive) the record becomes `RETIRE_FAILED`, non-authoritative, with a note to stop it by
hand; nothing is signalled.

Terminating the old Main uses `Popen.terminate()` (TerminateProcess on Windows: abrupt, no cleanup in the child). OBSERVED
on this machine with Python sleepers (not claude): it ends only that process; a **grandchild it started keeps running**. For
claude that means its MCP servers and shells may outlive the old Main and a second set starts with the new one (for example a
second Resolve MCP). Whether claude's own transcript survives an abrupt end, and whether the terminal keeps stale modes
(mouse tracking, bracketed paste, alt-screen) afterwards, are NOT TESTED. Tree termination was deliberately not added: it needs
a live test first.

## Errors: retryable versus fatal

- Reading or writing a coordination file can fail transiently (sharing violation, antivirus, torn read). `OSError`,
  `StoreError` and bad JSON are **retryable**. The host loop logs, retries and never ends while it has a child. The
  supervisor loop skips that tick's reconcile and tries again. An unreadable command file is skipped, not rejected, and is
  rejected only after three consecutive unparseable looks.
- **Fatal for the host:** no child and 30 consecutive failing ticks (exit 1); failure to take the lock; a failed duplicate
  guard; a recovery record that cannot be saved before a destructive step (then that step is not taken).
- If the host must end abnormally while a child is running, it ends that child first so no unattended interactive `claude`
  stays in the tab competing for console input.
- Ctrl+C reaches every process on the console, so it reaches the host whenever you press it in `claude`. The host holds SIGINT
  off during handover/resume transitions and ignores it otherwise (`claude` handles its own copy). Closing the tab ends both.

## Host disconnect and restart

Closing tab 1 ends the host and its Main. The supervisor marks the Main `EXITED`; the loop ends when no live host remains
(`exit_with_main`). Run `main-host --yes` again for a fresh Main, or `main-host --yes --resume-last` to resume the recorded
session (the observed session id is what the supervisor records). A new host carries over the earlier host's record and notes
that earlier child as observed gone, which is what lets the supervisor retire its session record.

## Moving from the current (console mode) supervisor

The running supervisor and its invisible Main are never signalled by this code. Safe cut-over, all by you:

1. Have the current Main write and verify a handoff (`handoff --input ... --yes`).
2. Stop the old supervisor loop with Ctrl+C in its tab (Main and workers keep running).
3. Close the invisible Main yourself (it has no window): `python -B -m dev_supervisor status` shows its pid; end that process
   with Task Manager or `Stop-Process -Id <pid>`. Only then will `main-host` start; while it is alive `main-host` refuses.
4. After this change is merged, set `"main_launch_mode": "attached"` and start the two tabs as above.

## Safe live smoke test (needs your explicit approval: it starts a real Claude session)

Goal: prove host start, adoption, typing, Ctrl+C and a manual `--resume` without touching CreatorStudio. Everything lives in a
throwaway folder **outside every Git working tree and outside CreatorStudio**, with its own state directory, no GitHub issue,
the cheapest model and a trivial first prompt. The duplicate guard also refuses to start while a live Main-named session runs in
the *same* repository, but the throwaway repo is a different directory on purpose.

```powershell
$S = Join-Path $env:USERPROFILE "cs-smoke"          # must not be inside CreatorStudio or any other repository
New-Item -ItemType Directory -Force "$S\repo", "$S\state" | Out-Null
git -C "$S\repo" init
Set-Content -Path "$S\repo\README.md" -Value "smoke test" -Encoding ascii
git -C "$S\repo" add README.md
git -C "$S\repo" -c user.name=smoke -c user.email=smoke@example.invalid commit -m init
$cfg = [ordered]@{
  state_dir = ("$S/state").Replace('\', '/'); repo_root = ("$S/repo").Replace('\', '/')
  main_launch_mode = "attached"; models = @{ main = "haiku" }
  main_initial_prompt = "Reply with the single word OK, then wait."
  allow_paid_api = $false; main_permission_mode = "auto"; dangerously_skip_permissions = $false
  auto_replace_main = $false }
$cfg | ConvertTo-Json | Set-Content -Encoding ascii "$S\config.json"     # ascii: Windows PowerShell 5.1 '>' writes UTF-16
cd <WORKTREE>\orchestrator
python -B -m dev_supervisor --config "$S\config.json" config                # check source, state_directory, no github issue
```

Steps (stop at the first surprise; each step is read-only until `--yes`):

1. **Tab 1 dry run:** `python -B -m dev_supervisor --config "$S\config.json" main-host`. Read `isolation` (repo_root and
   state_directory under `cs-smoke`, `github_issue` null, model `haiku`, the one-line prompt) and `duplicate_check: OK`. Abort
   if any of it points at CreatorStudio.
2. **Tab 1:** add `--yes`. Check you can type, colours look right, and a Ctrl+C does not end the host.
3. **Tab 2:** `python -B -m dev_supervisor --config "$S\config.json" start --yes`, then `status`: Main `RUNNING`, `main_host`
   fresh, session id equal to the one in `claude`.
4. **Manual resume experiment (no automatic replacement):** from a third tab end only the host's child
   (`Stop-Process -Id <child pid from status>`). Expect the host to report the Main ended and exit, and the supervisor loop to
   end. Then in tab 1 run `main-host --yes --resume-last` (supervisor tab can be restarted). Record whether the session id
   stayed the same, whether the conversation came back, whether the terminal is garbled, and whether orphan processes remain
   (`Get-CimInstance Win32_Process | Where ParentProcessId -eq <old pid>`).
5. **Only after 1-4 look right, and only with a second approval:** a real replacement in the throwaway repo
   (`handoff` then `replace --handoff-id <id> --yes`), still with `auto_replace_main` false.
6. **Clean up:** close both tabs, then delete `$S`.

Abort conditions: `duplicate_check` is not OK, any path points into CreatorStudio, `status` shows two live Mains, or the
terminal accepts no input.

## Limits

- The host is a Python process; the interactive `claude` inherits its console. Whether typing, colours and Ctrl+C behave in a
  VS Code integrated terminal is NOT TESTED.
- `claude --resume` flags were checked against `claude --help` (2.1.316) only; its behaviour was never run.
- Whether the real session file's pid equals the pid the host started (a wrapper process would differ) is NOT TESTED. Adoption
  and the replacement heartbeat match by session id, not pid.
- The supervisor still cannot type into Main and never sends `/compact`; compaction still means handoff then replacement.
- `claude --bg` / `claude attach` (background sessions) were not evaluated live; they are an alternative, not used here.

## Proposed edits to shared documents (NOT applied: owned by other tasks or the integrator)

The claim PR for this change should either carry these edits with the owner's agreement or leave them to the integrator.

- `DEVELOPMENT_OPERATING_SYSTEM.md`, line stating "Main is an interactive `claude` session in its own console window,
  launched by the supervisor's launcher": append "In `main_launch_mode` `attached` the user runs the interactive Main in a
  VS Code terminal tab under `main-host`, which owns it; the supervisor never launches it (see
  `orchestrator/dev_supervisor/MAIN_TERMINAL.md`; IMPLEMENTED, NOT TESTED live). `console` remains the default."
- `DEVELOPMENT_OPERATING_SYSTEM.md`, the capability row "Interactive Main launch in a new console window | IMPLEMENTED,
  OBSERVED": add the sentence "REPORTED 2026-11-19: the window was not visible (`MainWindowHandle = 0`)" and add a new row
  "Main terminal host (attached mode) | IMPLEMENTED, TESTED offline, NOT TESTED live".
- `AI_Creator_Studio_Documentation/DECISIONS_AND_OPEN_QUESTIONS.md`, the Issue #44 entry: add "DECIDED (Lukas, 2026-11-19):
  Option A, a Main terminal host in a VS Code tab (attached mode), because nothing outside VS Code can create integrated
  terminal tabs. OPEN: live test."
- The claim must add `orchestrator/tests/test_dev_main_host.py` to the registered test paths of the package.
