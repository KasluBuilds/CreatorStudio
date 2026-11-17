# Lane L2 of wave w01-acquisition-providers-review: Concrete research providers, caching, budgets and the pipeline stage adapter (offline, transport-injected)

Owner: `claude:research-stream`  |  Task id: `T-RSR-001`  |  Branch: `lane/w01-acquisition-providers-review/L2`  |  Depends on lanes: none

## You may edit only
- `orchestrator/research/`
- `orchestrator/tests/test_research.py`
- `orchestrator/tests/test_research_providers.py`
- `AI_Creator_Studio_Documentation/RESEARCH.md`
- `waves/w01-acquisition-providers-review/lanes/L2/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w01-acquisition-providers-review/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- At least four concrete providers are implemented as pure request builders plus response parsers over an injected Transport protocol: Crossref and OpenAlex (scholarly metadata), the MediaWiki API (encyclopedic) and the Wikimedia pageviews API (trend signal); every parser converts into the existing RawHit and goes through the existing coercion and validation (coerce_hit); malformed, oversize or hostile payloads are quarantined with a reason, never raised to the caller
- Provider fixtures are authored from the public API documentation and clearly labelled as such; the documentation and the report state NOT TESTED against the live services; no API key, credential or real network call exists anywhere in code or tests
- The existing source-quality machinery in sources.py is applied to every provider hit (tier and quality assigned, low-quality or unverifiable sources quarantined or flagged exactly as the existing rules specify) with a test matrix covering every provider and every tier outcome
- Budgets and resilience: ResearchBudget limits are enforced per provider and per run; a deterministic retry/backoff policy uses an injectable clock and sleeper (tests never sleep); a per-provider circuit breaker stops calling a failing provider and records why; every error message is scrubbed
- A caching layer stores provider responses on disk keyed by provider, normalized query and date with a TTL, writes atomically, never caches an error, and a ReplayTransport serves recorded responses so a research run is reproducible offline; tests cover hits, misses, expiry, corrupted cache files and concurrent writers
- A pipeline stage function run_research_stage(brief, providers, budget, today) returns the existing research->creative_director contract packet; the packet produced for every existing fixture is byte-for-byte unchanged, the contract version is not changed, and a test validates the packet shape (including the safety fields) against the documented contract without importing creative_director
- At least 70 new offline tests in orchestrator/tests/test_research_providers.py; one test proves no network is touched (socket creation is patched to fail); all existing tests in test_research.py still pass (they may be extended, never weakened)
- RESEARCH.md documents the providers, limits, cache and stage adapter, with IMPLEMENTED versus NOT TESTED labels; open questions are listed, not decided; the lane report lists anything needing a decision from Lukas

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_research.py, test_research_providers.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w01-acquisition-providers-review/lanes/L2/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w01-acquisition-providers-review`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
