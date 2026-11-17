"""Library-level creator review workflow (T-DNA-002). No UI: pure functions over a `DNAStore`.

  review_queue(store)               what a human should look at, in a fixed order, with the reasons
  apply_decisions(store, json)      the creator's decisions as one validated, all-or-nothing batch
  diff_exports(a, b)                what changed between two `store.export()` snapshots

All three are deterministic: same store or same exports in, same output out. None of them rewrites an
observation: decisions are appended events (a correction creates a new creator-authored trait and keeps
the old one with all its evidence), and the diff only reads.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from . import confidence as conf
from .schema import Category, DNAError, Layer, Scope, Status

DECISIONS_SCHEMA = "creator_dna.review_decisions/1"
DIFF_SCHEMA = "creator_dna.profile_diff/1"
QUEUE_SCHEMA = "creator_dna.review_queue/1"

# reason code -> priority (1 = look first)
_PRIORITY = {
    "legacy_possible_revocation": 1, "contested": 2, "confirmed_over_weak_footage": 3, "unrated_only": 4,
    "unattributed_evidence": 4, "hypothesis": 5, "revoked_evidence": 6, "quarantined_evidence": 6,
}


class ReviewDecisionError(DNAError):
    """A decision batch was refused as a whole. `problems` lists every reason; nothing was changed."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


# ===================================================================================== review queue
def review_queue(store, cfg: conf.ScoringConfig = conf.DEFAULT_CONFIG) -> dict:
    """Traits that deserve a human look, most urgent first (priority, then trait id)."""
    items = []
    for tid in sorted(store.traits):
        tr = store.traits[tid]
        if tr.status not in (Status.ACTIVE, Status.CONFIRMED):
            continue
        a = tr.assessment(cfg)
        reasons = []

        def add(code: str, detail: str) -> None:
            reasons.append({"code": code, "priority": _PRIORITY[code], "detail": detail})

        n_legacy = sum(1 for i in tr.evidence_items if i.legacy_possible_revocation)
        if n_legacy:
            add("legacy_possible_revocation", f"{n_legacy} pre-v4 rejection(s) may have been attribution revocations")
        if a.counter_sources > 0 and a.consistency < cfg.stable_min_consistency:
            add("contested", f"{a.counter_sources} counter source(s) against {a.support_sources} supporting; "
                             f"consistency {a.consistency:.2f}")
        if a.confidence_basis == "creator_confirmation_over_weak_footage":
            add("confirmed_over_weak_footage", f"confirmed by the creator; footage alone gives {a.footage_confidence:.2f}")
        if tr.layer is not Layer.SELF_REPORT and a.unrated_examples > 0 and a.support_examples == 0:
            add("unrated_only", f"{a.unrated_examples} example(s) have no supplied confidence and carry no weight")
        if a.unattributed_examples > 0:
            add("unattributed_evidence", f"{a.unattributed_examples} footage example(s) carry no creator attribution")
        if tr.layer is Layer.HYPOTHESIS and tr.status is Status.ACTIVE:
            add("hypothesis", "tentative: not yet confirmed, corrected or rejected by the creator")
        n_rev = sum(1 for i in tr.evidence_items if i.attribution_revoked)
        if n_rev:
            add("revoked_evidence", f"{n_rev} example(s) excluded because their attribution was revoked")
        n_q = sum(1 for i in tr.evidence_items if i.quarantined)
        if n_q:
            add("quarantined_evidence", f"{n_q} example(s) are quarantined (held out of the profile)")
        if not reasons:
            continue
        reasons.sort(key=lambda r: (r["priority"], r["code"]))
        items.append({"trait_id": tid, "statement": tr.statement, "category": tr.category.value,
                      "layer": tr.layer.value, "status": tr.status.value,
                      "confidence": round(a.confidence, 3), "scope": a.scope.value,
                      "priority": reasons[0]["priority"], "reasons": reasons})
    items.sort(key=lambda i: (i["priority"], i["trait_id"]))
    return {"schema": QUEUE_SCHEMA, "count": len(items), "items": items}


# ================================================================================== apply decisions
_ACTIONS: dict[str, tuple[frozenset, frozenset]] = {
    # action: (required keys, optional keys), besides "action" itself
    "confirm": (frozenset({"trait_id"}), frozenset({"note", "scope"})),
    "reject": (frozenset({"trait_id"}), frozenset({"note"})),
    "reopen": (frozenset({"trait_id"}), frozenset({"note"})),
    "correct": (frozenset({"trait_id", "new_statement", "note"}), frozenset({"category"})),
    "reject_evidence": (frozenset({"trait_id", "evidence_id", "note"}), frozenset()),
    "restore_evidence": (frozenset({"trait_id", "evidence_id"}), frozenset({"note"})),
}
_SCOPES = tuple(s.value for s in Scope if s is not Scope.UNASSESSED)


