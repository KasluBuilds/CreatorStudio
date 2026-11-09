"""Inspect a project and decide where to resume. Read-only: never changes state.

The analysis refuses to assume success. Anything inconsistent becomes a finding
with a severity:
  critical - do not continue until a human/AI has dealt with it
  warning  - continue with care; verify first
  info     - FYI
"""
from __future__ import annotations

import json
from pathlib import Path

from . import schema, util
from .schema import COMPLETED, DONE_STATUSES, FAILED, IN_PROGRESS, OP_STARTED, PENDING


def _finding(severity, code, message, stage=None):
    return {"severity": severity, "code": code, "message": message, "stage": stage}


def _check_outputs(state, pdir: Path, deep: bool, findings: list):
    for sid in state["stage_order"]:
        st = state["stages"][sid]
        if st["status"] != COMPLETED:
            continue
        if not st["outputs"]:
            findings.append(_finding("critical", "COMPLETED_WITHOUT_OUTPUT",
                                     "Stage is marked completed but records no verified output.", sid))
            continue
        for out in st["outputs"]:
            p = pdir / out["path"]
            if not p.exists():
                findings.append(_finding("critical", "OUTPUT_MISSING",
                                         f"Verified output no longer exists: {out['path']}", sid))
                continue
            if p.is_file():
                size = p.stat().st_size
                if size != out.get("size"):
                    findings.append(_finding("critical", "OUTPUT_CHANGED",
                                             f"Output size changed ({out.get('size')} -> {size} bytes): {out['path']}", sid))
                    continue
                if deep and util.sha256_file(p) != out.get("sha256"):
                    findings.append(_finding("critical", "OUTPUT_CHANGED",
                                             f"Output content changed since verification: {out['path']}", sid))
            elif deep:
                from .project import fingerprint
                if fingerprint(p)["sha256"] != out.get("sha256"):
                    findings.append(_finding("critical", "OUTPUT_CHANGED",
                                             f"Output folder content changed since verification: {out['path']}", sid))


def _check_sources(state, findings: list):
    for m in state["source_media"]:
        p = Path(m["path"])
        if not p.is_file():
            findings.append(_finding("critical", "SOURCE_MEDIA_MISSING",
                                     f"Original media is missing or offline: {m['path']}"))
        elif p.stat().st_size != m["size"]:
            findings.append(_finding("critical", "SOURCE_MEDIA_CHANGED",
                                     f"Original media size differs from when it was registered: {m['path']}"))


def analyze_state(state: dict, pdir: Path, deep: bool = True, checkpoint_info=None) -> dict:
    """Pure analysis of a loaded, structurally valid state."""
    findings = []
    order = state["stage_order"]
    sts = state["stages"]

    # Ordering: a stage cannot be finished while an earlier one is not.
    for i, sid in enumerate(order):
        if sts[sid]["status"] in DONE_STATUSES:
            for prev in order[:i]:
                if sts[prev]["status"] not in DONE_STATUSES:
                    findings.append(_finding("critical", "ORDER_VIOLATION",
                                             f"Stage is {sts[sid]['status']} but earlier stage '{prev}' is "
                                             f"{sts[prev]['status']}.", sid))
                    break

    active = [s for s in order if sts[s]["status"] == IN_PROGRESS]
    if len(active) > 1:
        findings.append(_finding("critical", "MULTIPLE_ACTIVE_STAGES",
                                 f"More than one stage is in progress: {', '.join(active)}."))
    for sid in active:
        findings.append(_finding("warning", "INTERRUPTED_STAGE",
                                 "Stage was started but never completed (session ended or crashed?).", sid))
    for sid in order:
        st = sts[sid]
        for op_id, op in st["operations"].items():
            if op["status"] == OP_STARTED:
                if op["destructive"]:
                    findings.append(_finding("critical", "UNCERTAIN_DESTRUCTIVE_OPERATION",
                                             f"Destructive operation '{op_id}' ({op['description']}) was started "
                                             "but never confirmed. Check the real result before doing anything; "
                                             "do NOT simply repeat it.", sid))
                else:
                    findings.append(_finding("warning", "UNFINISHED_OPERATION",
                                             f"Operation '{op_id}' ({op['description']}) never finished; safe to retry.", sid))
        if st["status"] == FAILED:
            findings.append(_finding("warning", "FAILED_STAGE", "Stage failed and has not been retried.", sid))

    _check_outputs(state, pdir, deep, findings)
    _check_sources(state, findings)

    expected = schema.first_unfinished(state)
    if expected and state.get("current_stage") != expected and not active:
        findings.append(_finding("warning", "CURRENT_STAGE_MISMATCH",
                                 f"current_stage is '{state.get('current_stage')}' but the first unfinished "
                                 f"stage is '{expected}'."))
    if checkpoint_info and checkpoint_info["state_revision"] > state["revision"]:
        findings.append(_finding("critical", "STATE_BEHIND_CHECKPOINT",
                                 f"State file (revision {state['revision']}) is older than checkpoint "
                                 f"{checkpoint_info['seq']:04d} (revision {checkpoint_info['state_revision']}). "
                                 "Updates may have been lost; consider `recover`."))

    unresolved = [e for e in state["errors"] if not e["resolved"]]
    if unresolved:
        findings.append(_finding("info", "UNRESOLVED_ERRORS", f"{len(unresolved)} unresolved error(s) recorded."))

    return {"findings": findings, "resume": _decide_resume(state, findings)}


