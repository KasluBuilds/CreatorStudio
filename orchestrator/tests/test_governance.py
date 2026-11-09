"""Tests for the governance package: scoring, validation, task registry, generated handoff, committed files."""
import copy
import datetime as dt
import itertools
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from governance import handoff, status, tasks  # noqa: E402
from governance.cli import main as cli_main  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SHA_A = "a" * 40
SHA_B = "b" * 40


def evidence_set():
    return {
        "E-T1": {"id": "E-T1", "type": "test_run", "date": "2026-11-01", "summary": "unit tests", "ref": "tests/x.py",
                 "command": "python -m unittest", "result": "OK", "commit": SHA_A},
        "E-O1": {"id": "E-O1", "type": "observation", "date": "2026-11-02", "summary": "observed", "ref": "run 1",
                 "observer": "claude:test"},
        "E-R1": {"id": "E-R1", "type": "reported", "date": "2026-11-03", "summary": "doc says so", "ref": "docs/x.md"},
        "E-H1": {"id": "E-H1", "type": "human_review", "date": "2026-11-04", "summary": "reviewed", "ref": "review 1",
                 "reviewer": "Lukas"},
        "E-H2": {"id": "E-H2", "type": "human_review", "date": "2026-11-05", "summary": "reviewed again",
                 "ref": "review 2", "reviewer": "Lukas"},
    }


def milestone(mid="M-AAA-01", weight=2, impl="IMPLEMENTED", vstate="NOT_STARTED", level="NONE", ev=(), **extra):
    m = {"id": mid, "title": "t " + mid, "description": "d", "phase": "P1", "weight": weight,
         "weight_rationale": "a rationale that is long enough", "implementation": {"state": impl, "summary": "s"},
         "verification": {"state": vstate, "level": level, "summary": "s", "evidence": list(ev)},
         "depends_on": [], "commit": None, "open_questions": [], "blockers": []}
    m.update(extra)
    return m


def status_doc(milestones=None):
    return {
        "schema_version": 1, "updated": "2026-11-09", "weights_status": "PROPOSED", "open_decisions": ["q"],
        "next_actions": ["a"],
        "baseline": {"commit": SHA_A, "branch": "main", "date": "2026-11-09", "test_command": "cmd", "tests_total": 10,
                     "failures": 0, "errors": 0, "skipped": 1, "scope": "unit"},
        "evidence": evidence_set(),
        "milestones": milestones if milestones is not None else [milestone()],
    }


