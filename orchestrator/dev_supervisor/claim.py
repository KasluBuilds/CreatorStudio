"""Claim command (T-DEV-045): register ONE task from a spec file and apply the claim-PR rules.

    python -B -m dev_supervisor.claim <spec.json> [--repo DIR] [--write]

Why: a task must be claimed in ACTIVE_TASKS.json before work starts, through a registry-only pull request, and the
rules for that pull request were learned by failing. This module encodes the ones that can be decided from the spec:

* exactly one task and one agreement per claim; the task id and the agreement id are new;
* the registered work branch starts with ``task/`` and is not used by another task;
* the worktree and every owned path are repo-relative (the governance path rules);
* the agreement text names no other task id (the integration tool's ownership rule scans it);
* nothing in the spec looks like a credential;
* the result passes the governance registry validation, and SESSION_HANDOFF.md is regenerated exactly as
  ``governance handoff --write`` does, so CI's ``handoff --check`` passes.

Dry run by default: it prints what it would add and writes nothing. ``--write`` edits ACTIVE_TASKS.json (line endings
preserved) and SESSION_HANDOFF.md of the given checkout. It never runs git or gh, never commits or pushes and never
touches the network: committing on a ``task/...-claim`` branch, running ``integrate --preflight`` and opening the pull
request stay separate, visible steps.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TextIO, Tuple

from governance import handoff as handoff_mod
from governance import tasks as tasks_mod

from . import integrate, testreq

__all__ = ["check_spec", "build_records", "claim", "main"]

TASK_ID = re.compile(r"^T-[A-Z0-9]+-\d{3}$")
AGREEMENT_ID = re.compile(r"^AGR-[A-Z0-9-]+-\d{2}$")
STR_FIELDS = ("id", "title", "description", "branch", "worktree", "agreement_id", "agreement", "integration_notes")
LIST_FIELDS = ("owned_paths", "acceptance_criteria", "test_requirements")
OPTIONAL_FIELDS = ("owner", "depends_on")
DEFAULT_OWNER = "claude:main-coordinator"
LEASE_HOURS = 24
REGISTRY = "ACTIVE_TASKS.json"
STATUS = "PROJECT_STATUS.json"
HANDOFF = "SESSION_HANDOFF.md"


def _nonempty(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _lines(spec: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for v in spec.values():
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out.extend(x for x in v if isinstance(x, str))
    return out


def check_spec(spec: Any, doc: Dict[str, Any]) -> List[str]:
    """Rule violations of ``spec`` against the registry ``doc`` (empty = the spec may be claimed). Pure."""
    if not isinstance(spec, dict):
        return ["the spec must be a JSON object"]
    probs: List[str] = []
    for k in sorted(set(spec) - set(STR_FIELDS) - set(LIST_FIELDS) - set(OPTIONAL_FIELDS)):
        probs.append(f"unknown field {k!r}")
    for k in STR_FIELDS:
        if not _nonempty(spec.get(k)):
            probs.append(f"{k} is required and must be a non-empty string")
    for k in LIST_FIELDS:
        v = spec.get(k)
        if not (isinstance(v, list) and v and all(_nonempty(x) for x in v)):
            probs.append(f"{k} is required and must be a non-empty list of non-empty strings")
    if "owner" in spec and not (isinstance(spec["owner"], str) and tasks_mod.OWNER.match(spec["owner"])):
        probs.append("owner must look like claude:<name>")
    if "depends_on" in spec and not (isinstance(spec["depends_on"], list) and all(_nonempty(x) for x in spec["depends_on"])):
        probs.append("depends_on must be a list of task ids")
    if probs:
        return probs        # later rules assume well-formed fields

    tasks = [t for t in doc.get("tasks", []) if isinstance(t, dict)]
    agreements = doc.get("integration_agreements") if isinstance(doc.get("integration_agreements"), dict) else {}
    if not TASK_ID.match(spec["id"]):
        probs.append(f"id {spec['id']!r} must look like T-XXX-000")
    elif any(t.get("id") == spec["id"] for t in tasks):
        probs.append(f"task {spec['id']} is already registered; claims are one new task each")
    if not AGREEMENT_ID.match(spec["agreement_id"]):
        probs.append(f"agreement_id {spec['agreement_id']!r} must look like AGR-XXX-00")
    elif spec["agreement_id"] in agreements:
        probs.append(f"agreement {spec['agreement_id']} already exists")
    if not spec["branch"].startswith("task/"):
        probs.append("the registered work branch must start with task/ (the merge gate refuses other prefixes)")
    elif any(t.get("branch") == spec["branch"] for t in tasks):
        probs.append(f"branch {spec['branch']} is already registered to another task")
    wt = spec["worktree"]
    segments = wt.split("/")
    if ("\\" in wt or wt.startswith("/") or re.match(r"^[A-Za-z]:", wt) or ".." in segments[1:]
            or any(s in ("", ".") for s in segments)):
        probs.append("worktree must be a relative path with forward slashes")
    for p in spec["owned_paths"]:
        msg = tasks_mod.path_problem(p)
        if msg:
            probs.append("owned_paths: " + msg)
    agreement = {"description": spec["agreement"], "task_ids": [spec["id"]], "paths": list(spec["owned_paths"])}
    foreign = sorted(integrate._tids(agreement) - {spec["id"]}) if TASK_ID.match(spec["id"]) else []
    if foreign:
        probs.append("the agreement text names other task ids (" + ", ".join(foreign) + "): the integration tool would call "
                     "the claim 'not this PR's own'; say 'an earlier task' instead")
    if integrate._secret_hits(_lines(spec)):
        probs.append("a field looks like a credential; specs are committed to a public repository")
    for i, text in enumerate(spec["test_requirements"]):
        req = testreq.classify(text, i)
        if req.kind == "manual":
            probs.append(f"test_requirements[{i}] is not machine-executable ({req.reason}): {text[:80]}; write it as an exact command or the gate wording (see dev_supervisor/TEST_EVIDENCE.md)")
    return probs


def build_records(spec: Dict[str, Any], now: _dt.datetime) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(task record, agreement record) from a spec that passed ``check_spec``. Key order matches existing records."""
    expires = now + _dt.timedelta(hours=LEASE_HOURS)
    task = {
        "id": spec["id"], "title": spec["title"], "description": spec["description"],
        "owner": spec.get("owner", DEFAULT_OWNER), "branch": spec["branch"], "worktree": spec["worktree"],
        "status": "ACTIVE", "depends_on": list(spec.get("depends_on", [])), "exclusive_resources": [],
        "integration_agreements": [spec["agreement_id"]], "owned_paths": list(spec["owned_paths"]),
        "acceptance_criteria": list(spec["acceptance_criteria"]), "test_requirements": list(spec["test_requirements"]),
        "integration_notes": spec["integration_notes"],
        "lease": {"acquired": now.isoformat(), "expires": expires.isoformat()},
    }
    agreement = {"description": spec["agreement"], "task_ids": [spec["id"]], "paths": list(spec["owned_paths"])}
    return task, agreement


