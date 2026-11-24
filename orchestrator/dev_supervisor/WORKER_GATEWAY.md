# Worker gateway, credential rotation and route evidence

Status labels: **OBSERVED** (seen on this machine on the date given), **REPORTED** (stated by a source, not re-checked), **NOT DEMONSTRATED** (never run).
Written 2026-11-24 for task T-DEV-042 (second overnight mission, Priorities 1 and 2). This page names no token, key or credential fragment and must stay that way.
Companion pages: `WORKER_DISPATCH.md` (the dispatch gate and the registry file), `OMNIROUTE_BENCHMARK.md` (the 2026-11-17 text benchmark).

## 1. State in one table

| Question | Answer |
|---|---|
| Is a worker route verified? | Yes, one: provider `gemini`, model `gemini-3.1-flash-lite`, on the **worker gateway** below. Two complete Claude Code round trips passed (OBSERVED, section 4). |
| Has any worker run through the dispatch path? | **NOT DEMONSTRATED** as of this page. A later record must say otherwise, with evidence. |
| Is the exposed gateway key still valid? | No. It was rotated on 2026-11-24 (section 2). |
| Is the main gateway safe from other machines? | **No, and unchanged.** It answers inference requests without any credential on all network interfaces (section 3). That needs the owner's decision. |
| Can the main gateway run the verified route? | No. The installed version (3.8.49) fails Claude Code tool turns on Gemini 3 routes. Version 3.8.54 passes. |

## 2. Credential hygiene (Priority 1)

**Exposure (OBSERVED).** The management key that the gitignored launcher holds was printed into two earlier session transcripts on this machine. A byte-level scan found it in exactly three files: the launcher and two session transcripts. The scan covered all 2025 git objects of the repository (all refs, unreachable ones included), the repository checkout, all linked worktrees, the supervisor state directory, the gateway's data directory with its logs and call logs, the Claude session and settings folders, and the top levels of the temp directory (text-like files only; very large or binary files were skipped). The credential-bearing variables of the user, machine and process environments were compared by value and none held it. The value is in no tracked file and in no task record.

**Rotation (OBSERVED, 2026-11-24).**
1. Recoverable copies were made first, in a user-private `recovery` folder inside the gateway's data directory: a copy of the launcher and a consistent snapshot of the gateway database (integrity check `ok`, one key row).
2. A new key was created with the gateway's default minimal scope (usage only, no management). The gateway listed it and recorded a use after one request.
3. The launcher was rewritten byte for byte with only the token value changed (same length, same line count, parses without errors in PowerShell).
4. The old key's record was regenerated, so the exposed secret is dead while the record survives. The replacement secret of that record was discarded unread (the owner can reveal it in the dashboard).
5. The old token now gets the same refusal (HTTP 403) on the management API as an invalid token. The new token appears only in the launcher.

**What stays.** The old, now dead, value remains in the two local transcripts and in the recovery copy. Editing session transcripts was judged destructive and unnecessary for a dead credential. The new key cannot manage the gateway; management goes through the dashboard login. A Supervisor process started before the rotation still holds the old value in its environment: it is harmless (dispatch is separate, section 5) and is replaced when the Supervisor is restarted.

**One more slip (OBSERVED).** While inspecting provider connections, a debugging dump printed the first 200 characters of a nested Kiro client-secret field. It is a truncated, unsigned fragment. Re-authorising the Kiro connection in the dashboard removes any remaining doubt. Nested provider fields must never be dumped.

## 3. Gateway findings (Priority 2)

