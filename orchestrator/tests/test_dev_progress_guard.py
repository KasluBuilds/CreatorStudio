import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import progress_guard as pg  # noqa: E402

P = pg.Policy(idle_min_s=300, cooldown_s=900, non_progress_limit=3, replace_pressure_pct=85,
              replace_budget=3, replace_window_s=3600)


def obs(now, **kw):
    base = dict(now=now, main_alive=True, session_status="idle", progress_marker="m0", eligible_work=2,
                pressure_pct=10.0, handoff_verified=False)
    base.update(kw)
    return pg.Observation(**base)


def run(states_obs):
    st = pg.GuardState()
    out = []
    for o in states_obs:
        st, d = pg.decide(st, o, P)
        out.append(d)
    return st, out


class IdleContinuationTests(unittest.TestCase):
    def test_continues_only_after_minimum_idle_time(self):
        _, ds = run([obs(0), obs(200), obs(300)])
        self.assertEqual([d.action for d in ds], [pg.NONE, pg.NONE, pg.CONTINUE])

    def test_busy_never_triggers_action(self):
        _, ds = run([obs(0, session_status="busy"), obs(1000, session_status="busy")])
        self.assertTrue(all(d.action == pg.NONE and d.label == "BUSY" for d in ds))

    def test_no_eligible_work_means_no_continuation(self):
        _, ds = run([obs(0, eligible_work=0), obs(1000, eligible_work=0)])
        self.assertTrue(all(d.action == pg.NONE for d in ds))

    def test_missing_observations_produce_no_action(self):
        _, ds = run([obs(0, main_alive=None), obs(1000, session_status=None),
                     obs(1001, session_status="idle", eligible_work=None), obs(2000, progress_marker=None)])
        self.assertTrue(all(d.action == pg.NONE for d in ds), [d.action for d in ds])

    def test_unrecognised_status_is_not_idle(self):
        _, ds = run([obs(0, session_status="weird"), obs(1000, session_status="weird")])
        self.assertTrue(all(d.action == pg.NONE for d in ds))


class DuplicatePreventionTests(unittest.TestCase):
    def test_no_second_continuation_inside_cooldown(self):
        _, ds = run([obs(0), obs(300), obs(301), obs(302)])
        self.assertEqual(ds[2].action, pg.HOLD)  # the CONTINUE at 300 blocks 301 and 302
        self.assertEqual([d.action for d in ds].count(pg.CONTINUE), 1)

    def test_next_continuation_allowed_after_cooldown(self):
        _, ds = run([obs(0), obs(300), obs(1300)])
        self.assertEqual(ds[1].action, pg.CONTINUE)
        self.assertEqual(ds[2].action, pg.CONTINUE)


class NonProgressTests(unittest.TestCase):
    def test_escalates_after_limit_without_marker_change(self):
        seq = [obs(0)] + [obs(300 + i * 1000) for i in range(5)]
        _, ds = run(seq)
        actions = [d.action for d in ds if d.action != pg.NONE]
        self.assertEqual(actions, [pg.CONTINUE, pg.CONTINUE, pg.CONTINUE, pg.ESCALATE])

    def test_escalation_is_emitted_once(self):
        seq = [obs(0)] + [obs(300 + i * 1000) for i in range(8)]
        _, ds = run(seq)
        self.assertEqual([d.action for d in ds].count(pg.ESCALATE), 1)

    def test_progress_resets_counter_and_escalation(self):
        seq = [obs(0), obs(300), obs(1300), obs(2300),
               obs(3300, progress_marker="m1"), obs(4600, progress_marker="m1")]
        st, ds = run(seq)
        self.assertEqual(ds[-1].action, pg.CONTINUE)
        self.assertFalse(st.escalated)

    def test_escalated_state_waits_for_progress(self):
        seq = [obs(0), obs(300), obs(1300), obs(2300), obs(3300), obs(4300)]
        _, ds = run(seq)
        self.assertEqual(ds[4].action, pg.ESCALATE)
        self.assertEqual(ds[5].action, pg.NONE)
        self.assertEqual(ds[5].label, "ESCALATED")


class CrashAndExhaustionTests(unittest.TestCase):
    def test_crash_without_verified_handoff_is_held_not_restarted(self):
        _, ds = run([obs(0), obs(10, main_alive=False)])
        self.assertEqual(ds[1].action, pg.HOLD)
        self.assertEqual(ds[1].label, "CRASHED")

    def test_crash_with_verified_handoff_while_idle_replaces(self):
        _, ds = run([obs(0), obs(10, main_alive=False, handoff_verified=True, session_status="idle")])
        self.assertEqual(ds[1].action, pg.REPLACE)

    def test_crash_while_session_busy_is_held(self):
        _, ds = run([obs(0), obs(10, main_alive=False, handoff_verified=True, session_status="busy")])
        self.assertEqual(ds[1].action, pg.HOLD)

    def test_never_started_is_not_running(self):
        _, ds = run([obs(0, main_alive=False)])
        self.assertEqual(ds[0].label, "NOT_RUNNING")
        self.assertEqual(ds[0].action, pg.NONE)

    def test_exhaustion_replaces_only_when_idle_and_verified(self):
        _, busy = run([obs(0, pressure_pct=90, session_status="busy", handoff_verified=True)])
        self.assertEqual(busy[0].action, pg.HOLD)
        _, idle = run([obs(0, pressure_pct=90, session_status="idle", handoff_verified=True)])
        self.assertEqual(idle[0].action, pg.REPLACE)
        self.assertEqual(idle[0].label, "EXHAUSTED")