def claim(root: Path, spec: Any, write: bool = False, now: Optional[_dt.datetime] = None) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """(problems, task record). Writes ACTIVE_TASKS.json and SESSION_HANDOFF.md under ``root`` only when ``write`` is true
    and there are no problems; otherwise writes nothing. Never raises for bad input: problems are returned."""
    reg_path, status_path, handoff_path = root / REGISTRY, root / STATUS, root / HANDOFF
    try:
        raw = reg_path.read_bytes().decode("utf-8")
        doc = json.loads(raw)
        status_doc = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"cannot read {REGISTRY} and {STATUS} under {root}: {type(e).__name__}"], None
    if not isinstance(doc, dict) or not isinstance(doc.get("tasks"), list):
        return [f"{REGISTRY} has an unexpected shape"], None
    probs = check_spec(spec, doc)
    if probs:
        return probs, None
    now = (now or _dt.datetime.now(_dt.timezone.utc)).replace(microsecond=0)
    task, agreement = build_records(spec, now)
    new_doc = json.loads(raw)
    new_doc["tasks"].append(task)
    new_doc["integration_agreements"][spec["agreement_id"]] = agreement
    probs = ["registry: " + p for p in tasks_mod.validate(new_doc)]
    try:
        handoff_text = handoff_mod.render(status_doc, new_doc)
    except Exception as e:
        probs.append(f"the handoff cannot be rendered: {type(e).__name__}")
    if probs:
        return probs, None
    if write:
        text = json.dumps(new_doc, indent=2, ensure_ascii=False) + "\n"
        crlf = "\r\n" in raw
        old_handoff = None
        try:
            old_handoff = handoff_path.read_bytes() if handoff_path.is_file() else None
            handoff_path.write_text(handoff_text, encoding="utf-8", newline="\n")   # first: a failure here changes nothing
            reg_path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
        except OSError as e:
            try:
                if old_handoff is not None:
                    handoff_path.write_bytes(old_handoff)
            except OSError:
                pass
            return [f"writing failed ({type(e).__name__}); the handoff was restored where possible; "
                    "verify with: python -B -m governance handoff --check"], None
    return [], task


def main(argv: Optional[Sequence[str]] = None, out: Optional[TextIO] = None, now: Optional[_dt.datetime] = None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="dev_supervisor.claim", description="Register one task from a spec (dry run unless --write).")
    ap.add_argument("spec", help="path of the task spec JSON")
    ap.add_argument("--repo", default=".", help="checkout whose registry to edit (default: the current directory's repository root)")
    ap.add_argument("--write", action="store_true", help="edit ACTIVE_TASKS.json and SESSION_HANDOFF.md (default: dry run)")
    a = ap.parse_args(argv)
    try:
        spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"refused: cannot read the spec: {type(e).__name__}", file=out)
        return 2
    root = Path(a.repo).resolve()
    if not (root / REGISTRY).exists() and (root.parent / REGISTRY).exists():
        root = root.parent          # called from orchestrator/ (the default --repo is the current directory)
    probs, task = claim(root, spec, write=a.write, now=now)
    if probs:
        print("refused:", file=out)
        for p in probs:
            print("  - " + integrate._clean(p, 300), file=out)
        return 1
    print(("claimed " if a.write else "dry run, nothing written: would claim ") + task["id"] + " on branch " + task["branch"], file=out)
    print(f"  lease {task['lease']['acquired']} -> {task['lease']['expires']}; owned paths: {', '.join(task['owned_paths'])}", file=out)
    if a.write:
        print("next: commit ACTIVE_TASKS.json and SESSION_HANDOFF.md on a task/<name>-claim branch (one task per claim pull request), "
              "run `python -B -m dev_supervisor.integrate --preflight` and check its exit code, push, open the pull request.", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
