# Development Operating System

A reusable, project-independent guide to running autonomous Claude Code development from one human-facing session ("Main"), with
specialist workers, a Python supervisor outside the conversation, GitHub as the durable record, and a read-only dashboard (Orbit).
It was built inside CreatorStudio (issue #44, wave `w03-dev-supervisor`) but nothing in the method is specific to video.

Conventions: placeholders such as `<REPO_ROOT>`, `<STATE_DIR>`, `<OMNIROUTE_URL>`, `<OWNER>/<REPOSITORY>` stand for machine-local values that never
belong in a tracked file. Status labels are defined in section 14; every capability carries one. Nothing here is VERIFIED unless a command output
is cited, and the table in section 14 is the status of record for this document.

## 1. Principles

1. One master, many workers. Main is the only session that talks to the human; workers report to Main.
2. The repository is the memory. Tasks, owners, branches and decisions live in tracked governance files and on GitHub; chat and terminal output are never the only record.
3. Evidence over claims. An agent statement is REPORTED until a command output, test, inspected file, GitHub state or read-back supports it.
4. Bounded autonomy. Bounded retries, no repeated identical failure, explicit stop conditions, recorded blockers.
5. Unknown stays unknown. A value the system cannot observe is `UNAVAILABLE` with a reason; it is never guessed or filled with a default that looks like data.
6. A fresh session does not replenish account usage. Replacing a session solves context and continuity only.

## 2. Roles: ChatGPT, Main, workers, supervisor

| Role | What it is | What it does not do |
|---|---|---|
| ChatGPT project or conversation (optional) | Idea origin and requirements partner. Helps the human shape a mission statement. | It is not a runtime control interface for development. It does not execute, merge or approve. |
| Main | One interactive Claude Code session (VS Code or its own console window). Sole human-facing orchestrator. | Does not self-approve or merge pull requests unless the project's integration rules grant that role; does not do routine bulk work itself. |
| Specialist workers | Headless Claude Code sessions (or subagents), each with one task, one branch, one worktree. Possibly routed through OmniRoute (section 9). | Never ask the human routine questions; do not edit paths outside their ownership; do not update shared registries. |
| Python supervisor | A process outside every conversation: lifecycle, monitoring, handoff, replacement, logging, snapshot. | It cannot type into Main and does not decide what to build. |

Typical specialist roles: implementer, tester, researcher/searcher, reviewer, documentation writer, integrator. Roles are labels on a worker record, not separate software.

## 3. How Main controls the operation

1. Start from the repository's own contract files: protocol, handoff, status and active-task registry (in CreatorStudio: `PROJECT_OPERATING_PROTOCOL.md`, `SESSION_HANDOFF.md`, `PROJECT_STATUS.json`, `ACTIVE_TASKS.json`).
2. Turn the agreed objective into small tasks with owned paths, dependencies and acceptance criteria. Register each through the repository's legal workflow (task claim or wave manifest) and as a GitHub issue linked to its parent.
3. Assign each task to a worker with its own branch and worktree: `python -B -m dev_supervisor assign --task <id> --role <role> --branch <branch> --worktree <path> --owner-stream <stream> --prompt-file <file>` (a dry run; `--yes` queues it).
4. Monitor with `status`, `workers` and `snapshot`; collect results. A result is REPORTED until Main (or an independent check) verifies it.
5. Verify before accepting: run the tests, read the diff, run the scope and secret checks, run the governance validator, and read back anything written to GitHub.
6. Treat contradictory reports from two workers as a finding: neither side is VERIFIED until evidence settles it; the supervisor flags the conflict and blocks promotion.
7. Escalate: a failed validation or repeated failure moves the task to a stronger model or to Main itself; a blocker is recorded, not retried forever.
8. Report milestones to the human; keep routine progress on GitHub, not in chat.

## 4. Proposing ideas and agreeing a large build

Main does not start a large build from a one-line wish. Before building anything substantial it runs this loop and records the outcome (a GitHub issue or decision record):

1. Restate the goal in the human's words and name the real outcome (what will be different when it works).
2. Propose two or three plausible approaches, including a deliberately small first slice. For each: what it gives, what it costs (time, quota, complexity, risk), what it depends on, and what could make it fail.
3. Explain feasibility honestly with labels: what is OBSERVED on this machine, what is REPORTED by documentation, what is PROPOSED. Name unknowns and say how a cheap experiment would resolve them.
4. State trade-offs plainly (autonomy against safety, free routes against reliability, build time against reuse, a quick slice against a clean design).
5. Ask only the essential clarifying questions: those whose answer changes the design, spends money or quota, touches personal data, or is irreversible. Offer a default for each so the human can answer in one word. Do not ask what the repository, the environment or a cheap read-only probe can answer.
6. Get an explicit agreement on direction and scope before the large build. After agreement, work autonomously in small recorded tasks; routine decisions inside the agreed scope are not re-asked.
7. Record the decision with its reason. Never promote a proposal to a fact, and never overwrite an earlier human decision silently.

## 5. Communication between agents

- Live: Claude Code's own mechanisms (subagents, the supervisor's worker streams and result files) carry the live exchange. Main reads worker output from the supervisor's registry and log files.
- Durable: GitHub issues, comments, branches and pull requests, plus the tracked governance files. GitHub is the shared record, not a substitute for live messaging.
- A worker's final report names: what was done, the exact commands and their results, what is NOT TESTED, what is unresolved and the paths it touched. Main does not accept a report without the commands.
- Workers never edit shared registries or another worker's paths. If they need a change outside their ownership they say so in the report.

