# Creator DNA — Authenticity and Style Learning

**Status (2026-11-17, T-DNA-002):** DECIDED direction. The evidence store, confidence model, input contract, the **speaker-attribution / eligibility gate**, and (new in T-DNA-002, schema v5) an **injected attribution verifier, purpose-scoped consent with withdrawal and quarantine, three deterministic analyzers, session-aware independence and a library-level review/diff workflow** are **IMPLEMENTED and unit-tested on synthetic data only** (`orchestrator/creator_dna/` and `orchestrator/speaker_attribution/`; 86 tests in `tests/test_creator_dna.py` and 141 in `tests/test_creator_dna_learning.py`, 56 in `tests/test_speaker_attribution.py`, plus `tests/test_integration_phase2_phase3.py`). **NOT TESTED on any real recording; no real-input verification has occurred.** No real footage has been analysed, no profile of the creator exists, and nothing runs this subsystem automatically (see §9). Footage evidence only counts as evidence about the creator if an eligibility decision found it eligible (§8a) **and** a verifier confirmed that decision (§8b) **and** a current consent covers it (§8c). Test levels used below: *implemented* (code exists), *unit-tested* (synthetic, one package), *integration-tested* (synthetic, across packages), *real-input-verified* (a real recording reviewed by the creator): only the first three apply, and the last applies to nothing.

## 1. Goal

Build a continuously improving model of the creator’s authentic on-camera identity from actual footage, audio and editing feedback. The goal is not to create a fictional persona or force the creator into a generic influencer style.

## 2. Primary evidence

Prioritize:
1. Actual spoken dialogue and transcript.
2. Vocal delivery, timing, pauses, emphasis and energy.
3. Spontaneous jokes, reactions and conversational habits.
4. Facial expressions, gestures and camera presence.
5. Repeated patterns across multiple videos.
6. Explicit creator feedback and corrections.

Self-description can help, but observed footage is the main evidence. In the implementation this is enforced: the creator’s own statements are a separate kind of evidence (§5) and never count as footage.

## 3. Traits to learn

- Vocabulary, slang, recurring phrases and language mix.
- Croatian language characteristics and pronunciation.
- Humor: irony, absurdity, sarcasm, understatement, timing and callbacks.
- Typical sentence length and speaking rhythm.
- Energy and emotional range by context.
- Comfort with silence, improvisation and tangents.
- Camera presence, gestures and expressions.
- Preferred visual treatment and tolerance for effects.
- Topics, opinions and recurring ways of explaining things.

Do not assume all traits are stable across every topic or mood.

IMPLEMENTED: a trait must belong to one of a **closed list of categories** (`vocabulary, language, humor, speech_rhythm, pauses, delivery, gesture, expression, camera_presence, editing_preference, topic, other_production`). This keeps the profile on observable production behaviour and prevents unrelated personal inferences.

## 4. Evidence model (IMPLEMENTED, schema v5)

A **trait** is a short statement about observable creative behaviour. It never contains the creator’s words itself; those live in its **evidence**. Each evidence item points at a source recording with timecodes, an optional exact quote, context tags, and either supports or counters (counterexample) the trait.

### 4.1 Layers (a trait is in exactly one)

| Layer | Meaning | Evidence allowed |
|---|---|---|
| `observed` | Checkable in the material | `verbatim` (human-reviewed text) and `measured` only |
| `interpretation` | An AI reading of observed material | verbatim, measured, `ai_estimate`, `asr_unverified` |
| `hypothesis` | Tentative; needs more evidence | same as interpretation |
| `self_report` | What the creator says about themselves | `creator_statement` only |

Interpretations may link (`based_on`) only to `observed` traits. Moving a trait between layers is explicit (`reclassify`, with a reason); a trait cannot be reclassified into or out of `self_report`, and cannot be called `observed` while it holds AI-estimate or unreviewed-ASR evidence.

### 4.2 Evidence methods

| Method | What it is | Notes |
|---|---|---|
| `verbatim` | Exact quote from a **human-reviewed** transcript | requires `reviewed_by="human:<name>"` |
| `asr_unverified` | Wording recognised by a transcription engine, not reviewed | never verbatim; hypothesis/interpretation layers only; weight 0.6 |
| `measured` | Deterministic measurement (e.g. a pause length) | weight 1.0 |
| `ai_estimate` | Model/detector judgement | weight 0.5 |
| `creator_statement` | The creator’s own words about themselves | weight 0.7; self-report (§5) |

Every footage-derived method (everything except `creator_statement`) must also carry an eligible-creator **attribution stamp** (§8a); evidence without one is refused on write and never counted.

Every export separates observed facts, AI interpretations, hypotheses and creator self-report into different sections (`observed_facts`, `ai_interpretations`, `hypotheses`, `creator_self_report`), plus `creator_rejected_or_corrected`.

