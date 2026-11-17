"""AGENT_REGISTRY.json: validation rules, helpers, and the real file against the real canonical records."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from governance import agents, waves

ROOT = Path(__file__).resolve().parents[2]
STATUS = {"milestones": [{"id": f"M-{p}-0{n}"} for p in ("AAA", "BBB", "CCC") for n in (1, 2)]}
TASKS = {"package_owners": {"alpha": {}, "beta": {}}}


def agent(aid="alpha", **kw):
    d = {"id": aid, "name": aid.title(), "role": "does things", "status": "active", "registered": "2026-11-17",
         "streams": [f"claude:{aid}-stream"], "package_keys": ["alpha"], "milestones": ["M-AAA-01"], "docs": []}
    d.update(kw)
    return d


def registry(*ags):
    return {"schema_version": 1, "authority": "x", "agents": list(ags) or [agent()]}


class ValidationTests(unittest.TestCase):
    def check(self, doc, text=None):
        ps = agents.validate(doc, STATUS, TASKS)
        if text is None:
            self.assertEqual(ps, [])
        else:
            self.assertTrue(any(text in p for p in ps), f"{text!r} not in {ps}")

    def test_valid(self):
        self.check(registry(agent(), agent("beta", streams=["claude:b"], milestones=["M-BBB-01"], package_keys=["beta"])))

    def test_garbage_never_raises(self):
        for g in (None, [], "x", {}, {"agents": "x"}, {"agents": [None, 3, {}]}):
            self.assertIsInstance(agents.validate(g, STATUS, TASKS), list)

    def test_header(self):
        d = registry(); d["schema_version"] = 2
        self.check(d, "schema_version")
        d = registry(); d["authority"] = ""
        self.check(d, "authority")
        self.check({"schema_version": 1, "authority": "x", "agents": []}, "non-empty list")

    def test_ids_and_keys(self):
        self.check(registry(agent("Bad_ID")), "id must match")
        self.check(registry(agent(), agent()), "duplicate id")
        self.check(registry(agent(extra=1)), "unknown key")
        self.check(registry(agent(name="")), "name is required")
        self.check(registry(agent(status="busy")), "status must be one of")
        self.check(registry(agent(registered="yesterday")), "registered must be YYYY-MM-DD")

    def test_streams_belong_to_one_agent(self):
        self.check(registry(agent(), agent("beta", streams=["claude:alpha-stream"], milestones=["M-BBB-01"])), "already registered")
        self.check(registry(agent(streams=["nonsense"])), "must match")

    def test_milestones_exist_and_are_exclusive(self):
        self.check(registry(agent(milestones=["M-ZZZ-01"])), "does not exist")
        self.check(registry(agent(milestones=["bad"])), "malformed")
        self.check(registry(agent(), agent("beta", streams=["claude:b"], milestones=["M-AAA-01"])), "already assigned")

    def test_active_agent_needs_work_unless_remainder(self):
        self.check(registry(agent(milestones=[])), "at least one milestone")
        self.check(registry(agent(milestones=[], remainder=True)))
        self.check(registry(agent(milestones=[], remainder=True), agent("beta", streams=["claude:b"], milestones=[], remainder=True)),
                   "only one agent may be the remainder")
        self.check(registry(agent(milestones=[], remainder=True, status="planned")), "remainder agent must be active")
        self.check(registry(agent(remainder="yes")), "remainder must be true or false")

    def test_planned_and_retired_agents_need_no_milestones(self):
        self.check(registry(agent(), agent("editor", streams=["claude:editor-stream"], milestones=[], status="planned")))

    def test_package_keys_and_docs(self):
        self.check(registry(agent(package_keys=["nope"])), "package_owners")
        self.check(registry(agent(docs=["../x"])), "path")
        ps = agents.validate(registry(agent(docs=["does/not/exist.md"])), STATUS, TASKS, ROOT)
        self.assertTrue(any("does not exist" in p for p in ps), ps)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.doc = registry(agent(), agent("beta", streams=["claude:b"], milestones=["M-BBB-01"], package_keys=["beta"]),
                            agent("main", streams=["claude:m"], milestones=[], remainder=True),
                            agent("editor", streams=["claude:e"], milestones=[], status="planned"))

    def test_active_agents_exclude_planned(self):
        self.assertEqual([a["id"] for a in agents.active_agents(self.doc)], ["alpha", "beta", "main"])

    def test_milestone_and_stream_lookup_with_remainder(self):
        self.assertEqual(agents.agent_for_milestone(self.doc, "M-BBB-01"), "beta")
        self.assertEqual(agents.agent_for_milestone(self.doc, "M-CCC-02"), "main")
        self.assertEqual(agents.agent_for_stream(self.doc, "claude:b"), "beta")
        self.assertEqual(agents.agent_for_stream(self.doc, "claude:unknown"), "main")
        self.assertEqual(agents.agent_for_stream(self.doc, "claude:e"), "main")      # planned agents own nothing yet

    def test_no_remainder_means_unassigned(self):
        doc = registry(agent())
        self.assertIsNone(agents.agent_for_milestone(doc, "M-CCC-01"))


class RealRegistryTests(unittest.TestCase):
    def load(self, name):
        return json.loads((ROOT / name).read_text(encoding="utf-8"))

    def test_the_real_registry_is_valid(self):
        self.assertEqual(agents.validate(self.load("AGENT_REGISTRY.json"), self.load("PROJECT_STATUS.json"),
                                         self.load("ACTIVE_TASKS.json"), ROOT), [])

    def test_every_milestone_has_exactly_one_responsible_agent(self):
        reg, status = self.load("AGENT_REGISTRY.json"), self.load("PROJECT_STATUS.json")
        for m in status["milestones"]:
            self.assertIsNotNone(agents.agent_for_milestone(reg, m["id"]), m["id"])

    def test_the_five_launch_agents_are_registered_and_future_ones_are_not_yet(self):
        reg = self.load("AGENT_REGISTRY.json")
        ids = [a["id"] for a in agents.active_agents(reg)]
        self.assertEqual(ids, ["main", "creative-director", "creator-dna", "research", "assets"])
        self.assertTrue(all(a["id"] not in ("editor", "resolve-executor", "qc") for a in reg["agents"]))


class WaveLaneMilestoneTests(unittest.TestCase):
    def lane(self, **kw):
        d = {"id": "L1", "task_id": "T-ZZ1-001", "title": "t", "owner": "claude:zz", "branch": "lane/w01-demo/L1",
             "owned_paths": ["orchestrator/zz/"], "test_modules": ["test_zz.py"], "acceptance_criteria": ["c"], "depends_on": []}
        d.update(kw)
        return {"schema_version": 1, "wave_id": "w01-demo", "title": "t", "integration_branch": "wave/w01-demo",
                "base_commit": "a" * 40, "created": "2026-11-17", "inactivity_hours": 48, "lanes": [d]}

    def test_optional_milestones_field(self):
        self.assertEqual(waves.validate_manifest(self.lane()), [])
        self.assertEqual(waves.validate_manifest(self.lane(milestones=["M-AST-02"])), [])
        for bad in (["nope"], "M-AST-02", [1]):
            ps = waves.validate_manifest(self.lane(milestones=bad))
            self.assertTrue(any("milestones (optional)" in p for p in ps), (bad, ps))

    def test_generated_brief_asks_for_a_heartbeat_push(self):
        m = self.lane()
        self.assertIn("git push origin HEAD", waves.build_brief(m, m["lanes"][0]))


if __name__ == "__main__":
    unittest.main()