## 6. The Python supervisor

Package `orchestrator/dev_supervisor/`, standard library only, run from `orchestrator/` as `python -B -m dev_supervisor <command>`.
Every command is non-destructive without `--yes`.

| Command | Purpose |
|---|---|
| `doctor` | Environment checks (claude, git, gh login, OmniRoute reachability, Resolve discovery, state directory, clean repository, worktrees) |
| `config` | Effective configuration; `config --example` prints the template with placeholders |
| `start` | Launch Main and run the supervisor loop (`--yes`) |
| `status` / `workers` | Main, authority, workers, pending requests, lock |
| `assign` | Validate (and with `--yes` queue) a worker assignment |
| `handoff` | Build, write and verify a handoff (`--verify <id>`) |
| `compact-check` | Main's context pressure and the compaction decision |
| `replace` | Replace Main after a verified handoff (`--yes`) |
| `report` | Flush the GitHub progress log (`--yes`) |
| `snapshot` | Write the read-only Orbit snapshot |
| `route` | Explain a model/provider routing decision |

State is machine-local under `<STATE_DIR>` (outside any git working tree; the store refuses a path inside one). It is a cache of live process facts and a handoff.
It is never a second task registry: where it disagrees with the repository's governance files, the repository wins.

How sessions are run:
- Main is an interactive `claude` session in its own console window, launched by the supervisor's launcher (`launch.py`) with an argument list (no shell string parsing), a session id, a name and an appended system-prompt file that carries the operating instructions and, after a replacement, the verified handoff path and hash.
- Workers are headless: `claude -p --output-format stream-json --verbose`, prompt on stdin, output to log files, one process group each. The supervisor never signals a pid it did not start.
- OBSERVED once (headless command with a trivial prompt): exit 0 after 8.5 s with init, assistant, rate_limit_event and result events; without `--verbose` it fails with stream-json; `--append-system-prompt-file` is accepted although `--help` does not list it.
- Worker lifecycle: assign, spawn (STARTING persisted before launch), monitor (exit code, heartbeat and activity staleness, bounded runtime), restart with exponential backoff and a restart budget, BLOCKED after repeated identical failures until a human unblocks, graceful stop then kill, and recover (reload state, reconcile pids, mark vanished workers LOST; plan only, it never launches or signals).

## 7. Context rotation, handoff and recovery

Context pressure and account usage are different quantities and are tracked separately.