### 4.3 Confidence (derived on every read, never stored)

1. Evidence weight = method weight × `strength` (the extractor’s own certainty, 0..1).
2. Evidence is grouped into independent **witnesses**. Normally one **source recording** is one witness; when the evidence's `Origin.session_id` is known, **all recordings of that session are one witness** (OPEN default, `ScoringConfig.group_sessions`; a whole shoot is not three independent observations). A witness's mass is `min(1, strongest + 0.3 × sum(others))`, so ten examples from one video, or three videos from one session, are about one source. Evidence without a session id is grouped per recording exactly as before, so historical results do not change; mixed legacy and session-tagged evidence is allowed (each legacy recording is its own witness, each session one witness). Counterexamples are grouped the same way. A `stable` trait therefore needs three recordings, or three sessions where session ids exist.
3. `S` = summed mass of supporting sources, `C` = same for counterexamples. `support_score = 1 − exp(−S/2)` (1 recording 0.39, 2 → 0.63, 3 → 0.78, 5 → 0.92); `consistency = S/(S+C)`; `footage_confidence = min(layer cap, support_score × consistency)`.
4. Layer caps: observed 0.95, interpretation 0.85, hypothesis 0.50, self_report 0.95. Bands: low < 0.45, medium < 0.65, high < 0.85, very_high.
5. **Scope**, derived from rated footage evidence only: `unassessed` (no rated footage evidence), `one_off` (one recording), `emerging` (two), `contextual` (counterexamples with consistency < 0.8, or all support in one identical context), `stable` (≥ 3 recordings, consistency ≥ 0.8, not confined to one context). A single recording never becomes a rule.
6. Creator overrides: *confirmed* or *creator-authored* → final `confidence` = max(footage confidence, 0.95); *rejected* → 0 and excluded. The assessment always reports `footage_confidence` unchanged next to it, plus `confidence_basis` (`footage`, `footage_and_creator_confirmation`, `creator_confirmation_over_weak_footage`, `creator_self_report`, `creator_rejected`) and a plain-language `explanation`.

The constants (k = 2, 0.3, 0.6/0.7/0.5 method weights, 3 witnesses, 0.8 consistency, 0.95 floor, grouping by session) are **defaults chosen by the implementer, not decisions by the creator** (OPEN, see `DECISIONS_AND_OPEN_QUESTIONS.md` Q12 and Q26).

### 4.4 When evidence confidence is unavailable

`strength` and a detector’s confidence may be `None`. `None` is **kept as `None`** (no default is ever substituted). For `verbatim`, `measured` and `creator_statement` evidence, certainty is inherent and counts as 1.0. For `ai_estimate` and `asr_unverified` evidence with no supplied confidence the item is *unrated*: it is stored, exported and counted in `unrated_examples`, but contributes no confidence mass and no source count. A trait with only unrated evidence therefore has confidence 0 and scope `unassessed`.

## 5. Creator self-report versus footage observation (IMPLEMENTED)

- `confirm()`, `reject()`, `reopen()`, `correct()`, and per-example `reject_evidence()` are explicit creator actions recorded as events (actor `creator`).
- A **correction** creates a new creator-authored trait in the `self_report` layer; the old trait stays (status `corrected`, `superseded_by` set, all its evidence kept) and the new one records `supersedes`.
- Creator statements attached to a footage trait are tracked in its `self_report` block and never counted as footage examples, sources, or toward `stable` scope. Each exported evidence item carries `evidence_origin` (`footage` or `creator_self_report`).
- A creator may declare a scope on `confirm(scope=…)`; it is reported as `scope_source: creator_declared`.
- A rejected trait is never resurrected by learning: new evidence is still recorded, the status stays `rejected` until the creator reopens it.

## 6. Authenticity rules

- Never rewrite actual spoken dialogue as though the creator said the replacement.
- Do not remove personality merely to make speech more concise.
- Do not smooth away every hesitation, laugh, imperfect phrase or spontaneous tangent.
- Do not exaggerate emotion or create false reactions.
- Do not fabricate opinions or imply endorsement of a researched claim.
- AI-generated hooks and talking points are suggestions for future filming, not replacement voiceover unless explicitly requested as separate AI narration.

## 7. Input contract from media analysis (IMPLEMENTED)

Transcript, audio and visual inputs reach this subsystem as `TranscriptSegment`, `AudioWindow` and `VisualObservation` (`creator_dna/inputs.py`), built by `media_analysis/adapters.py` (see `INGESTION.md` §Media analysis). Five kinds of information are kept apart: **measurement**, **transcription**, **reviewed transcription**, **detection**, and **creator self-report**; AI/human *interpretations* (“joke”, “reaction”) are not handed over by the adapters.

