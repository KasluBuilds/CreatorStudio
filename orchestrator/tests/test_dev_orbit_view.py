"""Orbit desktop view: runs the Node tests and renders the Python snapshot fixture through orbit.js (synthetic data, no network)."""
import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[2] / "dashboard" / "desktop"
FIXTURE = DESKTOP / "tests" / "fixtures" / "orbit" / "snapshot.json"
NODE = shutil.which("node")
SKIP = "node is not on PATH, so the Orbit desktop tests cannot run here"

RENDER = r"""
const orbit = require('./src/orbit');
const fs = require('fs');
const raw = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
if (process.argv[2]) raw.account_usage = JSON.parse(process.argv[2]);
const now = Date.parse(raw.generated_at) + 10000;
console.log(JSON.stringify(orbit.evaluate(raw, now)));
"""


def render(usage=None):
    args = [NODE, "-e", RENDER, str(FIXTURE)] + ([json.dumps(usage)] if usage is not None else [])
    done = subprocess.run(args, cwd=DESKTOP, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, SKIP)
class OrbitViewTests(unittest.TestCase):
    def test_node_tests_pass(self):
        done = subprocess.run([NODE, "--test", "tests/*.test.js"], cwd=DESKTOP, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, (done.stdout + done.stderr)[-3000:])

    def test_fixture_usage_is_unavailable_and_has_a_quality_badge(self):
        view = render()
        account = next(s for s in view["sections"] if s["id"] == "account")
        self.assertTrue(account["rows"])
        for row in account["rows"]:
            self.assertEqual(row["quality"], "unavailable")
            self.assertIsNone(row["value"])

    def test_reported_usage_values_do_not_leak(self):
        stamp = json.loads(FIXTURE.read_text(encoding="utf8"))["generated_at"]

        def ev(value, label):
            return {"label": label, "value": value, "timestamp": stamp, "source": "statusline", "freshness": "live"}

        usage = {"provider": "statusline", "usage": {"five_hour_used_percentage": ev(7391, "REPORTED"), "weekly_used_percentage": ev(8462, "INFERRED")}}
        text = json.dumps(render(usage))
        self.assertNotIn("7391", text)
        self.assertNotIn("8462", text)

    def test_observed_usage_renders_readably(self):
        stamp = json.loads(FIXTURE.read_text(encoding="utf8"))["generated_at"]
        usage = {"provider": "rate_limit_event", "usage": {"five_hour_used_percentage": {
            "label": "OBSERVED", "value": 21.0, "timestamp": stamp, "source": "rate_limit_event:w/f", "freshness": "live"}}}
        account = next(s for s in render(usage)["sections"] if s["id"] == "account")
        self.assertEqual(account["rows"][0]["name"], "5-hour window: used")
        self.assertEqual(account["rows"][0]["text"], "21%")
        self.assertEqual(account["rows"][0]["quality"], "live")


RENDER_SNAP = r"""
const orbit = require('./src/orbit');
const raw = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(orbit.evaluate(raw, Date.parse(raw.generated_at) + 5000)));
"""


def render_snapshot(snap):
    done = subprocess.run([NODE, "-e", RENDER_SNAP], cwd=DESKTOP, input=json.dumps(snap), capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, SKIP)