class TestScoring(unittest.TestCase):
    def test_known_example(self):
        doc = status_doc([
            milestone("M-AAA-01", 2, "IMPLEMENTED", "IN_PROGRESS", "UNIT_ISOLATED", ["E-T1"]),
            milestone("M-AAA-02", 3, "IN_PROGRESS", "IN_PROGRESS", "INTEGRATION", ["E-O1"]),
            milestone("M-AAA-03", 5, "NOT_STARTED"),
        ])
        r = status.calculate(doc)
        self.assertEqual(r["total_weight"], 10)
        self.assertEqual(r["implementation_percent"], 35.0)   # (2*1 + 3*1/2) / 10
        self.assertEqual(r["verification_percent"], 20.0)     # (2*1/4 + 3*min(1/2, 1/2)) / 10

    def test_verification_is_capped_by_implementation(self):
        doc = status_doc([milestone("M-AAA-01", 4, "IN_PROGRESS", "IN_PROGRESS", "REAL_INPUT_REVIEWED",
                                    ["E-T1", "E-H1"])])
        r = status.calculate(doc)
        self.assertEqual(r["implementation_percent"], 50.0)
        self.assertEqual(r["verification_percent"], 50.0)

    def test_no_evidence_no_credit(self):
        r = status.calculate(status_doc([milestone("M-AAA-01", 5, "IMPLEMENTED", "NOT_STARTED", "NONE")]))
        self.assertEqual((r["implementation_percent"], r["verification_percent"]), (100.0, 0.0))

    def test_uncounted_verification_states_give_nothing(self):
        for vstate in ("NOT_STARTED", "BLOCKED", "DEFERRED", "FAILED"):
            m = milestone("M-AAA-01", 1, "IMPLEMENTED", vstate, "NONE", blockers=["b"] if vstate == "BLOCKED" else [])
            self.assertEqual(status.calculate(status_doc([m]))["verification_percent"], 0.0, vstate)

    def test_levels_and_ladder_values(self):
        cases = {"UNIT_ISOLATED": (["E-T1"], 25.0), "INTEGRATION": (["E-O1"], 50.0),
                 "REAL_INPUT_REVIEWED": (["E-T1", "E-H1"], 90.0),
                 "REPEATED_REAL_USE": (["E-T1", "E-H1", "E-H2"], 100.0)}
        for level, (ev, expected) in cases.items():
            vstate = "VERIFIED" if level in status.VERIFIED_LEVELS else "IN_PROGRESS"
            impl = "VERIFIED" if vstate == "VERIFIED" else "IMPLEMENTED"
            r = status.calculate(status_doc([milestone("M-AAA-01", 3, impl, vstate, level, ev)]))
            self.assertEqual(r["verification_percent"], expected, level)

    def test_half_up_rounding(self):
        ms = [milestone("M-AAA-01", 1, "IMPLEMENTED")] + [milestone(f"M-AAA-0{i}", 5, "NOT_STARTED") for i in (2, 3, 4)]
        self.assertEqual(status.calculate(status_doc(ms))["implementation_percent"], 6.3)  # 6.25 rounds half up

    def test_order_independent_and_deterministic(self):
        ms = [milestone("M-AAA-01", 2, "IMPLEMENTED", "IN_PROGRESS", "UNIT_ISOLATED", ["E-T1"]),
              milestone("M-AAA-02", 3, "IN_PROGRESS"), milestone("M-AAA-03", 5, "NOT_STARTED")]
        a = status.calculate(status_doc(ms))
        b = status.calculate(status_doc(list(reversed(ms))))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(status.calculate(status_doc(ms)), sort_keys=True))

    def test_reported_only_is_flagged(self):
        doc = status_doc([milestone("M-AAA-01", 2, "IMPLEMENTED", "IN_PROGRESS", "INTEGRATION", ["E-R1"]),
                          milestone("M-AAA-02", 2, "IMPLEMENTED", "IN_PROGRESS", "INTEGRATION", ["E-R1", "E-O1"])])
        self.assertEqual(status.calculate(doc)["reported_only_milestones"], ["M-AAA-01"])

    def test_verified_never_exceeds_implemented_over_all_valid_combinations(self):
        ev_all = ["E-T1", "E-O1", "E-H1", "E-H2"]
        checked = 0
        for impl, vstate, level in itertools.product(status.STATES, status.STATES, status.VERIFICATION_LEVELS):
            m = milestone("M-AAA-01", 7, impl, vstate, level, ev_all if level != "NONE" else [],
                          blockers=["b"] if "BLOCKED" in (impl, vstate) else [])
            doc = status_doc([m, milestone("M-AAA-02", 3, "NOT_STARTED")])
            if status.validate(doc):
                continue
            r = status.calculate(doc)
            self.assertLessEqual(r["verification_percent"], r["implementation_percent"], (impl, vstate, level))
            checked += 1
        self.assertGreater(checked, 40)


