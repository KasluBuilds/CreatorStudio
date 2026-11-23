"""Deterministic progress guard for Main (T-DEV-035).

Pure: no I/O, no process control, time is an argument. The Supervisor loop gathers the observations, calls ``decide``,
carries out the action, and persists the returned state. No language model is involved.

Observations are only what the Supervisor saw this tick. Any signal that is missing is ``None`` and produces NO action.

Actions
- NONE       nothing to do
- CONTINUE   send Main one continuation instruction (Main is observed idle, eligible work remains)
- HOLD       wait: a condition for acting is not met, or a duplicate action would be sent, or no verified handoff exists
- REPLACE    replace a dead or context-exhausted Main; only when the session is observed idle and a handoff is verified
- ESCALATE   stop acting and record a blocker for a human: non-progress limit or replacement budget reached

Safety rules
- Never CONTINUE or REPLACE while the session is observed busy (never interrupt active work).
- Never REPLACE without a verified handoff.
- After CONTINUE, the progress marker must change, or the non-progress counter grows; at the limit the guard escalates.
- Replacements inside a window are budgeted; exhausting the budget escalates instead of looping.
- ESCALATE is emitted once; it stays until the progress marker changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Mapping, Optional

NONE, CONTINUE, HOLD, REPLACE, ESCALATE = "NONE", "CONTINUE", "HOLD", "REPLACE", "ESCALATE"
ACTIONS = (NONE, CONTINUE, HOLD, REPLACE, ESCALATE)


@dataclass(frozen=True)
class Policy:
    idle_min_s: float = 300.0           # observed idle this long before a continuation
    cooldown_s: float = 900.0           # minimum time between continuations (duplicate prevention)
    non_progress_limit: int = 3         # continuations without a marker change before escalation
    replace_pressure_pct: float = 85.0  # context pressure at which a replacement is considered
    replace_budget: int = 3             # replacements allowed inside the window
    replace_window_s: float = 3600.0


@dataclass(frozen=True)
class Observation:
    now: float
    main_alive: Optional[bool]          # None = not observed
    session_status: Optional[str]       # "idle" or "busy" as observed in the session file; None = not observed
    progress_marker: Optional[str]      # changes when real progress lands; None = not observed
    eligible_work: Optional[int]        # actionable items; None = not observed
    pressure_pct: Optional[float]       # context pressure; None = not observed
    handoff_verified: bool = False      # a verified handoff exists for a replacement


@dataclass
class GuardState:
    was_alive: bool = False
    last_marker: Optional[str] = None
    idle_since: Optional[float] = None
    last_continue_at: Optional[float] = None
    continuations_without_progress: int = 0
    replacements: List[float] = field(default_factory=list)
    escalated: bool = False
    budget_escalated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Any) -> "GuardState":
        """Restore persisted state. Anything malformed yields a fresh state (safe: no action is forced by it)."""
        if not isinstance(data, Mapping):
            return cls()
        out = cls()
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.name in ("was_alive", "escalated", "budget_escalated"):
                ok = isinstance(value, bool)
            elif f.name == "continuations_without_progress":
                ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            elif f.name == "replacements":
                ok = isinstance(value, list) and all(isinstance(v, (int, float)) for v in value)
            elif f.name == "last_marker":
                ok = value is None or isinstance(value, str)
            else:
                ok = value is None or isinstance(value, (int, float))
            if ok:
                setattr(out, f.name, list(value) if f.name == "replacements" else value)
        return out


@dataclass(frozen=True)
class Decision:
    action: str
    label: str        # the observed condition: NOT_RUNNING, CRASHED, UNKNOWN, BUSY, IDLE, EXHAUSTED, ESCALATED
    reason: str


def _recent_replacements(state: GuardState, now: float, policy: Policy) -> List[float]:
    return [t for t in state.replacements if now - t < policy.replace_window_s]


def _replace_or_hold(state: GuardState, obs: Observation, policy: Policy, label: str) -> Decision:
    if obs.session_status != "idle":
        return Decision(HOLD, label, "Main is not observed idle; waiting rather than interrupting active work")
    if not obs.handoff_verified:
        return Decision(HOLD, label, "no verified handoff; refusing to replace Main without one")
    recent = _recent_replacements(state, obs.now, policy)
    if len(recent) >= policy.replace_budget:
        if state.budget_escalated:
            return Decision(HOLD, label, "replacement budget exhausted; already escalated, not repeating")
        state.budget_escalated = True
        return Decision(ESCALATE, label, "replacement budget exhausted inside the window; restart loop refused")
    state.budget_escalated = False
    state.replacements = recent + [obs.now]
    state.last_continue_at = None
    state.continuations_without_progress = 0
    state.idle_since = None
    return Decision(REPLACE, label, "Main is idle with a verified handoff; replacing it")


def decide(state: GuardState, obs: Observation, policy: Policy = Policy()) -> "tuple[GuardState, Decision]":
    """Return (new state, decision). The input state is not mutated."""
    st = GuardState.from_dict(state.to_dict())

    if obs.main_alive is None:
        return st, Decision(NONE, "UNKNOWN", "Main liveness not observed")

    if not obs.main_alive:
        if st.was_alive:
            st.was_alive = False
            st.idle_since = None
            return st, _replace_or_hold(st, obs, policy, "CRASHED")
        return st, Decision(NONE, "NOT_RUNNING", "Main is not running")

    st.was_alive = True

    if obs.pressure_pct is not None and obs.pressure_pct >= policy.replace_pressure_pct:
        return st, _replace_or_hold(st, obs, policy, "EXHAUSTED")

    if obs.progress_marker is not None and obs.progress_marker != st.last_marker:
        st.last_marker = obs.progress_marker
        st.continuations_without_progress = 0
        st.escalated = False

    if obs.progress_marker is None:
        return st, Decision(NONE, "UNKNOWN", "progress not observed; cannot verify progress, so no action")

    if obs.session_status is None:
        return st, Decision(NONE, "UNKNOWN", "session status not observed")

    if obs.session_status == "busy":
        st.idle_since = None
        return st, Decision(NONE, "BUSY", "Main is observed busy")

    if obs.session_status != "idle":
        return st, Decision(NONE, "UNKNOWN", "session status is not a recognised value")

    if st.idle_since is None:
        st.idle_since = obs.now
    if st.escalated:
        return st, Decision(NONE, "ESCALATED", "escalated; waiting for progress before acting again")
    if obs.eligible_work is None:
        return st, Decision(NONE, "UNKNOWN", "eligible work not observed")
    if obs.eligible_work <= 0:
        return st, Decision(NONE, "IDLE", "idle with no eligible work")
    idle_for = obs.now - st.idle_since
    if idle_for < policy.idle_min_s:
        return st, Decision(NONE, "IDLE", "idle for " + str(int(idle_for)) + "s, below the minimum")
    if st.last_continue_at is not None and obs.now - st.last_continue_at < policy.cooldown_s:
        return st, Decision(HOLD, "IDLE", "inside the cooldown after the last continuation (no duplicate sent)")
    if st.continuations_without_progress >= policy.non_progress_limit:
        st.escalated = True
        return st, Decision(ESCALATE, "IDLE", "no progress after the continuation limit; escalating")
    st.last_continue_at = obs.now
    st.continuations_without_progress += 1
    return st, Decision(CONTINUE, "IDLE", "idle with eligible work; sending one continuation instruction")
