"""The desktop dashboard (dashboard/desktop) must agree with the canonical Python governance code on the CURRENT records.

It runs the app's own JavaScript model under Node and compares it with governance.status.calculate and
governance.agents. Skipped when Node is not installed. The app's own unit tests (node --test) run in CI separately."""
import json
import shutil
import subprocess
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from governance import agents, status

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "dashboard" / "desktop"
NODE = shutil.which("node")

SCRIPT = r"""
const { repoSnapshot } = require('./tests/helpers');
const { buildModel } = require('./src/model');
const m = buildModel(repoSnapshot(), { now: () => new Date('2026-11-17T12:00:00Z') });
process.stdout.write(JSON.stringify({
  ok: m.ok, problems: m.problems, global: m.global,
  milestones: m.milestones.map((x) => ({ id: x.id, agent: x.agent, implCredit: x.implCredit, verCredit: x.verCredit, state: x.state, order: x.order, dependsOn: x.dependsOn })),
  phases: m.phases.map((p) => ({ name: p.name, impl: p.stats.implementation, ver: p.stats.verification })),
  agents: m.agents.map((a) => ({ id: a.id, milestones: a.milestones })),
  gate: m.gates.map((g) => ({ id: g.id, display: g.display, fired: g.fired, inconsistent: g.inconsistent })),
}));
"""


@unittest.skipUnless(NODE and APP.is_dir(), "Node.js is not installed")
class DashboardAgreesWithGovernance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run([NODE, "-e", SCRIPT], cwd=str(APP), capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        cls.js = json.loads(r.stdout)
        cls.status = json.loads((ROOT / "PROJECT_STATUS.json").read_text(encoding="utf-8"))
        cls.tasks = json.loads((ROOT / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
        cls.registry = json.loads((ROOT / "AGENT_REGISTRY.json").read_text(encoding="utf-8"))
        cls.py = status.calculate(cls.status)

    def test_the_model_builds_from_the_current_records(self):
        self.assertTrue(self.js["ok"], self.js["problems"])

    def test_global_figures_are_the_canonical_ones_and_the_recalculation_agrees(self):
        g = self.js["global"]
        self.assertEqual(g["implementation"], self.status["computed"]["implementation_percent"])
        self.assertEqual(g["verification"], self.status["computed"]["verification_percent"])
        self.assertEqual(g["recomputedImplementation"], self.py["implementation_percent"])
        self.assertEqual(g["recomputedVerification"], self.py["verification_percent"])
        self.assertTrue(g["consistent"])

    def test_every_milestone_credit_equals_the_python_credit(self):
        py = {r["id"]: r for r in self.py["milestones"]}
        self.assertEqual(set(py), {m["id"] for m in self.js["milestones"]})
        for m in self.js["milestones"]:
            self.assertEqual(Fraction(m["implCredit"], 20), Fraction(py[m["id"]]["implementation_credit"]), m["id"])
            self.assertEqual(Fraction(m["verCredit"], 20), Fraction(py[m["id"]]["verification_credit"]), m["id"])

    def test_phase_figures_equal_the_python_phase_figures(self):
        for p in self.js["phases"]:
            ref = self.py["by_phase"][p["name"]]
            self.assertEqual(p["impl"], ref["implementation_percent"], p["name"])
            self.assertEqual(p["ver"], ref["verification_percent"], p["name"])

    def test_agent_assignment_equals_the_python_registry_logic(self):
        for m in self.js["milestones"]:
            self.assertEqual(m["agent"], agents.agent_for_milestone(self.registry, m["id"]), m["id"])
        self.assertEqual([a["id"] for a in self.js["agents"]], [a["id"] for a in agents.active_agents(self.registry)])

    def test_roadmap_order_respects_dependencies(self):
        pos = {m["id"]: m["order"] for m in self.js["milestones"]}
        for m in self.js["milestones"]:
            for d in m["dependsOn"]:
                self.assertLess(pos[d], pos[m["id"]], f"{d} must precede {m['id']}")

    def test_the_gate_state_equals_the_python_trigger_rule(self):
        ids = {m["id"]: m for m in self.status["milestones"]}
        for g in self.status["user_action_gates"]:
            fired = status.gate_triggered(g, ids)
            js = next(x for x in self.js["gate"] if x["id"] == g["id"])
            self.assertEqual(js["fired"], fired)
            self.assertFalse(js["inconsistent"], "the canonical validator already rejects an inconsistent gate")
            if g["status"] == "PENDING":
                self.assertEqual(js["display"], "pending")
            elif g["status"] == "TRIGGERED":
                self.assertEqual(js["display"], "ready")


if __name__ == "__main__":
    unittest.main()
