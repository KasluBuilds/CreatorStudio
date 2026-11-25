# Editing Engine — Creative and Technical Editing Specification

**Status:** Creative principles are DECIDED; detailed implementation is PROPOSED and requires testing.

## 1. Purpose

The Editing Engine turns real footage into clear, engaging, authentic videos. It should make deliberate choices about what viewers need to see and hear, not apply a fixed template to every video.

## 2. Source fidelity

- Preserve the creator’s actual spoken words and meaning.
- Do not replace spontaneous jokes, reactions, opinions or delivery with generated dialogue.
- Removing repetition or false starts is allowed only when it does not change meaning, personality or timing.
- If a factual correction or missing explanation is needed, flag it for the creator or use clearly separate sourced graphics/context; do not silently put words in the creator’s mouth.
- Preserve original footage and create non-destructive timelines.

## 3. Story and attention

Evaluate:
- What question, promise or curiosity brings the viewer in?
- What information does the viewer need at each moment?
- What changes or progresses?
- Where are the strongest demonstrations, reveals, jokes or reactions?
- What is the payoff?
- Which pauses help the viewer process or make a joke land?
- Which repetitions add emphasis versus waste time?

Avoid formulaic “hook every few seconds” rules. Use evidence and observed audience outcomes as context, not as universal laws.

## 4. Pacing

Pacing is the relationship between information, emotion, movement and time.

Possible actions:
- Remove genuinely redundant material.
- Tighten long technical gaps when no meaningful activity occurs.
- Retain pauses before/after jokes, reveals or emotional statements.
- Let demonstrations run long enough to be understood.
- Use faster sequences for appropriate energy, not by default.
- Vary rhythm intentionally.

A quiet moment can be more effective than another cut, zoom or sound effect.

## 5. Humor

When editing humor:
- Preserve setup, timing, reaction and release.
- Do not cut the pause that makes the punchline work.
- Avoid adding laugh tracks or reaction effects unless they fit the creator’s established style.
- Use zooms, freezes, sound effects, captions or visual callbacks sparingly and purposefully.
- Do not invent a joke by rearranging clips in a way that changes what happened or what the creator meant.

## 6. Visual language

Potential tools:
- B-roll and cutaways.
- Close-ups and detail shots.
- Diagrams, labels and on-screen text.
- Screen recordings and demonstrations.
- Motion graphics and simple animation.
- Reframing and subject-aware crops.
- Split screens, comparison views and visual callbacks.

Every visual addition should clarify, demonstrate, contextualize, emphasize or intentionally create a comedic effect.

## 7. Audio

Target capabilities:
- Dialogue intelligibility and consistent loudness.
- Noise reduction only when it improves the recording without artifacts.
- Music that supports rather than competes with speech.
- Sound effects placed with timing and purpose.
- Smooth transitions and controlled peaks.
- Preserve natural room tone where it helps continuity.

Specific loudness targets, codecs and processing chains remain OPEN and should be selected per platform/use case and tested.

## 8. Captions and on-screen text

- Accurate transcription is more important than decorative styling.
- Preserve Croatian diacritics and names.
- Use readable line lengths and timing.
- Keep text within platform-safe areas.
- Emphasize only selected words when appropriate.
- Avoid captions that cover the subject or important visual information.
- Review automatic captions against the audio.

## 9. Long-form vs short-form

Long-form:
- Prioritize coherent progression, context and sustained comprehension.
- Avoid over-compressing demonstrations or thoughtful discussion.
- Use chapter/section structure when helpful.

Short-form:
- Must work independently.
- Start with a strong, truthful hook.
- Include enough context to understand the clip.
- Deliver a payoff or complete useful thought.
- Reframe for vertical viewing.
- Some shorts can promote the full video, but not all should.

## 10. Edit-plan representation

**Status: IMPLEMENTED offline (T-EDT-001, schema `edit-plan/0.1`), unit-tested only — NOT VERIFIED.** The code is `orchestrator/edit_plan/` (see its README for codes and limits). Nothing here has run against Resolve or real footage.

