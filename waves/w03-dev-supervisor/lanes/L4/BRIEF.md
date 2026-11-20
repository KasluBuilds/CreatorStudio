# Lane L4 of wave w03-dev-supervisor: Provider/model routing and OmniRoute benchmarking (issue #44 part D)

Owner: `claude:dev-routing`  |  Task id: `T-DEV-004`  |  Branch: `lane/w03-dev-supervisor/L4`  |  Depends on lanes: none

## You may edit only
- `orchestrator/dev_supervisor/routing.py`
- `orchestrator/dev_supervisor/omniroute.py`
- `orchestrator/dev_supervisor/OMNIROUTE_BENCHMARK.md`
- `orchestrator/tests/test_dev_routing.py`
- `waves/w03-dev-supervisor/lanes/L4/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w03-dev-supervisor/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- omniroute.py: a client over an injected HTTP transport for the local OmniRoute (base URL from configuration or env; the API key is read from the environment or the machine-local OmniRoute env file ONLY at call time, never logged, never stored, never in exceptions); lists models, sends a small benchmark prompt, records latency, status, token counts if returned and error class; localhost-only by default and refuses non-local base URLs unless explicitly allowed
- routing.py: a task-profile scheduler: input is criticality, complexity class, estimated context size, deadline, concurrency, current context pressure and quota signals (each possibly UNAVAILABLE); output is a ranked decision with reasons (strongest Claude model for architecture, integration and high criticality; OmniRoute Gemini/Groq routes for routine, mechanical, search and repetitive work; escalation to Claude on validation failure or repeated failure; never a paid API route unless a configuration flag explicitly authorises it, default off); deterministic and fully explained; no fixed percentage split
- A benchmark harness runs a fixed set of small synthetic tasks (code transform, JSON extraction, summarise, a tool-use style instruction, a refusal-safe task) against each reachable OmniRoute route, scores them with deterministic checkers (not an LLM judge), records pass rate, latency and failure class, and writes a results JSON containing no secrets
- LIVE verification: run the harness against the local OmniRoute (the Coordinator started it on port 20128; if it is down start it with `omniroute serve --daemon --no-open`); report which Gemini/Groq routes answer, pass rates and latencies, which fail and why; write the factual results into OMNIROUTE_BENCHMARK.md labelled OBSERVED with date and commands; do not print or store any key, do not look for billing, do not configure paid providers
- At least 40 offline tests; routing decisions covered for every branch including all-UNAVAILABLE signals and the paid-route-off default
- Synthetic data only: no real footage, no network in unit tests (a test patches socket creation to fail where a transport is involved), no secrets/tokens/keys printed, stored or committed, no absolute user paths in tracked files
- Any evidence from the real machine (live observation) is summarised as numbers and identifiers only, never user prompts, transcript text or file contents; stated as OBSERVED with the exact command; anything not observed is labelled NOT TESTED
- Nothing is described as VERIFIED without command output; the lane report separates implemented, tested, reported, proposed and open items

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_dev_routing.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w03-dev-supervisor/lanes/L4/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w03-dev-supervisor`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
