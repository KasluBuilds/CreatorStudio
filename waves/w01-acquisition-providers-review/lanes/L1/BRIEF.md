# Lane L1 of wave w01-acquisition-providers-review: Asset acquisition pipeline and rights-evidence handling (offline, transport-injected)

Owner: `claude:asset-recovery`  |  Task id: `T-AST-003`  |  Branch: `lane/w01-acquisition-providers-review/L1`  |  Depends on lanes: none

## You may edit only
- `orchestrator/assets/`
- `orchestrator/tests/test_assets.py`
- `orchestrator/tests/test_asset_acquisition.py`
- `AI_Creator_Studio_Documentation/ASSETS.md`
- `waves/w01-acquisition-providers-review/lanes/L1/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w01-acquisition-providers-review/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- A new module orchestrator/assets/acquisition.py provides an explicit acquire step: it runs the existing triage/rights decision first, fetches only PERMITTED candidates, records QUARANTINE candidates as quarantined records with their reason codes and never fetches them, and never fetches REJECT candidates; unknown rights are never permission
- Fetching goes through an injected Transport protocol (no real network code is exercised anywhere): https-only URLs, a per-provider host allow-list, a maximum byte size, a content-type allow-list per media type, a timeout parameter; the file is written atomically (temporary file, sha256 verified, then moved into the library layout), refuses path traversal, never overwrites an existing asset, and detects duplicates by sha256
- Every failure path (transport error, truncated or oversize download, wrong content type, hash mismatch, library write error) leaves no partial file behind and records a scrubbed error that contains no credential-like text; each path has its own test
- Rights-evidence handling: the licence evidence captured at acquisition (source URL, retrieval date, a hash of the licence text snapshot, the verifier) is stored with the asset; a re-verification function detects licence drift (stored hash differs from the current text) and moves the asset to quarantine without deleting it; human attestations keep working through the existing Attestation model
- A concrete offline LocalFolderProvider searches a directory of user-supplied files described by sidecar rights files (documented, validated schema, strict: unknown keys and missing rights are quarantined, never permitted); acquisition copies, and original files are never modified, moved or deleted (a test hashes the originals before and after)
- At least one open-licence catalogue adapter (Openverse or Wikimedia Commons) is implemented as a pure request builder plus response parser over the injected transport and tested only against fixture payloads authored from the public API documentation; the documentation and the report state NOT TESTED against the live service
- A single function find_and_acquire(request, registry, library, transport, context) runs search, triage and acquisition and returns a deterministic report listing acquired, quarantined, rejected and failed items with reasons; ordering is stable across runs
- At least 60 new offline tests in orchestrator/tests/test_asset_acquisition.py; one test proves no network is touched (socket creation is patched to fail); synthetic tiny files only, no real media; the existing test_assets.py passes (it may be extended, never weakened)
- ASSETS.md documents the acquisition flow, the sidecar schema, the failure behaviour and what is IMPLEMENTED versus NOT TESTED (live providers, real media); open questions are listed, not decided; the lane report lists anything needing a decision from Lukas

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_assets.py, test_asset_acquisition.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w01-acquisition-providers-review/lanes/L1/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w01-acquisition-providers-review`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