def _parse(decisions_json: Any) -> Any:
    if isinstance(decisions_json, (bytes, bytearray)):
        decisions_json = bytes(decisions_json).decode("utf-8")
    if isinstance(decisions_json, str):
        try:
            return json.loads(decisions_json)
        except json.JSONDecodeError as exc:
            raise ReviewDecisionError([f"decisions are not valid JSON ({exc.msg} at line {exc.lineno})"]) from exc
    return decisions_json


def validate_decisions(decisions_json: Any) -> list[dict]:
    """Strict schema check of the whole batch. Returns the decisions or raises ReviewDecisionError listing
    every problem."""
    doc = _parse(decisions_json)
    if not isinstance(doc, Mapping) or doc.get("schema") != DECISIONS_SCHEMA or set(doc) - {"schema", "decisions"}:
        raise ReviewDecisionError([f"decisions must be an object with exactly 'schema': {DECISIONS_SCHEMA!r} and 'decisions'"])
    decisions = doc.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ReviewDecisionError(["'decisions' must be a non-empty list"])
    problems: list[str] = []
    for n, d in enumerate(decisions):
        if not isinstance(d, Mapping):
            problems.append(f"decision {n}: must be an object")
            continue
        action = d.get("action")
        if action not in _ACTIONS:
            problems.append(f"decision {n}: unknown action {action!r}; allowed {sorted(_ACTIONS)}")
            continue
        req, opt = _ACTIONS[action]
        keys = set(d) - {"action"}
        for k in sorted(req - keys):
            problems.append(f"decision {n} ({action}): missing '{k}'")
        for k in sorted(keys - req - opt):
            problems.append(f"decision {n} ({action}): unexpected key '{k}'")
        for k in sorted(keys & (req | opt)):
            v = d[k]
            if not isinstance(v, str) or (k != "note" and not v.strip()) or (k in req and k == "note" and not v.strip()):
                problems.append(f"decision {n} ({action}): '{k}' must be a non-empty string")
        if action == "confirm" and isinstance(d.get("scope"), str) and d["scope"] not in _SCOPES:
            problems.append(f"decision {n} (confirm): scope must be one of {list(_SCOPES)}")
        if action == "correct" and isinstance(d.get("category"), str) and d["category"] not in [c.value for c in Category]:
            problems.append(f"decision {n} (correct): unknown category {d['category']!r}")
    if problems:
        raise ReviewDecisionError(problems)
    return [dict(d) for d in decisions]


def _run_one(store, d: dict, actor: str) -> str | None:
    a = d["action"]
    if a == "confirm":
        store.confirm(d["trait_id"], d.get("note", ""), Scope(d["scope"]) if d.get("scope") else None, actor)
    elif a == "reject":
        store.reject(d["trait_id"], d.get("note", ""), actor)
    elif a == "reopen":
        store.reopen(d["trait_id"], d.get("note", ""), actor)
    elif a == "correct":
        return store.correct(d["trait_id"], d["new_statement"], d["note"],
                             Category(d["category"]) if d.get("category") else None, actor=actor)
    elif a == "reject_evidence":
        store.reject_evidence(d["trait_id"], d["evidence_id"], d["note"], actor)
    elif a == "restore_evidence":
        store.restore_evidence(d["trait_id"], d["evidence_id"], d.get("note", ""), actor)
    return None


def apply_decisions(store, decisions_json: Any, actor: str = "creator", dry_run: bool = False) -> dict:
    """Apply the creator's decisions all-or-nothing.

    1. The batch is validated strictly (schema, keys, types, closed vocabularies).
    2. It is rehearsed against a scratch replay of the real log, so decisions that depend on earlier ones
       in the same batch are judged correctly and every store rule (revoked evidence cannot be restored,
       a corrected trait cannot be corrected twice, ...) applies.
    3. Only if the rehearsal succeeds is the batch written for real (and with `dry_run=True` nothing is).
    Any failure raises ReviewDecisionError and leaves the store and its log exactly as they were."""
    if not isinstance(actor, str) or not actor.strip():
        raise ReviewDecisionError(["every decision needs an actor"])
    decisions = validate_decisions(decisions_json)

    scratch_dir = Path(tempfile.mkdtemp(prefix="dna_review_"))
    try:
        if store.log_path.exists():
            shutil.copyfile(store.log_path, scratch_dir / store.log_path.name)
        scratch = type(store)(scratch_dir, clock=lambda: "1970-01-01T00:00:00+00:00", project_id=store.project_id)
        results = []
        for n, d in enumerate(decisions):
            try:
                new_id = _run_one(scratch, d, actor)
            except DNAError as exc:
                raise ReviewDecisionError([f"decision {n} ({d['action']}): {exc}"]) from exc
            results.append({"index": n, "action": d["action"], "trait_id": d["trait_id"],
                            "new_trait_id": new_id, "status": "would_apply"})
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    before = store._seq
    if dry_run:
        return {"dry_run": True, "applied": 0, "events_before": before, "events_after": before, "results": results}
    checkpoint = (store._size, store._seq)
    try:
        for n, d in enumerate(decisions):
            results[n]["new_trait_id"] = _run_one(store, d, actor)
            results[n]["status"] = "applied"
    except Exception as exc:          # not expected after a clean rehearsal (e.g. disk error): undo completely
        store._rollback(checkpoint)
        raise ReviewDecisionError([f"batch undone, nothing applied: {exc}"]) from exc
    return {"dry_run": False, "applied": len(results), "events_before": before, "events_after": store._seq,
            "results": results}


