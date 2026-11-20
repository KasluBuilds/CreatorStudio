# Research Agent - trust boundary, signer contract and known limits

Status of everything here: **implemented and unit-tested on synthetic data; not VERIFIED on real footage, real
sources or a real verification service.** Nothing in this package fetches, signs, or talks to a network.

This document covers the Assignment 6 security remediation (task `T-RSR-001`) of the Assignment 5 audit findings
**F-01, F-02, F-04, F-05, F-06 and F-07**. The recovered audit record does **not** establish an F-03 finding, and none
is invented or renumbered here.

## The two authorities that live OUTSIDE this package

The research package can only **verify**. Two services, owned by the orchestrator, hold the keys:

| Authority | Signs | Verified here as |
|---|---|---|
| Review boundary (human reviewer / registered process) | `Attestation` (scopes: source, citation, evergreen, independence, claim_kind, risk_downgrade, **resolution**, **ledger**) | purpose `"review"` |
| Ingestion boundary (fetcher) | `RetrievalRecord`, `ExcerptProof` | purpose `"ingestion"` |

The package holds a *verifier*: `(purpose, message, signature, key_id) -> bool`.

## F-02 - the signing boundary (Research side)

* `RemoteVerifier(transport)` is the client for a verification **service**. It holds a transport, never a key; it sends
  only `{v, purpose, message(hex), signature, key_id}`; it accepts **only** the exact answer `{"v":1,"valid":true}`;
  an exception, bad JSON or an unintelligible answer raises `VerifierUnavailable`, which every `TrustPolicy.verify_*`
  turns into a refusal ("the verification boundary is unavailable; failing closed").
* `TrustPolicy(require_separated_verifier=True)` refuses any verifier that is not a `RemoteVerifier`. A bare callable
  can close over a key; the package cannot detect that, so a production policy must set this flag.
* A `TrustPolicy` / `RemoteVerifier` is never copied or pickled (`__deepcopy__` returns itself, `__reduce__` raises).
* `check_signing_request(request)` is a **reference policy for the signer** (it signs nothing): the signer must receive
  the *structured* subject, re-derive the digest, and refuse scope/kind/context combinations it does not allow, so it is
  not a blind signing oracle. The signer must also authenticate the reviewer and manage its own key state.
* No key, `hmac`, environment lookup or signing code exists in the package (checked by a source-scan test).

**DEPENDENCY, stated plainly - the deployment-level F-02 remediation is NOT complete.** It needs an orchestrator-side
verification service (and a signer that applies `check_signing_request`'s rules). Neither exists. Until they do, the only
verifier anyone can inject is an in-process callable, which is an interface, not isolation. This package cannot close
that gap and does not pretend to.

## F-01 - what a human review is bound to

Every attestation is signed over `bind_context(brief_id, topic_kind)` = `"<brief id>#<topic kind>"`, the subject digest,
attestor, scope, attestation id, key id, issue/expiry dates and any process record. Changing the brief id **or the topic
kind** after review makes every attestation in the brief stop verifying (previously, flipping a brief to `evergreen`
switched freshness off and revived stale evidence). The source subject now also covers `text_origin`.

**Signer-contract change (version bump):** `context_id` must now be `bind_context(...)`; the source subject gained
`text_origin`; two new scopes exist (`resolution`, `ledger`; method scope `ledger_seal`). Attestations issued under the
old contract do not verify. The brief schema is `research-brief/0.5`; `0.4` and older briefs migrate **without** trust.
The Creative Director contract `research->creative_director/0.3` is unchanged.

## F-04 - retrieved evidence, not plausible URLs

A URL from a provider or an LLM is a *lead*. `provenance_stage()` recomputes how far a piece of evidence has got:
`discovered` (address only) -> `retrieved` (ingestion-signed receipt) -> `inspected` (ingestion-signed excerpt proof
ties the locator to the content) -> `reviewed` (valid human/process attestation). Only inspected/reviewed evidence
counts. Lead-stage scoring caps unretrieved sources at `LEAD_EVIDENCE_CAP` (0.25, below the 0.30 evidence floor), and a
policy with no trusted ingestion component accepts no receipt.

## F-05 - the evidence ledger

The builder appends every citation to an append-only, hash-chained ledger (`LedgerEntry`) with the stance that was
assessed. A recorded **contradiction** that is later deleted, relabelled, flipped, re-pointed, attached to a
re-worded claim, or detached by reclassifying the claim stays in force as an **unresolved contradiction**: it blocks
`VERIFIED`, raises the safety warning on safety-critical claims, appears in the caveats and fails validation
(`CONTRADICTION_ERASED`). It is cleared only by a `ContradictionResolution`: a valid **human** attestation (never a
process, never the LLM) over the entry digest, the claim text *as it is now* and a written rationale. Entries are never
removed, so the history stays auditable. Chain edits are caught (`LEDGER_TAMPERED`).

**Limit (pinned by a test):** without a seal the ledger is tamper-*evident*, not tamper-*proof*. Whoever can rewrite
the whole chain consistently can forge it, and an LLM-facing process holding the brief can. The orchestrator can seal
the head (`attach_ledger_seal`, scope `ledger`) and `TrustPolicy(require_ledger_seal=True)` makes validation and export
refuse an unsealed or re-written ledger. A contradiction that was never given to the builder is not in the ledger.

## F-06 - quantity versus quality

A claim side is worth at most 1.25x its strongest independent witness; derivative, duplicate, same-publisher and
unestablished-origin sources are one witness. Lead-stage evidence gives further retrieved tier A/B origins at most
`ORIGIN_BONUS_CAP` (0.1) in total, and a contradicted/disputed fact caps `evidence_from_claims` (0.35 / 0.65) so easy
verified facts cannot dilute it. (An LLM-only credible contradiction still *blocks* VERIFIED, by design; it never
*creates* a disputed/contradicted status.)

## F-07 - derived fields

Claim status, route, explanation, derived caveats, independence groups and the policy fingerprint were already
recomputed; now also **relevance** (`relevance_problems`: range, weights/caps recorded with the score, the weighted sum,
`confidence = min(completeness, reliability)`, and evidence quality/reliability against the brief's own evidence) and
**text origin** (`text_origin="human"` needs a valid human attestation, and the export derives `untrusted_text` from the
attestation, not from the label). `to_markdown` recomputes statuses and prints a validation-failed banner instead of
echoing stored values.

## Residual risks (not fixed here)

* Deployment-level F-02 (external verification service + policy-enforcing signer) - orchestrator work.
* Ledger forgery by a full-chain rewrite without a seal (above).
* `Source.classification_warnings`, `title`, `notes` and `language` are cosmetic: they are exported but not signed, and a
  cleared warning is not detected. They never change a tier or status.
* `topic_kind` itself is now bound by review, but a brief *built* with the wrong kind (e.g. a current event labelled
  `evergreen` by the caller) is still accepted: classifying the topic is the caller's job.
* The mirrored producer rule for the Creative Director (`research->creative_director/0.3`) was not touched; the
  packet shape is unchanged.