class RestartLoopTests(unittest.TestCase):
    def test_replacement_budget_stops_a_restart_loop(self):
        seq = [obs(0)]
        t = 10
        for _ in range(6):
            seq.append(obs(t, main_alive=False, handoff_verified=True, session_status="idle"))
            seq.append(obs(t + 1, main_alive=True, session_status="idle", progress_marker="m%d" % t))
            t += 60
        _, ds = run(seq)
        replaces = [d for d in ds if d.action == pg.REPLACE]
        escalations = [d for d in ds if d.action == pg.ESCALATE]
        self.assertEqual(len(replaces), P.replace_budget)
        self.assertEqual(len(escalations), 1)

    def test_budget_recovers_after_window(self):
        seq = [obs(0)]
        for i, t in enumerate([10, 20, 30]):
            seq.append(obs(t, main_alive=False, handoff_verified=True, session_status="idle"))
            seq.append(obs(t + 1, main_alive=True, session_status="idle", progress_marker="x%d" % i))
        seq.append(obs(10 + 3600 + 100, main_alive=False, handoff_verified=True, session_status="idle"))
        _, ds = run(seq)
        self.assertEqual(ds[-1].action, pg.REPLACE)


class StatePersistenceTests(unittest.TestCase):
    def test_state_survives_json_round_trip_and_keeps_counters(self):
        st, _ = run([obs(0), obs(300), obs(1300)])
        restored = pg.GuardState.from_dict(json.loads(json.dumps(st.to_dict())))
        self.assertEqual(restored.to_dict(), st.to_dict())
        nxt, d = pg.decide(restored, obs(2300), P)
        self.assertEqual(d.action, pg.CONTINUE)
        self.assertEqual(nxt.continuations_without_progress, 3)

    def test_malformed_state_is_marked_corrupt_and_fails_closed(self):
        for bad in (None, [], "x", {}, {"continuations_without_progress": -1}, {"replacements": "no"},
                    {"was_alive": "yes"}, {"was_alive": True}):
            with self.subTest(bad=bad):
                st = pg.GuardState.from_dict(bad)
                self.assertTrue(st.corrupt)
                st1, d1 = pg.decide(st, obs(0), P)
                self.assertEqual(d1.action, pg.ESCALATE)
                _, d2 = pg.decide(st1, obs(1000), P)
                self.assertEqual(d2.action, pg.HOLD)

    def test_decide_does_not_mutate_input_state(self):
        st, _ = run([obs(0)])
        before = st.to_dict()
        pg.decide(st, obs(400), P)
        self.assertEqual(st.to_dict(), before)

    def test_recovery_after_supervisor_restart_does_not_repeat_continuation(self):
        st, _ = run([obs(0), obs(300)])
        restored = pg.GuardState.from_dict(json.loads(json.dumps(st.to_dict())))
        _, d = pg.decide(restored, obs(301), P)
        self.assertEqual(d.action, pg.HOLD)


class ReviewFixTests(unittest.TestCase):
    def test_missing_observation_breaks_idle_continuity(self):
        seq = [obs(0), obs(200), obs(250, eligible_work=None), obs(260)]
        _, ds = run(seq)
        self.assertEqual(ds[-1].action, pg.NONE, "idle time restarts after an unobserved tick")

    def test_held_crash_is_replaced_once_a_verified_handoff_exists(self):
        seq = [obs(0), obs(10, main_alive=False, handoff_verified=False, session_status="idle"),
               obs(20, main_alive=False, handoff_verified=True, session_status="idle")]
        _, ds = run(seq)
        self.assertEqual(ds[1].action, pg.HOLD)
        self.assertEqual(ds[2].action, pg.REPLACE)

    def test_repeated_death_after_replacement_is_bounded_by_budget(self):
        seq = [obs(0)]
        t = 10
        for _ in range(6):
            seq.append(obs(t, main_alive=False, handoff_verified=True, session_status="idle"))
            t += 10
        _, ds = run(seq)
        self.assertEqual([d.action for d in ds].count(pg.REPLACE), P.replace_budget)
        self.assertEqual([d.action for d in ds].count(pg.ESCALATE), 1)

    def test_bools_and_non_finite_numbers_are_refused_in_restore(self):
        st = pg.GuardState.from_dict({"replacements": [True, float("inf")], "last_continue_at": float("nan"),
                                      "escalated": "yes"})
        self.assertEqual(st.replacements, [])
        self.assertIsNone(st.last_continue_at)
        self.assertFalse(st.escalated)


