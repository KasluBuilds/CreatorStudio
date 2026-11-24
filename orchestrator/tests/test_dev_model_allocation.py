"""Model allocation tests (T-DEV-038). Synthetic registries only; no process, no network, no credentials.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_model_allocation.py -v
"""
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import model_allocation as ma
from dev_supervisor import worker_spawner as ws
from dev_supervisor.model_allocation import Allocation, AllocationRefused, allocate


def route(model="auto/a", provider="gemini", roles=("implementation",), capability=2, evidence="test:synthetic",
          cost_zero=True):
    return ws.VerifiedRoute(model, provider, evidence, cost_zero, tuple(roles), capability)


ALL_ROLES = ma.ROLES


class TestAllocationChoice(unittest.TestCase):
    def test_the_cheapest_sufficient_route_is_chosen_for_general_roles(self):
        routes = [route("auto/strong", capability=5, roles=ALL_ROLES), route("auto/mid", capability=3, roles=ALL_ROLES),
                  route("auto/weak", capability=1, roles=ALL_ROLES), route("auto/ok", capability=2, roles=ALL_ROLES)]
        for role in ("implementation", "research", "test_writing"):
            with self.subTest(role=role):
                self.assertEqual(allocate(role, routes).model, "auto/ok")      # 1 is below the minimum of 2

    def test_architecture_takes_the_highest_capability_and_needs_at_least_four(self):
        routes = [route("auto/a", capability=3, roles=ALL_ROLES), route("auto/b", capability=5, roles=ALL_ROLES),
                  route("auto/c", capability=4, roles=ALL_ROLES)]
        self.assertEqual(allocate("architecture", routes).model, "auto/b")
        with self.assertRaises(AllocationRefused):
            allocate("architecture", [route("auto/a", capability=3, roles=ALL_ROLES)])

    def test_review_is_at_least_as_capable_as_the_author(self):
        routes = [route("auto/r3", capability=3, roles=("review",)), route("auto/r4", capability=4, roles=("review",)),
                  route("auto/r5", capability=5, roles=("review",))]
        self.assertEqual(allocate("review", routes).model, "auto/r3")                       # minimum 3
        self.assertEqual(allocate("review", routes, author_capability=4).model, "auto/r4")
        self.assertEqual(allocate("review", routes, author_capability=5).model, "auto/r5")
        with self.assertRaises(AllocationRefused):
            allocate("review", routes[:2], author_capability=5)                           # nothing as capable as the work

    def test_review_prefers_a_different_route_than_the_author_at_equal_capability(self):
        a = route("auto/a", capability=3, roles=("review",))
        b = route("auto/b", capability=3, roles=("review",))
        self.assertEqual(allocate("review", [a, b], author_route=("gemini", "auto/a")).model, "auto/b")
        self.assertEqual(allocate("review", [a, b], author_route=("gemini", "auto/b")).model, "auto/a")
        only = allocate("review", [a], author_route=("gemini", "auto/a"))
        self.assertEqual(only.model, "auto/a")
        self.assertIn("author's own route is the only fit", only.reason)

    def test_the_choice_is_deterministic_whatever_the_registry_order(self):
        routes = [route("auto/b", "groq", capability=2, roles=ALL_ROLES), route("auto/a", "gemini", capability=2, roles=ALL_ROLES),
                  route("auto/c", "gemini", capability=2, roles=ALL_ROLES)]
        results = {(allocate("implementation", list(p)).provider, allocate("implementation", list(p)).model)
                   for p in itertools.permutations(routes)}
        self.assertEqual(results, {("gemini", "auto/a")})

    def test_there_is_never_a_fallback_to_a_weaker_or_other_route(self):
        routes = [route("auto/a", capability=1, roles=ALL_ROLES), route("auto/b", capability=3, roles=("research",))]
        for role in ("implementation", "test_writing", "review", "architecture"):
            with self.subTest(role=role), self.assertRaises(AllocationRefused):
                allocate(role, routes)
        self.assertEqual(allocate("research", routes).model, "auto/b")

    def test_routes_without_recorded_roles_or_capability_are_never_chosen_by_role(self):
        for r in (route(roles=(), capability=3), route(roles=("implementation",), capability=0),
                  route(roles=("research",), capability=5)):
            with self.subTest(route=r), self.assertRaises(AllocationRefused):
                allocate("implementation", [r])

    def test_unusable_routes_are_never_chosen(self):
        bad = [route("gemini/claude-x", roles=ALL_ROLES, capability=5), route("auto/sonnet-ish", roles=ALL_ROLES, capability=5),
               route("auto/z", "anthropic", roles=ALL_ROLES, capability=5), route("auto/z", "openai", roles=ALL_ROLES, capability=5),
               route("auto/p", roles=ALL_ROLES, capability=5, cost_zero=False),
               route("auto/q", roles=ALL_ROLES, capability=5, evidence=""), route("auto/q", roles=ALL_ROLES, capability=5, evidence="  "),
               route("auto/k", roles=ALL_ROLES, capability=5, evidence="key sk-abcdefghijklmnopqrstuvwxyz"),
               route("auto/g", roles=ALL_ROLES, capability=5, evidence="token ghp_abcdefghijklmnopqrstuv"),
               "not a route", None, {"model": "auto/a"}, 5]
        for r in bad:
            with self.subTest(route=r), self.assertRaises(AllocationRefused):
                allocate("implementation", [r])
        good = route("auto/good", roles=ALL_ROLES, capability=2)
        self.assertEqual(allocate("implementation", bad + [good]).model, "auto/good")

    def test_unknown_roles_and_bad_author_capability_are_refused(self):
        r = route(roles=ALL_ROLES, capability=5)
        for role in ("", "dev", "Implementation", None, 5):
            with self.subTest(role=role), self.assertRaises(AllocationRefused):
                allocate(role, [r])
        for cap in (-1, True, "3", 2.5, None):
            with self.subTest(author_capability=cap), self.assertRaises(AllocationRefused):
                allocate("review", [r], author_capability=cap)

    def test_the_allocation_record_has_six_fields_and_no_credential(self):
        a = allocate("implementation", [route(evidence="T-DEV-038 live probe 2026-12-01", roles=ALL_ROLES)])
        self.assertIsInstance(a, Allocation)
        d = a.to_dict()
        self.assertEqual(sorted(d), ["capability", "evidence", "model", "provider", "reason", "role"])
        self.assertEqual((d["role"], d["provider"], d["model"], d["capability"]), ("implementation", "gemini", "auto/a", 2))
        self.assertIn("lowest capability that fits", d["reason"])
        json.dumps(d)

    def test_long_evidence_is_truncated(self):
        a = allocate("implementation", [route(evidence="e" * 1000, roles=ALL_ROLES)])
        self.assertEqual(len(a.evidence), ma.MAX_EVIDENCE)

    def test_the_minimums_match_the_documented_policy(self):
        self.assertEqual(ma.MIN_CAPABILITY, {"implementation": 2, "research": 2, "test_writing": 2, "review": 3,
                                             "architecture": 4})
        self.assertEqual(ma.ROLES, ("implementation", "research", "test_writing", "review", "architecture"))


