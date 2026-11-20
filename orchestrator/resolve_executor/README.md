# resolve_executor — safe, validated DaVinci Resolve operations

Task `T-RSV-002`, milestone `M-RSV-02`. Standard library only. Status: **implemented, unit-tested offline; the
core operations were exercised live by the previous session on Resolve Free 21.0.4.5 with synthetic media**
(evidence `E-RSV-LIVE-0001`, recorded in `PROJECT_STATUS.json`; the evidence file itself is machine-local, so the
takeover review below could not re-observe it). M-RSV-02 is **not VERIFIED**: nothing here has been used on real
footage or reviewed by a human, and nothing is wired into a project pipeline yet (the Editor Agent and its edit-plan
schema, `M-EDT-01`, do not exist).

## Why it exists

The LLM must never drive Resolve with arbitrary low-level API calls. An Editor produces **typed requests**; this
package validates them, executes only supported operations, reads the result back from Resolve, journals it, and
refuses or reports anything it cannot prove.

```
Editor plan ─▶ operations (typed, frozen) ─▶ validation (pure; never calls Resolve)
           ─▶ Executor: capability gate ─▶ idempotency journal ─▶ identity + preconditions (reads)
           ─▶ journal "begin" ─▶ adapter mutation ─▶ read back + verify ─▶ journal "finish"
                         │
                         ├─ live.LiveResolveAdapter ─▶ bridge.BridgeClient ─▶ in-app bridge 127.0.0.1 ─▶ Resolve
                         └─ fake.FakeResolve (offline tests, CI)
Renders: render.run_render ─▶ job readback ─▶ monitor ─▶ probe.ffprobe ─▶ export report
```

| Module | Role |
|---|---|
| `timebase.py` | Exact frame/time arithmetic (`Fraction`, integers). Half-open `FrameRange`, rate parsing, floor rescaling, timecode (non-drop only). |
| `operations.py` | The request vocabulary. There is no "call this method" request. |
| `validation.py` | Deterministic checks: identifiers, rates, ranges, transform keys and bounds, media and output allowlists (symlink-resolved), forbidden media names. Imports no adapter. |
| `capabilities.py` | Inventory with status VERIFIED / IMPLEMENTED / NOT_TESTED / UNSUPPORTED; the executor refuses the last two. Also the list of the 37 MCP tools. |
| `executor.py` | Lifecycle, preconditions, read-after-write checks, bounded retries, checkpoints, `resolve_uncertain`. |
| `render.py` | Render configuration, job readback, monitoring, timeout stop, file verification. |
| `probe.py` | ffprobe wrapper and comparison with the requested spec (fails closed without ffprobe). |
| `journal.py` | Append-only JSONL journal (fsynced `begin` before every mutation) and checkpoint snapshots. |
| `errors.py` | `FailureClass` and the recovery recommendation for each. |
| `adapter.py` | The narrow, data-only adapter interface. |
| `live.py`, `bridge.py` | Live adapter over the local bridge (HMAC-signed; token read from the machine-local config, held only in a closure, never logged or returned). |
| `fake.py` | In-memory simulation of the measured Resolve behaviour, with fault injection. |
| `live_suite.py` | The live integration suite (disposable project, synthetic media). |

## Using it

```python
from resolve_executor import *
from resolve_executor.live import LiveResolveAdapter
from resolve_executor.journal import Journal

adapter = LiveResolveAdapter.connect()                       # needs Workspace > Scripts > resolve_bridge running
policy = Policy(allowed_media_roots=(media_dir,), allowed_output_roots=(render_dir,),
                forbidden_media_names=("MVI_0879.MOV",))
ex = Executor(adapter, policy, Journal(journal_dir), expected_project="My project")
r = ex.execute(AppendSegment("seg-1", timeline="Main", media_path=clip, source=FrameRange(0, 100),
                             record_frame=90000, idempotency_key="plan42-seg1"))
if not r.ok:
    print(r.outcome, r.failure, r.message, r.recovery)
```

Outcomes: `SUCCEEDED` (readback shows exactly the request), `ALREADY_DONE` (idempotency key completed before;
nothing sent), `REFUSED` (nothing sent: validation, capability, identity, precondition, unresolved earlier attempt),
`FAILED` (sent; the resulting state is known from readback and is not the request), `UNCERTAIN` (sent; state could
not be read; the journal entry stays open and the key is blocked until `Executor.resolve_uncertain(key, applied,
note)` records what an inspection found).

Rules the executor enforces:

* An expected project must be set and open; a timeline's rate is read before any frame arithmetic.
* Media must already be in the media pool exactly once; the source range must lie inside the file; the media must
  have the requested video/audio part; the slot must be free (half-open overlap test); tracks must exist.
* A mutation whose effect is unknown is never repeated. A transport error is retried (once by default) only when
  the readback proves nothing changed.
