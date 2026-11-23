"""Deterministic progress guard for Main (T-DEV-035).

Pure: no I/O, no process control, time is an argument. The Supervisor loop gathers the observations, calls ``decide``,
carries out the action, and persists the returned state. No language model is involved.

Observations are only what the Supervisor saw this tick. Any signal that is missing is ``None`` and produces NO action.

Actions
- NONE       nothing to do
- HOLD       wait: a condition for acting is not met, or a duplicate action would be sent, or no verified handoff exists
- REPLACE    replace a dead or context-exhausted Main; only when the session is observed idle and a handoff is verified
             (through the host's supported handover command)
- ESCALATE   record a blocker for a human, once per condition: an idle Main with eligible work (the Stop hook did not
             keep it working), a replacement budget reached, or a hold that lasted past its timeout

Contract change (T-DEV-036): CONTINUE was removed. Attached Main has no supported terminal-input channel, and an idle
Main has already ended its turn, so the Stop hook cannot continue it. The state schema keeps its earlier fields
(last_continue_at, continuations_without_progress, cooldown_s, non_progress_limit) for compatibility; they are unused.

Safety rules
- Never REPLACE while the session is observed busy (never interrupt active work).
- Never REPLACE without a verified handoff.
- An idle Main with eligible work escalates once; escalation holds until the progress marker changes.
- Replacements are budgeted by a lifetime count (a clock step cannot restore it); exhausting it escalates once.
- ESCALATE for an idle Main with eligible work is emitted once and stays until the progress marker changes; budget and
  hold escalations are emitted once per condition.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Mapping, Optional

NONE, HOLD, REPLACE, ESCALATE = "NONE", "HOLD", "REPLACE", "ESCALATE"
ACTIONS = (NONE, HOLD, REPLACE, ESCALATE)


@dataclass(frozen=True)
class Policy:
    idle_min_s: float = 300.0           # observed idle this long before an escalation
    cooldown_s: float = 900.0           # unused since T-DEV-036 (kept for the persisted schema)
    non_progress_limit: int = 3         # unused since T-DEV-036 (kept for the persisted schema)
    replace_pressure_pct: float = 85.0  # context pressure at which a replacement is considered
    replace_budget: int = 3             # replacements allowed over the lifetime of the state (no time window)
    replace_window_s: float = 3600.0    # unused since T-DEV-036 (the budget is a count, not a window)
    hold_escalate_s: float = 4 * 3600.0   # a hold this long escalates once


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
    corrupt: bool = False
    held_since: Optional[float] = None
    hold_escalated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Any) -> "GuardState":
        """Restore persisted state. Malformed or incomplete state is FAIL-CLOSED: the result is marked corrupt and decide()
        refuses to act on it until a human repairs or resets the state. A brand-new guard starts from GuardState(), not
        from an empty dict; a missing state file is the caller's concern."""
        if not isinstance(data, Mapping):
            return cls(corrupt=True)
        out = cls()
        for f in fields(cls):
            if f.name not in data:
                return cls(corrupt=True)   # a complete state always has every key; a truncated one is malformed
            value = data[f.name]
            if f.name in ("was_alive", "escalated", "budget_escalated", "hold_escalated", "corrupt"):
                ok = isinstance(value, bool)
            elif f.name == "continuations_without_progress":
                ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            elif f.name == "replacements":
                ok = isinstance(value, list) and all(_finite_number(v) for v in value)
            elif f.name == "last_marker":
                ok = value is None or isinstance(value, str)
            else:
                ok = value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)
                                       and value == value and abs(value) != float("inf"))
            if not ok:
                return cls(corrupt=True)   # never a partial restore; never a silent reset
            setattr(out, f.name, list(value) if f.name == "replacements" else value)
        return out


@dataclass(frozen=True)
class Decision:
    action: str
    label: str        # the observed condition: NOT_RUNNING, CRASHED, UNKNOWN, BUSY, IDLE, EXHAUSTED, ESCALATED
    reason: str


def _normalised(obs: Observation) -> Observation:
    """Strict observation: non-finite or non-numeric readings become unobserved (None); flags must be exact booleans."""
    alive = obs.main_alive if isinstance(obs.main_alive, bool) else None
    status = obs.session_status if isinstance(obs.session_status, str) else None
    marker = obs.progress_marker if isinstance(obs.progress_marker, str) else None
    work = obs.eligible_work if (isinstance(obs.eligible_work, int) and not isinstance(obs.eligible_work, bool)
                                 and obs.eligible_work >= 0) else None
    pressure = obs.pressure_pct if _finite_number(obs.pressure_pct) else None
    return Observation(now=obs.now, main_alive=alive, session_status=status, progress_marker=marker,
                       eligible_work=work, pressure_pct=pressure, handoff_verified=obs.handoff_verified is True)


