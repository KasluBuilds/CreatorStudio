import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import guard_loop as gl  # noqa: E402
from dev_supervisor import progress_guard as pg  # noqa: E402


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, **data):
        self.events.append((kind, data))

    def kinds(self):
        return [k for k, _ in self.events]


def sig(**kw):
    base = dict(main_alive=True, session_status="idle", eligible_work=2, progress_marker="m0", pressure_pct=10.0,
                handoff_verified=False, session_id=None)
    base.update(kw)
    return gl.Signals(**base)


def adapter(state_dir, clock, handover_reply=None, rec=None):
    rec = rec or Recorder()
    calls = []

    def handover(signals):
        calls.append(signals)
        return handover_reply if handover_reply is not None else {"sent": True, "cid": "c1"}

    a = gl.GuardAdapter(state_dir, clock=clock, request_handover=handover, event=rec)
    return a, rec, calls


class FlagTests(unittest.TestCase):
    def test_flag_is_exactly_one(self):
        self.assertTrue(gl.enabled({gl.ENV_FLAG: "1"}))
        for v in ("0", "", "true", "yes", "11"):
            with self.subTest(v=v):
                self.assertFalse(gl.enabled({gl.ENV_FLAG: v}))
        self.assertFalse(gl.enabled({}))


class TickTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_idle_with_work_escalates_once_and_records_it(self):
        clock = Clock(0.0)
        a, rec, _ = adapter(self.dir, clock)
        out = [a.tick(sig())]
        clock.t = 400.0
        out.append(a.tick(sig()))
        clock.t = 500.0
        out.append(a.tick(sig()))
        self.assertEqual([o["action"] for o in out], [pg.NONE, pg.ESCALATE, pg.NONE])
        self.assertEqual(rec.kinds().count("guard_escalation"), 1)

    def test_replace_without_verified_handoff_is_held_and_never_sent(self):
        clock = Clock(0.0)
        a, rec, calls = adapter(self.dir, clock)
        a.tick(sig())
        clock.t = 10.0
        out = a.tick(sig(main_alive=False, session_status="idle", handoff_verified=False))
        self.assertEqual(out["action"], pg.HOLD)
        self.assertEqual(calls, [])

    def test_replace_with_verified_handoff_is_sent_once_and_pending(self):
        clock = Clock(0.0)
        a, rec, calls = adapter(self.dir, clock)
        a.tick(sig())
        out = a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        self.assertEqual(out["action"], pg.REPLACE)
        self.assertTrue(out["acted"])
        self.assertEqual(len(calls), 1)
        self.assertIn("guard_replace_requested", rec.kinds())

    def test_duplicate_handover_is_refused_while_one_is_pending(self):
        clock = Clock(0.0)
        a, rec, calls = adapter(self.dir, clock)
        a.tick(sig())
        clock.t = 1.0
        a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        clock.t = 10.0
        out = a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        self.assertEqual(len(calls), 1, "a second handover must not be sent while one is pending")
        self.assertFalse(out["acted"])
        self.assertIn("guard_replace_suppressed", rec.kinds())

    def test_failed_send_does_not_spend_the_replacement_budget(self):
        clock = Clock(0.0)
        a, rec, _ = adapter(self.dir, clock, handover_reply={"sent": False, "reason": "host not reachable"})
        a.tick(sig())
        for t in (1.0, 2.0, 3.0, 4.0, 5.0):
            clock.t = t
            a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        state, _ = a.load()
        self.assertEqual(state.replacements, [], "failed sends must not consume the budget")
        self.assertIn("guard_replace_failed", rec.kinds())

    def test_pending_handover_expires_then_a_new_one_can_be_sent(self):
        clock = Clock(0.0)
        a, rec, calls = adapter(self.dir, clock)
        a.tick(sig())
        clock.t = 1.0
        a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        clock.t = gl.HANDOVER_PENDING_S + 2
        a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        self.assertIn("guard_handover_expired", rec.kinds())
        self.assertEqual(len(calls), 2)

    def test_budget_exhaustion_escalates_once(self):
        clock = Clock(0.0)
        a, rec, calls = adapter(self.dir, clock)
        a.tick(sig(session_id="s0"))
        t = 1.0
        for i in range(6):
            clock.t = t
            a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True, session_id="s%d" % i))
            clock.t = t + 5
            a.tick(sig(progress_marker="m%d" % i, session_id="s%d" % (i + 1)))
            t += 60
        self.assertEqual(len(calls), pg.Policy().replace_budget)
        self.assertEqual(rec.kinds().count("guard_escalation"), 1)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_state_survives_a_supervisor_restart(self):
        clock = Clock(0.0)
        a, _, _ = adapter(self.dir, clock)
        a.tick(sig())
        clock.t = 400.0
        a.tick(sig())
        b, rec, _ = adapter(self.dir, Clock(401.0))
        out = b.tick(sig(progress_marker="m0"))
        self.assertEqual(out["label"], "ESCALATED", "the restarted guard must not escalate again")
        self.assertEqual(rec.kinds().count("guard_escalation"), 0)

    def test_pending_handover_survives_a_restart(self):
        a, _, _ = adapter(self.dir, Clock(0.0))
        a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        b, rec, calls = adapter(self.dir, Clock(5.0))
        b.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        self.assertEqual(calls, [], "a restarted supervisor must not duplicate a pending handover")

    def test_malformed_state_file_fails_closed(self):
        (self.dir / gl.STATE_FILE).write_text("{not json", encoding="utf-8")
        a, rec, calls = adapter(self.dir, Clock(0.0))
        out = a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        self.assertEqual(out["action"], pg.ESCALATE)
        self.assertEqual(calls, [])
        out2 = a.tick(sig())
        self.assertEqual(out2["action"], pg.HOLD)

    def test_non_object_state_file_fails_closed(self):
        (self.dir / gl.STATE_FILE).write_text(json.dumps([1, 2]), encoding="utf-8")
        a, _, calls = adapter(self.dir, Clock(0.0))
        out = a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
        self.assertNotEqual(out["action"], pg.REPLACE)
        self.assertEqual(calls, [])

    def test_missing_state_file_starts_fresh(self):
        a, _, _ = adapter(self.dir, Clock(0.0))
        state, pending = a.load()
        self.assertFalse(state.corrupt)
        self.assertIsNone(pending)

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        a, _, _ = adapter(self.dir, Clock(0.0))
        a.tick(sig())
        self.assertTrue((self.dir / gl.STATE_FILE).exists())
        self.assertFalse((self.dir / (gl.STATE_FILE + ".tmp")).exists())
        json.loads((self.dir / gl.STATE_FILE).read_text(encoding="utf-8"))

    def test_missing_signals_produce_no_action(self):
        a, rec, calls = adapter(self.dir, Clock(0.0))
        for s in (sig(main_alive=None), sig(session_status=None), sig(eligible_work=None),
                  sig(progress_marker=None)):
            out = a.tick(s)
            self.assertEqual(out["action"], pg.NONE)
        self.assertEqual(calls, [])


