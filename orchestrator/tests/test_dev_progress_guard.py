import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import progress_guard as pg  # noqa: E402

P = pg.Policy(idle_min_s=300, replace_pressure_pct=85, replace_budget=3, replace_window_s=3600,
              hold_escalate_s=4 * 3600.0)


def obs(now, **kw):
    base = dict(now=now, main_alive=True, session_status="idle", progress_marker="m0", eligible_work=2,
                pressure_pct=10.0, handoff_verified=False)
    base.update(kw)
    return pg.Observation(**base)


def run(seq, policy=P, state=None):
    st = state or pg.GuardState()
    out = []
    for o in seq:
        st, d = pg.decide(st, o, policy)
        out.append(d)
    return st, out


class ContractTests(unittest.TestCase):
    def test_no_continue_action_exists(self):
        self.assertFalse(hasattr(pg, "CONTINUE"))
        self.assertNotIn("CONTINUE", pg.ACTIONS)

    def test_idle_with_work_escalates_once_not_continues(self):
        _, ds = run([obs(0), obs(400), obs(500), obs(900)])
        actions = [d.action for d in ds]
        self.assertEqual(actions.count(pg.ESCALATE), 1)
        self.assertEqual(actions[0], pg.NONE)
        self.assertEqual(ds[1].action, pg.ESCALATE)
        self.assertEqual(ds[2].label, "ESCALATED")

    def test_escalation_holds_until_progress_lands_then_can_escalate_again(self):
        seq = [obs(0), obs(400), obs(500, progress_marker="m1"), obs(600, progress_marker="m1"),
               obs(1000, progress_marker="m1")]
        _, ds = run(seq)
        self.assertEqual(ds[1].action, pg.ESCALATE)
        self.assertEqual(ds[2].label, "PROGRESSING")
        self.assertEqual(ds[3].action, pg.ESCALATE, "idle with work again after progress escalates once more")
        self.assertEqual(ds[4].label, "ESCALATED")

    def test_busy_never_escalates_or_replaces(self):
        _, ds = run([obs(0, session_status="busy"), obs(1000, session_status="busy", handoff_verified=True)])
        self.assertTrue(all(d.action == pg.NONE for d in ds))

    def test_no_work_no_escalation(self):
        _, ds = run([obs(0, eligible_work=0), obs(1000, eligible_work=0)])
        self.assertTrue(all(d.action == pg.NONE for d in ds))

    def test_missing_signals_produce_no_action(self):
        cases = [
            [obs(0, main_alive=None), obs(1000, main_alive=None)],
            [obs(0, session_status=None), obs(1000, session_status=None)],
            [obs(0, progress_marker=None), obs(1000, progress_marker=None)],
            [obs(0, eligible_work=None), obs(1000, eligible_work=None)],
        ]
        for seq in cases:
            with self.subTest(first=seq[0]):
                _, ds = run(seq)
                self.assertTrue(all(d.action == pg.NONE for d in ds), [d.action for d in ds])

    def test_unobserved_tick_breaks_idle_continuity(self):
        _, ds = run([obs(0), obs(200, eligible_work=None), obs(300)])
        self.assertEqual(ds[-1].action, pg.NONE, "idle time restarts after the unobserved tick")