class TestStatusValidation(unittest.TestCase):
    def assertRejected(self, doc, fragment):
        problems = status.validate(doc)
        self.assertTrue(any(fragment in p for p in problems), f"expected {fragment!r} in {problems}")
        with self.assertRaises(status.StatusError):
            status.calculate(doc)

    def test_minimal_document_is_valid(self):
        self.assertEqual(status.validate(status_doc()), [])

    def test_garbage_never_raises(self):
        for junk in (None, [], "x", 5, {}, {"milestones": [None, 5, "x"]}, {"milestones": [{}], "evidence": []},
                     {"schema_version": 1, "milestones": [{"id": "M-AAA-01", "depends_on": [1]}], "evidence": {}}):
            self.assertTrue(status.validate(junk), junk)

    def test_structural_errors(self):
        d = status_doc(); d["schema_version"] = 2; self.assertRejected(d, "schema_version")
        d = status_doc([]); self.assertRejected(d, "non-empty list")
        d = status_doc([milestone("bad")]); self.assertRejected(d, "id must match")
        d = status_doc([milestone(), milestone()]); self.assertRejected(d, "duplicate milestone id")

    def test_weights(self):
        for bad in (0, 11, -1, 2.5, True, "3", None):
            self.assertRejected(status_doc([milestone(weight=bad)]), "weight must be an integer")
        self.assertRejected(status_doc([milestone(weight_rationale="short")]), "weight_rationale")

    def test_states_and_levels(self):
        d = status_doc(); d["milestones"][0]["implementation"]["state"] = "DONE"
        self.assertRejected(d, "implementation needs a valid")
        d = status_doc(); d["milestones"][0]["verification"]["level"] = "GREAT"
        self.assertRejected(d, "verification needs valid")

    def test_levels_need_matching_evidence(self):
        self.assertRejected(status_doc([milestone(vstate="IN_PROGRESS", level="UNIT_ISOLATED")]), "no usable evidence")
        self.assertRejected(status_doc([milestone(vstate="IN_PROGRESS", level="UNIT_ISOLATED", ev=["E-R1"])]),
                            "requires a test_run")
        self.assertRejected(status_doc([milestone(vstate="IN_PROGRESS", level="INTEGRATION", ev=["E-H1"])]),
                            "requires a test_run, observation or reported")
        self.assertRejected(status_doc([milestone(vstate="IN_PROGRESS", level="REAL_INPUT_REVIEWED", ev=["E-T1"])]),
                            "requires a human_review")
        self.assertRejected(status_doc([milestone(vstate="IN_PROGRESS", level="REAL_INPUT_REVIEWED", ev=["E-H1", "E-R1"])]),
                            "test_run or observation")
        self.assertRejected(status_doc([milestone(vstate="IN_PROGRESS", level="REPEATED_REAL_USE", ev=["E-T1", "E-H1"])]),
                            "two different dates")

    def test_reported_evidence_cannot_reach_real_input_levels(self):
        self.assertRejected(status_doc([milestone(vstate="IN_PROGRESS", level="REAL_INPUT_REVIEWED",
                                                  ev=["E-R1", "E-H1"])]), "test_run or observation")

    def test_unknown_evidence_reference(self):
        self.assertRejected(status_doc([milestone(vstate="IN_PROGRESS", level="UNIT_ISOLATED", ev=["E-NOPE"])]),
                            "does not exist")

    def test_malformed_evidence_records(self):
        cases = [
            ("test_run missing commit", lambda e: e["E-T1"].pop("commit"), "requires 'commit'"),
            ("short commit", lambda e: e["E-T1"].__setitem__("commit", "abc1234"), "40-character"),
            ("uppercase commit", lambda e: e["E-T1"].__setitem__("commit", "A" * 40), "40-character"),
            ("impossible date", lambda e: e["E-T1"].__setitem__("date", "2026-02-30"), "real YYYY-MM-DD"),
            ("wrong date format", lambda e: e["E-T1"].__setitem__("date", "09.11.2026"), "real YYYY-MM-DD"),
            ("unknown type", lambda e: e["E-T1"].__setitem__("type", "vibes"), "type must be one of"),
            ("unknown key", lambda e: e["E-T1"].__setitem__("score", 5), "unknown keys"),
            ("id mismatch", lambda e: e["E-T1"].__setitem__("id", "E-OTHER"), "must equal its key"),
            ("empty summary", lambda e: e["E-T1"].__setitem__("summary", " "), "summary is required"),
            ("review without reviewer", lambda e: e["E-H1"].pop("reviewer"), "requires 'reviewer'"),
            ("observation without observer", lambda e: e["E-O1"].pop("observer"), "requires 'observer'"),
            ("not an object", lambda e: e.__setitem__("E-T1", "text"), "must be an object"),
        ]
        for name, mutate, fragment in cases:
            d = status_doc(); mutate(d["evidence"])
            with self.subTest(name):
                self.assertRejected(d, fragment)

    def test_verified_state_rules(self):
        self.assertRejected(status_doc([milestone(impl="IMPLEMENTED", vstate="VERIFIED", level="UNIT_ISOLATED",
                                                  ev=["E-T1"])]), "verification VERIFIED requires level")
        self.assertRejected(status_doc([milestone(impl="VERIFIED", vstate="IN_PROGRESS", level="UNIT_ISOLATED",
                                                  ev=["E-T1"])]), "implementation VERIFIED requires")
        self.assertRejected(status_doc([milestone(impl="IN_PROGRESS", vstate="VERIFIED", level="REAL_INPUT_REVIEWED",
                                                  ev=["E-T1", "E-H1"])]), "implemented milestone")
        ok = status_doc([milestone(impl="VERIFIED", vstate="VERIFIED", level="REAL_INPUT_REVIEWED", ev=["E-T1", "E-H1"])])
        self.assertEqual(status.validate(ok), [])

    def test_cannot_verify_unstarted_or_inconsistent(self):
        self.assertRejected(status_doc([milestone(impl="NOT_STARTED", vstate="IN_PROGRESS", level="UNIT_ISOLATED",
                                                  ev=["E-T1"])]), "cannot have verification level")
        self.assertRejected(status_doc([milestone(vstate="NOT_STARTED", level="UNIT_ISOLATED", ev=["E-T1"])]),
                            "NOT_STARTED must have level NONE")

    def test_blocked_needs_blockers(self):
        self.assertRejected(status_doc([milestone(impl="BLOCKED")]), "BLOCKED requires")
        self.assertEqual(status.validate(status_doc([milestone(impl="BLOCKED", blockers=["Q11"])])), [])

    def test_dependencies(self):
        self.assertRejected(status_doc([milestone(depends_on=["M-ZZZ-01"])]), "unknown milestone")
        self.assertRejected(status_doc([milestone(depends_on=["M-AAA-01"])]), "depends on itself")
        a = milestone("M-AAA-01", depends_on=["M-AAA-02"]); b = milestone("M-AAA-02", depends_on=["M-AAA-01"])
        self.assertRejected(status_doc([a, b]), "dependency cycle")

    def test_header_fields(self):
        d = status_doc(); d["baseline"]["commit"] = "xyz"; self.assertRejected(d, "baseline.commit")
        d = status_doc(); d["baseline"]["failures"] = -1; self.assertRejected(d, "baseline.failures")
        d = status_doc(); d["updated"] = "yesterday"; self.assertRejected(d, "'updated'")
        d = status_doc(); del d["weights_status"]; self.assertRejected(d, "weights_status")

    def test_computed_block_must_match(self):
        d = status_doc()
        self.assertTrue(status.check_computed(d))
        r = status.calculate(d)
        d["computed"] = {k: r[k] for k in ("implementation_percent", "verification_percent", "total_weight",
                                           "milestone_count")}
        self.assertEqual(status.check_computed(d), [])
        d["computed"]["verification_percent"] = 99.0
        self.assertTrue(any("stale" in p for p in status.check_computed(d)))