- **Anonymous inference on every interface (OBSERVED).** On the main gateway (port 20128, listening on all interfaces) requests to `/v1/messages` and `/v1/chat/completions` with no token and with an invalid token were processed; one reached the provider and came back with the provider's own error. Only `/api/*` needs the key. Anyone who can reach the machine can spend the owner's free-tier provider quota. The gateway's own start-up text (3.8.54) says this is a deliberate local-first default and names advisory GHSA-wmgv-ph3p-rv57; its two remedies are `REQUIRE_API_KEY=true` or binding loopback with `OMNIROUTE_SERVER_HOST=127.0.0.1`. **Not applied to the main gateway**: other tools may rely on it, so it is the owner's decision.
- **Gemini 2.5 routes are retired for this account (OBSERVED).** A request returned the provider's 404 "no longer available to new users". The "429 all credentials cooling down" seen on 2026-11-24 was the gateway's cooldown after such 404s, not exhausted quota. Re-probing them is pointless.
- **Gemini 3 tool turns fail on gateway 3.8.49 (OBSERVED earlier, REPORTED cause).** The failure is the provider's missing `thought_signature` on the second turn. The gateway release notes list "replay Gemini thought_signature on the direct Claude to Gemini path" in 3.8.50. The installed version predates it.
- **Other routes (OBSERVED, one attempt each, not repeated):** Groq returns a Cloudflare 403 from the gateway; the OpenCode free model answered "Model is unavailable"; `gemini-3.5-flash` timed out after 150 s in a Claude Code round trip (and timed out in the 2026-11-17 benchmark).

## 4. Route evidence on the worker gateway

**Worker gateway.** A second, separate OmniRoute 3.8.54 installation, started with its own data directory, bound to loopback only, carrying a copy of the database reduced to the Gemini connection (the Kiro, Groq and OpenCode connections and every API key row were deleted from the copy, so a second instance cannot disturb their OAuth tokens and the exposed key does not exist there). It listens on port 20130. The main gateway was not touched.

**Probe (OBSERVED, 2026-11-24).** One headless Claude Code run per attempt, built with the same environment isolation the spawner uses, asked to read a sentinel file with the Read tool and to answer with its content. Result both times: exit 0, 2 turns, no error, the sentinel returned. Wall time 54 s and 68 s. The gateway's call log recorded provider `gemini`, model `gemini-3.1-flash-lite`, status 200. The second attempt ran after the final configuration (loopback bind, reduced database). A direct raw request in between received a transient provider 503 "high demand"; expect slow and occasionally failing free-tier calls.

**Limits of the claim.**
- It covers one trivial tool round trip. It is not evidence that the model can implement a task. The registry entry therefore claims capability 2 (of 5).
- `cost_zero` is **REPORTED, not proven**: the Gemini connection is configured to import free models only, and an earlier session recorded a response-cost header of zero for this route. The gateway computes cost from a price table, and the billing tier of the Google key was not checked on Google's side.

## 5. Start and use (PowerShell)

1. Install beside, not over, the main gateway: `npm install --prefix <tools dir> omniroute@3.8.54`.
2. Prepare the data directory from a database snapshot; delete every row of `api_keys` and every `provider_connections` row except `gemini` in the copy; copy the gateway's `.env`.
3. Start it from **PowerShell** with `$env:DATA_DIR` set to that directory and `$env:OMNIROUTE_SERVER_HOST = '127.0.0.1'`, then `node <tools dir>\node_modules\omniroute\bin\omniroute.mjs serve --port 20130 --no-open --log`. Check that the listener's address is 127.0.0.1. Pitfall (OBSERVED): `serve` respawns a killed child with its original environment, so a wrapper started earlier from another shell keeps rebinding all interfaces; stop wrappers before children.
4. Start the Supervisor with `ANTHROPIC_BASE_URL=http://127.0.0.1:20130` and an `ANTHROPIC_AUTH_TOKEN` that is **not** the main gateway key (this gateway does not check it). `provider_env.worker_env` accepts local URLs only.
5. Add the route to `<state dir>/verified_routes.json` (format in `WORKER_DISPATCH.md`) with this page as evidence.

## 6. Recommendations for the owner

1. Decide on the main gateway: bind loopback or require an API key (section 3). Both change behaviour for other tools.
2. Upgrade the main gateway to 3.8.50 or later, or keep using the worker gateway for workers. Upgrading changes a working service; keep the database snapshot and the previous version to roll back.
3. Re-authorise the Kiro connection (section 2).