class ReplacementTests(unittest.TestCase):
    def test_crash_with_verified_handoff_while_idle_replaces(self):
        _, ds = run([obs(0), obs(10, main_alive=False, handoff_verified=True)])
        self.assertEqual(ds[1].action, pg.REPLACE)

    def test_crash_without_verified_handoff_holds(self):
        _, ds = run([obs(0), obs(10, main_alive=False, handoff_verified=False)])
        self.assertEqual(ds[1].action, pg.HOLD)

    def test_crash_while_busy_holds(self):
        _, ds = run([obs(0), obs(10, main_alive=False, handoff_verified=True, session_status="busy")])
        self.assertEqual(ds[1].action, pg.HOLD)

    def test_exhaustion_replaces_only_when_idle_and_verified(self):
        _, busy = run([obs(0, pressure_pct=90, session_status="busy", handoff_verified=True)])
        self.assertEqual(busy[0].action, pg.HOLD)
        _, idle = run([obs(0, pressure_pct=90, session_status="idle", handoff_verified=True)])
        self.assertEqual(idle[0].action, pg.REPLACE)

    def test_replacement_budget_bounds_a_restart_loop_and_escalates_once(self):
        seq = [obs(0)]
        t = 10
        for _ in range(6):
            seq.append(obs(t, main_alive=False, handoff_verified=True))
            seq.append(obs(t + 1, main_alive=True, progress_marker="m%d" % t))
            t += 60
        _, ds = run(seq)
        actions = [d.action for d in ds]
        self.assertEqual(actions.count(pg.REPLACE), P.replace_budget)
        self.assertEqual(actions.count(pg.ESCALATE), 1)

    def test_budget_is_not_reset_by_elapsed_or_jumped_time(self):
        seq = [obs(0)]
        for i, t in enumerate([10, 20, 30]):
            seq.append(obs(t, main_alive=False, handoff_verified=True))
            seq.append(obs(t + 1, main_alive=True, progress_marker="x%d" % i))
        seq.append(obs(10 + 3600 * 24 * 30, main_alive=False, handoff_verified=True))
        _, ds = run(seq)
        self.assertNotEqual(ds[-1].action, pg.REPLACE, "a clock jump must not restore the replacement budget")

    def test_replacement_that_never_resumes_work_escalates(self):
        _, ds = run([obs(0), obs(10, main_alive=False, handoff_verified=True),
                     obs(20, progress_marker="m0"), obs(400, progress_marker="m0")])
        self.assertEqual(ds[1].action, pg.REPLACE)
        self.assertEqual(ds[-1].action, pg.ESCALATE, "idle with work after replacement and no progress escalates")


class HoldAndStateTests(unittest.TestCase):
    def test_long_hold_escalates_once(self):
        p = pg.Policy(hold_escalate_s=100.0)
        seq = [obs(0), obs(10, main_alive=False, handoff_verified=False), obs(60, main_alive=False),
               obs(170, main_alive=False), obs(400, main_alive=False)]
        _, ds = run(seq, policy=p)
        self.assertEqual([d.action for d in ds], [pg.NONE, pg.HOLD, pg.HOLD, pg.ESCALATE, pg.HOLD])

    def test_corrupt_state_fails_closed(self):
        for bad in (None, [], "x", {}, {"was_alive": "yes"}):
            with self.subTest(bad=bad):
                st = pg.GuardState.from_dict(bad)
                self.assertTrue(st.corrupt)
                st1, d1 = pg.decide(st, obs(0), P)
                self.assertEqual(d1.action, pg.ESCALATE)
                _, d2 = pg.decide(st1, obs(1000), P)
                self.assertEqual(d2.action, pg.HOLD)

    def test_truncated_state_is_corrupt(self):
        full = pg.GuardState().to_dict()
        del full["escalated"]
        self.assertTrue(pg.GuardState.from_dict(full).corrupt)

    def test_complete_state_round_trips_through_json(self):
        st, _ = run([obs(0), obs(400)])
        restored = pg.GuardState.from_dict(json.loads(json.dumps(st.to_dict())))
        self.assertFalse(restored.corrupt)
        self.assertEqual(restored.to_dict(), st.to_dict())

    def test_decide_does_not_mutate_input(self):
        st, _ = run([obs(0)])
        before = st.to_dict()
        pg.decide(st, obs(400), P)
        self.assertEqual(st.to_dict(), before)

    def test_backward_clock_restarts_idle_time(self):
        st = pg.GuardState(idle_since=1000.0)
        st, d = pg.decide(st, obs(500), P)
        self.assertEqual(d.action, pg.NONE)
        _, d = pg.decide(st, obs(850), P)
        self.assertEqual(d.action, pg.ESCALATE)

    def test_non_finite_time_refused(self):
        for bad in (float("nan"), float("inf")):
            with self.subTest(now=bad):
                _, d = pg.decide(pg.GuardState(), obs(bad), P)
                self.assertEqual(d.action, pg.NONE)

    def test_non_finite_pressure_never_replaces(self):
        for bad in (float("inf"), float("nan")):
            with self.subTest(pressure=bad):
                _, d = pg.decide(pg.GuardState(), obs(0, pressure_pct=bad, handoff_verified=True), P)
                self.assertNotEqual(d.action, pg.REPLACE)

    def test_truthy_non_boolean_flags_are_not_trusted(self):
        _, d = pg.decide(pg.GuardState(), obs(0, main_alive=0, handoff_verified=1), P)
        self.assertEqual(d.action, pg.NONE)
        st = pg.GuardState(was_alive=True)
        _, d = pg.decide(st, obs(0, main_alive=False, handoff_verified=1), P)
        self.assertNotEqual(d.action, pg.REPLACE)


if __name__ == "__main__":
    unittest.main()
