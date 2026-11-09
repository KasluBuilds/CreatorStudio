"""Render SESSION_HANDOFF.md deterministically from PROJECT_STATUS.json and ACTIVE_TASKS.json.

Nothing here reads the clock, git or the network: the output depends only on the two input documents,
so `python -m governance handoff --check` can prove the committed handoff is not stale.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import status as status_mod
from . import tasks as tasks_mod

HEADER = (
    "<!-- GENERATED FILE. Do not edit by hand. Regenerate with:\n"
    "     cd orchestrator && python -B -m governance handoff --write\n"
    "     Source of truth: PROJECT_STATUS.json and ACTIVE_TASKS.json. -->"
)


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _gates_section(status_doc: Dict[str, Any]) -> List[str]:
    gates = status_doc.get("user_action_gates") or []
    if not gates:
        return []
    ids = {m["id"]: m for m in status_doc["milestones"]}
    out = ["## User-action gates (things Lukas must be told at a milestone)", ""]
    for g in gates:
        trig = ", ".join(g["trigger_milestones"])
        if g["status"] == "TRIGGERED":
            out += [f"### ACTION REQUIRED NOW - {g['id']}: {g['title']}", "",
                    f"**Tell Lukas, before any other development work:** {g['message']}", ""]
            out += [f"{i}. {s}" for i, s in enumerate(g["steps"], 1)]
            out += ["", f"Checklist: `{g.get('checklist', '')}`. Status stays TRIGGERED until Lukas acknowledges it.", ""]
        elif g["status"] == "ACKNOWLEDGED":
            out += [f"- {g['id']} ({g['title']}): acknowledged by {g.get('acknowledged_by')} on "
                    f"{g.get('acknowledged_date')}.", ""]
        else:
            states = ", ".join(f"{m} is {ids[m]['implementation']['state']}" for m in g["trigger_milestones"])
            out += [f"- **{g['id']} - {g['title']}** (PENDING; fires when any of {trig} is implemented; now: {states}).",
                    f"  When it fires: {g['message']}",
                    f"  Notification: {g['notification']}", ""]
    return out


def render(status_doc: Dict[str, Any], tasks_doc: Dict[str, Any]) -> str:
    calc = status_mod.calculate(status_doc)
    base = status_doc["baseline"]
    lines: List[str] = [HEADER, "", "# Session handoff", ""]
    lines += [f"Status record dated **{status_doc['updated']}**. Baseline commit **`{base['commit'][:12]}`** "
              f"on `{base['branch']}`.", ""]
    lines += _gates_section(status_doc)
    lines += ["## Verified baseline", "",
              f"- Command: `{base['test_command']}`",
              f"- Result: {base['tests_total']} tests, {base['failures']} failures, {base['errors']} errors, "
              f"{base['skipped']} skipped ({base['date']}; {base['scope']}).",
              "- This is a point-in-time result tied to the commit above. Re-run before relying on it.", ""]
    lines += ["## Progress (computed from PROJECT_STATUS.json)", "",
              f"- Implementation: **{calc['implementation_percent']}%**  ",
              f"- Verification: **{calc['verification_percent']}%**  ",
              f"- {calc['milestone_count']} milestones, total weight {calc['total_weight']}. "
              "Verification never exceeds implementation. "
              + ("Weights are PROPOSED: drafted by an AI session and not yet reviewed by Lukas, so these figures are "
                 "provisional. " if status_doc["weights_status"] == "PROPOSED" else
                 f"Weights APPROVED by {status_doc.get('weights_approved_by')} on {status_doc.get('weights_approved_date')}. "),
              "- How to read them: *implementation* is the weighted share of milestones whose code exists (half credit "
              "if in progress); *verification* is the weighted share demonstrated at the level evidence supports. "
              "Neither measures effort, time left, or whether the product works on real footage.",
              f"- Milestones verified on reviewed real input: {len(calc['real_input_reviewed_milestones'])} of "
              f"{calc['milestone_count']} ({', '.join(calc['real_input_reviewed_milestones']) or 'none'}).",
              f"- Milestones whose verification rests only on *reported* (not re-observed) evidence: "
              f"{', '.join(calc['reported_only_milestones']) or 'none'}.", "",
              "| Phase | Weight | Implemented | Verified |", "|---|---:|---:|---:|"]
    for phase, row in calc["by_phase"].items():
        lines.append(f"| {_cell(phase)} | {row['weight']} | {row['implementation_percent']}% | "
                     f"{row['verification_percent']}% |")
    lines += ["", "## Milestones", "", "| ID | Title | Weight | Implementation | Verification level |", "|---|---|---:|---|---|"]
    for r in calc["milestones"]:
        lines.append(f"| {r['id']} | {_cell(r['title'])} | {r['weight']} | {r['implementation_state']} | "
                     f"{r['verification_level']} ({r['verification_state']}) |")

    active = [t for t in tasks_doc["tasks"] if t["status"] in tasks_mod.ACTIVE_STATUSES]
    lines += ["", "## Active tasks", ""]
    if active:
        lines += ["| Task | Owner | Branch | Status | Lease expires | Owns |", "|---|---|---|---|---|---|"]
        for t in active:
            lines.append(f"| {t['id']} {_cell(t['title'])} | {t['owner']} | `{t['branch']}` | {t['status']} | "
                         f"{t['lease']['expires']} | {_cell(', '.join(t['owned_paths']))} |")
        lines.append("")
        lines.append("Leases are not judged here (this file never reads the clock): run "
                     "`cd orchestrator && python -B -m governance leases` to list expired ones.")
    else:
        lines.append("No active tasks.")
    lines += ["", "## Package ownership", "", "| Package | Owner | How established |", "|---|---|---|"]
    for name, pkg in tasks_doc["package_owners"].items():
        lines.append(f"| `{name}` | {pkg['owner']} | {_cell(pkg['owner_status'])} |")
    unresolved = [n for n, p in tasks_doc["package_owners"].items() if p["owner"] == tasks_mod.UNRESOLVED]
    lines += ["", f"Unresolved ownership: {', '.join('`'+n+'`' for n in unresolved) or 'none'}. "
              "Touching these needs an integration agreement (see PROJECT_OPERATING_PROTOCOL.md).", ""]

    blocked = [m for m in status_doc["milestones"] if m["blockers"]]
    lines += ["## Blockers and open questions", ""]
    for m in sorted(blocked, key=lambda x: x["id"]):
        lines.append(f"- **{m['id']}** blocked by: {'; '.join(m['blockers'])}")
    for q in status_doc["open_decisions"]:
        lines.append(f"- {q}")
    lines += ["", "## Next actions", ""]
    for i, a in enumerate(status_doc["next_actions"], 1):
        lines.append(f"{i}. {a}")
    lines += ["", "## Where things live", "",
              "- Rules and procedures: `PROJECT_OPERATING_PROTOCOL.md`",
              "- Status and evidence: `PROJECT_STATUS.json` (authoritative for milestone status)",
              "- Tasks, leases, ownership: `ACTIVE_TASKS.json`",
              "- Design and decisions: `AI_Creator_Studio_Documentation/` (MASTER, ROADMAP, DECISIONS_AND_OPEN_QUESTIONS)",
              "- Environment and tools: `AI_Creator_Studio_Documentation/ENVIRONMENT.md`, `TOOLS.md`", ""]
    return "\n".join(lines)
