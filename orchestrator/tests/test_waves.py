"""Tests for governance.waves: manifest rules, scope, closeout records, and the git machinery (init, worktrees,
submit with a concurrent push, verify, close) run against a throwaway bare origin. Synthetic data only."""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from governance import cli
from governance import tasks as tasks_mod
from governance import waves

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
SHA2 = "b" * 40

REGISTRY = {
    "schema_version": 1, "shared_files": {"CHANGELOG.md": "x", "ACTIVE_TASKS.json": "x"},
    "package_owners": {
        "assets": {"paths": ["orchestrator/assets/"], "owner": "claude:asset-stream", "owner_status": "REPORTED"},
        "orphan": {"paths": ["orchestrator/orphan/"], "owner": "UNRESOLVED", "owner_status": "UNRESOLVED"},
    },
    "integration_agreements": {},
    "tasks": [{"id": "T-OLD-001", "status": "ACTIVE", "owned_paths": ["orchestrator/busy/"],
               "owner": "claude:x", "branch": "task/old"}],
}


def lane(i, owner="claude:zz-stream", paths=None, **kw):
    d = {"id": f"L{i}", "task_id": f"T-ZZ{i}-001", "title": f"Lane {i}", "owner": owner,
         "branch": f"lane/w01-demo/L{i}", "owned_paths": paths or [f"orchestrator/zz{i}/"],
         "test_modules": [f"test_zz{i}.py"], "acceptance_criteria": ["it works"], "depends_on": []}
    d.update(kw)
    return d


def manifest(lanes=None):
    return {"schema_version": 1, "wave_id": "w01-demo", "title": "Demo wave", "integration_branch": "wave/w01-demo",
            "base_commit": SHA, "created": "2026-11-17", "inactivity_hours": 48,
            "lanes": lanes if lanes is not None else [lane(1), lane(2)]}


def report(lid, **kw):
    d = {"lane": lid, "status": "DONE", "summary": "did the thing", "unresolved": [],
         "commands": [{"command": "python -B -I -m unittest", "result": "OK"}], "changelog": "Did the thing."}
    d.update(kw)
    return d


