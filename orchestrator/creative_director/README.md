# Creative Director package

Offline, read-only planner: transcript material (+ optional Creator DNA view and Research packet) in, a
validated **story brief** and **editor view** out. It writes nothing, calls nothing external and is not wired into
the pipeline. Status: **implemented, tested on synthetic data only**. Nothing here is VERIFIED against real footage
or a real Research run. Design background: `AI_Creator_Studio_Documentation/AGENTS.md`.

This README is the in-package home for Creative Director behavior notes, created by `T-CRD-001`. It documents the
Research-input rules below; it does not yet describe the rest of the package.

## Research input: fail-closed behavior (T-CRD-001)

Research is **context, never creator speech**, and a claim the Research Agent could not resolve is never turned into
ordinary guidance. Every rule below fails closed: when something is missing, malformed or inconsistent, the claim (or
the whole packet) becomes *less* usable, never more.

### 1. Exact contract version

`ResearchContext.from_packet()` accepts exactly `research->creative_director/0.3` (`inputs.RESEARCH_CONTRACT`).
A missing, non-string, malformed (case, spaces, suffixes, control characters), older or newer value makes the
**whole packet unusable**: no claims, sources, angles, audience, topic or perspectives are read, `meta` is empty and one
high-severity issue (`research_contract_unsupported`; `research_packet_unusable` for a non-mapping) is recorded. The
rejected value is echoed only cleaned and truncated. The brief is still produced, without research.

### 2. What is preserved on each `ResearchItem`

Producer fields are kept **as supplied and typed**, separate from what this package derives:

| Field | Meaning |
|---|---|
| `producer_kind`, `producer_status` | the claim kind and the **original** status (`verified`, `partial`, ...) from the packet |
| `safety_critical`, `unresolved_safety_warning` | real booleans, or `None` when missing/malformed (never coerced: `"false"`, `0`, `1` are malformed) |
| `safety_metadata` | `ok`, `missing`, `malformed`, or `not_applicable` (opinions, interpretations, hypotheses carry no flags) |
| `producer_usage` | the producer's own `usage` string, never replaced or discarded |
| `usage_check` | `matches`, `mismatch`, `missing`, `unverifiable` against the mirrored producer rule |
| `category`, `usage` | **derived here**; `usage` is this package's rule, distinct from `producer_usage` |
| `restricted`, `restriction_reasons` | why a claim is capped (below) |
| `metadata_conflicts` | duplicate entries that disagreed on producer metadata (below), kept as text, never dropped |

### 3. Restrictions

A factual claim is **restricted** when any of: safety metadata missing or malformed; `unresolved_safety_warning` is
true; it is safety-critical and not verified; its status or kind is unrecognised; its producer usage is missing,
mismatching or cannot be checked. A restricted claim is capped below verified (verified and partially supported become
`unverified_fact`; disputed and contradicted stay), gets the usage `unresolved_safety_do_not_recommend`, a caveat and a
high (safety) or medium (usage/status) issue. It is never ordinary factual guidance. If the creator asserts such a
claim, the planner's issue is raised at high severity.

A **verified safety-critical claim without a warning may stay verified** (not restricted), with a caveat, but it can
**never be used as a hook**.

A verified fact also needs at least one supporting provenance record: `stance="supports"`,
`counted_as_evidence=true`, a recognized `review_label` other than `unreviewed`, and a present recognized
`verification_level` (`assessed`, `human` or `process`). Missing or unknown review metadata never counts as
support, even if the other fields look valid. Duplicate source records merge support-deciding fields pessimistically.

**Duplicate claim ids** (same id sent more than once, in either claim list) are resolved deterministically and
pessimistically, whatever the input order:

- each entry is judged on its **own** metadata; a restricted entry is capped before entries are compared;
- the entry kept is the most restrictive one (lowest permissiveness, restricted first), with remaining ties broken by
  kind, status, usage, text and attribution, never by position;
- safety flags merge pessimistically: any `true` wins, then any missing/malformed flag;
- if the entries disagree on producer kind, status, usage or either safety flag, the disagreement is written to
  `metadata_conflicts` (original values, `<missing>` for absent ones), the claim is restricted
  (`duplicate entries disagree on producer metadata`) and a medium issue `research_duplicate_metadata_conflict` is
  raised. A later entry therefore can never be silently ignored, and a conflict can never make a claim more
  permissive. Identical entries still collapse without restriction.

### 4. Producer usage rule (mirrored)

`inputs.PRODUCER_STATUS_USAGE` copies the Research producer's rule by value: verified `may_state_as_fact`, partial
`attribute_and_hedge`, unverified `do_not_state_as_fact`, disputed `present_all_perspectives`, contradicted
`report_as_contradicted`, any non-verified safety-critical claim `unresolved_safety_claim_do_not_recommend`, other kinds
`label_as_<kind>`.

**Manual synchronization risk.** This package does not import Research internals and no cross-package test pins the two
tables. If the producer changes its rule, this table drifts silently until it is updated by hand together with a new
contract version. The tests here check the mirror only against explicit expected cases in
`tests/test_creative_director.py`.

### 5. Research digest (integrity consistency, not authentication)

`StoryBrief.research_digest` (and the same value on the editor view) is a deterministic sha256 over the research contract
and, per claim (sorted by id): text, attribution, source ids, derived category and usage, original producer kind, status
and usage, the usage check, both safety flags, the safety state, duplicate-metadata conflicts and the restriction.
Provenance records and caveats are not in the digest (they are validated structurally). Validation recomputes it and rejects any difference, so an edit to a covered field is
detected. Validation also re-derives `usage_check`, `restricted`, `restriction_reasons`, `category` limits and `usage`
from the preserved producer fields, so a forged flag cannot be hidden by recomputing the digest alone for the
derived fields.

**Limits.** The digest proves only that the document is internally consistent. It is **not authentication and not
proof of who wrote the packet**: anyone who can rewrite a brief can also recompute the digest, and a forgery that is
consistent in every derived field (for example flipping `safety_critical` on a verified claim and recomputing) cannot be
detected from the document alone. It cannot show the packet itself was honest; it does not replace `material_digest`,
which covers the transcript only.

### 6. Hook validation

A hook's `relies_on_claim_ids` must each be a `verified_fact` that is **not** safety-critical, has no unresolved
warning, is not restricted and has valid safety metadata. Anything else (unknown id, non-verified, opinion,
safety-critical, warned, restricted, metadata missing) invalidates the brief. The same rule runs on editor views.

### 7. Validation also rejects

`research_meta.contract` other than the supported version (or claims without it), a missing/extra digest key, a digest
mismatch, a `verified_fact` whose producer did not say fact/verified, a restricted claim above `unverified_fact`, and
inconsistent flag/state combinations.

### Schema versions

`creative_director.story_brief/0.3` and `creative_director.editor_view/0.3` (both were 0.2). `ResearchItem` gained the
fields above, `StoryBrief` and `EditorView` gained `research_digest`; documents written under 0.2 no longer validate and must be
re-planned. No consumer outside this package reads these versions (checked by search at implementation time).

### Not covered

Real Research output, real footage, the Research producer, and any cross-package behavior. Checks that exist as
defense in depth (for example the hook rule repeating the item-level restriction) are intentionally redundant.
