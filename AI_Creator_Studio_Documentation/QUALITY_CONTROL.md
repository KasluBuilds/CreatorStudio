# Quality Control and Autonomous Review

**Status:** PROPOSED. The QC loop is a target capability, not yet implemented.

## 1. Purpose

Prevent technical failures and identify editorial problems before final delivery. QC should be evidence-based and specific, not a vague “make it better” loop.

## 2. Technical checks

### Media/timeline
- All referenced source files exist and are online.
- No accidental offline media.
- Timeline duration and frame rate are correct.
- Segment in/out points are within source bounds.
- Audio/video sync is maintained.
- No unintended gaps, overlaps or black frames.
- Track routing and channel mapping are correct.

### Render
- Output file exists and is non-zero.
- Container, codec, resolution, frame rate and duration match target.
- Audio and video streams are present.
- File decodes and plays.
- No render job remains failed or indefinitely pending.
- Export path is inside an allowed output directory.

### Platform framing
- Correct aspect ratio and dimensions.
- Important subjects/text remain in safe areas.
- No essential content cropped.
- Captions are readable on a phone.
- Audio is intelligible.

## 3. Editorial checks

- Opening makes a truthful promise.
- Story is coherent and understandable.
- No context-critical sentence has been removed.
- Jokes and reactions retain timing.
- Meaningful pauses are preserved.
- B-roll supports the point being made.
- Graphics do not contradict narration.
- Music/effects do not mask speech.
- Short-form clips have independent context and payoff.
- Creator’s actual speech and personality remain intact.

## 4. Review artifacts

Use:
- Low-resolution review render.
- Representative frame samples.
- Audio samples/waveform.
- Transcript with source/timeline timecodes.
- Timeline metadata and edit plan.
- Asset manifest and rights status.

## 5. Issue format

Each issue should contain:
- `issue_id`
- severity: blocker / major / minor / subjective
- timestamp or frame range
- category
- observed evidence
- expected result
- proposed correction
- confidence
- whether automatic correction is safe

## 6. Retry and stop policy

- Fix blockers before export.
- Recheck after each targeted correction.
- Limit automatic revision cycles.
- If repeated fixes fail or the issue is subjective/ambiguous, report it instead of looping indefinitely.
- Never sacrifice meaning or creator authenticity to satisfy a simplistic metric.

## 7. Current validation baseline

The project has verified one minimal MP4/H.264 export through Resolve Free 21.0.4.5. No full QC pipeline, caption check, audio analysis, vertical export validation or automated editorial review has been implemented yet.
