"""Draft a claim spec for a proposed milestone (T-DEV-055, phase 1 of the autonomy mission).

    python -B -m dev_supervisor.specdraft <milestone-id> [--repo ROOT]

Read-only: it reads PROJECT_STATUS.json and ACTIVE_TASKS.json, prints one JSON object and writes nothing, runs no git or
gh command and registers no task. The output is the starting point of a claim spec for ``dev_supervisor.claim``:

* copied verbatim from the milestone record: ``title`` and ``description``;
* derived by the repository's naming rules: ``id`` (next free number for the milestone's area), ``branch``,
  ``worktree``, ``agreement_id``;
* the protocol's own checks as ``test_requirements`` (registry validation, merge preflight, independent review);
* everything else is NOT invented. ``owned_paths``, ``acceptance_criteria`` and ``agreement`` are listed under
  ``missing`` and stay absent, so the draft is rejected by ``claim`` until someone who knows the answer supplies them.

``status`` is ``draft`` (nothing blocks a claim except the missing fields), ``needs_decision`` (the milestone carries
open questions or blockers: they are printed, and no task should be registered before they are answered) or ``refused``
(unknown milestone, or not eligible: already started, a dependency not implemented, or an open task mentions it).
Exit codes: 0 draft, 3 needs_decision, 2 refused or usage error.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TextIO

from . import autonomy

__all__ = ["draft", "main"]

EXIT_DRAFT, EXIT_REFUSED, EXIT_NEEDS_DECISION = 0, 2, 3
MISSING = ["owned_paths", "acceptance_criteria", "agreement"]
TEST_REQUIREMENTS = [
    "python -B -m governance validate from orchestrator/",
    "integrate --preflight passes on the branch",
    "independent review of the exact diff",
]
_AREA = re.compile(r"^M-([A-Z0-9]+)-\d+$")
_SLUG_MAX = 40


def _load(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    s = s[:_SLUG_MAX].rstrip("-")
    return s or "task"


def _impl_state(m: Dict[str, Any]) -> str:
    impl = m.get("implementation")
    return str(impl.get("state")) if isinstance(impl, dict) else "UNKNOWN"


def _refusal_reasons(m: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> List[str]:
    reasons: List[str] = []
    if _impl_state(m) != "NOT_STARTED":
        reasons.append(f"implementation state is {_impl_state(m)}, not NOT_STARTED")
    deps = m.get("depends_on") or []
    if not isinstance(deps, list):
        reasons.append("depends_on is malformed")
    else:
        for d in deps:
            if not isinstance(d, str) or d not in by_id:
                reasons.append(f"dependency {d!r} is not a known milestone")
            elif _impl_state(by_id[d]) != "IMPLEMENTED":
                reasons.append(f"dependency {d} is {_impl_state(by_id[d])}, not IMPLEMENTED")
    return reasons


def draft(root: Path, milestone_id: str) -> Dict[str, Any]:
    """The draft record for ``milestone_id`` (never raises for bad data: that is a ``refused`` record)."""
    def refused(*why: str) -> Dict[str, Any]:
        return {"milestone_id": milestone_id, "status": "refused", "reasons": list(why)}

    try:
        status_doc = _load(root / "PROJECT_STATUS.json")
        registry = _load(root / "ACTIVE_TASKS.json")
        milestones = [m for m in status_doc["milestones"] if isinstance(m, dict) and isinstance(m.get("id"), str)]
        tasks = [t for t in registry["tasks"] if isinstance(t, dict)]
    except (OSError, ValueError, KeyError, TypeError):
        return refused("PROJECT_STATUS.json or ACTIVE_TASKS.json is missing or malformed")
    by_id = {m["id"]: m for m in milestones}
    m = by_id.get(milestone_id)
    if m is None:
        return refused("unknown milestone id")
    area = _AREA.match(milestone_id)
    if area is None:
        return refused("the milestone id does not look like M-AREA-NN, so no task id can be derived")

    warnings: List[str] = []
    proposed = {p["milestone_id"] for p in autonomy._milestone_proposals(root, warnings)}
    if milestone_id not in proposed:
        reasons = _refusal_reasons(m, by_id)
        reasons += warnings
        if not reasons:
            reasons.append("an open task in ACTIVE_TASKS.json already mentions this milestone")
        return refused(*reasons)

    title, description = m.get("title"), m.get("description")
    if not (isinstance(title, str) and title.strip() and isinstance(description, str) and description.strip()):
        return refused("the milestone has no title or description to copy")

    prefix = area.group(1)
    numbers = [int(x.group(1)) for t in tasks
               for x in [re.match(rf"^T-{re.escape(prefix)}-(\d{{3}})$", str(t.get("id", "")))] if x]
    number = max(numbers, default=0) + 1
    if number > 999:
        return refused(f"no free three-digit task number in area {prefix}")
    task_id = f"T-{prefix}-{number:03d}"
    branch = f"task/{task_id.lower()}-{_slug(title)}"
    used_branches = {t.get("branch") for t in tasks}
    if branch in used_branches:
        return refused(f"the derived branch {branch} is already registered")
    questions = [str(q) for q in (m.get("open_questions") or [])] if isinstance(m.get("open_questions"), list) else [
        "open_questions is malformed in PROJECT_STATUS.json"]
    blockers = [str(b) for b in (m.get("blockers") or [])] if isinstance(m.get("blockers"), list) else [
        "blockers is malformed in PROJECT_STATUS.json"]
    return {
        "milestone_id": milestone_id,
        "status": "needs_decision" if (questions or blockers) else "draft",
        "reasons": [],
        "draft": {
            "id": task_id,
            "title": title.strip(),
            "description": description.strip(),
            "branch": branch,
            "worktree": f"../CreatorStudio-worktrees/{branch[len('task/'):]}",
            "agreement_id": f"AGR-{prefix}-{number:03d}-01",
            "test_requirements": list(TEST_REQUIREMENTS),
            "integration_notes": f"Drafted by dev_supervisor.specdraft from milestone {milestone_id}; "
                                 "owned paths, acceptance criteria and the agreement text still have to be supplied by whoever claims it.",
        },
        "missing": list(MISSING),
        "open_questions": questions,
        "blockers": blockers,
        "note": "owned_paths, acceptance_criteria and agreement are deliberately absent: they are decisions, not "
                "derivable facts. The claim command rejects this draft until they are supplied.",
    }


def main(argv: Optional[Sequence[str]] = None, *, out: Optional[TextIO] = None) -> int:
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="dev_supervisor.specdraft", description="Draft a claim spec for a milestone (read-only)")
    p.add_argument("milestone_id")
    p.add_argument("--repo", help="repository checkout holding PROJECT_STATUS.json (default: this one)")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_REFUSED if e.code else EXIT_DRAFT
    root = Path(a.repo) if a.repo else Path(__file__).resolve().parents[2]
    rec = draft(root, a.milestone_id)
    out.write(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    return {"draft": EXIT_DRAFT, "needs_decision": EXIT_NEEDS_DECISION}.get(rec["status"], EXIT_REFUSED)


if __name__ == "__main__":
    sys.exit(main())
