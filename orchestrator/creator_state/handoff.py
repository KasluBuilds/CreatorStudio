"""Render HANDOFF.md: everything a replacement AI needs, from recorded facts only.

Nothing here depends on hidden model reasoning. If it is not in project_state.json,
the activity log or a file on disk, it is not in the handoff.
"""
from __future__ import annotations

from pathlib import Path

from . import recovery, schema, util
from .redact import redact_text

HANDOFF_FILE = "HANDOFF.md"

_ICON = {"completed": "[x]", "skipped": "[-]", "in_progress": "[~]", "failed": "[!]", "pending": "[ ]"}


def _bullets(items, empty="(none recorded)"):
    return "\n".join(items) if items else empty


def render(state: dict, report: dict, project_dir: Path, recent_log: list, media_lines=None) -> str:
    resume = report["resume"] or {}
    stage_titles = schema.STAGE_TITLES
    L = []
    L.append(f"# HANDOFF — {state['title']}")
    L.append("")
    L.append(f"_Generated {util.utcnow()} from state revision {state['revision']}. "
             "Regenerated automatically after every state change._")
    L.append("")
    L.append("## Read this first")
    L.append("- This document is a **recorded-facts summary**. It does not contain, and you cannot recover, "
             "the previous AI's private reasoning. Re-derive judgement from the facts and files below.")
    L.append("- **Verify before trusting.** Stage completion is only recorded after outputs were checked, "
             "but re-check any file you depend on. Run `python -m creator_state check <project>` for a fresh audit.")
    L.append("- Never repeat an operation listed as *already completed* if it is destructive. "
             "Never put API keys or tokens into project files.")
    L.append("- Project rules are in the **Constraints** section below; the global rules live in "
             "`AI_Creator_Studio_Documentation/MASTER.md` (read it, plus WORKFLOW.md for stage meanings).")
    L.append("")
    L.append("## Project")
    L.append(f"- **Title:** {state['title']}  ")
    L.append(f"- **ID / slug:** `{state['project_id']}` / `{state['slug']}`  ")
    L.append(f"- **Status:** {state['status']}  ")
    L.append(f"- **Project folder:** `{project_dir}`  ")
    L.append(f"- **State file:** `project_state.json` (schema v{state['schema_version']})")
    L.append("")

    L.append("## Where to resume")
    if not report["state_ok"]:
        L.append("**STATE UNUSABLE** — see findings below; run `recover` before anything else.")
    else:
        L.append(f"- **Stage:** `{resume.get('stage')}`"
                 + (f" ({stage_titles.get(resume.get('stage'), '')})" if resume.get("stage") else ""))
        L.append(f"- **Action:** `{resume.get('action')}`" + ("  — **BLOCKED until findings below are handled**"
                                                              if resume.get("blocking") else ""))
        L.append(f"- **Why:** {resume.get('reason')}")
    L.append("")

    crit = [f for f in report["findings"] if f["severity"] in ("critical", "warning")]
    L.append("## Integrity findings")
    L.append(_bullets([f"- **{f['severity'].upper()}** `{f['code']}`"
                       + (f" [{f['stage']}]" if f["stage"] else "") + f": {f['message']}" for f in crit],
                      "None. State and outputs agree." ))
    L.append("")

    L.append("## Stage progress")
    for sid in state["stage_order"]:
        st = state["stages"][sid]
        line = f"- {_ICON.get(st['status'], '[?]')} **{sid}** — {stage_titles.get(sid, sid)}: {st['status']}"
        if st["attempts"]:
            line += f" (attempts: {st['attempts']})"
        if st["status"] == "completed":
            line += f", completed {st['completed_at']}"
        if st["status"] == "skipped":
            line += f" — reason: {st['skip_reason']}"
        L.append(line)
        if st["summary"]:
            L.append(f"    - Summary: {st['summary']}")
        for out in st["outputs"]:
            L.append(f"    - Output: `{out['path']}` ({out['kind']}, {out['size']} bytes, "
                     f"sha256 `{out['sha256'][:12]}…`, verified {out['verified_at']})")
        for op_id, op in st["operations"].items():
            tag = "DESTRUCTIVE " if op["destructive"] else ""
            L.append(f"    - {tag}operation `{op_id}`: {op['status']} — {op['description']}")
    L.append("")

    L.append("## Creative decisions")
    L.append(_bullets([f"- **{d['id']}** ({d['category']}, by {d['made_by']}, {d['at']}"
                       + (f", stage {d['stage']}" if d["stage"] else "") + f"): {d['text']}"
                       + (f"  \n  _Why:_ {d['rationale']}" if d["rationale"] else "")
                       for d in state["decisions"]]))
    L.append("")
    L.append("## Constraints")
    L.append(_bullets([f"- **{c['id']}** {c['text']}" for c in state["constraints"]]))
    L.append("")

    L.append("## Pending work")
    open_tasks = [t for t in state["tasks"] if t["status"] != "done"]
    L.append(_bullets([f"- **{t['id']}**" + (f" [{t['stage']}]" if t["stage"] else "") + f": {t['text']}"
                       for t in open_tasks], "No open tasks recorded. (Stage list above shows unfinished stages.)"))
    L.append("")

    L.append("## Known problems")
    errs = [e for e in state["errors"] if not e["resolved"]]
    L.append(_bullets([f"- **{e['id']}** {e['kind']}" + (f" [{e['stage']}]" if e["stage"] else "")
                       + f" ({e['at']}): {e['message']}" for e in errs], "No unresolved errors recorded."))
    resolved = [e for e in state["errors"] if e["resolved"]]
    if resolved:
        L.append("")
        L.append("Resolved earlier: " + "; ".join(f"{e['id']} ({e['resolution']})" for e in resolved))
    L.append("")

    if media_lines:
        L.append("## Media inventory")
        L.extend(media_lines)
        L.append("")

    L.append("## Source media (originals — read-only, never modify)")
    L.append(_bullets([f"- `{m['path']}` ({m['size']} bytes"
                       + (f", sha256 `{m['sha256'][:12]}…`" if m.get("sha256") else "") + ")"
                       for m in state["source_media"]]))
    L.append("")

    L.append("## Notes")
    L.append(_bullets([f"- ({n['at']}) {n['text']}" for n in state["notes"]]))
    L.append("")

    L.append("## Provider history")
    L.append(_bullets([f"- {p['at']}: {p['from']} → {p['to']}" + (f" — {p['reason']}" if p["reason"] else "")
                       for p in state["provider_history"]],
                      "No provider switches recorded. (Switching is done by the user; "
                      "`switch-provider` only records it.)"))
    L.append("")

    L.append("## Key files")
    L.append("- `project_state.json` — authoritative state")
    L.append("- `checkpoints/` — append-only snapshots taken after completed stages")
    L.append("- `logs/activity.log` (human) and `logs/activity.jsonl` (machine)")
    L.append("- `backups/` — preserved copies of damaged state")
    L.append("- Working folders: transcripts/, analysis/, research/, edit_plans/, assets/, resolve/, "
             "review_renders/, final_exports/, qc/")
    L.append("")

    L.append("## Recent activity (last entries)")
    L.append("```text")
    L.extend(recent_log or ["(no log entries)"])
    L.append("```")
    L.append("")
    return redact_text("\n".join(L))


def write(project, deep: bool = False) -> Path:
    """Regenerate HANDOFF.md atomically. deep=True re-hashes outputs (slower, stricter)."""
    report = recovery.analyze(project, deep=deep)
    try:
        state = project.load_state()
    except Exception:
        # State unusable: build the handoff from the latest checkpoint so it is still useful.
        _, state = project.load_checkpoint_state()
        if state is None:
            raise
    try:
        from . import ingest
        media_lines = ingest.manifest_summary_lines(project)
    except Exception:
        media_lines = []
    text = render(state, report, project.dir, project.log.tail(15), media_lines)
    path = project.dir / HANDOFF_FILE
    util.atomic_write_text(path, text)
    return path
