"""Supervisor state repair: retire stale authoritative Main records (T-DEV-017, issue #68).

Problem (OBSERVED once): the sessions document can hold several records with role ``main``, ``authoritative`` true and
status ACTIVE/RETIRING, for example one left behind by the first supervisor start whose process is long gone.

What it does (IMPLEMENTED, offline-tested with synthetic data):
- ``plan()`` reads the sessions document and ``main.json`` WITHOUT writing anything (no store recovery, no temp-file
  cleanup, no directory creation) and lists the records that may be retired: role main, authoritative, ACTIVE/RETIRING,
  not the session named in ``main.json``, and whose process is not alive. The pid comes from the record itself or from
  the per-process session files (``telemetry.read_sessions``); the liveness probe is injectable. A record with no pid
  evidence at all is reported as "no process found" (not alive) and is still proposed; a live process is never touched.
  A probe that raises is treated as "alive" (never retire on an unanswered question).
- ``apply()`` re-plans on the fresh document and writes it once through ``StateStore`` (atomic, ``.bak`` kept): the record
  gets ``authoritative`` false, ``status`` RETIRED_STALE and a note with reason and time. Records are never deleted,
  ``main.json`` is never written, and a ``state_repaired`` event is appended. It refuses when the supervisor lock is held
  by a live process unless ``allow_while_running`` is given (the sessions document is still the only thing edited).
- Re-running is idempotent: RETIRED_STALE records are no longer candidates.

CLI: ``python -B -m dev_supervisor.repair [--yes] [--allow-while-running] [--state-dir D] [--sessions-dir D] [--json]``.
Dry run is the default. Exit codes: 0 ok / nothing to do, 2 refused or unusable state.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import telemetry
from .state_store import (LOCK_FILE, SchemaError, StateStore, StoreError, _read_bytes, decode_envelope,
                          default_directory, utc_iso)

STALE_STATUS = "RETIRED_STALE"
_CANDIDATE_STATUSES = ("ACTIVE", "RETIRING")
EXIT_OK, EXIT_REFUSED = 0, 2

PidAlive = Callable[[int], bool]


class RepairError(StoreError):
    """The state cannot be repaired safely (damaged document, live lock); nothing was written."""


def short(sid: Any) -> str:
    return str(sid)[:8]


# ------------------------------------------------------------------ reading (no writes)

def _read_doc(directory: Path, name: str) -> Optional[Dict[str, Any]]:
    """Read a stored document without any recovery side effect. None when absent; RepairError when damaged."""
    raw = _read_bytes(directory / f"{name}.json")
    if raw is None:
        return None
    try:
        data = decode_envelope(raw, name)
    except (ValueError, SchemaError) as e:
        raise RepairError(f"{name}.json is damaged or unsupported ({e}); repair refuses to guess "
                          "(run a normal supervisor command to recover it from the backup first)") from None
    if not isinstance(data, dict):
        raise RepairError(f"{name}.json is not an object")
    return data


def _safe_alive(probe: PidAlive, pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    try:
        return bool(probe(pid))
    except Exception:  # noqa: BLE001 - an unanswerable probe is read as "alive": never retire on doubt
        return True


def lock_state(directory: Path, pid_alive: PidAlive) -> Optional[Dict[str, Any]]:
    """The supervisor lock holder ``{pid, owner, alive}`` or None when there is no readable lock."""
    try:
        held = json.loads((Path(directory) / LOCK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(held, dict):
        return None
    pid = held.get("pid")
    alive = isinstance(pid, int) and not isinstance(pid, bool) and pid != os.getpid() and _safe_alive(pid_alive, pid)
    return {"pid": pid, "owner": held.get("owner"), "alive": bool(alive)}


def _session_file_pids(sessions_dir: Optional[Path], pid_alive: PidAlive) -> Dict[str, List[int]]:
    """session id -> pids found in the per-process session files."""
    out: Dict[str, List[int]] = {}
    if sessions_dir is None:
        return out
    try:
        entries = telemetry.read_sessions(sessions_dir, pid_alive=pid_alive)
    except Exception:  # noqa: BLE001
        return out
    for e in entries:
        sid, pid = e.get("session_id"), e.get("pid")
        if sid is None or pid is None or not getattr(sid, "available", False) or not getattr(pid, "available", False):
            continue
        if isinstance(pid.value, int):
            out.setdefault(str(sid.value), []).append(pid.value)
    return out


def _evidence(rec: Dict[str, Any], file_pids: List[int], pid_alive: PidAlive) -> Dict[str, Any]:
    pids: List[int] = []
    rp = rec.get("pid")
    if isinstance(rp, int) and not isinstance(rp, bool):
        pids.append(rp)
    pids.extend(p for p in file_pids if p not in pids)
    if not pids:
        return {"alive": False, "pids": [],
                "note": "no pid recorded and no session file names this session; no process found"}
    live = [p for p in pids if _safe_alive(pid_alive, p)]
    return {"alive": bool(live), "pids": pids,
            "note": f"pid {live[0]} is alive" if live else "recorded process(es) not alive: " + ", ".join(map(str, pids))}


# ------------------------------------------------------------------ plan

def plan(directory: Path, *, sessions_dir: Optional[Path] = None, pid_alive: PidAlive = telemetry.default_pid_alive,
         ) -> Dict[str, Any]:
    """Build the repair plan. Writes nothing. Raises ``RepairError`` for a damaged sessions or main document."""
    directory = Path(directory)
    result: Dict[str, Any] = {"state_dir_exists": directory.is_dir(), "main_session_id": None, "main_status": None,
                              "authoritative": [], "retire": [], "keep": [], "warnings": []}
    if not directory.is_dir():
        result["warnings"].append("state directory does not exist")
        return result
    doc = _read_doc(directory, "sessions")
    main = _read_doc(directory, "main") or {}
    if doc is None:
        result["warnings"].append("no sessions document")
        return result
    sessions = doc.get("sessions")
    if not isinstance(sessions, dict):
        raise RepairError("sessions document has no 'sessions' object")
    main_sid = main.get("session_id") if isinstance(main.get("session_id"), str) else None
    result["main_session_id"], result["main_status"] = main_sid, main.get("status")
    no_main = main_sid is None or main.get("status") != "RUNNING"
    if no_main:
        result["warnings"].append("main.json has no RUNNING session; nothing identifies the current Main")
    file_pids = _session_file_pids(sessions_dir, pid_alive)
    for sid, rec in sessions.items():
        if not isinstance(rec, dict):
            continue
        if not (rec.get("role") == "main" and rec.get("authoritative") is True
                and rec.get("status") in _CANDIDATE_STATUSES):
            continue
        result["authoritative"].append(sid)
        item = {"session_id": sid, "status": rec.get("status"), "updated_at": rec.get("updated_at"),
                "handoff_id": rec.get("handoff_id"), "replaces": rec.get("replaces")}
        if sid == main_sid:
            result["keep"].append({**item, "reason": "is main.json's session"})
            continue
        ev = _evidence(rec, file_pids.get(sid, []), pid_alive)
        if ev["alive"]:
            result["keep"].append({**item, "reason": ev["note"]})
            continue
        result["retire"].append({**item, "reason": ev["note"], "pids": ev["pids"]})
    if no_main and result["retire"] and len(result["retire"]) == len(result["authoritative"]):
        result["warnings"].append("every authoritative Main record would be retired")
    return result


def render(p: Dict[str, Any]) -> str:
    lines = ["supervisor state repair plan"]
    lines.append(f"current Main (main.json): {short(p['main_session_id']) if p['main_session_id'] else 'none'} "
                 f"[{p['main_status']}]")
    lines.append("authoritative Main records: " + (", ".join(short(s) for s in p["authoritative"]) or "none"))
    for k in p["keep"]:
        lines.append(f"  KEEP   {short(k['session_id'])} {k['status']}: {k['reason']}")
    for r in p["retire"]:
        lines.append(f"  RETIRE {short(r['session_id'])} {r['status']} (updated {r['updated_at']}): {r['reason']}"
                     f" -> authoritative false, status {STALE_STATUS}")
    for w in p["warnings"]:
        lines.append(f"  warning: {w}")
    if not p["retire"]:
        lines.append("nothing to retire")
    return "\n".join(lines)


# ------------------------------------------------------------------ apply

def apply(directory: Path, *, sessions_dir: Optional[Path] = None, pid_alive: PidAlive = telemetry.default_pid_alive,
          allow_while_running: bool = False, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Retire the stale records. Returns ``{plan, retired, lock}``. Raises ``RepairError`` and writes nothing when the
    state is damaged or the supervisor lock is held by a live process (unless ``allow_while_running``)."""
    directory = Path(directory)
    lock = lock_state(directory, pid_alive)
    if lock is not None and lock["alive"] and not allow_while_running:
        raise RepairError(f"supervisor lock is held by live pid {lock['pid']}; stop the supervisor or pass "
                          "--allow-while-running (only the sessions document is edited)")
    p = plan(directory, sessions_dir=sessions_dir, pid_alive=pid_alive)
    if not p["retire"]:
        return {"plan": p, "retired": [], "lock": lock}
    store = StateStore(directory, clock=clock, create=False)
    doc = store.read("sessions", default=None)  # fresh read; plan() already proved it is intact
    if not isinstance(doc, dict) or not isinstance(doc.get("sessions"), dict):
        raise RepairError("sessions document changed or vanished since planning")
    now = utc_iso(clock())
    retired = []
    for item in p["retire"]:
        rec = doc["sessions"].get(item["session_id"])
        if not isinstance(rec, dict) or rec.get("status") not in _CANDIDATE_STATUSES or not rec.get("authoritative"):
            continue
        rec["authoritative"] = False
        rec["status"] = STALE_STATUS
        rec["retired_stale"] = {"at": now, "reason": item["reason"], "previous_status": item["status"],
                                "by": "dev_supervisor.repair"}
        rec["updated_at"] = now
        retired.append(item["session_id"])
    if retired:
        store.write("sessions", doc)
        store.append_event("state_repaired", repair="retire_stale_authoritative_main", retired=retired,
                           kept=[k["session_id"] for k in p["keep"]], main_session_id=p["main_session_id"],
                           lock_live=bool(lock and lock["alive"]), allow_while_running=bool(allow_while_running))
    return {"plan": p, "retired": retired, "lock": lock}


