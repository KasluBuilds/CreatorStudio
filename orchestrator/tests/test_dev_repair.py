"""Supervisor state repair (T-DEV-017): retire stale authoritative Main records.

Offline only: synthetic data in temp directories, injected liveness probes, a fake clock; no network and no real process
is probed. Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_repair.py -v
"""
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import repair  # noqa: E402
from dev_supervisor.state_store import StateStore  # noqa: E402

T0 = 1_800_000_000.0
MAIN = "11111111-1111-4111-8111-111111111111"
STALE = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
LIVE_PID, DEAD_PID, STALE_PID = 4001, 4002, 4003


def alive_set(*pids):
    return lambda pid: pid in pids


def rec(sid, **kw):
    base = {"session_id": sid, "role": "main", "authoritative": True, "status": "ACTIVE",
            "updated_at": "2026-11-18T08:01:39+00:00"}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self._sock = mock.patch.object(socket, "socket", side_effect=AssertionError("network blocked"))
        self._sock.start()
        self.addCleanup(self._sock.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "state"
        self.sess_dir = Path(self.tmp.name) / "claude_sessions"
        self.sess_dir.mkdir()
        self.store = StateStore(self.dir, clock=lambda: T0)

    def put(self, sessions, main=MAIN, main_status="RUNNING", extra=None):
        doc = {"sessions": {r["session_id"]: r for r in sessions}, "failures": {}}
        doc.update(extra or {})
        self.store.write("sessions", doc)
        if main is not None:
            self.store.write("main", {"session_id": main, "status": main_status, "pid": LIVE_PID})

    def session_file(self, pid, sid):
        (self.sess_dir / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": sid, "cwd": "x", "status": "idle"}), encoding="utf-8")

    def listing(self):
        return sorted((str(p.relative_to(self.dir)), p.stat().st_mtime_ns, p.stat().st_size)
                      for p in self.dir.rglob("*") if p.is_file())

    def plan(self, alive=alive_set(LIVE_PID), sess=True):
        return repair.plan(self.dir, sessions_dir=self.sess_dir if sess else None, pid_alive=alive)

    def doc(self):
        return self.store.read("sessions")["sessions"]


class PlanTests(Base):
    def test_stale_proposed_current_kept(self):
        self.put([rec(MAIN, handoff_id="h1"), rec(STALE)])
        p = self.plan()
        self.assertEqual([r["session_id"] for r in p["retire"]], [STALE])
        self.assertEqual([k["session_id"] for k in p["keep"]], [MAIN])
        self.assertEqual(p["authoritative"], [MAIN, STALE])

    def test_current_main_never_proposed_even_if_pid_dead(self):
        self.put([rec(MAIN, pid=DEAD_PID), rec(STALE)])
        p = self.plan(alive=alive_set())
        self.assertNotIn(MAIN, [r["session_id"] for r in p["retire"]])
        self.assertEqual(p["keep"][0]["reason"], "is main.json's session")

    def test_alive_record_pid_kept(self):
        self.put([rec(MAIN), rec(STALE, pid=LIVE_PID)])
        p = self.plan(alive=alive_set(LIVE_PID))
        self.assertEqual(p["retire"], [])
        self.assertEqual({k["session_id"] for k in p["keep"]}, {MAIN, STALE})

    def test_dead_record_pid_proposed(self):
        self.put([rec(MAIN), rec(STALE, pid=DEAD_PID)])
        p = self.plan()
        self.assertEqual(p["retire"][0]["pids"], [DEAD_PID])

    def test_session_file_pid_alive_keeps(self):
        self.put([rec(MAIN), rec(STALE)])
        self.session_file(STALE_PID, STALE)
        p = self.plan(alive=alive_set(STALE_PID))
        self.assertEqual(p["retire"], [])

    def test_session_file_pid_dead_proposes(self):
        self.put([rec(MAIN), rec(STALE)])
        self.session_file(STALE_PID, STALE)
        p = self.plan(alive=alive_set())
        self.assertEqual(p["retire"][0]["pids"], [STALE_PID])

    def test_no_pid_evidence_is_no_process_found(self):
        self.put([rec(MAIN), rec(STALE)])
        p = self.plan()
        self.assertIn("no process found", p["retire"][0]["reason"])

    def test_any_live_pid_among_several_keeps(self):
        self.put([rec(MAIN), rec(STALE, pid=DEAD_PID)])
        self.session_file(STALE_PID, STALE)
        self.assertEqual(self.plan(alive=alive_set(STALE_PID))["retire"], [])

    def test_probe_exception_is_treated_as_alive(self):
        self.put([rec(MAIN), rec(STALE, pid=DEAD_PID)])

        def boom(pid):
            raise OSError("probe failed")
        self.assertEqual(self.plan(alive=boom)["retire"], [])

    def test_non_candidates_ignored(self):
        self.put([rec(MAIN), rec(OTHER, authoritative=False), rec(STALE, status="RETIRED"),
                  rec("44444444-4444-4444-8444-444444444444", role="worker"),
                  rec("55555555-5555-4555-8555-555555555555", status="FAILED")])
        p = self.plan()
        self.assertEqual(p["retire"], [])
        self.assertEqual(p["authoritative"], [MAIN])

    def test_retiring_status_is_candidate(self):
        self.put([rec(MAIN), rec(STALE, status="RETIRING")])
        self.assertEqual([r["session_id"] for r in self.plan()["retire"]], [STALE])

    def test_main_not_running_warns_and_flags_all_retired(self):
        self.put([rec(STALE)], main_status="STOPPED")
        p = self.plan()
        self.assertTrue(any("RUNNING" in w for w in p["warnings"]))
        self.assertTrue(any("every authoritative" in w for w in p["warnings"]))

    def test_missing_main_json_warns(self):
        self.put([rec(STALE)], main=None)
        self.assertTrue(any("RUNNING" in w for w in self.plan()["warnings"]))

    def test_missing_directory_and_document(self):
        p = repair.plan(Path(self.tmp.name) / "nope")
        self.assertEqual(p["retire"], [])
        self.assertFalse(Path(self.tmp.name, "nope").exists())
        p2 = self.plan()
        self.assertIn("no sessions document", p2["warnings"])

    def test_corrupt_sessions_document_refused(self):
        self.put([rec(MAIN), rec(STALE)])
        self.store.path("sessions").write_text("{ not json", encoding="utf-8")
        before = self.listing()
        with self.assertRaises(repair.RepairError):
            self.plan()
        with self.assertRaises(repair.RepairError):
            repair.apply(self.dir, pid_alive=alive_set())
        self.assertEqual(self.listing(), before)

    def test_checksum_mismatch_refused(self):
        self.put([rec(MAIN), rec(STALE)])
        p = self.store.path("sessions")
        env = json.loads(p.read_text(encoding="utf-8"))
        env["data"]["sessions"][STALE]["status"] = "ACTIVE "
        p.write_text(json.dumps(env), encoding="utf-8")
        with self.assertRaises(repair.RepairError):
            self.plan()

    def test_dry_run_writes_nothing(self):
        self.put([rec(MAIN), rec(STALE)])
        before = self.listing()
        self.plan()
        out = io.StringIO()
        self.assertEqual(repair.main(["--state-dir", str(self.dir), "--sessions-dir", str(self.sess_dir)],
                                     pid_alive=alive_set(), out=out), 0)
        self.assertEqual(self.listing(), before)
        self.assertIn("RETIRE 22222222", out.getvalue())
        self.assertIn("KEEP   11111111", out.getvalue())

    def test_dry_run_leaves_stale_temp_files_alone(self):
        self.put([rec(MAIN), rec(STALE)])
        tmpf = self.dir / ".sessions.json.1.abcd.tmp"
        tmpf.write_bytes(b"x")
        self.plan()
        self.assertTrue(tmpf.exists())


class ApplyTests(Base):
    def apply(self, **kw):
        kw.setdefault("pid_alive", alive_set(LIVE_PID))
        kw.setdefault("clock", lambda: T0 + 60)
        return repair.apply(self.dir, sessions_dir=self.sess_dir, **kw)

    def test_apply_retires_stale_only(self):
        self.put([rec(MAIN, handoff_id="h1"), rec(STALE)])
        res = self.apply()
        self.assertEqual(res["retired"], [STALE])
        d = self.doc()
        self.assertFalse(d[STALE]["authoritative"])
        self.assertEqual(d[STALE]["status"], "RETIRED_STALE")
        self.assertIn("no process found", d[STALE]["retired_stale"]["reason"])
        self.assertEqual(d[STALE]["retired_stale"]["previous_status"], "ACTIVE")
        self.assertTrue(d[STALE]["retired_stale"]["at"].endswith("+00:00"))
        self.assertTrue(d[MAIN]["authoritative"])
        self.assertEqual(d[MAIN]["status"], "ACTIVE")
        self.assertEqual(d[MAIN]["handoff_id"], "h1")

    def test_records_never_deleted_and_failures_kept(self):
        self.put([rec(MAIN), rec(STALE), rec(OTHER, status="RETIRED", authoritative=False)],
                 extra={"failures": {OTHER: 2}})
        self.apply()
        self.assertEqual(set(self.doc()), {MAIN, STALE, OTHER})
        self.assertEqual(self.store.read("sessions")["failures"], {OTHER: 2})

    def test_backup_kept_with_previous_content(self):
        self.put([rec(MAIN), rec(STALE)])
        self.apply()
        bak = json.loads(self.store.backup_path("sessions").read_text(encoding="utf-8"))
        self.assertTrue(bak["data"]["sessions"][STALE]["authoritative"])

    def test_main_json_untouched(self):
        self.put([rec(MAIN), rec(STALE)])
        p = self.store.path("main")
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        self.apply()
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)

    def test_event_appended(self):
        self.put([rec(MAIN), rec(STALE)])
        self.apply()
        evs, bad = self.store.read_events()
        self.assertEqual(bad, 0)
        ev = [e for e in evs if e["kind"] == "state_repaired"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["data"]["retired"], [STALE])
        self.assertEqual(ev[0]["data"]["main_session_id"], MAIN)

    def test_idempotent_rerun(self):
        self.put([rec(MAIN), rec(STALE)])
        self.apply()
        after_first = self.store.read("sessions")
        res = self.apply()
        self.assertEqual(res["retired"], [])
        self.assertEqual(self.store.read("sessions"), after_first)
        evs, _ = self.store.read_events()
        self.assertEqual(len([e for e in evs if e["kind"] == "state_repaired"]), 1)

    def test_nothing_to_do_writes_nothing(self):
        self.put([rec(MAIN)])
        before = self.listing()
        self.assertEqual(self.apply()["retired"], [])
        self.assertEqual(self.listing(), before)

    def test_alive_stale_candidate_not_touched(self):
        self.put([rec(MAIN), rec(STALE, pid=LIVE_PID)])
        before = self.store.path("sessions").read_bytes()
        self.assertEqual(self.apply()["retired"], [])
        self.assertEqual(self.store.path("sessions").read_bytes(), before)

    def test_lock_held_by_live_pid_refuses(self):
        self.put([rec(MAIN), rec(STALE)])
        (self.dir / "supervisor.lock").write_text(json.dumps({"pid": LIVE_PID, "owner": "supervisor"}))
        before = self.listing()
        with self.assertRaises(repair.RepairError) as cm:
            self.apply()
        self.assertIn("lock is held", str(cm.exception))
        self.assertEqual(self.listing(), before)

    def test_lock_held_allow_while_running_edits_only_sessions(self):
        self.put([rec(MAIN), rec(STALE)])
        (self.dir / "supervisor.lock").write_text(json.dumps({"pid": LIVE_PID, "owner": "supervisor"}))
        mainp = self.store.path("main")
        before_main = mainp.read_bytes()
        lock_before = (self.dir / "supervisor.lock").read_bytes()
        res = self.apply(allow_while_running=True)
        self.assertEqual(res["retired"], [STALE])
        self.assertEqual(mainp.read_bytes(), before_main)
        self.assertEqual((self.dir / "supervisor.lock").read_bytes(), lock_before)

    def test_stale_lock_with_dead_pid_does_not_block(self):
        self.put([rec(MAIN), rec(STALE)])
        (self.dir / "supervisor.lock").write_text(json.dumps({"pid": DEAD_PID, "owner": "supervisor"}))
        self.assertEqual(self.apply()["retired"], [STALE])

    def test_garbage_lock_file_does_not_block(self):
        self.put([rec(MAIN), rec(STALE)])
        (self.dir / "supervisor.lock").write_text("not json")
        self.assertEqual(self.apply()["retired"], [STALE])

    def test_own_pid_lock_does_not_block(self):
        self.put([rec(MAIN), rec(STALE)])
        (self.dir / "supervisor.lock").write_text(json.dumps({"pid": os.getpid()}))
        self.assertEqual(self.apply(pid_alive=lambda p: True)["retired"], [STALE])

    def test_secret_in_reason_not_stored(self):
        # a credential-shaped string built at runtime in a record field must not survive the rewrite
        fake = "sk-" + "ant-" + "x" * 24
        self.put([rec(MAIN), rec(STALE, note=fake)])
        self.apply()
        self.assertNotIn(fake, self.store.path("sessions").read_text(encoding="utf-8"))


class CliTests(Base):
    def run_cli(self, *argv, alive=alive_set(LIVE_PID)):
        out = io.StringIO()
        code = repair.main(["--state-dir", str(self.dir), "--sessions-dir", str(self.sess_dir), *argv],
                           pid_alive=alive, out=out)
        return code, out.getvalue()

    def test_cli_yes_applies(self):
        self.put([rec(MAIN), rec(STALE)])
        code, text = self.run_cli("--yes")
        self.assertEqual(code, 0)
        self.assertIn("retired: 22222222", text)
        self.assertEqual(self.doc()[STALE]["status"], "RETIRED_STALE")

    def test_cli_lock_refusal_exit_2(self):
        self.put([rec(MAIN), rec(STALE)])
        (self.dir / "supervisor.lock").write_text(json.dumps({"pid": LIVE_PID}))
        code, text = self.run_cli("--yes")
        self.assertEqual(code, 2)
        self.assertIn("repair refused", text)
        self.assertEqual(self.doc()[STALE]["status"], "ACTIVE")

    def test_cli_corrupt_exit_2(self):
        self.put([rec(MAIN), rec(STALE)])
        self.store.path("sessions").write_text("garbage")
        self.assertEqual(self.run_cli()[0], 2)

    def test_cli_json_output_uses_full_ids_and_is_valid(self):
        self.put([rec(MAIN), rec(STALE)])
        code, text = self.run_cli("--json")
        data = json.loads(text)
        self.assertEqual((code, data["applied"], data["plan"]["retire"][0]["session_id"]), (0, False, STALE))

    def test_cli_text_shortens_ids_to_8(self):
        self.put([rec(MAIN), rec(STALE)])
        _, text = self.run_cli()
        self.assertIn("22222222", text)
        self.assertNotIn(STALE, text)

    def test_cli_missing_state_dir_ok(self):
        out = io.StringIO()
        code = repair.main(["--state-dir", str(Path(self.tmp.name) / "none")], pid_alive=alive_set(), out=out)
        self.assertEqual(code, 0)
        self.assertIn("nothing to retire", out.getvalue())


if __name__ == "__main__":
    unittest.main()
