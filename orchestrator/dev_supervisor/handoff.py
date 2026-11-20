"""Session handoff: build, write and VERIFY-by-read-back (T-DEV-003, issue #44 part C).

"Main is replaceable" rests on one invariant: a session is never cleared or replaced until a handoff has been written
AND verified by reading it back. This module provides both halves:

- ``build_handoff`` captures objective, decisions, current plan, task graph, worker identities and status,
  branches/worktrees, latest commits and pull requests, tests and evidence (each with a label), blockers, next actions and
  resource state (context and usage signals with their L1 evidence labels). It refuses secrets and transcript-like text
  and caps every string, so a handoff is a summary of recorded facts, never a transcript.
- ``write_handoff`` writes machine-readable JSON plus a human Markdown file into the machine-local state store
  (``<store>/handoffs/``, never the repository) with a SHA-256 content hash, and records a ``handoff_written`` event.
- ``verify_handoff`` re-reads both files, validates the schema, recomputes the hash, and confirms every referenced
  branch, worktree, commit and pull request through injected checkers. Anything missing gives ``NOT_SAFE`` with reasons.
  Only a ``SAFE`` verification yields a ``VerifiedHandoff``, and that object cannot be constructed anywhere else (a
  module-private seal), so ``replacement.replace_session`` structurally cannot run with an unverified handoff.

Status: IMPLEMENTED and tested offline (fake checkers, temp store). ``GitRefChecker``/``GhPrChecker`` wrap the real
``git``/``gh`` CLIs with argument lists; they are NOT TESTED against a live repository by the unit tests.
Stdlib only. No network in this module except through the injected/real checkers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

try:
    from .evidence import Evidence, Label
    from .github_log import scrub as sensitivity_scrub
    from .github_log import sensitivity_flags
    from .state_store import StateStore, StoreError, contains_secret, parse_iso, utc_iso
    from .workers import Worker
except ImportError:  # pragma: no cover - standalone import
    from evidence import Evidence, Label  # type: ignore
    from github_log import scrub as sensitivity_scrub  # type: ignore
    from github_log import sensitivity_flags  # type: ignore
    from state_store import StateStore, StoreError, contains_secret, parse_iso, utc_iso  # type: ignore
    from workers import Worker  # type: ignore

__all__ = ["HANDOFF_SCHEMA_VERSION", "HANDOFF_KIND", "EVIDENCE_LABELS", "HandoffError", "HandoffRecord",
           "VerificationReport", "VerifiedHandoff", "RefCheckers", "GitRefChecker", "GhPrChecker", "build_handoff",
           "worker_entries", "pressure_to_resource", "content_hash", "validate_schema", "write_handoff",
           "verify_handoff", "require_verified", "render_markdown", "handoff_dir", "SAFE", "NOT_SAFE",
           "build_mechanical_handoff", "read_active_tasks"]

HANDOFF_SCHEMA_VERSION = 1
HANDOFF_KIND = "dev_handoff"
HANDOFF_SUBDIR = "handoffs"
SAFE = "SAFE"
NOT_SAFE = "NOT_SAFE"
# Labels allowed on tests, evidence and resource signals. VERIFIED needs command output recorded with it.
EVIDENCE_LABELS = ("VERIFIED", "OBSERVED", "REPORTED", "INFERRED", "UNAVAILABLE", "NOT_TESTED")
MAX_TEXT = 500          # any single string: a summary line, never a transcript
MAX_ITEMS = 200         # any single list
_HANDOFF_ID = re.compile(r"^h-[0-9a-f]{12}$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,199}$")
_SEAL = object()        # only verify_handoff holds this: VerifiedHandoff cannot be forged elsewhere
_ISSUED: "Dict[Tuple[str, str, float], Any]" = {}   # objects issued by verify_handoff (copies are not accepted)
_ISSUED_MAX = 256


class HandoffError(StoreError):
    """A handoff could not be built or written (bad input, secret or transcript-like text)."""


class UnverifiedHandoffError(HandoffError):
    """Raised when an action that clears or replaces a session is given anything but a fresh VerifiedHandoff."""


__all__.append("UnverifiedHandoffError")


# ------------------------------------------------------------------ building

def _text(value: Any, what: str) -> str:
    s = "" if value is None else str(value)
    if len(s) > MAX_TEXT:
        raise HandoffError(f"{what} is {len(s)} characters (limit {MAX_TEXT}): a handoff holds summaries, "
                           "not transcripts or file contents")
    return s


def _list(value: Any, what: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise HandoffError(f"{what} must be a list")
    if len(value) > MAX_ITEMS:
        raise HandoffError(f"{what} has {len(value)} items (limit {MAX_ITEMS})")
    return list(value)


def _label(value: Any, what: str) -> str:
    v = str(value or "").upper().replace(" ", "_")
    if v not in EVIDENCE_LABELS:
        raise HandoffError(f"{what}: label {value!r} is not one of {EVIDENCE_LABELS}")
    return v


def _evidence_dict(ev: Any, what: str) -> Dict[str, Any]:
    """An L1 Evidence (or its dict) as a labelled resource signal. UNAVAILABLE keeps value None and its reason."""
    if isinstance(ev, Evidence):
        d = ev.to_dict()
    elif isinstance(ev, Mapping):
        d = dict(ev)
    elif ev is None:
        return {"label": "UNAVAILABLE", "value": None, "reason": "not supplied", "source": "", "timestamp": ""}
    else:
        raise HandoffError(f"{what} must be an Evidence or a dict")
    label = _label(d.get("label"), what)
    if label == "UNAVAILABLE" and d.get("value") is not None:
        raise HandoffError(f"{what}: an UNAVAILABLE signal must not carry a value")
    if label != "UNAVAILABLE" and d.get("value") is None:
        raise HandoffError(f"{what}: a {label} signal needs a value")
    out = {"label": label, "value": d.get("value"), "source": _text(d.get("source", ""), what),
           "timestamp": _text(d.get("timestamp", ""), what)}
    if d.get("reason") or label == "UNAVAILABLE":
        out["reason"] = _text(d.get("reason") or "no reason given", what)
    return out


def pressure_to_resource(pressure: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Convert ``telemetry.context_pressure(...)`` output into labelled resource fields (labels preserved)."""
    pressure = pressure or {}
    return {k: _evidence_dict(pressure.get(k), f"resources.context.{k}")
            for k in ("tokens", "model", "window", "ratio", "level")}