### 7.1 Unverified ASR

A transcription engine’s output is a recognition, not verified speech.
- `TranscriptSegment.verification` defaults to `unverified`; `reviewed` requires `reviewer="human:<name>"`; an unverified segment cannot name a reviewer.
- `Evidence(method=verbatim)` without `reviewed_by` is rejected. ASR evidence cannot be added to an `observed` trait.
- At ingestion, if the transcript is supplied, a `verbatim` quote is accepted only if it occurs in a segment **reviewed by the same named reviewer**; unverified text can never confirm it. `asr_unverified` quotes only need to occur in the text.
- The reference `RecurringPhraseAnalyzer` proposes a phrase as `observed` only when *every* occurrence is in reviewed text; with any unreviewed occurrence it proposes a `hypothesis`.

### 7.2 Human review and correction

Review lives in `media_analysis` (`review.py`), not in this package: the ASR transcript is never edited. A review names the reviewer, binds to the exact transcript content by hash, and marks each reviewed segment `confirmed` or `corrected(text)`. Only reviewed segments reach DNA as `reviewed`, carrying `Origin.review` (reviewer, review id, status, original segment id, and the original ASR text when corrected). Corrected text is what DNA quotes; the original remains available upstream.

### 7.3 Audio metric contract (v1; authoritative table in `creator_dna/metrics.py`)

`AudioWindow.metrics` may contain only these names, each a finite number in range. Every metric is optional; unknown names, booleans, strings, NaN/inf, out-of-range values and inconsistent combinations are rejected. Read metrics with `AudioWindow.metric(name)` (returns `None` when absent); consumers must decide explicitly what to do without a metric.

| Name | Unit | Scope | Range | Meaning | Larger means |
|---|---|---|---|---|---|
| `pause_duration_s` | s | duration | ≥ 0 | length of one continuous quiet run (level ≤ the analysis threshold) | a longer quiet run |
| `energy_rise_db` | dB | point (at window start) | 0..120 | event-window level minus the baseline | a bigger jump above the recent baseline |
| `level_dbfs` | dBFS | window | −120..0 | RMS level of the window | louder |
| `baseline_dbfs` | dBFS | window | −120..0 | median RMS of the preceding active windows (within the upstream baseline length) | louder background |
| `rms_dbfs` | dBFS | window | −120..0 | RMS level of the window | louder |
| `peak_dbfs` | dBFS | window | −120..0 | highest absolute sample in the window | louder peak |

Consistency checks (when present): `peak_dbfs ≥ rms_dbfs − 0.01`; `|(level − baseline) − energy_rise| ≤ 0.1`. −120 dBFS is the floor (digital silence). “Quiet” depends on the upstream threshold (heuristic when upstream used `auto`), carried in `Origin.settings` (`resolved_threshold_dbfs`, `window_s`, `min_gap_s`, `long_gap_s`, `baseline_s`, `rise_db`). A pause is a **measurement**; nothing here says a pause is awkward, comedic or dead air, and a rise is a level change, not “emphasis”.

### 7.4 Visual input

`VisualObservation.estimate_confidence` is `None` when the detector supplied none (see §4.4). A shot boundary is evidence that the picture changed, not that the topic did, and it is not behaviour of a person: the eligibility layer (§8a) never lets a shot boundary through to Creator DNA.

## 8. Provenance and traceability (IMPLEMENTED)

Each input and evidence item can carry an `Origin`: `media_id`, `source_sha256`, `source_path` (all naming the **original** recording, never a proxy or analysis copy), `record_id`/`record_kind`/`engine` of the upstream record, the analysis `settings`, `analysis_copy_id` (recorded separately), and `review` info. Evidence times are seconds on the original media timeline; `Evidence.source_hash` is the original’s sha256. A test traces a DNA evidence item, reloaded from the log, back to the Phase 2 record, the media item and the file bytes. If the checksum is unknown upstream it stays `None`. `media_analysis.SourceResolver` refuses an analysis copy (by id or path) as a source. Absolute source paths are included by default and can be omitted (`include_source_paths=False`); which default is right is OPEN (Q29).

## 8a. Speaker attribution and Creator DNA eligibility (IMPLEMENTED, Phase 2.3)

**Why.** Creator DNA must be learned from the creator, not from whoever else appears in the footage. A friend speaking the same dialect, a voiceover by someone else, a television in the background or an unknown person on screen are not evidence of the creator's personality. The creator does not record special samples: attribution is established **from the footage being edited**, by a person confirming it, by explicit project metadata, or (future) by supporting providers.