* `DeleteItems` (the only destructive request; timeline items only) checkpoints first.
* Renders never overwrite, send every setting in its own payload, read the queued job back, judge completion by
  percentage and error (the JobStatus text is localized), stop on timeout, restore the page, and verify the FILE.

## Frame semantics (measured live, see `RESOLVE_INTEGRATION.md`)

* Source ranges are half-open `[startFrame, endFrame)` source frames from the first frame of the file:
  `[0, 100)` is 100 frames, `[99, 100)` is the 100th frame alone. Pixel-verified by decoding a render.
* Timeline positions are absolute frames including the start frame (90000 for 01:00:00:00 at 25 fps);
  `GetEnd()`/`GetEndFrame()` are exclusive.
* Mixed rates: a source range of `n` frames occupies `floor(n × timeline_rate / source_rate)` timeline frames.
* The historical "100 frames became 95": see "The 95/100 discrepancy" below. No constant correction exists anywhere.
* Render `MarkOut` is inclusive; the executor sends `mark_out - 1` for its half-open range.

## Tests

```
cd orchestrator
python -B -I -m unittest discover -s tests -p test_resolve_executor.py        # offline, CI
python -B -m resolve_executor.live_suite --out "<folder inside the bridge's allowed output roots>"   # live
```

The live suite creates new projects named `CS_T-RSV-002_<stamp>` (and `_ntsc`), writes only inside `--out`, uses
only ffmpeg-generated media, and leaves those projects in Resolve's project list (deleting them is left to a human).

## Acceptance-criteria audit (step S-RSV-002-01, offline takeover review)

Status words: **IMPLEMENTED** = code and offline tests exist and were run in this review; **NOT TESTED (live)** =
no live observation exists for that part; **MISSING** = nothing exists. "Live" below always means the previous
session's `E-RSV-LIVE-0001` unless it says otherwise; this review made no Resolve call.
Test names are in `orchestrator/tests/test_resolve_executor.py` (`Class.test`).

| # | Criterion | Where implemented | Tests | Status |
|---|---|---|---|---|
| 1 | Package with typed requests, validation that never calls Resolve, replaceable adapter, executor with read-after-write verification | `operations.py`, `validation.py`, `adapter.py` (`live.py`, `fake.py`), `executor.py` (`_Prepared.check`) | `TestValidation.test_validation_layer_never_imports_an_adapter`, `TestLayering.*`, `TestAppendSegment.*`, `TestCreateTimelineAndOthers.*` | IMPLEMENTED; read-after-write also seen live (26/26 checks, `E-RSV-LIVE-0001`) |
| 2a | Exact integer/rational frame arithmetic, regression-tested: first/last frame, one frame, 100 frames, mixed rates, non-zero start, invalid ranges | `timebase.py`; `executor._prep_append_segment` | `TestFrameRange.*`, `TestRateConversion.*`, `TestFrameRegression.*` (every timeline rate × first/last/100 frames; start timecodes 00:00:00:00 to 10:00:00:00; ranges past the file end; the inclusive-end trap) | IMPLEMENTED; half-open and floor rules also measured live |
| 2b | 95/100 discrepancy investigated, not compensated with a constant | `timebase.timeline_duration_for_source` (one rule, no constant) | `TestFrameRegression.test_the_95_candidates_are_exactly_two_*`, `TestMeasuredResolveBehaviour.test_ninety_five_*` | IMPLEMENTED; cause **explained** (arithmetic: offline; reproduction: live, prior session); which of two candidates the original call used is **not settled offline**: see below |
| 3a | Unsupported operations fail explicitly | `executor.execute` (capability gate first, unknown request types refused), `capabilities.py` | `TestCapabilities.test_unsupported_*`, `TestSafetyGates.test_unsupported_is_reported_*`, `TestSafetyGates.test_foreign_requests_*` | IMPLEMENTED; `add_transition` and audio-level absence also confirmed live |
| 3b | Output paths allowlisted | `validation.validate_render_spec`, `validate` (`VerifyExport`, now fail-closed), `_real_inside` (symlink-resolved), `render.run_render` (`lexists`, never overwrites) | `TestValidation.test_render_spec`, `test_no_roots_configured_fails_closed`, `TestSafetyGates.test_render_*`, `test_verify_export_fails_closed_*`, `test_symlink_*`, `test_a_dangling_symlink_*`, `TestRender.test_never_overwrites` | IMPLEMENTED. The two symlink tests skip where the OS refuses symlinks (they skipped on the review machine). The bridge's own allowlist is a separate layer and was not touched |
| 3c | Interrupted mutations never repeated automatically | `journal.py` (fsynced `begin` before the mutation), `executor._mutate`, `resolve_uncertain` | `TestRecovery.test_unknown_outcome_is_never_repeated`, `TestSafetyGates.test_unfinished_attempt_without_an_idempotency_key_*`, `test_unexpected_exception_*`, `TestRender.test_bridge_loss_while_rendering_*` | IMPLEMENTED; **NOT TESTED (live)**: a real bridge restart or crash in the middle of a mutation was never provoked |
| 4 | Render validates settings and destination, monitors, detects failure/completion, verifies with ffprobe | `render.py`, `probe.py` (`runner` injectable) | `TestRender.*`, `TestMeasuredResolveBehaviour.test_queued_job_readback_*`, `TestRenderMonitoringAndProbeRunner.*`, `TestProbe.*` | IMPLEMENTED; live: renders (H.264/H.265, 1920×1080 and 1080×1920, range, timeout stop) verified with ffprobe. **NOT TESTED (live)**: a failure reported by Resolve itself (`Error` text); ffprobe-missing path |
| 5 | Capability inventory with VERIFIED / IMPLEMENTED / NOT TESTED / UNSUPPORTED; docs match code | `capabilities.py`; table below | `TestCapabilities.*`, `TestCapabilityInventoryConsistency.*` (README table vs code, VERIFIED needs an evidence id present in `PROJECT_STATUS.json`) | IMPLEMENTED |
| 6 | Offline tests run in CI without Resolve; live only on a disposable project with synthetic media; no source media modified; bridge allowlist and machine-local configuration untouched | all tests use `FakeResolve`, temp directories and injected probers; `live_suite.py` is not imported by tests | `TestLayering.*`, `TestBridgeClient.*`; `TestLiveSmoke` is skipped unless `CREATOR_STUDIO_RESOLVE_LIVE=1` | IMPLEMENTED (offline). Live suite not re-run in the takeover review |
| 7 | Full suite and governance checks pass; M-RSV-02 not recorded VERIFIED without evidence | `PROJECT_STATUS.json` M-RSV-02 verification stays IN_PROGRESS | `TestCapabilityInventoryConsistency.test_nothing_is_verified_without_the_recorded_evidence_id_*` | The full suite and governance validators were **not run** by this worker (instructed to run only this module); Main runs them |

