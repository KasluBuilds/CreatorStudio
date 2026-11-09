"""Versioned project-state schema (v1), stage list and validation."""
from __future__ import annotations

SCHEMA_VERSION = 1

# Order follows WORKFLOW.md. Stage ids are stable identifiers; titles are for humans.
STAGES = (
    ("ingest", "Intake and ingest"),
    ("understand", "Understand the material"),
    ("research", "Research"),
    ("creative_plan", "Creative plan"),
    ("asset_acquisition", "Asset acquisition"),
    ("plan_validation", "Edit-plan validation"),
    ("resolve_assembly", "Resolve assembly"),
    ("review_iteration", "Review and iteration"),
    ("long_form_delivery", "Long-form delivery"),
    ("short_form", "Short-form derivation"),
    ("completion", "Completion and archive"),
)
STAGE_IDS = tuple(s for s, _ in STAGES)
STAGE_TITLES = dict(STAGES)

PENDING, IN_PROGRESS, COMPLETED, FAILED, SKIPPED = (
    "pending", "in_progress", "completed", "failed", "skipped")
STAGE_STATUSES = (PENDING, IN_PROGRESS, COMPLETED, FAILED, SKIPPED)
DONE_STATUSES = (COMPLETED, SKIPPED)

OP_STARTED, OP_COMPLETED, OP_FAILED, OP_NOT_APPLIED = (
    "started", "completed", "failed", "not_applied")

# Subfolders from TECHNICAL_ARCHITECTURE.md section 7, plus checkpoints/.
PROJECT_DIRS = (
    "transcripts", "analysis", "research", "edit_plans", "assets", "resolve",
    "review_renders", "final_exports", "qc", "logs", "backups", "checkpoints",
)

# Standing rules from MASTER.md that every replacement AI must see.
DEFAULT_CONSTRAINTS = (
    "Preserve the creator's authenticity: never write replacement dialogue or alter the creator's real words.",
    "Source media is immutable: never modify, move or delete original media.",
    "Do not claim a step is done until its output exists and has been verified.",
    "Unknown asset rights are not permission: reject or quarantine such assets.",
    "Do not edit in DaVinci Resolve without a validated plan and a recovery path; serialize Resolve mutations.",
    "Never store API keys, tokens or other secrets in project files.",
)


def new_stage() -> dict:
    return {
        "status": PENDING, "attempts": 0, "started_at": None, "completed_at": None,
        "summary": None, "outputs": [], "verification": None,
        "operations": {}, "skip_reason": None, "history": [],
    }


def new_state(project_id: str, slug: str, title: str, now: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "slug": slug,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "revision": 1,
        "status": "created",
        "current_stage": STAGE_IDS[0],
        "stage_order": list(STAGE_IDS),
        "stages": {s: new_stage() for s in STAGE_IDS},
        "source_media": [],
        "decisions": [],
        "constraints": [{"id": f"C{i:03d}", "text": t, "added_at": now}
                        for i, t in enumerate(DEFAULT_CONSTRAINTS, 1)],
        "tasks": [],
        "errors": [],
        "notes": [],
        "provider_history": [],
    }


def derive_status(state: dict) -> str:
    sts = [state["stages"][s]["status"] for s in state["stage_order"]]
    if all(s in DONE_STATUSES for s in sts):
        return "completed"
    if any(s == FAILED for s in sts):
        return "failed"
    if any(s != PENDING for s in sts):
        return "in_progress"
    return "created"


def first_unfinished(state: dict):
    for s in state["stage_order"]:
        if state["stages"][s]["status"] not in DONE_STATUSES:
            return s
    return None


def migrate(state: dict) -> dict:
    """Upgrade older schema versions in memory. Refuses versions from the future."""
    v = state.get("schema_version")
    if not isinstance(v, int):
        raise ValueError("state has no integer schema_version")
    if v > SCHEMA_VERSION:
        raise ValueError(
            f"state schema_version {v} is newer than this code supports ({SCHEMA_VERSION}); "
            "upgrade creator_state instead of editing this project")
    # Future: if v == 1: ...transform to 2...; v = 2
    return state


_REQUIRED = {
    "schema_version": int, "project_id": str, "slug": str, "title": str,
    "created_at": str, "updated_at": str, "revision": int, "status": str,
    "stage_order": list, "stages": dict, "source_media": list, "decisions": list,
    "constraints": list, "tasks": list, "errors": list, "notes": list,
    "provider_history": list,
}


def validate_state(state) -> list:
    """Return a list of human-readable structural problems (empty list = valid)."""
    if not isinstance(state, dict):
        return ["state is not a JSON object"]
    problems = []
    for key, typ in _REQUIRED.items():
        if key not in state:
            problems.append(f"missing field '{key}'")
        elif not isinstance(state[key], typ):
            problems.append(f"field '{key}' should be {typ.__name__}")
    if problems:
        return problems
    for s in state["stage_order"]:
        st = state["stages"].get(s)
        if not isinstance(st, dict):
            problems.append(f"stage '{s}' missing from stages")
            continue
        if st.get("status") not in STAGE_STATUSES:
            problems.append(f"stage '{s}' has invalid status {st.get('status')!r}")
        for k in ("outputs", "operations"):
            if k not in st:
                problems.append(f"stage '{s}' missing '{k}'")
    cur = state.get("current_stage")
    if cur is not None and cur not in state["stage_order"]:
        problems.append(f"current_stage {cur!r} is not a known stage")
    return problems
