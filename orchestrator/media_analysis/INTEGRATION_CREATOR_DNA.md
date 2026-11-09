# media_analysis ↔ creator_dna — evidence contract (Phase 2.1)

Status: IMPLEMENTED and unit-tested on synthetic data only. No real transcription engine exists; nothing here was run on real footage.
Code: `media_analysis/{adapters,review,pipeline}.py`, `creator_dna/{schema,inputs,metrics,confidence}.py`.
Tests: `tests/test_integration_phase2_phase3.py`, `tests/test_creator_dna.py`, `tests/test_media_analysis.py`.

## Five kinds of information (never merged)

| Kind | Phase 2 record | DNA evidence | Notes |
|---|---|---|---|
| Measurement | `QuietGap`, `EnergyEvent`, level windows | `Method.MEASURED`, `Modality.AUDIO` | numbers per `creator_dna/metrics.py` |
| Transcription | `TranscriptSegment` (ASR, always `unverified`) | `Method.ASR_UNVERIFIED` | hypothesis layer only; never OBSERVED |
| Reviewed transcription | `ReviewedSegment` from a `TranscriptReview` by `human:<name>` | `Method.VERBATIM` + `reviewed_by` | the ASR file is never edited |
| Detection | `ShotBoundary`, `VisualEvent` | `Method.AI_ESTIMATE`, `Modality.VISUAL` | confidence may be `None` (stays `None`, no mass) |
| Creator self-report | — (DNA only) | `Method.CREATOR_STATEMENT`, layer `self_report` | tracked apart from footage in scoring and export |

Interpretations (joke, reaction, …) stay in `moment_index.Interpretation` with an author; they are not handed to DNA by the adapters.

## Transcript review
`Transcript` = engine output, always `unverified`. `TranscriptReview` (separate write-once file, chained by `previous_review_id`) holds per-segment `confirmed` / `corrected(text)` + reviewer + hash of the exact transcript reviewed. `apply_review()` gives a view with both texts. Only segments a named human reviewed reach DNA as `verification="reviewed"`; the rest are skipped (default) or sent as `unverified` (`DnaEligibility.include_unverified_transcripts=True`). DNA refuses `VERBATIM` without `reviewed_by`, refuses VERBATIM quotes confirmed only by unverified text, and refuses ASR evidence on OBSERVED traits.

## Audio metric contract (v1)
Authoritative table: `creator_dna/metrics.py`. Names: `pause_duration_s`, `energy_rise_db`, `level_dbfs`, `baseline_dbfs`, `rms_dbfs`, `peak_dbfs`. All optional; unknown names, NaN/inf/bool/str, out-of-range and inconsistent combinations are rejected. Analysis parameters (threshold, window, min gap…) travel in `Origin.settings`.

## Provenance (`creator_dna.Origin`)
`media_id`, `source_sha256`, `source_path` always name the ORIGINAL; `record_id`/`record_kind`/`engine` name the Phase 2 record; `analysis_copy_id` records that a copy was analysed (copies are rejected as sources by `SourceResolver`); `review` carries reviewer, review id, status and original ASR text. Evidence times are seconds on the original media timeline. Evidence `source_hash` = original sha256.

## Pipeline (creator_state)
`media_analysis/pipeline.py` uses only `creator_state`'s public API: `ingest.verify_sources` (originals verified first), `Project.begin/complete/fail_operation` under stage `understand`, outputs under `Projects/<slug>/analysis/media/`. It does NOT complete `understand` and does NOT learn automatically; `prepare_dna_handoff` writes `dna_handoff.json`, and `ingest_handoff_into_dna` is a separate explicit call.

## Compatibility
Creator DNA event log schema v2: logs holding VERBATIM evidence without `reviewed_by` are refused on load (`DNALogError`). `AudioWindow` rejects unknown metric names (old `pause_before_s`). `TranscriptSegment` defaults to `unverified`.

## Speaker attribution and eligibility (Phase 2.3)
Pipeline order: media analysis -> `speaker_attribution` (who speaks / who is visible) -> eligibility decision -> human review where required -> DNA handoff. `build_handoff()` above yields unattributed inputs; the Creator DNA store (schema v3) **refuses footage evidence without an `EvidenceAttribution` stamp**, and the stamp can only describe an eligible creator attribution. Use `speaker_attribution.build_attributed_handoff()` / `pipeline.prepare_dna_handoff()`. Attribution is independent of transcript review: an attributed but unreviewed segment is still only `asr_unverified` evidence. See `CREATOR_DNA.md` §8a.