### Capability inventory (mirrors `capabilities.py`; a test fails if this table and the code disagree)

| Capability | Status | Basis |
|---|---|---|
| `inspect_project` | VERIFIED | `E-RSV-LIVE-0001` |
| `inspect_media_pool` | VERIFIED | `E-RSV-LIVE-0001` |
| `import_media` | VERIFIED | `E-RSV-LIVE-0001` |
| `create_project` | VERIFIED | `E-RSV-LIVE-0001` |
| `save_project` | VERIFIED | `E-RSV-LIVE-0001` (bool return only) |
| `create_timeline` | VERIFIED | `E-RSV-LIVE-0001` (24, 25, 29.97 and NTSC via project rate) |
| `inspect_timeline` | VERIFIED | `E-RSV-LIVE-0001` |
| `add_track` | VERIFIED | `E-RSV-LIVE-0001` |
| `append_segment` | VERIFIED | `E-RSV-LIVE-0001` |
| `set_transform` | VERIFIED | `E-RSV-LIVE-0001` (readback, not pixels) |
| `add_marker` | VERIFIED | `E-RSV-LIVE-0001` |
| `place_title` | VERIFIED | `E-RSV-LIVE-0001` |
| `render_timeline` | VERIFIED | `E-RSV-LIVE-0001` |
| `verify_export` | VERIFIED | `E-RSV-LIVE-0001` |
| `delete_items` | VERIFIED | `E-RSV-LIVE-0001` |
| `project_recovery` | VERIFIED | `E-RSV-LIVE-0001` (journal and readback; there is no scripted undo) |
| `render_failure_reported_by_resolve` | IMPLEMENTED | offline fake only; never observed live |
| `interrupted_mutation_handling` | IMPLEMENTED | offline fault injection only |
| `create_timeline_high_frame_rates` | IMPLEMENTED | 30/50/59.94/60 fps accepted by validation; not exercised live |
| `variable_frame_rate_sources` | NOT_TESTED | no code distinguishes VFR media |
| `link_video_audio_clips` | NOT_TESTED | parts are placed in sync, not linked |
| `text_plus_styling` | NOT_TESTED | only text, size and centre are set |
| `proxy_relink_retime_multicam_compound` | NOT_TESTED | no request type |
| `set_clip_audio_level` | UNSUPPORTED | no API (live: `SetProperty` returns False) |
| `audio_fades` | UNSUPPORTED | no API on 21.0 |
| `add_transition` | UNSUPPORTED | no `AddTransition` on 21.0.4.5 (live) |
| `add_subtitle` | UNSUPPORTED | reported to abort Resolve; deliberately not tried |
| `noise_reduction_eq` | UNSUPPORTED | no API |
| `edit_modes` | UNSUPPORTED | no insert/overwrite/ripple API |
| `drop_frame_timecode` | UNSUPPORTED | refused by `timebase` |

