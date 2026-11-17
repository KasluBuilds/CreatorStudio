"""Orbit observability snapshot (T-DEV-007, issue #44 part F): ONE read-only JSON for the dashboard.

It combines the L1 telemetry snapshot (sessions, context pressure, account usage) with the L2 worker registry and
event log. It never writes to the supervisor state, the repository or GitHub. The only file it can write is the
snapshot JSON itself, at a path the caller gives (`write_snapshot`).

Every value is a field `{label, value, timestamp, source, freshness, age_seconds[, reason]}`:
- label: OBSERVED / REPORTED / INFERRED / UNAVAILABLE (from `evidence.Label`; UNAVAILABLE never holds a value).
- freshness: `live` (read now from its source and recent), `cached` (recovered from a persisted record, or older
  than `live_after_seconds`), `stale` (older than `stale_after_seconds` or no usable timestamp), `offline`
  (UNAVAILABLE: no source answered). Stale and offline are first-class: a missing source is never an empty success.
- Account usage is exposed only when every usage field is labelled OBSERVED. Anything else (including a REPORTED
  statusline payload) becomes UNAVAILABLE with the reason.
No prompts, transcript text or file contents are ever included: only identifiers, counts, labels and timestamps.
Stdlib only. No network.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .evidence import Evidence, Label, now_iso, parse_ts, unavailable

SCHEMA_VERSION = 1
PRODUCT = "Orbit"
LIVE_AFTER_SECONDS = 120.0
STALE_AFTER_SECONDS = 900.0
FRESHNESS_ORDER = ("live", "cached", "stale", "offline")      # best to worst
MAX_EVENTS = 20

_FAILURE_KINDS = ("worker_failed", "worker_blocked", "worker_lost", "assign_refused_duplicate",
                  "assign_refused_worktree", "result_contradiction", "restart_refused_backoff")
_RETRY_KINDS = ("worker_restart",)


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def field(ev: Any, *, now: Optional[datetime] = None, cached: bool = False,
          live_after: float = LIVE_AFTER_SECONDS, stale_after: float = STALE_AFTER_SECONDS) -> Dict[str, Any]:
    """Turn an Evidence (or an already-serialised evidence dict) into a snapshot field with freshness and age."""
    if isinstance(ev, Evidence):
        ev = ev.to_dict()
    label = ev.get("label", "UNAVAILABLE")
    out = {"label": label, "value": ev.get("value"), "timestamp": ev.get("timestamp", ""),
           "source": ev.get("source", ""), "age_seconds": None}
    if ev.get("reason"):
        out["reason"] = ev["reason"]
    if label == Label.UNAVAILABLE.value:
        out["value"] = None
        out["freshness"] = "offline"
        out.setdefault("reason", "no source")
        return out
    t = parse_ts(out["timestamp"])
    if t is None:
        out["freshness"] = "stale"
        out["reason"] = out.get("reason") or "no usable timestamp"
        return out
    age = max(0.0, (_now(now) - t).total_seconds())
    out["age_seconds"] = round(age, 1)
    out["freshness"] = "stale" if age > stale_after else ("cached" if cached or age > live_after else "live")
    return out


def _unavail(reason: str, source: str, now: Optional[datetime], **kw) -> Dict[str, Any]:
    return field(unavailable(reason, source, _now(now).isoformat()), now=now, **kw)


def _worst(fields: Iterable[Dict[str, Any]]) -> str:
    """Overall freshness: `offline` only when no value at all is available; otherwise the worst of the available
    values (a single unavailable value, such as a missing PR, is shown on its own row and downgrades `live` to `cached`)."""
    fr = [f["freshness"] for f in fields if isinstance(f, dict) and "freshness" in f]
    avail = [x for x in fr if x != "offline"]
    if not avail:
        return "offline"
    worst = max(FRESHNESS_ORDER.index(x) for x in avail)
    if len(avail) < len(fr):
        worst = max(worst, FRESHNESS_ORDER.index("cached"))
    return FRESHNESS_ORDER[worst]


def _walk_fields(x: Any):
    if isinstance(x, dict):
        if "freshness" in x and "label" in x:
            yield x
        else:
            for v in x.values():
                yield from _walk_fields(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_fields(v)


def _ev(label: Label, value, source, ts) -> Evidence:
    return Evidence(label, value, ts, source)


# ---------------------------------------------------------------------------------------------- sections

def _account_usage(tele: Optional[Mapping], now, kw) -> Dict[str, Any]:
    src = (tele or {}).get("account_usage") if tele else None
    if not src:
        return {"provider": "none", "usage": _unavail("telemetry snapshot has no account usage", "telemetry", now, **kw)}
    usage = src.get("usage") or {}
    prov = src.get("provider", "none")
    if "label" in usage:                     # a single evidence: shown only when UNAVAILABLE or OBSERVED
        if usage["label"] in ("UNAVAILABLE", Label.OBSERVED.value):
            return {"provider": prov, "usage": field(usage, now=now, **kw)}
        return {"provider": prov, "usage": _unavail("usage is not OBSERVED (labelled %s)" % usage["label"],
                                                    "account_usage:" + str(prov), now, **kw)}
    vals = {k: v for k, v in usage.items() if isinstance(v, dict)}
    not_observed = sorted(k for k, v in vals.items() if v.get("label") != Label.OBSERVED.value)
    if not vals or not_observed:
        why = ("no usage values" if not vals else
               "usage is not OBSERVED (labelled %s); only OBSERVED account usage is shown"
               % "/".join(sorted({vals[k].get("label", "?") for k in not_observed})))
        return {"provider": prov, "usage": _unavail(why, "account_usage:" + str(prov), now, **kw)}
    return {"provider": prov, "usage": {k: field(v, now=now, **kw) for k, v in vals.items()}}


def _context(sess: Mapping, now, kw) -> Dict[str, Any]:
    ctx = sess.get("context") or {}
    return {k: field(ctx[k], now=now, **kw) if k in ctx else _unavail("not measured", "telemetry", now, **kw)
            for k in ("tokens", "model", "window", "ratio", "level")}


def _pick_main(sessions: List[Mapping], main_session_id: Optional[str]) -> Optional[Mapping]:
    good = [s for s in sessions if "error" not in s]
    for s in good:
        sid = s.get("session_id") or {}
        if main_session_id and sid.get("value") == main_session_id:
            return s
    if main_session_id:
        return None
    alive = [s for s in good if (s.get("alive") or {}).get("value") is True] or good
    alive.sort(key=lambda s: str((s.get("updated_at") or {}).get("timestamp") or ""), reverse=True)
    return alive[0] if alive else None


def _main(tele: Optional[Mapping], main_session_id, now, kw) -> Dict[str, Any]:
    if tele is None:
        return {"present": False, "reason": "no telemetry source",
                "status": _unavail("no telemetry source", "telemetry", now, **kw)}
    s = _pick_main(list(tele.get("sessions") or []), main_session_id)
    if s is None:
        return {"present": False, "reason": "no matching Claude session file",
                "status": _unavail("no matching Claude session file", "telemetry", now, **kw)}
    out = {"present": True, "selected_by": "session_id" if main_session_id else "most recent alive session"}
    for k in ("session_id", "status", "alive", "updated_at", "stale", "started_at"):
        out[k] = field(s[k], now=now, **kw) if k in s else _unavail("%s missing" % k, "telemetry", now, **kw)
    out["context_pressure"] = _context(s, now, kw)
    model = out["context_pressure"]["model"]
    out["model"] = model
    return out


def _workers(registry, now, kw) -> List[Dict[str, Any]]:
    out = []
    for w in registry.list():
        d = w.to_dict()
        ts = d.get("last_heartbeat") or d.get("started_at") or d.get("assigned_at") or ""
        src = "registry:" + d["worker_id"]
        rep = lambda v: field(_ev(Label.REPORTED, v, src, ts), now=now, cached=True, **kw) if v not in (None, "") else \
            _unavail("not recorded", src, now, cached=True, **kw)
        out.append({
            "worker_id": d["worker_id"], "role": d["role"], "task_id": d["task_id"],
            "status": field(_ev(Label.OBSERVED, d["status"], src, ts), now=now, cached=True, **kw) if ts else
            _unavail("no timestamp recorded", src, now, cached=True, **kw),
            "model": rep(d.get("model")), "provider": rep(d.get("provider")), "branch": rep(d.get("branch")),
            "restart_count": d["restart_count"], "failure_count": len(d.get("failures") or []),
            "last_failure": (d.get("failures") or [None])[-1], "exit_code": d.get("exit_code"),
            "next_restart_not_before": d.get("next_restart_not_before"),
            "last_heartbeat": rep(d.get("last_heartbeat")), "note": d.get("note", ""),
        })
    return out


def _tasks(registry, now, kw) -> List[Dict[str, Any]]:
    tasks: Dict[str, Dict[str, Any]] = {}
    for w in registry.list():
        t = tasks.setdefault(w.task_id, {"task_id": w.task_id, "workers": [], "results": []})
        t["workers"].append(w.worker_id)
    for r in registry.results.values():
        t = tasks.setdefault(r.task_id, {"task_id": r.task_id, "workers": [], "results": []})
        t["results"].append({"result_id": r.result_id, "outcome": r.outcome, "status": r.status,
                             "contradiction": bool(r.contradiction), "ref": r.ref})
    return [tasks[k] for k in sorted(tasks)]


def _events(events: List[Mapping], kinds, now, kw) -> List[Dict[str, Any]]:
    out = []
    for e in events:
        if e.get("kind") in kinds:
            d = e.get("data") or {}
            out.append({"kind": e["kind"], "worker_id": d.get("worker_id"), "task_id": d.get("task_id"),
                        "detail": field(_ev(Label.OBSERVED, d.get("signature") or d.get("reason") or e["kind"],
                                            "event_log", e.get("ts", "")), now=now, cached=True, **kw)})
    return out[-MAX_EVENTS:]


def _checkpoint(events: List[Mapping], explicit, now, kw) -> Dict[str, Any]:
    """Last durable GitHub checkpoint: an explicit value from the caller, else the newest checkpoint event."""
    if explicit is not None:
        return field(explicit, now=now, cached=True, **kw)
    for e in reversed(events):
        if e.get("kind") in ("github_checkpoint", "github_log_posted"):
            d = e.get("data") or {}
            val = {k: d.get(k) for k in ("issue", "event_id", "kind") if d.get(k) is not None} or {"event": e["kind"]}
            return field(_ev(Label.REPORTED, val, "event_log", e.get("ts", "")), now=now, cached=True, **kw)
    return _unavail("no GitHub checkpoint recorded", "event_log", now, cached=True, **kw)


def _next_actions(workers: List[Dict[str, Any]], blockers: List[Mapping], contradictions: List[Mapping]) -> List[str]:
    acts = []
    for b in blockers:
        acts.append("Resolve blocker: %s" % (b.get("reason") or b.get("worker_id") or "unspecified"))
    for w in workers:
        st = w["status"].get("value")
        if st == "BLOCKED":
            acts.append("Human decision needed for worker %s (task %s)" % (w["worker_id"], w["task_id"]))
        elif st == "STALE":
            acts.append("Check worker %s: no recent heartbeat" % w["worker_id"])
        elif st in ("FAILED", "LOST"):
            acts.append("Review %s worker %s before any restart" % (st, w["worker_id"]))
    if contradictions:
        acts.append("Inspect %d reported-result contradiction(s)" % len(contradictions))
    return acts


# ---------------------------------------------------------------------------------------------- build

def build_snapshot(telemetry: Optional[Mapping] = None, registry=None, events: Optional[List[Mapping]] = None,
                   *, main_session_id: Optional[str] = None, branch_pr: Optional[Mapping] = None,
                   checkpoint: Optional[Evidence] = None, now: Optional[datetime] = None,
                   live_after_seconds: float = LIVE_AFTER_SECONDS,
                   stale_after_seconds: float = STALE_AFTER_SECONDS) -> Dict[str, Any]:
    """Build the Orbit snapshot. Pure: reads its arguments only. `telemetry` is the dict from
    `dev_supervisor.telemetry.snapshot()`; `registry` a `WorkerRegistry`; `events` the supervisor event list;
    `branch_pr` an optional {"branch": Evidence, "pr": Evidence} supplied by a caller that really observed them."""
    kw = {"live_after": live_after_seconds, "stale_after": stale_after_seconds}
    n = _now(now)
    events = list(events or [])
    snap: Dict[str, Any] = {"schema_version": SCHEMA_VERSION, "product": PRODUCT, "read_only": True,
                            "generated_at": n.isoformat(),
                            "thresholds": {"live_after_seconds": live_after_seconds,
                                           "stale_after_seconds": stale_after_seconds}}
    snap["main"] = _main(telemetry, main_session_id, n, kw)
    snap["account_usage"] = _account_usage(telemetry, n, kw)
    if registry is None:
        why = "no supervisor registry available"
        snap.update({"registry": {"present": False, "reason": why}, "workers": [], "tasks": [], "blockers": [],
                     "failures": [], "retries": []})
        snap["model_provider"] = {"model": snap["main"].get("model") or _unavail(why, "registry", n, **kw),
                                  "providers": []}
        snap["next_actions"] = [why + ": start or point Orbit at the supervisor state"]
    else:
        workers = _workers(registry, n, kw)
        snap["registry"] = {"present": True, "worker_count": len(workers)}
        snap["workers"] = workers
        snap["tasks"] = _tasks(registry, n, kw)
        snap["blockers"] = [dict(b) for b in registry.blockers]
        snap["failures"] = _events(events, _FAILURE_KINDS, n, kw)
        snap["retries"] = _events(events, _RETRY_KINDS, n, kw)
        snap["model_provider"] = {"model": snap["main"].get("model") or _unavail("no main session", "telemetry", n, **kw),
                                  "providers": sorted({(w["provider"].get("value"), w["model"].get("value"))
                                                       for w in workers if w["provider"].get("value")},
                                                      key=lambda p: (p[0], p[1] or ""))}
        snap["model_provider"]["providers"] = [{"provider": p, "model": m} for p, m in snap["model_provider"]["providers"]]
        snap["next_actions"] = _next_actions(workers, registry.blockers, registry.contradictions)
    bp = branch_pr or {}
    snap["branch"] = field(bp["branch"], now=n, **kw) if bp.get("branch") is not None else \
        _unavail("branch not supplied (workers list their own branches)", "git", n, **kw)
    snap["pull_request"] = field(bp["pr"], now=n, **kw) if bp.get("pr") is not None else \
        _unavail("pull request not observed", "github", n, **kw)
    snap["last_github_checkpoint"] = _checkpoint(events, checkpoint, n, kw)
    snap["overall_freshness"] = _worst(_walk_fields(snap))
    return json.loads(json.dumps(snap, default=str))


def load_inputs(directory=None, sessions_dir=None, projects_dir=None, now=None):
    """Read-only convenience: (telemetry, registry, events) from the real supervisor store and session files.
    A missing store yields registry None, so the snapshot says so instead of looking healthy."""
    from . import telemetry as tl
    from .state_store import StateStore
    from .workers import WorkerRegistry
    tele = tl.snapshot(sessions_dir, projects_dir, now=now) if sessions_dir else None
    reg, events = None, []
    try:
        store = StateStore(directory, create=False)
        if store.directory.is_dir():
            reg = WorkerRegistry(store)
            events, _bad = store.read_events()
    except Exception:
        reg = None
    return tele, reg, events


def write_snapshot(snap: Mapping, path) -> None:
    """Atomically write the snapshot JSON to `path` (temp file + replace). This is the ONLY write in the module."""
    path = os.path.abspath(str(path))
    fd, tmp = tempfile.mkstemp(prefix=".orbit-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write the read-only Orbit snapshot JSON.")
    ap.add_argument("--out", required=True, help="where to write the snapshot JSON")
    ap.add_argument("--state-dir", default=None, help="supervisor state directory (read only)")
    ap.add_argument("--sessions-dir", default=None, help="Claude session files directory (read only)")
    ap.add_argument("--projects-dir", default=None, help="Claude transcripts directory (read only)")
    a = ap.parse_args(argv)
    tele, reg, events = load_inputs(a.state_dir, a.sessions_dir, a.projects_dir)
    snap = build_snapshot(tele, reg, events)
    write_snapshot(snap, a.out)
    print("snapshot written; overall freshness: %s" % snap["overall_freshness"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