def tasks_doc():
    return {
        "schema_version": 1, "updated": "2026-11-09", "lease_policy": {"default_hours": 24, "max_hours": 72},
        "package_owners": {
            "research": {"paths": ["orchestrator/research/"], "owner": "claude:research", "owner_status": "REPORTED"},
            "media": {"paths": ["orchestrator/media_analysis/"], "owner": "UNRESOLVED", "owner_status": "UNRESOLVED - x"},
        },
        "shared_files": {"ROADMAP.md": "shared"},
        "integration_agreements": {},
        "tasks": [task("T-AAA-001", "claude:research", "task/a", ["orchestrator/research/"])],
    }


def task(tid, owner, branch, paths, status_="ACTIVE", **extra):
    t = {"id": tid, "title": "t", "description": "d", "owner": owner, "branch": branch,
         "worktree": f"C:\\wt\\{tid}" if owner.startswith("claude:") else None, "status": status_, "depends_on": [],
         "lease": {"acquired": "2026-11-09T10:00:00+01:00", "expires": "2026-11-10T10:00:00+01:00"},
         "exclusive_resources": [], "integration_agreements": [], "owned_paths": paths,
         "acceptance_criteria": ["c"], "test_requirements": ["t"], "integration_notes": "n"}
    t.update(extra)
    return t