class TestRegistryFields(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "verified_routes.json"

    def write(self, entries):
        self.path.write_text(json.dumps({"routes": entries}), encoding="utf-8")
        return ws.load_verified_routes(self.path)

    def entry(self, **over):
        e = {"model": "auto/a", "provider": "gemini", "evidence": "test:x", "cost_zero": True}
        e.update(over)
        return e

    def test_roles_and_capability_are_parsed_and_normalised(self):
        (r,) = self.write([self.entry(roles=["Review", " research ", "review", ""], capability=4)])
        self.assertEqual(r.roles, ("research", "review"))
        self.assertEqual(r.capability, 4)

    def test_wrong_typed_or_out_of_range_values_mean_not_recorded(self):
        for bad_cap in (True, False, 4.0, "4", None, 0, 6, -1, [4]):
            with self.subTest(capability=bad_cap):
                (r,) = self.write([self.entry(capability=bad_cap)])
                self.assertEqual(r.capability, 0)
        for bad_roles in ("review", 5, None, {"review": 1}, [1, None, ""]):
            with self.subTest(roles=bad_roles):
                (r,) = self.write([self.entry(roles=bad_roles)])
                self.assertEqual(r.roles, ())

    def test_a_legacy_entry_still_loads_for_explicit_dispatch_but_is_never_allocated(self):
        (r,) = self.write([self.entry()])
        self.assertEqual((r.model, r.provider, r.cost_zero, r.roles, r.capability), ("auto/a", "gemini", True, (), 0))
        with self.assertRaises(AllocationRefused):
            allocate("implementation", [r])

    def test_allocate_from_registry_reads_the_file_and_refuses_a_missing_or_broken_one(self):
        self.write([self.entry(roles=["implementation"], capability=2)])
        self.assertEqual(ma.allocate_from_registry(self.path, "implementation").model, "auto/a")
        with self.assertRaises(AllocationRefused) as cm:
            ma.allocate_from_registry(self.path.with_name("missing.json"), "implementation")
        self.assertIn("registry unavailable", str(cm.exception))
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(AllocationRefused):
            ma.allocate_from_registry(self.path, "implementation")

    def test_a_non_zero_cost_entry_is_not_loaded_even_with_roles(self):
        self.assertEqual(self.write([self.entry(cost_zero=False, roles=["implementation"], capability=5)]), [])


if __name__ == "__main__":
    unittest.main()
