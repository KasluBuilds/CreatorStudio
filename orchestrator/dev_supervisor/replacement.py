"""Replace a session (Main or worker) safely (T-DEV-003, issue #44 part C).

IMPORTANT: a fresh session does NOT replenish account usage. Replacement only resets the per-session context; plan
limits and reset windows are unchanged. Every ``ReplacementResult`` carries this note.

Procedure (``replace_session``), IMPLEMENTED and tested offline with a fake launcher, fake heartbeat probe and fake
retirer; the real launch path (L2 ``ClaudeCliLauncher``) is NOT TESTED end to end:

1. Gate: the caller must hand over a ``VerifiedHandoff`` issued by ``handoff.verify_handoff`` within
   ``policy.handoff_max_age_s`` for the session being replaced. Anything else raises ``UnverifiedHandoffError`` before
   any state changes: no launch, nothing retired, nothing cleared.
2. Stop rule: after ``policy.failure_stop_limit`` failed replacements of the same session in a row the procedure refuses
   (``BLOCKED``) until a human calls ``reset_failures``.
3. Re-verify on disk (schema, hash equal to the verified hash, every branch/worktree/commit/PR through the checkers).
   ``NOT_SAFE`` -> refused, the old session is untouched.
4. Mark the old session ``RETIRING`` (it stays authoritative), write a prompt file that only points to the verified
   handoff (path and hash, no transcript), launch a fresh session through the injected L2 ``ProcessLauncher``.
5. Wait for the new session's first heartbeat (bounded). A launch error, early exit or timeout fails the attempt; an
   owned new process is terminated. Bounded attempts (``policy.max_attempts``).
6. Confirmed: the new session becomes authoritative. The handoff is verified once more, then the injected retirer
   retires (clears) the old session. If that last check or the retirer fails, the old session is NOT cleared and the
   result says so.
7. All attempts failed: rollback. The old session goes back to ``ACTIVE`` and stays authoritative.

Session records live in the state store document ``sessions``; every step is an event in the store event log, and every
intent is persisted before it is acted on.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

try:
    from .handoff import (SAFE, UnverifiedHandoffError, VerifiedHandoff, handoff_dir, require_verified,
                          verify_handoff)
    from .lifecycle import LaunchSpec, ProcessLauncher, build_claude_argv
    from .state_store import StateStore, StoreError, utc_iso
except ImportError:  # pragma: no cover - standalone import
    from handoff import (SAFE, UnverifiedHandoffError, VerifiedHandoff, handoff_dir, require_verified,  # type: ignore
                         verify_handoff)
    from lifecycle import LaunchSpec, ProcessLauncher, build_claude_argv  # type: ignore
    from state_store import StateStore, StoreError, utc_iso  # type: ignore

__all__ = ["USAGE_NOTE", "SessionStatus", "ReplacementStatus", "ReplacementPolicy", "ReplacementResult",
           "HeartbeatProbe", "SessionRetirer", "replace_session", "register_session", "load_sessions",
           "reset_failures", "UnverifiedHandoffError", "ReplacementError"]

USAGE_NOTE = ("A fresh session does NOT replenish account usage: plan limits and reset windows are unchanged; only the "
              "per-session context starts empty.")
SESSIONS_DOC = "sessions"


class ReplacementError(StoreError):
    pass


class SessionStatus:
    ACTIVE = "ACTIVE"
    RETIRING = "RETIRING"          # replacement in progress; still authoritative
    RETIRED = "RETIRED"            # cleared after the new session was confirmed
    STARTING = "STARTING"
    FAILED = "FAILED"
    EXITED = "EXITED"              # process ended without a replacement; never authoritative
    RETIRE_FAILED = "RETIRE_FAILED"  # new session confirmed, old one could not be cleared: stop it by hand


class ReplacementStatus:
    REPLACED = "REPLACED"
    REPLACED_OLD_NOT_CLEARED = "REPLACED_OLD_NOT_CLEARED"
    ROLLED_BACK = "ROLLED_BACK"
    REFUSED_HANDOFF_NOT_SAFE = "REFUSED_HANDOFF_NOT_SAFE"
    BLOCKED = "BLOCKED"


@dataclass
class ReplacementPolicy:
    max_attempts: int = 2              # launches per replacement
    heartbeat_timeout_s: float = 300.0
    heartbeat_poll_s: float = 5.0
    handoff_max_age_s: float = 300.0
    failure_stop_limit: int = 3        # failed replacements in a row for one session -> BLOCKED


class HeartbeatProbe(Protocol):
    def first_heartbeat(self, session_id: str, pid: int) -> Optional[float]: ...  # timestamp, or None if none yet


class SessionRetirer(Protocol):
    def retire(self, session_id: str) -> Tuple[bool, str]: ...  # clears/ends the old session


@dataclass
class ReplacementResult:
    status: str
    old_session_id: str
    authoritative_session_id: str
    new_session_id: Optional[str] = None
    handoff_id: Optional[str] = None
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    old_session_cleared: bool = False
    usage_note: str = USAGE_NOTE

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "old_session_id": self.old_session_id,
                "authoritative_session_id": self.authoritative_session_id, "new_session_id": self.new_session_id,
                "handoff_id": self.handoff_id, "attempts": list(self.attempts), "reasons": list(self.reasons),
                "old_session_cleared": self.old_session_cleared, "usage_note": self.usage_note}


# ------------------------------------------------------------------ session records

def load_sessions(store: StateStore) -> Dict[str, Any]:
    doc = store.read(SESSIONS_DOC, default=None) or {}
    doc.setdefault("sessions", {})
    doc.setdefault("failures", {})
    return doc


def _save(store: StateStore, doc: Dict[str, Any]) -> None:
    store.write(SESSIONS_DOC, doc)


def _set(store: StateStore, doc: Dict[str, Any], sid: str, **fields: Any) -> None:
    rec = doc["sessions"].setdefault(sid, {"session_id": sid})
    rec.update(fields)
    rec["updated_at"] = utc_iso(store.clock())
    _save(store, doc)


def register_session(store: StateStore, session_id: str, role: str) -> Dict[str, Any]:
    """Record a running session as ACTIVE and authoritative (for example the current Main)."""
    doc = load_sessions(store)
    if role == "main":                   # only one Main is authoritative
        for sid, rec in doc["sessions"].items():
            if sid != session_id and rec.get("role") == "main" and rec.get("authoritative"):
                rec["authoritative"] = False
                rec["status"] = SessionStatus.EXITED
    _set(store, doc, session_id, role=role, status=SessionStatus.ACTIVE, authoritative=True)
    store.append_event("session_registered", session_id=session_id, role=role)
    return doc["sessions"][session_id]


def mark_exited(store: StateStore, session_id: str) -> bool:
    """A session whose process ended: status EXITED, non-authoritative. False if it is not recorded."""
    doc = load_sessions(store)
    if session_id not in doc["sessions"]:
        return False
    _set(store, doc, session_id, status=SessionStatus.EXITED, authoritative=False)
    store.append_event("session_exited", session_id=session_id)
    return True


def reset_failures(store: StateStore, session_id: str, reason: str) -> None:
    """A human decision: clear the consecutive-failure counter so replacement may be tried again."""
    if not str(reason).strip():
        raise ReplacementError("a reason is required")
    doc = load_sessions(store)
    doc["failures"].pop(session_id, None)
    _save(store, doc)
    store.append_event("replacement_failures_reset", session_id=session_id, reason=str(reason)[:200])


# ------------------------------------------------------------------ the procedure

def _write_prompt(store: StateStore, verified: VerifiedHandoff, role: str) -> Path:
    p = handoff_dir(store) / f"{verified.handoff_id}.resume.prompt.txt"
    text = (f"You are a fresh {role} session replacing session {verified.session_id}.\n"
            f"Before doing anything, read the verified handoff JSON at {verified.json_path}\n"
            f"(human copy: {verified.md_path}) and confirm its content hash is {verified.content_hash}.\n"
            "If the file is missing or the hash differs, stop and report; do not continue the work.\n"
            "Note: this fresh session does NOT replenish account usage.\n")
    store.atomic_write_bytes(p, text.encode("utf-8"))
    return p


def replace_session(store: StateStore, launcher: ProcessLauncher, *, old_session_id: str, role: str, model: str,
                    cwd: str, verified: VerifiedHandoff, checkers: Any, heartbeat: HeartbeatProbe,
                    retirer: SessionRetirer, clock: Callable[[], float] = time.time,
                    sleep: Callable[[float], None] = time.sleep, policy: Optional[ReplacementPolicy] = None,
                    session_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
                    executable: str = "claude") -> ReplacementResult:
    """Replace ``old_session_id`` with a fresh session. See the module docstring for the full procedure.

    Raises ``UnverifiedHandoffError`` (before touching anything) when ``verified`` is not a fresh, genuine
    ``VerifiedHandoff`` for ``old_session_id``.
    """
    pol = policy or ReplacementPolicy()
    # 1. structural gate: nothing below runs without a genuine, fresh verified handoff for this session
    v = require_verified(verified, now=float(clock()), max_age_s=pol.handoff_max_age_s)
    if v.session_id != old_session_id:
        raise UnverifiedHandoffError(f"the verified handoff belongs to session {v.session_id}, not {old_session_id}")
    res = ReplacementResult(status=ReplacementStatus.ROLLED_BACK, old_session_id=old_session_id,
                            authoritative_session_id=old_session_id, handoff_id=v.handoff_id)
    doc = load_sessions(store)

    # 2. repeated failure stops
    fails = int(doc["failures"].get(old_session_id, 0))
    if fails >= pol.failure_stop_limit:
        res.status = ReplacementStatus.BLOCKED
        res.reasons.append(f"{fails} failed replacements in a row (limit {pol.failure_stop_limit}); "
                           "a human must reset_failures first")
        store.append_event("replacement_blocked", old_session_id=old_session_id, failures=fails)
        return res

    # 3. re-verify on disk right now
    rep = verify_handoff(store, v.handoff_id, checkers, expected_hash=v.content_hash)
    if rep.status != SAFE:
        res.status = ReplacementStatus.REFUSED_HANDOFF_NOT_SAFE
        res.reasons.extend(rep.reasons)
        store.append_event("replacement_refused", old_session_id=old_session_id, handoff_id=v.handoff_id,
                           reasons=rep.reasons[:20])
        return res

    # 4. old session RETIRING (still authoritative), prompt points to the handoff
    _set(store, doc, old_session_id, role=role, status=SessionStatus.RETIRING, authoritative=True,
         handoff_id=v.handoff_id)
    store.append_event("session_retiring", session_id=old_session_id, handoff_id=v.handoff_id)
    prompt = _write_prompt(store, v, role)
    log_dir = store.directory / "logs" / "replacements"
    os.makedirs(str(log_dir), exist_ok=True)

    confirmed: Optional[str] = None
    for attempt in range(1, max(1, int(pol.max_attempts)) + 1):
        new_sid = str(uuid.UUID(str(session_factory())))
        info: Dict[str, Any] = {"attempt": attempt, "session_id": new_sid}
        res.attempts.append(info)
        _set(store, doc, new_sid, role=role, status=SessionStatus.STARTING, authoritative=False,
             replaces=old_session_id, handoff_id=v.handoff_id)
        store.append_event("replacement_launch_begin", old_session_id=old_session_id, new_session_id=new_sid,
                           attempt=attempt)
        ok, why, pid = _launch_and_wait(launcher, pol, clock, sleep, heartbeat, new_sid, model, executable, cwd,
                                        prompt, log_dir)
        info.update(ok=ok, reason=why, pid=pid)
        if ok:
            confirmed = new_sid
            break
        _set(store, doc, new_sid, status=SessionStatus.FAILED, failure=why)
        store.append_event("replacement_attempt_failed", new_session_id=new_sid, attempt=attempt, reason=why)

    if confirmed is None:
        # 7. rollback: the old session stays authoritative and active
        doc["failures"][old_session_id] = fails + 1
        _set(store, doc, old_session_id, status=SessionStatus.ACTIVE, authoritative=True)
        res.status = ReplacementStatus.ROLLED_BACK
        res.reasons.append(f"no new session confirmed after {len(res.attempts)} attempt(s); old session kept")
        store.append_event("replacement_rolled_back", old_session_id=old_session_id,
                           failures=doc["failures"][old_session_id])
        if doc["failures"][old_session_id] >= pol.failure_stop_limit:
            store.append_event("replacement_blocked", old_session_id=old_session_id,
                               failures=doc["failures"][old_session_id])
        return res

    # 6. confirmed: authority moves, then (and only then) the old session may be cleared
    res.new_session_id = res.authoritative_session_id = confirmed
    doc["failures"].pop(old_session_id, None)
    _set(store, doc, confirmed, status=SessionStatus.ACTIVE, authoritative=True)
    _set(store, doc, old_session_id, authoritative=False, replaced_by=confirmed)
    store.append_event("replacement_confirmed", old_session_id=old_session_id, new_session_id=confirmed)
    final = verify_handoff(store, v.handoff_id, checkers, expected_hash=v.content_hash)
    back = load_sessions(store)["sessions"]        # read back: the new session must be recorded authoritative first
    new_rec, old_rec = back.get(confirmed) or {}, back.get(old_session_id) or {}
    if final.status != SAFE:
        cleared, detail = False, ("handoff no longer verifies; old session not retired, new session is authoritative: "
                                  + "; ".join(final.reasons[:5]))
    elif not (new_rec.get("authoritative") is True and new_rec.get("status") == SessionStatus.ACTIVE
              and old_rec.get("authoritative") is not True):
        cleared, detail = False, "new session is not recorded as the authoritative ACTIVE session; old session not retired"
    else:
        try:
            cleared, detail = retirer.retire(old_session_id)
            cleared = cleared is True
        except Exception as e:  # noqa: BLE001
            cleared, detail = False, f"retirer failed ({e.__class__.__name__})"
    res.old_session_cleared = cleared
    if cleared:
        _set(store, doc, old_session_id, status=SessionStatus.RETIRED)
        res.status = ReplacementStatus.REPLACED
        store.append_event("session_retired", session_id=old_session_id, replaced_by=confirmed)
    else:
        _set(store, doc, old_session_id, status=SessionStatus.RETIRE_FAILED, failure=str(detail)[:200])
        res.status = ReplacementStatus.REPLACED_OLD_NOT_CLEARED
        res.reasons.append(f"old session not cleared ({str(detail)[:200]}); stop it by hand")
        store.append_event("session_retire_failed", session_id=old_session_id, reason=str(detail)[:200])
    return res


def _launch_and_wait(launcher: ProcessLauncher, pol: ReplacementPolicy, clock: Callable[[], float],
                     sleep: Callable[[float], None], heartbeat: HeartbeatProbe, sid: str, model: str,
                     executable: str, cwd: str, prompt: Path, log_dir: Path) -> Tuple[bool, str, Optional[int]]:
    try:
        argv = build_claude_argv(model, sid, executable=executable)
        spec = LaunchSpec(argv=argv, cwd=cwd, stdin_path=str(prompt),
                          stdout_path=str(log_dir / f"{sid}.stream.jsonl"),
                          stderr_path=str(log_dir / f"{sid}.stderr.log"), max_runtime_s=4 * 3600.0)
        pid = int(launcher.launch(spec).pid)
    except Exception as e:  # noqa: BLE001
        return False, f"launch_error:{e.__class__.__name__}", None
    deadline = float(clock()) + pol.heartbeat_timeout_s
    while True:
        try:
            code = launcher.poll(pid)
        except Exception as e:  # noqa: BLE001
            code, why = -1, f"poll_error:{e.__class__.__name__}"
        else:
            why = f"exited:{code}"
        if code is not None:
            if why.startswith("poll_error"):
                _stop(launcher, pid)
            return False, why, pid
        try:
            hb = heartbeat.first_heartbeat(sid, pid)
        except Exception:  # noqa: BLE001 - an unreadable heartbeat is not a heartbeat
            hb = None
        if hb is not None:
            return True, "first heartbeat seen", pid
        if float(clock()) >= deadline:
            _stop(launcher, pid)
            return False, "heartbeat_timeout", pid
        sleep(pol.heartbeat_poll_s)


def _stop(launcher: ProcessLauncher, pid: int) -> None:
    try:
        if launcher.owns(pid):
            launcher.terminate(pid)
            launcher.kill(pid)
    except Exception:  # noqa: BLE001 - best effort; the attempt is already recorded as failed
        pass
