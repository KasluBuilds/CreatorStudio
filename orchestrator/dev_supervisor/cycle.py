"""Cycle tracker: where is a task in the task-to-merge chain, and what is the next step? (T-DEV-057)

    python -B -m dev_supervisor.cycle <task-id> [--repo ROOT] [--base origin/main] [--json]

Read-only. It reads ACTIVE_TASKS.json as of ``--base`` (default ``origin/main``, as of the last fetch: it never
fetches), the task's pull requests through ``gh pr list`` and, for an open pull request, the integrate gates
(``integrate.evaluate``, unchanged). It writes nothing, merges nothing and never raises on bad data.

Stages, in chain order::

    unregistered         no task record on the base ref                        -> draft a spec and claim it
    awaiting_work        registered, no pushed branch and no pull request      -> implement or dispatch a worker
    branch_pushed        branch on origin, no pull request                     -> open the pull request (intake)
    in_review            open pull request, the review gate is the missing one -> independent review of the diff hash
    waiting_ci           open pull request, only CI / merge state outstanding  -> wait; update the branch if it is behind
    needs_fix            open pull request, a gate other than review/CI fails  -> fix the named gates
    ready_to_merge       open pull request, every gate PASS                    -> integrate --yes
    awaiting_closeout    a pull request merged, task still ACTIVE              -> registry-only close-out on the task branch
    done                 task COMPLETED or RELEASED
    unknown              something could not be read; the reason is stated, never guessed

Every evidence line is labelled OBSERVED (read by this command just now). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from . import integrate, memory_guard

__all__ = ["track", "main", "STAGES"]

STAGES = ("unregistered", "awaiting_work", "branch_pushed", "in_review", "waiting_ci", "needs_fix", "ready_to_merge",
          "awaiting_closeout", "done", "unknown")
DONE_STATUSES = ("COMPLETED", "RELEASED")
WORK_STATUSES = ("PLANNED", "ACTIVE", "UNDER_REVIEW")
_CI_GATES = {"checks", "merge_state"}


def _ev(text: str) -> Dict[str, str]:
    return {"label": "OBSERVED", "text": text}


def _result(task_id: str, stage: str, evidence: List[Dict[str, str]], next_action: str, pr: Optional[int] = None) -> Dict[str, Any]:
    return {"task_id": task_id, "stage": stage, "pr": pr, "evidence": evidence, "next_action": next_action}


def _unknown(task_id: str, why: str, evidence: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    return _result(task_id, "unknown", list(evidence or []) + [_ev(why)], "resolve the reason above; nothing was guessed")


def _json_list(text: str) -> Optional[List[Any]]:
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, list) else None


def classify(gates: Sequence[Any]) -> Tuple[str, str]:
    """(stage, description) for an open pull request from its gate results."""
    bad = [g for g in gates if g.status != integrate.PASS]
    if not gates:
        return "unknown", "no gate result"
    if not bad:
        return "ready_to_merge", "all gates PASS"
    names = {g.name for g in bad}
    if names - _CI_GATES - {"review"}:
        return "needs_fix", "failing gates: " + ", ".join(sorted(names - _CI_GATES - {"review"}))
    if "review" in names:
        return "in_review", "the review gate is not satisfied" + (" (CI also outstanding)" if names & _CI_GATES else "")
    checks = next((g for g in bad if g.name == "checks"), None)
    if checks is not None and ("pending" in checks.detail or "not reported" in checks.detail):
        return "waiting_ci", "CI has not finished on the head commit"
    return "needs_fix", "failing gates: " + ", ".join(sorted(names)) + " (a behind branch shows as merge_state)"


def track(task_id: str, gh: Callable, git: Callable, *, base: str = "origin/main", pr_base: str = "main",
          evaluate: Callable = integrate.evaluate, free_memory: Callable = memory_guard.free_memory_mb) -> Dict[str, Any]:
    """The cycle record for ``task_id``. Never raises."""
    try:
        return _track(task_id, gh, git, base, pr_base, evaluate, free_memory)
    except Exception as exc:                                                       # a tracker must not crash its caller
        return _unknown(str(task_id), f"unexpected {type(exc).__name__} while reading state")


def _track(task_id: str, gh: Callable, git: Callable, base: str, pr_base: str, evaluate: Callable,
           free_memory: Callable) -> Dict[str, Any]:
    rc, raw = git(["show", f"{base}:ACTIVE_TASKS.json"])
    if rc != 0:
        return _unknown(task_id, f"cannot read ACTIVE_TASKS.json at {base}")
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
        tasks = [t for t in doc["tasks"] if isinstance(t, dict)]
    except (ValueError, KeyError, TypeError):
        return _unknown(task_id, f"ACTIVE_TASKS.json at {base} is malformed")
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if task is None:
        return _result(task_id, "unregistered", [_ev(f"no task {task_id} in ACTIVE_TASKS.json at {base}")],
                       "draft a spec (dev_supervisor.specdraft for a milestone), then dev_supervisor.claim")
    status, branch = str(task.get("status")), task.get("branch")
    ev = [_ev(f"registry at {base}: status {status}, branch {branch}")]
    if status in DONE_STATUSES:
        return _result(task_id, "done", ev, "select the next task (autonomy next)")
    if status not in WORK_STATUSES or not isinstance(branch, str) or not branch:
        return _unknown(task_id, f"status {status} is not a working status or the branch is missing", ev)

    rc, out = gh(["pr", "list", "--head", branch, "--state", "all", "--limit", "20", "--json", "number,state,mergedAt,title"], None)
    prs = _json_list(out) if rc == 0 else None
    if prs is None:
        return _unknown(task_id, "gh pr list failed or returned an unreadable answer", ev)
    prs = [p for p in prs if isinstance(p, dict) and isinstance(p.get("number"), int)]
    ev.append(_ev(f"pull requests from {branch}: " + (", ".join(f"#{p['number']} {p.get('state')}" for p in prs) or "none")))
    opened = [p for p in prs if p.get("state") == "OPEN"]
    if opened:
        n = max(p["number"] for p in opened)
        gates, info = evaluate(n, gh, git, pr_base)
        stage, why = classify(gates)
        ev.append(_ev(f"PR #{n} gates: " + "; ".join(f"{g.num}:{g.name}={g.status}" for g in gates)))
        ev.append(_ev(why))
        nxt = {
            "in_review": f"independent review of PR #{n} diff {info.get('diff_sha') or '?'}: fresh read-only reviewer, post the dev-review marker",
            "waiting_ci": f"wait for CI on PR #{n}; if the branch is behind main, merge origin/main into it",
            "needs_fix": f"fix the failing gates on PR #{n}, then re-run integrate (dry run)",
            "ready_to_merge": f"python -B -m dev_supervisor.integrate {n} --yes",
            "unknown": "gate results were not readable",
        }[stage]
        return _result(task_id, stage, ev, nxt, n)
    merged = [p for p in prs if p.get("state") == "MERGED"]
    if merged:
        n = max(p["number"] for p in merged)
        return _result(task_id, "awaiting_closeout", ev + [_ev(f"PR #{n} is merged and the task is still {status}")],
                       "registry-only close-out pull request from the task's own branch (status COMPLETED, lease released)", n)
    rc, _ = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])
    if rc == 0:
        return _result(task_id, "branch_pushed", ev + [_ev(f"origin/{branch} exists (as of the last fetch)")],
                       "open the pull request: dev_supervisor.intake for a worker's branch, else gh pr create")
    free = free_memory()
    mem = f"free memory {free} MB, dispatch floor {memory_guard.min_free_mb()} MB" if isinstance(free, int) else "free memory unreadable"
    return _result(task_id, "awaiting_work", ev + [_ev(f"no pushed branch and no pull request; {mem}")],
                   "implement it, or dispatch a worker (dev_supervisor assign ... --allocation-role implementation); "
                   "the memory guard refuses a dispatch below the floor")


def main(argv: Optional[Sequence[str]] = None, *, out: Optional[TextIO] = None,
         gh: Optional[Callable] = None, git: Optional[Callable] = None) -> int:
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="dev_supervisor.cycle", description="Where is a task in the task-to-merge chain? (read-only)")
    p.add_argument("task_id")
    p.add_argument("--repo", help="repository checkout (default: this one)")
    p.add_argument("--base", default="origin/main", help="ref holding ACTIVE_TASKS.json (default %(default)s)")
    p.add_argument("--json", action="store_true")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    root = Path(a.repo) if a.repo else Path(__file__).resolve().parents[2]
    rec = track(a.task_id, gh or integrate.real_gh_runner(root), git or integrate.real_git_runner(root), base=a.base)
    if a.json:
        out.write(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    else:
        out.write(f"{rec['task_id']}: {rec['stage']}" + (f" (PR #{rec['pr']})" if rec["pr"] else "") + "\n")
        for e in rec["evidence"]:
            out.write(f"  [{e['label']}] {e['text']}\n")
        out.write(f"  next: {rec['next_action']}\n")
    return 3 if rec["stage"] == "unknown" else 0


if __name__ == "__main__":
    sys.exit(main())