### The 95/100 discrepancy

**Question.** A 100-frame source range once became a 95-frame timeline item. Inclusive vs exclusive end frame? Start
timecode offset?

**Findings, with evidence level.**

1. *Start-timecode offset cannot change a clip's length.* Item length is `GetEnd() - GetStart()`; both include the
   same start frame. Tested for four start timecodes at 24 and 25 fps (`test_timeline_start_offset_never_changes_a_clip_length`).
   **Evidence: offline code and tests (fake adapter encodes the live-measured rules).**
2. *The end frame is exclusive (half-open).* Pixel-verified by the previous session by decoding a render
   (`RESOLVE_INTEGRATION.md` section 11). **Evidence: live, previous session, not re-observed here.**
3. *Mixed rates floor the duration.* `floor(n × timeline_rate / source_rate)`. **Evidence: live measurement
   (previous session) plus exact arithmetic here.**
4. *Exactly two combinations of supported rates give 95* (`test_the_95_candidates_are_exactly_two_and_differ_*`,
   n in 99..101, rates 23.976 to 60): **(a)** 99 source frames, 25 fps source in a 24 fps timeline
   (`floor(99×24/25) = 95`), i.e. an `endFrame` of 99 sent for "frames 0 to 99", the inclusive-end mistake; **(b)** 100
   source frames of a 25 fps source in a 23.976 fps timeline (`floor(100×23.976/25) = 95`). **Evidence: arithmetic,
   offline, exact.** 100 frames at 24 fps give 96, not 95.
5. *The historical case is (a).* The previous session read the historical timeline's rate (24 fps, not 25) by
   read-only inspection and found a source start of 0 and duration 95, then reproduced both numbers with synthetic
   media. **Evidence: REPORTED to this review (documented in `RESOLVE_INTEGRATION.md`), not re-observed.** The
   original request that created the item was not preserved, so the `endFrame` value actually sent (99, hypothesis
   (a)) is **INFERRED**, not observed. `GetSourceEndFrame` readback is known to be unreliable on this build, so it
   cannot settle it.

**Not settled offline, and what would settle it.** Read-only, on the historical timeline `MCP_TRIM_TEST` (no mutation,
no footage touched beyond metadata; needs Lukas to start the bridge): (i) `GetSetting("timelineFrameRate")` of the
timeline and `GetClipProperty("FPS")` of the clip: 24 and 25 confirms (a) and excludes (b) (23.976); (ii) the item's
`GetDuration()` (95) and `GetLeftOffset()` / `GetRightOffset()` or `GetSourceStartFrame()`; (iii) if the original
MCP call is in any transcript or log, its `endFrame` argument. If (i) matches, the case is closed apart from (iii).
A fresh synthetic reproduction (25 fps, 100 frames, 24 fps timeline, `[0, 99)` → 95 and `[0, 100)` → 96) is the
check already in the live suite.

**Practical rule.** Describe ranges to the executor as half-open `FrameRange(start, end)`; use
`FrameRange.from_inclusive(first, last)` when a person gives first/last frames. Never add a correction.

## Review notes (what the takeover review changed)

Found by reading the code against the criteria and fixed, each with a test:

* A mutating request submitted **without** an `idempotency_key` was journaled under `op:<op_id>` but an unfinished
  attempt did not block resubmission (only explicit keys did). It now does.
* An unexpected exception while applying a mutation propagated with the journal `begin` open; it is now reported as
  `UNCERTAIN` / `internal` (the key stays blocked). An unexpected exception in preflight is `REFUSED` / `internal`.
* `VerifyExport` accepted any path when no output roots were configured; it now fails closed like every other path.
* The capability gate ran after validation, so an unsupported request with bad fields was reported as a validation
  error, and a foreign request object as "validation"; both are now `UNSUPPORTED` before anything else.
* Render file names: reserved Windows device names (`CON`, `NUL`, `COM1`, ...) and trailing dots are refused; an
  existing or dangling symlink at the output path counts as existing (`lexists`).
* `probe.ffprobe` and `count_frames` accept an injected `runner`; a missing/unstartable ffprobe is `ffprobe_unavailable`
  and a `count_frames` timeout returns `None`.
* The capability inventory gained IMPLEMENTED, NOT_TESTED and one more UNSUPPORTED entries; before, only VERIFIED and
  UNSUPPORTED were used, which overstated what the live run covered.

## Not supported (explicit refusals)

Clip/track audio levels, fades, EQ, noise reduction (no API on 21.0), transitions (`AddTransition` exists only from
21.1, whose Free edition has no Python scripting), subtitle-track captions (reported to crash Resolve; burn-in
titles via `PlaceTitle` instead), insert/overwrite/ripple edits (no API). See `capabilities.py`.
