"""Live-autonomy harness tests (T-DEV-026). Offline: real git in temp dirs, NO claude process is started.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_live_autonomy.py
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import live_autonomy as la  # noqa: E402

GOOD = {
    "L-001": "import re\n\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n",
    "L-003": "def clamp(x, lo, hi):\n    if lo > hi:\n        raise ValueError('lo > hi')\n    return max(lo, min(hi, x))\n",
}


_TPL = {}


def setUpModule():
    _TPL["td"] = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    _TPL["root"] = la.build_sandbox(Path(_TPL["td"].name) / "tpl").root     # built once; each test copies it


def tearDownModule():
    _TPL["td"].cleanup()


def ctl(lay, cmd, *args):
    buf = []
    rc = la.ctl(lay, cmd, list(args), buf.append)
    return json.loads(buf[-1]) if buf else {}


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.td.cleanup)
        dst = Path(self.td.name) / "sb"
        shutil.copytree(_TPL["root"], dst)
        self.lay = la.Layout(dst)
        la.git(["remote", "set-url", "origin", str(self.lay.origin)], self.lay.main)

    def led(self):
        return la.read_ledger(self.lay)

    def set_status(self, tid, status, **kw):
        with la.Ledger(self.lay) as L:
            L.data["tasks"][tid]["status"] = status
            L.data["tasks"][tid].update(kw)

    def work(self, tid, source=None, extra=None):
        """Claim, let a 'worker' commit the implementation, mark it WORKER_DONE (what the supervisor would do)."""
        self.assertTrue(ctl(self.lay, "claim", tid)["ok"])
        wt = self.lay.wt / tid
        (wt / la.task_def(tid)["module"]).write_text(source or GOOD[tid], encoding="utf-8")
        files = [la.task_def(tid)["module"]]
        for name, body in (extra or {}).items():
            (wt / name).write_text(body, encoding="utf-8")
            files.append(name)
        la.git(["add", *files], wt)
        la.git(["commit", "-q", "-m", "impl"], wt)
        self.set_status(tid, "WORKER_DONE")

    def approve(self, tid, reviewer="r-test-1", digest=None):
        with la.Ledger(self.lay) as L:
            L.data["reviews"][tid] = {"verdict": "APPROVE", "reviewer": reviewer, "diff_hash": digest or la.diff_hash(self.lay, tid)}
            L.data["tasks"][tid]["status"] = "REVIEWED"


class Sandbox(Base):
    def test_built_in_isolation_with_failing_tests_and_ledger(self):
        self.assertTrue((self.lay.main / "backlog.json").is_file())
        self.assertEqual(set(self.led()["tasks"]), {t["id"] for t in la.TASKS})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:       # the real builder, once
            built = la.build_sandbox(Path(td) / "fresh")
            self.assertTrue((built.main / "tests" / "test_l001.py").is_file())
            self.assertEqual(la.git(["rev-parse", "main"], built.main), la.git(["rev-parse", "origin/main"], built.main))
        self.assertEqual(la.git(["rev-parse", "main"], self.lay.main), la.git(["rev-parse", "origin/main"], self.lay.main))

    def test_nonempty_root_refused(self):
        with self.assertRaises(RuntimeError):
            la.build_sandbox(self.lay.root)

    def test_eligibility_respects_dependencies_and_blocked(self):
        self.assertEqual(ctl(self.lay, "next")["eligible"], ["L-001", "L-002", "L-003", "L-005"])
        self.assertEqual(ctl(self.lay, "next")["waiting_on_dependencies"], ["L-004", "L-006"])
        self.set_status("L-003", "INTEGRATED")
        self.assertIn("L-004", ctl(self.lay, "next")["eligible"])
        self.set_status("L-005", "BLOCKED", note="x")
        n = ctl(self.lay, "next")
        self.assertNotIn("L-006", n["eligible"])
        self.assertIn("L-005", n["blocked"])

    def test_claim_registers_before_dispatch_and_refuses_ineligible(self):
        self.assertTrue(ctl(self.lay, "claim", "L-001")["ok"])
        self.assertEqual(self.led()["tasks"]["L-001"]["status"], "CLAIMED")
        self.assertFalse(ctl(self.lay, "claim", "L-001")["ok"])        # already claimed
        self.assertFalse(ctl(self.lay, "claim", "L-004")["ok"])        # dependency not integrated
        self.assertFalse(ctl(self.lay, "claim", "L-999")["ok"])

    def test_dispatch_queues_a_request_and_counts_attempts(self):
        ctl(self.lay, "claim", "L-001")
        r = ctl(self.lay, "dispatch", "L-001")
        self.assertEqual((r["ok"], r["worker"], r["attempt"]), (True, "w-l-001-1", 1))
        self.assertTrue((self.lay.requests / "w-l-001-1.json").is_file())
        self.assertFalse(ctl(self.lay, "dispatch", "L-001")["ok"])      # a live (queued) worker already exists

    def test_recover_blocks_after_attempts_are_exhausted(self):
        ctl(self.lay, "claim", "L-005")
        with la.Ledger(self.lay) as L:
            L.data["tasks"]["L-005"].update(status="DEAD", attempts=la.MAX_ATTEMPTS)
        r = ctl(self.lay, "recover", "L-005")
        self.assertEqual((r["status"], r["reason"]), ("BLOCKED", "attempts exhausted"))


class VerifyAndGates(Base):
    def test_full_cycle_integrates_through_all_gates(self):
        self.work("L-001")
        v = ctl(self.lay, "verify", "L-001")
        self.assertEqual((v["tests_pass"], v["status"], v["out_of_scope"]), (True, "VERIFIED", []))
        self.approve("L-001")
        dry = la.integrate(self.lay, "L-001", apply=False)
        self.assertTrue(dry["gates_all_pass"] and dry["dry_run"] and len(dry["gates"]) == 8)
        res = ctl(self.lay, "integrate", "L-001")
        self.assertTrue(res["merged"], res)
        self.assertEqual(self.led()["tasks"]["L-001"]["status"], "INTEGRATED")
        self.assertIn("slugify.py", la.git(["ls-tree", "--name-only", "main"], self.lay.main).split())
        self.assertEqual(la.git(["rev-parse", "main"], self.lay.main), la.git(["rev-parse", "origin/main"], self.lay.main))
        self.assertFalse(self.lay.merge_lock.exists())

    def test_failing_tests_are_not_verified(self):
        self.work("L-001", source="def slugify(text):\n    return text\n")
        v = ctl(self.lay, "verify", "L-001")
        self.assertEqual((v["tests_pass"], v["status"]), (False, "CHANGES_NEEDED"))
        self.assertIn("repair", v["hint"])

    def test_out_of_scope_file_fails_verify_and_gate(self):
        self.work("L-003", extra={"stray.txt": "x\n"})
        v = ctl(self.lay, "verify", "L-003")
        self.assertEqual((v["out_of_scope"], v["status"]), (["stray.txt"], "CHANGES_NEEDED"))
        self.approve("L-003")
        g = {x.name: x for x in la.evaluate_gates(self.lay, "L-003", self.led())}
        self.assertFalse(g["scope"].ok)

    def test_review_gate_rejects_wrong_hash_self_review_and_missing(self):
        self.work("L-001")
        ctl(self.lay, "verify", "L-001")
        self.approve("L-001", digest="0" * 16)
        r = ctl(self.lay, "integrate", "L-001")
        self.assertFalse(r["merged"])
        self.assertFalse(next(x for x in r["gates"] if x["gate"] == "review")["ok"])
        self.assertEqual(self.led()["tasks"]["L-001"]["status"], "CHANGES_NEEDED")
        # the reviewer is the worker itself
        self.set_status("L-001", "REVIEWED")
        with la.Ledger(self.lay) as L:
            L.data["workers"]["w-l-001-1"] = {"task": "L-001", "role": "worker", "status": "EXITED"}
            L.data["reviews"]["L-001"] = {"verdict": "APPROVE", "reviewer": "w-l-001-1", "diff_hash": la.diff_hash(self.lay, "L-001")}
        r = la.integrate(self.lay, "L-001", apply=False)
        self.assertFalse(next(x for x in r["gates"] if x["gate"] == "review")["ok"])

    def test_changes_requested_never_merges(self):
        self.work("L-001")
        self.approve("L-001")
        with la.Ledger(self.lay) as L:
            L.data["reviews"]["L-001"]["verdict"] = "CHANGES_REQUESTED"
        self.assertFalse(la.integrate(self.lay, "L-001", apply=False)["gates_all_pass"])

    def test_secret_in_diff_fails_gate(self):
        self.work("L-001", source=GOOD["L-001"] + "KEY = 'AKIAABCDEFGHIJKLMNOP'\n")
        self.approve("L-001")
        r = la.integrate(self.lay, "L-001", apply=False)
        self.assertFalse(next(x for x in r["gates"] if x["gate"] == "secrets")["ok"])
        self.assertFalse(r["gates_all_pass"])

    def test_forbidden_file_fails_gate(self):
        self.work("L-001", extra={".env": "A=1\n"})
        self.approve("L-001")
        r = la.integrate(self.lay, "L-001", apply=False)
        self.assertFalse(next(x for x in r["gates"] if x["gate"] == "forbidden_files")["ok"])

    def test_branch_behind_main_fails_up_to_date(self):
        self.work("L-001")
        self.approve("L-001")
        # main moves on: integrate L-003 first
        self.work("L-003")
        self.approve("L-003")
        self.assertTrue(la.integrate(self.lay, "L-003", apply=True)["merged"])
        r = la.integrate(self.lay, "L-001", apply=False)
        self.assertFalse(next(x for x in r["gates"] if x["gate"] == "up_to_date")["ok"])

    def test_merge_lock_serialises(self):
        self.work("L-001")
        self.approve("L-001")
        self.lay.merge_lock.write_text("held")
        r = la.integrate(self.lay, "L-001", apply=True)
        self.assertFalse(r["merged"])
        self.assertFalse(next(x for x in r["gates"] if x["gate"] == "lock_free")["ok"])
        self.assertNotEqual(self.led()["tasks"]["L-001"]["status"], "INTEGRATED")

    def test_unclaimed_or_unreviewed_task_is_not_registered_for_merge(self):
        self.work("L-001")                                   # WORKER_DONE, never reviewed
        r = la.integrate(self.lay, "L-001", apply=False)
        self.assertFalse(next(x for x in r["gates"] if x["gate"] == "registered")["ok"])


class ProcessesAndLiveness(unittest.TestCase):
    REC = {"pid": 4242, "marker": "cs-live-w1", "create_time": 1000.0, "parent_pid": 7, "worker_id": "w1", "session_id": "s-w1"}

    def live(self, **kw):
        d = {"pid": 4242, "exe": "claude.exe", "cmdline": "claude --append-system-prompt x [cs-live-w1]", "parent_pid": 7,
             "create_time": 1000.4}
        d.update(kw)
        return d

    def test_kill_with_full_identity(self):
        killed = []
        ok, why = la.kill_with_proof(self.REC, 7, info=lambda pid: self.live(), killer=killed.append)
        self.assertEqual((ok, killed), (True, [4242]))

    def test_pid_reuse_is_refused(self):
        killed = []
        for live in (self.live(create_time=5000.0), self.live(cmdline="notepad.exe"), None, self.live(pid=1)):
            ok, why = la.kill_with_proof(self.REC, 7, info=lambda pid, l=live: l, killer=killed.append)
            self.assertFalse(ok, why)
        self.assertEqual(killed, [])

    def test_wrong_parent_or_protected_pid_refused_by_authority(self):
        killed = []
        ok, why = la.kill_with_proof(self.REC, 999, info=lambda pid: self.live(parent_pid=8), killer=killed.append)
        self.assertFalse(ok)
        self.assertIn("authority policy", why)
        ok, why = la.kill_with_proof(self.REC, 7, protected=[4242], info=lambda pid: self.live(), killer=killed.append)
        self.assertFalse(ok)
        self.assertEqual(killed, [])

    def test_liveness_classes_distinguish_alive_from_healthy(self):
        rec = dict(self.REC, started=0.0, last_progress=0.0)
        alive = lambda pid: self.live()
        gone = lambda pid: None
        self.assertEqual(la.classify_worker(dict(rec), 10, alive, 5.0), "RUNNING")
        stalled = dict(rec)
        la.classify_worker(stalled, 10, alive, 5.0)
        self.assertEqual(la.classify_worker(stalled, 10, alive, 5.0 + la.STALL_SECONDS + 1), "STALLED")
        grew = dict(stalled)
        self.assertEqual(la.classify_worker(grew, 99, alive, 5.0 + la.STALL_SECONDS + 1), "RUNNING")
        self.assertEqual(la.classify_worker(dict(rec, exit_code=0), 10, gone, 5.0), "EXITED")
        self.assertEqual(la.classify_worker(dict(rec, exit_code=1), 10, gone, 5.0), "DEAD")
        self.assertEqual(la.classify_worker(dict(rec), 10, gone, 5.0), "DEAD")
        self.assertEqual(la.classify_worker({"worker_id": "q"}, 0, alive, 5.0), "QUEUED")


class LoopControl(Base):
    def test_stop_hook_blocks_while_work_remains_and_releases_at_the_end(self):
        out = []
        la.stop_hook(self.lay, out.append)
        self.assertEqual(json.loads(out[0])["decision"], "block")
        with la.Ledger(self.lay) as L:
            for k in L.data["tasks"]:
                L.data["tasks"][k]["status"] = "INTEGRATED" if k in ("L-001", "L-002", "L-003", "L-004") else "BLOCKED"
        out2 = []
        la.stop_hook(self.lay, out2.append)
        self.assertEqual(out2, [])                                   # nothing left: Main may stop

    def test_stop_hook_has_a_block_cap(self):
        with la.Ledger(self.lay) as L:
            L.data["counters"]["stop_blocks"] = 400
        out = []
        la.stop_hook(self.lay, out.append, max_blocks=400)
        self.assertEqual(out, [])

    def test_verdict_and_result_parsing(self):
        self.assertEqual(la.parse_verdict("ok\nVERDICT: APPROVE"), "APPROVE")
        self.assertEqual(la.parse_verdict("VERDICT: APPROVE\nbut then VERDICT: CHANGES_REQUESTED"), "CHANGES_REQUESTED")
        self.assertIsNone(la.parse_verdict("looks fine"))
        stream = "\n".join([json.dumps({"type": "assistant"}), "junk", json.dumps({"type": "result", "result": "done\nVERDICT: APPROVE"})])
        self.assertEqual(la.parse_verdict(la.final_text(stream)), "APPROVE")

    def test_ledger_lock_serialises_and_recovers_stale_lock(self):
        with la.Ledger(self.lay) as L:
            L.data["counters"]["x"] = 1
        self.assertEqual(self.led()["counters"]["x"], 1)
        self.lay.lock_path.write_text("999")
        import os
        old = 1
        os.utime(self.lay.lock_path, (old, old))
        with la.Ledger(self.lay) as L:                              # stale lock (age > 30s) is taken over
            L.data["counters"]["y"] = 2
        self.assertEqual(self.led()["counters"]["y"], 2)

    def test_dry_run_cli_starts_nothing(self):
        buf = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(buf), tempfile.TemporaryDirectory() as td:
            self.assertEqual(la.main(["run", "--root", str(Path(td) / "x")]), 0)
        self.assertIn("dry run", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
