"""DEVELOPMENT_TRACKER.json: structure, task linkage, step states/transitions and the completion gate.

Pure checks on the repository's real tracker plus fixture documents; nothing is written and no Git or network is used.
"""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

import tracker_check as tc  # noqa: E402

SHA = "a" * 40
GOOD_COMPLETION = {"reviewed_by_chatgpt": True, "lukas_confirmed": True, "date": "2026-11-12",
                   "source": "Lukas said so in the session"}


def step(n, status="PLANNED", **extra):
    s = {"id": f"S-GOV-002-{n:02d}", "title": f"step {n}", "status": status,
         "acceptance_criteria": ["something checkable"], "evidence": [], "completion": None}
    if status == "COMPLETED":
        s["evidence"] = [{"type": "commit", "summary": "merged", "commit": SHA}]
        s["completion"] = dict(GOOD_COMPLETION)
    s.update(extra)
    return s


def doc(*steps):
    return {"schema_version": 1, "authority": "a", "protocol": "DEVELOPMENT_STEP_PROTOCOL.md",
            "tasks": [{"task_id": "T-GOV-002", "title": "t", "steps": list(steps)}]}


REGISTRY = {"tasks": [{"id": "T-GOV-002"}]}


class TestRealTracker(unittest.TestCase):
    def setUp(self):
        self.tracker, self.registry = tc.load(ROOT)

    def test_repository_tracker_is_valid(self):
        self.assertEqual(tc.validate(self.tracker, self.registry), [])

    def test_every_task_is_registered_and_step_zero_is_the_merged_claim(self):
        ids = {t["id"] for t in self.registry["tasks"]}
        for t in self.tracker["tasks"]:
            self.assertIn(t["task_id"], ids)
        gov = next(t for t in self.tracker["tasks"] if t["task_id"] == "T-GOV-002")
        s0 = gov["steps"][0]
        self.assertEqual((s0["id"], s0["status"]), ("S-GOV-002-00", "COMPLETED"))
        self.assertTrue(any(e["type"] == "pull_request" and e["commit"] == "27685bac2b0bc58c961e4e3d26b57af38f1bc3b2"
                            for e in s0["evidence"]))

    def test_at_most_one_working_step_and_later_steps_are_blocked_from_starting(self):
        gov = next(t for t in self.tracker["tasks"] if t["task_id"] == "T-GOV-002")
        working = [s for s in gov["steps"] if s["status"] in tc.WORKING]
        self.assertLessEqual(len(working), 1)
        seen_unfinished = False
        for s in gov["steps"]:
            if seen_unfinished:
                self.assertIn(s["status"], ("PLANNED", "BLOCKED"), s["id"])
            if s["status"] != "COMPLETED":
                seen_unfinished = True

    def test_tracker_holds_no_milestone_or_lease_data(self):
        keys = {k.lower() for _, k, _ in tc._walk(self.tracker)}
        self.assertEqual(keys & tc.FORBIDDEN_KEYS, set())
        self.assertNotIn("milestone_id", keys)

    def test_governance_documents_require_the_step_protocol(self):
        for name in ("CLAUDE.md", "PROJECT_OPERATING_PROTOCOL.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("DEVELOPMENT_STEP_PROTOCOL.md", text, name)
            self.assertIn("READY_FOR_REVIEW", text, name)
        proto = (ROOT / "DEVELOPMENT_STEP_PROTOCOL.md").read_text(encoding="utf-8")
        for needle in ("READY_FOR_REVIEW", "explicitly", "ChatGPT", "terminal"):
            self.assertIn(needle, proto)


class TestStructure(unittest.TestCase):
    def test_minimal_valid_documents(self):
        self.assertEqual(tc.validate(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"), step(2)), REGISTRY), [])

    def test_rejects_non_object_and_bad_header(self):
        self.assertTrue(tc.validate([]))
        d = doc(step(0))
        d["schema_version"] = 2
        self.assertTrue(any("schema_version" in p for p in tc.validate(d)))

    def test_rejects_unlinked_task(self):
        d = doc(step(0))
        d["tasks"][0]["task_id"] = "T-NOP-001"
        self.assertTrue(any("not registered" in p for p in tc.validate(d, REGISTRY)))
        d["tasks"][0]["task_id"] = "bad"
        self.assertTrue(any("task_id" in p for p in tc.validate(d, REGISTRY)))

    def test_rejects_bad_step_fields(self):
        for mutate, text in [
            (lambda s: s.update(status="DONE"), "status must be"),
            (lambda s: s.update(acceptance_criteria=[]), "acceptance_criteria"),
            (lambda s: s.update(title=" "), "title"),
            (lambda s: s.update(id="S-GOV-002-07"), "numbered in order"),
            (lambda s: s.update(evidence="x"), "evidence must be a list"),
            (lambda s: s.update(evidence=[{"type": "vibes", "summary": "s"}]), "evidence needs a type"),
            (lambda s: s.update(evidence=[{"type": "commit", "summary": "s", "commit": "abc"}]), "40-character"),
            (lambda s: s.update(evidence=[{"type": "test_run", "summary": "s"}]), "command and result"),
        ]:
            s = step(0)
            mutate(s)
            self.assertTrue(any(text in p for p in tc.validate(doc(s))), text)

    def test_duplicate_ids_rejected(self):
        d = doc(step(0), step(0))
        self.assertTrue(any("duplicate" in p for p in tc.validate(d)))

    def test_blocked_needs_reason(self):
        self.assertTrue(any("blocked_reason" in p for p in tc.validate(doc(step(0, "BLOCKED")))))
        self.assertEqual(tc.validate(doc(step(0, "BLOCKED", blocked_reason="waiting for Lukas"))), [])

    def test_duplicated_authoritative_data_is_rejected(self):
        for key in ("weight", "percentage", "lease", "owned_paths", "milestones", "computed"):
            s = step(0)
            s[key] = 1
            self.assertTrue(any("duplicates another authoritative" in p for p in tc.validate(doc(s))), key)

    def test_urls_are_not_local_paths(self):
        self.assertEqual(tc.validate(doc(step(0, note="https://github.com/KasluBuilds/CreatorStudio/pull/4"))), [])

    def test_local_paths_and_secrets_rejected(self):
        for value in ("see C:\\Users\\someone\\x", "/Users/someone/x", "token ghp_" + "A" * 24):
            s = step(0, note=value)
            self.assertTrue(tc.validate(doc(s)), value)


class TestTransitionsAndGate(unittest.TestCase):
    def test_transition_table(self):
        ok = [("PLANNED", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "COMPLETED"),
              ("READY_FOR_REVIEW", "IN_PROGRESS"), ("IN_PROGRESS", "BLOCKED"), ("BLOCKED", "IN_PROGRESS")]
        bad = [("PLANNED", "READY_FOR_REVIEW"), ("PLANNED", "COMPLETED"), ("IN_PROGRESS", "COMPLETED"),
               ("COMPLETED", "IN_PROGRESS"), ("COMPLETED", "PLANNED"), ("BLOCKED", "COMPLETED")]
        for a, b in ok:
            self.assertTrue(tc.can_transition(a, b), (a, b))
        for a, b in bad:
            self.assertFalse(tc.can_transition(a, b), (a, b))
        self.assertEqual(set(tc.TRANSITIONS), set(tc.STATUSES))

    def test_transition_problems_against_base(self):
        base = doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"), step(2))
        self.assertEqual(tc.transition_problems(base, doc(step(0, "COMPLETED"), step(1, "READY_FOR_REVIEW"), step(2))), [])
        skip = doc(step(0, "COMPLETED"), step(1, "COMPLETED"), step(2))
        self.assertTrue(any("not allowed" in p for p in tc.transition_problems(base, skip)))
        edited = doc(step(0, "COMPLETED", title="rewritten"), step(1, "IN_PROGRESS"), step(2))
        self.assertTrue(any("must not change" in p for p in tc.transition_problems(base, edited)))
        removed = doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"))
        self.assertTrue(any("may not be removed" in p for p in tc.transition_problems(base, removed)))
        self.assertEqual(tc.transition_problems(None, base), [])

    def test_completion_gate_requires_every_element(self):
        for field in ("reviewed_by_chatgpt", "lukas_confirmed"):
            s = step(0, "COMPLETED")
            s["completion"][field] = False
            self.assertTrue(any(field in p for p in tc.validate(doc(s))), field)
        s = step(0, "COMPLETED")
        s["completion"]["date"] = "yesterday"
        self.assertTrue(any("date" in p for p in tc.validate(doc(s))))
        s = step(0, "COMPLETED")
        s["completion"]["source"] = ""
        self.assertTrue(any("source" in p for p in tc.validate(doc(s))))
        s = step(0, "COMPLETED")
        s["evidence"] = []
        self.assertTrue(any("evidence record" in p for p in tc.validate(doc(s))))
        s = step(0, "COMPLETED")
        s["completion"] = None
        self.assertTrue(any("'completion' record" in p for p in tc.validate(doc(s))))

    def test_completion_record_only_on_completed_steps(self):
        s = step(0, "READY_FOR_REVIEW", completion=dict(GOOD_COMPLETION))
        self.assertTrue(any("only a COMPLETED step" in p for p in tc.validate(doc(s))))

    def test_next_step_cannot_start_before_the_previous_is_completed(self):
        for first in ("PLANNED", "IN_PROGRESS", "READY_FOR_REVIEW"):
            problems = tc.validate(doc(step(0, first), step(1, "IN_PROGRESS")))
            self.assertTrue(any("earlier step is not COMPLETED" in p or "only one step" in p for p in problems), first)
        # a step after an unfinished one may stay planned or blocked
        self.assertEqual(tc.validate(doc(step(0, "IN_PROGRESS"), step(1), step(2, "BLOCKED", blocked_reason="x"))), [])

    def test_only_one_working_step(self):
        problems = tc.validate(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"), step(2, "READY_FOR_REVIEW")))
        self.assertTrue(any("only one step" in p for p in problems))

    def test_ready_for_review_is_not_completed(self):
        d = doc(step(0, "COMPLETED"), step(1, "READY_FOR_REVIEW"), step(2))
        self.assertEqual(tc.validate(d), [])
        d2 = copy.deepcopy(d)
        d2["tasks"][0]["steps"][2]["status"] = "IN_PROGRESS"  # starting the next step without Lukas's completion record
        self.assertTrue(tc.validate(d2))


class TestCli(unittest.TestCase):
    def test_main_exit_codes(self):
        import contextlib
        import io
        import tempfile
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tc.main(["--root", str(ROOT)]), 0)
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(tc.main(["--root", tmp]), 2)  # files missing
                (Path(tmp) / "ACTIVE_TASKS.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
                (Path(tmp) / "DEVELOPMENT_TRACKER.json").write_text(json.dumps(doc(step(0, "COMPLETED", completion=None))),
                                                                     encoding="utf-8")
                self.assertEqual(tc.main(["--root", tmp]), 1)


class TestCliAgainstBase(unittest.TestCase):
    """The real CLI path (`main --against REF`) over a throwaway Git repository: history is compared, not just helpers."""

    def setUp(self):
        import shutil
        import subprocess
        import tempfile
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        (self.root / "ACTIVE_TASKS.json").write_text(json.dumps(REGISTRY), encoding="utf-8")

    def git(self, *args):
        import subprocess
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

    def write(self, d):
        (self.root / "DEVELOPMENT_TRACKER.json").write_text(json.dumps(d), encoding="utf-8")

    def commit(self, d, msg="c"):
        self.write(d)
        self.git("add", "DEVELOPMENT_TRACKER.json", "ACTIVE_TASKS.json")
        self.git("commit", "-q", "-m", msg)

    def run_cli(self, *extra):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tc.main(["--root", str(self.root), *extra])
        return code, out.getvalue() + err.getvalue()

    def test_valid_transition_passes(self):
        self.commit(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"), step(2)))
        self.write(doc(step(0, "COMPLETED"), step(1, "READY_FOR_REVIEW"), step(2)))
        code, out = self.run_cli("--against", "HEAD")
        self.assertEqual(code, 0, out)
        self.assertIn("transitions checked against HEAD", out)

    def test_illegal_transition_fails_through_the_cli(self):
        self.commit(doc(step(0, "COMPLETED"), step(1, "PLANNED")))
        # PLANNED -> COMPLETED, with a fully valid-looking completion record: only the history check can catch it
        self.write(doc(step(0, "COMPLETED"), step(1, "COMPLETED")))
        self.assertEqual(self.run_cli()[0], 0)  # ordinary validation alone accepts the file
        code, out = self.run_cli("--against", "HEAD")
        self.assertEqual(code, 1)
        self.assertIn("PLANNED -> COMPLETED is not allowed", out)

    def test_editing_a_completed_step_and_removing_a_step_fail(self):
        self.commit(doc(step(0, "COMPLETED"), step(1)))
        self.write(doc(step(0, "COMPLETED", title="rewritten"), step(1)))
        code, out = self.run_cli("--against", "HEAD")
        self.assertEqual(code, 1)
        self.assertIn("must not change", out)
        self.write(doc(step(0, "COMPLETED")))
        code, out = self.run_cli("--against", "HEAD")
        self.assertEqual(code, 1)
        self.assertIn("may not be removed", out)

    def test_no_base_ref_still_validates_the_current_file(self):
        self.write(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS")))
        self.assertEqual(self.run_cli()[0], 0)
        self.write(doc(step(0, "COMPLETED", completion=None)))
        self.assertEqual(self.run_cli()[0], 1)

    def test_base_without_a_tracker_file_compares_nothing(self):
        (self.root / "x.txt").write_text("x", encoding="utf-8")
        self.git("add", "x.txt", "ACTIVE_TASKS.json")
        self.git("commit", "-q", "-m", "no tracker")
        self.write(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS")))
        code, out = self.run_cli("--against", "HEAD")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to compare", out)

    def test_bad_refs_and_missing_values_exit_2(self):
        self.commit(doc(step(0)))
        self.assertEqual(self.run_cli("--against", "no-such-ref")[0], 2)
        self.assertEqual(self.run_cli("--against", "--output=x")[0], 2)  # option-looking ref refused
        self.assertEqual(self.run_cli("--against")[0], 2)

    def test_check_is_read_only(self):
        self.commit(doc(step(0, "COMPLETED"), step(1, "PLANNED")))
        self.write(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS")))
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.run_cli("--against", "HEAD")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})


if __name__ == "__main__":
    unittest.main()
