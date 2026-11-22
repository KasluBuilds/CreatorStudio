"""Tests for the worker dispatch gate (T-DEV-032). Offline: no process, network or credential is touched."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import dispatch_routing as dr  # noqa: E402
from dev_supervisor import routing  # noqa: E402

OPEN = {"verified": True, "env_implemented": True}


class DefaultGateTests(unittest.TestCase):
    def test_default_refuses_with_reason_and_never_names_anthropic_as_provider(self):
        d = dr.decide_worker_dispatch()
        self.assertFalse(d.allowed)
        self.assertIsNone(d.provider)
        self.assertIsNone(d.route)
        self.assertIn("NOT TESTED", d.reason)
        self.assertIn("refusing rather than falling back to Claude Pro", d.reason)

    def test_refusal_lists_claude_routes_as_not_worker_routes(self):
        d = dr.decide_worker_dispatch()
        excluded = dict(d.excluded)
        self.assertIn(routing.CLAUDE_STANDARD, excluded)
        self.assertIn(routing.CLAUDE_STRONG, excluded)
        self.assertIn("never run on Claude Pro", excluded[routing.CLAUDE_STANDARD])

    def test_module_defaults_are_closed(self):
        # The shipped flags stay closed until a live probe verifies them (see OMNIROUTE_BENCHMARK.md).
        self.assertFalse(dr.CLAUDE_CODE_OVER_OMNIROUTE_VERIFIED)
        self.assertFalse(dr.OMNIROUTE_WORKER_ENV_IMPLEMENTED)
        self.assertEqual(dr.MAX_ACTIVE_WORKERS, 1)


class OpenGateTests(unittest.TestCase):
    def test_successful_routing_picks_an_omniroute_route_with_its_model(self):
        d = dr.decide_worker_dispatch(**OPEN)
        self.assertTrue(d.allowed)
        self.assertEqual(d.provider, dr.PROVIDER_OMNIROUTE)
        self.assertIn(d.route, routing.OMNI_ROUTES)
        self.assertEqual(d.model, dict(routing.RoutingConfig().omniroute_models)[d.route])

    def test_strong_task_is_refused_rather_than_sent_to_claude(self):
        # routing.decide excludes OmniRoute for critical work ("Claude only"); the gate must refuse, not fall back.
        profile = routing.TaskProfile(criticality="critical", complexity="architecture")
        d = dr.decide_worker_dispatch(profile, **OPEN)
        self.assertFalse(d.allowed)
        self.assertIsNone(d.route)
        self.assertIsNone(d.provider)
        self.assertIn("no OmniRoute route available", d.reason)
        self.assertIn("Claude only", dict(d.excluded)[routing.OMNI_GEMINI])

    def test_fallback_to_the_other_free_route_when_one_is_unreachable(self):
        cfg = routing.RoutingConfig(reachable_omniroute=(routing.OMNI_GROQ,))
        d = dr.decide_worker_dispatch(config=cfg, **OPEN)
        self.assertTrue(d.allowed)
        self.assertEqual(d.route, routing.OMNI_GROQ)
        self.assertIn(routing.OMNI_GEMINI, dict(d.excluded))

    def test_no_reachable_omniroute_route_refuses(self):
        cfg = routing.RoutingConfig(reachable_omniroute=())
        d = dr.decide_worker_dispatch(config=cfg, **OPEN)
        self.assertFalse(d.allowed)
        self.assertIsNone(d.provider)
        self.assertIn("no OmniRoute route available", d.reason)

    def test_paid_route_is_not_a_fallback_even_when_authorised(self):
        cfg = routing.RoutingConfig(allow_paid_api=True, reachable_omniroute=())
        d = dr.decide_worker_dispatch(config=cfg, **OPEN)
        self.assertFalse(d.allowed)
        self.assertNotEqual(d.route, routing.PAID_API)
        self.assertIn(routing.PAID_API, dict(d.excluded))


class EnvironmentGateTests(unittest.TestCase):
    def test_verified_without_worker_environment_still_refuses(self):
        # Prevents a worker labelled omniroute from running on Claude Pro by accident.
        d = dr.decide_worker_dispatch(verified=True, env_implemented=False)
        self.assertFalse(d.allowed)
        self.assertIn("worker environment is not implemented", d.reason)

    def test_flags_are_read_at_call_time(self):
        with mock.patch.multiple(dr, CLAUDE_CODE_OVER_OMNIROUTE_VERIFIED=True, OMNIROUTE_WORKER_ENV_IMPLEMENTED=True):
            self.assertTrue(dr.decide_worker_dispatch().allowed)
        self.assertFalse(dr.decide_worker_dispatch().allowed)


class ProviderGateTests(unittest.TestCase):
    def test_anthropic_provider_is_never_dispatched_even_with_gate_open(self):
        with mock.patch.multiple(dr, CLAUDE_CODE_OVER_OMNIROUTE_VERIFIED=True, OMNIROUTE_WORKER_ENV_IMPLEMENTED=True):
            d = dr.gate_provider("anthropic")
        self.assertFalse(d.allowed)
        self.assertIn("not a worker provider", d.reason)

    def test_missing_provider_is_refused(self):
        d = dr.gate_provider(None, **OPEN)
        self.assertFalse(d.allowed)

    def test_omniroute_provider_is_checked_by_the_route_decision(self):
        d = dr.gate_provider("omniroute", **OPEN)
        self.assertTrue(d.allowed)
        self.assertEqual(d.provider, dr.PROVIDER_OMNIROUTE)


class ConcurrencyCapTests(unittest.TestCase):
    def test_cap_of_one_refuses_when_one_worker_is_active(self):
        d = dr.decide_worker_dispatch(active_workers=1, **OPEN)
        self.assertFalse(d.allowed)
        self.assertIn("concurrency cap", d.reason)

    def test_no_active_worker_allows_dispatch(self):
        self.assertTrue(dr.decide_worker_dispatch(active_workers=0, **OPEN).allowed)

    def test_gate_provider_applies_the_cap_too(self):
        self.assertFalse(dr.gate_provider("omniroute", active_workers=1, **OPEN).allowed)


class DecisionRecordTests(unittest.TestCase):
    def test_to_dict_records_provider_route_and_reason(self):
        out = dr.decide_worker_dispatch(**OPEN).to_dict()
        self.assertEqual(out["provider"], dr.PROVIDER_OMNIROUTE)
        self.assertTrue(out["route"])
        self.assertTrue(out["reason"])
        self.assertTrue(out["model"])
        self.assertTrue(all(isinstance(e, list) and len(e) == 2 for e in out["excluded"]))


if __name__ == "__main__":
    unittest.main()