class SecondReviewFixTests(unittest.TestCase):
    def test_busy_breaks_idle_continuity_even_with_pressure_reading(self):
        seq = [obs(0), obs(100, session_status="busy", pressure_pct=10.0),
               obs(110, pressure_pct=10.0), obs(305, pressure_pct=10.0)]
        _, ds = run(seq)
        self.assertEqual(ds[-1].action, pg.NONE, "only 195s of observed idle since the busy tick")

    def test_unobserved_liveness_breaks_idle_continuity(self):
        seq = [obs(0), obs(200, main_alive=None), obs(300)]
        _, ds = run(seq)
        self.assertEqual(ds[-1].action, pg.NONE)

    def test_non_finite_time_refused(self):
        for bad in (float("nan"), float("inf")):
            with self.subTest(now=bad):
                st = pg.GuardState()
                _, d = pg.decide(st, obs(bad), P)
                self.assertEqual(d.action, pg.NONE)

    def test_long_hold_escalates_once(self):
        p = pg.Policy(hold_escalate_s=100.0)
        st = pg.GuardState()
        seq = [obs(0), obs(10, main_alive=False, handoff_verified=False, session_status="idle"),
               obs(60, main_alive=False, handoff_verified=False, session_status="idle"),
               obs(170, main_alive=False, handoff_verified=False, session_status="idle"),
               obs(400, main_alive=False, handoff_verified=False, session_status="idle")]
        actions = []
        for o in seq:
            st, d = pg.decide(st, o, p)
            actions.append(d.action)
        self.assertEqual(actions, [pg.NONE, pg.HOLD, pg.HOLD, pg.ESCALATE, pg.HOLD])


class ThirdReviewFixTests(unittest.TestCase):
    def test_non_finite_pressure_never_replaces(self):
        for bad in (float("inf"), float("nan")):
            with self.subTest(pressure=bad):
                _, d = pg.decide(pg.GuardState(), obs(0, pressure_pct=bad, session_status="idle",
                                                      handoff_verified=True), P)
                self.assertNotEqual(d.action, pg.REPLACE)

    def test_infinite_eligible_work_never_continues(self):
        seq = [obs(0, eligible_work=float("inf")), obs(1000, eligible_work=float("inf"))]
        _, ds = run(seq)
        self.assertTrue(all(d.action == pg.NONE for d in ds))

    def test_truthy_non_boolean_liveness_is_not_dead(self):
        _, d = pg.decide(pg.GuardState(), obs(0, main_alive=0, session_status="idle", handoff_verified=True), P)
        self.assertNotEqual(d.action, pg.REPLACE)
        self.assertEqual(d.action, pg.NONE)

    def test_truthy_non_boolean_handoff_is_not_verified(self):
        st = pg.GuardState(was_alive=True)
        _, d = pg.decide(st, obs(0, main_alive=False, session_status="idle", handoff_verified=1), P)
        self.assertNotEqual(d.action, pg.REPLACE)

    def test_hold_escalation_not_suppressed_by_earlier_non_progress_escalation(self):
        p = pg.Policy(hold_escalate_s=100.0)
        st = pg.GuardState(escalated=True, was_alive=True)
        st, _ = pg.decide(st, obs(0, main_alive=False, session_status="idle", handoff_verified=False), p)
        st, d3 = pg.decide(st, obs(200, main_alive=False, session_status="idle", handoff_verified=False), p)
        self.assertEqual(d3.action, pg.ESCALATE)

    def test_a_later_separate_hold_escalates_again(self):
        p = pg.Policy(hold_escalate_s=100.0)
        st = pg.GuardState(was_alive=True)
        actions = []
        for now, alive in ((0, False), (200, False), (210, True), (300, False), (500, False)):
            st, d = pg.decide(st, obs(now, main_alive=alive, session_status="idle", handoff_verified=False), p)
            actions.append(d.action)
        self.assertEqual(actions.count(pg.ESCALATE), 2, actions)

    def test_mixed_corrupt_state_is_not_a_partial_restore(self):
        st = pg.GuardState.from_dict({"escalated": True, "continuations_without_progress": "x", "was_alive": True})
        self.assertTrue(st.corrupt)
        self.assertFalse(st.was_alive)

    def test_backward_clock_step_does_not_freeze_idle_time(self):
        st = pg.GuardState(idle_since=1000.0)
        st, d = pg.decide(st, obs(500), P)
        self.assertEqual(d.action, pg.NONE)
        st, d = pg.decide(st, obs(900), P)
        self.assertEqual(d.action, pg.CONTINUE, "idle time restarted at 500, so 400s has passed")


class SixthReviewFixTests(unittest.TestCase):
    def test_truncated_state_is_corrupt_not_a_silent_reset(self):
        full = pg.GuardState(continuations_without_progress=3).to_dict()
        del full["continuations_without_progress"]
        st = pg.GuardState.from_dict(full)
        self.assertTrue(st.corrupt)

    def test_complete_state_round_trips_without_corruption(self):
        st = pg.GuardState(continuations_without_progress=2, last_marker="m")
        self.assertFalse(pg.GuardState.from_dict(st.to_dict()).corrupt)


if __name__ == "__main__":
    unittest.main()
