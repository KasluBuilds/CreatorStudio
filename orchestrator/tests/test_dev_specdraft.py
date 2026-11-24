import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import specdraft  # noqa: E402


def milestone(mid, state="NOT_STARTED", deps=(), questions=(), blockers=(), title=None, description="Does a thing."):
    return {"id": mid, "title": title or f"Title of {mid}", "phase": "Phase 9", "description": description, "weight": 3,
            "implementation": {"state": state, "summary": "s"}, "depends_on": list(deps),
            "open_questions": list(questions), "blockers": list(blockers)}


class Repo:
    def __init__(self, test, milestones, tasks=()):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        (self.root / "PROJECT_STATUS.json").write_text(json.dumps({"milestones": milestones}), encoding="utf-8")
        (self.root / "ACTIVE_TASKS.json").write_text(json.dumps({"tasks": list(tasks)}), encoding="utf-8")


class DraftTests(unittest.TestCase):
    def repo(self, milestones, tasks=()):
        return Repo(self, milestones, tasks).root

    def test_eligible_milestone_gets_a_rule_derived_draft_and_a_missing_list(self):
        root = self.repo([milestone("M-AAA-01", "IMPLEMENTED"),
                          milestone("M-ZZZ-02", deps=["M-AAA-01"], title="Learn To Fly!", description="Wings.")],
                         [{"id": "T-ZZZ-004", "branch": "task/t-zzz-004-x", "status": "COMPLETED"}])
        rec = specdraft.draft(root, "M-ZZZ-02")
        self.assertEqual(rec["status"], "draft")
        d = rec["draft"]
        self.assertEqual((d["id"], d["title"], d["description"]), ("T-ZZZ-005", "Learn To Fly!", "Wings."))
        self.assertEqual(d["branch"], "task/t-zzz-005-learn-to-fly")
        self.assertEqual(d["worktree"], "../CreatorStudio-worktrees/t-zzz-005-learn-to-fly")
        self.assertEqual(d["agreement_id"], "AGR-ZZZ-005-01")
        self.assertEqual(rec["missing"], ["owned_paths", "acceptance_criteria", "agreement"])
        for k in rec["missing"]:
            self.assertNotIn(k, d, "a missing field must never be invented")

    def test_first_task_of_an_area_is_number_001(self):
        root = self.repo([milestone("M-NEW-01")])
        self.assertEqual(specdraft.draft(root, "M-NEW-01")["draft"]["id"], "T-NEW-001")

    def test_open_questions_or_blockers_need_a_decision(self):
        for kw in ({"questions": ["which codec?"]}, {"blockers": ["no license"]}):
            with self.subTest(kw=kw):
                rec = specdraft.draft(self.repo([milestone("M-NEW-01", **kw)]), "M-NEW-01")
                self.assertEqual(rec["status"], "needs_decision")
                self.assertTrue(rec["open_questions"] or rec["blockers"])

    def test_refusals(self):
        root = self.repo([milestone("M-AAA-01", "IN_PROGRESS"), milestone("M-BBB-01", deps=["M-AAA-01"]),
                          milestone("M-CCC-01", deps=["M-MISSING-01"]), milestone("M-DDD-01"), milestone("BAD-ID")],
                         [{"id": "T-DDD-001", "branch": "task/t-ddd-001-x", "status": "ACTIVE",
                           "description": "works on M-DDD-01"}])
        cases = {"M-AAA-01": "not NOT_STARTED", "M-BBB-01": "not IMPLEMENTED", "M-CCC-01": "not a known milestone",
                 "M-DDD-01": "already mentions", "M-NOPE-01": "unknown milestone", "BAD-ID": "does not look like"}
        for mid, text in cases.items():
            with self.subTest(mid=mid):
                rec = specdraft.draft(root, mid)
                self.assertEqual(rec["status"], "refused", rec)
                self.assertIn(text, " ".join(rec["reasons"]))
                self.assertNotIn("draft", rec)

    def test_missing_or_malformed_files_are_a_refusal_not_a_crash(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        self.assertEqual(specdraft.draft(empty, "M-NEW-01")["status"], "refused")
        (empty / "PROJECT_STATUS.json").write_text("{not json", encoding="utf-8")
        (empty / "ACTIVE_TASKS.json").write_text("{}", encoding="utf-8")
        self.assertEqual(specdraft.draft(empty, "M-NEW-01")["status"], "refused")

    def test_a_derived_branch_that_is_taken_is_refused(self):
        root = self.repo([milestone("M-NEW-01", title="Same")],
                         [{"id": "T-OTH-001", "branch": "task/t-new-001-same", "status": "ACTIVE"}])
        self.assertEqual(specdraft.draft(root, "M-NEW-01")["status"], "refused")

    def test_the_command_writes_nothing_and_sets_exit_codes(self):
        root = self.repo([milestone("M-NEW-01"), milestone("M-NEW-02", questions=["q"])])
        before = sorted(p.name for p in root.iterdir())
        for mid, code in (("M-NEW-01", 0), ("M-NEW-02", 3), ("M-NEW-09", 2)):
            out = io.StringIO()
            self.assertEqual(specdraft.main([mid, "--repo", str(root)], out=out), code)
            json.loads(out.getvalue())
        self.assertEqual(sorted(p.name for p in root.iterdir()), before)
        self.assertEqual(specdraft.main([], out=io.StringIO()), 2)

    def test_a_draft_is_rejected_by_the_claim_command_until_the_missing_fields_exist(self):
        from dev_supervisor import claim
        root = self.repo([milestone("M-NEW-01")])
        rec = specdraft.draft(root, "M-NEW-01")
        problems = claim.check_spec(rec["draft"], {"tasks": [], "integration_agreements": {}})
        joined = " ".join(problems)
        for k in rec["missing"]:
            self.assertIn(k, joined)


if __name__ == "__main__":
    unittest.main()
