# Asset Agent — Discovery, Generation, Rights and Organization

**Status:** Separate Asset Acquisition service is DECIDED; detailed policy and implementation are PROPOSED. An offline foundation (`orchestrator/assets/`: metadata schema, fail-closed rights engine, library) is implemented and unit-tested on synthetic data; there is no provider, downloader or automated licence verification (M-AST-01 in `PROJECT_STATUS.json`).

## 1. Mission

Acquire or create supporting material that improves clarity, demonstration, context, humor or visual interest without introducing rights or provenance problems.

## 2. Asset types

- B-roll and stock footage.
- Images and illustrations.
- Diagrams, charts and maps.
- Motion graphics and titles.
- Sound effects and music.
- Memes and reaction visuals.
- Generated images/video/graphics.
- Creator-owned supplementary footage.

## 3. Rights policy

Auto-eligible only when the rights basis is known and compatible with the intended use, such as:
- Creator-owned material.
- Public-domain material with verified status.
- Properly licensed material whose terms allow the intended use.
- Creative Commons material whose exact license and attribution requirements are compatible.
- Generated assets where the generation provider’s terms permit the intended use.

Reject or quarantine:
- Unknown-license assets.
- Watermarked previews.
- Assets whose license forbids commercial use when the project may be monetized.
- Assets with incompatible attribution/share-alike/derivative restrictions.
- Material whose source cannot be established.

No watermark does not mean free to use. Publicly viewable does not mean reusable. The creator’s willingness to remove a video after a takedown does not make an infringement safe or authorized.

## 4. Asset manifest

Recommended fields:
- `asset_id`
- `filename` and local path
- `asset_type`
- `source_url` or provider
- `creator/author`
- `retrieved_at`
- `license_name`
- `license_url`
- `commercial_use_allowed`
- `modification_allowed`
- `attribution_required`
- `attribution_text`
- `restrictions`
- `intended_project`
- `intended_use`
- `rights_status`
- `notes`

## 5. Asset selection

Choose assets that:
- Directly support the point being made.
- Match the tone and creator’s style.
- Are technically suitable for the timeline.
- Do not distract from important speech or demonstrations.
- Have appropriate resolution, aspect ratio and audio quality.
- Can be cropped or reframed without losing essential information.

Do not insert B-roll simply to cover every cut.

## 6. Storage

Suggested project structure:
```text
Assets/
  Images/
  Videos/
  SoundEffects/
  Music/
  Graphics/
  Memes/
  Manifests/
```

Project-specific downloaded/generated assets should be linked to a manifest and copied or referenced in a predictable location. Avoid duplicate uncontrolled downloads.

## 7. Autonomy

Routine insertion of rights-cleared assets does not require user approval. Unknown or incompatible rights must block use automatically. The agent should report rejected assets and find alternatives rather than silently using them.

## 8. Acquisition (offline, transport-injected) — IMPLEMENTED (unit-tested); live use NOT TESTED

Added by task T-AST-003 (wave w01, lane L1). Code: `orchestrator/assets/acquisition.py`, `local_provider.py`, `openverse.py`; tests: `orchestrator/tests/test_asset_acquisition.py`. Status labels: **IMPLEMENTED** = code and offline unit tests exist; **NOT TESTED** = never run against the real thing; nothing in this section is **VERIFIED** (no real input has been used).

### 8.1 What it does, and what it does not

`find_and_acquire(request, registry, library, transport, context)` runs search, triage, acquisition and returns a deterministic report. It contains **no network code**: every byte arrives through an injected `Transport` (`fetch(TransportRequest) -> TransportResponse`), and a test patches the socket layer to fail while a whole run executes. Nothing it acquires is ever approved: the asset lands in the library **QUARANTINED** and only `AssetLibrary.attest()` by a named human (the existing `Attestation` model) can approve it. There is no automated licence verification and no legal opinion.

### 8.2 The fetch gate ("acquirable")

`triage()` can never return `permitted` (a candidate has no file, no checksum, no attestation), so the lane brief's "fetch only PERMITTED candidates" is implemented as **ACQUIRABLE**: the triage verdict is not `reject` **and** every quarantine reason is one the acquisition step resolves or a human clears afterwards (`NO_FILE`, `UNVERIFIED`, `NO_OWNER_DECLARATION`, `WATERMARK_UNCHECKED`, `CLAIM_ONLY_EVIDENCE`, `NO_DOCUMENT_EVIDENCE`). Everything else holds the candidate back:

| Triage result | Outcome | Fetched? |
|---|---|---|
| `reject` (non-commercial, no-derivatives, claimed watermark/preview, unknown source ...) | `rejected` (report only) | never |
| quarantine for any other reason (unknown or unrecognised licence, missing attribution text, share-alike not accepted, preview/sample hint in title or tags, unknown commercial or modification status, no licence-text URL, no download source) | `quarantined`: recorded in `candidate_quarantine.json` with its reason codes | never |
| only acquisition-resolvable reasons | fetch | yes |

