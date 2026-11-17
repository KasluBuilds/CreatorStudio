"""Validation of AGENT_REGISTRY.json: which development agents exist and which milestones each one answers for.

Standard library only. The registry stores NO progress. It only names agents, their Claude Code streams (the `owner`
ids used in ACTIVE_TASKS.json and in wave manifests), the packages they own and the milestones they are responsible
for. The desktop development dashboard builds one tab per `active` agent from it, so adding an entry is how a new agent
gets a tab (and a `planned` entry is how a future agent is announced without getting one yet).

Rules
  - agent ids are unique, lowercase and stable; a stream id may belong to one agent only;
  - every milestone named here exists in PROJECT_STATUS.json and belongs to at most one agent;
  - at most one active agent may have `remainder: true`; it answers for every milestone no other agent names;
  - package keys exist in ACTIVE_TASKS.json -> package_owners; document paths are repo-relative and (when a root is
    given) exist.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import tasks as tasks_mod

SCHEMA_VERSION = 1
AGENT_ID = re.compile(r"^[a-z][a-z0-9-]*$")
MILESTONE_ID = re.compile(r"^M-[A-Z]{3}-\d{2}$")
STATUSES = ("active", "planned", "retired")
KNOWN_KEYS = {"id", "name", "role", "status", "registered", "chat", "streams", "package_keys", "milestones",
              "remainder", "docs", "notes"}


def _nonempty(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _list_of_str(v: Any) -> bool:
    return isinstance(v, list) and all(_nonempty(x) for x in v)


def validate(doc: Any, status_doc: Optional[Dict[str, Any]] = None, tasks_doc: Optional[Dict[str, Any]] = None,
             root: Optional[Path] = None) -> List[str]:
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    p: List[str] = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        p.append(f"schema_version must be {SCHEMA_VERSION}")
    if not _nonempty(doc.get("authority")):
        p.append("authority is required")
    agents = doc.get("agents")
    if not isinstance(agents, list) or not agents:
        return p + ["agents must be a non-empty list"]
    milestones = None
    if isinstance(status_doc, dict):
        milestones = {m.get("id") for m in status_doc.get("milestones", []) if isinstance(m, dict)}
    package_keys = None
    if isinstance(tasks_doc, dict) and isinstance(tasks_doc.get("package_owners"), dict):
        package_keys = set(tasks_doc["package_owners"])
    ids: Dict[str, dict] = {}
    stream_owner: Dict[str, str] = {}
    milestone_owner: Dict[str, str] = {}
    remainder = []
    for idx, a in enumerate(agents):
        label = f"agents[{idx}]"
        if not isinstance(a, dict):
            p.append(f"{label}: must be an object")
            continue
        aid = a.get("id")
        if not (isinstance(aid, str) and AGENT_ID.match(aid)):
            p.append(f"{label}: id must match {AGENT_ID.pattern}")
            continue
        label = f"agent {aid}"
        if aid in ids:
            p.append(f"{label}: duplicate id")
            continue
        ids[aid] = a
        for k in a:
            if k not in KNOWN_KEYS:
                p.append(f"{label}: unknown key {k!r}")
        for f in ("name", "role"):
            if not _nonempty(a.get(f)):
                p.append(f"{label}: {f} is required")
        if a.get("status") not in STATUSES:
            p.append(f"{label}: status must be one of {list(STATUSES)}")
        try:
            _dt.date.fromisoformat(a.get("registered", ""))
        except (TypeError, ValueError):
            p.append(f"{label}: registered must be YYYY-MM-DD")
        if "chat" in a and not _nonempty(a["chat"]):
            p.append(f"{label}: chat must be a non-empty string when present")
        streams = a.get("streams", [])
        if not _list_of_str(streams):
            p.append(f"{label}: streams must be a list of owner ids")
            streams = []
        for s in streams:
            if not tasks_mod.OWNER.match(s):
                p.append(f"{label}: stream {s!r} must match {tasks_mod.OWNER.pattern}")
            elif s in stream_owner:
                p.append(f"{label}: stream {s} is already registered to agent {stream_owner[s]}")
            else:
                stream_owner[s] = aid
        pk = a.get("package_keys", [])
        if not _list_of_str(pk):
            p.append(f"{label}: package_keys must be a list of package names")
            pk = []
        if package_keys is not None:
            for k in pk:
                if k not in package_keys:
                    p.append(f"{label}: package key {k!r} is not in ACTIVE_TASKS.json package_owners")
        ms = a.get("milestones", [])
        if not _list_of_str(ms):
            p.append(f"{label}: milestones must be a list of milestone ids")
            ms = []
        for m in ms:
            if not MILESTONE_ID.match(m):
                p.append(f"{label}: milestone id {m!r} is malformed")
            elif milestones is not None and m not in milestones:
                p.append(f"{label}: milestone {m} does not exist in PROJECT_STATUS.json")
            elif m in milestone_owner:
                p.append(f"{label}: milestone {m} is already assigned to agent {milestone_owner[m]}")
            else:
                milestone_owner[m] = aid
        if "remainder" in a and type(a["remainder"]) is not bool:
            p.append(f"{label}: remainder must be true or false")
        if a.get("remainder") is True:
            remainder.append(aid)
            if a.get("status") != "active":
                p.append(f"{label}: the remainder agent must be active")
        docs = a.get("docs", [])
        if not _list_of_str(docs):
            p.append(f"{label}: docs must be a list of paths")
            docs = []
        for d in docs:
            msg = tasks_mod.path_problem(d)
            if msg:
                p.append(f"{label}: {msg}")
            elif root is not None and not (Path(root) / d).is_file():
                p.append(f"{label}: document {d} does not exist")
        if a.get("status") == "active" and not ms and a.get("remainder") is not True:
            p.append(f"{label}: an active agent must name at least one milestone (or be the remainder agent)")
    if len(remainder) > 1:
        p.append(f"only one agent may be the remainder agent (found {remainder})")
    return sorted(set(p))


def active_agents(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The agents that get a dashboard tab, in registry order."""
    return [a for a in doc.get("agents", []) if isinstance(a, dict) and a.get("status") == "active"]


def agent_for_milestone(doc: Dict[str, Any], milestone_id: str) -> Optional[str]:
    """The agent responsible for a milestone: the one that names it, else the remainder agent."""
    rem = None
    for a in active_agents(doc):
        if milestone_id in a.get("milestones", []):
            return a["id"]
        if a.get("remainder") is True:
            rem = a["id"]
    return rem


def agent_for_stream(doc: Dict[str, Any], owner: str) -> Optional[str]:
    """The agent a task or lane owner id belongs to; unknown streams belong to the remainder agent."""
    rem = None
    for a in active_agents(doc):
        if owner in a.get("streams", []):
            return a["id"]
        if a.get("remainder") is True:
            rem = a["id"]
    return rem