class CliWiringTests(unittest.TestCase):
    """Calls the real SupervisorLoop hook methods on a stub with fake services (no live Main)."""

    def setUp(self):
        from dev_supervisor import cli as cli_mod
        self.cli = cli_mod
        self.dir = Path(tempfile.mkdtemp())
        self.events = []

        class Store:
            directory = self.dir

            def append_event(_s, kind, **data):
                self.events.append((kind, data))

        class Svc:
            clock = staticmethod(lambda: 100.0)
            pid_alive = staticmethod(lambda pid: True)
            session_factory = staticmethod(lambda: "new-session-id")

            def read_sessions(_s, *a, **kw):
                return []

        class Cfg:
            host_command_timeout_s = 30

            def sessions_directory(_s):
                return self.dir

        class Stub:
            pass

        self.stub = Stub()
        self.stub.store = Store()
        self.stub.svc = Svc()
        self.stub.cfg = Cfg()
        self.stub.root = self.dir
        self.stub._handoff_fresh = lambda sid: False

    def test_flag_off_means_the_hook_is_not_called(self):
        with mock.patch.dict(os.environ, {}, clear=False),                 mock.patch.object(self.cli.guard_loop, "GuardAdapter", side_effect=AssertionError("must not build")):
            os.environ.pop(gl.ENV_FLAG, None)
            self.cli.SupervisorLoop._guard_tick(self.stub, {"session_id": "s"}, True, None)
        self.assertEqual(self.events, [])

    def test_guard_failure_is_recorded_and_never_raises(self):
        with mock.patch.dict(os.environ, {gl.ENV_FLAG: "1"}),                 mock.patch.object(self.cli.guard_loop, "GuardAdapter", side_effect=RuntimeError("boom")):
            self.cli.SupervisorLoop._guard_tick(self.stub, {"session_id": "s"}, True, None)
        kinds = [k for k, _ in self.events]
        self.assertIn("guard_error", kinds)

    def test_signals_come_from_the_injected_sources(self):
        picture = {"items": [{"id": "a"}, {"id": "b"}], "fingerprint": "fp1"}
        ev = mock.Mock(available=True, value=True)
        with mock.patch.object(self.cli.autonomy, "compute_items", return_value=picture),                 mock.patch.object(self.cli, "idle_evidence", return_value=ev),                 mock.patch.object(self.cli, "find_session", return_value={"x": 1}):
            sig_ = self.cli.SupervisorLoop._guard_signals(self.stub, {"session_id": "s"}, True)
        self.assertEqual(sig_.main_alive, True)
        self.assertEqual(sig_.session_status, "idle")
        self.assertEqual(sig_.eligible_work, 2)
        self.assertEqual(sig_.progress_marker, "fp1")
        self.assertIsNone(sig_.pressure_pct, "pressure is not wired yet and must say so")
        self.assertFalse(sig_.handoff_verified)

    def test_unobserved_session_is_not_idle(self):
        picture = {"items": [], "fingerprint": "fp"}
        ev = mock.Mock(available=False, value=None)
        with mock.patch.object(self.cli.autonomy, "compute_items", return_value=picture),                 mock.patch.object(self.cli, "idle_evidence", return_value=ev),                 mock.patch.object(self.cli, "find_session", return_value=None):
            sig_ = self.cli.SupervisorLoop._guard_signals(self.stub, {"session_id": "s"}, True)
        self.assertIsNone(sig_.session_status)

    def test_handover_refused_without_a_verified_handoff(self):
        self.stub._guard_sid = "old-sid"
        sig_ = gl.Signals(main_alive=False, session_status="idle", eligible_work=1, progress_marker="m",
                          pressure_pct=None, handoff_verified=False)
        with mock.patch.object(self.cli.main_host, "send_command") as send:
            out = self.cli.SupervisorLoop._guard_request_handover(self.stub, sig_)
        self.assertFalse(out["sent"])
        send.assert_not_called()

    def test_handover_sent_through_the_host_command_when_verified(self):
        self.stub._guard_sid = "old-sid"
        sig_ = gl.Signals(main_alive=False, session_status="idle", eligible_work=1, progress_marker="m",
                          pressure_pct=None, handoff_verified=True)
        with mock.patch.object(self.cli.main_host, "send_command", return_value="cid-9") as send:
            out = self.cli.SupervisorLoop._guard_request_handover(self.stub, sig_)
        self.assertEqual(out, {"sent": True, "cid": "cid-9"})
        args, kwargs = send.call_args
        self.assertEqual(args[1], "handover")
        self.assertEqual(args[2], {"old_session_id": "old-sid", "new_session_id": "new-session-id"})
        self.assertEqual(kwargs.get("supervisor_pid"), os.getpid())