def worker_entries(workers: Iterable[Worker]) -> List[Dict[str, Any]]:
    """Worker identities and status from the L2 registry (no prompt contents, only identifiers)."""
    out = []
    for w in workers:
        out.append({"worker_id": w.worker_id, "role": w.role, "task_id": w.task_id, "status": w.status,
                    "branch": w.branch, "worktree": w.worktree, "session_id": w.session_id, "model": w.model,
                    "provider": w.provider})
    return out


MECHANICAL_WORKER_STATUSES = ("ASSIGNED", "STARTING", "RUNNING", "STALE", "STOPPING", "FAILED", "BLOCKED", "LOST")
_NOISY_EVENT_PREFIXES = ("handoff_", "compaction_decision", "request_", "session_registered")


def _plain(text: Any, limit: int = 200) -> str:
    """A recorded string made safe for a public-log-grade handoff: credential/path/opaque text redacted, capped."""
    return sensitivity_scrub(text, limit)


def read_active_tasks(path: Any) -> List[Dict[str, Any]]:
    """ACTIVE tasks of ACTIVE_TASKS.json as {task_id, title, branch, depends_on}; [] when missing or unreadable."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        tasks = doc["tasks"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    out = []
    for t in tasks if isinstance(tasks, list) else []:
        if isinstance(t, dict) and t.get("status") == "ACTIVE" and isinstance(t.get("id"), str):
            out.append({"task_id": t["id"], "title": t.get("title") or "", "branch": t.get("branch") or "",
                        "depends_on": [d for d in (t.get("depends_on") or []) if isinstance(d, str)]})
    return out


def build_mechanical_handoff(*, session_id: str, pressure: Optional[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]],
                             workers: Iterable[Worker], events: Sequence[Mapping[str, Any]] = (),
                             approvals: Sequence[Mapping[str, Any]] = (),
                             pr_lister: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None,
                             head_of: Optional[Callable[[str], Optional[str]]] = None, usage: Any = None,
                             event_limit: int = 15, role: str = "main", with_titles: bool = True,
                             created_at: Optional[str] = None) -> Dict[str, Any]:
    """A handoff assembled ONLY from recorded state (T-DEV-030): ACTIVE tasks, the worker registry (live or failed),
    open pull requests (UNAVAILABLE without ``pr_lister``), git heads of the task branches (``head_of`` returns None
    when a branch does not resolve, which is then left out), pending approvals and the last supervisor events. No
    model-written text and no transcript. Raises ``HandoffError`` like ``build_handoff``."""
    ratio = (pressure or {}).get("ratio")
    ratio_v = ratio.value if isinstance(ratio, Evidence) and ratio.available else (
        ratio.get("value") if isinstance(ratio, Mapping) else None)
    pct = f"{float(ratio_v):.0%}" if isinstance(ratio_v, (int, float)) and not isinstance(ratio_v, bool) else "unknown"
    task_graph = [{"task_id": t["task_id"], "title": _plain(t.get("title"), 160) if with_titles else "",
                   "status": "ACTIVE", "depends_on": list(t.get("depends_on") or [])[:20]} for t in tasks][:MAX_ITEMS]
    branches, commits = [], []
    for t in tasks:
        b = t.get("branch") or ""
        sha = None
        if b and head_of is not None:
            try:
                sha = head_of(b)
            except Exception:  # noqa: BLE001 - an unresolvable head is simply left out
                sha = None
        if b and sha and _SHA.match(sha):
            branches.append(b)
            commits.append({"sha": sha, "branch": b, "subject": f"head of the branch of {t['task_id']}"})
    ev = [{"claim": "mechanical handoff built only from recorded state (ACTIVE_TASKS.json, worker registry, git heads, "
                    "supervisor events, pending approvals); no transcript text and no model-written summary",
           "label": "OBSERVED", "source": "supervisor"}]
    prs: List[Dict[str, Any]] = []
    if pr_lister is None:
        ev.append({"claim": "open pull requests: UNAVAILABLE (no gh runner injected)", "label": "UNAVAILABLE",
                   "source": "supervisor"})
    else:
        try:
            for p in list(pr_lister())[:50]:
                if isinstance(p.get("number"), int) and not isinstance(p.get("number"), bool) and p["number"] > 0:
                    prs.append({"number": p["number"], "state": str(p.get("state") or "OPEN")[:20],
                                "branch": str(p.get("headRefName") or p.get("branch") or "")[:200]})
        except Exception as e:  # noqa: BLE001
            ev.append({"claim": f"open pull requests: UNAVAILABLE (gh failed: {e.__class__.__name__})",
                       "label": "UNAVAILABLE", "source": "supervisor"})
    shown = [e for e in events if isinstance(e.get("kind"), str) and not e["kind"].startswith(_NOISY_EVENT_PREFIXES)]
    for e in shown[-max(0, event_limit):]:
        ev.append({"claim": _plain(f"event {e['kind']} at {e.get('ts', '')}", 120), "label": "OBSERVED",
                   "source": "supervisor_events"})
    blockers = [_plain(f"pending approval {a.get('id')}: {a.get('tool')} ({a.get('rule')})", 160)
                for a in approvals if isinstance(a, Mapping)][:50]
    registry = [w for w in workers if w.status in MECHANICAL_WORKER_STATUSES][-MAX_ITEMS:]
    return build_handoff(
        session_id=session_id, role=role, created_at=created_at,
        objective=f"Mechanical handoff for the Main session {str(session_id)[:8]} at context {pct}: continue "
                  "supervising the ACTIVE tasks and workers recorded here",
        plan=["Read this handoff, then continue the ACTIVE tasks listed in the task graph",
              "Check workers with: python -B -m dev_supervisor workers"],
        task_graph=task_graph, workers=worker_entries(registry), branches=branches, commits=commits,
        pull_requests=prs, evidence=ev, blockers=blockers, context=pressure, usage=usage,
        next_actions=["Add your own notes with: python -B -m dev_supervisor handoff --input <facts.json> --yes "
                      "if something is not in the recorded state",
                      "Pending approvals are resolved only by Lukas; continue unrelated work"])


def build_handoff(*, session_id: str, role: str, objective: str, decisions: Sequence[str] = (),
                  plan: Sequence[str] = (), task_graph: Sequence[Mapping[str, Any]] = (),
                  workers: Sequence[Mapping[str, Any]] = (), branches: Sequence[str] = (),
                  worktrees: Sequence[Mapping[str, Any]] = (), commits: Sequence[Mapping[str, Any]] = (),
                  pull_requests: Sequence[Mapping[str, Any]] = (), tests: Sequence[Mapping[str, Any]] = (),
                  evidence: Sequence[Mapping[str, Any]] = (), blockers: Sequence[str] = (),
                  next_actions: Sequence[str] = (), context: Optional[Mapping[str, Any]] = None,
                  usage: Any = None, created_at: Optional[str] = None,
                  handoff_id: Optional[str] = None) -> Dict[str, Any]:
    """Assemble a handoff payload. Raises ``HandoffError`` on secrets, transcript-like text or malformed input.

    ``context`` is the dict from ``telemetry.context_pressure``; ``usage`` is an account-usage Evidence (or None, which
    is recorded as UNAVAILABLE: no account figure is ever invented).
    """
    if not str(objective or "").strip():
        raise HandoffError("objective is required")
    hid = handoff_id or f"h-{uuid.uuid4().hex[:12]}"
    if not _HANDOFF_ID.match(hid):
        raise HandoffError(f"invalid handoff id {hid!r}")
    h: Dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "kind": HANDOFF_KIND,
        "handoff_id": hid,
        "session_id": _text(session_id, "session_id"),
        "role": _text(role, "role"),
        "created_at": created_at or utc_iso(time.time()),
        "objective": _text(objective, "objective"),
        "decisions": [_text(d, "decisions[]") for d in _list(decisions, "decisions")],
        "plan": [_text(p, "plan[]") for p in _list(plan, "plan")],
        "task_graph": [],
        "workers": [],
        "branches": [],
        "worktrees": [],
        "commits": [],
        "pull_requests": [],
        "tests": [],
        "evidence": [],
        "blockers": [_text(b, "blockers[]") for b in _list(blockers, "blockers")],
        "next_actions": [_text(a, "next_actions[]") for a in _list(next_actions, "next_actions")],
        "resources": {"context": pressure_to_resource(context),
                      "usage": _evidence_dict(usage, "resources.usage"),
                      "note": "context figures are per-session; account usage is UNAVAILABLE unless a usage "
                              "provider supplied it. A fresh session does NOT replenish account usage."},
    }
    if not h["session_id"].strip() or not h["role"].strip():
        raise HandoffError("session_id and role are required")
    for t in _list(task_graph, "task_graph"):
        h["task_graph"].append({"task_id": _text(t.get("task_id"), "task_graph.task_id"),
                                "title": _text(t.get("title", ""), "task_graph.title"),
                                "status": _text(t.get("status", ""), "task_graph.status"),
                                "depends_on": [_text(x, "task_graph.depends_on")
                                               for x in _list(t.get("depends_on"), "task_graph.depends_on")]})
    for w in _list(workers, "workers"):
        h["workers"].append({k: (None if w.get(k) is None else _text(w.get(k), f"workers.{k}"))
                             for k in ("worker_id", "role", "task_id", "status", "branch", "worktree",
                                       "session_id", "model", "provider")})
    h["branches"] = sorted({_text(b, "branches[]") for b in _list(branches, "branches")})
    for wt in _list(worktrees, "worktrees"):
        h["worktrees"].append({"path": _text(wt.get("path"), "worktrees.path"),
                               "branch": _text(wt.get("branch"), "worktrees.branch")})
    for c in _list(commits, "commits"):
        h["commits"].append({"sha": _text(c.get("sha"), "commits.sha").lower(),
                             "branch": _text(c.get("branch", ""), "commits.branch"),
                             "subject": _text(c.get("subject", ""), "commits.subject")})
    for p in _list(pull_requests, "pull_requests"):
        num = p.get("number")
        if not isinstance(num, int) or isinstance(num, bool) or num <= 0:
            raise HandoffError(f"pull_requests.number must be a positive integer, got {num!r}")
        h["pull_requests"].append({"number": num, "state": _text(p.get("state", ""), "pull_requests.state"),
                                   "branch": _text(p.get("branch", ""), "pull_requests.branch")})
    for t in _list(tests, "tests"):
        label = _label(t.get("label"), "tests.label")
        entry = {"command": _text(t.get("command"), "tests.command"), "result": _text(t.get("result", ""),
                 "tests.result"), "label": label}
        if label == "VERIFIED" and not entry["result"].strip():
            raise HandoffError("a VERIFIED test needs its recorded command output (result)")
        h["tests"].append(entry)
    for e in _list(evidence, "evidence"):
        h["evidence"].append({"claim": _text(e.get("claim"), "evidence.claim"),
                              "label": _label(e.get("label"), "evidence.label"),
                              "source": _text(e.get("source", ""), "evidence.source")})
    problems = validate_schema(h)
    if problems:
        raise HandoffError("handoff is malformed: " + "; ".join(problems))
    _refuse_sensitive(h)
    return h


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _refuse_sensitive(h: Mapping[str, Any]) -> None:
    if contains_secret(h):
        raise HandoffError("handoff contains credential-shaped text; refused (nothing written)")
    for s in _strings(h):
        flags = [f for f in sensitivity_flags(s) if f in ("secret", "transcript")]
        if flags:
            raise HandoffError(f"handoff contains {', '.join(flags)}-like text; refused (nothing written)")


# ------------------------------------------------------------------ schema and hash

_REQUIRED: Dict[str, type] = {
    "schema_version": int, "kind": str, "handoff_id": str, "session_id": str, "role": str, "created_at": str,
    "objective": str, "decisions": list, "plan": list, "task_graph": list, "workers": list, "branches": list,
    "worktrees": list, "commits": list, "pull_requests": list, "tests": list, "evidence": list, "blockers": list,
    "next_actions": list, "resources": dict,
}


def validate_schema(h: Any) -> List[str]:
    """Return a list of schema problems (empty = valid)."""
    if not isinstance(h, dict):
        return ["handoff is not an object"]
    probs = []
    for k, t in _REQUIRED.items():
        if k not in h:
            probs.append(f"missing field {k}")
        elif not isinstance(h[k], t) or (t is int and isinstance(h[k], bool)):
            probs.append(f"field {k} has type {type(h[k]).__name__}, expected {t.__name__}")
    if probs:
        return probs
    if h["schema_version"] != HANDOFF_SCHEMA_VERSION:
        probs.append(f"schema_version {h['schema_version']} is not {HANDOFF_SCHEMA_VERSION}")
    if h["kind"] != HANDOFF_KIND:
        probs.append(f"kind is {h['kind']!r}")
    if not _HANDOFF_ID.match(h["handoff_id"]):
        probs.append("handoff_id is malformed")
    if parse_iso(h["created_at"]) is None:
        probs.append("created_at is not an ISO timestamp")
    if not h["objective"].strip():
        probs.append("objective is empty")
    for b in h["branches"]:
        if not isinstance(b, str) or not _BRANCH.match(b) or ".." in b:
            probs.append(f"branch name {b!r} is malformed")
    for wt in h["worktrees"]:
        if not isinstance(wt, dict) or not wt.get("path") or not wt.get("branch"):
            probs.append("worktree entry needs path and branch")
    for c in h["commits"]:
        if not isinstance(c, dict) or not _SHA.match(str(c.get("sha", ""))):
            probs.append(f"commit sha {c.get('sha') if isinstance(c, dict) else c!r} is malformed")
    for p in h["pull_requests"]:
        if not isinstance(p, dict) or not isinstance(p.get("number"), int) or p["number"] <= 0:
            probs.append("pull request entry needs a positive number")
    for group in ("tests", "evidence"):
        for e in h[group]:
            if not isinstance(e, dict) or e.get("label") not in EVIDENCE_LABELS:
                probs.append(f"{group} entry has no valid label")
    res = h["resources"]
    ctx, usage = res.get("context"), res.get("usage")
    if not isinstance(ctx, dict) or not isinstance(usage, dict):
        probs.append("resources need context and usage")
    else:
        for name, sig in [("usage", usage)] + [(f"context.{k}", v) for k, v in ctx.items()]:
            if not isinstance(sig, dict) or sig.get("label") not in EVIDENCE_LABELS:
                probs.append(f"resources.{name} has no valid label")
            elif sig["label"] == "UNAVAILABLE" and sig.get("value") is not None:
                probs.append(f"resources.{name} is UNAVAILABLE but carries a value")
    return probs


def canonical_bytes(h: Mapping[str, Any]) -> bytes:
    return json.dumps(h, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def content_hash(h: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(h)).hexdigest()


# ------------------------------------------------------------------ writing

@dataclass(frozen=True)
class HandoffRecord:
    handoff_id: str
    json_path: str
    md_path: str
    content_hash: str
    written_at: str


def handoff_dir(store: StateStore) -> Path:
    return store.directory / HANDOFF_SUBDIR


def render_markdown(h: Mapping[str, Any], digest: str) -> str:
    def bullets(items: Iterable[str]) -> List[str]:
        items = list(items)
        return [f"- {i}" for i in items] if items else ["- (none)"]

    def sig(d: Mapping[str, Any]) -> str:
        if d.get("label") == "UNAVAILABLE":
            return f"UNAVAILABLE ({d.get('reason', '')})"
        return f"{d.get('value')} [{d.get('label')}]"

    lines = [f"# Session handoff {h['handoff_id']}", "",
             f"- Content hash: `{digest}`",
             f"- Session: `{h['session_id']}` ({h['role']})", f"- Created: {h['created_at']}",
             "- Verify before use: a replacement must re-read the JSON file and confirm this hash.", "",
             "## Objective", "", h["objective"], "", "## Decisions", "", *bullets(h["decisions"]), "",
             "## Current plan", "", *bullets(h["plan"]), "", "## Task graph", "",
             *bullets(f"{t['task_id']} [{t['status']}] {t['title']}"
                      + (f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else "")
                      for t in h["task_graph"]), "",
             "## Workers", "",
             *bullets(f"{w['worker_id']} {w['role']} task={w['task_id']} status={w['status']} branch={w['branch']}"
                      for w in h["workers"]), "",
             "## Branches and worktrees", "", *bullets(h["branches"]),
             *bullets(f"worktree {wt['path']} on {wt['branch']}" for wt in h["worktrees"]), "",
             "## Commits and pull requests", "",
             *bullets(f"{c['sha']} {c['branch']} {c['subject']}".rstrip() for c in h["commits"]),
             *bullets(f"PR #{p['number']} {p['state']} {p['branch']}".rstrip() for p in h["pull_requests"]), "",
             "## Tests and evidence", "",
             *bullets(f"[{t['label']}] `{t['command']}` -> {t['result']}" for t in h["tests"]),
             *bullets(f"[{e['label']}] {e['claim']}" for e in h["evidence"]), "",
             "## Blockers", "", *bullets(h["blockers"]), "", "## Next actions", "", *bullets(h["next_actions"]), "",
             "## Resources", "",
             *[f"- context {k}: {sig(v)}" for k, v in h["resources"]["context"].items()],
             f"- account usage: {sig(h['resources']['usage'])}",
             f"- {h['resources']['note']}", ""]
    return "\n".join(lines)


def write_handoff(store: StateStore, h: Mapping[str, Any]) -> HandoffRecord:
    """Write ``<id>.json`` and ``<id>.md`` atomically under ``<store>/handoffs/``; record an event. Not yet verified."""
    problems = validate_schema(h)
    if problems:
        raise HandoffError("refusing to write a malformed handoff: " + "; ".join(problems))
    _refuse_sensitive(h)
    digest = content_hash(h)
    d = handoff_dir(store)
    os.makedirs(str(d), exist_ok=True)
    jp, mp = d / f"{h['handoff_id']}.json", d / f"{h['handoff_id']}.md"
    doc = {"schema_version": HANDOFF_SCHEMA_VERSION, "kind": HANDOFF_KIND, "content_hash": digest, "handoff": h}
    store.atomic_write_bytes(jp, (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
                             .encode("utf-8"))
    store.atomic_write_bytes(mp, render_markdown(h, digest).encode("utf-8"))
    written_at = utc_iso(store.clock())
    store.append_event("handoff_written", handoff_id=h["handoff_id"], content_hash=digest,
                       session_id=h["session_id"], verified=False)
    return HandoffRecord(h["handoff_id"], str(jp), str(mp), digest, written_at)


# ------------------------------------------------------------------ verification

Check = Tuple[bool, str]


class RefCheckers(Protocol):
    """Injected existence checks. Each returns (exists, detail). Exceptions count as NOT confirmed."""

    def branch(self, name: str) -> Check: ...
    def worktree(self, path: str, branch: str) -> Check: ...
    def commit(self, sha: str) -> Check: ...
    def pull_request(self, number: int) -> Check: ...


@dataclass(frozen=True)
class VerifiedHandoff:
    """Proof that a handoff passed ``verify_handoff``. Only that function can create one (module-private seal)."""
    handoff_id: str
    content_hash: str
    json_path: str
    md_path: str
    session_id: str
    verified_at: float
    seal: object = field(repr=False, compare=False, default=None)

    def __post_init__(self):
        if self.seal is not _SEAL:
            raise UnverifiedHandoffError("a VerifiedHandoff can only be produced by handoff.verify_handoff")


@dataclass
class VerificationReport:
    status: str
    handoff_id: str
    reasons: List[str] = field(default_factory=list)
    checked: Dict[str, int] = field(default_factory=dict)
    content_hash: Optional[str] = None
    verified: Optional[VerifiedHandoff] = None

    @property
    def safe(self) -> bool:
        return self.status == SAFE and self.verified is not None

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "handoff_id": self.handoff_id, "reasons": list(self.reasons),
                "checked": dict(self.checked), "content_hash": self.content_hash}


def _run_check(fn: Optional[Callable[..., Check]], what: str, *args: Any) -> Optional[str]:
    """None when confirmed, else the reason."""
    if fn is None:
        return f"{what}: no checker available, cannot confirm"
    try:
        ok, detail = fn(*args)
    except Exception as e:  # noqa: BLE001 - a failing checker never confirms anything
        return f"{what}: checker failed ({e.__class__.__name__})"
    return None if ok is True else f"{what}: not found ({detail})"


def verify_handoff(store: StateStore, handoff_id: str, checkers: Any, *,
                   expected_hash: Optional[str] = None) -> VerificationReport:
    """Read the handoff back from disk and confirm it. ``SAFE`` only when every check passes."""
    rep = VerificationReport(status=NOT_SAFE, handoff_id=str(handoff_id))
    if not _HANDOFF_ID.match(str(handoff_id)):
        rep.reasons.append("invalid handoff id")
        return _finish(store, rep)
    d = handoff_dir(store)
    jp, mp = d / f"{handoff_id}.json", d / f"{handoff_id}.md"
    try:
        raw = jp.read_bytes()
    except OSError as e:
        rep.reasons.append(f"handoff JSON missing or unreadable ({e.__class__.__name__})")
        return _finish(store, rep)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        rep.reasons.append("handoff JSON is corrupt (does not parse)")
        return _finish(store, rep)
    if not isinstance(doc, dict) or not isinstance(doc.get("handoff"), dict):
        rep.reasons.append("handoff JSON has no handoff object")
        return _finish(store, rep)
    h = doc["handoff"]
    rep.reasons.extend(f"schema: {p}" for p in validate_schema(h))
    if doc.get("schema_version") != HANDOFF_SCHEMA_VERSION or doc.get("kind") != HANDOFF_KIND:
        rep.reasons.append("schema: envelope schema_version/kind mismatch")
    actual = content_hash(h)
    rep.content_hash = actual
    if doc.get("content_hash") != actual:
        rep.reasons.append("content hash mismatch: the stored hash does not match the content")
    if expected_hash is not None and expected_hash != actual:
        rep.reasons.append("content hash mismatch: the content is not the handoff that was written")
    if h.get("handoff_id") != handoff_id:
        rep.reasons.append("handoff id in the file does not match its name")
    try:
        md = mp.read_text(encoding="utf-8")
        if actual not in md:
            rep.reasons.append("Markdown handoff does not carry the content hash")
    except OSError:
        rep.reasons.append("Markdown handoff missing or unreadable")
    try:
        _refuse_sensitive(h)
    except HandoffError as e:
        rep.reasons.append(str(e))
    if not any(r.startswith("schema:") for r in rep.reasons):
        for b in h["branches"]:
            _add(rep, "branches", _run_check(getattr(checkers, "branch", None), f"branch {b}", b))
        for wt in h["worktrees"]:
            _add(rep, "worktrees", _run_check(getattr(checkers, "worktree", None),
                                              f"worktree on {wt['branch']}", wt["path"], wt["branch"]))
        for c in h["commits"]:
            _add(rep, "commits", _run_check(getattr(checkers, "commit", None), f"commit {c['sha']}", c["sha"]))
        for p in h["pull_requests"]:
            _add(rep, "pull_requests", _run_check(getattr(checkers, "pull_request", None),
                                                  f"PR #{p['number']}", p["number"]))
    if not rep.reasons:
        rep.status = SAFE
        rep.verified = VerifiedHandoff(handoff_id=handoff_id, content_hash=actual, json_path=str(jp),
                                       md_path=str(mp), session_id=h["session_id"],
                                       verified_at=float(store.clock()), seal=_SEAL)
        if len(_ISSUED) >= _ISSUED_MAX:
            _ISSUED.pop(next(iter(_ISSUED)))
        _ISSUED[(handoff_id, actual, rep.verified.verified_at)] = rep.verified
    return _finish(store, rep)


def _add(rep: VerificationReport, group: str, reason: Optional[str]) -> None:
    rep.checked[group] = rep.checked.get(group, 0) + 1
    if reason:
        rep.reasons.append(reason)


def _finish(store: StateStore, rep: VerificationReport) -> VerificationReport:
    store.append_event("handoff_verified" if rep.status == SAFE else "handoff_not_safe",
                       handoff_id=rep.handoff_id, status=rep.status, reasons=rep.reasons[:20],
                       content_hash=rep.content_hash)
    return rep


def require_verified(verified: Any, *, now: float, max_age_s: float) -> VerifiedHandoff:
    """Gate for any action that clears, compacts or replaces a session. Raises ``UnverifiedHandoffError``."""
    if (not isinstance(verified, VerifiedHandoff) or verified.seal is not _SEAL
            or _ISSUED.get((verified.handoff_id, verified.content_hash, verified.verified_at)) is not verified):
        raise UnverifiedHandoffError("no verified handoff: the session must not be cleared or replaced")
    age = now - verified.verified_at
    if age < 0 or age > max_age_s:
        raise UnverifiedHandoffError(f"handoff verification is {int(age)}s old (limit {int(max_age_s)}s): "
                                     "verify it again first")
    return verified


# ------------------------------------------------------------------ real checkers (NOT TESTED live)

class GitRefChecker:
    """Branch, worktree and commit checks through ``git`` (argument lists, ``shell=False``). PRs via ``pr_checker``."""

    def __init__(self, repo: str, git: str = "git", timeout_s: float = 20.0,
                 run: Callable[..., Any] = subprocess.run, worktree_checker: Optional[Callable[..., Check]] = None,
                 pr_checker: Optional[Callable[[int], Check]] = None) -> None:
        self.repo, self.git, self.timeout_s, self._run = repo, git, timeout_s, run
        self._worktree, self._pr = worktree_checker, pr_checker

    def _git(self, *args: str) -> Tuple[int, str]:
        try:
            r = self._run([self.git, "-C", self.repo, *args], capture_output=True, text=True,
                          timeout=self.timeout_s, shell=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return -1, e.__class__.__name__
        return r.returncode, (r.stdout or "").strip()

    def branch(self, name: str) -> Check:
        if not _BRANCH.match(name) or ".." in name:
            return False, "malformed branch name"
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            code, _ = self._git("rev-parse", "--verify", "--quiet", ref)
            if code == 0:
                return True, ref
        return False, "no local or origin ref"

    def commit(self, sha: str) -> Check:
        if not _SHA.match(sha):
            return False, "malformed sha"
        code, _ = self._git("cat-file", "-e", f"{sha}^{{commit}}")
        return (code == 0), ("present" if code == 0 else "not in object store")

    def worktree(self, path: str, branch: str) -> Check:
        if self._worktree is not None:
            return self._worktree(path, branch)
        code, out = self._git("worktree", "list", "--porcelain")
        if code != 0:
            return False, "git worktree list failed"
        want = os.path.normcase(os.path.normpath(path))
        cur = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                cur = os.path.normcase(os.path.normpath(line[len("worktree "):]))
            elif line.startswith("branch ") and cur == want:
                return (line.strip() == f"branch refs/heads/{branch}"), line.strip()
        return False, "worktree not registered"

    def pull_request(self, number: int) -> Check:
        if self._pr is None:
            return False, "no pull request checker configured"
        return self._pr(number)


class GhPrChecker:
    """``gh pr view <n> --json number`` (argument list). NOT TESTED live."""

    def __init__(self, repo: Optional[str] = None, gh: str = "gh", timeout_s: float = 30.0,
                 run: Callable[..., Any] = subprocess.run) -> None:
        self.repo, self.gh, self.timeout_s, self._run = repo, gh, timeout_s, run

    def __call__(self, number: int) -> Check:
        if not isinstance(number, int) or number <= 0:
            return False, "malformed PR number"
        argv = [self.gh, "pr", "view", str(number), "--json", "number"]
        if self.repo:
            argv += ["--repo", self.repo]
        try:
            r = self._run(argv, capture_output=True, text=True, timeout=self.timeout_s, shell=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, e.__class__.__name__
        if r.returncode != 0:
            return False, "gh pr view failed"
        try:
            return (json.loads(r.stdout or "{}").get("number") == number), "gh pr view"
        except ValueError:
            return False, "unparseable gh output"
