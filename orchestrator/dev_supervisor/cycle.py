"""Cycle tracker: where is a task in the task-to-merge chain, and what is the next step? (T-DEV-057)

    python -B -m dev_supervisor.cycle <task-id> [--repo ROOT] [--base origin/main] [--json]

Read-only. It reads ACTIVE_TASKS.json as of ``--base`` (default ``origin/main``, as of the last fetch: it never
fetches), the task's pull requests through ``gh pr list`` and, for an open pull request, the integrate gates
(``integrate.evaluate``, unchanged) plus integrate's ninth ``tests`` check (T-DEV-065). It writes nothing, merges nothing and never raises on bad data.

Stages, in chain order::

    unregistered         no task record on the base ref                        -> draft a spec and claim it
    awaiting_work        registered, no pushed branch and no pull request      -> implement or dispatch a worker
    branch_pushed        branch on origin, no pull request                     -> open the pull request (intake)
    in_review            open pull request, the review gate is the missing one -> independent review of the diff hash
    waiting_ci           open pull request, CI pending or merge state not yet computed -> wait
    needs_fix            open pull request, a gate fails (incl. red CI, a branch behind main) -> fix the named gates
    needs_tests          open pull request, the eight gates PASS, the tests check does not  -> testreq run in the task worktree
    ready_to_merge       open pull request, every gate PASS and the tests check PASS         -> integrate --yes
    worker_testing       no pull request yet; the Supervisor's test run for the worker's head is running or waiting -> wait
    worker_tests_failed  no pull request yet; that run failed or is blocked                                      -> fix, commit, it is re-tested
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
from .state_store import StateStore, StoreError, default_directory

__all__ = ["track", "main", "STAGES"]

STAGES = ("unregistered", "awaiting_work", "worker_testing", "worker_tests_failed", "branch_pushed", "in_review", "waiting_ci", "needs_fix", "needs_tests", "ready_to_merge",
          "awaiting_closeout", "done", "unknown")
DONE_STATUSES = ("COMPLETED", "RELEASED")
WORK_STATUSES = ("PLANNED", "ACTIVE", "UNDER_REVIEW")
_CI_GATES = {"checks", "merge_state"}
EXPECTED_GATES = ("pr_state", "checks", "merge_state", "scope", "review", "secrets", "forbidden_files", "reserved")


def _ev(text: str) -> Dict[str, str]:
    return {"label": "OBSERVED", "text": text}


def _result(task_id: str, stage: str, evidence: List[Dict[str, str]], next_action: str, pr: Optional[int] = None) -> Dict[str, Any]:
    return {"task_id": task_id, "stage": stage, "pr": pr, "evidence": evidence, "next_action": next_action}


def _unknown(task_id: str, why: str, evidence: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    return _result(task_id, "unknown", list(evidence or []) + [_ev(why)], "resolve the reason above; nothing was guessed")


def worker_tests_reader(state_dir: Optional[str] = None) -> Callable[[str], Optional[Dict[str, Any]]]:
    """task id -> the newest entry of the Supervisor's ``worker_tests`` document for it (None when absent or unreadable)."""
    def read(task_id: str) -> Optional[Dict[str, Any]]:
        try:
            directory = state_dir or str(default_directory())
            doc = StateStore(directory, create=False).read("worker_tests", default=None)
            entries = [e for e in (doc or {}).get("entries", {}).values() if isinstance(e, dict) and e.get("task_id") == task_id]
        except (StoreError, OSError, AttributeError, TypeError, ValueError):
            return None
        return max(entries, key=lambda e: str(e.get("updated") or ""), default=None)
    return read


def _json_list(text: str) -> Optional[List[Any]]:
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, list) else None


