# First real video: checklist for Lukas

**Status: PROPOSED guidance. Not an instruction to start filming now.** It is delivered to Lukas when the user-action gate
`G-FIRST-REAL-VIDEO` in `PROJECT_STATUS.json` fires, which is when the first end-to-end long-form editing pipeline (Phase 6)
exists and is ready for real-world validation. That is the readiness milestone `M-LFE-03`, which needs evidence that one run processed footage,
produced a validated plan and Resolve timeline, rendered a complete long-form output, completed initial QC and export verification, and kept originals
and project state recoverable. It means ready to *start* validation, not that production-quality editing is proven. Until then the gate is `PENDING`. No scheduled notification or
external automation exists; sessions are told by `python -B -m governance validate` and by `SESSION_HANDOFF.md`.

## Why this moment matters
Every earlier phase was tested on synthetic clips or on one short recording. Only real videos can show whether the editing is good,
and the long-form videos produced now are the source material for Phase 7 (short-form repurposing). When the gate fires, the right
next work is producing and reviewing real videos, not more development-only work.

## What the session must do when the gate fires
1. Tell Lukas, plainly, that the pipeline is ready for real-world validation and that development-only work should pause.
2. Give him this checklist and explain the recording variety below.
3. Explain safe ingestion (below).
4. With his permission, run long-form editing on the real recordings and record every run and his review as evidence.
5. Plan Phase 7 short-form work on the resulting long-form videos.
6. Set the gate to `ACKNOWLEDGED` only when Lukas confirms he has read it.

Before this point the session also confirms what is still open: the transcription engine decision (Q11) and any blocker listed in
`SESSION_HANDOFF.md`, because the pipeline cannot be validated on speech without them.

## Before recording
- [ ] Pick a real topic Lukas would publish anyway. Real intent produces the real personality the system must preserve.
- [ ] Decide the format: one or two long-form videos of roughly 8 to 20 minutes each is enough for a first validation. More is not
      better yet; variety is.
- [ ] Do a 30-second test recording and listen to it on headphones: is the voice clear, is the level steady, is there hum or echo?
- [ ] Set the camera and phone to a **constant** frame rate where the device allows it. Variable frame rate (common on phones and
      screen recorders) is refused by shot detection and must be handled before editing (open question Q22).
- [ ] Make sure there is enough free disk space for the originals plus a backup copy.

## What to record (variety is what makes the validation useful)
- **Talking to camera:** the main take, with natural pauses, restarts and corrections. Do not tidy them: the editor must cope.
- **Different energy:** at least one calm passage and one animated passage; one passage where you laugh or make a joke.
- **A repeated phrase or a re-take** of a sentence, so the editor must choose the best take.
- **Quiet and fast speech** in at least one place, because transcription is weakest there.
- **Camera variety:** a second angle or a closer shot if available; a phone clip if you normally use one.
- **Screen recording** if the video will show software or a browser.
- **B-roll:** a few short cutaways (5 to 10 seconds each) that illustrate what you say.
- **Room tone:** 20 to 30 seconds of silence in the same room, recorded without speaking.
- **Audio:** the best microphone you have, kept at the same distance. If you record separate audio, also clap once at the start so
  picture and sound can be aligned.
- **Notes:** a short text file of what you intended to say and anything the system must not cut (names, jokes, claims).

## What to avoid
- Copyrighted music or footage you do not have the right to use. Rights for B-roll and music are checked by the asset rules.
- Other people's faces or voices without their agreement.
- Deleting or "cleaning up" clips afterwards. Originals are immutable.

## Ingesting the footage safely
1. Copy or move the recordings into `Incoming/<project-name>/` yourself, using the optional subfolders `camera`, `phone`, `screen`,
   `audio`, `images` and `notes` (see `Incoming/README.md`). Keep a second copy on another drive before the system touches anything.
2. Ask the session to run `python -m creator_state incoming-list`, then `incoming-inspect <project-name>`. Both are dry runs.
3. Only after you agree, run `ingest`. Originals are catalogued in place and are never moved, edited or deleted.
4. Run `verify-sources <slug> --deep` whenever you come back to the project, to prove the originals are unchanged.
5. Proxies, managed copies and any run in Resolve need your explicit permission each time.

## How the validation is judged
A milestone counts as verified only after Lukas has reviewed the result on real footage, and that review is recorded as evidence in
`PROJECT_STATUS.json`. Output files existing, or a command finishing, is not enough.