**Four separate dimensions** (`orchestrator/speaker_attribution/`; never one label):
1. **Who** — `Identity`: `creator`, `other_identified` (a stable project-local `person_id`; no real name needed), `unknown`, `multiple_or_overlapping`, `non_speech` (speech only).
2. **How** — `Delivery` (speech only): `on_camera_direct_speech`, `off_camera_speech`, `voiceover`, `narration`, `playback_or_background_audio`, `conversation`, `unknown`. This is context, not identity: the creator or a friend can each do a voiceover.
3. **Whether** — `Decision`: `eligible`, `ineligible`, `requires_review`, always with a machine-readable `reason_code`.
4. **How sure** — a derived confidence (kept apart from the decision), `None` when it cannot be quantified.

Speech and visual behaviour are separate **channels**. A visible person is not automatically the speaker, and a speaker is not automatically visible.

### Attribution evidence (what can establish an identity)
Every attribution is an `AttributionEvidence` with method, actor, time range, source reference, rationale, optional confidence and optional upstream record references. Nothing is identified by software.
- **Authoritative** (can establish an attribution): `human_confirmation`, `human_correction` (overrides named earlier evidence, only inside its own range, and only where it covers the candidate; the overridden evidence stays on record), `project_metadata` (an explicit statement recorded in the project). A human outranks metadata. Contradictory authoritative evidence at the same rank gives `conflicted` and the identity is treated as unknown.
- **Supporting** (can only *propose*, never confirm; capped at confidence 0.70; never eligible alone): `diarization_cluster`, `av_sync`, `visual_context`, `continuity`, `prior_reviewed_decision`. Lip-sync and visual presence may support an attribution but never prove it.
- **A diarisation cluster is a grouping of similar voices, not an identity.** It yields an identity only through a human `map_cluster` decision for that provider's cluster in that recording, and the resulting confidence is `mapping confidence × membership confidence`; if the provider supplied no membership confidence the result is unquantified (`attribution_confidence_unquantified` → `requires_review`). Two voices sharing a dialect are two clusters.
- Resolution states: `confirmed`, `confirmed_via_cluster`, `proposed`, `conflicted`, `unattributed`. Evidence must cover the candidate (tolerance 0.25 s; adjacent same-identity items are combined). A different identity overlapping part of the range makes it `mixed`.
- Default confidences (implementer defaults, OPEN Q37): human 0.95, project metadata 0.85, supporting cap 0.70, minimum for `eligible` 0.80.