def classify(gates: Sequence[Any]) -> Tuple[str, str]:
    """(stage, description) for an open pull request from its gate results.

    ``ready_to_merge`` needs exactly the eight integrate gates, all PASS. A gate that could not be read (UNKNOWN) is
    never a failure and never a pass: on the CI side (checks, merge_state) it means "not yet", elsewhere the stage is
    ``unknown``."""
    by_name = {g.name: g for g in gates}
    if len(gates) != len(EXPECTED_GATES) or set(by_name) != set(EXPECTED_GATES):
        return "unknown", "the gate list is incomplete or has unexpected gates"
    bad = {n: g for n, g in by_name.items() if g.status != integrate.PASS}
    if not bad:
        return "ready_to_merge", "all gates PASS"
    other = {n: g for n, g in bad.items() if n not in _CI_GATES and n != "review"}
    fails = sorted(n for n, g in other.items() if g.status == integrate.FAIL)
    if fails:
        return "needs_fix", "failing gates: " + ", ".join(fails)
    unreadable = sorted(n for n, g in other.items() if g.status != integrate.FAIL)
    if unreadable:
        return "unknown", "gates that could not be read: " + ", ".join(unreadable)
    review = bad.get("review")
    if review is not None:
        if review.status == integrate.FAIL:
            return "in_review", "the review gate is not satisfied" + (" (CI also outstanding)" if set(bad) & _CI_GATES else "")
        return "unknown", "the review gate could not be read"
    checks, merge = bad.get("checks"), bad.get("merge_state")
    pending = checks is not None and (checks.status == integrate.UNKNOWN or any(
        w in checks.detail.lower() for w in ("pending", "not reported")))
    if checks is not None and not pending:
        return "needs_fix", "CI is not green: " + checks.detail
    if merge is not None and merge.status == integrate.UNKNOWN:
        return "waiting_ci", "the merge state is not computed yet"
    if merge is not None and not pending:
        return "needs_fix", "merge state " + (merge.detail or "not clean") + " (a behind branch needs main merged in)"
    return "waiting_ci", "CI has not finished on the head commit"


