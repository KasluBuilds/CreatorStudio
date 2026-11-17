# Lane L1 of wave w02-openverse-live-fixes: Openverse live-run fixes: canonical licence URL, distinct retryable 429, evidence section in ASSETS.md

Owner: `claude:asset-recovery`  |  Task id: `T-AST-004`  |  Branch: `lane/w02-openverse-live-fixes/L1`  |  Depends on lanes: none

## You may edit only
- `orchestrator/assets/openverse.py`
- `orchestrator/assets/providers.py`
- `orchestrator/tests/test_asset_acquisition.py`
- `AI_Creator_Studio_Documentation/ASSETS.md`
- `waves/w02-openverse-live-fixes/lanes/L1/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w02-openverse-live-fixes/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- license_text_url is derived canonically from license + version (for example cc0 1.0 -> https://creativecommons.org/publicdomain/zero/1.0/), never from the response's license_url; if it cannot be derived the item fails closed (quarantined/failed with a reason code, never permitted); tests cover cc0, a by-style licence, an unknown licence and a response URL with a trailing deed.en/ path that is ignored
- HTTP 429 maps to its own distinct retryable error code (separate from HTTP_STATUS), recorded with a scrubbed message; the asset stays unfetched and nothing partial is left; tests cover it, including that other 4xx/5xx codes keep their existing behaviour
- ASSETS.md gains a section recording the 2026-11-17 live Openverse run (runs 1-3, sizes, sha256 prefixes, 404 and 429 findings) with VERIFIED limited to exactly what was exercised (search and parse on a real payload, bounded HTTPS download with exact-host allow-list, content-type and magic-byte check on a real JPEG and PNG, sha256, licence snapshot, fail-closed on 404/429, temp cleanup) and NOT verified listed (redirects, drift over time, other providers, video/audio, legal clearance, SVG, run 1 response)
- No network code is exercised by tests (existing no-socket test still passes); no downloaded media or live payloads committed; existing tests are not weakened; M-AST-02 is not recorded as verified or complete

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_assets.py, test_asset_acquisition.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
   Commit often and push your lane branch regularly (`git push origin HEAD`): a push is your heartbeat, and it is how
   the development dashboard and `wave status` can see that you are working.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w02-openverse-live-fixes/lanes/L1/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w02-openverse-live-fixes`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