- Context pressure: computed from the token usage recorded in the session transcript against a configurable window per model, with configurable warn, compact and replace thresholds (defaults 0.6 / 0.8 / 0.92 are configuration, not facts about any account). The real per-model windows are unknown; an unlisted model reports UNAVAILABLE rather than a guess.
- Handoff: a labelled document (objective, decisions, plan, task graph, worker identities and status, branches, worktrees, commits, pull requests, tests and evidence, blockers, next actions, resource state), written as JSON and Markdown with a SHA-256 hash. It refuses secret-like or transcript-like text. `handoff --verify` re-reads it, recomputes the hash and confirms every branch, worktree, commit and PR through checkers; any failure or missing checker means NOT_SAFE. Only a SAFE result can authorise replacement.
- The supervisor cannot type into Main. Compaction of Main therefore means: Main writes a handoff, the supervisor verifies it by read-back, then replaces the session. Replacement marks the old session RETIRING, launches the new one pointing at the handoff, waits (bounded) for a heartbeat, moves authority, re-verifies, and only then retires the old session; it rolls back on failure and stops after repeated failed replacements until a human resets.
- No session is cleared or replaced before a verified handoff exists. Each decision and its reason is recorded.
- A replacement does not replenish account usage.

## 8. GitHub as the durable log

- Every major task and every meaningful subtask is an issue (linked to its parent) before or when work starts, with owner, scope, dependencies, acceptance criteria and branch/worktree.
- Completed steps, decisions, test runs, failures, retries, provider changes, handoffs, compactions, blockers and pull requests are posted promptly with evidence.
- Implementation: `github_log.py` has eleven event kinds, a scrubber (tokens, key-value secrets, userinfo, query strings, absolute and personal paths, long opaque strings), a Markdown renderer that neutralises `@mentions` and marker forgery, a local spool, a ledger for duplicate prevention, rate limiting, bounded retries, and read-back of an idempotency marker before re-posting after an ambiguous failure. The supervisor loop spools events; `report --yes` posts them.
- The repository may be public: posts and commits carry no secrets, no transcript or prompt text, no private media and no personal absolute paths.
- Branches, worktrees, pull requests and integration follow the host repository's rules. In CreatorStudio: work on a task or lane branch in its own worktree, claim paths first (a wave lane's manifest entry is its claim), never push to the default branch, never force-push, stage explicit paths only, and open a pull request. For the issue-44 mission Main opens pull requests and does not merge them itself.

## 9. Claude Pro versus OmniRoute

| Route | What it is | Reality |
|---|---|---|
| Claude Pro (direct login) | The human's subscription, used by Main and by workers started with that login | Usage is shared across every session on the account. Per-agent quota is not isolated. Reset windows are visible only if a source exposes them. |
| OmniRoute | A local OpenAI-compatible gateway at `<OMNIROUTE_URL>` (default `http://localhost:20128`) fronting free Gemini and Groq tiers | Free quotas are volatile. No paid API is assumed or used: the scheduler excludes paid routes unless `allow_paid_api` is set by the human in the machine-local config. |

- Allocation is by task criticality, capability, remaining quota, context pressure, deadline and concurrency, not fixed percentages: `python -B -m dev_supervisor route ...` prints an explained decision. Main gets the strongest model; routine implementation, mechanical tests and search go to cheaper routes; failed validation or repeated failure escalates.
- OBSERVED benchmark (one run, one machine, one moment; `orchestrator/dev_supervisor/OMNIROUTE_BENCHMARK.md`): 2 of 7 routes answered (a Gemini lite route 5 of 5 tasks, a Gemini flash preview route 4 of 5); other Gemini routes returned not_found, rate_limited, timeout or upstream_error; both Groq routes returned auth_error (cause not investigated). Not tested: long contexts, concurrency, multi-turn tool use, authenticated requests, Claude Code itself through OmniRoute.
- Account usage and reset windows come from the `rate_limit_event` that Claude Code writes into every headless worker's stream-json log (`<STATE_DIR>/logs/<worker>/run-<n>.stream.jsonl`). `RateLimitEventProvider` reads the newest one: 5-hour and 7-day utilization (as a percentage), reset times and status, labelled OBSERVED and timestamped with the log's last write time. `status`, `snapshot` and `handoff` use it. Interactive Main writes no stream-json, so the figure is only as fresh as the last worker run; with no worker run yet it is UNAVAILABLE with that reason. The statusline payload ingestor still exists but has not been fed a real Claude Code payload.
- A fresh session does not replenish usage. When the account is exhausted the options are to wait for the reset, switch the launcher to OmniRoute (a human action), or stop. The supervisor records provider switches; it does not hide them.
- Provider and model identity are recorded per worker; actual token usage is reported only where the provider exposes it.