class ManifestTests(unittest.TestCase):
    def problems(self, m, reg=REGISTRY):
        return waves.validate_manifest(m, reg)

    def assertRejected(self, m, text, reg=REGISTRY):
        ps = self.problems(m, reg)
        self.assertTrue(any(text in p for p in ps), f"expected {text!r} in {ps}")

    def test_valid(self):
        self.assertEqual(self.problems(manifest()), [])

    def test_garbage_never_raises(self):
        for g in (None, [], "x", {}, {"lanes": "x"}, {"lanes": [None, 3]}):
            self.assertIsInstance(waves.validate_manifest(g, REGISTRY), list)

    def test_header_rules(self):
        m = manifest(); m["wave_id"] = "demo"
        self.assertRejected(m, "wave_id must match")
        m = manifest(); m["integration_branch"] = "wave/other"
        self.assertRejected(m, "integration_branch must be")
        m = manifest(); m["base_commit"] = "abc"
        self.assertRejected(m, "base_commit must be a full 40-character SHA")
        m = manifest(); m["inactivity_hours"] = 0
        self.assertRejected(m, "inactivity_hours")
        self.assertRejected(manifest([]), "lanes must be a non-empty list")

    def test_lanes_must_be_disjoint(self):
        m = manifest([lane(1, paths=["orchestrator/zz/"]), lane(2, paths=["orchestrator/zz/sub/a.py"])])
        self.assertRejected(m, "both own")

    def test_shared_and_reserved_paths_are_refused(self):
        for bad in ("CHANGELOG.md", "ACTIVE_TASKS.json", "PROJECT_STATUS.json", "CLAUDE.md"):
            self.assertRejected(manifest([lane(1, paths=[bad])]), "shared file")
        for bad in (".github/workflows/x.yml", "waves/w01-demo/wave.json", "tools/", "Incoming/a", ".githooks/pre-push",
                    "pro.ps1", ".mcp.json"):
            self.assertRejected(manifest([lane(1, paths=[bad])]), "is reserved")

    def test_path_syntax(self):
        for bad in ("../x", "/abs/x", "C:/x", "a/*.py", "a\\b"):
            self.assertRejected(manifest([lane(1, paths=[bad])]), "path")

    def test_package_ownership_needs_authorization(self):
        m = manifest([lane(1, owner="claude:other-stream", paths=["orchestrator/assets/rights.py"])])
        self.assertRejected(m, "owned by claude:asset-stream")
        auth = {"path": "orchestrator/assets/rights.py", "granted_by": "lukas", "reference": "message 2026-11-17"}
        m = manifest([lane(1, owner="claude:other-stream", paths=["orchestrator/assets/rights.py"],
                           authorizations=[auth])])
        self.assertEqual(self.problems(m), [])
        m = manifest([lane(1, owner="claude:asset-stream", paths=["orchestrator/assets/"])])
        self.assertEqual(self.problems(m), [])
        self.assertRejected(manifest([lane(1, paths=["orchestrator/orphan/"])]), "owned by UNRESOLVED")
        bad = dict(auth, granted_by="claude")
        self.assertRejected(manifest([lane(1, paths=["orchestrator/orphan/"], authorizations=[bad])]),
                            "each authorization needs")

    def test_overlap_with_an_active_task(self):
        self.assertRejected(manifest([lane(1, paths=["orchestrator/busy/a.py"])]), "overlaps active task T-OLD-001")

    def test_task_ids(self):
        self.assertRejected(manifest([lane(1), lane(2, task_id="T-ZZ1-001")]), "is also used by lane L1")
        self.assertRejected(manifest([lane(1, task_id="T-OLD-001")]), "already exists in ACTIVE_TASKS.json")
        self.assertRejected(manifest([lane(1, task_id="bad")]), "task_id must match")

    def test_lane_fields(self):
        self.assertRejected(manifest([lane(1, branch="lane/w01-demo/L9")]), "branch must be")
        self.assertRejected(manifest([lane(1, test_modules=["rm -rf"])]), "test_modules")
        self.assertRejected(manifest([lane(1, test_modules=[])]), "test_modules")
        self.assertRejected(manifest([lane(1, owner="chatgpt:x")]), "Claude Code stream")
        self.assertRejected(manifest([lane(1, acceptance_criteria=[])]), "acceptance_criteria")
        self.assertRejected(manifest([lane(1, id="X1")]), "id must match")

    def test_lane_dependencies(self):
        self.assertRejected(manifest([lane(1, depends_on=["L9"])]), "unknown lane L9")
        self.assertRejected(manifest([lane(1, depends_on=["L1"])]), "depends on itself")
        self.assertRejected(manifest([lane(1, depends_on=["L2"]), lane(2, depends_on=["L1"])]), "cycle")

    def test_more_than_the_limit_is_refused(self):
        ls = [lane(i, task_id=f"T-ZZ{i}-001", id=f"L{i}", paths=[f"orchestrator/zz{i}/"]) for i in range(1, 52)]
        self.assertRejected(manifest(ls), "at most")

    def test_template_manifest_is_valid(self):
        t = json.loads((ROOT / "waves" / "TEMPLATE" / "wave.json").read_text(encoding="utf-8"))
        self.assertEqual(waves.validate_manifest(t, REGISTRY, allow_auto_base=True), [])
        self.assertTrue(waves.validate_manifest(t, REGISTRY))     # AUTO is only for an unstarted manifest


