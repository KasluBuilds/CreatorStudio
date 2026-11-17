"""Orbit snapshot tests. Synthetic data only; temp dirs; sockets blocked (no network).

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_snapshot.py -v
"""
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib

# `dev_supervisor.snapshot` is shadowed on the package by L1's re-exported telemetry.snapshot() function,
# so the submodule is loaded explicitly.
sn = importlib.import_module("dev_supervisor.snapshot")
from dev_supervisor import telemetry as tl
from dev_supervisor.evidence import Label, observed, reported, unavailable
from dev_supervisor.state_store import StateStore
from dev_supervisor.workers import Worker, WorkerRegistry, WorkerStatus

NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
MS = int(NOW.timestamp() * 1000)


def _no_net(*a, **k):
    raise AssertionError("network access attempted")


def _walk(x):
    if isinstance(x, dict):
        if "freshness" in x and "label" in x:
            yield x
        else:
            for v in x.values():
                yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self._net = mock.patch("socket.socket", _no_net)
        self._net.start()
        self.addCleanup(self._net.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def session(self, age_s=5, status="busy", sid="sess-main-0001"):
        d = self.root / "sessions"
        d.mkdir(exist_ok=True)
        (d / "4242.json").write_text(json.dumps({
            "pid": 4242, "sessionId": sid, "cwd": "synthetic-cwd", "name": "main", "status": status,
            "startedAt": MS - 600000, "statusUpdatedAt": MS - age_s * 1000}), encoding="utf-8")
        return d

    def telemetry(self, age_s=5, provider=None):
        return tl.snapshot(self.session(age_s), None, usage_provider=provider, pid_alive=lambda p: True, now=NOW)

    def registry(self, with_worker=True):
        store = StateStore(self.root / "state", clock=lambda: NOW.timestamp())
        reg = WorkerRegistry(store)
        if with_worker:
            hb = (NOW - timedelta(seconds=30)).isoformat()
            reg.add(Worker("w-1", "builder", "T-X-1", "lane/x/L1", str(self.root / "wt1"), "claude:test",
                           model="synthetic-model", status=WorkerStatus.RUNNING, last_heartbeat=hb, restart_count=1,
                           failures=["exit:1"]))
            reg.add(Worker("w-2", "builder", "T-X-2", "lane/x/L2", str(self.root / "wt2"), "claude:test",
                           status=WorkerStatus.BLOCKED, last_heartbeat=hb))
        return store, reg


class FreshnessTests(SnapshotCase):
    def test_live_cached_stale_offline_states(self):
        kw = dict(now=NOW)
        live = sn.field(observed(1, "s", (NOW - timedelta(seconds=10)).isoformat()), **kw)
        aged = sn.field(observed(1, "s", (NOW - timedelta(seconds=300)).isoformat()), **kw)
        stale = sn.field(observed(1, "s", (NOW - timedelta(hours=3)).isoformat()), **kw)
        cached = sn.field(observed(1, "s", (NOW - timedelta(seconds=10)).isoformat()), cached=True, **kw)
        off = sn.field(unavailable("source down", "s", NOW.isoformat()), **kw)
        notime = sn.field({"label": "OBSERVED", "value": 3, "timestamp": "garbage", "source": "s"}, **kw)
        self.assertEqual([x["freshness"] for x in (live, aged, stale, cached, off, notime)],
                         ["live", "cached", "stale", "cached", "offline", "stale"])
        self.assertEqual(live["age_seconds"], 10.0)
        self.assertIsNone(off["value"])
        self.assertIn("source down", off["reason"])
        self.assertIsNone(notime["age_seconds"])

    def test_every_value_has_label_freshness_and_age_key(self):
        _, reg = self.registry()
        snap = sn.build_snapshot(self.telemetry(), reg, [], now=NOW)
        fields = list(_walk(snap))
        self.assertGreater(len(fields), 10)
        for f in fields:
            self.assertIn(f["label"], [l.value for l in Label])
            self.assertIn(f["freshness"], sn.FRESHNESS_ORDER)
            self.assertIn("age_seconds", f)
            if f["label"] == "UNAVAILABLE":
                self.assertIsNone(f["value"])
                self.assertEqual(f["freshness"], "offline")


class SnapshotTests(SnapshotCase):
    def test_full_snapshot_live_main_cached_workers(self):
        _, reg = self.registry()
        events = [{"ts": NOW.isoformat(), "kind": "worker_failed", "data": {"worker_id": "w-1", "task_id": "T-X-1", "signature": "exit:1"}},
                  {"ts": NOW.isoformat(), "kind": "worker_restart", "data": {"worker_id": "w-1", "task_id": "T-X-1"}},
                  {"ts": NOW.isoformat(), "kind": "github_checkpoint", "data": {"issue": 51, "event_id": "abcd1234"}}]
        snap = sn.build_snapshot(self.telemetry(), reg, events, now=NOW)
        self.assertEqual(snap["product"], "Orbit")
        self.assertTrue(snap["read_only"])
        self.assertTrue(snap["main"]["present"])
        self.assertEqual(snap["main"]["status"]["freshness"], "live")
        self.assertEqual(len(snap["workers"]), 2)
        self.assertEqual(snap["workers"][0]["status"]["freshness"], "cached")   # registry values are persisted records
        self.assertEqual(snap["workers"][0]["status"]["value"], "RUNNING")
        self.assertEqual(len(snap["tasks"]), 2)
        self.assertEqual(snap["failures"][0]["kind"], "worker_failed")
        self.assertEqual(len(snap["retries"]), 1)
        self.assertEqual(snap["last_github_checkpoint"]["value"]["issue"], 51)
        self.assertTrue(any("w-2" in a for a in snap["next_actions"]))        # BLOCKED worker -> human action
        self.assertEqual(snap["model_provider"]["providers"][0]["provider"], "anthropic")
        self.assertIn(snap["overall_freshness"], ("cached", "offline"))

    def test_stale_main_session(self):
        _, reg = self.registry(False)
        snap = sn.build_snapshot(self.telemetry(age_s=7200), reg, [], now=NOW)
        self.assertEqual(snap["main"]["status"]["freshness"], "stale")
        self.assertGreater(snap["main"]["status"]["age_seconds"], 7000)

    def test_offline_when_no_sources(self):
        snap = sn.build_snapshot(None, None, None, now=NOW)
        self.assertFalse(snap["main"]["present"])
        self.assertFalse(snap["registry"]["present"])
        self.assertEqual(snap["overall_freshness"], "offline")
        self.assertEqual(snap["workers"], [])
        self.assertTrue(snap["next_actions"])                                   # never an empty success
        self.assertEqual(snap["last_github_checkpoint"]["freshness"], "offline")
        self.assertIsNone(snap["last_github_checkpoint"]["value"])

    def test_pinned_main_session_not_found(self):
        snap = sn.build_snapshot(self.telemetry(), None, None, main_session_id="nope", now=NOW)
        self.assertFalse(snap["main"]["present"])

    def test_account_usage_only_when_observed(self):
        # no provider -> UNAVAILABLE with reason
        snap = sn.build_snapshot(self.telemetry(), None, None, now=NOW)
        self.assertEqual(snap["account_usage"]["usage"]["label"], "UNAVAILABLE")
        self.assertTrue(snap["account_usage"]["usage"]["reason"])
        # REPORTED statusline -> still hidden
        sl = tl.StatuslineProvider({"rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": 1}}})
        snap = sn.build_snapshot(self.telemetry(provider=sl), None, None, now=NOW)
        u = snap["account_usage"]["usage"]
        self.assertEqual(u["label"], "UNAVAILABLE")
        self.assertIn("REPORTED", u["reason"])
        self.assertNotIn("42", json.dumps(snap["account_usage"]))

        class Obs(tl.UsageProvider):
            name = "observed_test"

            def get_usage(self):
                return {"five_hour_used_percentage": observed(17, "test", NOW.isoformat())}
        snap = sn.build_snapshot(self.telemetry(provider=Obs()), None, None, now=NOW)
        u = snap["account_usage"]["usage"]
        self.assertEqual(u["five_hour_used_percentage"]["value"], 17)
        self.assertEqual(u["five_hour_used_percentage"]["label"], "OBSERVED")

        class Mixed(tl.UsageProvider):
            name = "mixed"

            def get_usage(self):
                return {"a": observed(1, "t", NOW.isoformat()), "b": reported(2, "t", NOW.isoformat())}
        snap = sn.build_snapshot(self.telemetry(provider=Mixed()), None, None, now=NOW)
        self.assertEqual(snap["account_usage"]["usage"]["label"], "UNAVAILABLE")

    def test_no_secrets_prompts_or_user_paths_in_output(self):
        _, reg = self.registry()
        text = json.dumps(sn.build_snapshot(self.telemetry(), reg, [], now=NOW))
        self.assertNotIn(str(self.root), text)                                  # worktree paths are not emitted
        for needle in ("prompt_file", "messaging_socket", "wt1"):
            self.assertNotIn(needle, text)

    def test_build_does_not_modify_supervisor_state(self):
        store, reg = self.registry()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.directory.iterdir() if p.is_file()}
        events, _ = store.read_events()
        sn.build_snapshot(self.telemetry(), reg, events, now=NOW)
        tele, reg2, ev2 = sn.load_inputs(store.directory, self.root / "sessions", None, now=NOW)
        sn.build_snapshot(tele, reg2, ev2, now=NOW)
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.directory.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_load_inputs_missing_store_reports_no_registry(self):
        tele, reg, events = sn.load_inputs(self.root / "absent", None, None)
        self.assertIsNone(reg)
        self.assertFalse((self.root / "absent").exists())                       # create=False: nothing was created
        snap = sn.build_snapshot(tele, reg, events, now=NOW)
        self.assertFalse(snap["registry"]["present"])

    def test_write_snapshot_atomic_roundtrip_and_cli(self):
        snap = sn.build_snapshot(None, None, None, now=NOW)
        out = self.root / "orbit.json"
        sn.write_snapshot(snap, out)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["product"], "Orbit")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])
        out2 = self.root / "cli.json"
        rc = sn.main(["--out", str(out2), "--state-dir", str(self.root / "none")])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out2.read_text(encoding="utf-8"))["overall_freshness"], "offline")

    def test_module_has_no_network_or_write_apis(self):
        src = Path(sn.__file__).read_text(encoding="utf-8")
        for bad in ("import socket", "urllib", "requests", "subprocess", "http.client", "append_event", ".save(", ".write("):
            self.assertNotIn(bad, src.replace("fh.write(\"\\n\")", "").replace("json.dump(", "json.dump_("), bad)


if __name__ == "__main__":
    unittest.main()
