# Worker gateway, credential rotation and route evidence

Status labels: **OBSERVED** (seen on this machine on the date given), **REPORTED** (stated by a source, not re-checked), **NOT DEMONSTRATED** (never run).
Written 2026-11-24 for task T-DEV-042 (second overnight mission, Priorities 1 and 2). This page names no token, key or credential fragment and must stay that way.
Companion pages: `WORKER_DISPATCH.md` (the dispatch gate and the registry file), `OMNIROUTE_BENCHMARK.md` (the 2026-11-17 text benchmark).

## 1. State in one table

| Question | Answer |
|---|---|
| Is a worker route verified? | Yes, one: provider `gemini`, model `gemini/gemini-3.1-flash-lite` (the gateway's model id, which is the string the registry and `--model` use), on the **worker gateway** below. The registry entry is written in the state directory, not in the repository; it is not part of this change. Two complete Claude Code round trips passed (OBSERVED, section 4). |
| Has any worker run through the dispatch path? | Several times (OBSERVED; sections 7 and 8 describe the first three, sections 9 to 12 the later ones, including two small changes taken to merge in sections 11 and 12). The first three: the first run failed on a provider 503 and Main finished its task; the second and third each completed a small task alone, and the third was published with the intake command and merged through the gates. **Autonomous operation is not declared complete**: task selection and review spawning are still manual, a failed worker restarts only on an explicit `restart` command (PR #179; used once on a real failed worker, which then failed again on a provider 503, section 9), and no dispatched independent reviewer exists. |
| Is the exposed gateway key still valid? | No. It was rotated on 2026-11-24 (section 2). |
| Is the main gateway safe from other machines? | **No, and unchanged.** It answers inference requests without any credential on all network interfaces (section 3). That needs the owner's decision. |
| Can the main gateway run the verified route? | No. The installed version (3.8.49) fails Claude Code tool turns on Gemini 3 routes. Version 3.8.54 passed on `gemini-3.1-flash-lite` (INFERRED to be the release fix, because the release notes name it; the same model was not re-tested on 3.8.49). |

## 2. Credential hygiene (Priority 1)

**Exposure (OBSERVED).** The management key that the gitignored launcher holds was printed into two earlier session transcripts on this machine. A byte-level scan found it in exactly three files: the launcher and two session transcripts. The scan covered all 2025 git objects of the repository (all refs, unreachable ones included), the repository checkout, all linked worktrees, the supervisor state directory, the gateway's data directory with its logs and call logs, the Claude session and settings folders, and the top levels of the temp directory (text-like files only; very large or binary files were skipped). The credential-bearing variables of the user, machine and process environments were compared by value and none held it. The value is in no tracked file and in no task record.

**Rotation (OBSERVED, 2026-11-24).**
1. Recoverable copies were made first, in a user-private `recovery` folder inside the gateway's data directory: a copy of the launcher and a consistent snapshot of the gateway database (integrity check `ok`, one key row).
2. A new key was created with the gateway's default minimal scope (usage only, no management). The gateway listed it and recorded a use after one request.
3. The launcher was rewritten byte for byte with only the token value changed (same length, same line count, parses without errors in PowerShell).
4. The old key's record was regenerated, so the exposed secret is dead while the record survives. The replacement secret of that record was discarded unread (the owner can reveal it in the dashboard).
5. The old token now gets the same refusal (HTTP 403) on the management API as an invalid token. The new token appears only in the launcher.

**What stays.** The old, now dead, value remains in the two local transcripts and in the recovery copy. Editing session transcripts was judged destructive and unnecessary for a dead credential. The new key cannot manage the gateway; management goes through the dashboard login. A Supervisor process started before the rotation still holds the old value in its environment: it is inert because the key is dead (dispatch is separate, section 5) and is replaced when the Supervisor is restarted.

**One more slip (OBSERVED).** While inspecting provider connections, a debugging dump printed the first 200 characters of a nested Kiro client-secret field. It is a truncated, unsigned fragment. Re-authorising the Kiro connection in the dashboard removes any remaining doubt. Nested provider fields must never be dumped.

## 3. Gateway findings (Priority 2)

Label note: a statement marked OBSERVED was seen directly; where a conclusion is drawn from observations it says INFERRED.

- **Anonymous inference on every interface (OBSERVED).** On the main gateway (port 20128, listening on all interfaces) requests to `/v1/messages` and `/v1/chat/completions` with no token and with an invalid token were processed; one reached the provider and came back with the provider's own error. Only `/api/*` needs the key. INFERRED, not tested from another host or against the firewall: anyone who can reach the machine can spend the owner's free-tier provider quota. The gateway's own start-up text (3.8.54) says this is a deliberate local-first default and names advisory GHSA-wmgv-ph3p-rv57; its two remedies are `REQUIRE_API_KEY=true` or binding loopback with `OMNIROUTE_SERVER_HOST=127.0.0.1`. **Not applied to the main gateway**: other tools may rely on it, so it is the owner's decision.
- **Gemini 2.5 routes are retired for this account (OBSERVED for `gemini-2.5-flash`, INFERRED for `gemini-2.5-flash-lite`, which returned a bare 404).** A request returned the provider's 404 "no longer available to new users". The "429 all credentials cooling down" seen on 2026-11-24 was the gateway's cooldown after such 404s, not exhausted quota. Re-probing them is pointless.
- **Gemini 3 tool turns fail on gateway 3.8.49 (OBSERVED earlier, REPORTED cause).** The failure is the provider's missing `thought_signature` on the second turn. The gateway release notes list "replay Gemini thought_signature on the direct Claude to Gemini path" in 3.8.50. The installed version predates it.
- **Other routes (OBSERVED, one attempt each, not repeated):** Groq returns a Cloudflare 403 from the gateway; the OpenCode free model answered "Model is unavailable"; `gemini-3.5-flash` timed out after 150 s in a Claude Code round trip (and timed out in the 2026-11-17 benchmark).

## 4. Route evidence on the worker gateway

**Worker gateway.** A second, separate OmniRoute 3.8.54 installation, started with its own data directory, bound to loopback only, carrying a copy of the database reduced to the Gemini connection (the Kiro, Groq and OpenCode connections and every API key row were deleted from the copy, so a second instance cannot disturb their OAuth tokens and the exposed key does not exist there). It listens on port 20130. The main gateway was not touched.

**Probe (OBSERVED, 2026-11-24).** One headless Claude Code run per attempt, built with the same environment isolation the spawner uses, asked to read a sentinel file with the Read tool and to answer with its content. Result both times: exit 0, 2 turns, no error, the sentinel returned. Wall time 54 s and 68 s. The gateway's call log recorded provider `gemini`, model `gemini-3.1-flash-lite`, status 200. The second attempt ran after the final configuration (loopback bind, reduced database). A direct raw request in between received a transient provider 503 "high demand"; expect slow and occasionally failing free-tier calls.

**Limits of the claim.**
- It covers one trivial tool round trip. It is not evidence that the model can implement a task. The registry entry therefore claims capability 2 (of 5) and the roles implementation, research and test_writing.
- `cost_zero` is **REPORTED, not proven**: the Gemini connection is configured to import free models only, and an earlier session recorded a response-cost header of zero for this route. The gateway computes cost from a price table, and the billing tier of the Google key was not checked on Google's side.

## 5. Start and use (PowerShell)

1. Install beside, not over, the main gateway: `npm install --prefix <tools dir> omniroute@3.8.54`.
2. Prepare the data directory from a database snapshot; delete every row of `api_keys` and every `provider_connections` row except `gemini` in the copy; copy the gateway's `.env`. That file holds credentials: keep it out of the repository, logs and reports.
3. Start it from **PowerShell** with `$env:DATA_DIR` set to that directory and `$env:OMNIROUTE_SERVER_HOST = '127.0.0.1'`, then `node <tools dir>\node_modules\omniroute\bin\omniroute.mjs serve --port 20130 --no-open --log`. Check that the listener's address is 127.0.0.1. Pitfall (OBSERVED): `serve` respawns a killed child with its original environment, so a wrapper started earlier from another shell keeps rebinding all interfaces; stop wrappers before children.
4. Start the Supervisor with `ANTHROPIC_BASE_URL=http://127.0.0.1:20130` and an `ANTHROPIC_AUTH_TOKEN` that is **not** the main gateway key (this gateway does not check it). `provider_env.worker_env` accepts local URLs only.
5. Add the route to `<state dir>/verified_routes.json` (format in `WORKER_DISPATCH.md`) with model `gemini/gemini-3.1-flash-lite`, provider `gemini`, `cost_zero` true, `roles` and `capability` 2, and this page as evidence. Without `roles` and `capability` the entry is never chosen by role.

## 6. Recommendations for the owner

1. Decide on the main gateway: bind loopback or require an API key (section 3). Both change behaviour for other tools.
2. Upgrade the main gateway to 3.8.50 or later, or keep using the worker gateway for workers. Upgrading changes a working service; keep the database snapshot and the previous version to roll back.
3. Re-authorise the Kiro connection (section 2).

## 7. First dispatched worker run (T-DEV-043), 2026-11-24

Superseded in part: section 8 records two later runs that completed their tasks alone; the 'NOT DEMONSTRATED' items below that those runs answered are marked there. The figure of 28.7 minutes is Claude Code's own run duration; the supervisor record gives 29.1.

Labels: OBSERVED = read from the Supervisor event log, the worker's stream log or the worker gateway's call log on this machine.

**What ran.** `assign --allocation-role implementation` resolved the registry's only route (`gemini/gemini-3.1-flash-lite`), the dispatch gate passed, and the Supervisor started worker `w-b88c568c18` in its own worktree with the isolated worker environment that the launcher builds and re-checks (the gate and the launcher refuse a login, an API key or a non-local gateway). The task was small: add `Allocation.independent` with tests, two owned files.

**What happened (OBSERVED).**
- The worker's command line carried the authority policy: its hook (policy `worker-routine-allowlist`) **denied one compound shell command** (`cd orchestrator && python ...`, "not one simple command"). Workers must issue single simple commands; from the repository root the test command `python -B -I -m unittest discover -s orchestrator/tests -p <file>` is one.
- Wall time 28.7 minutes, 8 reported turns, tool calls: 2 Read, 3 Edit, 2 Bash (one denied).
- The worker gateway answered **48 calls** in that window: 7 succeeded (HTTP 200), 25 returned 503 "high demand", 11 returned 429, 3 timed out at 110 s (504) and 2 were cancelled by the client (499). The free route is slow and unreliable.
- Claude Code ended with `API Error: 503` after its own bounded retries (exit 1). The Supervisor recorded `worker_failed` (signature `exit:1`) with a restart-not-before time. `auto_restart` is off by default, so the worker stays FAILED until someone restarts it. At the time of this run the CLI had no restart command; it was added afterwards (PR #179, below).

**What the worker produced (kept as written).** The new field, its computation and the main new test, correct as reviewed (the worker's test needed two additions, below). It never ran the tests and so never noticed that one existing assertion pins the exact `to_dict()` keys.

**What Main did.** Updated one existing test (renamed, its pinned key list extended), ran four suites, and, after the first independent review found two uncaught mutations, added a test for them. Merged as PR #160 with all eight gates passing and an independent review bound to the diff hash. The PR body discloses this split.

**What this does and does not show.**
- OBSERVED: a worker can start through the production gate on a verified zero-cost route, work in its own worktree under the authoritative hook, and produce correct code. Failure was bounded (one run, no loop) and recorded.
- NOT DEMONSTRATED: a worker finishing a task alone (tests, commit, handoff), a dispatched independent reviewer, an automatic restart, a complete select-register-dispatch-implement-review-merge-select cycle.
- Reliability: with 7 of 48 gateway calls succeeding (a raw call ratio that counts Claude Code retries and client cancellations, not a per-task rate), a free Gemini route cannot yet carry a cycle unattended. No other verified route exists on the gateway today (`WORKER_GATEWAY.md` section 3).
- Did role allocation reduce general-purpose usage? **Not measurable from one run.** By construction a dispatched worker spends no Claude login usage; independent review and Main's own work still do, so the saving is bounded by how much implementation the free route can carry.

**Next steps.**
1. Re-dispatch one small task with the prompt telling the worker to use single simple commands and the root-level test command, and to commit before finishing; compare with this run.
2. Done afterwards: the `restart <worker-id> [--yes]` command (PR #179, dry run by default, same dispatch gate, the lifecycle restart budget and backoff, `auto_restart` still off). It was first used on a real failed worker later the same day (section 9).
3. Owner decisions in section 6 (main gateway exposure, upgrade, Kiro re-authorisation).
4. A reviewer route of capability 3 or more must be verified before a dispatched reviewer is possible.

## 8. Runs 2 and 3, the intake command and the demonstrated chain (2026-11-24)

Labels as in section 7. Figures are from the supervisor's worker records and the worker gateway's call log.

| Run | Task | Worker | Result | Wall time (supervisor record) | Gateway calls (ok / all) |
|---|---|---|---|---|---|
| 1 | T-DEV-043 | `w-b88c568c18` | FAILED on a provider 503 after correct partial work; Main finished it (PR #160) | 29.1 min | 7 / 48 |
| 2 | T-DEV-047 | `w-195e048477` | EXITED: code, test, test run and commit all by the worker (PR #167) | 3.5 min | 12 / 19 (4 x 503, 3 x 429) |
| 3 | T-DEV-049 | `w-51544909ee` | EXITED: a two-line test change, by the worker (PR #172) | 1.3 min | 9 / 9 |

The same route was unreliable in the first run and reliable in the third. The cause is not known (INFERRED: provider load that varies with the time of day). Nothing here promises that the route stays reliable.

**The chain, as it ran for T-DEV-049, and who did each step.**
1. Choose the task: Main, by hand (a review note on the previous task). `autonomy next` does not yet turn proposals into specs.
2. Register it: the `claim` command (a tool) writes the registry record and the handoff from a spec file. Main created the branch, committed, pushed and opened the claim pull request, and merged it with `integrate --yes` (a tool that evaluates the eight gates and merges only when all pass).
3. Dispatch: Main ran `assign --allocation-role implementation`; the Supervisor started the worker through the production gate.
4. Implement, test, commit: the worker, in its own worktree, under the authority hook. It never pushes.
5. Verify: Main ran the test file and a mutation of the code under test before publishing. This is Main's check, not a tool's.
6. Publish: the `intake` command (a tool), dry run first, then `--yes`: it merged the base branch in, pushed and opened the pull request with the recorded provenance.
7. Independent review: a fresh Claude subagent of the Main session, bound to the diff hash. Same account, different context; not a dispatched worker.
8. Merge: Main ran `integrate --yes`; the tool evaluated all eight gates, found them PASS and merged, one pull request at a time.
9. Close out the task in the registry: a registry-only pull request on the task's own branch, prepared and pushed by Main with a small script and merged like step 8.

**Still manual or not demonstrated (be exact).**
- Task selection, writing the spec and the worker prompt; creating the branch and worktree; committing, pushing and opening the claim and close-out pull requests; running `integrate --yes`.
- The verification in step 5, and spawning the reviewer in step 7.
- A **dispatched independent reviewer**: no verified route reaches capability 3, the minimum for the review role.
- **Restart of a failed worker**: `python -B -m dev_supervisor restart <worker-id> [--yes]` exists (IMPLEMENTED, PR #179: refuses a worker that is active or BLOCKED, past its restart budget or inside its backoff, whose route no longer passes the dispatch gate, or whose prompt file or worktree is gone; the running loop applies the gate again). It was exercised in tests with fake launchers and once on a real failed worker (section 9: the worker started again, then ended on the same provider 503). `auto_restart` stays off, so nothing restarts without that command.
- **Unattended operation**: nothing starts the next task by itself, and the route's reliability is not established.
- Only small, precisely specified tasks have been tried (2 files at most, a prompt that names the exact change). Nothing says a free capability-2 model can do open-ended work.

**The `intake` command (IMPLEMENTED, PR #169).** `python -B -m dev_supervisor.intake <worker-id> [--yes] [--base main]`. It refuses, naming the unmet condition and doing nothing, when the worker is unknown or still active, is not on its registered `task/` branch, has uncommitted changes, has no commit of its own, has no registered task for that branch on `origin/main`, or the merge preflight fails or is unknown. With `--yes` it merges `origin/main` in when the branch is behind (aborting and refusing on failure), repeats the preflight, pushes without force and opens the pull request, after checking that no pull request is open and that the body contains no credential shape. Worker-written text is neutralised (no pings, no forged markers, no `#123` references). It never merges a pull request, reviews, edits files or force-pushes. The body says UNREVIEWED and discloses a worker that did not finish.

**Starting everything again after a reboot (PowerShell).**
1. Worker gateway: section 5 above (install once; start with `DATA_DIR` and `OMNIROUTE_SERVER_HOST=127.0.0.1`; check the listener address).
2. Supervisor: from `orchestrator/`, with `ANTHROPIC_BASE_URL` set to the worker gateway and a placeholder `ANTHROPIC_AUTH_TOKEN`, run `python -B -m dev_supervisor start --yes --no-main`.
3. Route registry: `<state dir>/verified_routes.json` persists; it is not in the repository.
4. A worker is assigned with `assign ... --allocation-role implementation --yes` and a prompt file that tells it to use single simple shell commands and to commit but not push.

## 9. Later on 2026-11-24: first real restart, and what stopped T-DEV-056

Labels as in section 7.

- **Worker run 4 (T-DEV-056, `w-1c6bd827e3`), OBSERVED.** A two-file change taken from the Known limits list (preflight reads `git status` without optional locks). It was dispatched through `assign --allocation-role implementation`. It ended FAILED on a provider 503 ("high demand") after about two minutes, with no tool call and no change in its worktree.
- **First real use of `restart`, OBSERVED.** The restart request was queued, and the long-running Supervisor loop answered `unknown op 'restart'`. That loop had been started at 07:33 local time (06:33 UTC), before the restart operation was merged, and a running loop does not load merged code. After the loop was stopped (REPORTED by Main from the process list and the worker list at that moment: no worker was running and Main was not attached) and started again from the merged checkout, `restart w-1c6bd827e3 --yes` queued, the loop restarted the worker, and it was RUNNING again within seconds (restart 1 of 3). The restarted run ended on the same 503 after about two minutes, again with no change. **Lesson:** merging code that the loop executes does not change a running loop; restart the loop after such a merge.
- **No further retry.** The route was saturated twice in a row; the mission rule is not to hammer an unavailable provider, so the worker was left FAILED (restart budget used: 1 of 3). T-DEV-056 is NOT completed and no work of it exists. Its claim stays ACTIVE.
- **Memory floor, REPORTED by Main from its own command output (not in the event log).** While free memory sat near 650 MB, `assign` was refused by the memory guard (`memory guard: 644 MB free, at least 1024 MB required`). The dispatch went through when free memory rose to about 1.5 GB. The refusal worked as designed; the cause of the low memory was other applications on the machine, which were not touched.

## 10. Evening of 2026-11-24: route probes and the orphaned worker

Labels as in section 7. Times are UTC.

- **Orphaned worker, OBSERVED (registry read, read-only).** `w-1c6bd827e3` (T-DEV-056) is recorded RUNNING with a pid that no longer exists, `restart_count` 2 of 3 and two recorded failures (`exit:1`, `exit:1`). The second restart (its third run) was stopped when Claude Code reaped the background command that was watching it (REPORTED by Main: "the system is running low on memory"). Nothing of that run is in the worktree. One restart remains. It was not spent.
- **Gateway day record, OBSERVED (call logs of the worker gateway, read at about 21:11).** 124 calls that day: 32 answered 200, 68 answered 503, 19 answered 429, 3 answered 504, 2 were cancelled (499). The last failure was at 18:20; between 17:00 and 18:59 nothing succeeded (0 of 38).
- **Token, OBSERVED.** Three `POST /v1/messages` requests with an empty body answered 400 (body validation): no token, the placeholder in `x-api-key`, and the placeholder as a bearer token. `GET /v1/models` answered 401 with no token and with the placeholder. The messages endpoint therefore does not check the token on this loopback gateway, which matches section 5. The placeholder is not a credential. The three 400s were logged by the gateway after the day record above was read, so they are not among its 124 calls; the two 401s were not logged.
- **Probe 1, OBSERVED (21:12 to 21:14).** One headless Claude Code run, built with the same environment isolation the spawner uses, in a throwaway directory, asked to read a sentinel file with the Read tool and answer with its content. It finished with exit 0 after 140.8 s, two tool calls, no error, and the sentinel returned. The gateway logged 8 calls for it: 3 answered 200 and 5 answered 429 with the provider's own wording "Resource has been exhausted (e.g. check quota)". Claude Code retried through the 429s by itself.
- **Probe 2, OBSERVED (21:29, about 15 minutes later).** The same run again: exit 0 after 45.5 s, one tool call, sentinel returned. The gateway logged 2 calls, both 200, no 429.
- **What the probes show.** The five 429s of probe 1 carry the provider's own wording (OBSERVED); that they were a free-tier quota or rate limit and not the gateway's own cooldown (section 3 records a different, gateway-originated 429) is INFERRED. The same request shape succeeded within the same run and again about 15 minutes later in probe 2. The route was usable on two complete Claude Code round trips, each with a tool call and a final answer. The probes were foreground commands, not workers, and used no restart. This supersedes the restart count given in section 9: the registry now shows 2 of 3 used.
- **What they do not show (INFERRED, not tested).** Reliability over a full task (a worker run makes many calls and may meet the quota or a 503 again), that the model can implement the T-DEV-056 change, or how long the good period lasts. Both probes were small; the two earlier real attempts failed on 503 at about 17:00 to 18:00.
- **Both runs printed** `[claude-code:unrecognized_model]` for the model name on stderr and still exited 0.

## 11. First worker-produced change taken through to merge (night of 2026-11-24, UTC)

Labels as in section 7. Everything below was read from the registry, the worker's own log, the call log and GitHub after the fact.

- **Supervisor, OBSERVED.** The owner had started the Supervisor in a normal PowerShell window. `status` showed one lock with `'alive': True` (the second `python` process was its launcher parent), a FRESH heartbeat and a rising tick, and the orphan `w-1c6bd827e3` as LOST.
- **First real use of `retire`, OBSERVED.** `retire w-1c6bd827e3 --yes` was queued; within a few ticks the worker was STOPPED with note `retired: orphaned by stopped Claude session; process confirmed dead`, restart count still 2, failures `['exit:1','exit:1']` unchanged, and the count of LOST plus active workers was 0. No process was launched.
- **Dry-run and route check, OBSERVED.** The dry-run `assign` for T-DEV-056 was valid (before the retirement the same command was refused only by the concurrency cap). A foreground probe just before dispatch (free memory 1763 MB, floor 1024 MB) completed in 10 s with one tool call.
- **Worker run, OBSERVED.** A fresh worker `w-80f5a2f6a8` (restart count 0) started at 22:50:28 and ended at 22:59:30 with exit code 0: 27 turns, 26 tool calls (8 Read, 8 Edit, 9 Bash, 1 Write). It made the two edits asked for in the prompt, ran `test_dev_integrate.py`, and committed on the task branch; three forms of `git commit -m` with a multi-line message were each blocked by the authority hook, so it wrote a file `commit_msg.txt` and used `git commit -F`; it also tried `rm commit_msg.txt`, which the hook held for approval, which is why the file was left behind (from its own log and the reviewer's reading of it). Inside the worker's own window (22:50:28 to 22:59:30) the gateway logged 35 calls: 26 answered 200 and 9 answered 429; no 503. (A wider 22:50 to 23:00 count gives 37 because it includes the two calls of the pre-dispatch probe.)
- **What the worker produced, OBSERVED.** One commit, two files, +8 −4: the one-argument change in `integrate.py` and the matching changes in `test_dev_integrate.py` (the fake git's status match, two widened command-name assertions, one new test). Nothing outside the owned paths was committed. It left the untracked file `commit_msg.txt` in the worktree root, which Main moved out of the worktree before publishing.
- **What Main did, OBSERVED.** Chose the task (it was already claimed and had a prompt), checked the claim and the owned paths, merged main into the worktree, queued `retire`, ran the probe and the dry run, queued the assignment, watched the worker through `status` and `workers`, ran the required test file again (142 tests, OK), published the branch with `intake` (pull request #211, review bound to diff `d545a537b1f69f44`), had a fresh read-only subagent review it (APPROVE, with one non-blocking note: the widened command-name assertions could strip the leading option instead), merged it with `integrate 211 --yes` after CI and all eight gates passed (merge commit `a5a8a082611c`), and closed the task out in the registry (#212).
- **Retries.** The new worker used none. The old worker's restart count stayed at 2 of 3.

**What this shows.** A free-route worker, started by the Supervisor, produced a small, correct, test-covered change that went through independent review, CI, all eight gates, merge and close-out.

**What it does not show.** The task was chosen, claimed and prompted by Main, and the dispatch, publication, review request, merge and close-out were all driven by Main. Main did not select a task unattended. The reviewer was a Claude subagent, not a dispatched worker (no capability-3 route exists). The Supervisor does not select or dispatch anything by itself. One 9-minute, two-file change is not evidence of reliability on larger tasks, and the route met 429s during the run. The system is not autonomous.

## 12. Second worker-produced change (T-DEV-063) and the state left for the next Main

Labels as in section 7. UTC, night of 2026-11-24 to 2026-11-25.

- **Task and prompt, OBSERVED.** T-DEV-063 (tests only, one file, `test_dev_integrate.py`) was a follow-up to the non-blocking note on the review of PR #211: two read-only-whitelist assertions had been widened to accept the option `--no-optional-locks` as if it were a command name. Main wrote the claim (acceptance criteria included), had it merged (#215), and wrote an exact prompt.
- **Worker run, OBSERVED.** Fresh worker `w-1965a9ef72`, started 23:17:53, ended 23:22:38, exit 0, restart count 0. 15 turns, 14 tool calls (1 Skill, 1 Read, 4 Edit, 2 Grep, 6 Bash). The gateway logged 20 calls in that window: 14 answered 200, 6 answered 429, no 503. Seven tool results in its log were errors: one `Skill` call (it tried to invoke `lean-engineering`; the answer was `Unknown skill`), one `Edit` (the text matched twice), two `Grep` calls (a regular-expression error), and three `Bash` calls blocked by the worker allowlist hook (a bare `cd orchestrator` and two `git commit -m` forms that carried a trailer line, one with a newline in the message and one with a second `-m`). Its single-line commit then worked and is the one in the history. No stray file was left.
- **What it produced, OBSERVED.** One commit, one file, +11 −2: a `command_names` helper, both assertions switched to it with the whitelist restored to exactly `fetch, rev-parse, status, merge-base, diff, show, log`, and one new test. `test_dev_integrate.py`: 143 tests, OK (run by Main as well as by the worker).
- **Main's steps, OBSERVED.** Claim, prompt, worktree, Supervisor check, dry-run assignment, pre-dispatch probe (14 s, 2444 MB free; REPORTED from Main's console output, not in the event log), queued assignment, publishing with `intake` (#217, diff `a2dc1a20949388c5`), a fresh read-only subagent review (APPROVE, with mutation checks: injected `push` calls and removal of the flag handling made the tests fail), merge through all eight gates (merge commit `2929250533e9`), close-out (#218).
- **What this adds.** A second small change by a worker through the same chain, with no restart and no 503. It does not change the limits stated in section 11: the task, prompt, dispatch, publication, review request, merge and close-out were all driven by Main; the reviewer was a Claude subagent; the sample is two small tasks on one free route that returned 429s during both runs. The system is not autonomous.

**State left for the next Main session (registry and Supervisor read at the end of the night, OBSERVED).**
- The Supervisor loop is running in the owner's PowerShell window (single lock holder, heartbeat FRESH, `--no-main`). It selects nothing.
- Workers: no active worker. `w-1c6bd827e3` (T-DEV-056, first attempts) STOPPED and retired, restart count 2 of 3, still restartable; `w-80f5a2f6a8` (T-DEV-056) and `w-1965a9ef72` (T-DEV-063) EXITED with exit 0.
- Tasks completed tonight: T-DEV-056, 059 to 063 (T-DEV-064 closes with this record). Other ACTIVE tasks belong to other streams (T-RSV-002, T-EDT-001). Open pull requests not from this mission: #113, #34, #31.
- Gateway: worker route `gemini/gemini-3.1-flash-lite`; use a foreground probe before each dispatch; keep the 1024 MB floor.
- Open decisions for the owner: whether to build a task-selection loop that queues work on its own, a dispatched reviewer route (capability 3), and what to do about the main gateway answering without a credential on all interfaces (unchanged).
- **Still not demonstrated:** Main selecting and dispatching tasks without a human or Main session in the loop; a dispatched independent reviewer; unattended Supervisor operation for a whole night; reliability on a task larger than two files; automatic test runs tied to the task's `test_requirements` (implemented on 2026-11-25, see section 13 for what that shows and does not show).

## 13. Test evidence, the third worker change (T-DEV-066) and where the gaps are (2026-11-25, UTC)

Labels as in section 7. Everything below was read from the registry, GitHub, the worker's own log and the call log after the fact, except where marked REPORTED.

**What was built and merged (IMPLEMENTED, unit-tested; each through an independent read-only subagent review whose marker carries the diff hash computed by `integrate` (the reviewers read the head itself; the #225 reviewer said it did not recompute the hash), CI, and every merge check).**
- T-DEV-065, PR #223 (claim #222, close-out #229): `dev_supervisor/testreq.py` and a ninth, supervisor-side `tests` check in `integrate`. Page: `TEST_EVIDENCE.md`. Two review rounds (as the approving comment on #223 says): the first head was CHANGES_REQUESTED (a `wave/` branch name exempted a pull request from the check; node's default reporter was not parsed; an evidence read was not pure; a tail-scrub test proved nothing), all fixed and re-verified; 26 mutations were tried by the reviewer and the material ones are caught.
- T-DEV-068, PR #225 (claim #224, close-out #226): the launch-path audit test lists `testreq.py` as a reviewed `Popen` user. Found by the full `test_dev_*` run (REPORTED from Main's console output: 1890 tests, 512 s, one failure; the output is not in the repository) and by CI. The first merge decided with the ninth row active.
- T-DEV-067, PR #230 (claim #228, close-out #232): `cycle` reports a new stage `needs_tests` when the eight gates pass and the tests check does not.
- T-DEV-066, PR #231 (claim #227, close-out #237): `claim` refuses a spec whose `test_requirements` cannot be run or decided by a gate. **Worker-produced, see below.**
- T-DEV-070, PR #235 (claim #234, close-out #238): the intake test fixture used a prose requirement that T-DEV-066 would refuse. Found by the independent review of #231.
- T-DEV-069, PR #236 (claim #233, close-out #239): `autonomy next` shows, for each `start_task` item, `dispatch_ready` and `dispatch_problems` computed from the registry record.

**The third worker run, OBSERVED.** Fresh worker `w-f35a638636`, started 06:31:13, ended 06:34:14, exit 0, restart count 0, route `gemini/gemini-3.1-flash-lite`. 13 turns by the result record's `num_turns` (16 assistant stream messages; the earlier runs are counted by `num_turns`), 12 tool calls (2 Read, 5 Edit, 5 Bash), one errored tool result (a multi-line `git commit --amend` blocked by the worker allowlist hook). The gateway logged 16 calls in that window (by file time): 12 answered 200, 4 answered 429, no 503. A foreground probe immediately before dispatch completed in 12 s with 3113 MB free (REPORTED from Main's console output, not in the event log). The worker's commit changed `claim.py` (+5 −1) and `test_dev_claim.py` exactly as the prompt said, and the tests it ran passed.
- **What Main did, OBSERVED.** Chose and claimed the task, wrote the prompt, created the worktree, ran the dry run and the probe, queued the assignment, ran the task's own requirements with `testreq run` on the worker's commit (3 commands, all passed), published with `intake` (#231), asked for the review, merged and closed out.
- **What review found, REPORTED (the first verdict survives only as the account in the approving comment on #231; the later facts are OBSERVED in the repository).** CHANGES_REQUESTED on the first head. (1) The change broke three tests in `test_dev_intake.py`, a file the task did not own: Main's task specification missed it, not the worker's work. Fixed by T-DEV-070 first. (2) The message form was not pinned by a test. **Main added one test** (commit `291d9ae`, disclosed in the commit message) and the reviewer confirmed it catches three mutations. The merged diff is therefore the worker's change plus one Main test. Re-review: APPROVE for diff `1798c0b245e02812`.
- **Merge, OBSERVED.** `integrate 231 --yes` showed nine passing rows; row 9 read `3 command requirement(s) passed for head 831fa0cfaac4 diff 1798c0b245e02812`. Merge commit `452b7e2a82c8`.

**What is now demonstrated, and what is not.**
- Demonstrated (OBSERVED once, on this repository): for a code pull request, `integrate` refused to merge without passing evidence of the task's own `test_requirements` for the exact head and diff, and merged with it (PRs #225, #230, #231, #235, #236). The evidence is recorded by `testreq run` in `<state dir>/test_evidence/` and bound to head, diff hash and requirement text. A new commit, a changed requirement or a different diff makes it unusable (unit-tested, and OBSERVED when every merge of main into a branch required a re-run).
- Demonstrated: a worker-produced change went through task requirements run by the runner, independent review, CI and nine checks to merge (#231). It is the third worker change, and the first whose evidence came from the runner instead of Main's reading of command output.
- **Not demonstrated** (updated by section 14: the scan now exists in code but has not run in the live Supervisor). (1) Nothing runs `testreq` by itself: Main (or a person) calls it. (2) Main did not select the tasks; `autonomy next` has no `start_task` item with a complete record today, because no PLANNED task exists and the only eligible milestones need owned paths and acceptance criteria that nobody has decided. (3) The reviewer is a Claude subagent: no dispatched reviewer route exists. (4) The Supervisor still selects and queues nothing. (5) Three small worker tasks on one free route, each with 429s in its window, are not evidence of reliability on larger tasks. (6) Wave pull requests are refused by the tests check, not supported. (7) The POSIX process-group kill is untested. The system is not autonomous.

**An open defect found while checking selection (OBSERVED, not fixed here).** `autonomy next` still offers `worker_failed:w-b88c568c18:FAILED:0` for T-DEV-043, whose registry status is COMPLETED, although the module text says a task that is not PLANNED or ACTIVE is never offered. `_worker_items` does not look at the task's registry status. Until it does, the item keeps appearing and the Stop hook can keep reporting it as actionable. Acceptance for a fix: with a FAILED or LOST worker whose task is COMPLETED or RELEASED, `next` offers nothing for it and a test says so.

**Open pull requests not from this work (OBSERVED, not touched):** #113 (T-EDT-001, `validate` failing since 2026-11-21), #34 (T-DNA-002, 2026-11-17, merge state unknown), #31 (a docs-only 10-line change on a `chatgpt/` branch, merge state unknown). Their owners decide.

**Next milestones, each with a measurable acceptance criterion.**
1. Supervisor-triggered evidence: when a worker exits 0 on a task branch, the Supervisor runs `testreq run` for that head and records it. Done when a worker run is followed within 10 minutes by a passing evidence record for its head with no Main command in between, shown by the `worker_exited` event time, the evidence record's `started` time and its `pid` being the Supervisor's.
2. A dispatched reviewer: a capability 3 route that can post a valid review marker. Done when three consecutive pull requests are reviewed by it, each with at least two of three injected mutations caught, and the merge check still passes with `reviewer` not equal to any author.
3. Task selection that queues work: given a PLANNED task whose `autonomy next` item has `dispatch_ready` true, the memory floor met and a probe passed, a command queues the assignment. Done when one such task goes from the registry to an open pull request with zero Main commands, shown by a new Supervisor event that names the selected task, the `worker_assigned` event for it, and the pull request opened by `intake` (the selection event is part of the milestone; the event log does not record it today).
4. Wave support in the tests check, or a recorded decision to keep refusing it. Done when a unit test shows `integrate` on a wave pull request with two lane tasks failing without evidence for both and passing with it, or when Lukas records the decision to keep refusing in the protocol.
5. Reliability: a worker completes a task touching three or more files. Done when at least 8 of 10 such tasks pass review without Main editing the diff, with the 429 and 503 counts recorded per run.

## 14. The worker-to-integration pipeline mission (2026-11-25, UTC)

Labels as in section 7. Read from GitHub, the registry, the review comments, the Supervisor event log and Main's own console output. Console output that the repository does not hold is REPORTED, however it is worded below. PR #247 carries no `dev-integrate` comment (the post failed during a GitHub outage); its merge through all nine checks is recorded in the Supervisor event log (merge commit `7e8e5be`).

**Merged (IMPLEMENTED, each through an independent read-only review bound to the diff hash, CI and all nine merge checks).**
- T-DEV-072, PR #244 (claim #243, close-out #246): `autonomy next` reads the registry status of a worker's task. One review, APPROVE (17 mutations, 15 caught; the two survivors are the order of the new checks, cosmetic).
- T-DEV-073, PR #247 (claim #245, close-out #253): `worker_tests.py` and `testreq.spawn_run`. **Five review rounds**: four CHANGES_REQUESTED, then APPROVE. The findings, all real and reproduced by the reviewers: a second runner could start while the first was alive after the head moved; attempts counted per worker so one head could be launched more than three times; a dead runner's pid stayed in the entry so a reused pid could hold the slot or all launches forever; a gate-only task became PASSED without a record; a FAILED verdict did not refresh after a manual passing run; two runners after a crash between spawn and the document write; attempt counters pruned by name instead of age; an adopted run ignored the age budget. The test file grew from 37 to 70 tests over the rounds; the reviewers ran several dozen mutations in all (25 in the last round alone).
- T-DEV-075, PR #250 (claim #249, close-out #251): `task_select`. Two review rounds: CHANGES_REQUESTED (the memory figure in the fingerprint recorded an event per run; a damaged worker registry allowed a selection; an `OSError` escaped), then APPROVE.
- T-DEV-074, PR #252 (claim #248, close-out #255): the loop wiring, `autonomy next` items, `cycle` stages. One review, APPROVE (19 mutations, 18 caught).
- Tests run by Main: the full `test_dev_*` suite on the branch of T-DEV-074 before the last fixes of T-DEV-073: 2002 tests, 394 s, OK (REPORTED from a console output file that is not in the repository). The task requirements of every code PR were run by the runner and bound to head and diff (row 9 of `integrate`).

**Observed on the real machine.** `autonomy next` no longer offers the stale T-DEV-043 `worker_failed` item and shows it as suppressed. `task_select` on the real registry: no PLANNED task; the only global reason is the missing independent review path (no authoritative Main session is registered in this session, and the registry has no review route of capability 3).

**Offline end-to-end (OBSERVED, section "Supervisor-triggered runs" of `TEST_EVIDENCE.md`).** Real repository, real runner process: PASSED for the exact head, no duplicate run, no repeated event, a later failing head gives FAILED with the cause.

**Independent reviewer route (phase 5), OBSERVED probes through the isolated worker gateway, each one headless Claude Code run with only the Read tool, a throwaway directory, a sentinel and a four-line diff.** Model `gemini/gemini-3-flash-preview`: two completed runs (17 s and 12 s, 2 turns each, exit 0), both returned the sentinel and CHANGES_REQUESTED naming the injected defect (a swapped `min`/`max` in `clamp`; `items[-n - 1:]` in `last_n`); two further runs (a correct refactor and an SQL string concatenation) ended on `429 all credentials cooling down` and produced no verdict. `gemini/gemini-3.1-pro-preview`: raw call answered 429 with a cooldown until 2026-11-26T08:00Z. `gemini/gemini-3.5-flash`: raw call answered 200 with an empty text; it timed out in earlier Claude Code runs (section 3). **Not qualified, and not added to `verified_routes.json`**: two correct verdicts are not three consecutive reviewed pull requests with injected mutations (milestone below), no clean-diff (false positive) result exists, the capability rank is a claim the owner records, and the same Gemini key backs the author route, so quota exhaustion on one route would probably starve the other (INFERRED from the 429s of section 10 on the author route). What is missing for capability 3: a recorded decision and evidence of at least the milestone below, and a route whose quota is independent of the author's.

**Larger worker task (phase 4).** The facts about the run are OBSERVED (worker registry, worker stream log, Supervisor event log); the pre-dispatch checks are REPORTED from Main's commands, which are not logged. T-DEV-077 (four files: `testreq.py`, `worker_tests.py` and their two test files; explicit owned paths, executable requirements, no dependency; the runner's output is lost today and the review found it) was claimed (#254) and a prompt with exact edit steps was written. Main dispatched it at 17:12 UTC with `assign --allocation-role implementation` (REPORTED: dry run valid, memory 1.9 to 2.3 GB free, a raw call to the route at 17:10 had answered 200; the Claude Code probe before it, at about 17:00, had ended on a 503, and no second probe was run). Worker `w-27737b38ef` on `gemini/gemini-3.1-flash-lite` ran 1143.6 s and ended **FAILED, exit code 1, restart count 0**: 5 turns, 4 `Read` tool calls, no edit, no commit; the stream shows 5 API retries on 429 and 4 on 503 (back-off between 18 s and 48 s) and a final `API Error: 503 ... high demand`. The worktree was left clean on the branch with no commit. The restart budget (3) was not used and no further call was made. T-DEV-077 stays ACTIVE (claimed, prompt prepared) for a later attempt. **No task of three or more files has been completed by a worker (0 of 1 attempts); the target of 8 of 10 cannot be claimed.**

**What is demonstrated.** Task-specific tests, bound to head and diff, gate every code merge made through `integrate` (wave pull requests are refused), and four code PRs and ten registry PRs were merged; each passed all nine checks with a fresh reviewer for each code PR (by Main, with Claude subagents). The scanner, the selection and the new views work in unit tests and, for the scanner, in one offline end-to-end run.

**What is NOT demonstrated, so the system is not autonomous.** (1) The live Supervisor has never run the scan: it holds the code it loaded; its owner must restart it. (2) Nothing selects and queues work: `task_select` selects and records, activation of a PLANNED record, the worktree, the prompt and the assignment stay manual, and there is no PLANNED task and no authoritative Main session now. (3) No dispatched reviewer exists. (4) No worker has completed a task of three or more files. (5) Every PR in this mission was published, reviewed and merged by Main. (6) Wave pull requests are still refused by the tests check.

**Non-blocking review notes kept as follow-ups.** `cycle.main` tests read the real state directory (pass an injected reader); `cycle` does not compare the entry head with the worktree head as `autonomy next` does; entries of a BLOCKED task are still offered by `autonomy next`; one `git rev-parse` per offered entry per `next` call (about 0.7 s); `task_select` does not reject a task that has a LOST or FAILED leftover worker, does not check `exclusive_resources`, and gives exit 1 instead of a refusal when the state directory is missing without `--record`; `review_worker_result` for a finished task is still offered.

**Next milestones, each with a measurable acceptance criterion.**
1. Live scan: the owner restarts the Supervisor; then a worker run (or a test worker record registered for the purpose) is followed within 10 minutes by a PASSED or FAILED entry for its head. Done when the `worker_tests_started` event and the sealed evidence record (its `pid` is the child's, its `started` time is after the `worker_exited` event) exist with no Main command in between.
2. Re-dispatch of T-DEV-077 (restart budget 3 unused, prompt prepared) when a Claude Code probe and a raw call both succeed: done when the worker commits, the runner passes its requirements, the PR is reviewed without Main editing the diff, with the 429 and 503 counts of its window recorded.
3. Reliability: at least 8 of 10 tasks touching three or more files pass review without Main editing the diff.
4. Reviewer route: capability 3 recorded for a route whose quota does not depend on the author's, after three consecutive pull requests reviewed by it with at least two of three injected mutations caught each and one clean diff approved; then `task_select` no longer rejects for the missing review path.
5. Queueing: a command that, for a PLANNED task with a selected verdict, activates it, creates the worktree and prompt from the record and queues the assignment after the memory and route probes. Done when one task goes from the registry to an open pull request with zero Main commands, shown by `task_selected`, `worker_assigned` and the `intake` pull request.