class ScopeAndReportTests(unittest.TestCase):
    def test_lane_scope(self):
        m = manifest()
        ok = ["orchestrator/zz1/a.py", "orchestrator/zz1/sub/b.py", "waves/w01-demo/lanes/L1/report.json"]
        self.assertEqual(waves.lane_scope_problems(m, "L1", ok), [])
        bad = waves.lane_scope_problems(m, "L1", ["orchestrator/zz2/a.py", "CHANGELOG.md",
                                                  "waves/w01-demo/wave.json", "waves/w01-demo/lanes/L2/report.json"])
        self.assertEqual(len(bad), 4)
        self.assertTrue(waves.lane_scope_problems(m, "L7", ["a"]))

    def test_wave_scope(self):
        m = manifest()
        self.assertEqual(waves.wave_scope_problems(m, ["orchestrator/zz1/a.py", "orchestrator/zz2/b.py",
                                                       "waves/w01-demo/wave.json"]), [])
        self.assertEqual(len(waves.wave_scope_problems(m, ["CHANGELOG.md", "orchestrator/elsewhere/x.py"])), 2)
        self.assertEqual(waves.wave_scope_problems(m, ["CHANGELOG.md", "SESSION_HANDOFF.md"], allow_closeout=True), [])
        self.assertTrue(waves.wave_scope_problems(m, ["PROJECT_STATUS.json"], allow_closeout=True))

    def test_report_rules(self):
        self.assertEqual(waves.report_problems(report("L1"), "L1"), [])
        self.assertEqual(waves.report_problems(report("L1", status="IN_PROGRESS", commands=[], changelog=""), "L1"), [])
        self.assertTrue(waves.report_problems(report("L2"), "L1"))
        self.assertTrue(waves.report_problems(report("L1", status="MAYBE"), "L1"))
        self.assertTrue(waves.report_problems(report("L1", summary=""), "L1"))
        self.assertTrue(waves.report_problems(report("L1", commands=[]), "L1"))
        self.assertTrue(waves.report_problems(report("L1", changelog="two\nlines"), "L1"))
        self.assertTrue(waves.report_problems(report("L1", status="IN_PROGRESS"), "L1", need_done=True))
        self.assertTrue(waves.report_problems("nope", "L1"))

    def test_secret_scan(self):
        self.assertTrue(waves.secret_problems(["token = ghp_" + "a" * 24]))
        self.assertEqual(waves.secret_problems(["task-governance-foundation is fine"]), [])

    def test_brief_names_ownership_and_submit(self):
        m = manifest()
        b = waves.build_brief(m, m["lanes"][0])
        for s in ("orchestrator/zz1/", "waves/w01-demo/lanes/L1/", "governance wave submit", "never merge pull requests",
                  "Acceptance criteria", "test_zz1.py"):
            self.assertIn(s, b)


