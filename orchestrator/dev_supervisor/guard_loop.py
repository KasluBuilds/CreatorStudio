"""Progress guard adapter for the SupervisorLoop (T-DEV-037).

Feature-flagged: active only when the environment variable CREATORSTUDIO_SUPERVISOR_GUARD is exactly "1". Off by default,
and SupervisorLoop behaves exactly as before when it is off.

Each tick the loop gathers Signals from real Main sources (done by cli.py), and this adapter:

1. loads the persisted guard state (malformed state fails closed through progress_guard),
2. runs progress_guard.decide on an Observation built from the Signals,
3. carries out the decision:
   - REPLACE: requested through the injected request_handover, which must use the host's supported "handover" command.
     Refused while an earlier handover is still pending (duplicate-intervention prevention). If the request is not sent,
     the decision is discarded, so the replacement budget is not spent on an action that did not happen.
   - ESCALATE: recorded as an event (the guard already emits each condition once).
   - HOLD / NONE: no action.
4. persists the new state and any pending handover atomically, so a Supervisor restart resumes the same state.

Nothing here starts a process or sends terminal input. Host ownership checks stay in the host's handover handler.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from . import progress_guard as pg

ENV_FLAG = "CREATORSTUDIO_SUPERVISOR_GUARD"
STATE_FILE = "guard_state.json"
HANDOVER_PENDING_S = 600.0


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get(ENV_FLAG) or "").strip() == "1"


@dataclass(frozen=True)
class Signals:
    """What the Supervisor observed this tick. None means not observed."""
    main_alive: Optional[bool]
    session_status: Optional[str]
    eligible_work: Optional[int]
    progress_marker: Optional[str]
    pressure_pct: Optional[float]
    handoff_verified: bool
    session_id: Optional[str] = None   # the Main session observed this tick


class GuardAdapter:
    def __init__(self, state_dir: Path, *, clock: Callable[[], float], request_handover: Callable[[Signals], Dict[str, Any]],
                 event: Callable[..., None], policy: pg.Policy = pg.Policy()) -> None:
        self.path = Path(state_dir) / STATE_FILE
        self.clock = clock
        self.request_handover = request_handover
        self.event = event
        self.policy = policy

    # ---- persistence -------------------------------------------------------------------------------------------------
    def load(self) -> "tuple[pg.GuardState, Optional[Dict[str, Any]]]":
        if not self.path.exists():
            return pg.GuardState(), None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return pg.GuardState.from_dict(None), None          # unreadable: fail closed
        if not isinstance(data, dict):
            return pg.GuardState.from_dict(None), None
        pending = data.get("pending_handover")
        started = pending.get("started") if isinstance(pending, dict) else None
        if not (isinstance(started, (int, float)) and not isinstance(started, bool) and math.isfinite(started)):
            pending = None
        return pg.GuardState.from_dict(data.get("guard")), pending

    def save(self, state: pg.GuardState, pending: Optional[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        body = {"guard": state.to_dict(), "pending_handover": pending}
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(body, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ---- one tick ----------------------------------------------------------------------------------------------------
    def tick(self, signals: Signals) -> Dict[str, Any]:
        state, pending = self.load()
        now = self.clock()
        if pending is not None and now - float(pending["started"]) >= HANDOVER_PENDING_S:
            self.event("guard_handover_expired", cid=pending.get("cid"))
            pending = None
        if (pending is not None and signals.main_alive is True and signals.session_id is not None
                and pending.get("sid") is not None and signals.session_id != pending.get("sid")):
            self.event("guard_handover_completed", cid=pending.get("cid"))   # a different session is Main now
            pending = None
        obs = pg.Observation(now=now, main_alive=signals.main_alive, session_status=signals.session_status,
                             progress_marker=signals.progress_marker, eligible_work=signals.eligible_work,
                             pressure_pct=signals.pressure_pct, handoff_verified=signals.handoff_verified)
        new_state, decision = pg.decide(state, obs, self.policy)
        acted = False
        if decision.action == pg.REPLACE:
            if pending is not None:
                self.event("guard_replace_suppressed", reason="a handover is already pending", label=decision.label)
                new_state = state                        # the decision did not happen: do not spend the budget
            else:
                # Intent is persisted BEFORE the command is sent, so a failure after sending still blocks duplicates.
                pending = {"cid": None, "started": now, "sid": signals.session_id}
                self.save(state, pending)
                sent = self.request_handover(signals)
                if sent.get("sent") is True:
                    pending["cid"] = sent.get("cid")
                    acted = True
                    self.save(new_state, pending)
                    self.event("guard_replace_requested", cid=sent.get("cid"), label=decision.label)
                else:
                    pending = None
                    new_state = state                    # not sent: the budget is not spent
                    self.save(new_state, pending)
                    self.event("guard_replace_failed", reason=str(sent.get("reason", ""))[:200], label=decision.label)
        elif decision.action == pg.ESCALATE:
            self.event("guard_escalation", label=decision.label, reason=decision.reason)
        self.save(new_state, pending)
        return {"action": decision.action, "label": decision.label, "reason": decision.reason, "acted": acted,
                "pending_handover": pending is not None}
