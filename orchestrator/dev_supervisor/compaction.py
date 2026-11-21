"""Compaction policy for a supervised session (T-DEV-003, issue #44 part C).

Given the per-session context pressure from L1 (``telemetry.context_pressure``) and the L1 ``PressureConfig``
thresholds, ``decide`` returns one of NONE / WARN / COMPACT / REPLACE with a recorded reason. The level is recomputed
from the ratio with the configured thresholds (the same ``PressureConfig.level`` L1 uses), so changing the thresholds
changes the decision.

Safety rules (IMPLEMENTED, tested offline):
- Pressure UNAVAILABLE (no transcript, unknown model window, ...) gives NONE with a reason that says so. No figure is
  guessed and nothing is requested.
- Stale pressure evidence gives NONE (the figure is too old to act on).
- COMPACT or REPLACE is only *allowed* (``allowed=True``) when the session is idle AND a handoff was verified just now
  (a ``VerifiedHandoff`` within ``handoff_max_age_s``). Otherwise the decision keeps its action but lists what blocks it.
- ``request_compaction`` calls the injected requester only for an allowed COMPACT decision and records every outcome in
  the state store event log (``compaction_decision``, ``compaction_requested``, ``compaction_refused``).
- Context pressure is a per-session figure, not account usage. Compaction or a fresh session does NOT replenish
  account usage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

try:
    from .evidence import Evidence, Label
    from .handoff import UnverifiedHandoffError, VerifiedHandoff, require_verified
    from .state_store import StateStore
    from .telemetry import PressureConfig
except ImportError:  # pragma: no cover - standalone import
    from evidence import Evidence, Label  # type: ignore
    from handoff import UnverifiedHandoffError, VerifiedHandoff, require_verified  # type: ignore
    from state_store import StateStore  # type: ignore
    from telemetry import PressureConfig  # type: ignore

__all__ = ["NONE", "WARN", "PREPARE_HANDOFF", "COMPACT", "REPLACE", "ACTIONS", "CompactionDecision",
           "CompactionRequester", "decide", "record_decision", "request_compaction", "USAGE_NOTE", "ContextTracker",
           "CONTEXT_DOC", "LEVEL_RANK"]

NONE, WARN, PREPARE_HANDOFF, COMPACT, REPLACE = "NONE", "WARN", "PREPARE_HANDOFF", "COMPACT", "REPLACE"
ACTIONS = (NONE, WARN, PREPARE_HANDOFF, COMPACT, REPLACE)
_LEVEL_TO_ACTION = {"ok": NONE, "warn": WARN, "prepare_handoff": PREPARE_HANDOFF, "compact": COMPACT,
                    "replace": REPLACE}
LEVEL_RANK = {"ok": 0, "warn": 1, "prepare_handoff": 2, "compact": 3, "replace": 4}
CONTEXT_DOC = "context_mgmt"
USAGE_NOTE = ("context pressure is a per-session figure, not account usage; compacting or starting a fresh session "
              "does NOT replenish account usage")
DEFAULT_HANDOFF_MAX_AGE_S = 300.0
DEFAULT_PRESSURE_MAX_AGE_S = 600.0


@dataclass
class CompactionDecision:
    action: str
    reason: str
    allowed: bool = False                     # True only for COMPACT/REPLACE with idle session + fresh verified handoff
    blocked_by: List[str] = field(default_factory=list)
    pressure_label: str = "UNAVAILABLE"
    ratio: Optional[float] = None              # None whenever the pressure is UNAVAILABLE: never a guessed figure
    level: Optional[str] = None
    thresholds: Dict[str, float] = field(default_factory=dict)
    session_id: str = ""
    handoff_id: Optional[str] = None
    usage_note: str = USAGE_NOTE

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "allowed": self.allowed,
                "blocked_by": list(self.blocked_by), "pressure_label": self.pressure_label, "ratio": self.ratio,
                "level": self.level, "thresholds": dict(self.thresholds), "session_id": self.session_id,
                "handoff_id": self.handoff_id, "usage_note": self.usage_note}


def _as_evidence(v: Any) -> Optional[Evidence]:
    if isinstance(v, Evidence):
        return v
    if isinstance(v, Mapping) and v.get("label") in {l.value for l in Label}:
        try:
            return Evidence(Label(v["label"]), v.get("value"), v.get("timestamp", ""), v.get("source", ""),
                            v.get("reason", ""))
        except ValueError:
            return None
    return None


def _idle_state(idle: Any) -> Tuple[Optional[bool], str]:
    """(True/False/None-for-unknown, label). An UNAVAILABLE idle signal is treated as NOT idle."""
    if isinstance(idle, bool):
        return idle, "caller"
    ev = _as_evidence(idle)
    if ev is None or not ev.available:
        return None, "UNAVAILABLE"
    return bool(ev.value), ev.label.value


def decide(pressure: Optional[Mapping[str, Any]], *, idle: Any, verified: Optional[VerifiedHandoff] = None,
           config: Optional[PressureConfig] = None, now: Optional[float] = None, session_id: str = "",
           handoff_max_age_s: float = DEFAULT_HANDOFF_MAX_AGE_S,
           pressure_max_age_s: float = DEFAULT_PRESSURE_MAX_AGE_S) -> CompactionDecision:
    """Decide NONE / WARN / COMPACT / REPLACE from labelled context pressure. Pure: records nothing (see
    ``record_decision``)."""
    cfg = config or PressureConfig()
    now_ts = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    win_ev = _as_evidence((pressure or {}).get("window"))
    window = win_ev.value if win_ev is not None and win_ev.available and isinstance(win_ev.value, int) else None
    thr = cfg.effective(window)          # the absolute cost guard (compact_tokens) needs the window
    dec = CompactionDecision(action=NONE, reason="", thresholds=dict(thr), session_id=session_id)
    ratio_ev = _as_evidence((pressure or {}).get("ratio"))
    if ratio_ev is None or not ratio_ev.available:
        why = ratio_ev.reason if ratio_ev is not None else "no pressure supplied"
        dec.reason = (f"context pressure UNAVAILABLE ({why}); no exact figure is claimed and no compaction or "
                      "replacement is requested")
        dec.blocked_by.append("pressure_unavailable")
        return dec
    dec.pressure_label = ratio_ev.label.value
    if ratio_ev.is_stale(pressure_max_age_s, datetime.fromtimestamp(now_ts, tz=timezone.utc)):
        dec.reason = (f"context pressure evidence is stale or untimestamped (older than {int(pressure_max_age_s)}s); "
                      "re-read it before deciding")
        dec.blocked_by.append("pressure_stale")
        return dec
    try:
        ratio = float(ratio_ev.value)
    except (TypeError, ValueError):
        dec.pressure_label = "UNAVAILABLE"
        dec.reason = "context pressure ratio is not a number; treated as UNAVAILABLE, nothing requested"
        dec.blocked_by.append("pressure_unavailable")
        return dec
    dec.ratio = ratio
    dec.level = cfg.level(ratio, window)
    dec.action = _LEVEL_TO_ACTION[dec.level]
    t = thr
    pct = f"{ratio:.1%} [{dec.pressure_label}]"
    if dec.action == NONE:
        dec.reason = f"context pressure {pct} is below the warn threshold {t['warn']:.0%}"
        return dec
    if dec.action == WARN:
        dec.reason = f"context pressure {pct} reached the warn threshold {t['warn']:.0%}; no action requested"
        return dec
    if dec.action == PREPARE_HANDOFF:
        dec.reason = (f"context pressure {pct} reached the prepare_handoff threshold {t['prepare_handoff']:.0%}; the "
                      "supervisor writes a durable handoff (nothing is compacted or replaced yet)")
        return dec
    limit = t["compact"] if dec.action == COMPACT else t["replace"]
    base = f"context pressure {pct} reached the {dec.action.lower()} threshold {limit:.0%}"
    idle_v, idle_label = _idle_state(idle)
    if idle_v is not True:
        dec.blocked_by.append("session_not_idle" if idle_v is False else "idle_state_unavailable")
    try:
        v = require_verified(verified, now=now_ts, max_age_s=handoff_max_age_s)
        dec.handoff_id = v.handoff_id
    except UnverifiedHandoffError as e:
        dec.blocked_by.append("no_fresh_verified_handoff")
        base += f"; {e}"
    dec.allowed = not dec.blocked_by
    if dec.allowed:
        dec.reason = f"{base}; session idle ({idle_label}) and handoff {dec.handoff_id} verified just now"
    else:
        dec.reason = f"{base}; deferred: {', '.join(dec.blocked_by)}"
    return dec


def record_decision(store: StateStore, decision: CompactionDecision) -> Dict[str, Any]:
    return store.append_event("compaction_decision", **decision.to_dict())


# ------------------------------------------------------------------ unattended context management (T-DEV-030)

PREPARE_REFRESH_S = 1800.0        # a prepared handoff older than this is rewritten while pressure stays high
BOUNDARY_SLACK_S = 60.0           # a compact_boundary this much before our first observation of the crossing still counts
STARTED_GRACE_S = 300.0           # after a PreCompact marker, wait this long before counting observations as failures
DEFAULT_FAILED_OBSERVATIONS = 6


def _fresh_state(session_id: str) -> Dict[str, Any]:
    return {"session_id": session_id,
            "prepare": {"active": False, "handoff_id": None, "prepared_at": None, "count": 0},
            "compact": {"state": "none", "crossed_at": None, "consecutive": 0, "last_obs": None, "started": False,
                        "started_at": None, "boundary_at": None, "result": None}}


class ContextTracker:
    """Per-session context-management state in the store document ``context_mgmt`` (so a supervisor restart keeps it
    and never repeats an action). The transition functions are pure (they mutate the dict they are given and return
    the events to record); the supervisor does the I/O around them.

    compact state: none -> pending (>= compact crossed, native compaction expected) -> boundary_seen (a
    ``compact_boundary`` transcript record after the crossing) -> none again with a COMPACTED event once usage is
    below prepare_handoff. pending -> failed (COMPACT_FAILED) after K consecutive NEW observations at >= compact with no
    boundary record (and no recent PreCompact marker)."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def load(self, session_id: str) -> Dict[str, Any]:
        doc = self.store.read(CONTEXT_DOC, default=None)
        if not isinstance(doc, dict) or doc.get("session_id") != session_id:
            return _fresh_state(session_id)
        base = _fresh_state(session_id)
        base["prepare"].update(doc.get("prepare") or {})
        base["compact"].update(doc.get("compact") or {})
        return base

    def save(self, st: Dict[str, Any]) -> None:
        self.store.write(CONTEXT_DOC, st)

    @staticmethod
    def prepare_action(st: Dict[str, Any], level: str, now: float, refresh_s: float = PREPARE_REFRESH_S) -> str:
        """'prepare' (new crossing), 'refresh' (still high, handoff old), 'clear' (back below) or 'none'."""
        p, rank = st["prepare"], LEVEL_RANK.get(level, 0)
        if rank >= LEVEL_RANK["prepare_handoff"]:
            if not p["active"]:
                return "prepare"
            if p["prepared_at"] is None or now - float(p["prepared_at"]) > refresh_s:
                return "refresh"
            return "none"
        return "clear" if p["active"] else "none"

    @staticmethod
    def step_compaction(st: Dict[str, Any], *, level: str, ratio: Optional[float], label: str,
                        obs_key: Optional[str], boundary_ts: Optional[float], started_ts: Optional[float],
                        now: float, k: int = DEFAULT_FAILED_OBSERVATIONS,
                        launched_ratio: Optional[float] = None) -> List[Tuple[str, Dict[str, Any]]]:
        """``launched_ratio``: the autocompact percentage (/100) Claude Code was actually launched with. Native
        compaction is only expected from max(compact level, launched_ratio); observations below that are not counted."""
        c, rank, events = st["compact"], LEVEL_RANK.get(level, 0), []
        high = rank >= LEVEL_RANK["compact"]
        armed = high and (launched_ratio is None or ratio is None or float(ratio) >= float(launched_ratio))
        below_prepare = rank < LEVEL_RANK["prepare_handoff"]
        info = {"session_id": st["session_id"], "ratio": ratio, "pressure_label": label}
        if c["state"] == "none":
            if high:
                c.update(state="pending", crossed_at=now, consecutive=1 if armed else 0, last_obs=obs_key, started=False,
                         started_at=None, boundary_at=None, result=None)
                events.append(("compaction_expected", {**info, "k": k}))
            return events
        crossed = float(c["crossed_at"] or now)
        if c["state"] in ("pending", "failed") and started_ts is not None and not c["started"] \
                and started_ts >= crossed - BOUNDARY_SLACK_S:
            c.update(started=True, started_at=started_ts)
            events.append(("compaction_started", {**info, "marker": "PreCompact hook"}))
        if c["state"] in ("pending", "failed") and boundary_ts is not None and boundary_ts >= crossed - BOUNDARY_SLACK_S:
            c.update(state="boundary_seen", boundary_at=boundary_ts)
            events.append(("compact_boundary_seen", {**info, "boundary_epoch": round(boundary_ts, 3)}))
        if c["state"] == "boundary_seen":
            if below_prepare:
                events.append(("COMPACTED", {**info, "verified": True, "boundary_epoch": round(float(c["boundary_at"]), 3),
                                              "evidence": "compact_boundary record in the transcript and usage "
                                                          "below the prepare_handoff threshold"}))
                c.update(state="none", result="COMPACTED", consecutive=0, last_obs=None)
            return events
        if c["state"] == "pending":
            if high:
                if armed and obs_key is not None and obs_key != c["last_obs"]:
                    c["last_obs"] = obs_key
                    in_grace = c["started"] and c["started_at"] is not None and now - float(c["started_at"]) < STARTED_GRACE_S
                    if not in_grace:
                        c["consecutive"] = int(c["consecutive"]) + 1
                    if c["consecutive"] >= k:
                        c.update(state="failed", result="COMPACT_FAILED")
                        events.append(("COMPACT_FAILED", {**info, "observations": c["consecutive"],
                                                          "reason": "usage stayed at or above the compact threshold "
                                                                    "with no compact_boundary record"}))
            elif below_prepare or rank < LEVEL_RANK["compact"]:
                c.update(state="none", consecutive=0, last_obs=None, result="cleared_without_boundary")
                events.append(("compaction_crossing_cleared", {**info, "reason": "usage fell below compact with no "
                                                                                "compact_boundary record: NOT verified"}))
        elif c["state"] == "failed" and below_prepare:
            c.update(state="none", consecutive=0, last_obs=None)
            events.append(("compact_failed_cleared", info))
        return events


class CompactionRequester(Protocol):
    def request_compaction(self, session_id: str, handoff_id: str) -> Tuple[bool, str]: ...


def request_compaction(store: StateStore, decision: CompactionDecision, requester: Any) -> Dict[str, Any]:
    """Ask the injected requester to compact, only for an allowed COMPACT decision. Every outcome is recorded."""
    record_decision(store, decision)
    if decision.action != COMPACT or not decision.allowed or not decision.handoff_id:
        why = (f"decision is {decision.action}" if decision.action != COMPACT
               else f"blocked by {', '.join(decision.blocked_by) or 'missing verified handoff'}")
        store.append_event("compaction_refused", session_id=decision.session_id, reason=why)
        return {"requested": False, "ok": False, "reason": why, "usage_note": USAGE_NOTE}
    try:
        ok, detail = requester.request_compaction(decision.session_id, decision.handoff_id)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"requester failed ({e.__class__.__name__})"
    store.append_event("compaction_requested", session_id=decision.session_id, handoff_id=decision.handoff_id,
                       ok=bool(ok), detail=str(detail)[:200], reason=decision.reason)
    return {"requested": True, "ok": bool(ok), "reason": str(detail)[:200], "usage_note": USAGE_NOTE}