class TestTaskRegistry(unittest.TestCase):
    def assertRejected(self, doc, fragment):
        problems = tasks.validate(doc)
        self.assertTrue(any(fragment in p for p in problems), f"expected {fragment!r} in {problems}")

    def test_valid_minimal(self):
        self.assertEqual(tasks.validate(tasks_doc()), [])

    def test_overlap_function(self):
        for a, b, expected in [("a/", "a/b.py", True), ("a/b.py", "a/", True), ("a/", "a/", True), ("a/b", "a/c", False),
                               ("a/", "ab/c", False), ("a/b/", "a/", True), ("README.md", "README.md", True)]:
            self.assertEqual(tasks.overlaps(a, b), expected, (a, b))

    def test_overlapping_claims_are_rejected(self):
        d = tasks_doc()
        d["tasks"].append(task("T-BBB-001", "claude:other", "task/b", ["ROADMAP.md", "orchestrator/research/x.py"]))
        self.assertRejected(d, "without a covering integration agreement")

    def test_two_tasks_may_claim_disjoint_files(self):
        d = tasks_doc()
        d["tasks"].append(task("T-BBB-001", "claude:other", "task/b", ["ROADMAP.md"]))
        self.assertEqual(tasks.validate(d), [])

    def test_agreement_allows_overlap_only_when_it_covers(self):
        d = tasks_doc()
        d["package_owners"]["research"]["owner"] = "claude:research"
        d["tasks"].append(task("T-BBB-001", "claude:research", "task/b", ["orchestrator/research/x.py"]))
        self.assertRejected(d, "without a covering integration agreement")
        d["integration_agreements"]["AGR-R"] = {"description": "split", "task_ids": ["T-AAA-001", "T-BBB-001"],
                                                "paths": ["orchestrator/research/"]}
        for t in d["tasks"]:
            t["integration_agreements"] = ["AGR-R"]
        self.assertEqual(tasks.validate(d), [])
        d["integration_agreements"]["AGR-R"]["paths"] = ["elsewhere/"]
        self.assertRejected(d, "without a covering integration agreement")

    def test_inactive_tasks_do_not_conflict(self):
        d = tasks_doc()
        for st in ("PLANNED", "COMPLETED", "RELEASED", "BLOCKED"):
            d["tasks"][1:] = [task("T-BBB-001", "claude:other", "task/b", ["orchestrator/research/"], st)]
            self.assertEqual(tasks.validate(d), [], st)

    def test_package_ownership(self):
        d = tasks_doc()
        d["tasks"] = [task("T-AAA-001", "claude:intruder", "task/a", ["orchestrator/research/x.py"])]
        self.assertRejected(d, "owned by claude:research")
        d["tasks"] = [task("T-AAA-001", "claude:anyone", "task/a", ["orchestrator/media_analysis/y.py"])]
        self.assertRejected(d, "owned by UNRESOLVED")
        d["integration_agreements"]["AGR-M"] = {"description": "ok", "task_ids": ["T-AAA-001"],
                                                "paths": ["orchestrator/media_analysis/"]}
        d["tasks"][0]["integration_agreements"] = ["AGR-M"]
        self.assertEqual(tasks.validate(d), [])

    def test_owner_status_must_match_owner(self):
        d = tasks_doc(); d["package_owners"]["media"]["owner_status"] = "REPORTED"
        self.assertRejected(d, "owner_status must start with UNRESOLVED")
        d = tasks_doc(); d["package_owners"]["research"]["owner_status"] = "UNRESOLVED - hmm"
        self.assertRejected(d, "owner_status must start with UNRESOLVED")

    def test_resolve_is_serialized(self):
        d = tasks_doc()
        d["tasks"][0]["exclusive_resources"] = ["resolve"]
        d["tasks"].append(task("T-BBB-001", "claude:other", "task/b", ["ROADMAP.md"], exclusive_resources=["resolve"]))
        self.assertRejected(d, "exclusive resource 'resolve'")
        d["tasks"][1]["status"] = "PLANNED"
        self.assertEqual(tasks.validate(d), [])
        d["tasks"][1]["exclusive_resources"] = ["gpu"]
        self.assertRejected(d, "exclusive_resources")

    def test_lease_rules(self):
        d = tasks_doc(); d["tasks"][0]["lease"]["expires"] = "2026-11-12T10:00:00+01:00"  # exactly 72 h: allowed
        self.assertEqual(tasks.validate(d), [])
        d = tasks_doc(); d["tasks"][0]["lease"]["expires"] = "2026-11-12T11:00:00+01:00"  # 73 h
        self.assertRejected(d, "lease longer than 72 hours")
        d = tasks_doc(); d["tasks"][0]["lease"]["expires"] = "2026-11-09T09:00:00+01:00"
        self.assertRejected(d, "expires must be after acquired")
        d = tasks_doc(); d["tasks"][0]["lease"]["expires"] = "2026-11-10T10:00:00"
        self.assertRejected(d, "UTC offset")
        d = tasks_doc(); d["tasks"][0]["lease"] = None
        self.assertRejected(d, "lease needs")

    def test_expired_lease_detection_uses_supplied_time(self):
        d = tasks_doc()
        before = dt.datetime.fromisoformat("2026-11-10T09:00:00+01:00")
        after = dt.datetime.fromisoformat("2026-11-10T10:00:01+01:00")
        self.assertEqual(tasks.expired_leases(d, before), [])
        self.assertEqual([e["id"] for e in tasks.expired_leases(d, after)], ["T-AAA-001"])
        d["tasks"][0]["status"] = "COMPLETED"
        self.assertEqual(tasks.expired_leases(d, after), [])

    def test_invalid_paths(self):
        for bad in ("C:/x", "/abs", "a\\b", "../x", "a/../b", "a/*.py", "a//b", "", "./a"):
            d = tasks_doc(); d["tasks"][0]["owned_paths"] = [bad]
            with self.subTest(bad):
                self.assertTrue(tasks.validate(d))

    def test_identity_rules(self):
        d = tasks_doc(); d["tasks"].append(copy.deepcopy(d["tasks"][0])); self.assertRejected(d, "duplicate task id")
        d = tasks_doc(); d["tasks"].append(task("T-BBB-001", "claude:o", "task/a", ["ROADMAP.md"]))
        self.assertRejected(d, "already used")
        d = tasks_doc(); d["tasks"][0]["branch"] = "main"; self.assertRejected(d, "branch must match")
        d = tasks_doc(); d["tasks"][0]["id"] = "T1"; self.assertRejected(d, "id must match")
        d = tasks_doc(); d["tasks"][0]["owner"] = "somebody"; self.assertRejected(d, "owner must match")
        d = tasks_doc(); d["tasks"][0]["worktree"] = None; self.assertRejected(d, "own worktree")
        d = tasks_doc(); d["tasks"][0]["status"] = "RUNNING"; self.assertRejected(d, "status must be one of")
        d = tasks_doc(); d["tasks"][0]["acceptance_criteria"] = []; self.assertRejected(d, "acceptance_criteria")

    def test_dependencies(self):
        d = tasks_doc(); d["tasks"][0]["depends_on"] = ["T-ZZZ-001"]; self.assertRejected(d, "unknown task")
        d = tasks_doc()
        d["tasks"].append(task("T-BBB-001", "claude:o", "task/b", ["ROADMAP.md"], depends_on=["T-AAA-001"]))
        d["tasks"][0]["depends_on"] = ["T-BBB-001"]
        self.assertRejected(d, "task dependency cycle")

    def test_chatgpt_tasks_need_no_worktree(self):
        d = tasks_doc()
        d["tasks"].append(task("T-GPT-001", "chatgpt:planning", "chatgpt/plan", ["ROADMAP.md"]))
        self.assertEqual(tasks.validate(d), [])

    def test_garbage_never_raises(self):
        for junk in (None, [], "x", {}, {"tasks": [None, 3]}, {"tasks": "x"}):
            self.assertTrue(tasks.validate(junk))


class TestCommittedFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.status = json.loads((ROOT / "PROJECT_STATUS.json").read_text(encoding="utf-8"))
        cls.tasks = json.loads((ROOT / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))

    def test_status_file_is_valid(self):
        self.assertEqual(status.validate(self.status), [])

    def test_computed_block_is_current(self):
        self.assertEqual(status.check_computed(self.status), [])

    def test_verification_never_exceeds_implementation_in_real_file(self):
        r = status.calculate(self.status)
        self.assertLessEqual(r["verification_percent"], r["implementation_percent"])

    def test_nothing_is_verified_without_human_reviewed_evidence(self):
        for m in self.status["milestones"]:
            if "VERIFIED" in (m["implementation"]["state"], m["verification"]["state"]):
                kinds = {self.status["evidence"][e]["type"] for e in m["verification"]["evidence"]}
                self.assertIn("human_review", kinds, m["id"])

    def test_task_registry_is_valid(self):
        self.assertEqual(tasks.validate(self.tasks), [])

    def test_unresolved_ownership_is_recorded_not_guessed(self):
        self.assertEqual(self.tasks["package_owners"]["media_analysis"]["owner"], "UNRESOLVED")

    def test_handoff_is_current(self):
        want = handoff.render(self.status, self.tasks)
        have = (ROOT / "SESSION_HANDOFF.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(have, want, "run: cd orchestrator && python -B -m governance handoff --write")

    def test_roadmap_milestone_ids_exist(self):
        text = (ROOT / "AI_Creator_Studio_Documentation" / "ROADMAP.md").read_text(encoding="utf-8")
        known = {m["id"] for m in self.status["milestones"]}
        mentioned = set(re.findall(r"\bM-[A-Z]{3}-\d{2}\b", text))
        self.assertTrue(mentioned, "ROADMAP.md should reference milestone ids")
        self.assertEqual(mentioned - known, set())

    def test_every_package_directory_has_an_owner_entry(self):
        covered = {p for pkg in self.tasks["package_owners"].values() for p in pkg["paths"]}
        for d in sorted((ROOT / "orchestrator").iterdir()):
            if d.is_dir() and d.name not in ("tests", "__pycache__"):
                self.assertIn(f"orchestrator/{d.name}/", covered, f"package {d.name} has no ownership entry")

    def test_no_secrets_in_governance_files(self):
        # (?<![A-Za-z0-9]) keeps words such as "task-governance-foundation" from looking like an "sk-..." key
        pattern = re.compile(r"((?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
                             r"|BEGIN [A-Z ]*PRIVATE KEY)")
        for name in ("PROJECT_STATUS.json", "ACTIVE_TASKS.json", "SESSION_HANDOFF.md", "CHANGELOG.md",
                     "PROJECT_OPERATING_PROTOCOL.md", "AI_Creator_Studio_Documentation/ENVIRONMENT.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(text), name)


class TestCli(unittest.TestCase):
    def setUp(self):
        import contextlib
        import io
        stack = contextlib.ExitStack()  # keep the CLI's console output out of the test log
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(stack.close)

    def test_validate_and_check_succeed_on_the_repository(self):
        self.assertEqual(cli_main(["validate"]), 0)
        self.assertEqual(cli_main(["handoff", "--check"]), 0)

    def test_leases_exit_codes(self):
        self.assertEqual(cli_main(["leases", "--now", "2026-11-09T20:00:00+01:00"]), 0)
        self.assertEqual(cli_main(["leases", "--now", "2030-01-01T00:00:00+00:00"]), 1)
        self.assertEqual(cli_main(["leases", "--now", "2026-11-09T20:00:00"]), 2)

    def test_invalid_input_exits_nonzero_without_traceback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "PROJECT_STATUS.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            (root / "ACTIVE_TASKS.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(cli_main(["--root", tmp, "score"]), 1)
            self.assertEqual(cli_main(["--root", tmp, "tasks"]), 2)
            self.assertEqual(cli_main(["--root", tmp + "/missing", "validate"]), 2)


if __name__ == "__main__":
    unittest.main()
