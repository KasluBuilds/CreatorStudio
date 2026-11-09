"""Validation and deterministic progress calculation for PROJECT_STATUS.json.

Standard library only. No clock, no network, no randomness: the same file always produces the same
numbers, independent of the order of milestones.

SCORING LADDER (fixed; change it only by editing this module AND PROJECT_OPERATING_PROTOCOL.md)

Implementation credit comes from ``implementation.state``:

    IMPLEMENTED, UNDER_REVIEW, VERIFIED -> 1
    IN_PROGRESS                          -> 1/2   (convention for partially built work)
    NOT_STARTED, BLOCKED, DEFERRED, FAILED -> 0

Verification credit comes from ``verification.level`` (what was actually demonstrated), and only
while ``verification.state`` is IN_PROGRESS, UNDER_REVIEW or VERIFIED:

    NONE                 0     nothing demonstrated
    UNIT_ISOLATED        1/4   unit tests (synthetic data / fakes) pass
    INTEGRATION          1/2   several components, or a real tool on synthetic/disposable input
    REAL_INPUT_REVIEWED  9/10  run on real input or in the real target setting AND reviewed by a human
    REPEATED_REAL_USE    1     the above on at least two separate occasions (different dates)

Verification credit of a milestone is capped at its implementation credit, so the total verified
percentage can never exceed the total implemented percentage. A level above NONE is accepted only
with evidence of the required kind (see EVIDENCE_RULES); otherwise validation fails.

"reported" evidence (a project document stating that something was observed earlier) is accepted for
UNIT_ISOLATED and INTEGRATION only, and milestones that rest on it alone are flagged ``reported_only``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
import subprocess
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, List

SCHEMA_VERSION = 1
STATES = ("NOT_STARTED", "IN_PROGRESS", "IMPLEMENTED", "UNDER_REVIEW", "VERIFIED", "BLOCKED", "DEFERRED", "FAILED")

IMPLEMENTATION_CREDIT = {
    "NOT_STARTED": Fraction(0), "IN_PROGRESS": Fraction(1, 2), "IMPLEMENTED": Fraction(1),
    "UNDER_REVIEW": Fraction(1), "VERIFIED": Fraction(1), "BLOCKED": Fraction(0),
    "DEFERRED": Fraction(0), "FAILED": Fraction(0),
}
VERIFICATION_LEVELS = {
    "NONE": Fraction(0), "UNIT_ISOLATED": Fraction(1, 4), "INTEGRATION": Fraction(1, 2),
    "REAL_INPUT_REVIEWED": Fraction(9, 10), "REPEATED_REAL_USE": Fraction(1),
}
VERIFIED_LEVELS = ("REAL_INPUT_REVIEWED", "REPEATED_REAL_USE")
COUNTED_VERIFICATION_STATES = ("IN_PROGRESS", "UNDER_REVIEW", "VERIFIED")
IMPLEMENTED_STATES = ("IMPLEMENTED", "UNDER_REVIEW", "VERIFIED")
STARTED_STATES = ("IN_PROGRESS",) + IMPLEMENTED_STATES

MILESTONE_ID = re.compile(r"^M-[A-Z]{3}-\d{2}$")
EVIDENCE_ID = re.compile(r"^E-[A-Z0-9][A-Z0-9-]*$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
MIN_RATIONALE = 20
WEIGHT_RANGE = (1, 10)

# evidence type -> fields that must be present (non-empty strings) besides id, type, date, summary
EVIDENCE_RULES = {
    "test_run": ("ref", "command", "result", "commit"),
    "observation": ("ref", "observer"),
    "reported": ("ref",),
    "human_review": ("ref", "reviewer"),
    "commit": ("commit",),
}
EVIDENCE_KEYS = {"id", "type", "date", "summary", "ref", "command", "result", "commit", "reviewer", "observer",
                 "real_input"}
LEVEL_ORDER = list(VERIFICATION_LEVELS)
WEIGHTS_STATUSES = ("PROPOSED", "APPROVED")
GATE_STATUSES = ("PENDING", "TRIGGERED", "ACKNOWLEDGED")
GATE_ID = re.compile(r"^G-[A-Z0-9-]+$")
# A human_review must be by a human: these name prefixes are rejected as reviewers/approvers.
NON_HUMAN_PREFIXES = ("claude", "chatgpt", "gpt", "openai", "anthropic", "assistant")


class StatusError(ValueError):
    """Raised with every problem found, not just the first."""

    def __init__(self, problems: List[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _nonempty(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def is_human_name(name: Any) -> bool:
    return _nonempty(name) and not name.strip().lower().startswith(NON_HUMAN_PREFIXES)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_date(v: Any) -> bool:
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return False
    try:
        _dt.date.fromisoformat(v)
        return True
    except ValueError:
        return False


def _validate_evidence(eid: str, rec: Any, problems: List[str]) -> None:
    where = f"evidence[{eid}]"
    if not isinstance(rec, dict):
        problems.append(f"{where}: must be an object")
        return
    if not EVIDENCE_ID.match(eid):
        problems.append(f"{where}: id must match {EVIDENCE_ID.pattern}")
    if rec.get("id") != eid:
        problems.append(f"{where}: 'id' field must equal its key")
    unknown = set(rec) - EVIDENCE_KEYS
    if unknown:
        problems.append(f"{where}: unknown keys {sorted(unknown)}")
    typ = rec.get("type")
    if typ not in EVIDENCE_RULES:
        problems.append(f"{where}: type must be one of {sorted(EVIDENCE_RULES)}")
        return
    if not _valid_date(rec.get("date")):
        problems.append(f"{where}: date must be a real YYYY-MM-DD date")
    if not _nonempty(rec.get("summary")):
        problems.append(f"{where}: summary is required")
    for field in EVIDENCE_RULES[typ]:
        if not _nonempty(rec.get(field)):
            problems.append(f"{where}: {typ} evidence requires '{field}'")
    if typ in ("test_run", "commit") and _nonempty(rec.get("commit")) and not SHA40.match(rec["commit"]):
        problems.append(f"{where}: commit must be a full 40-character lowercase hex SHA")
    if "real_input" in rec and not isinstance(rec["real_input"], bool):
        problems.append(f"{where}: real_input must be true or false")
    if typ == "human_review" and _nonempty(rec.get("reviewer")) and not is_human_name(rec["reviewer"]):
        problems.append(f"{where}: a human_review must name a human reviewer, not an AI agent")


def _evidence_supports(level: str, records: List[dict]) -> List[str]:
    """Return problems if `records` (already individually valid) cannot support `level`."""
    types = [r["type"] for r in records]
    if level == "NONE":
        return []
    if level == "UNIT_ISOLATED":
        return [] if any(t == "test_run" for t in types) else ["UNIT_ISOLATED requires a test_run evidence record"]
    if level == "INTEGRATION":
        ok = any(t in ("test_run", "observation", "reported") for t in types)
        return [] if ok else ["INTEGRATION requires a test_run, observation or reported evidence record"]
    reviews = [r for r in records if r["type"] == "human_review"]
    problems = []
    if not reviews:
        problems.append(f"{level} requires a human_review evidence record")
    if not any(t in ("test_run", "observation") for t in types):
        problems.append(f"{level} requires a test_run or observation evidence record (reported/review alone is not enough)")
    if reviews and not any(r.get("real_input") is True for r in reviews):
        problems.append(f"{level} requires a human_review record with real_input: true "
                        "(the reviewer attests that the material reviewed was real input, not synthetic)")
    if level == "REPEATED_REAL_USE" and len({r["date"] for r in reviews}) < 2:
        problems.append("REPEATED_REAL_USE requires human_review records on at least two different dates")
    return problems


def _validate_header(doc: Dict[str, Any]) -> List[str]:
    """Top-level fields that the generated handoff relies on."""
    problems = []
    if not _valid_date(doc.get("updated")):
        problems.append("'updated' must be a real YYYY-MM-DD date")
    if doc.get("weights_status") not in WEIGHTS_STATUSES:
        problems.append(f"'weights_status' must be one of {', '.join(WEIGHTS_STATUSES)}")
    if doc.get("weights_status") == "APPROVED":
        if not is_human_name(doc.get("weights_approved_by")):
            problems.append("APPROVED weights need 'weights_approved_by' naming a human (not an AI agent)")
        if not _valid_date(doc.get("weights_approved_date")):
            problems.append("APPROVED weights need a real 'weights_approved_date'")
    gates = doc.get("user_action_gates")
    if gates is not None and not isinstance(gates, list):
        problems.append("'user_action_gates' must be a list")
    for key in ("open_decisions", "next_actions"):
        if not isinstance(doc.get(key), list) or not all(_nonempty(x) for x in doc[key]):
            problems.append(f"'{key}' must be a list of non-empty strings")
    base = doc.get("baseline")
    if not isinstance(base, dict):
        problems.append("'baseline' must be an object")
        return problems
    for key in ("commit", "branch", "test_command", "scope"):
        if not _nonempty(base.get(key)):
            problems.append(f"baseline.{key} is required")
    if _nonempty(base.get("commit")) and not SHA40.match(base["commit"]):
        problems.append("baseline.commit must be a full 40-character lowercase hex SHA")
    if not _valid_date(base.get("date")):
        problems.append("baseline.date must be a real YYYY-MM-DD date")
    for key in ("tests_total", "failures", "errors", "skipped"):
        if not _is_int(base.get(key)) or base[key] < 0:
            problems.append(f"baseline.{key} must be a non-negative integer")
    return problems


def validate(doc: Any) -> List[str]:
    """Return a list of problems (empty list = valid). Never raises for bad input."""
    problems: List[str] = []
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    problems.extend(_validate_header(doc))
    evidence = doc.get("evidence")
    if not isinstance(evidence, dict):
        problems.append("'evidence' must be an object keyed by evidence id")
        evidence = {}
    for eid, rec in evidence.items():
        _validate_evidence(str(eid), rec, problems)
    milestones = doc.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        problems.append("'milestones' must be a non-empty list")
        return problems

    ids: Dict[str, dict] = {}
    for idx, m in enumerate(milestones):
        label = f"milestones[{idx}]"
        if not isinstance(m, dict):
            problems.append(f"{label}: must be an object")
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not MILESTONE_ID.match(mid):
            problems.append(f"{label}: id must match {MILESTONE_ID.pattern}")
            continue
        label = mid
        if mid in ids:
            problems.append(f"{label}: duplicate milestone id")
            continue
        ids[mid] = m
        for field in ("title", "description", "phase"):
            if not _nonempty(m.get(field)):
                problems.append(f"{label}: '{field}' is required")
        w = m.get("weight")
        if not _is_int(w) or not (WEIGHT_RANGE[0] <= w <= WEIGHT_RANGE[1]):
            problems.append(f"{label}: weight must be an integer {WEIGHT_RANGE[0]}..{WEIGHT_RANGE[1]}")
        rat = m.get("weight_rationale")
        if not isinstance(rat, str) or len(rat.strip()) < MIN_RATIONALE:
            problems.append(f"{label}: weight_rationale must explain the weight (>= {MIN_RATIONALE} characters)")
        for lst in ("open_questions", "blockers", "depends_on"):
            if not isinstance(m.get(lst), list) or not all(_nonempty(x) for x in m.get(lst, [])):
                problems.append(f"{label}: '{lst}' must be a list of non-empty strings")
        commit = m.get("commit")
        if commit is not None and not (isinstance(commit, str) and SHA40.match(commit)):
            problems.append(f"{label}: commit must be null or a full 40-character lowercase hex SHA")

        impl, ver = m.get("implementation"), m.get("verification")
        if not isinstance(impl, dict) or impl.get("state") not in STATES or not _nonempty(impl.get("summary")):
            problems.append(f"{label}: implementation needs a valid 'state' ({', '.join(STATES)}) and a 'summary'")
            continue
        if not isinstance(ver, dict) or ver.get("state") not in STATES or not _nonempty(ver.get("summary")) \
                or ver.get("level") not in VERIFICATION_LEVELS or not isinstance(ver.get("evidence"), list):
            problems.append(f"{label}: verification needs valid 'state', 'level', 'summary' and an 'evidence' list")
            continue
        istate, vstate, level = impl["state"], ver["state"], ver["level"]
        refs = ver["evidence"]
        recs = []
        for ref in refs:
            if not isinstance(ref, str) or ref not in evidence or not isinstance(evidence[ref], dict):
                problems.append(f"{label}: evidence reference {ref!r} does not exist")
            else:
                recs.append(evidence[ref])
        if len(set(refs)) != len(refs):
            problems.append(f"{label}: duplicate evidence references")
        if recs and all(isinstance(r.get("type"), str) and r.get("type") in EVIDENCE_RULES and _valid_date(r.get("date")) for r in recs):
            for p in _evidence_supports(level, recs):
                problems.append(f"{label}: {p}")
        elif level != "NONE":
            problems.append(f"{label}: level {level} has no usable evidence")
        if level != "NONE" and istate not in STARTED_STATES:
            problems.append(f"{label}: cannot have verification level {level} while implementation is {istate}")
        if vstate == "NOT_STARTED" and level != "NONE":
            problems.append(f"{label}: verification NOT_STARTED must have level NONE")
        if vstate == "VERIFIED" and (level not in VERIFIED_LEVELS or istate not in IMPLEMENTED_STATES):
            problems.append(f"{label}: verification VERIFIED requires level {' or '.join(VERIFIED_LEVELS)} "
                            f"and an implemented milestone")
        if istate == "VERIFIED" and vstate != "VERIFIED":
            problems.append(f"{label}: implementation VERIFIED requires verification VERIFIED")
        if "BLOCKED" in (istate, vstate) and not m.get("blockers"):
            problems.append(f"{label}: BLOCKED requires at least one entry in 'blockers'")

    for mid, m in ids.items():
        for dep in m.get("depends_on", []) if isinstance(m.get("depends_on"), list) else []:
            if not isinstance(dep, str):
                continue
            if dep == mid:
                problems.append(f"{mid}: depends on itself")
            elif dep not in ids:
                problems.append(f"{mid}: depends_on unknown milestone {dep}")
    problems.extend(_dependency_cycles(ids))
    if doc.get("weights_status") == "APPROVED" and doc.get("weights_fingerprint") != weights_fingerprint(doc):
        problems.append("weights changed since they were approved (weights_fingerprint mismatch); set weights_status "
                        "to PROPOSED until Lukas approves the new weights")
    if isinstance(doc.get("user_action_gates"), list):
        problems.extend(_validate_gates(doc, ids))
    return problems


def weights_fingerprint(doc: Dict[str, Any]) -> str:
    """SHA-256 over the sorted 'id:weight' lines: any change to a weight, or to the set of milestones, changes it."""
    lines = sorted(f"{m.get('id')}:{m.get('weight')}" for m in doc.get("milestones", []) if isinstance(m, dict))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def gate_triggered(gate: Dict[str, Any], ids: Dict[str, dict]) -> bool:
    """A gate fires when ANY trigger milestone has reached an implemented state (code exists)."""
    for mid in gate.get("trigger_milestones", []):
        m = ids.get(mid)
        if m and isinstance(m.get("implementation"), dict) and m["implementation"].get("state") in IMPLEMENTED_STATES:
            return True
    return False


def _validate_gates(doc: Dict[str, Any], ids: Dict[str, dict]) -> List[str]:
    """User-action gates: instructions to tell the human something once a milestone is reached.

    A gate whose trigger has fired but whose status is still PENDING makes validation FAIL, so the reminder
    cannot be overlooked: whoever advances the milestone must also mark the gate TRIGGERED (and tell Lukas).
    No external notification exists; the gate is surfaced by `validate` and by the generated handoff only.
    """
    problems: List[str] = []
    seen = set()
    for idx, g in enumerate(doc.get("user_action_gates") or []):
        label = f"user_action_gates[{idx}]"
        if not isinstance(g, dict):
            problems.append(f"{label}: must be an object")
            continue
        gid = g.get("id")
        if not isinstance(gid, str) or not GATE_ID.match(gid):
            problems.append(f"{label}: id must match {GATE_ID.pattern}")
            continue
        label = gid
        if gid in seen:
            problems.append(f"{label}: duplicate gate id")
        seen.add(gid)
        for f in ("title", "message", "notification"):
            if not _nonempty(g.get(f)):
                problems.append(f"{label}: '{f}' is required")
        for f in ("trigger_milestones", "steps"):
            if not isinstance(g.get(f), list) or not g.get(f) or not all(_nonempty(x) for x in g[f]):
                problems.append(f"{label}: '{f}' must be a non-empty list of strings")
        triggers = g.get("trigger_milestones") if isinstance(g.get("trigger_milestones"), list) else []
        for mid in triggers:
            if mid not in ids:
                problems.append(f"{label}: trigger milestone {mid} does not exist")
        if g.get("status") not in GATE_STATUSES:
            problems.append(f"{label}: status must be one of {', '.join(GATE_STATUSES)}")
            continue
        fired = gate_triggered({"trigger_milestones": triggers}, ids)
        if fired and g["status"] == "PENDING":
            problems.append(f"{label}: trigger reached ({', '.join(triggers)} implemented) but the gate is "
                            "still PENDING; set it to TRIGGERED and tell Lukas now")
        if not fired and g["status"] != "PENDING":
            problems.append(f"{label}: status {g['status']} but the trigger milestone has not reached an implemented state")
        if g["status"] == "ACKNOWLEDGED":
            if not is_human_name(g.get("acknowledged_by")) or not _valid_date(g.get("acknowledged_date")):
                problems.append(f"{label}: ACKNOWLEDGED needs 'acknowledged_by' (a human) and a real 'acknowledged_date'")
    return problems


def _dependency_cycles(ids: Dict[str, dict]) -> List[str]:
    problems, state = [], {}

    def visit(node: str, trail: List[str]) -> None:
        state[node] = 1
        deps = ids[node].get("depends_on")
        for dep in deps if isinstance(deps, list) else []:
            if not isinstance(dep, str) or dep not in ids:
                continue
            if state.get(dep) == 1:
                problems.append("dependency cycle: " + " -> ".join(trail + [node, dep]))
            elif dep not in state:
                visit(dep, trail + [node])
        state[node] = 2

    for mid in sorted(ids):
        if mid not in state:
            visit(mid, [])
    return problems


def _pct(numerator: Fraction, denominator: Fraction) -> float:
    """Exact rational arithmetic, rounded half-up to one decimal place."""
    ratio = numerator * 100 / denominator
    value = Decimal(ratio.numerator) / Decimal(ratio.denominator)
    return float(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def calculate(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Validate then score. Raises StatusError if the document is invalid."""
    problems = validate(doc)
    if problems:
        raise StatusError(problems)
    evidence = doc["evidence"]
    total = Fraction(0)
    impl_sum = Fraction(0)
    ver_sum = Fraction(0)
    rows = []
    phases: Dict[str, List[Fraction]] = {}
    for m in sorted(doc["milestones"], key=lambda x: x["id"]):
        w = Fraction(m["weight"])
        icredit = IMPLEMENTATION_CREDIT[m["implementation"]["state"]]
        ver = m["verification"]
        vcredit = Fraction(0)
        if ver["state"] in COUNTED_VERIFICATION_STATES:
            vcredit = min(VERIFICATION_LEVELS[ver["level"]], icredit)
        types = {evidence[e]["type"] for e in ver["evidence"]}
        reported_only = bool(types) and types == {"reported"} and vcredit > 0
        total += w
        impl_sum += w * icredit
        ver_sum += w * vcredit
        agg = phases.setdefault(m["phase"], [Fraction(0), Fraction(0), Fraction(0)])
        agg[0] += w
        agg[1] += w * icredit
        agg[2] += w * vcredit
        rows.append({"id": m["id"], "title": m["title"], "phase": m["phase"], "weight": m["weight"],
                     "implementation_state": m["implementation"]["state"],
                     "verification_state": ver["state"], "verification_level": ver["level"],
                     "implementation_credit": str(icredit), "verification_credit": str(vcredit),
                     "reported_only": reported_only})
    assert ver_sum <= impl_sum, "invariant violated: verification credit exceeds implementation credit"
    real = sorted(r["id"] for r in rows if r["verification_level"] in VERIFIED_LEVELS
                  and r["verification_state"] in COUNTED_VERIFICATION_STATES and r["verification_credit"] != "0")
    return {
        "weights_status": doc["weights_status"],
        "real_input_reviewed_milestones": real,
        "total_weight": int(total),
        "milestone_count": len(rows),
        "implementation_percent": _pct(impl_sum, total),
        "verification_percent": _pct(ver_sum, total),
        "reported_only_milestones": sorted(r["id"] for r in rows if r["reported_only"]),
        "by_phase": {p: {"weight": int(a[0]), "implementation_percent": _pct(a[1], a[0]),
                         "verification_percent": _pct(a[2], a[0])} for p, a in sorted(phases.items())},
        "milestones": rows,
    }