class CloseoutRecordTests(unittest.TestCase):
    def docs(self):
        reg = json.loads((ROOT / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
        trk = json.loads((ROOT / "DEVELOPMENT_TRACKER.json").read_text(encoding="utf-8"))
        return reg, trk

    def build(self, source="REPORTED: ChatGPT approved under Lukas's standing delegation; not independently verified.",
              approver="chatgpt", lukas=True):
        reg, trk = self.docs()
        m = manifest()
        reports = {"L1": report("L1"), "L2": report("L2")}
        heads = {"L1": SHA, "L2": SHA2}
        return m, waves.closeout_records(m, reg, trk, reports, heads, date="2026-11-17", source=source,
                                         wave_head="c" * 40, approver=approver, lukas_standing=lukas)

    def test_generated_records_pass_the_real_validators(self):
        sys.path.insert(0, str(ROOT / "dashboard"))
        try:
            import tracker_check
        finally:
            sys.path.pop(0)
        m, (reg, trk, entry) = self.build()
        self.assertEqual(tasks_mod.validate(reg), [])
        self.assertEqual(tracker_check.validate(trk, reg), [])
        ids = {t["id"]: t for t in reg["tasks"]}
        for tid in ("T-ZZ1-001", "T-ZZ2-001"):
            self.assertEqual(ids[tid]["status"], "COMPLETED")
            self.assertIsNone(ids[tid]["lease"])
            self.assertEqual(ids[tid]["owned_paths"], [])
        self.assertIn("wave `w01-demo`", entry)
        self.assertIn("T-ZZ1-001", entry)
        self.assertIn("does not claim Lukas reviewed the code", entry)

    def test_inputs_are_not_modified(self):
        reg, trk = self.docs()
        before = (json.dumps(reg, sort_keys=True), json.dumps(trk, sort_keys=True))
        waves.closeout_records(manifest(), reg, trk, {"L1": report("L1"), "L2": report("L2")},
                               {"L1": SHA, "L2": SHA2}, date="2026-11-17", source="REPORTED: x", wave_head=SHA,
                               approver="chatgpt", lukas_standing=True)
        self.assertEqual(before, (json.dumps(reg, sort_keys=True), json.dumps(trk, sort_keys=True)))

    def test_approver_is_recorded_honestly(self):
        sys.path.insert(0, str(ROOT / "dashboard"))
        try:
            import tracker_check
        finally:
            sys.path.pop(0)
        for who, chatgpt_flag in (("chatgpt", True), ("repository_coordinator", False), ("lukas", False)):
            _, (reg, trk, _) = self.build(approver=who)
            comp = trk["tasks"][-1]["steps"][0]["completion"]
            self.assertEqual((comp["approved_by"], comp["reviewed_by_chatgpt"]), (who, chatgpt_flag))
            self.assertEqual(tracker_check.validate(trk, reg), [], who)

    def test_the_gate_rejects_inconsistent_approval_records(self):
        sys.path.insert(0, str(ROOT / "dashboard"))
        try:
            import tracker_check
        finally:
            sys.path.pop(0)
        _, (reg, trk, _) = self.build(approver="repository_coordinator")
        comp = trk["tasks"][-1]["steps"][0]["completion"]
        comp["reviewed_by_chatgpt"] = True                      # a coordinator approval must not claim ChatGPT
        self.assertTrue(any("must not claim reviewed_by_chatgpt" in p for p in tracker_check.validate(trk, reg)))
        comp["reviewed_by_chatgpt"] = False; comp["approved_by"] = "chatgpt"
        self.assertTrue(any("approved_by chatgpt needs" in p for p in tracker_check.validate(trk, reg)))
        comp["approved_by"] = "somebody"
        self.assertTrue(any("approved_by must be one of" in p for p in tracker_check.validate(trk, reg)))
        del comp["approved_by"]                                 # legacy shape still works only with ChatGPT review
        self.assertTrue(any("needs reviewed_by_chatgpt true" in p for p in tracker_check.validate(trk, reg)))

    def test_bad_approval_source_is_caught_by_the_tracker_validator(self):
        sys.path.insert(0, str(ROOT / "dashboard"))
        try:
            import tracker_check
        finally:
            sys.path.pop(0)
        _, (reg, trk, _) = self.build(source="the integrator said so")
        self.assertTrue(any("must begin with DIRECT" in p for p in tracker_check.validate(trk, reg)))


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class GitFlowTests(unittest.TestCase):
    """Real git against a bare origin in a temp directory. Layout mirrors production: <tmp>/seed is the primary
    checkout, <tmp>/CreatorStudio-worktrees holds the coordinator and lane worktrees."""

    @classmethod
    def setUpClass(cls):
        cls._env = {k: os.environ.get(k) for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                                                    "GIT_COMMITTER_EMAIL")}
        os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                          GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wavetest-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.seed = self.tmp / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.seed)], check=True)
        _git(self.seed, "config", "core.autocrlf", "false")
        for rel in ("ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json", "PROJECT_STATUS.json", "CHANGELOG.md",
                    "SESSION_HANDOFF.md", "dashboard/tracker_check.py"):
            dst = self.seed / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))
        _write(self.seed / "README.md", "seed\n")
        _git(self.seed, "add", "--", ".")
        _git(self.seed, "commit", "-q", "-m", "seed")
        _git(self.seed, "remote", "add", "origin", str(self.origin))
        _git(self.seed, "push", "-q", "origin", "main")
        m = manifest([lane(1), lane(2, depends_on=["L1"])])
        m["base_commit"] = "AUTO"
        for l in m["lanes"]:
            l["task_id"] = l["task_id"].replace("ZZ", "ZY")
        self.mfile = self.tmp / "m.json"
        self.mfile.write_text(json.dumps(m), encoding="utf-8")
        self.log = []
        self.assertEqual(waves.init_wave(self.seed, self.mfile, log=self.log.append), 0, self.log)
        self.assertEqual(waves.make_lane_worktrees(self.seed, "w01-demo", log=self.log.append), 0, self.log)
        wt = self.tmp / "CreatorStudio-worktrees"
        self.coord, self.l1, self.l2 = wt / "w01-demo-coordinator", wt / "w01-demo-L1", wt / "w01-demo-L2"
        self.ok_tests = lambda root, mods: (True, "")

    def submit(self, wt, runner=None):
        out = []
        rc = waves.submit(wt, test_runner=runner or self.ok_tests, log=out.append)
        return rc, out

    def work(self, wt, lid, name="a.py", text="x = 1\n", status="DONE"):
        _write(wt / f"orchestrator/zz{lid}/{name}", text)
        rep = report(f"L{lid}", status=status)
        _write(wt / f"waves/w01-demo/lanes/L{lid}/report.json", json.dumps(rep))
        _git(wt, "add", "--", f"orchestrator/zz{lid}", f"waves/w01-demo/lanes/L{lid}/report.json")
        _git(wt, "commit", "-q", "-m", f"lane {lid} work")

    def wave_tip(self):
        return _git(self.seed, "ls-remote", str(self.origin), "refs/heads/wave/w01-demo").split()[0]

    def test_init_created_branch_manifest_and_briefs(self):
        _git(self.seed, "fetch", "-q", "origin")
        tree = _git(self.seed, "ls-tree", "-r", "--name-only", "origin/wave/w01-demo")
        for f in ("waves/w01-demo/wave.json", "waves/w01-demo/lanes/L1/BRIEF.md", "waves/w01-demo/lanes/L2/BRIEF.md"):
            self.assertIn(f, tree)
        m = waves.load_manifest_at(self.seed, "origin/wave/w01-demo", "w01-demo")
        self.assertEqual(m["base_commit"], _git(self.seed, "rev-parse", "origin/main"))
        self.assertTrue(self.l1.is_dir() and self.l2.is_dir())
        self.assertEqual(_git(self.l1, "rev-parse", "--abbrev-ref", "HEAD"), "lane/w01-demo/L1")
        # a second init of the same wave is refused
        out = []
        self.assertEqual(waves.init_wave(self.seed, self.mfile, log=out.append), 1)

    def test_init_refuses_an_invalid_manifest(self):
        m = json.loads(self.mfile.read_text(encoding="utf-8"))
        m["wave_id"] = "w02-bad"; m["integration_branch"] = "wave/w02-bad"
        m["lanes"][0]["owned_paths"] = ["CHANGELOG.md"]
        for l in m["lanes"]:
            l["branch"] = l["branch"].replace("w01-demo", "w02-bad")
        f = self.tmp / "bad.json"; f.write_text(json.dumps(m), encoding="utf-8")
        out = []
        self.assertEqual(waves.init_wave(self.seed, f, log=out.append), 1)
        self.assertTrue(any("shared file" in l for l in out))
        self.assertEqual(_git(self.seed, "ls-remote", "--heads", str(self.origin), "wave/w02-bad"), "")

    def test_submit_delivers_to_the_wave_branch_not_main(self):
        main_before = _git(self.seed, "ls-remote", str(self.origin), "refs/heads/main")
        self.work(self.l1, 1)
        rc, out = self.submit(self.l1)
        self.assertEqual(rc, 0, out)
        _git(self.seed, "fetch", "-q", "origin")
        self.assertIn("orchestrator/zz1/a.py", _git(self.seed, "ls-tree", "-r", "--name-only", "origin/wave/w01-demo"))
        self.assertEqual(_git(self.seed, "ls-remote", str(self.origin), "refs/heads/main"), main_before)
        self.assertEqual(self.wave_tip(), _git(self.l1, "rev-parse", "HEAD"))

    def test_second_lane_merges_the_first_lanes_work(self):
        self.work(self.l1, 1)
        self.assertEqual(self.submit(self.l1)[0], 0)
        self.work(self.l2, 2)
        rc, out = self.submit(self.l2)
        self.assertEqual(rc, 0, out)
        _git(self.seed, "fetch", "-q", "origin")
        tree = _git(self.seed, "ls-tree", "-r", "--name-only", "origin/wave/w01-demo")
        self.assertIn("orchestrator/zz1/a.py", tree)
        self.assertIn("orchestrator/zz2/a.py", tree)

    def test_a_concurrent_push_is_retried_not_lost(self):
        self.work(self.l1, 1)
        self.work(self.l2, 2)
        calls = []

        def racing_runner(root, mods):
            calls.append(root)
            if len(calls) == 1:           # lane 2 pushes while lane 1 is still testing
                self.assertEqual(self.submit(self.l2)[0], 0)
            return True, ""
        rc, out = self.submit(self.l1, racing_runner)
        self.assertEqual(rc, 0, out)
        self.assertGreaterEqual(len(calls), 2)               # lane 1 re-ran after merging lane 2's push
        _git(self.seed, "fetch", "-q", "origin")
        tree = _git(self.seed, "ls-tree", "-r", "--name-only", "origin/wave/w01-demo")
        self.assertIn("orchestrator/zz1/a.py", tree)
        self.assertIn("orchestrator/zz2/a.py", tree)

    def test_scope_violations_are_refused_and_nothing_is_pushed(self):
        tip = self.wave_tip()
        self.work(self.l1, 1)
        _write(self.l1 / "orchestrator/zz2/steal.py", "x\n")
        _write(self.l1 / "CHANGELOG.md", "shared\n")
        _git(self.l1, "add", "--", "orchestrator/zz2/steal.py", "CHANGELOG.md")
        _git(self.l1, "commit", "-q", "-m", "oops")
        rc, out = self.submit(self.l1)
        self.assertEqual(rc, 1)
        self.assertTrue(any("zz2/steal.py" in l for l in out) and any("CHANGELOG.md" in l for l in out), out)
        self.assertEqual(self.wave_tip(), tip)

    def test_a_lane_cannot_edit_the_manifest(self):
        self.work(self.l1, 1)
        mp = self.l1 / "waves/w01-demo/wave.json"
        m = json.loads(mp.read_text(encoding="utf-8")); m["lanes"][0]["owned_paths"].append("orchestrator/")
        mp.write_text(json.dumps(m), encoding="utf-8")
        _git(self.l1, "add", "--", "waves/w01-demo/wave.json")
        _git(self.l1, "commit", "-q", "-m", "widen scope")
        tip = self.wave_tip()
        rc, out = self.submit(self.l1)
        self.assertEqual(rc, 1)
        self.assertTrue(any("wave.json" in l for l in out), out)
        self.assertEqual(self.wave_tip(), tip)

    def test_dirty_tree_missing_report_failing_tests_and_secrets_are_refused(self):
        _write(self.l1 / "orchestrator/zz1/a.py", "x\n")
        rc, out = self.submit(self.l1)
        self.assertEqual(rc, 1)
        self.assertTrue(any("uncommitted" in l for l in out))
        _git(self.l1, "add", "--", "orchestrator/zz1/a.py"); _git(self.l1, "commit", "-q", "-m", "no report")
        rc, out = self.submit(self.l1)
        self.assertEqual((rc, any("report.json is missing" in l for l in out)), (1, True))
        self.work(self.l1, 1, "b.py")
        rc, out = self.submit(self.l1, lambda r, m: (False, "boom"))
        self.assertEqual((rc, any("tests failed" in l for l in out)), (1, True))
        self.work(self.l1, 1, "c.py", "t = 'ghp_" + "a" * 24 + "'\n")
        rc, out = self.submit(self.l1)
        self.assertEqual((rc, any("credential" in l for l in out)), (1, True))

    def test_conflict_markers_are_refused(self):
        self.work(self.l1, 1, "m.py", "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> other\n")
        rc, out = self.submit(self.l1)
        self.assertEqual(rc, 1)
        self.assertTrue(any("m.py" in l for l in out), out)

    def test_submit_from_a_non_lane_branch_is_an_error(self):
        rc, out = self.submit(self.coord)
        self.assertEqual(rc, 2)

    def test_status_and_stale_detection(self):
        self.work(self.l1, 1)
        self.assertEqual(self.submit(self.l1)[0], 0)
        _git(self.seed, "fetch", "-q", "origin")
        st = waves.status(self.seed, "w01-demo")[0]["lanes"]
        by = {l["lane"]: l for l in st}
        self.assertTrue(by["L1"]["started"] and by["L1"]["submitted"] and not by["L1"]["stale"])
        self.assertEqual(by["L1"]["report"], "DONE")
        self.assertFalse(by["L2"]["started"])
        # lane 2 starts, pushes its branch, and then goes quiet for longer than inactivity_hours
        self.work(self.l2, 2)
        _git(self.l2, "push", "-q", "origin", "HEAD:refs/heads/lane/w01-demo/L2")
        _git(self.seed, "fetch", "-q", "origin")
        import datetime as dt
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=100)
        st = {l["lane"]: l for l in waves.status(self.seed, None, now=later)[0]["lanes"]}
        self.assertTrue(st["L2"]["stale"] and not st["L2"]["submitted"])
        self.assertEqual(waves.status(self.seed, "w99-none"), [])

    def run_cli(self, root, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--root", str(root), *argv])
        return rc, out.getvalue()

    def test_cli_commands_are_wired(self):
        rc, out = self.run_cli(self.seed, "wave", "validate", str(self.mfile))
        self.assertEqual(rc, 0, out)
        self.assertIn("w01-demo", out)
        self.work(self.l1, 1)
        self.assertEqual(self.submit(self.l1)[0], 0)
        _git(self.seed, "fetch", "-q", "origin")
        rc, out = self.run_cli(self.seed, "wave", "status", "w01-demo", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual({l["lane"]: l["submitted"] for l in data[0]["lanes"]}, {"L1": True, "L2": False})
        rc, out = self.run_cli(self.seed, "wave", "status")
        self.assertEqual(rc, 0)
        self.assertIn("SUBMITTED", out)
        _git(self.coord, "pull", "-q", "--ff-only", "origin", "wave/w01-demo")
        rc, out = self.run_cli(self.coord, "wave", "verify", "w01-demo")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(self.coord, "wave", "verify", "w01-demo", "--require-complete")
        self.assertEqual(rc, 1)                       # lane 2 has not delivered
        self.assertIn("lane L2", out)
        rc, out = self.run_cli(self.coord, "wave", "close", "w01-demo", "--approver", "chatgpt", "--source", "REPORTED: x",
                                "--date", "2026-11-17")
        self.assertEqual(rc, 1)                       # no approval flags: refuses

    def test_verify_complete_wave_then_close(self):
        self.work(self.l1, 1)
        self.work(self.l2, 2)
        self.assertEqual(self.submit(self.l1)[0], 0)
        self.assertEqual(self.submit(self.l2)[0], 0)
        _git(self.coord, "pull", "-q", "--ff-only", "origin", "wave/w01-demo")
        self.assertEqual(waves.verify(self.coord, "w01-demo", require_complete=True), [])
        out = []
        src = "REPORTED: ChatGPT approved under Lukas's standing delegation; not independently verified."
        # refused without both honest flags, with a bad source, and off the wave branch
        self.assertEqual(waves.close(self.coord, "w01-demo", source=src, date="2026-11-17", approver="nobody",
                                     lukas_standing=True, log=out.append), 1)
        self.assertEqual(waves.close(self.coord, "w01-demo", source="trust me", date="2026-11-17",
                                     approver="chatgpt", lukas_standing=True, log=out.append), 1)
        self.assertEqual(waves.close(self.l1, "w01-demo", source=src, date="2026-11-17", approver="repository_coordinator",
                                     lukas_standing=True, log=out.append), 1)
        self.assertEqual(waves.close(self.coord, "w01-demo", source=src, date="2026-11-17", approver="repository_coordinator",
                                     lukas_standing=True, log=out.append), 0, out)
        reg = json.loads((self.coord / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
        done = {t["id"]: t for t in reg["tasks"]}
        self.assertEqual(done["T-ZY1-001"]["status"], "COMPLETED")
        self.assertEqual(tasks_mod.validate(reg), [])
        changelog = (self.coord / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("wave `w01-demo`", changelog)
        self.assertLess(changelog.index("wave `w01-demo`"), changelog.index("T-GOV-003"))   # newest entry first
        self.assertEqual(_git(self.coord, "status", "--porcelain"), "")
        # the closeout files are only acceptable once the wave is closed
        self.assertTrue(waves.verify(self.coord, "w01-demo"))
        self.assertEqual(waves.verify(self.coord, "w01-demo", allow_closeout=True), [])
        # origin was never pushed to by close
        self.assertNotEqual(self.wave_tip(), _git(self.coord, "rev-parse", "HEAD"))

    def test_verify_ignores_files_that_came_in_from_main_but_still_catches_strays(self):
        self.work(self.l1, 1)
        self.assertEqual(self.submit(self.l1)[0], 0)
        # main moves on with an unrelated file; the integrator merges main into the wave branch before the single pull request
        _write(self.seed / "unrelated" / "new.txt", "from main\n")
        _git(self.seed, "add", "--", "unrelated/new.txt")
        _git(self.seed, "commit", "-q", "-m", "main moves on")
        _git(self.seed, "push", "-q", "origin", "main")
        _git(self.coord, "pull", "-q", "--ff-only", "origin", "wave/w01-demo")
        _git(self.coord, "fetch", "-q", "origin")
        _git(self.coord, "merge", "-q", "--no-edit", "origin/main")
        self.assertEqual(waves.verify(self.coord, "w01-demo"), [], "main's own files are not the wave's")
        _write(self.coord / "orchestrator/stray/x.py", "x\n")
        _git(self.coord, "add", "--", "orchestrator/stray/x.py")
        _git(self.coord, "commit", "-q", "-m", "stray")
        ps = waves.verify(self.coord, "w01-demo")
        self.assertEqual([p for p in ps if "belongs to no lane" in p], ["orchestrator/stray/x.py changed on the wave branch but belongs to no lane"])

    def test_the_workflow_allows_the_closeout_records_on_wave_branch_pushes(self):
        text = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count("wave verify"), 2)
        self.assertEqual(text.count("--allow-closeout"), 2, "both the push and the pull_request gate accept the closeout commit")

    def test_verify_rejects_unfinished_lanes_and_strays(self):
        self.work(self.l1, 1, status="IN_PROGRESS")
        self.assertEqual(self.submit(self.l1)[0], 0)           # partial work may be submitted
        _git(self.coord, "pull", "-q", "--ff-only", "origin", "wave/w01-demo")
        self.assertEqual(waves.verify(self.coord, "w01-demo"), [])
        ps = waves.verify(self.coord, "w01-demo", require_complete=True)
        self.assertTrue(any("not DONE" in p for p in ps) and any("lane L2" in p for p in ps), ps)
        _write(self.coord / "orchestrator/stray/x.py", "x\n")
        _git(self.coord, "add", "--", "orchestrator/stray/x.py"); _git(self.coord, "commit", "-q", "-m", "stray")
        self.assertTrue(any("belongs to no lane" in p for p in waves.verify(self.coord, "w01-demo")))


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell (Git for Windows provides one)")
class PrePushHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hooktest-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_bytes((ROOT / ".githooks" / "pre-push").read_bytes().replace(b"\r\n", b"\n"))
        hook.chmod(0o755)
        self.origin = self.tmp / "o.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.c = self.tmp / "c"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.c)], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@example.invalid"), ("core.hooksPath", str(hooks))):
            _git(self.c, "config", k, v)
        _git(self.c, "remote", "add", "origin", str(self.origin))
        _write(self.c / "a", "1\n")
        _git(self.c, "add", "--", "a")
        _git(self.c, "commit", "-q", "-m", "one")

    def push(self, *args, env=None):
        e = dict(os.environ, **(env or {}))
        return subprocess.run(["git", "-C", str(self.c), "push", "-q", "origin", *args], capture_output=True,
                              text=True, env=e).returncode

    def test_main_is_refused_and_override_works(self):
        self.assertNotEqual(self.push("HEAD:refs/heads/main"), 0)
        self.assertEqual(self.push("HEAD:refs/heads/feature"), 0)
        self.assertEqual(self.push("HEAD:refs/heads/main", env={"CREATORSTUDIO_ALLOW_PUSH_OVERRIDE": "1"}), 0)

    def test_wave_and_lane_branches_are_fast_forward_only_and_undeletable(self):
        self.assertEqual(self.push("HEAD:refs/heads/wave/w01-x"), 0)
        _write(self.c / "a", "2\n")
        _git(self.c, "commit", "-q", "-am", "two")
        self.assertEqual(self.push("HEAD:refs/heads/wave/w01-x"), 0)            # fast-forward
        _git(self.c, "reset", "-q", "--hard", "HEAD~1")
        _write(self.c / "a", "3\n")
        _git(self.c, "commit", "-q", "-am", "diverged")
        self.assertNotEqual(self.push("-f", "HEAD:refs/heads/wave/w01-x"), 0)   # forced non-fast-forward
        self.assertNotEqual(self.push("--delete", "wave/w01-x"), 0)
        self.assertEqual(self.push("HEAD:refs/heads/lane/w01-x/L1"), 0)
        self.assertNotEqual(self.push("--delete", "lane/w01-x/L1"), 0)
        self.assertEqual(self.push("--delete", "lane/w01-x/L1", env={"CREATORSTUDIO_ALLOW_PUSH_OVERRIDE": "1"}), 0)


if __name__ == "__main__":
    unittest.main()