Plan fields (strict: unknown fields and unknown versions are rejected; a JSON Schema is exported by `json_schema()`):
- `version` (`"edit-plan/0.1"`), `meta`: `project_slug`, `plan_id`, `created_by`, `source_brief_id`, `source_brief_hash`
- `timeline`: `name`, `frame_rate` as an exact rational `{num, den}` (29.97 = 30000/1001), `width`, `height`, `start_timecode` (non-drop)
- `sources`: `source_id`, `path` (reference only, never opened), `frame_rate`, `duration_frames`, `content_hash` (sha256), `origin` (`creator_recording`, `other_recording`, `stock_footage`, `music`, `sfx`, `graphic`, `generated_speech`), `has_video`, `has_audio`
- `tracks`: `track_id`, `kind` (`video`/`audio`/`subtitle`), 1-based `index`
- `segments` (ordered): `purpose` (`dialogue`, `b_roll`, `music`, `sfx`, `title`), `track_id`, `record_in` (timeline frames relative to the timeline start), `source_id`, `source_in`, `source_out` plus an explicit `range_convention` (`inclusive_out` = last included frame, `exclusive_out` = first excluded frame), `media` (`video`, `audio`, `video_audio`), `audio_track_id`, `speaker`, `title_text`/`title_duration_frames` (titles only), `audio_gain_db` (any value is refused by the capability gate), and a `rationale` (`text` plus `brief_item_id`). The timeline duration is derived from the source length and both rates (floor, as Resolve does); it is never stored.
- `transitions` (`cut`, `dissolve`, `dip_to_black`; between two adjacent segments), `markers`, `captions` (`text` must quote a `transcript_ref` of source frames), each with a rationale
- `qc_expectations`: `total_duration_frames`, `frame_rate`, `resolution`, `no_gaps_on_base_tracks`, `captions_match_transcript`, `sources_unmodified`, `custom`
- `exports`: requested outputs (directory, file name, format, codec)

Validation returns a structured report (errors and warnings with codes, total duration, gaps, plan hash) and never raises on bad data. It covers exact frame arithmetic, per-track overlap, mixed-rate conversion (exact, or flagged), the capability gate against `resolve_executor` (an UNSUPPORTED item — audio gain, any transition other than a plain cut, subtitle tracks/captions — is a hard error, never dropped) and the source-fidelity rules of section 2 as hard errors: dialogue only from creator recordings, no generated speech, captions quote a transcript range, outputs never inside a source directory, and exports only inside an explicitly configured allowlist (empty allowlist refuses every export; `CreatorStudio\Projects` is never an approved destination). `plan_hash` is the sha256 of the canonical JSON (sorted keys, reduced rates, NFC text). `to_executor_requests(plan)` builds the executor requests without executing anything. `plan_from_brief(brief, inventory, transcript=None)` is a deterministic offline planner that orders the Creative Director's chosen arc ranges into a candidate plan for human/QC review; it is not a creative engine.

Not yet in the schema (still proposed): playback speed/retiming, effects, asset provenance and rights status, platform targets, per-edit transforms. Timing stays in explicit frame counts on an exact timebase; original source frame numbers are preserved.

## 11. Style profiles

Editing profiles may be learned for:
- Vlogs/day-to-day.
- DIY/build/process videos.
- Mycology/biology/science explainers.
- Tech reviews/tutorials.
- Humor/commentary.
- Opinion/current-events discussion.

Profiles should be starting points, not rigid templates. The actual footage and creator delivery take priority.

## 12. Evidence and experimentation

Maintain an internal knowledge base of credible studies, professional editing examples and measured outcomes. Record:
- Source and date.
- What the source actually supports.
- Context and limitations.
- Which editing hypothesis it informs.
- Whether the hypothesis has been tested on this creator’s audience.

Do not claim that a particular cut frequency or effect universally increases retention without evidence.
