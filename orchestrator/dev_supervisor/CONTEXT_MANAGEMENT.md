# Unattended context management for Main (T-DEV-030, issue #44)

Status: IMPLEMENTED and tested offline with fakes (`orchestrator/tests/test_dev_context_management.py`). Nothing here is
VERIFIED against a live Claude Code session yet: the live proof is `context_monitor.py live-compaction-probe` (opt-in,
run by Main, see section 7). Facts about Claude Code below were OBSERVED by Main in the installed binary (2.1.316):
the `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` variable, the statusLine stdin JSON with `context_window`, the `compact_boundary`
transcript record and the `PreCompact` hook event.

**Context compaction does not reset Claude account limits.** Neither does replacing Main with a fresh session. Both only
shrink the per-session context, which lowers what each later API call re-reads (that is how it saves usage, see
`USAGE_ANALYSIS.md`), but plan limits and reset windows are unchanged. Context pressure is a per-session figure, never an
account figure.

## 1. Thresholds and actions

Configured in the machine-local `config.json` (`thresholds`, `compact_tokens`, `compact_failed_observations`; template:
`python -B -m dev_supervisor config --example`). Ratios are of the context window.

| Level | Default | What happens |
|---|---|---|
| warn | 60% | nothing (kept for display and compatibility) |
| prepare_handoff | 70% | the supervisor writes a durable MECHANICAL handoff and verifies it by read-back; a hook tells Main |
| compact | 80% | Claude Code compacts Main natively (the launch sets `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`); the supervisor verifies it |
| replace | 90% | the supervisor replaces Main, only with a fresh verified handoff and only while Main is idle |

Cost guard: `compact_tokens` (default 400000, `null` disables). Because context size drives account usage, native
compaction fires at `min(compact ratio, compact_tokens / window)`; warn and prepare_handoff scale down with it so the order
is kept. Example: 1,000,000-token window, guard 400000 -> compact at 40%, prepare_handoff at 35%.
The supervisor applies the guard whenever the window is known. For the launch variable the window comes from, in order:
`context_windows` for the configured model id, else the newest window seen in a statusLine record. A model alias such as
`opus` is not in the built-in window table, so before any statusLine record exists only the ratio applies: put
`"context_windows": {"opus": 1000000}` in `config.json` to make the first launch apply the token guard too.

COMPACT never replaces Main by itself. REPLACE, or COMPACT after `COMPACT_FAILED`, replaces Main when
`auto_replace_main` is true in the config (default false: the decision is recorded and `replace --handoff-id` stays a
manual command). `/clear` is never sent anywhere: a test scans the package for it.

## 2. Where the figure comes from (labels)

1. **OBSERVED**: the statusLine command added to Main's derived settings (`context_monitor.py statusline`) receives the
   JSON Claude Code itself computes (`context_window.used_percentage`, `context_window_size`) and atomically writes
   `<state>/context/<session_id>.json` (`session_id`, `used_percentage`, `context_window_size`, `model`, `utc`). The
   supervisor uses it when it is younger than 10 minutes and names exactly the Main session. Ratio and window are OBSERVED;
   the token count is derived from them and labelled INFERRED.
2. **INFERRED**: otherwise the last assistant usage record of the Main transcript against the configured model window.
3. **UNAVAILABLE**: neither exists (no file, stale, other session, no transcript, unknown model window). No figure is
   guessed and no action is taken on it.

A stale or foreign statusLine record is ignored, never trusted. `status` and `compact-check` show the label.

The user's own `statusLine` (from `~/.claude/settings.json`) is kept: ours runs first, then theirs with the same stdin, and
theirs is printed (the command is stored in a sidecar file next to the derived settings and executed through the shell, as
Claude Code would). If their command fails or prints nothing, ours prints. If their statusLine is not a `command` type, ours
wins and theirs is dropped for that session.

## 3. At prepare_handoff: the durable handoff

On crossing the threshold the supervisor builds a handoff from recorded state only (no model-written text, no transcript):
the ACTIVE tasks from `ACTIVE_TASKS.json` (id, title, dependencies), the worker registry (running, stale and failed), open
pull requests through `gh pr list` (`UNAVAILABLE`, with the reason, when no gh runner exists or it fails), the git head of each
ACTIVE task branch (a branch that does not resolve is left out), pending approvals as blockers, the last 15 supervisor events,
and the labelled context and usage signals. The existing handoff format and `verify_handoff` read-back are used unchanged.

- Event `handoff_prepared` is recorded once per crossing, and again (`reason: refresh`) when the handoff is older than 30
  minutes while pressure stays above the threshold. Falling below the threshold re-arms it. A verified handoff for this
  session younger than 30 minutes (written by Main or by an earlier crash) is adopted instead of rewritten
  (`adopted: true`). A failed build or verification records `handoff_prepare_failed` and is retried after 2 minutes.
