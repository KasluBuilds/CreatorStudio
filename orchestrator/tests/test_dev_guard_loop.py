import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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
                handoff_verified=False)
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
        a.tick(sig())
        t = 1.0
        for i in range(6):
            clock.t = t
            a.tick(sig(main_alive=False, session_status="idle", handoff_verified=True))
            clock.t = t + 5
            a.tick(sig(progress_marker="m%d" % i))
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


if __name__ == "__main__":
    unittest.main()