class ReviewRound2Tests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_live_same_session_does_not_clear_a_pending_handover(self):
        clock = Clock(0.0)
        a, rec, calls = adapter(self.dir, clock)
        a.tick(sig(session_id="old"))
        clock.t = 1.0
        a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True, session_id="old"))
        clock.t = 2.0
        a.tick(sig(main_alive=True, session_status="idle", session_id="old"))
        clock.t = 3.0
        a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True, session_id="old"))
        self.assertEqual(len(calls), 1, "the old Main is still the same session: no duplicate handover")

    def test_intent_is_saved_before_the_command_is_sent(self):
        clock = Clock(0.0)
        a, rec, _ = adapter(self.dir, clock)
        a.tick(sig(session_id="s0"))
        seen = {}

        def handover(signals):
            seen["pending_on_disk"] = json.loads((self.dir / gl.STATE_FILE).read_text(encoding="utf-8"))["pending_handover"]
            return {"sent": True, "cid": "c"}

        a.request_handover = handover
        clock.t = 1.0
        a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True, session_id="s0"))
        self.assertIsNotNone(seen.get("pending_on_disk"), "pending intent must be on disk before the send")

    def test_non_finite_pending_start_is_discarded(self):
        (self.dir / gl.STATE_FILE).write_text(
            json.dumps({"guard": pg.GuardState().to_dict(), "pending_handover": {"started": float("nan")}}),
            encoding="utf-8")
        a, _, _ = adapter(self.dir, Clock(0.0))
        _, pending = a.load()
        self.assertIsNone(pending)


if __name__ == "__main__":
    unittest.main()