# ======================================================================================= diff exports
_SECTIONS = ("observed_facts", "ai_interpretations", "hypotheses", "creator_self_report")


def _index(export: Mapping) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sec in _SECTIONS:
        for t in export.get(sec, []):
            sup, cnt = t.get("support", []), t.get("counterexamples", [])
            ev = {e["evidence_id"]: e for e in list(sup) + list(cnt)}
            out[t["trait_id"]] = {
                "statement": t["statement"], "layer": t["layer"], "status": t["status"],
                "confidence": t["assessment"]["confidence"], "scope": t["assessment"]["scope"],
                "band": t["assessment"]["band"], "consistency": t["assessment"]["consistency"],
                "support": sorted(e["evidence_id"] for e in sup), "counter": sorted(e["evidence_id"] for e in cnt),
                "rejected": sorted(i for i, e in ev.items() if e.get("rejected_by_creator")),
                "revoked": sorted(i for i, e in ev.items() if e.get("attribution_revoked")),
                "quarantined": sorted(i for i, e in ev.items() if e.get("quarantined")),
                "supersedes": t.get("supersedes"), "superseded_by": t.get("superseded_by"), "active": True}
    for t in export.get("creator_rejected_or_corrected", []):
        out.setdefault(t["trait_id"], {
            "statement": t["statement"], "layer": None, "status": t["status"], "confidence": None, "scope": None,
            "band": None, "consistency": None, "support": [], "counter": [], "rejected": [], "revoked": [],
            "quarantined": [], "supersedes": None, "superseded_by": t.get("superseded_by"), "active": False})
    return out


def _brief(tid: str, t: dict) -> dict:
    return {"trait_id": tid, "statement": t["statement"], "layer": t["layer"], "status": t["status"],
            "confidence": t["confidence"], "scope": t["scope"]}


def diff_exports(a: Mapping, b: Mapping) -> dict:
    """What changed from export `a` (older) to export `b` (newer). Only trait and evidence facts are compared;
    timestamps and event counters are ignored, so equal profiles give an empty diff."""
    ia, ib = _index(a), _index(b)
    added = [_brief(t, ib[t]) for t in sorted(set(ib) - set(ia))]
    removed = [_brief(t, ia[t]) for t in sorted(set(ia) - set(ib))]
    changes, contradictions = [], []
    for tid in sorted(set(ia) & set(ib)):
        x, y = ia[tid], ib[tid]
        ch: dict[str, Any] = {}
        for k in ("layer", "status", "scope", "band"):
            if x[k] != y[k]:
                ch[k] = {"from": x[k], "to": y[k]}
        if x["confidence"] != y["confidence"]:
            delta = None if x["confidence"] is None or y["confidence"] is None else round(y["confidence"] - x["confidence"], 3)
            ch["confidence"] = {"from": x["confidence"], "to": y["confidence"], "delta": delta}
        if x["superseded_by"] != y["superseded_by"]:
            ch["superseded_by"] = {"from": x["superseded_by"], "to": y["superseded_by"]}
        new_support = sorted(set(y["support"]) - set(x["support"]))
        new_counter = sorted(set(y["counter"]) - set(x["counter"]))
        excl = {k: sorted(set(y[k]) - set(x[k])) for k in ("rejected", "revoked", "quarantined")}
        back = {k: sorted(set(x[k]) - set(y[k]) & set(y["support"] + y["counter"]))
                for k in ("rejected", "quarantined")}
        if new_support:
            ch["new_support"] = new_support
        if new_counter:
            ch["new_counter"] = new_counter
        if any(excl.values()):
            ch["newly_excluded"] = {k: v for k, v in excl.items() if v}
        if any(back.values()):
            ch["returned_to_profile"] = {k: v for k, v in back.items() if v}
        if ch:
            changes.append({"trait_id": tid, "statement": y["statement"], "changes": ch})
        if y["active"]:
            if new_counter and y["support"]:
                why = "counter-evidence added to a supported trait"
                if y["status"] == "confirmed":
                    why = "counter-evidence added against a trait the creator confirmed"
                contradictions.append({"trait_id": tid, "statement": y["statement"], "reason": why,
                                       "evidence_ids": new_counter})
            elif ch.get("scope", {}).get("to") == "contextual":
                contradictions.append({"trait_id": tid, "statement": y["statement"],
                                       "reason": "scope became contextual", "evidence_ids": []})
    return {"schema": DIFF_SCHEMA, "traits_added": added, "traits_removed": removed, "trait_changes": changes,
            "contradictions": contradictions,
            "summary": {"added": len(added), "removed": len(removed), "changed": len(changes),
                        "contradictions": len(contradictions)}}