def _decide_resume(state: dict, findings: list) -> dict:
    """Which stage should run next, and what must happen first."""
    order, sts = state["stage_order"], state["stages"]
    crit = [f for f in findings if f["severity"] == "critical"]

    # A completed stage whose output is gone/changed invalidates everything after it.
    bad_done = [f for f in crit if f["code"] in ("OUTPUT_MISSING", "OUTPUT_CHANGED", "COMPLETED_WITHOUT_OUTPUT")
                and f["stage"]]
    if bad_done:
        earliest = min(bad_done, key=lambda f: order.index(f["stage"]))["stage"]
        return {"stage": earliest, "action": "reopen_and_redo",
                "reason": f"Stage '{earliest}' is recorded as completed but its output is missing or changed. "
                          "Reopen it (reopen_stage with a reason) rather than trusting the old result.",
                "blocking": True}
    nxt = schema.first_unfinished(state)
    if nxt is None:
        return {"stage": None, "action": "none", "reason": "All stages are completed or skipped.",
                "blocking": False}
    st = sts[nxt]
    uncertain = [f for f in crit if f["code"] == "UNCERTAIN_DESTRUCTIVE_OPERATION" and f["stage"] == nxt]
    if uncertain:
        return {"stage": nxt, "action": "verify_before_continuing",
                "reason": uncertain[0]["message"], "blocking": True}
    other_crit = [f for f in crit if f["code"] not in ("UNCERTAIN_DESTRUCTIVE_OPERATION",)]
    blocking = bool(other_crit)
    if st["status"] == IN_PROGRESS:
        done_ops = [k for k, o in st["operations"].items() if o["status"] == "completed"]
        extra = f" Operations already completed (do not repeat): {', '.join(done_ops)}." if done_ops else ""
        return {"stage": nxt, "action": "resume_interrupted",
                "reason": "Stage was interrupted. Re-check its partial outputs, then continue." + extra,
                "blocking": blocking}
    if st["status"] == FAILED:
        return {"stage": nxt, "action": "retry_failed",
                "reason": "Stage failed earlier. Read the recorded error before retrying.", "blocking": blocking}
    return {"stage": nxt, "action": "start", "reason": "Next stage has not been started.", "blocking": blocking}


def analyze(project, deep: bool = True) -> dict:
    """Full report for a Project, including cases where the state file is unusable."""
    from .project import StateError
    report = {"project_dir": str(project.dir), "state_ok": False, "findings": [], "resume": None,
              "summary": None}
    ck_info, ck_state = project.load_checkpoint_state()
    report["latest_checkpoint"] = ck_info
    try:
        state = project.load_state()
    except StateError as e:
        report["findings"].append(_finding("critical", "STATE_UNUSABLE", str(e)))
        if ck_state is not None:
            report["findings"].append(_finding(
                "info", "CHECKPOINT_AVAILABLE",
                f"Checkpoint {ck_info['seq']:04d} ({ck_info['reason']}, {ck_info['created_at']}) can be restored "
                "with `recover`; anything done after it must be re-verified."))
            partial = analyze_state(ck_state, project.dir, deep=False)
            report["resume"] = {"stage": partial["resume"]["stage"], "action": "recover_state_first",
                                "reason": "State file is unusable; restore it from the latest checkpoint first.",
                                "blocking": True}
        else:
            report["resume"] = {"stage": None, "action": "manual_investigation",
                                "reason": "State file unusable and no valid checkpoint exists.", "blocking": True}
        return report
    result = analyze_state(state, project.dir, deep=deep, checkpoint_info=ck_info)
    report.update(state_ok=True, findings=result["findings"], resume=result["resume"],
                  summary=summarize(state))
    return report


def summarize(state: dict) -> dict:
    stages = state["stages"]
    return {
        "project_id": state["project_id"], "title": state["title"], "slug": state["slug"],
        "status": state["status"], "revision": state["revision"], "updated_at": state["updated_at"],
        "current_stage": state["current_stage"],
        "stages": {s: stages[s]["status"] for s in state["stage_order"]},
        "completed": [s for s in state["stage_order"] if stages[s]["status"] == COMPLETED],
        "pending_tasks": [t for t in state["tasks"] if t["status"] != "done"],
        "unresolved_errors": [e for e in state["errors"] if not e["resolved"]],
    }


def has_critical(report: dict) -> bool:
    return any(f["severity"] == "critical" for f in report["findings"])