Unknown rights are never permission. After the download the rights decision is evaluated again on the facts then held; a reject found at that point is reported and nothing is indexed.

### 8.3 Bounded fetch

Per `AcquisitionPolicy`: https only (no credentials in the URL, port 443 only), a per-provider **host allow-list** (a provider with no entry can download nothing; the host is checked again after redirects), `max_bytes` (checked against the declared length and again while streaming), a **content-type allow-list per media type** (no SVG), a **magic-byte check** that the file signature matches the declared type, and a `timeout_s` passed to the transport. A licence-text fetch uses its own host allow-list (default creativecommons.org), size limit and `text/html` / `text/plain`. Download to a private staging directory under the library, verify, then hand to `AssetLibrary.add(..., mode="copy")` (exclusive create, checksum re-verified, never overwrites, duplicates by sha256 are reported as `duplicate`). A candidate that was already acquired is skipped without fetching.

### 8.4 Failure behaviour

Every failure returns a `failed` report item with a stable code and a scrubbed message, and removes the staging directory, any managed file and any licence snapshot created for it: `TRANSPORT_ERROR` (including a stream that dies midway), `HTTP_STATUS`, `TOO_LARGE`, `TRUNCATED`, `LENGTH_MISMATCH`, `EMPTY`, `CONTENT_TYPE_NOT_ALLOWED`, `CONTENT_MISMATCH`, `HASH_MISMATCH` (a provider-declared sha256), `HOST_NOT_ALLOWED`, `REDIRECT_NOT_ALLOWED`, `URL_NOT_ALLOWED`, `LIBRARY_WRITE_ERROR`. `scrub()` removes tokens, key/value secrets, URL userinfo and query strings, long opaque strings and caps the length; it is a best-effort filter, not a guarantee.

### 8.5 Licence evidence and drift

For a catalogue asset the licence text is fetched **first** and stored under `evidence/<asset_id>/license-<hash>.txt`; the asset gets a `license_page` evidence of strength `document` with the source URL (query string removed), retrieval date, sha256 of the text and the verifier (`asset-acquisition/1`, a tool, not a person). If the text cannot be captured nothing is acquired. `reverify_license()` re-fetches the text: unchanged -> `unchanged`; changed -> the asset gets a **hold** (quarantine that survives re-evaluation and re-attestation; only `release_hold()` by a named reviewer lifts it), the new text is saved as the new baseline, `add_evidence()` resets verification (the old attestation stays in history and no longer counts), and nothing is deleted; unreachable -> `unverifiable` and no change (optionally a hold). A snapshot hash proves the page text changed, not that the licence terms changed: pages with dynamic boilerplate will cause false drift (fails closed).

### 8.6 Providers

- `LocalFolderProvider(folder)`: offline. A file `clip.mp4` is described by `clip.mp4.rights.json`. Strict sidecar schema (`schema_version` 1; required `title`, `category`, `rights_basis`; optional `media_type`, `license_name`, `license_url`, `creator`, `source`, `source_url`, `attribution_text`, `commercial_use_allowed`, `modification_allowed`, `owner_declaration`, `tags`). A missing sidecar, bad JSON, an unknown key, a wrong type, a missing required key or a `user_owned` basis without `owner_declaration` produces a candidate with **no rights claim**, which is quarantined, never copied. The sidecar is the user's own claim: `user_owned` additionally needs a `user_declaration` attestation, and a CC or other licence claim stays claim-only evidence until a reviewer adds a document. Originals are only read (a test hashes them before and after); links are never followed.
- `OpenverseProvider(transport)`: a pure request builder (`build_search_url`) plus response parser (`parse_search_response`) over the injected transport. Images and audio only (no video endpoint). The licence name is derived only from the documented `license`/`license_version` fields; anything else is left unknown. The download hosts (the original uploaders' hosts) must be allow-listed by the caller. **NOT TESTED against the live service**: the fixtures were written from the public API documentation, not recorded. No API key is used or supported.

### 8.7 Open questions (not decided here)

- Whether a licence-page hash is the right drift signal (a legal-code URL or a normalised text hash may be better).
- Which download hosts to allow-list for Openverse results, and whether to add the Wikimedia Commons adapter.
- Whether watermark checking needs an automated aid; today it is unchecked until a reviewer sets it.
- The ledger and evidence snapshots are written by this module outside `index.json`; a cross-process lock does not exist (single process only, as before).
- Real-media verification and a live-service run are needed before any part of this can be called VERIFIED.