def _finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _recent_replacements(state: GuardState, now: float, policy: Policy) -> List[float]:
    """The replacement budget is a count, not a time window. A time window cannot tell a real elapsed interval from a
    forward clock step, so nothing here prunes by time; only a human reset of the state clears it (see PR notes)."""
    return list(state.replacements)


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
        return Decision(ESCALATE, label, "replacement budget exhausted; restart loop refused")
    state.budget_escalated = False
    state.replacements = recent + [obs.now]
    state.last_continue_at = None
    state.continuations_without_progress = 0
    state.idle_since = None
    return Decision(REPLACE, label, "Main is idle with a verified handoff; replacing it")


def decide(state: GuardState, obs: Observation, policy: Policy = Policy()) -> "tuple[GuardState, Decision]":
    """Return (new state, decision). The input state is not mutated."""
    st = GuardState.from_dict(state.to_dict())
    if st.corrupt:
        if st.escalated:
            return st, Decision(HOLD, "UNKNOWN", "persisted guard state is malformed; actions refused until repaired")
        st.escalated = True
        return st, Decision(ESCALATE, "UNKNOWN", "persisted guard state is malformed; refusing all actions, escalating")

    if not _finite_number(obs.now):
        return st, Decision(NONE, "UNKNOWN", "observation time is not a finite number")
    obs = _normalised(obs)
    if obs.main_alive is None and obs.progress_marker is None and obs.session_status is None:
        st.idle_since = None

    # Idle continuity: unbroken only while every signal is observed and eligible work exists. Any other tick restarts it.
    continuous = (obs.main_alive is True and obs.session_status == "idle" and obs.progress_marker is not None
                  and obs.eligible_work is not None and obs.eligible_work > 0)
    if not continuous:
        st.idle_since = None

    if obs.main_alive is None:
        return st, Decision(NONE, "UNKNOWN", "Main liveness not observed or not a boolean")

    if obs.main_alive is False:
        if st.was_alive:
            # stays "was alive" while dead: a held crash is re-evaluated later; the replacement budget bounds it
            return st, _hold_or_act(st, obs, policy, "CRASHED", _replace_or_hold(st, obs, policy, "CRASHED"))
        return st, Decision(NONE, "NOT_RUNNING", "Main is not running")

    st.was_alive = True

    if obs.pressure_pct is not None and obs.pressure_pct >= policy.replace_pressure_pct:
        return st, _hold_or_act(st, obs, policy, "EXHAUSTED", _replace_or_hold(st, obs, policy, "EXHAUSTED"))
    st.held_since = None
    st.hold_escalated = False

    if obs.progress_marker is not None and obs.progress_marker != st.last_marker:
        landed = st.last_marker is not None   # a change from a known marker is progress landing; the first marker is not
        st.last_marker = obs.progress_marker
        st.continuations_without_progress = 0
        st.escalated = False
        if landed:
            return st, Decision(NONE, "PROGRESSING", "progress marker changed; not interrupting this tick")

    if obs.progress_marker is None:
        return st, Decision(NONE, "UNKNOWN", "progress not observed; cannot verify progress, so no action")

    if obs.session_status is None:
        return st, Decision(NONE, "UNKNOWN", "session status not observed")

    if obs.session_status == "busy":
        return st, Decision(NONE, "BUSY", "Main is observed busy")

    if obs.session_status != "idle":
        return st, Decision(NONE, "UNKNOWN", "session status is not a recognised value")

    if st.escalated:
        return st, Decision(NONE, "ESCALATED", "escalated; waiting for progress before acting again")
    if obs.eligible_work is None:
        return st, Decision(NONE, "UNKNOWN", "eligible work not observed")
    if obs.eligible_work <= 0:
        return st, Decision(NONE, "IDLE", "idle with no eligible work")
    if st.idle_since is None or st.idle_since > obs.now:
        st.idle_since = obs.now
    idle_for = obs.now - st.idle_since
    if idle_for < policy.idle_min_s:
        return st, Decision(NONE, "IDLE", "idle for " + str(int(idle_for)) + "s, below the minimum")
    st.escalated = True
    return st, Decision(ESCALATE, "IDLE", "Main idle with eligible work: the Stop hook did not keep it working; "
                                          "escalating once (no unsupported continuation)")


def _hold_or_act(st: GuardState, obs: Observation, policy: Policy, label: str, d: Decision) -> Decision:
    """A held condition that lasts past the hold timeout escalates once; a held condition is never silent forever."""
    if d.action != HOLD:
        st.held_since = None
        st.hold_escalated = False
        return d
    if st.held_since is None or st.held_since > obs.now:
        st.held_since = obs.now            # first hold tick, or the clock went backwards: the timer restarts
    if obs.now - st.held_since >= policy.hold_escalate_s and not st.hold_escalated:
        st.hold_escalated = True
        return Decision(ESCALATE, label, "held longer than the hold timeout; escalating for a human: " + d.reason)
    return d
