# Deferred from wave w01-acquisition-providers-review

Wave w01 was planned with three lanes. On 2026-11-17 Lukas approved the Repository Coordinator's recommendation to remove lanes L2 and L3 and close the wave as a one-lane wave with L1 (Assets) only. Nothing below was started inside this wave: no lane branch for L2 or L3 was ever pushed and no commit of theirs exists.

## Why

The research and Creator DNA streams opened their own claim pull requests for different assignments before their lanes started. Those pull requests belong to other sessions and were not touched:

- **PR #32** claims `T-RSR-001` for "Research security remediation (Assignment 6)", reusing the task id this wave planned for its Research lane.
- **PR #34** claims `T-DNA-002` for the "learning-ready Creator DNA core (M-DNA-02a)", reusing the task id and the `orchestrator/creator_dna/` paths this wave planned for its Creator DNA lane.

Their task ids and paths collide with the planned lanes (`wave validate` rejects a lane whose task id already exists in `ACTIVE_TASKS.json`), and the owners have other priorities in flight. Dropping the two unstarted lanes lost no work.

## What was deferred (for proper future waves)

The original three-lane manifest, with every acceptance criterion, is preserved in git at the wave's first commit:

`git show a924ff427f4623f9023864c80bc483e9eee8599b:waves/w01-acquisition-providers-review/wave.json`

### L2: Concrete research providers, caching, budgets and the pipeline stage adapter (offline, transport-injected)

- Planned task id `T-RSR-001` (now taken, see above), owner `claude:research-stream`, milestone(s) M-RSR-02.
- Owned paths: `orchestrator/research/`, `orchestrator/tests/test_research.py`, `orchestrator/tests/test_research_providers.py`, `AI_Creator_Studio_Documentation/RESEARCH.md`.
- 8 measurable acceptance criteria and test modules `test_research.py`, `test_research_providers.py` in the manifest above.

### L3: Creator review workflow for Creator DNA: queue, decisions, offline review sheet and CLI

- Planned task id `T-DNA-002` (now taken, see above), owner `claude:creator-dna-stream`, milestone(s) M-DNA-03.
- Owned paths: `orchestrator/creator_dna/`, `orchestrator/tests/test_creator_dna.py`, `orchestrator/tests/test_creator_dna_review.py`, `AI_Creator_Studio_Documentation/CREATOR_DNA.md`.
- 8 measurable acceptance criteria and test modules `test_creator_dna.py`, `test_creator_dna_review.py`, `test_speaker_attribution.py`, `test_integration_phase2_phase3.py` in the manifest above.

### Conditions for rescheduling

- **Research providers (M-RSR-02, offline, transport-injected):** schedule after PR #32 lands or is withdrawn, under a fresh task id, rebased so the lane's paths do not overlap the security remediation.
- **Creator review workflow (M-DNA-03):** schedule after PR #34 lands or is withdrawn; its open design questions Q30 and Q39 (review workflow shape, how the creator is identified) are still undecided and stay a decision for Lukas.
- Both lanes are synthetic-data, offline work: no network, no credentials, no real footage.
