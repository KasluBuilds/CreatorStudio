# Account-usage analysis of the development supervisor (T-DEV-030, issue #44)

Evidence labels: OBSERVED = measured by Main from API usage records on 2026-11-20; PROPOSED = not done yet; IMPLEMENTED =
in code with offline tests, not yet shown to reduce real usage. Nothing here claims a measured saving.

## 1. Numbers (OBSERVED 2026-11-20)

| Source | API calls | Cache-read tokens | Note |
|---|---|---|---|
| Main (one interactive session) | 332 | about 92 million | context about 480k tokens per call by the end |
| All 25 worker runs together | not counted here | about 53 million | each worker has a small, fresh context |

Main alone read about 1.7 times as many cached tokens as all 25 workers together. The cause is not the number of calls but
their size: every call re-reads the whole context, so cost per call grows with the context, and total cost grows roughly with
the square of the session length. Main's own context size is the largest usage driver; compaction and replacement shrink it
but do not refund what was already used, and they do not reset account limits.

## 2. Recommendations

| # | Recommendation | Status |
|---|---|---|
| 1 | Compact Main earlier through an absolute token guard (`compact_tokens`, default 400000): native compaction fires at min(80% of window, 400000 tokens) instead of at 80% of a 1M window (800k). With 480k tokens per call at the end, the observed session would have been compacted at 400k. | IMPLEMENTED (launch variable + supervisor decisions, see `CONTEXT_MANAGEMENT.md`); effect on real usage NOT MEASURED |
| 2 | Fewer polling turns: Main waits on workers with long Monitor intervals (20 minutes or more) instead of checking every minute; each extra turn re-reads the full context. | PROPOSED (a rule for the Main prompt; not enforced) |
| 3 | Use haiku or sonnet for routine workers (lint-level changes, doc edits, test runs, log reading); reserve opus for security reviews and design decisions. Routing already supports model choice per task (`route`, `models`). | PROPOSED (config `models.worker` is sonnet today; per-task routing policy not tightened) |
| 4 | Run the full test suite only in CI, not in every worker and Main session; workers run their own module only. The worker rules already say so; CI is the gate for the full suite. | PROPOSED for Main and reviewers (workers: already the rule) |
| 5 | Cap concurrency at 1-2 workers: parallel workers each re-read their own prompt and the repository, and Main's tick must also read their results. | PROPOSED (no cap is enforced by the supervisor) |
| 6 | Keep durable state in files (handoffs, registry) so a replacement Main starts with a small context instead of re-reading history. | IMPLEMENTED (mechanical handoff at 70%, replacement at 90%) |

## 3. How to measure the effect (PROPOSED)

Compare cache-read tokens per Main call before and after `compact_tokens` is in force: the `COMPACTED` events carry the
ratio before the boundary, and each worker stream log has its own usage records. Report REPORTED numbers as such until a
second source (the account's own usage page) agrees.
