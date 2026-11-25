"""Task selection (T-DEV-075, issue #44): which PLANNED task may be handed to a worker next, and why not the others.

    python -B -m dev_supervisor.task_select [--json] [--record] [--repo ROOT] [--state-dir DIR]

Reads ``ACTIVE_TASKS.json`` and walks the PLANNED tasks in registry order. Every one gets a verdict: selected, or the list
of reasons it is rejected. The first task without a reason is the selection. Nothing else happens: the command never
claims or activates a task, never creates a worktree or a prompt, never queues an assignment, never runs git or gh
write commands and never invents an owned path or an acceptance criterion; it prints the next steps instead.

Reasons for one task (all taken from the registry record and the Supervisor state, none guessed):
  an unmet or unknown dependency; an owned path held by an ACTIVE or UNDER_REVIEW task; a worker already running for it
  (these three come from ``autonomy.compute_items``, unchanged); a record that is not dispatch ready (empty owned_paths or
  acceptance_criteria, a manual test requirement: ``autonomy.dispatch_problems``); no registered ``task/`` branch or
  worktree; no command test requirement (only gate wording: nothing could be run).
Reasons for every task: free memory below the floor or unreadable; no implementation route the allocation accepts; no
independent review path (neither an authoritative Main session whose fresh subagent reviews, nor a review-role route of
capability 3 or more that is not the author's).

``--record`` appends ``task_selected`` or ``task_selection_none`` to the event log and keeps the last selection in the
state document ``selection``; the same selection is not recorded twice. Without ``--record`` nothing is written.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from . import autonomy, dispatch_routing, memory_guard, model_allocation, testreq
from .config import ConfigError
from .state_store import StateStore, StoreError, utc_iso

__all__ = ["select", "main"]

EXIT_OK, EXIT_NONE, EXIT_REFUSED = 0, 1, 2
DOC = "selection"


def _global_reasons(store: Optional[StateStore], state_dir: Optional[Path], free_memory: Callable[[], Optional[int]],
                    floor: int) -> List[str]:
    reasons: List[str] = []
    ok, why = memory_guard.check(free_memory(), floor)
    if not ok:
        reasons.append(why)
    alloc = None
    if state_dir is None:
        reasons.append("no implementation route: the state directory is unknown")
    else:
        reg = dispatch_routing.registry_path(state_dir)
        try:
            alloc = model_allocation.allocate_from_registry(reg, "implementation")
        except model_allocation.AllocationRefused as e:
            reasons.append("no implementation route: " + str(e))
        main = autonomy.authoritative_main(store) if store is not None and store.directory.is_dir() else None
        if main is None:
            if alloc is None:
                reasons.append("no independent review path: no authoritative Main session and no author route to review against")
            else:
                try:
                    model_allocation.allocate_from_registry(reg, "review", author_capability=alloc.capability,
                                                            author_route=(alloc.provider, alloc.model))
                except model_allocation.AllocationRefused as e:
                    reasons.append("no independent review path: no authoritative Main session and " + str(e))
    return reasons


def _task_reasons(task: Dict[str, Any], ready: Dict[str, dict], blocked: Dict[str, str]) -> List[str]:
    tid = str(task.get("id"))
    if tid in blocked:
        return [blocked[tid]]
    reasons: List[str] = []
    item = ready.get(tid)
    if item is None:
        return ["not offered by autonomy next (no start_task item)"]
    reasons += [str(p) for p in item.get("dispatch_problems") or []]
    branch, worktree = task.get("branch"), task.get("worktree")
    if not (isinstance(branch, str) and branch.startswith("task/")):
        reasons.append("no registered task/ branch")
    if not (isinstance(worktree, str) and worktree.strip()):
        reasons.append("no registered worktree")
    try:
        if not any(r.kind == "command" for r in testreq.classify_all(task.get("test_requirements"))):
            reasons.append("no command test requirement: nothing could be run to test the change")
    except testreq.TestReqError:
        pass                                                   # already reported by dispatch_problems
    return reasons


def select(root: Path, store: Optional[StateStore], state_dir: Optional[Path], git: Optional[Callable] = None,
           free_memory: Callable[[], Optional[int]] = memory_guard.free_memory_mb,
           floor: Optional[int] = None) -> Dict[str, Any]:
    """The selection record. Raises ``RuntimeError`` only when the registry cannot be read."""
    try:
        reg = autonomy._load_json(root / "ACTIVE_TASKS.json")
        tasks = [t for t in reg["tasks"] if isinstance(t, dict) and t.get("id")]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError("ACTIVE_TASKS.json is unreadable or malformed") from None
    res = autonomy.compute_items(root, store, git or autonomy.real_git_runner(root))
    ready = {i["task_id"]: i for i in res["items"] if i["kind"] == "start_task"}
    blocked = {b["task_id"]: b["blocker"] for b in res["blocked"] if b.get("id") == f"blocked:{b.get('task_id')}"}
    glob = _global_reasons(store, state_dir, free_memory, memory_guard.min_free_mb() if floor is None else floor)
    verdicts: List[Dict[str, Any]] = []
    selected: Optional[str] = None
    for t in tasks:
        if t.get("status") != "PLANNED":
            continue
        reasons = _task_reasons(t, ready, blocked) + glob
        pick = not reasons and selected is None
        if pick:
            selected = str(t["id"])
        verdicts.append({"task_id": str(t["id"]), "selected": pick, "reasons": reasons})
    return {"selected": selected, "global_reasons": glob, "candidates": len(verdicts), "verdicts": verdicts,
            "next_steps": _next_steps(next((t for t in tasks if t.get("id") == selected), None))}


def _next_steps(task: Optional[Dict[str, Any]]) -> List[str]:
    if task is None:
        return []
    tid, branch, wt = task["id"], task.get("branch"), task.get("worktree")
    return [
        f"activate {tid}: its record is PLANNED; set it ACTIVE with a lease through a registry-only pull request "
        "(dev_supervisor.claim registers new tasks only, so this step is manual today)",
        f"git worktree add -b {branch} {wt} origin/main",
        "write the prompt file from the record's description, owned_paths and acceptance_criteria (nothing else)",
        f"python -B -m dev_supervisor assign --task {tid} --role worker --branch {branch} --worktree {wt} "
        "--owner-stream <stream> --prompt-file <prompt> --allocation-role implementation   (dry run; add --yes after the probe)",
        "after the worker exits 0 the Supervisor runs the task's test requirements (worker_tests); then intake, an independent "
        "review bound to the diff hash, integrate, close-out",
    ]


def fingerprint(rec: Dict[str, Any]) -> str:
    keep = {"selected": rec["selected"], "verdicts": [(v["task_id"], v["selected"], v["reasons"]) for v in rec["verdicts"]]}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def record(store: StateStore, rec: Dict[str, Any], clock: Callable[[], float] = time.time) -> bool:
    """Append the event and write the selection document unless this exact selection was recorded last. True if recorded."""
    fp = fingerprint(rec)
    last = store.read(DOC, default=None)
    if isinstance(last, dict) and last.get("fingerprint") == fp:
        return False
    rejected = [{"task_id": v["task_id"], "reasons": [r[:160] for r in v["reasons"][:3]]} for v in rec["verdicts"] if not v["selected"]]
    kind = "task_selected" if rec["selected"] else "task_selection_none"
    store.append_event(kind, task_id=rec["selected"], candidates=rec["candidates"], rejected=rejected[:20],
                       global_reasons=[r[:160] for r in rec["global_reasons"][:5]])
    store.write(DOC, {"fingerprint": fp, "at": utc_iso(clock()), "selected": rec["selected"], "candidates": rec["candidates"],
                      "verdicts": rec["verdicts"]})
    return True


def main(argv: Optional[Sequence[str]] = None, out: Optional[TextIO] = None, *, git: Optional[Callable] = None,
         free_memory: Callable[[], Optional[int]] = memory_guard.free_memory_mb, clock: Callable[[], float] = time.time,
         env: Optional[Dict[str, str]] = None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="dev_supervisor.task_select", description="Select the next dispatchable PLANNED task.")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", action="store_true", help="append the event and write the selection document")
    a = ap.parse_args(argv)
    try:
        cfg, root, store = autonomy._resolve(a, env, clock)
    except (ConfigError, StoreError, OSError) as e:
        out.write(f"refused: {type(e).__name__}\n")
        return EXIT_REFUSED
    state_dir = store.directory
    try:
        rec = select(root, store, state_dir, git, free_memory)
    except RuntimeError as e:
        out.write(f"refused: {e}\n")
        return EXIT_REFUSED
    recorded = False
    if a.record:
        if not store.directory.is_dir():
            out.write("refused: the state directory does not exist, nothing recorded\n")
            return EXIT_REFUSED
        try:
            recorded = record(store, rec, clock)
        except (StoreError, OSError) as e:
            out.write(f"refused: cannot record ({type(e).__name__})\n")
            return EXIT_REFUSED
    rec["recorded"] = recorded
    if a.json:
        out.write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    else:
        out.write(f"selected: {rec['selected'] or 'none'} ({rec['candidates']} PLANNED candidate(s))\n")
        for r in rec["global_reasons"]:
            out.write(f"  all tasks: {r}\n")
        for v in rec["verdicts"]:
            out.write(f"  {'SELECTED' if v['selected'] else 'rejected'} {v['task_id']}" +
                      ("" if v["selected"] else ": " + "; ".join(v["reasons"])) + "\n")
        for s in rec["next_steps"]:
            out.write(f"  next: {s}\n")
        if a.record:
            out.write("recorded\n" if recorded else "not recorded again: same selection as the last one\n")
    return EXIT_OK if rec["selected"] else EXIT_NONE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