## 10. Orbit

Orbit is the read-only dashboard identity (the Electron app under `dashboard/desktop/`). A new Orbit view reads the snapshot written by `snapshot`: Main session, workers, tasks, model/provider, context pressure, account usage (only when every value is OBSERVED, otherwise UNAVAILABLE with the reason), branch/PR, last GitHub checkpoint, blockers, failures, retries and next actions.
Every value carries a label, timestamp, source and freshness: live, cached, stale or offline (plus unconfigured and invalid for the file itself); ages are recomputed at display time. Orbit never writes supervisor state; its only write is one app setting in its own user-data folder. Released artifact names, package name and app ids were not renamed, to keep updates and checksums intact. The Electron window itself is NOT TESTED.

## 11. Windows and VS Code setup, machine-local configuration

- Windows 11 with VS Code, Git, GitHub CLI (`gh auth login`), Python 3 (standard library only, no dependency installation), Claude Code CLI on `PATH`. PowerShell 5.1 has no `&&`; Claude Code's `!` prefix uses Git Bash.
- Launchers that hold credentials stay local and are never copied into the repository or its documents; the supervisor's configuration holds no credential-like key or value (such keys are refused).
- Machine-local supervisor config: a JSON file at `<STATE_DIR>/config.json`, or the path in environment variable `CREATORSTUDIO_DEV_SUPERVISOR_CONFIG`; refused inside a git working tree. Create it from `python -B -m dev_supervisor config --example`. Fields cover models, context windows, thresholds, OmniRoute URL, GitHub repository and issue, `allow_paid_api` (default false) and `dangerously_skip_permissions` (default false).
- State directory: `<STATE_DIR>` defaults to a per-user local application-data folder and can be set by `CREATORSTUDIO_DEV_SUPERVISOR_DIR`.
- High autonomy: the human may accept elevated local execution risk for trusted, bounded development work, which can mean enabling the dangerous-permissions flag in the local config. That does not authorise paid spending, publishing credentials or private media, destructive changes to unrelated personal data, or bypassing the repository's governance.
- DaVinci Resolve (project-specific, optional): launching the application is not enough. On the Free edition the bridge needs `Workspace > Scripts > resolve_bridge` run by the user inside Resolve. Identity is whatever project is open, so the supervisor compares it with a machine-local identity file (never by name alone) and reports MATCH, MISMATCH, UNCONFIGURED or UNAVAILABLE (`orchestrator/dev_supervisor/RESOLVE_BOOTSTRAP.md`). The discovery command has no MCP client of its own and consumes a snapshot of read-only replies.

## 12. Onboarding a new repository

1. Install the prerequisites in section 11 and run `gh auth login`.
2. Clone the repository. Copy `orchestrator/dev_supervisor/` (and its tests `orchestrator/tests/test_dev_*.py`) into it, or vendor it as a package. It uses the standard library only.
3. Give the repository its governance files: a protocol document, a task/owner registry, a status file and a handoff file. The supervisor is a cache and relies on these; it does not replace them.
4. Choose the state directory outside any working tree and set `CREATORSTUDIO_DEV_SUPERVISOR_DIR` if the default is not wanted.
5. From `orchestrator/`: `python -B -m dev_supervisor config --example` and save an edited copy as the machine-local config. Set `github_repo` and `github_issue`. Leave `allow_paid_api` and `dangerously_skip_permissions` false until the human decides.
6. Run `python -B -m dev_supervisor doctor`; fix each failed check (claude, git, gh, OmniRoute reachability, state directory, clean repository). A warning that Resolve is UNCONFIGURED is acceptable for projects that do not use Resolve.
7. If OmniRoute is used, start the gateway (`omniroute serve --daemon --no-open`) and run the benchmark before trusting any route: `python -B -m dev_supervisor.omniroute --out <results.json>`.
8. Run the supervisor tests: `python -B -I -m unittest discover -s tests -p "test_dev_*.py"`.
9. Create the GitHub issue that will carry the progress log and put its number in the config.
10. Dry run: `python -B -m dev_supervisor status`, `python -B -m dev_supervisor snapshot`, then `python -B -m dev_supervisor handoff` without `--yes`.
11. Only then start: `python -B -m dev_supervisor start --yes`. This path (interactive Main launch, `start --yes`, replacement, heartbeat) is NOT TESTED live; the first real run should be watched.
12. Write the project's own agents separately (section 13) and add the project's safety rules to its CLAUDE.md or equivalent.