- Main hears about it through the PostToolUse hook (`context_monitor.py notice`): once per new notice file it adds
  `context at 72%: durable handoff h-... prepared; add your own notes with dev_supervisor handoff if needed` as
  `additionalContext`. It is silent otherwise, ignores any text in the notice file except the validated handoff id and
  percentage, and never blocks.
- Main may add its own notes with `python -B -m dev_supervisor handoff --input <facts.json> --yes`.

## 4. At compact: native compaction, verified

Main is launched (and every replacement Main is launched, through the same `launch.open_main`) with
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` set to the effective compact percentage as an integer in 1..100 (rounded down). A
`PreCompact` hook (`context_monitor.py precompact`) writes `<state>/context/compaction-started-<session_id>.json`; it never
blocks compaction.

When the supervisor first sees pressure at or above compact it records `compaction_expected` and follows the state
`pending -> boundary_seen -> done`, persisted in the store document `context_mgmt`:

- **COMPACTED (verified)**: a `compact_boundary` record with a timestamp at or after the crossing (60 s of slack) AND
  pressure below prepare_handoff afterwards (OBSERVED when the statusLine is fresh, else INFERRED from the next assistant
  usage record). A boundary alone, or a usage drop alone, is not enough. Usage that falls below compact with no boundary
  is recorded as `compaction_crossing_cleared` (NOT verified).
- **COMPACT_FAILED**: K consecutive NEW observations (default `compact_failed_observations` 6) at or above compact with no
  boundary. The crossing counts as the first. An observation is new when the statusLine timestamp or the transcript token
  count changed, so an idle Main does not accumulate failures. After a `PreCompact` marker the count pauses for 5 minutes. A
  late boundary after a failure still moves to `boundary_seen`.

## 5. At replace, or COMPACT_FAILED at compact

Uses `replace_main` / `replacement.replace_session` unchanged in spirit: the supervisor first refreshes the handoff if the
prepared one is older than 5 minutes, then replaces only when Main is idle (OBSERVED from its session file) and a handoff
verified just now exists. Order inside `replace_session`: new Main launched, its Claude session file found with a live pid
(heartbeat), the new session recorded authoritative and ACTIVE (the old one demoted), handoff verified again; then, and only
then, `main.json` is pointed at the new session (`main_promoted`) and the old Main retired. If the sessions record does not
show the new session authoritative and ACTIVE, nothing is retired. If the new Main never comes up the result is `ROLLED_BACK`
and the old Main stays authoritative. A supervisor that did not start the old Main never signals it (`REPLACED_OLD_NOT_CLEARED`:
close that window by hand). `/clear` is never sent.

## 6. Safety and failure modes

- One authoritative Main: `register_session` demotes any other; verified by tests after replacement and after restart.
- Workers are never touched by context management (a test replaces Main with two workers running).
- The supervisor lock is unchanged. State survives a supervisor restart (`context_mgmt`, `handoffs/`, event log): a new
  `SupervisorLoop` repeats no handoff, crossing or compaction event. A crash between writing a handoff and recording it adopts
  the written handoff; a crash between the handoff and the replacement reuses it.
- Exception safety: every new step runs inside `tick`; an error records `context_management_error` and the loop continues.
- Failure modes and their result: statusLine command missing or failing -> falls back to the transcript estimate
  (INFERRED); no transcript either -> UNAVAILABLE, no action; handoff cannot be built or verified -> no handoff event, no
  replacement, retry later; native compaction never happens -> `COMPACT_FAILED`, replacement if enabled; `gh` missing -> PRs
  UNAVAILABLE in the handoff; new Main fails to start -> rollback, old Main untouched; transcript shows 0 boundary records
  because the format differs from what was OBSERVED -> `COMPACT_FAILED` (visible, not silent).
- Known limits: the boundary record format and `used_percentage` semantics come from one observation of 2.1.316 and must be
  confirmed by the live probe; the statusLine file is written only while Claude Code redraws the status line; a window
  unknown at launch means the token guard is applied by the supervisor's decisions but not by the launch variable. The
  percentage actually launched is recorded in `main.json` (`autocompact_pct`); COMPACT_FAILED only counts observations at or
  above max(compact level, launched percentage), so it is never reported for a compaction Claude Code was not asked for.
  A statusLine file older than 600 s is not used (an idle Main falls back to INFERRED until the next assistant record).
  An automatic replacement only uses a handoff prepared within the last 5 minutes (refresh failed -> `no_fresh_verified_handoff`).

## 7. Operator commands

```text
python -B -m dev_supervisor status            # shows context ratio with its label and the context-management state
python -B -m dev_supervisor compact-check     # decision for the current Main (exit 4 when COMPACT or REPLACE is due)
python -B -I orchestrator/dev_supervisor/context_monitor.py live-compaction-probe          # dry run
python -B -I orchestrator/dev_supervisor/context_monitor.py live-compaction-probe --yes    # live proof, Main runs it
```

The probe starts a throwaway headless session (haiku model, temp folder, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1`) for a few
turns and reports whether a `compact_boundary` record appeared in the stream or the transcript. Unit tests never run it.
