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
| A worker running a real task through the OmniRoute gateway | OBSERVED three times (`WORKER_GATEWAY.md`, sections 7 and 8): one failed on a provider 503, two completed small tasks alone; one was published with `intake` and merged through the gates. **Unattended operation and a dispatched reviewer are NOT DEMONSTRATED** |
| `python -B -m dev_supervisor restart <worker-id> [--yes]` restarts a FAILED, LOST or STOPPED worker through the same dispatch gate, within the restart budget and backoff | IMPLEMENTED (PR #179); tested with fake launchers only, never used on a real failed worker. Automatic restart stays off |
| `python -B -m dev_supervisor.intake <worker-id> [--yes]` publishes a finished worker's branch as a pull request with recorded provenance | IMPLEMENTED (PR #169); used for PR #172 |
| `python -B -m dev_supervisor.claim spec.json [--write]` registers one task and regenerates the handoff, applying the claim-PR rules | IMPLEMENTED (PR #159); used to register T-DEV-046 |
| `integrate --preflight` also checks the branch name (gate 1) and that `SESSION_HANDOFF.md` is current (gate 2) | IMPLEMENTED (PR #155) |
| `Allocation.independent` records whether a review route differs from the author's (True / False / None) | IMPLEMENTED (PR #160); written by a dispatched worker, finished by Main |
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

## Known limits and follow-ups

- Only one route is verified, with the limits stated in `WORKER_GATEWAY.md` (one trivial tool round trip; `cost_zero` REPORTED, not proven). Independent review is still done by fresh Claude subagents of the Main session: that route is too weak (capability 2) to be an allocated reviewer.
- `routing.py` (the `route` command, task-profile ranking that includes Claude routes) overlaps with role allocation and should be reconciled.
- `Allocation.independent` exists (PR #160) but nothing yet refuses a review whose flag is False; the merge gate still relies on the reviewer id not being an author id. The flag is a claim about routes, not proof that the reviewer was independent.
- Hand-built `VerifiedRoute` objects are not type-checked in `allocate` (the loader normalises them).
- Skipping the gate branch in `process_requests` is stopped only by `Supervisor.assign`'s own validation (a mutant that no test catches on its own).
- Proposals: a milestone id directly after a JSON escape sequence in an open task's text is not matched; falsy junk in `depends_on` is treated as none.
- Preflight: no test covers `wave/` branches or `CHANGELOG.md`; `git status` could use `--no-optional-locks`.
- The OmniRoute key in the gitignored `backup.ps1` was printed into two earlier session transcripts. It was rotated on 2026-11-24 and the old value is dead (`WORKER_GATEWAY.md`, section 2). The main gateway still serves inference without a credential on all interfaces; that is the owner's decision (section 3 there).
