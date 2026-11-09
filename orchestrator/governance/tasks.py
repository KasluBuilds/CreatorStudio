"""Validation of ACTIVE_TASKS.json: file ownership, leases, packages and exclusive resources.

Standard library only. `validate` is clock-free (so tests and CI are reproducible); lease expiry is
checked separately by `expired_leases(doc, now)` with an explicitly supplied time.

Model
  - A task claims files/directories (`owned_paths`) while its status is ACTIVE or UNDER_REVIEW.
  - Two active tasks may not claim overlapping paths unless BOTH list the same integration agreement
    and that agreement covers the overlapping path.
  - A package (see `package_owners`) is owned by exactly one owner. A task owned by someone else (or by
    an UNRESOLVED package) needs an integration agreement to touch it.
  - Exclusive resources (currently only "resolve", the DaVinci Resolve bridge) may be held by at most
    one active task at a time: Resolve mutations are serialized.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, List

SCHEMA_VERSION = 1
TASK_STATUSES = ("PLANNED", "ACTIVE", "UNDER_REVIEW", "COMPLETED", "RELEASED", "BLOCKED")
ACTIVE_STATUSES = ("ACTIVE", "UNDER_REVIEW")  # these hold their file leases
EXCLUSIVE_RESOURCES = ("resolve",)
UNRESOLVED = "UNRESOLVED"

TASK_ID = re.compile(r"^T-[A-Z0-9]+-\d{3}$")
AGREEMENT_ID = re.compile(r"^AGR-[A-Z0-9-]+$")
BRANCH = re.compile(r"^(task|chatgpt)/[a-z0-9][a-z0-9._-]*$")
OWNER = re.compile(r"^(claude|chatgpt|human):[a-z0-9][a-z0-9._-]*$")
DEFAULT_LEASE_HOURS = 24
MAX_LEASE_HOURS = 72


def _nonempty(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _str_list(v: Any, allow_empty: bool = True) -> bool:
    return isinstance(v, list) and all(_nonempty(x) for x in v) and (allow_empty or bool(v))


def path_problem(p: Any) -> str:
    """Return a description of what is wrong with a repo-relative path, or '' if it is acceptable."""
    if not _nonempty(p):
        return "path must be a non-empty string"
    if "\\" in p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return f"path {p!r} must be repo-relative with forward slashes"
    if any(seg in ("..", ".") for seg in p.split("/")) or any(c in p for c in "*?[]"):
        return f"path {p!r} must not contain '.', '..' or glob characters"
    if "//" in p:
        return f"path {p!r} contains an empty segment"
    return ""


def overlaps(a: str, b: str) -> bool:
    """Directories end with '/'. Two paths overlap if equal or one is a directory containing the other."""
    return a == b or (a.endswith("/") and b.startswith(a)) or (b.endswith("/") and a.startswith(b))


def _overlap_path(a: str, b: str) -> str:
    return a if len(a) >= len(b) else b  # the more specific of two overlapping paths


def parse_time(value: Any) -> _dt.datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    dt = _dt.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return dt


def _agreement_covers(doc: Dict[str, Any], agr_id: str, task_a: dict, task_b: dict, path: str) -> bool:
    agr = doc.get("integration_agreements", {}).get(agr_id)
    if not isinstance(agr, dict):
        return False
    members = agr.get("task_ids", [])
    if task_a["id"] not in members or task_b["id"] not in members:
        return False
    return any(overlaps(p, path) for p in agr.get("paths", []) if isinstance(p, str))


def validate(doc: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")

    pol = doc.get("lease_policy")
    if not (isinstance(pol, dict) and pol.get("default_hours") == DEFAULT_LEASE_HOURS
            and pol.get("max_hours") == MAX_LEASE_HOURS):
        problems.append(f"lease_policy must be {{default_hours: {DEFAULT_LEASE_HOURS}, max_hours: {MAX_LEASE_HOURS}}}")

    owners = doc.get("package_owners")
    if not isinstance(owners, dict) or not owners:
        problems.append("'package_owners' must be a non-empty object")
        owners = {}
    seen_pkg_paths: Dict[str, str] = {}
    for name, pkg in owners.items():
        if not isinstance(pkg, dict) or not _str_list(pkg.get("paths"), allow_empty=False):
            problems.append(f"package_owners[{name}]: needs a non-empty 'paths' list")
            continue
        owner, status = pkg.get("owner"), pkg.get("owner_status")
        if owner != UNRESOLVED and not (isinstance(owner, str) and OWNER.match(owner)):
            problems.append(f"package_owners[{name}]: owner must be '{UNRESOLVED}' or match {OWNER.pattern}")
        if not _nonempty(status):
            problems.append(f"package_owners[{name}]: owner_status is required (say how the owner was established)")
        if (owner == UNRESOLVED) != (isinstance(status, str) and status.startswith(UNRESOLVED)):
            problems.append(f"package_owners[{name}]: owner_status must start with {UNRESOLVED} exactly when the owner is unresolved")
        for p in pkg["paths"]:
            msg = path_problem(p)
            if msg:
                problems.append(f"package_owners[{name}]: {msg}")
            elif p in seen_pkg_paths:
                problems.append(f"package_owners[{name}]: path {p!r} is also listed under {seen_pkg_paths[p]}")
            else:
                seen_pkg_paths[p] = name

    shared = doc.get("shared_files")
    if not isinstance(shared, dict):
        problems.append("'shared_files' must be an object {path: note}")
        shared = {}
    for p, note in shared.items():
        msg = path_problem(p)
        if msg:
            problems.append(f"shared_files: {msg}")
        if not _nonempty(note):
            problems.append(f"shared_files[{p}]: a note explaining why it is shared is required")

    agreements = doc.get("integration_agreements")
    if not isinstance(agreements, dict):
        problems.append("'integration_agreements' must be an object")
        agreements = {}
    for aid, agr in agreements.items():
        if not AGREEMENT_ID.match(str(aid)):
            problems.append(f"integration_agreements: id {aid!r} must match {AGREEMENT_ID.pattern}")
        if not (isinstance(agr, dict) and _nonempty(agr.get("description")) and _str_list(agr.get("task_ids"), False)
                and _str_list(agr.get("paths"), False)):
            problems.append(f"integration_agreements[{aid}]: needs description, task_ids and paths")
            continue
        for p in agr["paths"]:
            msg = path_problem(p)
            if msg:
                problems.append(f"integration_agreements[{aid}]: {msg}")

    tasks = doc.get("tasks")
    if not isinstance(tasks, list):
        problems.append("'tasks' must be a list")
        return problems
    by_id: Dict[str, dict] = {}
    for idx, t in enumerate(tasks):
        label = f"tasks[{idx}]"
        if not isinstance(t, dict):
            problems.append(f"{label}: must be an object")
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not TASK_ID.match(tid):
            problems.append(f"{label}: id must match {TASK_ID.pattern}")
            continue
        label = tid
        if tid in by_id:
            problems.append(f"{label}: duplicate task id")
            continue
        by_id[tid] = t
        for f in ("title", "description", "integration_notes"):
            if not _nonempty(t.get(f)):
                problems.append(f"{label}: '{f}' is required")
        if t.get("status") not in TASK_STATUSES:
            problems.append(f"{label}: status must be one of {', '.join(TASK_STATUSES)}")
        if not (isinstance(t.get("owner"), str) and OWNER.match(t["owner"])):
            problems.append(f"{label}: owner must match {OWNER.pattern}")
        if not (isinstance(t.get("branch"), str) and BRANCH.match(t["branch"])):
            problems.append(f"{label}: branch must match {BRANCH.pattern}")
        wt = t.get("worktree")
        if wt is not None and not _nonempty(wt):
            problems.append(f"{label}: worktree must be null or a path")
        if t.get("status") in ACTIVE_STATUSES and str(t.get("owner", "")).startswith("claude:") and not wt:
            problems.append(f"{label}: an active Claude Code task must declare its own worktree")
        for f in ("acceptance_criteria", "test_requirements"):
            if not _str_list(t.get(f), allow_empty=False):
                problems.append(f"{label}: '{f}' must be a non-empty list of strings")
        if not _str_list(t.get("depends_on")):
            problems.append(f"{label}: depends_on must be a list of task ids")
        if not _str_list(t.get("integration_agreements")):
            problems.append(f"{label}: integration_agreements must be a list of agreement ids")
        else:
            for a in t["integration_agreements"]:
                if a not in agreements:
                    problems.append(f"{label}: unknown integration agreement {a}")
        if not _str_list(t.get("exclusive_resources")) or set(t.get("exclusive_resources", [])) - set(EXCLUSIVE_RESOURCES):
            problems.append(f"{label}: exclusive_resources must be a subset of {list(EXCLUSIVE_RESOURCES)}")
        paths = t.get("owned_paths")
        if not _str_list(paths, allow_empty=t.get("status") in ("PLANNED", "COMPLETED", "RELEASED")):
            problems.append(f"{label}: owned_paths must be a list of paths (non-empty while active)")
            paths = []
        for p in paths:
            msg = path_problem(p)
            if msg:
                problems.append(f"{label}: {msg}")
        lease = t.get("lease")
        if t.get("status") in ACTIVE_STATUSES or lease is not None:
            try:
                acquired, expires = parse_time(lease["acquired"]), parse_time(lease["expires"])
                if expires <= acquired:
                    problems.append(f"{label}: lease expires must be after acquired")
                elif expires - acquired > _dt.timedelta(hours=MAX_LEASE_HOURS):
                    problems.append(f"{label}: lease longer than {MAX_LEASE_HOURS} hours")
            except (TypeError, KeyError, ValueError):
                problems.append(f"{label}: lease needs ISO-8601 'acquired' and 'expires' with a UTC offset")

    for tid, t in by_id.items():
        for dep in t.get("depends_on", []) if isinstance(t.get("depends_on"), list) else []:
            if dep not in by_id:
                problems.append(f"{tid}: depends_on unknown task {dep}")
    problems.extend(_task_cycles(by_id))

    active = [t for t in by_id.values() if t.get("status") in ACTIVE_STATUSES]
    branches: Dict[str, str] = {}
    for t in active:
        if t.get("branch") in branches:
            problems.append(f"{t['id']}: branch {t['branch']} is already used by {branches[t['branch']]}")
        branches[t.get("branch")] = t["id"]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            for pa in a.get("owned_paths", []) if isinstance(a.get("owned_paths"), list) else []:
                for pb in b.get("owned_paths", []) if isinstance(b.get("owned_paths"), list) else []:
                    if not (isinstance(pa, str) and isinstance(pb, str)) or not overlaps(pa, pb):
                        continue
                    where = _overlap_path(pa, pb)
                    shared_ok = [x for x in (a.get("integration_agreements") or [])
                                 if x in (b.get("integration_agreements") or [])
                                 and _agreement_covers(doc, x, a, b, where)]
                    if not shared_ok:
                        problems.append(f"{a['id']} and {b['id']} both claim {where!r} without a covering "
                                        f"integration agreement")
    for res in EXCLUSIVE_RESOURCES:
        holders = [t["id"] for t in active if res in (t.get("exclusive_resources") or [])]
        if len(holders) > 1:
            problems.append(f"exclusive resource '{res}' is held by more than one active task: {holders}")
    for t in active:
        for p in t.get("owned_paths", []) if isinstance(t.get("owned_paths"), list) else []:
            for name, pkg in owners.items():
                if not isinstance(pkg, dict):
                    continue
                for pp in pkg.get("paths", []) if isinstance(pkg.get("paths"), list) else []:
                    if isinstance(p, str) and isinstance(pp, str) and overlaps(p, pp) and pkg.get("owner") != t.get("owner"):
                        covered = any(x in (t.get("integration_agreements") or [])
                                      and isinstance(agreements.get(x), dict)
                                      and t["id"] in agreements[x].get("task_ids", [])
                                      and any(overlaps(q, _overlap_path(p, pp)) for q in agreements[x].get("paths", [])
                                              if isinstance(q, str))
                                      for x in agreements)
                        if not covered:
                            problems.append(f"{t['id']} claims {p!r} inside package '{name}' owned by "
                                            f"{pkg.get('owner')}; needs that owner's integration agreement")
    return sorted(set(problems))


def _task_cycles(by_id: Dict[str, dict]) -> List[str]:
    problems, state = [], {}

    def visit(node: str, trail: List[str]) -> None:
        state[node] = 1
        deps = by_id[node].get("depends_on")
        for dep in deps if isinstance(deps, list) else []:
            if dep not in by_id:
                continue
            if state.get(dep) == 1:
                problems.append("task dependency cycle: " + " -> ".join(trail + [node, dep]))
            elif dep not in state:
                visit(dep, trail + [node])
        state[node] = 2

    for tid in sorted(by_id):
        if tid not in state:
            visit(tid, [])
    return problems


def expired_leases(doc: Dict[str, Any], now: _dt.datetime) -> List[Dict[str, str]]:
    """Active tasks whose lease has run out at `now` (an aware datetime supplied by the caller)."""
    out = []
    for t in doc.get("tasks", []):
        if t.get("status") in ACTIVE_STATUSES and isinstance(t.get("lease"), dict):
            exp = parse_time(t["lease"]["expires"])
            if exp <= now:
                out.append({"id": t["id"], "owner": t["owner"], "expired": t["lease"]["expires"]})
    return out
