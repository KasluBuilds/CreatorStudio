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


if __name__ == "__main__":
    unittest.main()