## 13. Development agents versus product agents

| | Development-time agents (this document) | Product agents (project specific) |
|---|---|---|
| Purpose | Build and maintain the repository | Do the product's job |
| In CreatorStudio | Main, workers, supervisor, Orbit | Orchestrator, Research, Creative Director, Editor, Asset, Resolve Executor and Quality Control agents (`AI_Creator_Studio_Documentation/AGENTS.md`) |
| Lifetime | Exist while development runs | Exist while a video is produced |
| State | Task registry, GitHub, `<STATE_DIR>` | Per-project state and handoff (`creator_state`) |
| Reusable elsewhere | Yes: the supervisor, the handoff and evidence model, the logging | No: they encode the product's rules |

The two sets share no names, registries or state. The development supervisor never edits media or runs production stages; the video-production agents never launch or supervise development sessions. Resolve is a production tool, but a development worker may only inspect it read-only and never mutates a real project.

## 14. Capability status

Labels: IMPLEMENTED (code exists), TESTED (offline tests pass), OBSERVED (seen on the real machine with a stated command), REPORTED (stated by a document or another session, not re-observed), NOT TESTED (exists, no evidence), PROPOSED (design only). A row may combine labels; the text after a label is its evidence.

| Capability | Status | Evidence |
|---|---|---|
| Evidence model and telemetry (sessions, context pressure) | IMPLEMENTED, TESTED (offline), OBSERVED | 90 offline tests; one live read: 2 sessions, 1 pressure computed, 1 UNAVAILABLE (model not in window table) |
| Context windows and thresholds | PROPOSED | Configuration defaults; real per-model windows unknown |
| Account usage and reset windows (`rate_limit_event`) | IMPLEMENTED, TESTED (offline), OBSERVED | Shape inspected in a real `claude -p --output-format stream-json --verbose` run on 2026-11-18; the provider read a real log end to end (5-hour 21%, 7-day 41%, OBSERVED). Freshness is limited to the last worker run. Not yet seen through the running supervisor loop |
| Statusline usage ingestor | IMPLEMENTED, NOT TESTED | never fed a real payload |
| Crash-safe state store and worker registry | IMPLEMENTED, TESTED (offline) | 88 offline tests (lifecycle) |
| Worker lifecycle (assign, spawn, monitor, restart, stop, recover) | IMPLEMENTED, TESTED (offline) | fake launcher and clock; no real worker run by the lane |
| Headless worker command | OBSERVED | One run: exit 0, 8.5 s, init/assistant/rate_limit_event/result events; fails without `--verbose` |
| Interactive Main launch in a new console window | IMPLEMENTED, NOT TESTED | argument construction tested; no live launch |
| `start --yes` supervisor loop | IMPLEMENTED, NOT TESTED | not run live |
| Handoff build, write and verify | IMPLEMENTED, TESTED (offline) | 66 offline tests; live git and PR checkers NOT TESTED |
| Compaction decision | IMPLEMENTED, TESTED (offline) | decision and refusal rules only |
| Main replacement and heartbeat | IMPLEMENTED, TESTED (offline), NOT TESTED | no real heartbeat or retirer observed |
| Routing scheduler | IMPLEMENTED, TESTED (offline) | 70 offline tests; weights are PROPOSED, not calibrated |
| OmniRoute benchmark | OBSERVED | Two runs (2026-11-18): 2 of 7 routes answered both times (gemini-3.1-flash-lite 5/5, gemini-3-flash-preview 4/5). Groq fails with HTTP 403 Cloudflare error 1010 (client signature blocked at Groq's edge; the provider connection test passes), not a login problem |
| Claude Code through OmniRoute | OBSERVED (partial) | `claude -p` with the local gateway on gemini-3.1-flash-lite answers text; tool use fails (`400 Function call is missing a thought_signature`), so file-editing workers stay on Claude Pro |
| OmniRoute text-only workers (`dev_supervisor.text_worker`) | IMPLEMENTED, TESTED (offline), OBSERVED | 49 offline tests; live: summarise, classify, extract_json and review_notes CHECKED on the free Gemini route, plus Groq-to-Gemini fallback; output labelled REPORTED until a deterministic checker passes, then CHECKED and never promoted further |
| Paid API route | NOT TESTED | none exists; excluded unless `allow_paid_api` |
| Resolve discovery (MATCH/MISMATCH/UNCONFIGURED/UNAVAILABLE) | IMPLEMENTED, TESTED (offline), OBSERVED | 60 offline tests; live read-only run gave MISMATCH and UNCONFIGURED as described |
| Resolve bridge start from outside | NOT TESTED | the user must run `resolve_bridge`; raw bridge reply untested |
| GitHub progress log with spool and duplicate prevention | IMPLEMENTED, TESTED (offline), OBSERVED | 98 offline tests; one live comment, duplicate refused, read-after-write found 1 marker |
| Live issue create and close through the logger | NOT TESTED | not exercised |
| Orbit snapshot and view (live/cached/stale/offline) | IMPLEMENTED, TESTED (offline) | snapshot and evaluator tests |
| Orbit Electron window and IPC | NOT TESTED | electron not installed in the lane |
| Command line (`python -B -m dev_supervisor`) | IMPLEMENTED, TESTED (offline), OBSERVED | `doctor` ran live: required checks passed, Resolve UNCONFIGURED warning |
| Reusable onboarding to a second repository | PROPOSED | checklist in section 12 never run on another repository |
| Autonomy loop (`dev_supervisor.autonomy next/wait/stop-hook/main-settings`) | IMPLEMENTED, TESTED (offline), OBSERVED | 104 offline tests; a real Claude Code session with the generated `--settings` file was kept working by the Stop hook (blocked once, continued, then allowed); not yet wired into the supervisor's Main launch (PROPOSED) |
| Operation snapshot and Orbit views (next actions, waiting on others, blocked, idle reason, per-worker context, supervisor health, handoff) | IMPLEMENTED, TESTED (offline), OBSERVED | rendered from the real supervisor state; `cli snapshot` still uses the older builder (PROPOSED switch to `build_full_snapshot`) |
| Supervisor state repair (`dev_supervisor.repair`) | IMPLEMENTED, TESTED (offline), OBSERVED | applied once to the real state: retired a stale duplicate authoritative Main record |
| Worker waves run by Main | OBSERVED | waves w04, w05 and w06 (8 lanes) were implemented by worker subagents, verified independently by Main (including live checks that found and returned defects), closed and merged by Main as the Repository Coordinator |
| End-to-end autonomy acceptance harness (`dev_supervisor.acceptance`) | IMPLEMENTED, TESTED (offline), OBSERVED | 13/13 steps PASS in default mode and with `--real-worker` (a real haiku worker); handoff checkers, Main replacement and the context threshold are MOCKED or SIMULATED there |
| Supervisor-spawned real worker and unattended multi-cycle run | NOT TESTED | needs a supervisor restart with `worker_permission_mode` configured; workers so far were Claude Code subagents of Main |

## 15. Safety summary

Originals and the human's data are never modified; no secrets, launcher contents, transcript text or personal absolute paths in tracked files or GitHub posts; no paid spending unless configured by the human; no real Resolve project mutation; no bypass of branch protection or the repository's governance; the human owner keeps final authority.
