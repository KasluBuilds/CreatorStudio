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

import build_snapshot as bs  # noqa: E402
import tracker_check as tc  # noqa: E402

SHA = "a" * 40
GOOD_COMPLETION = {"reviewed_by_chatgpt": True, "lukas_confirmed": True, "date": "2026-11-12",
                   "source": "DIRECT: Lukas said so in the session"}


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

    def test_at_most_one_working_step_per_task_and_later_steps_are_blocked_from_starting(self):
        for task in self.tracker["tasks"]:
            working = [s for s in task["steps"] if s["status"] in tc.WORKING]
            self.assertLessEqual(len(working), 1, task["task_id"])
            seen_unfinished = False
            for s in task["steps"]:
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

    def test_protocol_authority_rules_do_not_contradict_the_standing_instruction(self):
        proto = (ROOT / "DEVELOPMENT_STEP_PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("## 3a. Who decides what", proto)
        section = proto.split("## 3a. Who decides what", 1)[1].split("\n## ", 1)[0]
        for needle in ("standing instruction", "Technically approve", "forbidden", "default integrator",
                       "ChatGPT under Lukas's explicit standing delegation", "Claude never does these",
                       "Lukas retains final authority", "DIRECT", "REPORTED",
                       "must not claim that Lukas reviewed the code", "READY_FOR_REVIEW"):
            self.assertIn(needle, section)
        # The old contradictory rules are gone.
        self.assertNotIn("only Lukas starts the next step", proto)
        self.assertNotIn("stays `PLANNED` and blocked until Lukas explicitly says to continue", proto)
        # Claude still stops, never self-approves; merging and lease release belong to the delegated integrator.
        self.assertNotIn("Lukas alone opens or merges", proto)
        self.assertIn("**stop**", proto)
        self.assertIn("never approves its own step", proto)
        pop = (ROOT / "PROJECT_OPERATING_PROTOCOL.md").read_text(encoding="utf-8")
        self.assertNotIn("Lukas alone opens or merges", pop)
        self.assertIn("Claude never approves its own step, merges PRs or releases leases", pop)
        self.assertIn("ChatGPT handles task PR opening/merging and lease release under Lukas's explicit standing delegation", pop)
        self.assertIn("never approves its own step", pop)

    def test_protocol_says_the_working_step_limit_is_per_task(self):
        proto = (ROOT / "DEVELOPMENT_STEP_PROTOCOL.md").read_text(encoding="utf-8")
        item = next(l for l in proto.splitlines() if l.startswith("1. **One step at a time"))
        for needle in ("per task", "Different tasks may each have one", "one writer to `main`", "claims and leases"):
            self.assertIn(needle, item)
        self.assertNotIn("At most one step is `IN_PROGRESS`", item)  # the old global wording
        pop = (ROOT / "PROJECT_OPERATING_PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("the working-step limit is per task", pop)
        for text in (proto, pop):
            self.assertIn("one-time bootstrap exception", text)

    def test_real_completion_sources_are_labelled(self):
        real = json.loads((ROOT / "DEVELOPMENT_TRACKER.json").read_text(encoding="utf-8"))
        completed = [s for t in real["tasks"] for s in t["steps"] if s["status"] == "COMPLETED"]
        self.assertTrue(completed)
        for s in completed:
            self.assertRegex(s["completion"]["source"], r"^(DIRECT|REPORTED)\b", s["id"])


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

    def test_lukas_confirmed_diagnostic_matches_protocol_section_3a(self):
        s = step(0, "COMPLETED")
        s["completion"]["lukas_confirmed"] = False
        problems = [p for p in tc.validate(doc(s)) if "lukas_confirmed" in p]
        self.assertEqual(len(problems), 1)
        text = problems[0]
        # The standing instruction is an allowed confirmation: the message must say so and must not demand an
        # explicit statement or describe a REPORTED standing instruction as inference.
        for needle in ("standing instruction", "REPORTED", "section 3a"):
            self.assertIn(needle, text)
        for banned in ("explicit word", "never inferred"):
            self.assertNotIn(banned, text)

    def test_standing_instruction_confirmation_is_accepted_without_a_separate_statement(self):
        s = step(0, "COMPLETED")
        s["completion"]["lukas_confirmed"] = True
        s["completion"]["source"] = ("REPORTED: Lukas's standing instruction authorizes ChatGPT to approve technical "
                                     "acceptance and advance between steps. This does not claim Lukas reviewed the code.")
        self.assertEqual(tc.validate(doc(s)), [])

    def test_active_tasks_wording_matches_protocol_section_3a(self):
        registry = json.loads((ROOT / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
        task = [t for t in registry["tasks"] if t["id"] == "T-GOV-002"][0]
        text = task["description"] + " " + " ".join(task["acceptance_criteria"])
        self.assertNotIn("blocked until Lukas explicitly says to continue", text)
        self.assertIn("standing instruction", text)
        self.assertNotIn("Lukas alone opens or merges", text)
        self.assertIn("Claude never self-approves, merges PRs or releases leases", text)
        self.assertIn("Lukas retains final authority", text)
        self.assertIn("never", text.lower())  # Claude never approves its own step
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
        # The source must honestly say whether the confirmation was direct or reported.
        for bad in ("Lukas said so in the session", "approved", "reported earlier", "DIRECTLY maybe"):
            s = step(0, "COMPLETED")
            s["completion"]["source"] = bad
            self.assertTrue(any("DIRECT" in p and "REPORTED" in p for p in tc.validate(doc(s))), bad)
        for good in ("DIRECT: yes", "REPORTED: standing instruction; Lukas did not review the code", "REPORTED"):
            s = step(0, "COMPLETED")
            s["completion"]["source"] = good
            self.assertEqual(tc.validate(doc(s)), [], good)
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


def task(task_id, *steps):
    return {"task_id": task_id, "title": task_id, "steps": list(steps)}


def astep(n, status="PLANNED", prefix="AST-001", **extra):
    s = step(n, status, **extra)
    s["id"] = f"S-{prefix}-{n:02d}"
    return s


def multi(*tasks):
    return {"schema_version": 1, "authority": "a", "protocol": "DEVELOPMENT_STEP_PROTOCOL.md", "tasks": list(tasks)}


MULTI_REGISTRY = {"tasks": [{"id": "T-GOV-002"}, {"id": "T-AST-001"}]}


class TestPerTaskLimit(unittest.TestCase):
    def test_two_tasks_may_each_have_one_working_step(self):
        for a, b in (("IN_PROGRESS", "IN_PROGRESS"), ("READY_FOR_REVIEW", "IN_PROGRESS"),
                     ("READY_FOR_REVIEW", "READY_FOR_REVIEW")):
            d = multi(task("T-GOV-002", step(0, "COMPLETED"), step(1, a), step(2)),
                      task("T-AST-001", astep(0, b), astep(1)))
            self.assertEqual(tc.validate(d, MULTI_REGISTRY), [], (a, b))

    def test_two_working_steps_in_one_task_are_rejected_and_name_the_task(self):
        d = multi(task("T-GOV-002", step(0, "COMPLETED"), step(1, "IN_PROGRESS"), step(2, "READY_FOR_REVIEW")),
                  task("T-AST-001", astep(0, "IN_PROGRESS"), astep(1)))
        problems = [p for p in tc.validate(d, MULTI_REGISTRY) if "only one step" in p]
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("T-GOV-002:"), problems[0])
        self.assertNotIn("T-AST-001", problems[0])
        # the second task breaking the limit is reported for that task instead
        d = multi(task("T-GOV-002", step(0, "IN_PROGRESS")),
                  task("T-AST-001", astep(0, "COMPLETED"), astep(1, "IN_PROGRESS"), astep(2, "IN_PROGRESS")))
        problems = [p for p in tc.validate(d, MULTI_REGISTRY) if "only one step" in p]
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("T-AST-001:"), problems[0])

    def test_ordering_is_enforced_independently_per_task(self):
        # task B starting step 1 over an unfinished step 0 fails even though task A is perfectly fine
        d = multi(task("T-GOV-002", step(0, "COMPLETED"), step(1, "IN_PROGRESS")),
                  task("T-AST-001", astep(0, "PLANNED"), astep(1, "IN_PROGRESS")))
        problems = tc.validate(d, MULTI_REGISTRY)
        self.assertTrue(any("S-AST-001-01" in p and "earlier step is not COMPLETED" in p for p in problems), problems)
        self.assertFalse(any("S-GOV-002" in p for p in problems), problems)
        # an unfinished step in task A does not hold back task B's steps
        d = multi(task("T-GOV-002", step(0, "IN_PROGRESS"), step(1)),
                  task("T-AST-001", astep(0, "COMPLETED"), astep(1, "IN_PROGRESS")))
        self.assertEqual(tc.validate(d, MULTI_REGISTRY), [])

    def test_unregistered_task_is_still_rejected(self):
        d = multi(task("T-GOV-002", step(0, "IN_PROGRESS")), task("T-AST-001", astep(0, "IN_PROGRESS")))
        problems = tc.validate(d, REGISTRY)
        self.assertTrue(any(p.startswith("T-AST-001:") and "not registered" in p for p in problems), problems)
        self.assertEqual([p for p in problems if "only one step" in p], [])

    def test_real_tracker_validates_under_the_per_task_rule(self):
        tracker, registry = tc.load(ROOT)
        self.assertEqual(tc.validate(tracker, registry), [])


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
        result = subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                 "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)
        return result.stdout.strip()

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
        self.commit(doc(step(0, "COMPLETED"), step(1, "PLANNED"), step(2)))
        base = self.git("rev-parse", "HEAD")
        self.commit(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"), step(2)))
        self.commit(doc(step(0, "COMPLETED"), step(1, "READY_FOR_REVIEW"), step(2)))
        code, out = self.run_cli("--against", base)
        self.assertEqual(code, 0, out)
        self.assertIn("first-parent history", out)

    def test_illegal_intermediate_transition_cannot_be_hidden_by_later_state(self):
        self.commit(doc(step(0, "COMPLETED"), step(1, "PLANNED"), step(2)))
        base = self.git("rev-parse", "HEAD")
        self.commit(doc(step(0, "COMPLETED"), step(1, "READY_FOR_REVIEW"), step(2)))
        # READY_FOR_REVIEW -> IN_PROGRESS is legal, but it cannot erase the earlier skipped state.
        self.write(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"), step(2)))
        code, out = self.run_cli("--against", base)
        self.assertEqual(code, 1)
        self.assertIn("PLANNED -> READY_FOR_REVIEW is not allowed", out)

    def test_valid_history_after_merging_newer_main(self):
        self.commit(doc(step(0, "COMPLETED"), step(1, "PLANNED"), step(2)))
        self.git("checkout", "-b", "task")
        self.commit(doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"), step(2)))
        self.git("checkout", "main")
        (self.root / "main-only.txt").write_text("main advanced", encoding="utf-8")
        self.git("add", "main-only.txt")
        self.git("commit", "-q", "-m", "advance main")
        main_ref = self.git("rev-parse", "HEAD")
        self.git("checkout", "task")
        self.git("merge", "--no-ff", "-m", "merge main", "main")
        self.commit(doc(step(0, "COMPLETED"), step(1, "READY_FOR_REVIEW"), step(2)))
        task_head = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")
        self.git("merge", "--no-ff", "-m", "synthetic PR merge", "task")
        # GitHub Actions checks out the synthetic merge commit by default. That view alone
        # hides the task branch's first-parent checkpoint, so CI must pass the actual PR head.
        self.assertEqual(self.run_cli("--against", main_ref)[0], 1)
        code, out = self.run_cli("--against", main_ref, "--head", task_head)
        self.assertEqual(code, 0, out)

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


def status_doc():
    return {"schema_version": 1, "updated": "2026-11-12", "repository": "https://github.com/example/repo",
            "weights_status": "PROPOSED", "baseline": {"commit": SHA, "branch": "main"},
            "computed": {"implementation_percent": 12.3, "verification_percent": 4.5, "total_weight": 10,
                         "milestone_count": 1},  # deliberately not what any recalculation would give
            "open_decisions": ["decide X"], "next_actions": ["do Y"],
            "milestones": [{"id": "M-AAA-01", "title": "m", "phase": "Phase 1", "weight": 3,
                            "implementation": {"state": "IMPLEMENTED"},
                            "verification": {"state": "IN_PROGRESS", "level": "UNIT_ISOLATED"},
                            "depends_on": [], "open_questions": [], "blockers": []}],
            "user_action_gates": [{"id": "G-X", "title": "g", "status": "PENDING", "trigger_milestones": ["M-AAA-01"],
                                   "message": "m", "steps": ["secret-ish step list"]}]}


def tasks_registry():
    return {"tasks": [{"id": "T-GOV-002", "title": "t", "owner": "claude:x", "branch": "task/x",
                       "worktree": "../wt/x", "status": "ACTIVE", "depends_on": [],
                       "lease": {"acquired": "2026-11-12T10:00:00+00:00", "expires": "2026-11-13T10:00:00+00:00"},
                       "owned_paths": ["dashboard/"], "acceptance_criteria": ["c"], "integration_notes": "n"}]}


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        import datetime
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()
        self.now = datetime.datetime(2026, 11, 12, 15, 0, 0, tzinfo=datetime.timezone.utc)
        self.put("PROJECT_STATUS.json", status_doc())
        self.put("ACTIVE_TASKS.json", tasks_registry())
        self.put("DEVELOPMENT_TRACKER.json", doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS")))

    def put(self, name, value):
        text = value if isinstance(value, str) else json.dumps(value, indent=2)
        (self.root / name).write_bytes(text.encode("utf-8"))  # LF bytes on every platform

    def git(self, *args):
        import subprocess
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

    def init_repo(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")

    def cli(self, *extra):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bs.main(["--root", str(self.root), *extra])
        return code, out.getvalue() + err.getvalue()


class TestSnapshotContent(SnapshotCase):
    def test_marks_itself_as_a_snapshot_not_live_data(self):
        snap = bs.build(self.root, self.now)
        self.assertTrue(snap["is_snapshot"])
        self.assertIs(snap["live_github_data"], False)
        self.assertIs(snap["github"]["live"], False)
        self.assertIn("not live GitHub data", snap["notice"])
        self.assertEqual(snap["source"]["generated_at"], "2026-11-12T15:00:00Z")

    def test_progress_is_copied_never_recalculated(self):
        snap = bs.build(self.root, self.now)
        self.assertEqual(snap["progress"]["computed"], status_doc()["computed"])
        self.assertEqual(snap["progress"]["weights_status"], "PROPOSED")

    def test_real_repository_sources_build_and_keep_the_real_figures(self):
        real = json.loads((ROOT / "PROJECT_STATUS.json").read_text(encoding="utf-8"))
        snap = bs.build(ROOT, self.now)
        self.assertEqual(snap["progress"]["computed"], real["computed"])
        self.assertEqual(len(snap["milestones"]), len(real["milestones"]))
        self.assertTrue(any(t["id"] == "T-GOV-002" for t in snap["tasks"]))
        self.assertEqual(snap["development_tracker"]["tasks"][0]["task_id"], "T-GOV-002")

    def test_tasks_omit_local_layout_and_gates_omit_step_lists(self):
        text = bs.render(bs.build(self.root, self.now))
        self.assertNotIn("../wt/x", text)
        self.assertNotIn("owned_paths", text)
        self.assertNotIn("secret-ish step list", text)
        self.assertIn("2026-11-13T10:00:00+00:00", text)

    def test_deterministic_apart_from_generation_time(self):
        a = bs.render(bs.build(self.root, self.now))
        self.assertEqual(a, bs.render(bs.build(self.root, self.now)))
        import datetime
        later = self.now + datetime.timedelta(hours=1)
        self.assertNotEqual(a, bs.render(bs.build(self.root, later)))

    def test_source_hashes_ignore_line_endings(self):
        a = bs.build(self.root, self.now)["source"]["files"]
        p = self.root / "ACTIVE_TASKS.json"
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(a, bs.build(self.root, self.now)["source"]["files"])

    def test_naive_generation_time_refused(self):
        import datetime
        with self.assertRaises(bs.SnapshotError):
            bs.build(self.root, datetime.datetime(2026, 11, 12, 15, 0, 0))

    def test_tracker_pull_requests_are_recorded_without_querying_github(self):
        t = doc(step(0, "COMPLETED"))
        t["tasks"][0]["steps"][0]["evidence"] = [{"type": "pull_request", "summary": "merged", "commit": SHA,
                                                  "ref": "https://github.com/example/repo/pull/4"}]
        self.put("DEVELOPMENT_TRACKER.json", t)
        gh = bs.build(self.root, self.now)["github"]
        self.assertEqual(gh["pull_requests_recorded_in_tracker"][0]["ref"], "https://github.com/example/repo/pull/4")
        self.assertIn("not queried", gh["note"])


class TestSnapshotGit(SnapshotCase):
    def test_commit_branch_and_clean_tree(self):
        self.init_repo()
        src = bs.build(self.root, self.now)["source"]
        self.assertTrue(src["git_available"])
        self.assertRegex(src["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(src["branch"], "main")
        self.assertIs(src["working_tree_clean"], True)
        self.assertEqual(src["uncommitted_file_count"], 0)

    def test_dirty_tree_is_reported(self):
        self.init_repo()
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        p = self.root / "ACTIVE_TASKS.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["updated"] = "x"
        p.write_text(json.dumps(data), encoding="utf-8")
        src = bs.build(self.root, self.now)["source"]
        self.assertIs(src["working_tree_clean"], False)
        self.assertEqual(src["uncommitted_file_count"], 2)

    def test_not_a_git_repository_gives_nulls_not_guesses(self):
        src = bs.build(self.root, self.now)["source"]  # the temp folder is not inside a repository
        if src["git_available"]:
            self.skipTest("temporary directory is inside a Git repository on this machine")
        self.assertIsNone(src["commit"])
        self.assertIsNone(src["branch"])
        self.assertIsNone(src["working_tree_clean"])
        self.assertIn("unknown", src["note"])

    def test_unavailable_git_still_writes_a_snapshot_with_null_metadata_and_exit_0(self):
        original = bs._git
        bs._git = lambda root, *args: None  # simulates git missing or failing
        self.addCleanup(setattr, bs, "_git", original)
        out = Path(self._tmp.name) / "snapshot.json"
        code, text = self.cli("--out", str(out))
        self.assertEqual(code, 0, text)
        src = json.loads(out.read_text(encoding="utf-8"))["source"]
        self.assertIs(src["git_available"], False)
        self.assertIsNone(src["commit"])
        self.assertIsNone(src["working_tree_clean"])
        self.assertIn("unknown", src["note"])
        self.assertIn("unknown", text)

    def test_cleanliness_is_measured_before_the_output_is_written(self):
        self.init_repo()
        out = self.root / "snap.json"
        snap = bs.write_snapshot(self.root, out, self.now)
        self.assertIs(snap["source"]["working_tree_clean"], True)
        self.assertTrue(out.exists())


class TestSnapshotExclusions(SnapshotCase):
    def test_private_data_is_scrubbed_everywhere(self):
        bad = ["see C:\\Users\\Someone\\Videos\\x", "/Users/someone/secret", "/home/someone/x",
               "Projects/my-first-test and Incoming/trip", "clip_0001.MP4 and take2.wav",
               "token ghp_" + "A" * 24, "key sk-" + "b" * 20, "-----BEGIN RSA PRIVATE KEY-----"]
        st = status_doc()
        st["open_decisions"] = bad
        st["milestones"][0]["open_questions"] = bad
        self.put("PROJECT_STATUS.json", st)
        snap = bs.build(self.root, self.now)
        text = bs.render(snap)
        for needle in ("Someone", "/Users/", "/home/", "my-first-test", "Incoming/", "clip_0001", "take2", "ghp_AAAA",
                       "sk-bbbb", "PRIVATE KEY", "C:\\"):
            self.assertNotIn(needle, text, needle)
        self.assertGreaterEqual(snap["sanitization"]["total"], 12)
        for rule in ("local_path", "private_path", "media_file", "credential"):
            self.assertIn(rule, snap["sanitization"]["replacements"], rule)

    def test_urls_and_ordinary_text_survive_scrubbing(self):
        counts = {}
        text = "https://github.com/KasluBuilds/CreatorStudio/pull/4 orchestrator/tests/test_x.py DEVELOPMENT_TRACKER.json"
        self.assertEqual(bs.scrub(text, counts), text)
        self.assertEqual(counts, {})

    def test_scrub_does_not_alter_non_strings(self):
        counts = {}
        value = {"a": 1.5, "b": [True, None, 3], "c": {"d": "ok"}}
        self.assertEqual(bs.scrub(value, counts), value)

    def test_real_snapshot_has_no_private_data(self):
        import re
        text = bs.render(bs.build(ROOT, self.now))
        for pattern in (r"[A-Za-z]:\\", r"/Users/", r"Korisnik", r"(?:Incoming|Projects)/", r"\.(?:mp4|mov|wav)\b",
                        r"ghp_[A-Za-z0-9]{20}", r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16}"):
            self.assertIsNone(re.search(pattern, text, re.IGNORECASE), pattern)


class TestSnapshotInputsAndCli(SnapshotCase):
    def sources(self):
        return {n: (self.root / n).read_bytes() for n in bs.SOURCES}

    def test_cli_writes_valid_json_and_leaves_sources_untouched(self):
        before = self.sources()
        out = Path(self._tmp.name) / "snapshot.json"
        code, text = self.cli("--out", str(out))
        self.assertEqual(code, 0, text)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(data["is_snapshot"])
        self.assertEqual(before, self.sources())
        self.assertEqual([p.name for p in Path(self._tmp.name).iterdir() if p.name.startswith(".snapshot-")], [])

    def test_missing_or_invalid_inputs_fail_clearly_and_write_nothing(self):
        out = Path(self._tmp.name) / "snapshot.json"
        (self.root / "ACTIVE_TASKS.json").unlink()
        code, text = self.cli("--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("ACTIVE_TASKS.json", text)
        self.assertFalse(out.exists())
        self.put("ACTIVE_TASKS.json", "{not json")
        code, text = self.cli("--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", text)
        self.put("ACTIVE_TASKS.json", tasks_registry())
        (self.root / "PROJECT_STATUS.json").write_bytes(b"\xff\xfe\x00")
        self.assertEqual(self.cli("--out", str(out))[0], 2)
        self.assertFalse(out.exists())

    def test_structurally_wrong_sources_are_refused(self):
        out = Path(self._tmp.name) / "snapshot.json"
        st = status_doc()
        del st["computed"]
        self.put("PROJECT_STATUS.json", st)
        code, text = self.cli("--out", str(out))
        self.assertEqual(code, 1)
        self.assertIn("never recalculated", text)
        self.put("PROJECT_STATUS.json", status_doc())
        self.put("DEVELOPMENT_TRACKER.json", doc(step(0, "COMPLETED", completion=None)))
        code, text = self.cli("--out", str(out))
        self.assertEqual(code, 1)
        self.assertIn("DEVELOPMENT_TRACKER.json failed validation", text)
        self.put("DEVELOPMENT_TRACKER.json", doc(step(0, "COMPLETED")))
        self.put("ACTIVE_TASKS.json", json.dumps({"tasks": "x"}))
        self.assertEqual(self.cli("--out", str(out))[0], 1)
        self.assertFalse(out.exists())

    def test_bad_arguments_and_output_paths(self):
        before = self.sources()
        self.assertEqual(self.cli()[0], 2)  # --out is required
        self.assertEqual(self.cli("--out")[0], 2)
        self.assertEqual(self.cli("--out", str(self.root / "PROJECT_STATUS.json"))[0], 2)  # never overwrite a source
        self.assertEqual(self.cli("--out", str(self.root))[0], 2)  # a directory
        self.assertEqual(self.cli("--out", str(self.root / "no_such_dir" / "s.json"))[0], 2)
        self.assertEqual(before, self.sources())

    def test_existing_output_is_replaced_with_identical_content_for_the_same_time(self):
        out = Path(self._tmp.name) / "snapshot.json"
        out.write_text("old", encoding="utf-8")
        bs.write_snapshot(self.root, out, self.now)
        first = out.read_bytes()
        bs.write_snapshot(self.root, out, self.now)
        self.assertEqual(first, out.read_bytes())
        self.assertTrue(first.endswith(b"\n"))


PRIVATE_STRINGS = [
    r"Projects\secretproj\clip01.mov",
    r"Incoming\tripday\take.wav",
    r"tools\privbin\ffmpeg.exe",
    "Projects/secretproj/clip01.mov",
    "/mnt/data/mydata/notes.txt",
    "/tmp/mydata/out",
    "/root/mydata",
    "/workspace/worksecret/a",
    "/home/someone/mydata",
    "/Users/someone/mydata",
    r"C:\Users\someone\mydata\x",
    "file:///tmp/mydata/x",
    "../CreatorStudio-worktrees/mydata",
    "~/mydata/x",
]
# Every directory name and file name above that must be gone: removing only the media file name is not enough.
PRIVATE_FRAGMENTS = ("secretproj", "tripday", "privbin", "mydata", "worksecret", "someone", "clip01", "take.wav",
                     "ffmpeg.exe", "Projects", "Incoming", "CreatorStudio-worktrees")

ORDINARY_STRINGS = [
    "orchestrator/tests/test_x.py", "dashboard/build_snapshot.py", "AI_Creator_Studio_Documentation/MASTER.md",
    "https://github.com/KasluBuilds/CreatorStudio/pull/4", "https://example.com/tmp/x", "https://example.com/data/x",
    "origin/main", "git push -u origin task/x", "and/or", "a/mnt/b", "DEVELOPMENT_TRACKER.json", "Resolve 21.0.4.5",
]


def _field_mutators():
    """(name, function(status, tasks, tracker, value)) placing a string into one field the snapshot copies out."""
    return [
        ("status.open_decisions", lambda s, t, k, v: s["open_decisions"].append(v)),
        ("status.next_actions", lambda s, t, k, v: s["next_actions"].append(v)),
        ("status.baseline.scope", lambda s, t, k, v: s["baseline"].update(scope=v)),
        ("status.repository", lambda s, t, k, v: s.update(repository=v)),
        ("milestone.title", lambda s, t, k, v: s["milestones"][0].update(title=v)),
        ("milestone.phase", lambda s, t, k, v: s["milestones"][0].update(phase=v)),
        ("milestone.open_questions", lambda s, t, k, v: s["milestones"][0]["open_questions"].append(v)),
        ("milestone.blockers", lambda s, t, k, v: s["milestones"][0]["blockers"].append(v)),
        ("gate.title", lambda s, t, k, v: s["user_action_gates"][0].update(title=v)),
        ("gate.message", lambda s, t, k, v: s["user_action_gates"][0].update(message=v)),
        ("task.title", lambda s, t, k, v: t["tasks"][0].update(title=v)),
        ("task.integration_notes", lambda s, t, k, v: t["tasks"][0].update(integration_notes=v)),
        ("task.acceptance_criteria", lambda s, t, k, v: t["tasks"][0]["acceptance_criteria"].append(v)),
        ("tracker.task.title", lambda s, t, k, v: k["tasks"][0].update(title=v)),
        ("tracker.step.title", lambda s, t, k, v: k["tasks"][0]["steps"][1].update(title=v)),
        ("tracker.step.acceptance_criteria", lambda s, t, k, v: k["tasks"][0]["steps"][1]["acceptance_criteria"].append(v)),
        ("tracker.evidence.summary", lambda s, t, k, v: k["tasks"][0]["steps"][0]["evidence"][0].update(summary=v)),
        ("tracker.completion.source", lambda s, t, k, v: k["tasks"][0]["steps"][0]["completion"].update(source="REPORTED: " + v)),
        ("tracker.step.note", lambda s, t, k, v: k["tasks"][0]["steps"][1].update(note=v)),
    ]


class TestPathScrubbingRegression(SnapshotCase):
    def test_private_directories_are_removed_not_just_media_names(self):
        for value in PRIVATE_STRINGS:
            counts = {}
            out = bs.scrub("before " + value + " after", counts)
            for frag in PRIVATE_FRAGMENTS:
                self.assertNotIn(frag, out, (value, frag))
            self.assertTrue(out.startswith("before [") and out.endswith("] after"), (value, out))
            self.assertEqual(sum(counts.values()), 1, (value, counts))

    def test_unc_paths_are_removed(self):
        value = chr(92) * 2 + "fileserver" + chr(92) + "share" + chr(92) + "mydata"
        out = bs.scrub("see " + value + " now", {})
        self.assertNotIn("fileserver", out)
        self.assertNotIn("mydata", out)

    def test_ordinary_repository_paths_and_urls_are_unchanged(self):
        for value in ORDINARY_STRINGS:
            counts = {}
            self.assertEqual(bs.scrub(value, counts), value)
            self.assertEqual(counts, {}, value)

    def test_each_source_field_is_scrubbed(self):
        for name, mutate in _field_mutators():
            values = PRIVATE_STRINGS
            if name.startswith("tracker."):  # the tracker validator already refuses Windows/Users/home paths outright
                values = [v for v in PRIVATE_STRINGS if not tc.LOCAL_PATH.search(v)]
            self.assertGreater(len(values), 8)
            st, tk, tr = status_doc(), tasks_registry(), doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"))
            tr["tasks"][0]["steps"][0]["evidence"] = [{"type": "observation", "summary": "ok"}]
            mutate(st, tk, tr, " ; ".join(values))  # one build per field; ';' separates tokens like prose does
            self.put("PROJECT_STATUS.json", st)
            self.put("ACTIVE_TASKS.json", tk)
            self.put("DEVELOPMENT_TRACKER.json", tr)
            snap = bs.build(self.root, self.now)
            text = bs.render(snap)
            for frag in PRIVATE_FRAGMENTS:
                self.assertNotIn(frag, text, (name, frag))
            self.assertGreaterEqual(snap["sanitization"]["total"], len(values), name)

    def test_dictionary_keys_are_scrubbed(self):
        credential = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
        for key in list(PRIVATE_STRINGS) + [credential]:
            counts = {}
            out = bs.scrub({"outer": {key: {"inner": [{key + "/deeper": 1}]}}}, counts)
            text = json.dumps(out)
            for frag in PRIVATE_FRAGMENTS + ("sk-a1B2",):
                self.assertNotIn(frag, text, (key, frag))
            self.assertGreaterEqual(sum(counts.values()), 2, (key, counts))

    def test_private_and_credential_keys_do_not_reach_the_rendered_snapshot(self):
        credential = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
        for key in ["Projects/secretproj/clip01.mov", "/mnt/data/mydata/notes.txt", "../CreatorStudio-worktrees/mydata",
                    "~/mydata/x", credential]:
            tr = doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"))
            tr["tasks"][0]["steps"][0]["evidence"] = [{"type": "observation", "summary": "ok", key: "extra"}]
            self.put("DEVELOPMENT_TRACKER.json", tr)
            try:
                text = bs.render(bs.build(self.root, self.now))
            except bs.SnapshotError as exc:  # refusing is also safe, but the key must not be echoed
                for frag in PRIVATE_FRAGMENTS + ("sk-a1B2",):
                    self.assertNotIn(frag, str(exc), key)
                continue
            for frag in PRIVATE_FRAGMENTS + ("sk-a1B2",):
                self.assertNotIn(frag, text, (key, frag))

    def test_key_collision_is_refused_deterministically_without_dropping_data(self):
        data = {"/mnt/data/aaa": 1, "/mnt/data/bbb": 2}  # both become "[local-path]"
        for _ in range(3):
            with self.assertRaises(bs.SnapshotError) as ctx:
                bs.scrub(data, {})
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("became identical", str(ctx.exception))
            self.assertNotIn("aaa", str(ctx.exception))
            self.assertNotIn("bbb", str(ctx.exception))
        self.assertEqual(data, {"/mnt/data/aaa": 1, "/mnt/data/bbb": 2})  # the input is untouched

    def test_key_collision_through_the_cli_writes_nothing_and_exits_2(self):
        tr = doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"))
        tr["tasks"][0]["steps"][0]["evidence"] = [{"type": "observation", "summary": "ok",
                                                   "/mnt/data/aaa": 1, "/mnt/data/bbb": 2}]
        self.put("DEVELOPMENT_TRACKER.json", tr)
        out = self.root / "snap.json"
        code, text = self.cli("--out", str(out))
        self.assertEqual(code, 2, text)
        self.assertFalse(out.exists())
        self.assertNotIn("aaa", text)

    def test_ordinary_keys_are_unchanged(self):
        counts = {}
        data = {"summary": 1, "orchestrator/tests/test_x.py": 2, "https://github.com/KasluBuilds/CreatorStudio/pull/4": 3}
        self.assertEqual(bs.scrub(data, counts), data)
        self.assertEqual(counts, {})

    def test_ordinary_strings_survive_in_every_source_field(self):
        for name, mutate in _field_mutators():
            st, tk, tr = status_doc(), tasks_registry(), doc(step(0, "COMPLETED"), step(1, "IN_PROGRESS"))
            tr["tasks"][0]["steps"][0]["evidence"] = [{"type": "observation", "summary": "ok"}]
            mutate(st, tk, tr, "see orchestrator/tests/test_x.py and https://github.com/KasluBuilds/CreatorStudio/pull/4")
            self.put("PROJECT_STATUS.json", st)
            self.put("ACTIVE_TASKS.json", tk)
            self.put("DEVELOPMENT_TRACKER.json", tr)
            text = bs.render(bs.build(self.root, self.now))
            self.assertIn("orchestrator/tests/test_x.py", text, name)
            self.assertIn("https://github.com/KasluBuilds/CreatorStudio/pull/4", text, name)



INDEX_HTML = Path(__file__).resolve().parents[2] / "dashboard" / "index.html"

NODE_DRIVER = """
const vm = require('vm'), fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const ctx = vm.createContext({});
vm.runInContext(code, ctx);
const out = {};
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
for (const k of Object.keys(cases)) {
  const r = ctx.Dash.analyze(cases[k]);
  out[k] = {errors: r.errors, warnings: r.warnings, model: r.model && {
    impl: r.model.implementation, ver: r.model.verification, phases: r.model.phases.map(p => p.phase),
    current: r.model.current && r.model.current.step.id, gate: r.model.gate && r.model.gate.state,
    gateText: r.model.gate && r.model.gate.text,
    terminal: r.model.terminal && r.model.terminal.state, terminalText: r.model.terminal && r.model.terminal.text,
    blockers: r.model.blockers.length}};
}
out.url_ok = ctx.Dash.safeGithubUrl('https://github.com/KasluBuilds/CreatorStudio/pull/4');
out.url_bad = [ctx.Dash.safeGithubUrl('javascript:alert(1)'), ctx.Dash.safeGithubUrl('https://evil.example/x'),
               ctx.Dash.safeGithubUrl('https://github.com.evil.example/x'), ctx.Dash.safeGithubUrl(null)];
console.log(JSON.stringify(out));
"""


class TestDashboardPage(SnapshotCase):
    """dashboard/index.html: static safety checks, plus its pure logic run under Node when Node is installed.
    The visual rendering in a real browser is not tested here."""

    @staticmethod
    def html():
        return INDEX_HTML.read_text(encoding="utf-8")

    @classmethod
    def script(cls):
        import re
        return "\n".join(re.findall(r"<script>(.*?)</script>", cls.html(), re.S))

    def test_page_is_self_contained_and_dependency_free(self):
        import re
        html = self.html()
        self.assertIn("<!doctype html>", html.lower())
        self.assertNotRegex(html, r"<script[^>]*\bsrc=")
        self.assertNotRegex(html, r"<link[^>]*\bhref=")
        self.assertNotRegex(html, r"@import|url\(")
        self.assertEqual(re.findall(r"""(?:src|action)\s*=\s*["']https?://""", html), [])
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("default-src 'none'", html)  # blocks every network connection
        self.assertNotIn("connect-src", html)

    def test_page_has_no_network_write_or_credential_paths(self):
        everything = self.html()
        for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource", "importScripts",
                       "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                       "localStorage", "sessionStorage", "indexedDB", "document.cookie", "Authorization", "Bearer",
                       "password", "api_key", "apikey", "showSaveFilePicker", "showDirectoryPicker",
                       "navigator.clipboard", "<form", "require("):
            self.assertNotIn(banned, everything, banned)

    def test_page_states_it_is_a_snapshot_and_read_only(self):
        html = self.html()
        for needle in ("SNAPSHOT, NOT LIVE DATA", "not live GitHub data", "Read-only viewer", "never recalculates progress",
                       "Not live: pull request state is not queried"):
            self.assertIn(needle, html)

    def test_page_has_required_sections_and_a_file_loader(self):
        html = self.html()
        for needle in ('type="file"', "dragover", "FileReader", "Completion gate", "Terminal-clear status", "Milestones by phase",
                       "Open decisions", "Blockers", "Next actions", "Task steps", "Acceptance criteria", "Evidence",
                       "source commit", "generated at (UTC)", "viewport"):
            self.assertIn(needle, html)

    def _node(self, cases):
        import shutil
        import subprocess
        import tempfile
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not available")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "page.js").write_text(self.script(), encoding="utf-8")
            (tmp / "driver.js").write_text(NODE_DRIVER, encoding="utf-8")
            (tmp / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
            r = subprocess.run([node, str(tmp / "driver.js"), str(tmp / "page.js"), str(tmp / "cases.json")],
                               capture_output=True, text=True, encoding="utf-8", cwd=str(tmp), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def _snap(self, mutate=None):
        snap = bs.build(self.root, self.now)
        if mutate:
            mutate(snap)
        return json.dumps(snap)

    @staticmethod
    def _steps(snap):
        return snap["development_tracker"]["tasks"][0]["steps"]

    def test_real_snapshot_is_understood_and_progress_is_copied(self):
        text = self._snap()
        out = self._node({"ok": text})["ok"]
        self.assertEqual(out["errors"], [])
        snap = json.loads(text)
        self.assertEqual(out["model"]["impl"], snap["progress"]["computed"]["implementation_percent"])
        self.assertEqual(out["model"]["ver"], snap["progress"]["computed"]["verification_percent"])
        self.assertEqual(out["model"]["current"], snap["development_tracker"]["tasks"][0]["steps"][1]["id"])
        self.assertEqual(out["model"]["gate"], "warn")

    def test_bad_input_gives_useful_errors_not_a_blank_page(self):
        good = json.loads(self._snap())
        no_progress = {k: v for k, v in good.items() if k != "progress"}
        out = self._node({"empty": "", "garbage": "{not json", "array": "[1,2]", "number": "5", "null": "null",
                          "newer": json.dumps(dict(good, snapshot_schema_version=99)),
                          "older": json.dumps(dict(good, snapshot_schema_version=0)),
                          "no_progress": json.dumps(no_progress),
                          "not_snapshot": json.dumps(dict(good, is_snapshot=False))})
        for name in ("empty", "garbage", "array", "number", "null", "newer", "no_progress", "not_snapshot"):
            self.assertTrue(out[name]["errors"], name)
            self.assertIsNone(out[name]["model"], name)
        self.assertTrue(any("newer" in e for e in out["newer"]["errors"]))
        self.assertEqual(out["older"]["errors"], [])
        self.assertTrue(any("older" in w for w in out["older"]["warnings"]))

    def test_missing_optional_sections_degrade_gracefully(self):
        good = json.loads(self._snap())
        for key in ("milestones", "tasks", "open_decisions", "next_actions", "user_action_gates", "github",
                    "development_tracker", "sanitization"):
            good.pop(key)
        good["progress"]["computed"] = {"implementation_percent": "lots", "verification_percent": 140}
        out = self._node({"x": json.dumps(good)})["x"]
        self.assertEqual(out["errors"], [])
        self.assertIsNone(out["model"]["impl"])  # invalid figures are not shown or invented
        self.assertIsNone(out["model"]["ver"])
        self.assertIsNone(out["model"]["current"])
        self.assertTrue(out["warnings"])

    def test_terminal_status_follows_the_snapshot(self):
        def case(status, clean, count=None):
            def f(snap):
                self._steps(snap)[1]["status"] = status
                snap["source"]["working_tree_clean"] = clean
                snap["source"]["uncommitted_file_count"] = count
            return self._snap(f)
        out = self._node({"working": case("IN_PROGRESS", True), "ready_clean": case("READY_FOR_REVIEW", True),
                          "ready_dirty": case("READY_FOR_REVIEW", False, 3), "ready_unknown": case("READY_FOR_REVIEW", None)})
        self.assertEqual(out["working"]["model"]["terminal"], "bad")  # still working: never "clear"
        self.assertEqual(out["ready_clean"]["model"]["terminal"], "ok")
        self.assertIn("pushed", out["ready_clean"]["model"]["terminalText"])  # says what a snapshot cannot know
        self.assertEqual(out["ready_dirty"]["model"]["terminal"], "bad")
        self.assertEqual(out["ready_unknown"]["model"]["terminal"], "warn")

    def test_completion_gate_is_never_passed_early_or_without_a_record(self):
        def case(status, completion, evidence=True):
            def f(snap):
                st = self._steps(snap)[1]
                st["status"] = status
                st["completion"] = completion
                st["evidence"] = [{"type": "observation", "summary": "x"}] if evidence else []
            return self._snap(f)
        full = {"reviewed_by_chatgpt": True, "lukas_confirmed": True, "date": "2026-11-12", "source": "REPORTED: x"}
        out = self._node({"ready": case("READY_FOR_REVIEW", None), "blocked": case("BLOCKED", None),
                          "done_full": case("COMPLETED", full), "done_partial": case("COMPLETED", {"reviewed_by_chatgpt": True}),
                          "done_no_evidence": case("COMPLETED", full, evidence=False)})
        # a COMPLETED step is not a "working" step, so the current step is whichever is still working (none here)
        self.assertEqual(out["ready"]["model"]["gate"], "warn")
        self.assertEqual(out["blocked"]["model"]["gate"], "bad")
        self.assertEqual(out["blocked"]["model"]["blockers"], 1)
        for name in ("done_full", "done_partial", "done_no_evidence"):
            self.assertIsNone(out[name]["model"]["gate"], name)

    def test_github_links_are_limited_to_github_https_urls(self):
        out = self._node({})
        self.assertEqual(out["url_ok"], "https://github.com/KasluBuilds/CreatorStudio/pull/4")
        self.assertEqual(out["url_bad"], [None, None, None, None])

    def test_milestones_are_grouped_by_phase_in_first_seen_order(self):
        def f(snap):
            base = snap["milestones"][0]
            snap["milestones"] = [dict(base, id="A", phase="Phase 2"), dict(base, id="B", phase="Phase 1"),
                                  dict(base, id="C", phase="Phase 2")]
        out = self._node({"x": self._snap(f)})["x"]
        self.assertEqual(out["model"]["phases"], ["Phase 2", "Phase 1"])


if __name__ == "__main__":
    unittest.main()
