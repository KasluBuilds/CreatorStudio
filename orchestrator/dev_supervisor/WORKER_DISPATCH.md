# Worker dispatch, route allocation and the merge preflight

Status labels follow the project convention: **IMPLEMENTED** (code and offline tests exist), **OBSERVED** (seen live), **NOT DEMONSTRATED** (never run for real).
Written 2026-11-24 for the overnight mission (tasks T-DEV-032, T-DEV-038, T-DEV-039, T-DEV-040). Read the code and the PR comments for evidence; this page is a map.

## What is true today

| Capability | Status |
|---|---|
| The supervisor's dispatch path starts workers only through one gate, with a verified zero-cost route and an isolated gateway-only environment | IMPLEMENTED (PR #124); unit tests plus a real-subprocess environment test. `worker_spawner.spawn_worker` (called only by tests) and the live-autonomy harness start `claude` separately and check the verified registry on their own |
| A worker record carries the route chosen by role (role, provider, model, capability, evidence id, reason) | IMPLEMENTED (PR #139) |
| `autonomy next` proposes eligible milestone work when nothing is actionable | IMPLEMENTED (PR #137) |
| `integrate --preflight` checks the offline merge gates before pushing | IMPLEMENTED (PR #144) |
| A worker running a real task through the OmniRoute gateway | OBSERVED three times (`WORKER_GATEWAY.md`, sections 7 and 8): one failed on a provider 503, two completed small tasks alone; one was published with `intake` and merged through the gates. The later runs of T-DEV-056 (sections 9 and 10) ended on a provider 503 twice and a third was stopped before it finished: none completed. A later fresh worker completed T-DEV-056 and that change was merged through the gates (section 11), the first worker-produced change taken to merge; Main still selected, dispatched, published, reviewed and merged it, so this is not unattended operation. A second fresh worker completed the follow-up T-DEV-063 the same way (section 12). **Unattended operation, automatic restart and a dispatched reviewer are NOT DEMONSTRATED** |
| `python -B -m dev_supervisor restart <worker-id> [--yes]` queues the restart of a FAILED, LOST or STOPPED worker through the same dispatch gate, within the restart budget and backoff | IMPLEMENTED (PR #179); first real use OBSERVED on 2026-11-24 (`WORKER_GATEWAY.md` section 9): the worker started again, then ended on the same provider 503. A running Supervisor loop must itself be restarted after this code is merged. Automatic restart stays off |
| `python -B -m dev_supervisor retire <worker-id> --reason TEXT [--yes]` queues the retirement of a LOST worker whose process is gone: it becomes STOPPED (note `retired: <reason>`), stops counting toward the one-worker cap, and keeps its restart count, failures, results and blockers; nothing is launched or signalled | IMPLEMENTED (T-DEV-059), tested offline; first real use on the live registry OBSERVED on 2026-11-24 (`WORKER_GATEWAY.md` section 11) |
| The dispatch gate refuses a worker when free physical memory is below a floor (default 1024 MB, `CREATORSTUDIO_MIN_FREE_MB`); an unreadable reading refuses | IMPLEMENTED (PR #186, T-DEV-053); REPORTED once refusing at 644 MB free (Main's command output, not in the event log). It reads only: it kills nothing and changes no setting |
| `python -B -m dev_supervisor.specdraft <milestone-id>` prints a claim-spec draft for an eligible milestone (title and description copied, task id, branch, worktree and agreement id by the naming rules); `owned_paths`, `acceptance_criteria` and `agreement` are listed as missing, never invented | IMPLEMENTED (PR #192, T-DEV-055); read-only; a human or Main must still supply the missing fields |
| `python -B -m dev_supervisor.cycle <task-id>` reports the stage of a task in the claim-to-close-out chain with OBSERVED evidence and the next action | IMPLEMENTED (PR #197, T-DEV-057); read-only; sees `origin/main` as of the last fetch; it does not drive the chain |
| `python -B -m dev_supervisor.testreq` with the action `plan`, `run`, `show` or `check` (and `--task T`) classifies a task's `test_requirements`, runs the command ones without a shell in the task worktree and stores evidence bound to the head, diff hash and requirement text; `integrate` appends a ninth `tests` row that refuses a merge without it | IMPLEMENTED (PR #223, T-DEV-065); used for the merge decisions of PRs #225, #230, #231, #235 and #236; see `TEST_EVIDENCE.md`; nothing runs it automatically |
| `claim` refuses a spec whose `test_requirements` contain a requirement that is neither a command nor gate wording | IMPLEMENTED (PR #231, T-DEV-066); the change was produced by a dispatched worker, with one test added by Main |
| `cycle` reports `needs_tests` when the eight gates pass but the tests check does not | IMPLEMENTED (PR #230, T-DEV-067) |
| `autonomy next` shows `dispatch_ready` and `dispatch_problems` for each `start_task` item | IMPLEMENTED (PR #236, T-DEV-069); reads the registry record only; selects and dispatches nothing |
| `autonomy next` offers no `worker_failed` item for a FAILED or LOST worker whose task is COMPLETED or RELEASED; it lists it under `suppressed` (BLOCKED task: a `blocked` entry) | IMPLEMENTED (PR #244, T-DEV-072); OBSERVED on the real registry (the T-DEV-043 item is gone and shown as `info (suppressed)`); `review_worker_result` for a finished task is still offered (reviewer note, not changed) |
| `worker_tests` scans finished workers each Supervisor tick and starts `testreq run` for their task requirements; verdict only from the sealed evidence record; state document `worker_tests`; events `worker_tests_*` | IMPLEMENTED (PRs #247 and #252, T-DEV-073 and T-DEV-074), unit-tested and run once offline end to end with a real repository and a real runner process; **NOT DEMONSTRATED in the live Supervisor**, which must be restarted by its owner to load the code. Details: `TEST_EVIDENCE.md` |
| `autonomy next` shows `worker_tests_failed` and `worker_tests_passed` items and a waiting note; `cycle` shows `worker_testing` and `worker_tests_failed` | IMPLEMENTED (PR #252, T-DEV-074) |
| `python -B -m dev_supervisor.task_select [--json] [--record]` gives every PLANNED task a verdict (selected or the reasons) and selects the first dispatchable one; `--record` appends `task_selected` or `task_selection_none` once per change of selection (a selection identical to the last recorded one is not recorded again) and keeps the `selection` document | IMPLEMENTED (PR #250, T-DEV-075); it never claims or activates a task, creates a worktree or prompt, or queues an assignment, and prints those steps instead. OBSERVED on the real registry: no PLANNED task exists, and no independent review path exists (no authoritative Main session, no review route of capability 3). **Queueing is NOT DEMONSTRATED** |
| `python -B -m dev_supervisor.intake <worker-id> [--yes]` publishes a finished worker's branch as a pull request with recorded provenance | IMPLEMENTED (PR #169); used for PR #172 |
| `python -B -m dev_supervisor.claim spec.json [--write]` registers one task and regenerates the handoff, applying the claim-PR rules | IMPLEMENTED (PR #159); used to register T-DEV-046 |
| `integrate --preflight` also checks the branch name (gate 1) and that `SESSION_HANDOFF.md` is current (gate 2) | IMPLEMENTED (PR #155) |
| `Allocation.independent` records whether a review route differs from the author's (True for a review, None for every other role); `allocate` refuses a review without the author's route or whose only fitting route is the author's own | IMPLEMENTED (PR #160; refusal added by T-DEV-054, tested offline only) |
| Any route listed as verified | OBSERVED for one route, on the isolated worker gateway only (`WORKER_GATEWAY.md`): `gemini` / `gemini/gemini-3.1-flash-lite`, two complete Claude Code round trips. The registry file lives in the state directory, not in the repository; without an entry every dispatch is refused |

Route state (OBSERVED, 2026-11-24): see `WORKER_GATEWAY.md` for the probe results, the corrected reading of the earlier 429 (the Gemini 2.5 routes are retired for this account; the 429 was the gateway cooldown after provider 404s, not exhausted quota), the isolated worker gateway and the credential rotation. Earlier observations: `OMNIROUTE_BENCHMARK.md` (2026-11-17, text-only) records `not_found` then `rate_limited` for the 2.5 routes and Groq `auth_error`; the report that Gemini 3.x routes break Claude Code tool turns (`thought_signature`) comes from earlier Main sessions (REPORTED, not recorded in a repository file).

## How a worker starts

1. `python -B -m dev_supervisor assign --task T --role R --branch B --worktree W --owner-stream S --prompt-file F --model M --provider P [--yes]`
   or, to let the registry choose, `--allocation-role {implementation,research,test_writing,review,architecture}` (see below). Without `--yes` it is a dry run. There is no default model or provider; without an allocation role, `--model` and `--provider` are required (exit 2).
2. `--yes` queues a request. The Supervisor loop (`start --yes`) processes it: it re-allocates from the registry when a role was named (the allocation in the request file is ignored), then runs the gate, then `Supervisor.assign`, then `spawn`.
3. The launcher builds the worker environment again and refuses anything unsafe, so a worker that was somehow assigned still cannot start on an unverified route.

## The gate (`dispatch_routing.decide_worker_dispatch`)

A worker is assigned only when all of these hold; every refusal names the failed check and never prints a URL, token or credential:

- the provider is not `anthropic` and the model is not a Claude model (any Claude alias or id, any prefix, non-ASCII refused);
- the exact (provider, model) pair is in `<state dir>/verified_routes.json` with `cost_zero` exactly `true` (a missing or unreadable file means nothing is dispatched);
- the supervisor's environment carries the gateway variables (`ANTHROPIC_BASE_URL`, a plain local http(s) URL, and `ANTHROPIC_AUTH_TOKEN`) and `provider_env.worker_env` accepts it: no API key, no OAuth or login variable, and no inherited `CLAUDE_CODE_*` variable passes through (the gateway flags it needs are set by `worker_env` itself); `worker_env` points `CLAUDE_CONFIG_DIR` at a worker-owned directory, which the launcher's environment builder creates at launch;
- at most one worker is assigned or live (a LOST worker still counts).

Worker `claude` processes also get `--setting-sources project` (`project,local` once the authority policy is installed), never the user's settings. `LaunchSpec.env` may only add `CS_AUTHORITY_*` variables. The only production `ClaudeCliLauncher` is built in `dispatch_routing`; a test (`LaunchPathAudit`) fails if a new way to start `claude` appears. Main still runs on the Claude login by design; that path is unchanged.

## The registry file

`<state dir>/verified_routes.json`, one entry per verified route:

```json
{"routes": [
  {"model": "<gateway model id, e.g. auto/coding>", "provider": "gemini", "evidence": "<where the verification is recorded>",
   "cost_zero": true, "roles": ["implementation", "research"], "capability": 2}
]}
```

- `provider` must be `gemini` or `groq`. `evidence` is a reference (PR, run id, note), never a secret; for allocation by role, evidence or a model name that looks like a credential makes the route unusable.
- `roles` and `capability` (1 weakest to 5 strongest) are optional. They state what the recorded evidence covers. A wrong type means "not recorded". An entry without them stays valid for an explicit dispatch but is never chosen by role.
- A model id is written as the gateway lists it; `worker_spawner.spawn_worker` refuses an id that is not `provider/model`.
- For allocation by role, duplicate (provider, model) entries are merged to the weakest claim: the lowest capability and only the roles every entry covers (an explicit dispatch checks the pair as written).
- **No tool creates this file.** An entry may be added only after a real Claude Code prompt, tool-result and final-response round trip through the gateway on that exact model has been observed and its evidence recorded. Do not add an entry from a text-only benchmark.

## Allocation by role (`model_allocation.py`)

| Role | Rule |
|---|---|
| implementation, research, test_writing | the lowest capability that fits, minimum 2 |
| review | minimum 3 and at least the author's capability (`--author-capability`); a route other than the author's (`--author-route PROVIDER MODEL`) always wins; the author's own route is used only when no other route fits, and the recorded reason says so |
| architecture | the highest capability available, minimum 4 |

No fitting route is a refusal with a reason: never a fallback, never a Claude model, never a route without recorded zero cost. Ties are broken by provider then model, so the result does not depend on registry order. The choice is recorded on the worker (`Worker.allocation`) and the `worker_assigned` event names the allocation role. "Capability" is a recorded claim about a route, not a measurement.

## Autonomy proposals and the merge preflight

- `python -B -m dev_supervisor.autonomy next` lists registered work first. When nothing is actionable it also lists `PROPOSED` milestones from `PROJECT_STATUS.json`: not started, every dependency implemented, no open task mentions them; milestones with open questions are flagged as needing a decision. Proposals never register anything.
- `python -B -m dev_supervisor.integrate --preflight [--repo ..]` evaluates the branch name, handoff currency, scope, secrets, forbidden files and reserved paths on the committed HEAD against `origin/main`, with no pull request, and prints the content diff hash a review must bind to. Run it before pushing. It does not check CI, merge state or review. A registry-only PR may add its own new task, step and agreement records and update the records of the task whose registered branch it is on; nothing else.
- Merge with `python -B -m dev_supervisor.integrate <PR> --yes` (it re-evaluates every gate under a lock and merges pinned to the evaluated head).

## What now refuses (and why old documents may disagree)

- `live_autonomy` needs `CREATORSTUDIO_VERIFIED_ROUTES` pointing at a verified registry that lists the model, and refuses every Claude model. `LIVE_AUTONOMY.md` describes a haiku run from 2026-11-19; that evidence is historical and the harness cannot be re-run on haiku.
- `session_policy live-probe --yes` and `context_monitor live-compaction-probe --yes` refuse unless a process runner is injected, because they would start a real Claude session on the Claude login. Their 2026-11-19 results remain historical evidence.

## Retiring an orphaned worker (T-DEV-059)

A worker whose process vanished while no Supervisor was watching is marked LOST at the next Supervisor start (`Supervisor.recover`). A LOST worker still counts toward the one-worker cap, so until it is dealt with no new task can be dispatched. `restart` clears it but spends one restart. `retire` clears it without spending anything:

1. Start the Supervisor loop (it marks the dead RUNNING worker LOST; nothing is relaunched).
2. From another window: `python -B -m dev_supervisor retire <worker-id> --reason "<why>"` (dry run), then the same with `--yes`. The running loop applies it on its next tick and writes a `worker_retired` event with the reason and the previous note.

What it refuses: any status except LOST (a RUNNING worker is refused, even with a dead pid: let the Supervisor mark it LOST first), a LOST worker whose recorded pid is alive (pids can be reused; stop that process by hand), a missing reason, an unknown worker. Retiring twice is a no-op. The retired worker is STOPPED, so `restart` can still use its remaining attempts later; retirement is not a ban. It does not touch the task registry or the task's claim.

## Running the Supervisor from a normal PowerShell window (T-DEV-059, T-DEV-061)

The Supervisor loop is a foreground command. It must run in an ordinary PowerShell window, not as a background command of a Claude Code session (Claude Code stopped such commands under memory pressure twice on 2026-11-24, REPORTED by Main). It selects no task and starts no worker by itself: it only applies requests that someone queues with `assign`, `restart` or `retire`, and it watches the workers it started.

1. Start (the two variables point worker processes at the isolated worker gateway; the token is a placeholder, not a credential, because that gateway does not check it on the messages endpoint, `WORKER_GATEWAY.md` section 10):

   ```powershell
   cd C:\Users\Korisnik\CreatorStudio\orchestrator
   $env:ANTHROPIC_BASE_URL = 'http://127.0.0.1:20130'
   $env:ANTHROPIC_AUTH_TOKEN = 'worker-gateway-placeholder'
   python -B -m dev_supervisor start --yes --no-main
   ```

   Expected, as observed in a throwaway state directory with the real code (OBSERVED): `recovery: N item(s), 0 blocker(s); nothing relaunched`, then one line `tick K: main_alive=False requests=0 worker_changes=0` every 15 seconds. A RUNNING worker whose process is gone is marked LOST at that point, with its restart count unchanged. A second `start` while one is running is refused ("already running").
2. Health, from a second window: `python -B -m dev_supervisor status`. Expect a `lock:` line whose dictionary shows `'alive': True`, `supervisor heartbeat: FRESH` with a tick number that rises every 15 seconds (it turns STALE after at least 45 seconds without a tick: three poll intervals, or three times the last tick's duration if that is longer), and the worker counts. `assign` and `restart` run from another window also need the two gateway variables set in that window.
3. Free the worker slot of an orphan without spending a restart, from the second window, after the Supervisor has marked it LOST: `python -B -m dev_supervisor retire <worker-id> --reason "<why>"` (dry run), then the same with `--yes`. The loop applies it on its next tick; `status` then shows the worker as STOPPED. A worker still RUNNING is refused: let the Supervisor start first.
4. Stop: Ctrl+C in the start window. It prints `interrupted; supervisor loop stopped (Main and workers keep running)` and releases the lock. Closing or killing the window instead leaves a stale lock, which the next `start` replaces. Both are covered by tests in `test_dev_cli.py` (`StartLifecycleTests`, PR #207); neither was exercised against the real registry.
5. `assign` and `restart` stay gated when the loop applies them: the memory floor (1024 MB), one active worker and a verified route; `restart` also needs its restart budget and backoff. `retire` has no dispatch gate: it launches nothing and only frees a slot. The loop never restarts a worker on its own (`auto_restart` is off).

Throwaway end-to-end run of the retire command (OBSERVED, real code, a temporary state directory, no worker launched): `retire` on a RUNNING orphan was refused; `start --max-ticks 1` marked it LOST (restart count 2 stayed 2); `retire --yes` queued one request; the next `start --max-ticks 1` applied it (status STOPPED, restart count 2, note `retired: e2e check`); a second `retire` answered "already retired" and queued nothing. The real registry has not been touched by it.

## Known limits and follow-ups

- Only one route is verified, with the limits stated in `WORKER_GATEWAY.md` (one trivial tool round trip; `cost_zero` REPORTED, not proven). Independent review is still done by fresh Claude subagents of the Main session: that route is too weak (capability 2) to be an allocated reviewer.
- `routing.py` (the `route` command, task-profile ranking that includes Claude routes) overlaps with role allocation and should be reconciled.
- `allocate` now refuses a review whose route would be the author's own, or whose author route is not given (T-DEV-054), so `independent` is never False. This compares routes only: it is a claim about routes, not proof that the reviewer was independent, and the merge gate still relies on the reviewer id not being an author id. The registry holds no route with the `review` role at capability 3 or more, so no OmniRoute reviewer can be allocated today; reviews use fresh Claude subagents of the Main session.
- Hand-built `VerifiedRoute` objects are not type-checked in `allocate` (the loader normalises them).
- Skipping the gate branch in `process_requests` is stopped only by `Supervisor.assign`'s own validation (a mutant that no test catches on its own).
- Proposals: a milestone id directly after a JSON escape sequence in an open task's text is not matched; falsy junk in `depends_on` is treated as none.
- Preflight: no test covers `wave/` branches or `CHANGELOG.md`; `git status` could use `--no-optional-locks`.
- The OmniRoute key in the gitignored `backup.ps1` was printed into two earlier session transcripts. It was rotated on 2026-11-24 and the old value is dead (`WORKER_GATEWAY.md`, section 2). The main gateway still serves inference without a credential on all interfaces; that is the owner's decision (section 3 there).