def check_computed(doc: Dict[str, Any]) -> List[str]:
    """If the document carries a 'computed' block, it must equal a fresh calculation."""
    block = doc.get("computed")
    if block is None:
        return ["'computed' block is missing (run: python -m governance score --write)"]
    result = calculate(doc)
    want = {k: result[k] for k in ("implementation_percent", "verification_percent", "total_weight", "milestone_count")}
    got = {k: block.get(k) for k in want}
    return [] if got == want else [f"'computed' block is stale: file has {got}, calculation gives {want}"]


def verify_commits(doc: Dict[str, Any], repo: Path) -> List[str]:
    """Optional: confirm every referenced commit SHA exists in the local repository."""
    shas = set()
    for rec in doc.get("evidence", {}).values():
        if isinstance(rec, dict) and rec.get("commit"):
            shas.add(rec["commit"])
    for m in doc.get("milestones", []):
        if m.get("commit"):
            shas.add(m["commit"])
    problems = []
    for sha in sorted(shas):
        r = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            problems.append(f"commit {sha[:12]} is not present in this repository")
    return problems


def transition_problems(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """Compare a NEW status document with an OLD one (for example the version on main).

    Catches changes that are individually valid but not legitimate: dropping milestones, rewriting history in
    the evidence registry, raising a verification level without new evidence, and changing weights after approval.
    """
    problems: List[str] = []
    old_ms = {m["id"]: m for m in old.get("milestones", []) if isinstance(m, dict) and "id" in m}
    new_ms = {m["id"]: m for m in new.get("milestones", []) if isinstance(m, dict) and "id" in m}
    for mid in old_ms:
        if mid not in new_ms:
            problems.append(f"{mid}: milestone removed (ids are stable; mark it DEFERRED instead)")
    old_ev, new_ev = old.get("evidence", {}), new.get("evidence", {})
    for eid, rec in old_ev.items():
        if eid not in new_ev:
            problems.append(f"evidence {eid} was removed (the registry is append-only)")
        elif new_ev[eid] != rec:
            problems.append(f"evidence {eid} was modified (the registry is append-only; add a new record)")
    fresh_ids = set(new_ev) - set(old_ev)
    for mid, nm in new_ms.items():
        om = old_ms.get(mid)
        nv = nm.get("verification", {})
        old_level = om["verification"]["level"] if om else "NONE"
        old_state = om["verification"]["state"] if om else "NOT_STARTED"
        fresh = set(nv.get("evidence", [])) & fresh_ids
        if nv.get("level") in LEVEL_ORDER and LEVEL_ORDER.index(nv["level"]) > LEVEL_ORDER.index(old_level) and not fresh:
            problems.append(f"{mid}: verification level raised from {old_level} to {nv['level']} without any new evidence record")
        if nv.get("state") == "VERIFIED" and old_state != "VERIFIED" and not fresh:
            problems.append(f"{mid}: became VERIFIED without any new evidence record")
    if old.get("weights_status") == "APPROVED" and weights_fingerprint(old) != weights_fingerprint(new) \
            and new.get("weights_status") != "PROPOSED":
        problems.append("weights changed after approval; weights_status must go back to PROPOSED until Lukas re-approves")
    return problems