### Eligibility policy (`speaker_attribution/eligibility.py`)
| Candidate and attribution | Decision | Reason code |
|---|---|---|
| Creator confirmed, delivery known and not playback, confidence ≥ 0.80 | eligible | `creator_speech_confirmed` / `creator_voiceover_confirmed` |
| Creator confirmed, delivery unknown | requires_review | `delivery_unknown` (it could be playback) |
| Creator only proposed by supporting evidence | requires_review | `creator_proposed_unconfirmed` |
| Confidence missing or below minimum; contradictory evidence | requires_review | `attribution_confidence_unquantified` / `low_attribution_confidence` / `conflicting_attribution_evidence` / `delivery_conflict` |
| Other identified person (e.g. a friend narrating) | ineligible | `other_speaker` |
| Unknown speaker or unknown narrator; cluster nobody mapped; no evidence | ineligible | `speaker_unknown` / `cluster_without_identity` |
| Multiple or overlapping speakers, creator not isolated | ineligible | `overlapping_speakers` |
| Creator plus another identity inside the range, not isolated | ineligible | `mixed_speakers_in_candidate` (a human can assert `isolated` creator contribution) |
| Playback / background audio, whoever is heard (even the creator's own voice) | ineligible | `playback_or_background_audio` |
| Pause between creator-attributed speech on both sides | eligible | `creator_pause_between_creator_speech` |
| Pause at the edge, or next to another speaker / unknown | ineligible | `gap_without_two_sided_speech` / `gap_not_between_creator_speech` / `speaker_unknown` |
| Energy rise during creator-attributed speech | follows the speech decision | |
| Visual behaviour event of a subject confirmed as the creator | eligible | `creator_visual_confirmed` |
| Visual event, other / several / unknown subject | ineligible | `other_person_visible` / `multiple_subjects` / `visual_subject_unknown` |
| Shot boundary | ineligible | `not_person_behavior` |

Visual behaviour is decided on the visual channel, independently of speech. Every decision (ineligible and requires_review included) is **persisted** with its reason, the resolution and the evidence ids; a changed attribution produces a new decision that names the one it supersedes.

### Independent of transcript review
Attribution says whose speech it is, not what was said. A decision records `transcript_review` (`reviewed` / `unverified`) and `max_evidence_grade` (`verbatim` only for a reviewed transcript, otherwise `asr_unverified_only`). Creator-attributed speech with an unreviewed transcript is still only `asr_unverified` evidence (hypothesis layer); nothing here promotes it.

### Enforcement inside Creator DNA (schema v3; revocation model v4)
- `Evidence.attribution` is an `EvidenceAttribution` stamp. **It can only describe an eligible creator attribution** (subject `creator`, decision `eligible`, confidence in (0, 1], cites the attribution evidence); no stamp can be built for an unknown or other person. It must match the evidence's modality (speech/visual), time range, media and upstream record.
- `DNAStore.add_evidence()` and `ingest()` **refuse footage-derived evidence without a stamp** (`unattributed footage evidence refused`) **and, since schema v5, refuse a stamp the injected verifier did not verify** (§8b). A stamp is a claim, never proof. Creator statements (self-report) are exempt.
- Footage evidence found without a stamp (e.g. in an older log) is listed but never counted in confidence (`unattributed_examples`).
- `DNAStore.revoke_attribution(decision_id, reason, actor)` and `speaker_attribution.sync_revocations()` revoke (not delete) evidence whose decision was later withdrawn or corrected. See *Evidence exclusion: creator rejection versus attribution revocation* below.

#### Evidence exclusion: creator rejection versus attribution revocation (schema v4, T-DNA-001)
An example stops counting for one of two independent reasons, each its own state on the evidence item and its own event:

| | Creator rejection | Attribution revocation |
|---|---|---|
| Event | `evidence_review` with action `reject` | `attribution_revoked` (`trait_id`, `evidence_id`, `decision_id`, `reason`) |
| State | `creator_rejected` | `revoked_decision_id` and `revocation_reason` |
| Written by | `reject_evidence()` (creator) | `revoke_attribution()` (upstream attribution sync) |
| Undone by | `restore_evidence()` (`evidence_review/restore`) | nothing: it is permanent for that decision |

- `EvidenceItem.rejected` is a **read-only** compatibility property: true if either state is set. Confidence and existing readers of `rejected` and `review_note` are unchanged. `review_note` holds the note of the latest review or revocation event.
- `evidence_review` accepts only `reject` and `restore`. Any other action refuses to replay (`DNALogError`); it used to be read as a restore.
- `restore_evidence()` clears only the creator's rejection. It raises `DNAError` for revoked evidence and for evidence flagged as a possible legacy revocation (below). A `restore` event found in a log never touches a revocation.
- `revoke_attribution()` records a revocation for every item stamped with the decision, including items the creator had already rejected. It skips only items already revoked, so repeated calls (and repeated `sync_revocations()`) write nothing. A revocation event that names a decision the item is not stamped with, has no reason, or revokes an item twice refuses to replay.
- Exports: `rejected_by_creator` is true only for creator rejection. `attribution_revoked`, `revocation_decision_id` and `revocation_reason` describe revocation separately. Before v4, revoked items appeared as `rejected_by_creator`; that is no longer so for items revoked through the new event.

**Legacy logs (schema v1 to v3).** Before v4 a revocation was written as an ordinary `evidence_review/reject` whose note began `attribution revoked (`, and a restore could undo it. Replay keeps the original semantics exactly: those events are creator rejections (and restores) as written. Nothing is migrated or reinterpreted, because the note text alone cannot prove who wrote the event. Only read-only warning flags are added:
- `legacy_possible_revocation`: a pre-v4 `reject` whose note starts with `attribution revoked (`.
- `legacy_restored_after_possible_revocation`: such an item was restored by a pre-v4 event (the evidence counted again, as it did under the old code).
- `DNAStore.legacy_warnings()` and the export key `legacy_evidence_warnings` list them; new `restore_evidence()` calls on flagged items are refused. Calling `revoke_attribution()` for the decision records an explicit v4 revocation on a flagged item. Flags never change what counts in confidence. A v4 `reject` with a similar note is an ordinary creator rejection and gets no flag.
- Not a legacy-preservation case: a pre-v4 log whose `evidence_review` carries an action other than `reject` or `restore` now fails to load rather than being read as a restore. None is known; no code ever wrote one.

### Human review and correction
Library calls on `AttributionStore`: `human_confirm_span`, `human_correct_span`, `project_metadata`, `map_cluster`. Corrections never delete anything; the decision history and the overridden evidence remain. There is **no CLI or UI yet** (OPEN, Q39).

### What can and cannot be determined automatically
- **Can:** apply recorded human/metadata decisions to time ranges, resolve conflicts conservatively, derive eligibility deterministically, propagate a human cluster mapping, record that a provider was unavailable.
- **Cannot:** identify anyone, recognise a voice or face, tell playback from live speech, detect overlap, decide that a visible person is the one speaking. No provider exists (interface and a synthetic fixture only), no model is installed, no voice biometrics or face recognition is used or required. Without human input everything is `unknown` / `ineligible`.

### Where it sits
`media analysis → speaker attribution → eligibility decision → human review where required → Creator DNA handoff` (each step inspectable). Pipeline (`media_analysis.pipeline`): `attribution_store`, `evaluate_eligibility` (writes `analysis/media/eligibility_report.json`), `prepare_dna_handoff` (keeps only eligible, stamped records); ingestion into the profile remains an explicit call. The test recording `MVI_0879.MOV` is a friend's narration: it may serve transcription-engine tests but **must not be Creator DNA evidence**, and no test uses it as such.

## 8b. Attribution verification (IMPLEMENTED, unit-tested, schema v5, T-DNA-002)

**Problem.** Before v5 the store accepted any well-formed stamp, so a caller could construct a stamp no upstream decision ever backed. Now `DNAStore(directory, clock, verifier=..., project_id=...)` needs an injected `AttributionVerifier` (`creator_dna/verification.py`).

- **Contract.** `verify(VerificationRequest) -> Verification`. The request is built by the store from the evidence itself: evidence id, source id, origin media id and record id, source hash, modality, method, time range and the stamp (decision id, candidate record, channel, range). Outcomes: `VERIFIED`; `STALE` (decision withdrawn, superseded or corrected); `UNKNOWN` (no such decision upstream: forged or foreign); `INVALID` (a decision exists but does not match media, record, channel or range). A verifier that is unreachable raises `VerificationUnavailable`.
- **Fail closed.** Only `VERIFIED` admits the evidence. A missing verifier, an unavailable verifier, any exception, a response that is not a `Verification`, and every other outcome refuse the write (`attribution not verified (<outcome>)`). There is **no flag, default or subclass hook in the package that disables this**. The error text never carries a verifier exception message.
- **Order of checks** in `DNAStore._check_attribution`: stamp present, then verifier, then consent (§8c). Creator statements skip all three.
- **Audit.** `audit_attributions(verifier=None)` re-checks every stored footage item. `STALE`, `INVALID` and `UNKNOWN` items are revoked through the existing append-only `attribution_revoked` event (permanent; restore cannot undo it); an unreachable verifier revokes nothing and is reported. It is idempotent (items already revoked are skipped, a second run writes nothing) and replay-consistent. Legacy footage evidence without a stamp is listed (`unattributed`) and left alone. Revoked evidence never contributes to confidence or scope.
- **Dependency direction.** Creator DNA defines the contract and imports neither `speaker_attribution` nor `media_analysis`. A future integration adapter implements `AttributionVerifier` against the real attribution store (requirements in §8f).
- **Test-only doubles.** `tests/_dna_verifiers.py` provides `RegistryVerifier` (a faithful fake of an upstream store) and `TrustingVerifierTestOnly` (accepts any stamp; documented as NOT a security control) and a `strict_test_store()` that builds a **real strict store** with them. The older `tests/_dna_lenient.py` fixture is unchanged and test-only; no production code references either.

## 8c. Consent, withdrawal and quarantine (IMPLEMENTED, unit-tested, schema v5, T-DNA-002)

Consent is the creator's explicit, purpose-scoped permission for a project. It is recorded as events (`consent_recorded`, `consent_withdrawn`) and checked on every footage ingestion.

- **Record.** Project-local creator identity (`creator_id`, no real name needed), `project_id`, a non-empty set of purposes from a **closed vocabulary**, the authorizing person (`authorized_by="human:<name>"`), the grant time (the event time), optional `expires_at` (ISO-8601 with a UTC offset, later than the grant) and short scalar provenance (for example the channel or a form version).
- **Purposes (OPEN engineering defaults, not approved by Lukas):** `style_learning` (required to ingest footage evidence), `editing_guidance` (let downstream stages use the profile), `profile_review` (show the profile to the creator).
- **Inspection.** `consent_decision(purpose, project_id)` returns allowed/refused with a code: `ok`, `no_project`, `no_consent`, `purpose_not_covered`, `expired`, `withdrawn`. `current_consents()` and the export's `consent` list show every consent with its status. A store with no project identity (neither `project_id` nor `Origin.project_id` on the evidence) refuses footage evidence; evidence naming another project than the store refuses.
- **Ingestion refusal.** Absent, expired, withdrawn or non-covering consent refuses the write. Re-recording an identical current consent writes nothing; re-granting after a withdrawal creates a new consent id.
- **Withdrawal.** `withdraw_consent(reason, actor, consent_id=None, quarantine=True)` withdraws one consent (or all not yet withdrawn). Learning stops at once. If a project is left without a current style-learning consent, `quarantine_all` holds **every footage-derived item of that project** out of the profile. Repeating a withdrawal or a quarantine writes nothing.
- **Quarantine** is its own reversible exclusion, independent of creator rejection and attribution revocation: `EvidenceItem.quarantined`, one `evidence_quarantined` event listing the exact items, `release_quarantine()` (`quarantine_released`) which only releases items whose project has a **current** style-learning consent and which **never** undoes a creator rejection or an attribution revocation. Quarantined items stay in the log and in the export (`quarantined`, `quarantine_reason`) but are excluded from confidence, scope, `active_traits` and every derived profile (`EvidenceItem.excluded`). Creator statements are never quarantined. Nothing is deleted from the log and no source media is touched: **physical deletion is a separate explicit decision (OPEN).**
- **Expiry** is not withdrawal: when a consent expires, learning stops but existing evidence stays counted until it is withdrawn or quarantined (OPEN: whether expiry should quarantine).

## 8d. Deterministic analyzers (IMPLEMENTED, unit-tested, T-DNA-002)

`creator_dna/analyzers.py`. They **measure; they never interpret**. All outputs are OBSERVED traits resting on MEASURED evidence; no humor, emotion, emphasis or "awkward" label exists anywhere in them. Inputs are sorted before processing, so output is deterministic and independent of input order. An input without an `origin` or an eligible-creator `attribution`, or without the needed measurement, is skipped and listed in `analyzer.skipped` with a reason; nothing is invented. With fewer than `min_samples` (default 3) usable inputs no trait is produced. After the typical bucket is derived from all usable inputs, every input in a different bucket is attached as **counter-evidence**. Bucket boundaries are OPEN engineering defaults and describe the measurements, not style quality.

| Analyzer | Input | Unit | Buckets | Category |
|---|---|---|---|---|
| `SentenceRhythmAnalyzer` | human-**reviewed** transcript segments only | words per sentence (median) | short ≤ 8, medium 9-16, long ≥ 17 | `speech_rhythm` |
| `PauseProfileAnalyzer` | windows with `pause_duration_s` | seconds (median) | short < 0.8, medium 0.8-1.5, long ≥ 1.5 | `pauses` |
| `EnergyRangeAnalyzer` | windows with `energy_rise_db` (`level_dbfs`, `baseline_dbfs` quoted in the note when supplied) | dB above the baseline (median) | small < 6, moderate 6-12, large ≥ 12 | `delivery` |

Limitations: sentence splitting is punctuation-based (`. ! ? …`) and counts letters/digits including Croatian diacritics; transcripts without punctuation are one sentence per segment. Pause and energy values mean only what the metric contract (§7.3) says; thresholds upstream (`min_gap_s`, `rise_db`) decide which events exist at all. The energy analyzer does not judge loudness itself (levels depend on microphone gain). Nothing here has run on real speech.

## 8e. Review workflow (IMPLEMENTED, unit-tested, library level, T-DNA-002)

`creator_dna/review.py`; no UI. All three functions are deterministic.

- `review_queue(store)` lists active/confirmed traits that deserve a human look, ordered by (priority, trait id), with machine-readable reasons: `legacy_possible_revocation` (1), `contested` (2), `confirmed_over_weak_footage` (3), `unrated_only` and `unattributed_evidence` (4), `hypothesis` (5), `revoked_evidence` and `quarantined_evidence` (6).
- `apply_decisions(store, decisions_json, actor="creator", dry_run=False)` applies the creator's decisions (`schema: creator_dna.review_decisions/1`; actions `confirm`, `reject`, `reopen`, `correct`, `reject_evidence`, `restore_evidence`) **all or nothing**: strict schema validation (unknown keys, types and vocabularies are refused and every problem is listed), then a rehearsal against a scratch replay of the real log (so decisions that depend on earlier ones are judged correctly and every store rule applies, for example restoring revoked evidence fails the whole batch), then the real write; a failure while writing truncates the log back and reloads. Corrections follow §5: the old trait and its evidence stay.
- `diff_exports(a, b)` compares two `store.export()` snapshots: traits added and removed, confidence/scope/status/layer changes, new support and counter-evidence, evidence newly excluded by creator rejection, attribution revocation or quarantine, evidence returned to the profile, and **contradictions** (counter-evidence added to a supported or creator-confirmed trait; a trait that became contextual). Timestamps and event counters are ignored.

## 8f. Integration requirements for the next milestone (NOT IMPLEMENTED)

Creator DNA does not import `speaker_attribution`, `media_analysis` or `creator_state`; those packages are UNRESOLVED and were not touched. To use this core on real material, an integration milestone (needs a named owner for those packages, see M-INT-02) must provide:

1. **A verifier adapter** (outside `creator_dna`) implementing `AttributionVerifier` over `speaker_attribution.AttributionStore`: look up `stamp.decision_id`; `UNKNOWN` if absent; `STALE` if the decision was superseded or is no longer `eligible`/`creator`; `INVALID` if its media, candidate record id, channel or time range does not cover the evidence; `VerificationUnavailable` if the attribution store cannot be read. It should also schedule `audit_attributions()` after each attribution correction (today `speaker_attribution.sync_revocations()` revokes per decision and still works unchanged).
2. **Project and session identity** on `Origin`: `media_analysis` must supply `project_id` (or the store gets `project_id`) and a `session_id` per shoot (a project-level fact: who decides what is one session is OPEN).
3. **Consent capture**: something (a CLI or review UI) must call `record_consent` with the creator's real authorization; nothing records consent automatically.
4. **Real transcripts and review**: `M-MED-02` (engine choice, Q11) and a human transcript review before any VERBATIM or sentence-rhythm evidence exists.
5. **Existing cross-package tests** that build a strict store now use `tests/_dna_verifiers.py` (`test_speaker_attribution.py`, `test_integration_phase2_phase3.py`: constructor-level change only, under `AGR-DNA-VERIFIER-01`).

## 9. Learning loop and what is not implemented

Implemented (package level, synthetic only): the verified, consented admission gate (§8b, §8c), three deterministic analyzers (§8d), the review queue, batch decisions and diff (§8e), and `DNAStore.ingest()` merges candidates into existing traits by (category, statement) and never overwrites; identical evidence is skipped (idempotent, not even a log entry); a counterexample cannot create a trait; refused evidence is reported; creator feedback is applied as events.

Not implemented: analysis of any real recording; humor, gesture, expression and camera-presence analyzers (the four analyzers that exist are the reference phrase analyzer and the three of §8d); a creator-facing UI or CLI for reviewing transcripts, confirming speakers or confirming/correcting traits (the library functions of §8e exist); a concrete verifier adapter and any consent capture (§8f); any attribution provider (diarisation, A/V sync) or any automatic speaker identification; scheduled periodic summaries (the diff of §8e exists but nothing runs it); use of the profile by the Creative Director/Editor; a pipeline stage that triggers learning. The media-analysis pipeline *prepares* a handoff but never ingests it: ingestion is an explicit call (`media_analysis.pipeline.ingest_handoff_into_dna`) because the choice of analyzers, the eligibility policy and the review state are decisions, not routine steps.

## 10. Storage and compatibility

- Source of truth: an append-only JSONL event log `events.jsonl` (strict: a torn last line, bad sequence number or unreadable event refuses to load; a size check refuses to append if another writer changed the file). The profile is rebuilt by replay; `profile.json` is a derived export written next to it.
- The store takes any directory. The earlier suggestion (`AI/CreatorDNA/` with `profile.md`, `vocabulary.json`, …) is **not implemented**; where the real profile will live is OPEN (Q31). Avoid storing sensitive personal inferences unrelated to video production.
- **Schema version 5 (T-DNA-002).** New since v4: the events `consent_recorded`, `consent_withdrawn`, `evidence_quarantined` and `quarantine_released`; optional `project_id` and `session_id` on `Origin` (written only when set); `EvidenceItem.quarantined`; export keys `consent` and `quarantined_evidence`; the verifier and project arguments of `DNAStore`. **Only the new event types (and an `evidence_added` whose origin carries the new ids) are written as `v: 5`; every other event is still written as `v: 4`**, so a log without the new features stays readable by a v4 reader. A reader older than v5 refuses a log that holds a v5 event (unknown event type or newer schema version) instead of guessing. v1 to v4 logs replay unchanged; appending footage evidence to one needs a verifier and a consent recorded in the store.
- **Schema version 4 (T-DNA-001).** New since v3: the `attribution_revoked` event, independent `creator_rejected` and revocation state on evidence items (with `rejected` kept as a read-only property), strict `evidence_review` actions, legacy warning flags and the `legacy_evidence_warnings` export key. v1 to v3 logs still replay with their original semantics; new events appended to them carry `v: 4`. A reader older than v4 refuses a log that contains a v4 event (unknown event type or newer schema version).
- **Schema version 3 (2026-11-09).** New since v2: `Evidence.attribution` (`EvidenceAttribution`), and the store **requires** it for footage-derived evidence on write. Reading older logs still works, but v2 footage evidence has no stamp: it stays visible, is never counted in confidence and is reported as `unattributed_examples`. Consumers that build evidence by hand must attach a valid stamp (the Creative Director test already does). `creator_statement` evidence is unchanged.
- **Schema version 2.** Compared with the first version: new `asr_unverified` method, `self_report` layer, `unassessed` scope, `reviewed_by` and `origin` on evidence, optional `strength`. A v1 log containing `verbatim` evidence without a reviewer is **refused on load** (`DNALogError`); no such real log is known (the subsystem has only run on synthetic data). `AudioWindow` rejects metric names outside the contract (e.g. the earlier `pause_before_s`). Creator statements no longer back `observed` traits, and creator corrections default to the `self_report` layer.
