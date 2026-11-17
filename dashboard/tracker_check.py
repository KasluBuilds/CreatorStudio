"""Validation of DEVELOPMENT_TRACKER.json: step structure, task linkage, transitions and the completion gate.
The working-step limit (at most one IN_PROGRESS or READY_FOR_REVIEW step) and the in-order rule apply per task.

Standard library only. Read-only: it never writes any project file. The tracker records *steps*; it must not
duplicate milestone states or percentages (PROJECT_STATUS.json) or task ownership and leases (ACTIVE_TASKS.json).
Usage from the repository root:
    python -B dashboard/tracker_check.py [--root DIR] [--against REF]
Without --against it validates the current file only. With --against REF (for example origin/main), REF must be
an ancestor of HEAD. The checker finds the branch fork point shared with REF, then reads every committed
DEVELOPMENT_TRACKER.json snapshot on the first-parent path from that point to HEAD (`git show`, read-only), in
order, and compares the working-tree file with HEAD. It rejects every illegal intermediate transition, an edit to a
COMPLETED step, or a removed step; later commits cannot hide an earlier violation. Exit codes: 0 ok, 1 problems
found, 2 files, REF or git unusable.
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUSES = ("PLANNED", "IN_PROGRESS", "READY_FOR_REVIEW", "COMPLETED", "BLOCKED")
# Allowed moves. COMPLETED is terminal. READY_FOR_REVIEW -> IN_PROGRESS means the reviewer asked for changes.
TRANSITIONS = {
    "PLANNED": {"IN_PROGRESS", "BLOCKED"},
    "IN_PROGRESS": {"READY_FOR_REVIEW", "BLOCKED"},
    "READY_FOR_REVIEW": {"COMPLETED", "IN_PROGRESS", "BLOCKED"},
    "BLOCKED": {"PLANNED", "IN_PROGRESS"},
    "COMPLETED": set(),
}
WORKING = ("IN_PROGRESS", "READY_FOR_REVIEW")  # at most one such step per task (different tasks may each have one)
APPROVERS = ("chatgpt", "repository_coordinator", "lukas")  # who technically approved a COMPLETED step
EVIDENCE_TYPES = ("commit", "pull_request", "test_run", "observation", "document")
# Keys that would duplicate another authoritative file. Rejected at any depth.
FORBIDDEN_KEYS = {"weight", "weights", "percent", "percentage", "implementation_percent", "verification_percent",
                  "milestones", "milestone_state", "computed", "lease", "lease_expires", "owned_paths", "owner"}
STEP_ID = re.compile(r"^S-[A-Z0-9]+(-[A-Z0-9]+)*-\d{2}$")
TASK_ID = re.compile(r"^T-[A-Z0-9]+-\d{3}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Private local paths and obvious secrets must never be recorded (the tracker is published in a snapshot).
LOCAL_PATH = re.compile(r"((?<![A-Za-z0-9])[A-Za-z]:[\\/](?!/)|/Users/|/home/|\\\\)")
SECRET = re.compile(r"((^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY)")


def _walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}/{k}", k, v
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


def _strs(x: Any) -> bool:
    return isinstance(x, list) and all(isinstance(i, str) and i.strip() for i in x)


def can_transition(old: str, new: str) -> bool:
    return new in TRANSITIONS.get(old, set())


def completion_problems(step: Dict[str, Any]) -> List[str]:
    """The completion gate: what a step needs before it may be COMPLETED (or stay so)."""
    sid = step.get("id", "?")
    out: List[str] = []
    if not step.get("evidence"):
        out.append(f"{sid}: a COMPLETED step needs at least one evidence record")
    c = step.get("completion")
    if not isinstance(c, dict):
        return out + [f"{sid}: a COMPLETED step needs a 'completion' record"]
    approver = c.get("approved_by")
    if approver is not None and approver not in APPROVERS:
        out.append(f"{sid}: completion approved_by must be one of {list(APPROVERS)}")
    elif approver == "chatgpt" and c.get("reviewed_by_chatgpt") is not True:
        out.append(f"{sid}: completion approved_by chatgpt needs reviewed_by_chatgpt true")
    elif approver is None and c.get("reviewed_by_chatgpt") is not True:
        out.append(f"{sid}: completion needs reviewed_by_chatgpt true (or approved_by repository_coordinator or lukas)")
    elif approver in ("repository_coordinator", "lukas") and c.get("reviewed_by_chatgpt") is True:
        out.append(f"{sid}: completion must not claim reviewed_by_chatgpt when approved_by is {approver}")
    if c.get("lukas_confirmed") is not True:
        out.append(f"{sid}: completion needs lukas_confirmed true (Lukas's confirmation: Lukas's direct word, or Lukas's standing instruction to ChatGPT recorded with a REPORTED source; see DEVELOPMENT_STEP_PROTOCOL.md section 3a)")
    if not (isinstance(c.get("date"), str) and DATE.match(c["date"])):
        out.append(f"{sid}: completion needs a YYYY-MM-DD date")
    if not (isinstance(c.get("source"), str) and c["source"].strip()):
        out.append(f"{sid}: completion needs a source saying where Lukas's confirmation came from")
    elif not re.match(r"(DIRECT|REPORTED)\b", c["source"]):
        out.append(f"{sid}: completion source must begin with DIRECT (Lukas said so in the session) or REPORTED "
                   "(relayed, or Lukas's standing instruction); see DEVELOPMENT_STEP_PROTOCOL.md section 3a")
    return out


def transition_problems(old_doc: Optional[Dict[str, Any]], new_doc: Dict[str, Any]) -> List[str]:
    """Compare the tracker on the base branch with the new one: no skipped states, no edits to finished steps."""
    if not old_doc:
        return []
    old = {s["id"]: s for t in old_doc.get("tasks", []) for s in t.get("steps", []) if isinstance(s, dict) and "id" in s}
    out: List[str] = []
    new_ids = set()
    for t in new_doc.get("tasks", []):
        for s in t.get("steps", []):
            if not isinstance(s, dict) or "id" not in s:
                continue
            new_ids.add(s["id"])
            o = old.get(s["id"])
            if o is None:
                continue
            if o["status"] == "COMPLETED":
                if s != o:
                    out.append(f"{s['id']}: a COMPLETED step must not change")
            elif s["status"] != o["status"] and not can_transition(o["status"], s["status"]):
                out.append(f"{s['id']}: transition {o['status']} -> {s['status']} is not allowed")
    for sid in old:
        if sid not in new_ids:
            out.append(f"{sid}: steps may not be removed (mark BLOCKED with a reason instead)")
    return out


def validate(doc: Any, active_tasks: Optional[Dict[str, Any]] = None) -> List[str]:
    problems: List[str] = []
    if not isinstance(doc, dict):
        return ["tracker must be a JSON object"]
    if doc.get("schema_version") != 1:
        problems.append("schema_version must be 1")
    for field in ("authority", "protocol"):
        if not (isinstance(doc.get(field), str) and doc[field].strip()):
            problems.append(f"'{field}' must be a non-empty string")
    for path, key, value in _walk(doc):
        if key.lower() in FORBIDDEN_KEYS:
            problems.append(f"{path}: key '{key}' duplicates another authoritative file (PROJECT_STATUS.json / ACTIVE_TASKS.json)")
        if isinstance(value, str):
            if LOCAL_PATH.search(value):
                problems.append(f"{path}: contains a private local path")
            if SECRET.search(value):
                problems.append(f"{path}: looks like a credential")
    tasks = doc.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return problems + ["'tasks' must be a non-empty list"]
    known = None
    if active_tasks is not None:
        known = {t.get("id") for t in active_tasks.get("tasks", []) if isinstance(t, dict)}
    seen_steps = set()
    for t in tasks:
        tid = t.get("task_id") if isinstance(t, dict) else None
        if not (isinstance(tid, str) and TASK_ID.match(tid)):
            problems.append(f"task entry needs a task_id like T-GOV-002 (got {tid!r})")
            continue
        if known is not None and tid not in known:
            problems.append(f"{tid}: not registered in ACTIVE_TASKS.json")
        steps = t.get("steps")
        if not isinstance(steps, list) or not steps:
            problems.append(f"{tid}: needs a non-empty 'steps' list")
            continue
        prev_complete = True
        working: List[str] = []  # the limit is per task, so it restarts for every task
        for idx, s in enumerate(steps):
            sid = s.get("id") if isinstance(s, dict) else None
            if not (isinstance(sid, str) and STEP_ID.match(sid)):
                problems.append(f"{tid}: step {idx} needs an id like S-GOV-002-00 (got {sid!r})")
                continue
            if sid in seen_steps:
                problems.append(f"{sid}: duplicate step id")
            seen_steps.add(sid)
            if sid.rsplit("-", 1)[1] != f"{idx:02d}":
                problems.append(f"{sid}: steps are numbered in order starting at 00 (position {idx})")
            if not (isinstance(s.get("title"), str) and s["title"].strip()):
                problems.append(f"{sid}: needs a title")
            if s.get("status") not in STATUSES:
                problems.append(f"{sid}: status must be one of {list(STATUSES)}")
                continue
            if not s.get("acceptance_criteria") or not _strs(s.get("acceptance_criteria")):
                problems.append(f"{sid}: needs non-empty acceptance_criteria")
            ev = s.get("evidence")
            if not isinstance(ev, list):
                problems.append(f"{sid}: evidence must be a list")
                ev = []
            for e in ev:
                if not (isinstance(e, dict) and e.get("type") in EVIDENCE_TYPES and isinstance(e.get("summary"), str)
                        and e["summary"].strip()):
                    problems.append(f"{sid}: evidence needs a type {list(EVIDENCE_TYPES)} and a summary")
                    continue
                if e["type"] in ("commit", "pull_request") and not SHA.match(str(e.get("commit", ""))):
                    problems.append(f"{sid}: {e['type']} evidence needs a full 40-character 'commit'")
                if e["type"] == "test_run" and not (e.get("command") and e.get("result")):
                    problems.append(f"{sid}: test_run evidence needs command and result")
            st = s["status"]
            if st == "BLOCKED" and not (isinstance(s.get("blocked_reason"), str) and s["blocked_reason"].strip()):
                problems.append(f"{sid}: a BLOCKED step needs a blocked_reason")
            if st in WORKING:
                working.append(sid)
            if st == "COMPLETED":
                problems.extend(completion_problems(s))
            elif s.get("completion") is not None:
                problems.append(f"{sid}: only a COMPLETED step may carry a 'completion' record")
            # Sequential gate: nothing starts or finishes until every earlier step is COMPLETED.
            if st not in ("PLANNED", "BLOCKED") and not prev_complete:
                problems.append(f"{sid}: {st} while an earlier step is not COMPLETED (steps run one at a time, in order)")
            prev_complete = prev_complete and st == "COMPLETED"
        if len(working) > 1:
            problems.append(f"{tid}: only one step may be IN_PROGRESS or READY_FOR_REVIEW at a time per task (found {working})")
    return sorted(set(problems))


def load(root: Path):
    doc = json.loads((root / "DEVELOPMENT_TRACKER.json").read_text(encoding="utf-8"))
    at = json.loads((root / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
    return doc, at


class BaseError(Exception):
    """The base ref could not be used (unknown ref, git missing, unreadable tracker)."""


def load_base(root: Path, ref: str) -> Optional[Dict[str, Any]]:
    """The tracker as committed at `ref`, or None if that commit has no tracker file. Read-only (rev-parse, cat-file, show)."""
    if not ref or ref.startswith("-"):
        raise BaseError(f"invalid ref {ref!r}")

    def git(*args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                                  timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BaseError(f"git unavailable: {exc}")

    if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise BaseError(f"unknown ref {ref!r}")
    spec = f"{ref}:DEVELOPMENT_TRACKER.json"
    if git("cat-file", "-e", spec).returncode != 0:
        return None
    shown = git("show", spec)
    if shown.returncode != 0:
        raise BaseError(f"cannot read {spec}")
    try:
        return json.loads(shown.stdout)
    except ValueError as exc:
        raise BaseError(f"{spec} is not valid JSON: {exc}")


def transition_history_problems(root: Path, ref: str, current_doc: Dict[str, Any]) -> List[str]:
    """Check each committed tracker snapshot from the branch fork point through HEAD.

    The supplied base ref must be an ancestor of HEAD (the branch is up to date). If
    main was merged into the task branch, the fork point is the nearest first-parent
    commit that is also an ancestor of the base ref. This avoids comparing an older
    task-branch snapshot directly with a newer main snapshot and catches intermediate
    transitions even when a later file would hide them.
    """
    def git(*args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BaseError(f"git unavailable: {exc}")

    if not ref or ref.startswith("-"):
        raise BaseError(f"invalid ref {ref!r}")
    base = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    head = git("rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if base.returncode != 0 or head.returncode != 0:
        raise BaseError(f"cannot resolve history from {ref!r} to HEAD")
    base_sha, head_sha = base.stdout.strip(), head.stdout.strip()
    if git("merge-base", "--is-ancestor", base_sha, head_sha).returncode != 0:
        raise BaseError(f"{ref!r} is not an ancestor of HEAD; update the branch before checking")

    first_parent = git("rev-list", "--first-parent", head_sha)
    if first_parent.returncode != 0:
        raise BaseError("cannot read first-parent history from HEAD")
    fork_base = None
    for candidate in first_parent.stdout.split():
        ancestry = git("merge-base", "--is-ancestor", candidate, base_sha)
        if ancestry.returncode == 0:
            fork_base = candidate
            break
        if ancestry.returncode != 1:
            raise BaseError(f"cannot determine branch fork point for {ref!r}")
    if fork_base is None:
        raise BaseError(f"cannot find a first-parent fork point shared with {ref!r}")

    previous = load_base(root, fork_base)
    commits = git("rev-list", "--first-parent", "--reverse", f"{fork_base}..{head_sha}")
    if commits.returncode != 0:
        raise BaseError(f"cannot read first-parent history from {fork_base} to HEAD")

    problems: List[str] = []
    for sha in commits.stdout.split():
        spec = f"{sha}:DEVELOPMENT_TRACKER.json"
        exists = git("cat-file", "-e", spec)
        if exists.returncode != 0:
            if previous is not None:
                raise BaseError(f"{sha}: DEVELOPMENT_TRACKER.json disappeared from history")
            continue
        shown = git("show", spec)
        if shown.returncode != 0:
            raise BaseError(f"cannot read {spec}")
        try:
            snapshot = json.loads(shown.stdout)
        except ValueError as exc:
            raise BaseError(f"{spec} is not valid JSON: {exc}")
        if previous is not None:
            problems.extend(transition_problems(previous, snapshot))
        previous = snapshot

    # Also compare the working-tree file with HEAD. This keeps local --against checks
    # useful before a commit, while CI normally sees an identical HEAD/worktree snapshot.
    if previous is not None and previous != current_doc:
        problems.extend(transition_problems(previous, current_doc))
    return sorted(set(problems))

def main(argv: List[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    against = None
    try:
        if "--root" in argv:
            root = Path(argv[argv.index("--root") + 1])
        if "--against" in argv:
            against = argv[argv.index("--against") + 1]
    except IndexError:
        print("ERROR: --root and --against need a value", file=sys.stderr)
        return 2
    try:
        doc, at = load(root)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    problems = validate(doc, at)
    note = ""
    if against is not None:
        try:
            base = load_base(root, against)
        except BaseError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if base is None:
            note = f" (no tracker at {against}: nothing to compare)"
        else:
            try:
                history_problems = transition_history_problems(root, against, doc)
            except BaseError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            problems = sorted(set(problems + history_problems))
            note = f" (transitions checked against {against} across first-parent history)"
    for p in problems:
        print("PROBLEM:", p)
    if not problems:
        print("OK: DEVELOPMENT_TRACKER.json is valid" + note)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
