"""Autonomy loop for Main: what to do next, wake-up wait and the Claude Code Stop hook (T-DEV-013, issue #44).

Commands (``python -B -m dev_supervisor.autonomy <cmd>``; standard library only)::

    next [--json] [--repo ROOT]       actionable items for Main, ordered by priority, each with a reason
    wait --timeout S [--poll S] [--cursor FILE]
                                      block until the actionable set or the supervisor event log changes
                                      (exit 0 = change, exit 3 = timeout)
    stop-hook                         Claude Code Stop hook: JSON on stdin; prints {"decision":"block",...} or nothing
    main-settings --out FILE          write a Claude Code settings file holding only a hooks.Stop entry

Rules (IMPLEMENTED, tested offline with synthetic data):
- Never invents work: every item comes from a repository record (ACTIVE_TASKS.json, DEVELOPMENT_TRACKER.json,
  waves/*/wave.json plus remote branch state) or from the supervisor worker registry. Repository files win over the
  supervisor cache: a task whose registry status is not PLANNED/ACTIVE is never offered, whatever a worker claims.
- ``next`` and ``wait`` only read. The Stop hook writes one small document (``autonomy.json``: fingerprint of what Main
  was last told and the consecutive-block counter) and only when the state directory already exists.
- The Stop hook fails OPEN: any error allows the stop (no output, exit 0) and is logged to the event log when possible.
  Reasons are built from item ids and fixed text only: no prompt, transcript text or secret can enter them.
- Git use is limited to read-only plumbing through an injectable runner (``git rev-parse``, ``merge-base``,
  ``ls-tree``); remote refs are as of the last fetch (the module never fetches).

Known limit: a wave merged into main by squash is not an ancestor of origin/main, so it can be listed as
``verify_wave`` until its remote wave branch is deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

try:  # package import (python -m dev_supervisor.autonomy) or run as a script (the generated hook command)
    from . import config as config_mod
    from .config import Config, ConfigError
    from .evidence import Label
    from . import testreq
    from .state_store import StateStore, StoreError, find_git_root, utc_iso, scrub
    from .workers import WorkerRegistry, WorkerStatus
except ImportError:  # pragma: no cover - script mode
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from dev_supervisor import config as config_mod  # type: ignore
    from dev_supervisor.config import Config, ConfigError  # type: ignore
    from dev_supervisor.evidence import Label  # type: ignore
    from dev_supervisor import testreq  # type: ignore
    from dev_supervisor.state_store import StateStore, StoreError, find_git_root, utc_iso, scrub  # type: ignore
    from dev_supervisor.workers import WorkerRegistry, WorkerStatus  # type: ignore

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_TIMEOUT = 0, 1, 2, 3
AUTONOMY_DOC = "autonomy"
ENV_MAX_BLOCKS = "CREATORSTUDIO_AUTONOMY_MAX_BLOCKS"
DEFAULT_MAX_BLOCKS = 3
DEFAULT_OWNER = "claude:repository-coordinator"
DEFAULT_MAX_RESTARTS = 3          # mirrors lifecycle.SupervisorPolicy defaults
DEFAULT_IDENTICAL_LIMIT = 3
WATCHED_EVENT_PREFIXES = ("worker_", "main_", "replacement")
DONE_STATUSES = ("COMPLETED", "RELEASED")
OCCUPYING_STATUSES = ("ACTIVE", "UNDER_REVIEW")
STEP_DONE = "COMPLETED"

PRIORITY = {"review_worker_result": 10, "worker_failed": 20, "review_step": 30, "verify_wave": 40,
            "start_task": 50, "start_step": 55}
INSPECT_HINT = {
    "review_worker_result": "python -B -m dev_supervisor workers",
    "worker_failed": "python -B -m dev_supervisor workers",
    "review_step": "python -B -m governance",
    "verify_wave": "python -B -m governance wave status",
    "start_task": "python -B -m governance",
    "start_step": "python -B -m governance",
}

GitRunner = Callable[[Sequence[str]], Tuple[int, str]]


# ------------------------------------------------------------------ small helpers

def fingerprint(item_ids: Sequence[str]) -> str:
    """Stable hash of the sorted actionable item ids; the empty set has the empty fingerprint."""
    ids = sorted(set(item_ids))
    if not ids:
        return ""
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]


def overlaps(a: str, b: str) -> bool:
    """Same rule as governance.tasks.overlaps: equal, or one contains the other (case-insensitive)."""
    a, b = a.lower(), b.lower()
    return a == b or b.startswith(a.rstrip("/") + "/") or a.startswith(b.rstrip("/") + "/")


def _load_json(path: Path) -> Any:
    with open(str(path), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _atomic_write_json(path: Path, data: Any) -> None:
    os.makedirs(str(path.parent), exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(str(tmp), "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(path))


def real_git_runner(root: Path, timeout: float = 20.0) -> GitRunner:
    def run(args: Sequence[str]) -> Tuple[int, str]:
        try:
            p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return 127, ""
        return p.returncode, p.stdout
    return run


# ------------------------------------------------------------------ repository records

def dispatch_problems(task: Dict[str, Any]) -> List[str]:
    """Why a registry record is not yet complete enough to hand to a worker (T-DEV-069). Empty means ready.

    Reads the record only. A record is ready when it names owned paths and acceptance criteria and every test
    requirement is a command or gate wording that ``dev_supervisor.testreq`` can classify (none is manual)."""
    probs: List[str] = []

    def filled(key: str) -> bool:
        v = task.get(key)
        return isinstance(v, list) and any(isinstance(x, str) and x.strip() for x in v)

    if not filled("owned_paths"):
        probs.append("owned_paths is empty")
    if not filled("acceptance_criteria"):
        probs.append("acceptance_criteria is empty")
    try:
        for r in testreq.classify_all(task.get("test_requirements")):
            if r.kind == "manual":
                probs.append(f"test_requirements[{r.index}] is not machine-executable ({r.reason})")
    except testreq.TestReqError as e:
        probs.append(str(e))
    return probs


def _task_items(root: Path, running_tasks: set, warnings: List[str], owner: str = DEFAULT_OWNER,
                others: Optional[List[dict]] = None) -> Tuple[List[dict], List[dict], int]:
    """(items, blocked, in_progress_count) from ACTIVE_TASKS.json and DEVELOPMENT_TRACKER.json.

    Steps of tasks owned by someone other than ``owner`` are informational only (appended to ``others``)."""
    items: List[dict] = []
    blocked: List[dict] = []
    in_progress = 0
    try:
        reg = _load_json(root / "ACTIVE_TASKS.json")
        tasks = [t for t in reg.get("tasks", []) if isinstance(t, dict) and t.get("id")]
        shared = {str(k).lower() for k in (reg.get("shared_files") or {})}
    except (OSError, ValueError, AttributeError):
        warnings.append("ACTIVE_TASKS.json unreadable: no registered task considered")
        return items, blocked, in_progress
    by_id = {t["id"]: t for t in tasks}

    def paths(t: dict) -> List[str]:
        return [p for p in (t.get("owned_paths") or []) if isinstance(p, str) and p.lower() not in shared]

    occupying = [t for t in tasks if t.get("status") in OCCUPYING_STATUSES]

    for t in tasks:
        if t.get("status") != "PLANNED":
            continue
        tid = t["id"]
        reasons: List[str] = []
        for dep in t.get("depends_on") or []:
            d = by_id.get(dep)
            if d is None:
                reasons.append(f"depends on unknown task {dep}")
            elif d.get("status") not in DONE_STATUSES:
                reasons.append(f"waits for {dep} ({d.get('status')})")
        for o in occupying:
            if o["id"] == tid:
                continue
            hit = next((p for p in paths(t) for q in paths(o) if overlaps(p, q)), None)
            if hit:
                reasons.append(f"path {hit} is held by {o['id']} ({o.get('status')})")
        if tid in running_tasks:
            reasons.append("a worker is already running for it")
        if reasons:
            blocked.append({"kind": "blocked", "id": f"blocked:{tid}", "task_id": tid, "blocker": "; ".join(reasons)})
        else:
            problems = dispatch_problems(t)
            items.append({"kind": "start_task", "id": f"start_task:{tid}", "task_id": tid,
                          "dispatch_ready": not problems, "dispatch_problems": problems,
                          "reason": f"{tid} is PLANNED, its dependencies are done and no owned path is held",
                          "evidence": Label.OBSERVED.value})

    try:
        tracker = _load_json(root / "DEVELOPMENT_TRACKER.json")
        ttasks = [t for t in tracker.get("tasks", []) if isinstance(t, dict) and t.get("task_id")]
    except (OSError, ValueError, AttributeError):
        warnings.append("DEVELOPMENT_TRACKER.json unreadable: no step considered")
        return items, blocked, in_progress
    for tt in ttasks:
        tid = tt["task_id"]
        rt = by_id.get(tid)
        if rt is None or rt.get("status") != "ACTIVE":
            continue
        steps = [s for s in tt.get("steps", []) if isinstance(s, dict)]
        cur = next((s for s in steps if s.get("status") != STEP_DONE), None)
        if cur is None:
            continue
        sid, st = cur.get("id", "?"), cur.get("status")
        if st in ("READY_FOR_REVIEW", "PLANNED") and rt.get("owner") != owner:
            if others is not None:
                others.append({"kind": "others_waiting_review" if st == "READY_FOR_REVIEW" else "others_waiting_start",
                               "id": f"{st.lower()}:{tid}:{sid}", "task_id": tid, "owner": rt.get("owner"),
                               "reason": f"step {sid} of {tid} is {st}; owned by another stream, not Main's to act on"})
            continue
        if st == "READY_FOR_REVIEW":
            items.append({"kind": "review_step", "id": f"review_step:{tid}:{sid}", "task_id": tid,
                          "reason": f"step {sid} of {tid} is READY_FOR_REVIEW and waits for review",
                          "evidence": Label.OBSERVED.value})
        elif st == "PLANNED":
            if tid in running_tasks:
                blocked.append({"kind": "blocked", "id": f"blocked:{tid}:{sid}", "task_id": tid,
                                "blocker": f"step {sid}: a worker is already running for {tid}"})
            else:
                items.append({"kind": "start_step", "id": f"start_step:{tid}:{sid}", "task_id": tid,
                              "reason": f"step {sid} of ACTIVE {tid} is the next PLANNED step "
                                        f"(needs step authorization per DEVELOPMENT_STEP_PROTOCOL.md)",
                              "evidence": Label.OBSERVED.value})
        elif st == "IN_PROGRESS":
            in_progress += 1
    return items, blocked, in_progress


def _done_task_ids(root: Path) -> set:
    try:
        reg = _load_json(root / "ACTIVE_TASKS.json")
        return {t["id"] for t in reg.get("tasks", []) if isinstance(t, dict) and t.get("status") in DONE_STATUSES}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return set()


def wave_closed(manifest: Dict[str, Any], done_ids: set) -> bool:
    """A wave is closed when ``wave close`` has written its lanes' tasks as COMPLETED/RELEASED into ACTIVE_TASKS.json
    (that is what governance.waves.closeout_records does), or when the manifest itself records a closed state."""
    if str(manifest.get("status", "")).upper() == "CLOSED" or manifest.get("closed"):
        return True
    tids = [l.get("task_id") for l in manifest.get("lanes", []) if isinstance(l, dict)]
    return bool(tids) and all(t in done_ids for t in tids)


def _wave_items(root: Path, git: GitRunner, warnings: List[str]) -> List[dict]:
    items: List[dict] = []
    done_ids = _done_task_ids(root)
    waves_dir = root / "waves"
    try:
        manifests = sorted(waves_dir.glob("*/wave.json"))
    except OSError:
        return items
    for mpath in manifests:
        try:
            m = _load_json(mpath)
            wid = str(m["wave_id"])
            lane_ids = [str(l["id"]) for l in m.get("lanes", [])]
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append(f"{mpath.parent.name}/wave.json unreadable")
            continue
        if not re.fullmatch(r"[A-Za-z0-9._-]+", wid):
            continue
        if wave_closed(m, done_ids):
            continue  # closed wave: never a verify item, even if its branch is not an ancestor of main
        ref = f"refs/remotes/origin/wave/{wid}"
        rc, _ = git(["rev-parse", "--verify", "--quiet", ref])
        if rc != 0:
            continue  # no remote wave branch: nothing submitted (or already cleaned up)
        rc, _ = git(["merge-base", "--is-ancestor", ref, "refs/remotes/origin/main"])
        if rc == 0:
            continue  # wave already merged into origin/main
        rc, out = git(["ls-tree", "-r", "--name-only", ref, "--", f"waves/{wid}/lanes/"])
        if rc != 0:
            warnings.append(f"wave {wid}: could not list lane reports")
            continue
        submitted = sorted({mm.group(1) for line in out.splitlines()
                            for mm in [re.fullmatch(rf"waves/{re.escape(wid)}/lanes/([^/]+)/report\.json", line.strip())]
                            if mm and mm.group(1) in lane_ids})
        if not submitted:
            continue
        items.append({"kind": "verify_wave", "id": f"verify_wave:{wid}:{','.join(submitted)}", "task_id": wid,
                      "reason": f"{len(submitted)} of {len(lane_ids)} lanes of wave {wid} submitted "
                                f"({', '.join(submitted)}); wave branch is not merged into origin/main",
                      "evidence": Label.OBSERVED.value})
    return items


def _worker_items(store: Optional[StateStore], warnings: List[str]) -> Tuple[List[dict], List[dict], set, int]:
    """(items, blocked, task ids with a live worker, running worker count)."""
    items: List[dict] = []
    blocked: List[dict] = []
    running_tasks: set = set()
    running = 0
    if store is None or not store.directory.is_dir():
        return items, blocked, running_tasks, running
    try:
        reg = WorkerRegistry(store)
    except (StoreError, ValueError, TypeError, OSError) as e:
        warnings.append(f"worker registry unreadable ({e.__class__.__name__})")
        return items, blocked, running_tasks, running
    for w in reg.workers.values():
        if w.status in WorkerStatus.ACTIVE:
            running_tasks.add(w.task_id)
            running += 1
    for r in sorted(reg.results.values(), key=lambda r: r.result_id):
        if r.status == "REPORTED":
            items.append({"kind": "review_worker_result", "id": f"review_worker_result:{r.result_id}",
                          "task_id": r.task_id, "worker_id": r.worker_id,
                          "reason": f"worker {r.worker_id} reported {r.outcome} for {r.task_id}; not verified yet",
                          "evidence": Label.REPORTED.value})
    for w in sorted(reg.workers.values(), key=lambda w: w.worker_id):
        if w.status == WorkerStatus.BLOCKED:
            blocked.append({"kind": "blocked", "id": f"blocked:worker:{w.worker_id}", "task_id": w.task_id,
                            "blocker": f"worker {w.worker_id} is BLOCKED (needs a human decision)"})
            continue
        if w.status not in (WorkerStatus.FAILED, WorkerStatus.LOST):
            continue
        if w.task_id in running_tasks:
            continue
        newer = [o for o in reg.workers.values() if o.task_id == w.task_id and o.worker_id != w.worker_id
                 and (o.assigned_at or "") > (w.assigned_at or "")]
        if newer:
            continue
        n = DEFAULT_IDENTICAL_LIMIT
        identical = len(w.failures) >= n and len(set(w.failures[-n:])) == 1
        exhausted = w.restart_count >= DEFAULT_MAX_RESTARTS
        if identical:
            retry = f"retry blocked: {n} identical failures in a row"
        elif exhausted:
            retry = f"retry blocked: restart budget exhausted ({w.restart_count}/{DEFAULT_MAX_RESTARTS})"
        else:
            retry = f"retry eligible (restart {w.restart_count}/{DEFAULT_MAX_RESTARTS})"
            if w.next_restart_not_before:
                retry += f" not before {w.next_restart_not_before}"
        items.append({"kind": "worker_failed",
                      "id": f"worker_failed:{w.worker_id}:{w.status}:{w.restart_count}", "task_id": w.task_id,
                      "worker_id": w.worker_id, "retry_eligible": not (identical or exhausted),
                      "reason": f"worker {w.worker_id} for {w.task_id} is {w.status}; {retry}",
                      "evidence": Label.REPORTED.value})
    return items, blocked, running_tasks, running


def _milestone_proposals(root: Path, warnings: List[str]) -> List[dict]:
    """Read-only advice for Main: milestones of PROJECT_STATUS.json that could be registered as work (T-DEV-039).

    Eligible: implementation NOT_STARTED, every dependency IMPLEMENTED, and no registered task that is not done
    mentions the milestone id. A milestone with blockers or open questions is flagged ``needs_decision``: a human or
    Main must resolve it before any task is registered. Nothing is registered or written. A missing file is silent (a
    repository without a plan); an unreadable or malformed one is a warning and yields no proposals."""
    path = root / "PROJECT_STATUS.json"
    if not path.is_file():
        return []
    try:
        doc = _load_json(path)
        if not isinstance(doc.get("milestones"), list):
            raise ValueError("milestones is not a list")
        milestones = [m for m in doc["milestones"] if isinstance(m, dict) and isinstance(m.get("id"), str)]
    except (OSError, ValueError, AttributeError):
        warnings.append("PROJECT_STATUS.json unreadable or malformed: no milestone proposals")
        return []

    def state(m: dict) -> str:
        impl = m.get("implementation")
        return str(impl.get("state")) if isinstance(impl, dict) else "UNKNOWN"

    by_id = {m["id"]: m for m in milestones}
    try:
        reg = _load_json(root / "ACTIVE_TASKS.json")
        if not (isinstance(reg, dict) and isinstance(reg.get("tasks"), list)
                and all(isinstance(t, dict) for t in reg["tasks"])):
            raise ValueError("the registry must be an object with a list of task objects")
        open_work = json.dumps([t for t in reg["tasks"] if t.get("status") not in DONE_STATUSES])
    except (OSError, ValueError, AttributeError, TypeError):
        # Without the registry nothing can be checked against open work, so propose nothing (fail closed).
        warnings.append("ACTIVE_TASKS.json unreadable or malformed: no milestone proposals")
        return []
    out: List[dict] = []
    for m in milestones:
        if state(m) != "NOT_STARTED":
            continue
        deps = m.get("depends_on") or []
        if not isinstance(deps, list) or any(not isinstance(d, str) or d not in by_id or state(by_id[d]) != "IMPLEMENTED"
                                             for d in deps):
            continue
        if re.search(r"(?<![A-Za-z0-9-])" + re.escape(m["id"]) + r"(?![A-Za-z0-9-])", open_work):   # whole-id match
            continue
        questions, blockers = m.get("open_questions") or [], m.get("blockers") or []
        malformed = not isinstance(questions, list) or not isinstance(blockers, list)
        if malformed:   # junk leans towards "needs a decision", never towards "ready"
            questions, blockers = ["open_questions or blockers is malformed in PROJECT_STATUS.json"], []
        questions, blockers = [str(q) for q in questions], [str(b) for b in blockers]
        out.append({"kind": "propose_milestone", "id": f"propose:{m['id']}", "milestone_id": m["id"],
                    "title": str(m.get("title", "")), "phase": str(m.get("phase", "")), "weight": m.get("weight"),
                    "needs_decision": bool(questions or blockers), "open_questions": questions, "blockers": blockers,
                    "evidence": "PROPOSED",
                    "reason": f"{m['id']} is NOT_STARTED, its dependencies are implemented and no open task mentions it"})
    out.sort(key=lambda p: p["needs_decision"])   # stable: document order inside each group
    return out


def compute_items(root: Path, store: Optional[StateStore], git: Optional[GitRunner] = None,
                  owner: str = DEFAULT_OWNER) -> Dict[str, Any]:
    """The actionable picture for Main. Never raises; problems become ``warnings``."""
    warnings: List[str] = []
    git = git or real_git_runner(root)
    items: List[dict] = []
    blocked: List[dict] = []
    others: List[dict] = []
    running = in_progress = 0
    running_tasks: set = set()
    try:
        witems, wblocked, running_tasks, running = _worker_items(store, warnings)
        items += witems
        blocked += wblocked
    except Exception as e:  # noqa: BLE001 - tolerant by contract
        warnings.append(f"worker items failed ({e.__class__.__name__})")
    try:
        titems, tblocked, in_progress = _task_items(root, running_tasks, warnings, owner, others)
        items += titems
        blocked += tblocked
    except Exception as e:  # noqa: BLE001
        warnings.append(f"task items failed ({e.__class__.__name__})")
    try:
        items += _wave_items(root, git, warnings)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"wave items failed ({e.__class__.__name__})")
    items.sort(key=lambda i: (PRIORITY.get(i["kind"], 99), i["id"]))
    try:
        proposals = _milestone_proposals(root, warnings)
    except Exception as e:  # noqa: BLE001 - tolerant by contract
        proposals = []
        warnings.append(f"milestone proposals failed ({e.__class__.__name__})")
    for i in items:
        i["priority"] = PRIORITY.get(i["kind"], 99)
        i["inspect"] = INSPECT_HINT.get(i["kind"], "python -B -m dev_supervisor.autonomy next")
    idle = None
    if not items:
        parts = []
        if running:
            parts.append(f"waiting for {running} running worker{'s' if running != 1 else ''}")
        if in_progress:
            parts.append(f"{in_progress} step{'s' if in_progress != 1 else ''} IN_PROGRESS")
        tb = [b for b in blocked if not b["id"].startswith("blocked:worker:")]
        if tb:
            parts.append(f"no eligible task: {tb[0]['task_id']} {tb[0]['blocker']}")
        elif blocked:
            parts.append(f"{len(blocked)} blocked item(s) need a human")
        idle = "; ".join(parts) or "no registered work: nothing is planned or waiting"
        if proposals:
            ready = [p["milestone_id"] for p in proposals if not p["needs_decision"]]
            idle += (f"; {len(proposals)} milestone(s) could be proposed ({len(ready)} without an open decision"
                     + (": " + ", ".join(ready[:5]) if ready else "") + ")")
    if store is not None and store.directory.is_dir():
        try:
            cands = authoritative_candidates(store)
            if len(cands) > 1:
                warnings.append("multiple authoritative Main sessions: " + ", ".join(c[:8] for c in cands))
        except Exception:  # noqa: BLE001
            pass
    ids = [i["id"] for i in items]
    return {"evidence": Label.OBSERVED.value, "counts": {"items": len(items), "blocked": len(blocked),
                                                          "running_workers": running, "warnings": len(warnings),
                                                          "others_waiting": len(others), "proposals": len(proposals)},
            "fingerprint": fingerprint(ids), "items": items, "blocked": blocked, "idle_reason": idle,
            "others_waiting_review": others, "warnings": warnings, "proposals": proposals}


# ------------------------------------------------------------------ state helpers

def open_store(cfg: Config, env: Optional[Dict[str, str]], clock: Callable[[], float],
               state_dir: Optional[str] = None) -> StateStore:
    root = cfg.repository_root()
    directory = Path(state_dir) if state_dir else cfg.state_directory(env)
    return StateStore(directory, repo_roots=[root] if root else [], clock=clock, create=False)


def authoritative_candidates(store: StateStore) -> List[str]:
    """Session ids recorded as authoritative Main (role main, ACTIVE or RETIRING), in document order."""
    try:
        doc = store.read("sessions", default=None) or {}
        return [str(sid) for sid, rec in (doc.get("sessions") or {}).items()
                if rec.get("role") == "main" and rec.get("authoritative")
                and rec.get("status") in ("ACTIVE", "RETIRING")]
    except (StoreError, AttributeError, TypeError):
        return []


def authoritative_main(store: StateStore) -> Optional[str]:
    """The authoritative Main session id, or None when unknown or ambiguous (callers then fail open).

    1. main.json is RUNNING and its session is among the candidates: that one (resolves stale duplicates).
    2. exactly one candidate: it.  3. several and none matches main.json: None.
    With no sessions document, main.json while RUNNING is the fallback."""
    cands = authoritative_candidates(store)
    running = None
    try:
        main = store.read("main", default=None) or {}
        if main.get("session_id") and main.get("status") == "RUNNING":
            running = str(main["session_id"])
    except (StoreError, AttributeError, TypeError):
        pass
    if running is not None and running in cands:
        return running
    if len(cands) == 1:
        return cands[0]
    if not cands:
        return running
    return None


def _resolve(args: argparse.Namespace, env: Optional[Dict[str, str]], clock: Callable[[], float]
             ) -> Tuple[Config, Path, StateStore]:
    cfg = config_mod.load(getattr(args, "config", None), env)
    root = Path(args.repo) if getattr(args, "repo", None) else cfg.repository_root()
    if root is None:
        root = find_git_root(Path(__file__).resolve().parent)
    if root is None:
        raise ConfigError("repository root unknown: pass --repo")
    store = open_store(cfg, env, clock, getattr(args, "state_dir", None))
    return cfg, Path(root), store


def _max_blocks(args: argparse.Namespace, env: Optional[Dict[str, str]]) -> int:
    if getattr(args, "max_blocks", None) is not None:
        return max(0, int(args.max_blocks))
    try:
        return max(0, int((env if env is not None else os.environ).get(ENV_MAX_BLOCKS, DEFAULT_MAX_BLOCKS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BLOCKS


# ------------------------------------------------------------------ commands

def cmd_next(a: argparse.Namespace, out: TextIO, env, clock, git) -> int:
    cfg, root, store = _resolve(a, env, clock)
    res = compute_items(root, store, git, a.owner)
    res["generated_at"] = utc_iso(clock())
    if a.json:
        out.write(json.dumps(res, indent=2, sort_keys=True) + "\n")
        return EXIT_OK
    if not res["items"]:
        out.write(f"idle: {res['idle_reason']}\n")
    for i in res["items"]:
        out.write(f"[{i['priority']:>2}] {i['kind']} {i['id']}\n     {i['reason']}\n     inspect: {i['inspect']}\n")
        if i["kind"] == "start_task":
            out.write("     dispatch: " + ("READY (record is complete; memory and a route probe are still checked at dispatch)"
                                           if i.get("dispatch_ready") else "NOT READY - " + "; ".join(i.get("dispatch_problems") or ["unknown"])) + "\n")
    for b in res["blocked"]:
        out.write(f"blocked {b['task_id']}: {b['blocker']}\n")
    if not res["items"]:
        for p in res["proposals"]:
            flag = " (needs a decision: " + "; ".join(p["open_questions"] + p["blockers"])[:160] + ")" \
                if p["needs_decision"] else ""
            out.write(f"PROPOSED {p['milestone_id']} [{p['phase']}] {p['title']}{flag}\n")
    for o in res["others_waiting_review"]:
        out.write(f"info (not actionable) {o['task_id']}: {o['reason']}\n")
    for w in res["warnings"]:
        out.write(f"warning: {w}\n")
    return EXIT_OK


def _new_events(store: StateStore, since: int) -> Tuple[List[dict], int]:
    try:
        events, _bad = store.read_events()
    except OSError:
        return [], since
    total = len(events)
    if since > total:
        since = 0  # log was rotated or replaced
    new = [{"kind": e.get("kind"), "ts": e.get("ts")} for e in events[since:]
           if str(e.get("kind", "")).startswith(WATCHED_EVENT_PREFIXES)]
    return new, total


def cmd_wait(a: argparse.Namespace, out: TextIO, env, clock, git, sleep: Callable[[float], None]) -> int:
    cfg, root, store = _resolve(a, env, clock)
    if a.cursor:
        cpath = Path(a.cursor)
    elif store.directory.is_dir():
        cpath = store.directory / "autonomy_cursor.json"
    else:
        cpath = None
    cur: Dict[str, Any] = {}
    if cpath is not None:
        try:
            cur = _load_json(cpath)
            if not isinstance(cur, dict):
                cur = {}
        except (OSError, ValueError):
            cur = {}
    base = compute_items(root, store, git, a.owner)
    known_fp = cur.get("fingerprint") if "fingerprint" in cur else base["fingerprint"]
    known_ids = cur.get("ids") if isinstance(cur.get("ids"), list) else [i["id"] for i in base["items"]]
    since = cur.get("event_count") if isinstance(cur.get("event_count"), int) else None
    if since is None:
        _n, since = _new_events(store, 10 ** 12)
    deadline = clock() + max(0.0, float(a.timeout))
    poll = max(0.05, float(a.poll))
    first = True
    while True:
        res = base if first else compute_items(root, store, git, a.owner)
        first = False
        new, total = _new_events(store, since)
        fp_changed = res["fingerprint"] != known_fp
        if fp_changed or new:
            ids = [i["id"] for i in res["items"]]
            summary = {"changed": True, "reason": ("fingerprint" if fp_changed else "events"),
                       "fingerprint": res["fingerprint"], "added": sorted(set(ids) - set(known_ids)),
                       "removed": sorted(set(known_ids) - set(ids)), "new_events": new[:20],
                       "items": len(ids), "idle_reason": res["idle_reason"]}
            _save_cursor(cpath, res, total)
            out.write(json.dumps(summary, sort_keys=True) + "\n")
            return EXIT_OK
        if clock() >= deadline:
            _save_cursor(cpath, res, total)
            out.write(json.dumps({"changed": False, "timeout": True, "fingerprint": res["fingerprint"],
                                  "items": len(res["items"])}, sort_keys=True) + "\n")
            return EXIT_TIMEOUT
        sleep(min(poll, max(0.0, deadline - clock())) or poll)


def _save_cursor(cpath: Optional[Path], res: Dict[str, Any], total: int) -> None:
    if cpath is None:
        return
    try:
        _atomic_write_json(cpath, {"fingerprint": res["fingerprint"], "ids": [i["id"] for i in res["items"]],
                                   "event_count": total})
    except OSError:
        pass


def _one_line(item: dict, total: int) -> str:
    text = f"{item['kind']} {item['id']}: {item['reason']}"
    text = scrub(re.sub(r"\s+", " ", text))[:260]
    more = f" (+{total - 1} more)" if total > 1 else ""
    return (f"Autonomy loop: {total} actionable item(s). Next: {text}{more}. "
            f"Inspect with: python -B -m dev_supervisor.autonomy next")


def decide_stop(payload: Any, store: Optional[StateStore], root: Path, git: Optional[GitRunner],
                max_blocks: int, owner: str = DEFAULT_OWNER) -> Optional[str]:
    """Return the block reason, or None to allow the stop. May raise (the caller fails open)."""
    if not isinstance(payload, dict) or store is None or not store.directory.is_dir():
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or authoritative_main(store) != sid:
        return None
    res = compute_items(root, store, git, owner)
    state = store.read(AUTONOMY_DOC, default=None) or {}
    if not isinstance(state, dict):
        state = {}
    fp = res["fingerprint"]
    if not res["items"]:
        if state.get("fingerprint") or state.get("consecutive_blocks"):
            store.write(AUTONOMY_DOC, {"fingerprint": "", "consecutive_blocks": 0, "updated_at": utc_iso(store.clock())})
        return None
    active = bool(payload.get("stop_hook_active"))
    count = state.get("consecutive_blocks") if isinstance(state.get("consecutive_blocks"), int) else 0
    if state.get("fingerprint") == fp:
        if active or count >= max_blocks:
            return None
        count += 1
    else:
        if max_blocks < 1:
            return None
        count = 1
    store.write(AUTONOMY_DOC, {"fingerprint": fp, "consecutive_blocks": count, "told_ids": [i["id"] for i in res["items"]],
                               "updated_at": utc_iso(store.clock())})
    return _one_line(res["items"][0], len(res["items"]))


def cmd_stop_hook(a: argparse.Namespace, out: TextIO, env, clock, git, stdin: TextIO) -> int:
    store: Optional[StateStore] = None
    try:
        try:
            payload = json.loads(stdin.read() or "null")
        except ValueError:
            return EXIT_OK
        cfg, root, store = _resolve(a, env, clock)
        reason = decide_stop(payload, store, root, git, _max_blocks(a, env), a.owner)
        if reason:
            out.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    except Exception as e:  # noqa: BLE001 - fail open on every error
        try:
            if store is not None and store.directory.is_dir():
                store.append_event("autonomy_hook_error", error=e.__class__.__name__)
        except Exception:  # noqa: BLE001
            pass
    return EXIT_OK


def _quote(s: str) -> str:
    s = str(s).replace("\\", "/")  # forward slashes survive cmd.exe, PowerShell and bash alike
    return '"' + s.replace('"', '\\"') + '"' if re.search(r"[\s\"'&|<>()^;$`]", s) else s


def build_hook_command(python: str, script: str, max_blocks: Optional[int] = None) -> str:
    cmd = f"{_quote(python)} -B {_quote(script)} stop-hook"
    if max_blocks is not None:
        cmd += f" --max-blocks {int(max_blocks)}"
    return cmd


def cmd_main_settings(a: argparse.Namespace, out: TextIO) -> int:
    orch = Path(a.orchestrator) if a.orchestrator else Path(__file__).resolve().parents[1]
    script = orch / "dev_supervisor" / "autonomy.py"
    python = a.python or sys.executable or "python"
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                               "command": build_hook_command(python, str(script), a.max_blocks),
                                               "timeout": 60}]}]}}
    target = Path(a.out)
    for forbidden in (Path.home() / ".claude", ):
        try:
            if os.path.abspath(str(target)).lower().startswith(os.path.abspath(str(forbidden)).lower() + os.sep):
                out.write("refused: settings file must not be written under ~/.claude\n")
                return EXIT_FAIL
        except OSError:
            pass
    if ".claude" in [p.lower() for p in target.parts]:
        out.write("refused: settings file must not be written into a .claude directory\n")
        return EXIT_FAIL
    _atomic_write_json(target, settings)
    out.write(f"wrote {target.name} with one hooks.Stop entry\n")
    return EXIT_OK


# ------------------------------------------------------------------ parser and entry point

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dev_supervisor.autonomy", description="Autonomy loop helpers for Main")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="machine-local supervisor config file")
    common.add_argument("--repo", help="repository checkout holding the registries (default: configured/this one)")
    common.add_argument("--owner", default=DEFAULT_OWNER,
                        help="task owner whose step reviews are Main's to act on (default %(default)s)")
    common.add_argument("--state-dir", dest="state_dir", help="override the supervisor state directory")
    sub = p.add_subparsers(dest="command", required=True)
    n = sub.add_parser("next", parents=[common], help="list actionable items for Main")
    n.add_argument("--json", action="store_true")
    w = sub.add_parser("wait", parents=[common], help="block until something changes (exit 3 on timeout)")
    w.add_argument("--timeout", type=float, required=True)
    w.add_argument("--poll", type=float, default=15.0)
    w.add_argument("--cursor", help="file remembering the last seen fingerprint and event position")
    s = sub.add_parser("stop-hook", parents=[common], help="Claude Code Stop hook (reads JSON on stdin)")
    s.add_argument("--max-blocks", dest="max_blocks", type=int, default=None,
                   help=f"consecutive blocks before allowing (default {DEFAULT_MAX_BLOCKS}, env {ENV_MAX_BLOCKS})")
    m = sub.add_parser("main-settings", help="write a Claude Code settings file with only a Stop hook")
    m.add_argument("--out", required=True)
    m.add_argument("--python", help="python executable (default: the current one)")
    m.add_argument("--orchestrator", help="orchestrator directory (default: this checkout's)")
    m.add_argument("--max-blocks", dest="max_blocks", type=int, default=None)
    return p


def main(argv: Optional[Sequence[str]] = None, *, out: Optional[TextIO] = None, stdin: Optional[TextIO] = None,
         env: Optional[Dict[str, str]] = None, git: Optional[GitRunner] = None,
         clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> int:
    out = out or sys.stdout
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    try:
        if a.command == "main-settings":
            return cmd_main_settings(a, out)
        if a.command == "stop-hook":
            return cmd_stop_hook(a, out, env, clock, git, stdin or sys.stdin)
        git_runner = git
        if git_runner is None:
            root = Path(a.repo) if a.repo else None
            git_runner = real_git_runner(root) if root else None
        if a.command == "next":
            return cmd_next(a, out, env, clock, git_runner)
        return cmd_wait(a, out, env, clock, git_runner, sleep)
    except ConfigError as e:
        out.write(f"configuration error: {e}\n")
        return EXIT_USAGE
    except StoreError as e:
        out.write(f"refused: {e.__class__.__name__}: {e}\n")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