# ------------------------------------------------------------------ CLI

def main(argv: Optional[Sequence[str]] = None, *, pid_alive: PidAlive = telemetry.default_pid_alive,
         out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="dev_supervisor.repair", description="Retire stale authoritative Main records.")
    ap.add_argument("--yes", action="store_true", help="apply the plan (default is a dry run)")
    ap.add_argument("--allow-while-running", action="store_true",
                    help="apply even though a live supervisor holds the lock (edits only the sessions document)")
    ap.add_argument("--state-dir", help="state directory (default: the supervisor's default directory)")
    ap.add_argument("--sessions-dir", help="per-process session files (default: ~/.claude/sessions)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    directory = Path(args.state_dir) if args.state_dir else default_directory()
    sdir = Path(args.sessions_dir) if args.sessions_dir else Path.home() / ".claude" / "sessions"
    try:
        if args.yes:
            res = apply(directory, sessions_dir=sdir, pid_alive=pid_alive,
                        allow_while_running=args.allow_while_running)
            p = res["plan"]
        else:
            p, res = plan(directory, sessions_dir=sdir, pid_alive=pid_alive), None
    except StoreError as e:
        print(f"repair refused: {e}", file=out)
        return EXIT_REFUSED
    if args.json:
        print(json.dumps({"applied": bool(args.yes), "plan": p, "retired": res["retired"] if res else []},
                         indent=2, default=str), file=out)
    else:
        print(render(p), file=out)
        if res is not None:
            print("retired: " + (", ".join(short(s) for s in res["retired"]) or "none"), file=out)
        else:
            print("dry run (nothing written): re-run with --yes to apply", file=out)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