class OrbitOperationFromPythonSnapshotTests(unittest.TestCase):
    """The snapshot is built by dev_supervisor.snapshot (lane L1 code), then rendered by orbit.js."""

    NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)

    def build(self, **extra):
        import sys
        import tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from dev_supervisor import snapshot as sn
        from dev_supervisor.state_store import StateStore
        from dev_supervisor.workers import Worker, WorkerRegistry, WorkerStatus
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reg = WorkerRegistry(StateStore(Path(tmp.name) / "state", clock=lambda: self.NOW.timestamp()))
        hb = (self.NOW - timedelta(seconds=30)).isoformat()
        for wid, task, status in (("w-run", "T-RUN", WorkerStatus.RUNNING), ("w-asg", "T-ASG", WorkerStatus.ASSIGNED), ("w-blk", "T-BLK", WorkerStatus.BLOCKED)):
            reg.add(Worker(wid, "builder", task, "lane/x/" + wid, str(Path(tmp.name) / wid), "claude:test", status=status, last_heartbeat=hb))
        return sn.build_snapshot(None, reg, [], now=self.NOW, **extra)

    @staticmethod
    def ev(value, now):
        return {"label": "OBSERVED", "value": value, "timestamp": now.isoformat(), "source": "synthetic"}

    def test_full_snapshot_renders_every_new_section(self):
        n = self.NOW
        op = {"items": [{"id": "T-RUN", "kind": "review", "priority": 1, "reason": "review it", "inspect": "git log"}],
              "others_waiting_review": [{"id": "T-ASG", "kind": "worker", "priority": 5, "reason": "waiting"}],
              "blocked": [{"id": "T-BLK", "kind": "task", "blocker": "restart budget used"}],
              "warnings": ["two Main sessions"], "fingerprint": "f" * 40}
        snap = self.build(operation=op, health={"lock_alive": self.ev(True, n), "last_tick_at": self.ev(n.isoformat(), n), "last_event_kind": self.ev("tick", n)},
                          handoff={"latest_id": self.ev("H-1", n), "verified": self.ev(True, n), "created_at": self.ev(n.isoformat(), n)},
                          worker_context={"w-run": {k: self.ev(v, n) for k, v in (("tokens", 100), ("window", 1000), ("ratio", 0.1), ("level", "low"))}})
        view = render_snapshot(snap)
        sec = {s["id"]: s for s in view["sections"]}
        self.assertIn("1. T-RUN [review] priority 1 review it", sec["operation_actionable"]["items"][0]["text"])
        self.assertIn("T-ASG", sec["operation_waiting"]["items"][0]["text"])
        self.assertIn("BLOCKED T-BLK", sec["operation_blocked"]["items"][0]["text"])
        self.assertEqual(sec["operation_idle"]["items"][0]["text"], "warning: two Main sessions")
        self.assertEqual(sec["operation_idle"]["rows"][0]["quality"], "unavailable")      # not idle: there is an actionable item
        for sid in ("supervisor_health", "handoff"):
            self.assertTrue(sec[sid]["rows"])
            self.assertTrue(all(r["label"] == "OBSERVED" for r in sec[sid]["rows"]), sid)
        by = {i["text"].split(" ")[0]: i for i in sec["workers"]["items"]}
        self.assertEqual(by["w-run"]["context"][0]["text"], "100")
        self.assertTrue(all(c["quality"] == "unavailable" and c["reason"] for c in by["w-asg"]["context"]))
        self.assertEqual(by["w-asg"]["activity"]["kind"], "registered")
        self.assertEqual(by["w-blk"]["activity"]["kind"], "blocked")
        self.assertEqual(by["w-run"]["activity"]["kind"], "active")
        tasks = {i["text"].split(" ")[0].rstrip(":"): i["text"] for i in sec["tasks"]["items"]}
        self.assertIn("[BLOCKED, not running]", tasks["T-BLK"])
        self.assertIn("[no worker running]", tasks["T-ASG"])

    def test_sections_not_supplied_render_as_unavailable(self):
        view = render_snapshot(self.build())
        sec = {s["id"]: s for s in view["sections"]}
        for sid in ("operation_actionable", "operation_waiting", "operation_blocked", "operation_idle", "supervisor_health", "handoff"):
            self.assertTrue(sec[sid]["rows"], sid)
            self.assertTrue(all(r["quality"] == "unavailable" and r["reason"] for r in sec[sid]["rows"]), sid)
            self.assertEqual(sec[sid]["items"], [], sid)
        self.assertIn("did not compute", sec["operation_actionable"]["rows"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
