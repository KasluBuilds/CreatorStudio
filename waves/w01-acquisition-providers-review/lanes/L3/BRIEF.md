# Lane L3 of wave w01-acquisition-providers-review: Creator review workflow for Creator DNA: queue, decisions, offline review sheet and CLI

Owner: `claude:creator-dna-stream`  |  Task id: `T-DNA-002`  |  Branch: `lane/w01-acquisition-providers-review/L3`  |  Depends on lanes: none

## You may edit only
- `orchestrator/creator_dna/`
- `orchestrator/tests/test_creator_dna.py`
- `orchestrator/tests/test_creator_dna_review.py`
- `AI_Creator_Studio_Documentation/CREATOR_DNA.md`
- `waves/w01-acquisition-providers-review/lanes/L3/` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/w01-acquisition-providers-review/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
- A new module orchestrator/creator_dna/review.py builds a deterministic review queue from a DNAStore: traits awaiting confirmation, evidence flagged unattributed or legacy_possible_revocation, and traits with conflicting counter-evidence; every item has a stable id, the trait statement, the evidence quotes and timecodes (references only, no media access) and the reason it was queued; the same store always yields the same queue in the same order
- A decision API applies creator decisions (confirm a trait, reject evidence, restore evidence, correct a trait statement, defer) exclusively through the existing event-log methods and rules (schema v4: restore never undoes an attribution revocation); every decision needs an explicit actor and a note, is idempotent where the event model allows it, and is replay-safe (a store rebuilt from its log equals the live store)
- An offline review sheet export (Markdown plus JSON) lets the creator read the queue away from the computer, and a decisions-file import validates the whole file first and then applies it all-or-nothing: one bad entry applies nothing and every error is reported with its line; a dry-run mode reports what would change and writes nothing
- A CLI python -m creator_dna review (queue, export, apply, status) with --store, --dry-run and an explicit --yes before any write, deterministic output, and non-zero exit codes on invalid input, mirroring the style of the creator_state CLI; no command ever deletes evidence or events
- The creator's identity (open question Q39) and the review workflow shape (Q30) are NOT decided: actor is a required explicit string that the module neither infers nor validates against any identity source; the open questions are recorded in the documentation and in the report as decisions for Lukas
- Existing event semantics and schema version 4 are unchanged unless a change is unavoidable and justified in the report; all existing tests in test_creator_dna.py, test_speaker_attribution.py and test_integration_phase2_phase3.py pass unmodified; speaker_attribution, media_analysis and every other package are not edited
- At least 50 new offline tests in orchestrator/tests/test_creator_dna_review.py including determinism of the queue, replay equality after a batch of decisions, an interrupted or partially invalid decisions file leaving the log byte-identical, and the all-or-nothing import; synthetic data only, no real recordings or transcripts
- CREATOR_DNA.md documents the review workflow, the sheet and decision-file formats and the CLI, with IMPLEMENTED versus NOT TESTED labels (nothing is VERIFIED on real data) and the extension point for transcript review once a transcription engine exists; the lane report lists anything needing a decision from Lukas

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: test_creator_dna.py, test_creator_dna_review.py, test_speaker_attribution.py, test_integration_phase2_phase3.py

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `waves/w01-acquisition-providers-review/lanes/L3/report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `wave/w01-acquisition-providers-review`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
