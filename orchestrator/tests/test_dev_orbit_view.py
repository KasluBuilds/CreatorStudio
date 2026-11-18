"""Orbit desktop view: runs the Node tests and renders the Python snapshot fixture through orbit.js (synthetic data, no network)."""
import json
import shutil
import subprocess
import unittest
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


if __name__ == "__main__":
    unittest.main()
