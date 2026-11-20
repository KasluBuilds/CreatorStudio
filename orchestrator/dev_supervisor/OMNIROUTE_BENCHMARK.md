# OmniRoute benchmark (T-DEV-004, wave w03 lane L4)

Status: **OBSERVED**, one run, one machine, one moment. Not VERIFIED as a stable property of any route.

## What was run
- Date: 2026-11-17 (UTC date stamped by the harness; run executed in the 2026-11-18 local session).
- Target: local OmniRoute gateway, `http://localhost:20128` (OpenAI-compatible `/v1`), started by the Coordinator with `omniroute serve --daemon --no-open`.
- Command (from `orchestrator/`, no key on the command line):
  `python -B -m dev_supervisor.omniroute --out <results.json> --timeout 45`
- Discovery: `GET /v1/models` returned 445 model ids (answered HTTP 200 without a key). Harness selected the Gemini/Groq chat routes only (embedding, image and audio models excluded): 7 routes.
- Auth: the harness reads a key only at call time from `OMNIROUTE_API_KEY` or the machine-local OmniRoute env file. In this run no key was set in the environment and the env file held no API-key entry, so requests were sent without an Authorization header. No key was printed, stored or committed.
- Tasks: 5 fixed synthetic prompts (code transform, JSON extraction, summarise, tool-use-style single-line instruction, refusal-safe battery-disposal question), `max_tokens` 512, temperature 0, scored by deterministic checkers (no LLM judge). Policy: 1 retry for timeout/429/5xx/connection errors; a route is abandoned after 2 consecutive identical failure classes (remaining tasks recorded as `skipped_after_failures`).
- Prompts and responses are not recorded; only task ids, pass/fail, status, latency, token counts and error classes.

## Results (single run)
| Route | Passed | Pass rate | Latency mean / max (s) | Failure classes seen |
|---|---|---|---|---|
| gemini/gemini-3.1-flash-lite | 5/5 | 1.0 | 8.23 / 21.01 | none |
| gemini/gemini-3-flash-preview | 4/5 | 0.8 | 4.53 / 5.94 | `check_failed` (tool_use_style) |
| gemini/gemini-2.5-flash | 0/5 | 0.0 | n/a | `not_found` (first call), then `rate_limited`; abandoned after 3 tasks |
| gemini/gemini-2.5-flash-lite | 0/5 | 0.0 | n/a | `not_found`, then `rate_limited`; abandoned after 3 tasks |
| gemini/gemini-3.5-flash | 0/5 | 0.0 | n/a | `timeout` (47 s), `upstream_error`, `rate_limited`; abandoned after 4 tasks |
| groq/openai/gpt-oss-120b | 0/5 | 0.0 | n/a | `auth_error` (2 identical, then abandoned) |
| groq/openai/gpt-oss-20b | 0/5 | 0.0 | n/a | `auth_error` (2 identical, then abandoned) |

Latencies are wall time per request through the gateway for tasks that returned HTTP 200 (small prompts, ~170-500 completion tokens including any model reasoning).

## Interpretation (OBSERVED facts vs reading)
- OBSERVED: two Gemini routes (3.1-flash-lite, 3-flash-preview) answered and passed 9 of 10 tasks combined. The one failing check was the exact one-line `CALL get_weather(city="Paris")` format on gemini-3-flash-preview (output did not match the strict pattern).
- OBSERVED: Groq routes returned `auth_error` (HTTP 401/403 class) on both models. Cause not investigated (the Groq provider connection or its key inside OmniRoute may be missing, expired or unconfigured); no provider was configured and no credentials were inspected.
- OBSERVED: `rate_limited`, `upstream_error`, `not_found` and a timeout on the remaining Gemini routes. The `rate_limited` rows suggest free-tier quota exhaustion in that window, but this was not confirmed (not checked with `omniroute quota`). `not_found` on the first call of 2.5-flash/2.5-flash-lite is unexplained.
- "Unreachable" in the harness JSON means no task returned HTTP 200 in this run; it does not mean the route is permanently down.
- Reading, not proven: the free Gemini routes are usable for small routine tasks when not rate-limited; free quotas are volatile, so the scheduler (`routing.py`) treats quota as a live signal and escalates to Claude after validation or repeated failure.

## NOT TESTED
- Long contexts (all prompts were under ~200 tokens), concurrency, sustained throughput, streaming.
- Tool use over many turns, real function-calling schemas (only a single-line "tool-use-style" text instruction was scored).
- Whether Claude Code itself works through OmniRoute (Anthropic-format endpoint, tool loop, extended sessions).
- Repeat runs, time-of-day variation, rate-limit recovery time, cost/quota numbers.
- Authenticated requests: no key was available in this run, so behaviour with a key is untested.
- Any paid route: none exists or was configured; the scheduler excludes paid routes unless `allow_paid_api=True`.

## Reproduce
`cd orchestrator && python -B -m dev_supervisor.omniroute --out results.json` (needs the gateway up; add `--route gemini/gemini-3.1-flash-lite` to restrict). The unit tests (`python -B -I -m unittest discover -s tests -p test_dev_routing.py`) use fake transports and block sockets.