def track(task_id: str, gh: Callable, git: Callable, *, base: str = "origin/main", pr_base: str = "main",
          evaluate: Callable = integrate.evaluate, free_memory: Callable = memory_guard.free_memory_mb,
          tests_check: Callable = integrate.tests_gate,
          worker_tests: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """The cycle record for ``task_id``. Never raises."""
    try:
        return _track(task_id, gh, git, base, pr_base, evaluate, free_memory, tests_check, worker_tests)
    except Exception as exc:                                                       # a tracker must not crash its caller
        return _unknown(str(task_id), f"unexpected {type(exc).__name__} while reading state")


def _track(task_id: str, gh: Callable, git: Callable, base: str, pr_base: str, evaluate: Callable,
           free_memory: Callable, tests_check: Callable,
           worker_tests: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    if not isinstance(base, str) or not base or base.startswith("-"):
        return _unknown(task_id, "the base ref is empty or starts with a dash")
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
    if len(prs) >= 20:
        ev.append(_ev("the pull request list was cut at 20; older ones are not shown"))
    closed = [p for p in prs if p.get("state") == "CLOSED"]
    if closed:
        ev.append(_ev("closed without merge: " + ", ".join(f"#{p['number']}" for p in closed)))
    opened = [p for p in prs if p.get("state") == "OPEN"]
    if opened:
        n = max(p["number"] for p in opened)
        gates, info = evaluate(n, gh, git, pr_base)
        stage, why = classify(gates)
        ev.append(_ev(f"PR #{n} gates: " + "; ".join(f"{g.num}:{g.name}={g.status}" for g in gates)))
        ev.append(_ev(why))
        if stage == "ready_to_merge" and info.get("tests") is not None:     # the ninth check, only when integrate gave its facts
            tg = tests_check(info)
            ev.append(_ev(f"tests check: {tg.status} - {tg.detail}"))
            if tg.status == integrate.UNKNOWN:
                stage, why = "unknown", "the tests check could not be read"
            elif tg.status != integrate.PASS:
                stage, why = "needs_tests", "the eight gates pass but the tests check does not"
            ev.append(_ev(why))
        nxt = {
            "in_review": f"independent review of PR #{n} diff {info.get('diff_sha') or '?'}: fresh read-only reviewer, post the dev-review marker",
            "waiting_ci": f"wait for CI / the merge state of PR #{n}, then run the tracker again",
            "needs_fix": f"fix the failing gates on PR #{n}, then re-run integrate (dry run)",
            "needs_tests": f"in the task worktree: python -B -m dev_supervisor.testreq run --task {task_id} (a prose requirement must first be rewritten as an exact command), then re-run integrate (dry run)",
            "ready_to_merge": f"python -B -m dev_supervisor.integrate {n} --yes",
            "unknown": "gate results were not readable",
        }[stage]
        return _result(task_id, stage, ev, nxt, n)
    merged = [p for p in prs if p.get("state") == "MERGED"]
    if merged:
        n = max(p["number"] for p in merged)
        return _result(task_id, "awaiting_closeout", ev + [_ev(f"PR #{n} is merged and the task is still {status}")],
                       "registry-only close-out pull request from the task's own branch (status COMPLETED, lease released)", n)
    entry = None
    if worker_tests is not None:
        try:
            entry = worker_tests(task_id)
        except Exception:                                                      # a missing or odd record changes nothing
            entry = None
    note = ""
    if isinstance(entry, dict) and entry.get("state") in ("RUNNING", "PENDING", "FAILED", "BLOCKED", "PASSED"):
        st, head, detail = str(entry["state"]), str(entry.get("head") or "")[:12], str(entry.get("detail") or "")[:300]
        ev.append(_ev(f"worker tests (Supervisor): {st} for head {head or '?'}: {detail}"))
        if st in ("RUNNING", "PENDING"):
            return _result(task_id, "worker_testing", ev, "wait: the Supervisor runs the task's test requirements for the worker's head; "
                           "it appends a worker_tests_* event when the result is known")
        if st in ("FAILED", "BLOCKED"):
            return _result(task_id, "worker_tests_failed", ev,
                           f"read the cause above (python -B -m dev_supervisor.testreq show --task {task_id}); fix and commit: the Supervisor tests the new head")
        note = f"the task tests passed for head {head}: "
    rc, _ = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])
    if rc == 0:
        return _result(task_id, "branch_pushed", ev + [_ev(f"origin/{branch} exists (as of the last fetch)")],
                       note + "open the pull request: dev_supervisor.intake for a worker's branch, else gh pr create")
    free = free_memory()
    mem = f"free memory {free} MB, dispatch floor {memory_guard.min_free_mb()} MB" if isinstance(free, int) else "free memory unreadable"
    return _result(task_id, "awaiting_work", ev + [_ev(f"no pushed branch and no pull request; {mem}")],
                   (note + "publish the worker's branch with dev_supervisor.intake <worker-id> --yes; or " if note else "") + "implement it, or dispatch a worker (dev_supervisor assign ... --allocation-role implementation); "
                   "the memory guard refuses a dispatch below the floor")


def main(argv: Optional[Sequence[str]] = None, *, out: Optional[TextIO] = None,
         gh: Optional[Callable] = None, git: Optional[Callable] = None,
         worker_tests: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None) -> int:
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="dev_supervisor.cycle", description="Where is a task in the task-to-merge chain? (read-only)")
    p.add_argument("task_id")
    p.add_argument("--repo", help="repository checkout (default: this one)")
    p.add_argument("--base", default="origin/main", help="ref holding ACTIVE_TASKS.json (default %(default)s)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--state-dir", default=None, help="Supervisor state directory (default: the usual one); read for the worker tests")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    root = Path(a.repo) if a.repo else Path(__file__).resolve().parents[2]
    rec = track(a.task_id, gh or integrate.real_gh_runner(root), git or integrate.real_git_runner(root), base=a.base,
                 worker_tests=worker_tests if worker_tests is not None else worker_tests_reader(a.state_dir))
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
