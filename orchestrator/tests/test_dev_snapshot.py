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
        self.assertEqual(snap["overall_freshness"], "cached")          # unavailable PR/branch keep it from "live"

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
        empty = self.root / "emptyrepo"
        empty.mkdir()
        rc = sn.main(["--out", str(out2), "--state-dir", str(self.root / "none"), "--repo", str(empty),
                      "--sessions-dir", str(self.root / "nosessions"), "--projects-dir", str(self.root / "noproj")])
        self.assertEqual(rc, 0)
        # the operation picture (idle reason read from the empty repo) is a live observation, so not "offline"
        self.assertEqual(json.loads(out2.read_text(encoding="utf-8"))["overall_freshness"], "cached")

    def test_module_has_no_network_or_write_apis(self):
        src = Path(sn.__file__).read_text(encoding="utf-8")
        for bad in ("import socket", "urllib", "requests", "subprocess", "http.client", "append_event", ".save(", ".write("):
            self.assertNotIn(bad, src.replace("fh.write(\"\\n\")", "").replace("json.dump(", "json.dump_("), bad)


class OperationSectionTests(SnapshotCase):
    def repo(self, tasks=None):
        r = self.root / "repo"
        r.mkdir(exist_ok=True)
        owner = "claude:repository-coordinator"
        (r / "ACTIVE_TASKS.json").write_text(json.dumps({"tasks": tasks if tasks is not None else [
            {"id": "T-A", "status": "PLANNED", "depends_on": [], "owned_paths": ["a/"], "owner": owner},
            {"id": "T-B", "status": "PLANNED", "depends_on": ["T-A"], "owned_paths": ["b/"], "owner": owner}]}),
            encoding="utf-8")
        return r

    def op(self, tasks=None, store=None):
        from dev_supervisor import autonomy
        return autonomy.compute_items(self.repo(tasks), store, lambda args: (1, ""))

    def test_actionable_record_shape(self):
        snap = sn.build_snapshot(None, None, None, now=NOW, operation=self.op())
        a = snap["operation"]["actionable"]
        self.assertEqual([i["id"] for i in a], ["start_task:T-A"])
        self.assertEqual(set(a[0]), {"id", "kind", "priority", "summary", "inspect"})
        self.assertIsInstance(a[0]["priority"], int)
        self.assertTrue(a[0]["summary"] and a[0]["inspect"])

    def test_blocked_record_shape(self):
        b = sn.build_snapshot(None, None, None, now=NOW, operation=self.op())["operation"]["blocked"]
        self.assertEqual([x["id"] for x in b], ["blocked:T-B"])
        self.assertEqual(set(b[0]), {"id", "kind", "summary"})
        self.assertIn("T-A", b[0]["summary"])

    def test_not_idle_has_unavailable_idle_reason(self):
        i = sn.build_snapshot(None, None, None, now=NOW, operation=self.op())["operation"]["idle_reason"]
        self.assertEqual(i["label"], "UNAVAILABLE")
        self.assertIn("not idle", i["reason"])

    def test_idle_reason_observed_when_nothing_to_do(self):
        op = sn.build_snapshot(None, None, None, now=NOW, operation=self.op(tasks=[]))["operation"]
        self.assertEqual(op["actionable"], [])
        self.assertEqual(op["idle_reason"]["label"], "OBSERVED")
        self.assertIn("no registered work", op["idle_reason"]["value"])
        self.assertEqual(op["idle_reason"]["freshness"], "live")

    def test_fingerprint_matches_autonomy(self):
        from dev_supervisor import autonomy
        op = self.op()
        got = sn.build_snapshot(None, None, None, now=NOW, operation=op)["operation"]["fingerprint"]
        self.assertEqual(got, autonomy.fingerprint([i["id"] for i in op["items"]]))
        self.assertEqual(got, op["fingerprint"])

    def test_others_waiting_records(self):
        op = {"items": [], "blocked": [], "fingerprint": "f", "warnings": [], "others_waiting_review": [
            {"kind": "others_waiting_review", "id": "ready_for_review:T-Z:S1", "task_id": "T-Z", "reason": "not for Main"}]}
        o = sn.build_snapshot(None, None, None, now=NOW, operation=op)["operation"]["others_waiting"]
        self.assertEqual(o[0]["id"], "ready_for_review:T-Z:S1")
        self.assertEqual(o[0]["summary"], "not for Main")
        self.assertEqual(o[0]["priority"], 99)
        self.assertEqual(o[0]["inspect"], sn.DEFAULT_INSPECT)

    def test_warnings_passed_through_as_strings(self):
        op = self.op()
        op["warnings"] = ["x", 5]
        self.assertEqual(sn.build_snapshot(None, None, None, now=NOW, operation=op)["operation"]["warnings"], ["x", "5"])

    def test_missing_operation_is_explicit_unavailable(self):
        op = sn.build_snapshot(None, None, None, now=NOW)["operation"]
        self.assertEqual(op["actionable"] + op["others_waiting"] + op["blocked"], [])
        self.assertEqual(op["idle_reason"]["label"], "UNAVAILABLE")
        self.assertTrue(op["warnings"])
        self.assertEqual(op["fingerprint"], "")

    def test_malformed_items_ignored(self):
        op = {"items": ["junk", {"id": "a", "kind": "k", "reason": "r"}], "blocked": [None], "fingerprint": "z"}
        o = sn.build_snapshot(None, None, None, now=NOW, operation=op)["operation"]
        self.assertEqual(len(o["actionable"]), 1)
        self.assertEqual(o["blocked"], [])


class HealthHandoffTests(SnapshotCase):
    def store(self):
        store, _ = self.registry(False)
        return store

    def test_lock_alive_true_and_false(self):
        st = self.store()
        (st.directory / "supervisor.lock").write_text(json.dumps({"pid": 777, "owner": "s"}), encoding="utf-8")
        h = sn.health_from_store(st, [], NOW, pid_alive=lambda p: p == 777)
        self.assertIs(h["lock_alive"].value, True)
        h = sn.health_from_store(st, [], NOW, pid_alive=lambda p: False)
        self.assertIs(h["lock_alive"].value, False)
        self.assertEqual(h["lock_alive"].label, Label.OBSERVED)

    def test_lock_absent_is_observed_false(self):
        h = sn.health_from_store(self.store(), [], NOW)
        self.assertIs(h["lock_alive"].value, False)
        self.assertIn("absent", h["lock_alive"].source)

    def test_lock_unreadable_and_no_pid(self):
        st = self.store()
        lock = st.directory / "supervisor.lock"
        lock.write_text("not json", encoding="utf-8")
        self.assertEqual(sn.health_from_store(st, [], NOW)["lock_alive"].label, Label.UNAVAILABLE)
        lock.write_text(json.dumps({"owner": "x"}), encoding="utf-8")
        self.assertEqual(sn.health_from_store(st, [], NOW)["lock_alive"].label, Label.UNAVAILABLE)

    def test_pid_check_failure_is_unavailable(self):
        st = self.store()
        (st.directory / "supervisor.lock").write_text(json.dumps({"pid": 5}), encoding="utf-8")

        def boom(pid):
            raise OSError("x")
        self.assertEqual(sn.health_from_store(st, [], NOW, pid_alive=boom)["lock_alive"].label, Label.UNAVAILABLE)

    def test_tick_event_preferred_over_last_event(self):
        ev = [{"ts": "2026-11-18T11:00:00+00:00", "kind": "tick"}, {"ts": "2026-11-18T11:30:00+00:00", "kind": "worker_start"}]
        h = sn.health_from_store(self.store(), ev, NOW)
        self.assertEqual(h["last_tick_at"].value, "2026-11-18T11:00:00+00:00")
        self.assertEqual(h["last_tick_at"].label, Label.OBSERVED)
        self.assertEqual(h["last_event_kind"].value, "worker_start")

    def test_no_tick_falls_back_to_inferred_last_event(self):
        ev = [{"ts": "2026-11-18T11:30:00+00:00", "kind": "worker_start"}]
        h = sn.health_from_store(self.store(), ev, NOW)
        self.assertEqual(h["last_tick_at"].label, Label.INFERRED)
        self.assertEqual(h["last_tick_at"].value, "2026-11-18T11:30:00+00:00")

    def test_no_events_unavailable(self):
        h = sn.health_from_store(self.store(), [], NOW)
        self.assertEqual(h["last_tick_at"].label, Label.UNAVAILABLE)
        self.assertEqual(h["last_event_kind"].label, Label.UNAVAILABLE)

    def test_health_in_snapshot_freshness(self):
        ev = [{"ts": (NOW - timedelta(hours=2)).isoformat(), "kind": "tick"}]
        h = sn.health_from_store(self.store(), ev, NOW)
        snap = sn.build_snapshot(None, None, ev, now=NOW, health=h)["supervisor_health"]
        self.assertEqual(set(snap), set(sn.HEALTH_KEYS))
        self.assertEqual(snap["last_tick_at"]["freshness"], "stale")

    def test_missing_health_and_handoff_unavailable(self):
        snap = sn.build_snapshot(None, None, None, now=NOW)
        for sec, keys in (("supervisor_health", sn.HEALTH_KEYS), ("handoff", sn.HANDOFF_KEYS)):
            self.assertEqual(set(snap[sec]), set(keys))
            for k in keys:
                self.assertEqual(snap[sec][k]["label"], "UNAVAILABLE")

    def write_handoff(self, st, hid, created, mtime):
        d = st.directory / "handoffs"
        d.mkdir(exist_ok=True)
        p = d / (hid + ".json")
        p.write_text(json.dumps({"handoff": {"created_at": created}}), encoding="utf-8")
        os.utime(p, (mtime, mtime))

    def test_no_handoff_dir(self):
        h = sn.handoff_from_store(self.store(), [], NOW)
        self.assertEqual({k: v.label for k, v in h.items()}, {k: Label.UNAVAILABLE for k in sn.HANDOFF_KEYS})

    def test_newest_handoff_picked_with_verification(self):
        st = self.store()
        self.write_handoff(st, "h-old", "2026-11-18T08:00:00+00:00", 1000)
        self.write_handoff(st, "h-new", "2026-11-18T10:00:00+00:00", 2000)
        ev = [{"ts": "2026-11-18T10:01:00+00:00", "kind": "handoff_not_safe", "data": {"handoff_id": "h-old"}},
              {"ts": "2026-11-18T10:02:00+00:00", "kind": "handoff_verified", "data": {"handoff_id": "h-new"}}]
        h = sn.handoff_from_store(st, ev, NOW)
        self.assertEqual(h["latest_id"].value, "h-new")
        self.assertEqual(h["created_at"].value, "2026-11-18T10:00:00+00:00")
        self.assertIs(h["verified"].value, True)

    def test_unsafe_handoff_reports_false(self):
        st = self.store()
        self.write_handoff(st, "h-1", "2026-11-18T10:00:00+00:00", 5)
        ev = [{"ts": "2026-11-18T10:02:00+00:00", "kind": "handoff_not_safe", "data": {"handoff_id": "h-1"}}]
        self.assertIs(sn.handoff_from_store(st, ev, NOW)["verified"].value, False)

    def test_handoff_without_verification_unavailable(self):
        st = self.store()
        self.write_handoff(st, "h-1", "2026-11-18T10:00:00+00:00", 5)
        h = sn.handoff_from_store(st, [], NOW)
        self.assertEqual(h["verified"].label, Label.UNAVAILABLE)
        self.assertEqual(h["latest_id"].label, Label.OBSERVED)

    def test_corrupt_handoff_file_keeps_id_drops_created(self):
        st = self.store()
        d = st.directory / "handoffs"
        d.mkdir()
        (d / "h-bad.json").write_text("{nope", encoding="utf-8")
        h = sn.handoff_from_store(st, [], NOW)
        self.assertEqual(h["latest_id"].value, "h-bad")
        self.assertEqual(h["created_at"].label, Label.UNAVAILABLE)

    def test_handoff_in_snapshot(self):
        st = self.store()
        self.write_handoff(st, "h-1", (NOW - timedelta(seconds=20)).isoformat(), 5)
        snap = sn.build_snapshot(None, None, [], now=NOW, handoff=sn.handoff_from_store(st, [], NOW))["handoff"]
        self.assertEqual(snap["latest_id"]["value"], "h-1")
        self.assertEqual(snap["created_at"]["age_seconds"], 20.0)

    def test_readers_do_not_modify_state_directory(self):
        st = self.store()
        self.write_handoff(st, "h-1", "2026-11-18T10:00:00+00:00", 5)
        before = sorted((p.name, p.stat().st_mtime_ns) for p in st.directory.rglob("*") if p.is_file())
        sn.health_from_store(st, [], NOW)
        sn.handoff_from_store(st, [], NOW)
        after = sorted((p.name, p.stat().st_mtime_ns) for p in st.directory.rglob("*") if p.is_file())
        self.assertEqual(before, after)


class WorkerContextTests(SnapshotCase):
    def transcript(self, sid, cwd, tokens=50000, model="claude-opus-4"):
        d = self.root / "projects" / tl.cwd_slug(cwd)
        d.mkdir(parents=True, exist_ok=True)
        line = {"type": "assistant", "message": {"model": model, "usage": {"input_tokens": tokens, "output_tokens": 10,
                                                                           "cache_read_input_tokens": 0,
                                                                           "cache_creation_input_tokens": 0}}}
        (d / (sid + ".jsonl")).write_text(json.dumps(line) + "\n", encoding="utf-8")
        return self.root / "projects"

    def reg_with_session(self, sid, worktree):
        store = StateStore(self.root / "state", clock=lambda: NOW.timestamp())
        reg = WorkerRegistry(store)
        reg.add(Worker("w-9", "builder", "T-Q", "lane/q", worktree, "claude:test", model="m",
                       status=WorkerStatus.RUNNING, last_heartbeat=NOW.isoformat(), session_id=sid))
        return reg

    def test_worker_pressure_from_own_transcript(self):
        reg = self.reg_with_session("sess-w9", "synthetic-worktree")
        proj = self.transcript("sess-w9", "synthetic-worktree")
        ctx = sn.worker_contexts(reg, proj)
        self.assertEqual(ctx["w-9"]["tokens"].value, 50000)
        snap = sn.build_snapshot(None, reg, [], now=NOW, worker_context=ctx)
        cp = snap["workers"][0]["context_pressure"]
        self.assertEqual(set(cp), set(sn.PRESSURE_KEYS))
        self.assertEqual(cp["tokens"]["value"], 50000)

    def test_worker_without_session_id_unavailable_with_reason(self):
        _, reg = self.registry()
        ctx = sn.worker_contexts(reg, self.root / "projects")
        self.assertIn("no session id", ctx["w-1"]["tokens"].reason)

    def test_worker_transcript_not_found(self):
        reg = self.reg_with_session("sess-none", "wt")
        (self.root / "projects").mkdir()
        ctx = sn.worker_contexts(reg, self.root / "projects")
        self.assertIn("transcript not found", ctx["w-9"]["ratio"].reason)

    def test_no_projects_dir(self):
        reg = self.reg_with_session("s1", "wt")
        self.assertIn("projects directory", sn.worker_contexts(reg, None)["w-9"]["level"].reason)

    def test_default_worker_pressure_unavailable_in_snapshot(self):
        _, reg = self.registry()
        for w in sn.build_snapshot(None, reg, [], now=NOW)["workers"]:
            for k in sn.PRESSURE_KEYS:
                self.assertEqual(w["context_pressure"][k]["label"], "UNAVAILABLE")
                self.assertTrue(w["context_pressure"][k]["reason"])

    def test_transcript_text_never_leaks(self):
        reg = self.reg_with_session("sess-w9", "synthetic-worktree")
        d = self.root / "projects" / tl.cwd_slug("synthetic-worktree")
        d.mkdir(parents=True)
        secret = "PRIVATE-PROMPT-TEXT"
        rec = {"type": "assistant", "content": secret, "message": {"model": "claude-opus-4", "usage": {"input_tokens": 9}}}
        (d / "sess-w9.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
        ctx = sn.worker_contexts(reg, self.root / "projects")
        self.assertNotIn(secret, json.dumps(sn.build_snapshot(None, reg, [], now=NOW, worker_context=ctx)))


class FullSnapshotTests(SnapshotCase):
    def full(self, **kw):
        repo = self.root / "repo"
        repo.mkdir(exist_ok=True)
        if not (repo / "ACTIVE_TASKS.json").exists():
            (repo / "ACTIVE_TASKS.json").write_text(json.dumps({"tasks": [
                {"id": "T-A", "status": "PLANNED", "depends_on": [], "owned_paths": ["a/"],
                 "owner": "claude:repository-coordinator"}]}), encoding="utf-8")
        args = dict(state_dir=self.root / "state", repo=repo, sessions_dir=self.session(), projects_dir=self.root / "projects",
                    now=NOW, git=lambda a: (1, ""), pid_alive=lambda p: True)
        args.update(kw)
        return sn.build_full_snapshot(**args)

    def test_full_snapshot_has_all_sections(self):
        self.registry()
        snap = self.full()
        for k in ("operation", "supervisor_health", "handoff", "workers", "main", "account_usage"):
            self.assertIn(k, snap)
        self.assertEqual([i["id"] for i in snap["operation"]["actionable"]], ["start_task:T-A"])
        self.assertEqual(snap["supervisor_health"]["lock_alive"]["label"], "OBSERVED")
        self.assertEqual(len(snap["workers"]), 2)
        self.assertIn("context_pressure", snap["workers"][0])

    def test_missing_state_dir_still_builds(self):
        snap = self.full(state_dir=self.root / "absent")
        self.assertFalse(snap["registry"]["present"])
        self.assertEqual(snap["supervisor_health"]["lock_alive"]["label"], "UNAVAILABLE")
        self.assertEqual(snap["handoff"]["latest_id"]["label"], "UNAVAILABLE")
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual(len(snap["operation"]["actionable"]), 1)

    def test_account_usage_unavailable_without_rate_limit_provider(self):
        with mock.patch.object(tl, "RateLimitEventProvider", None, create=True):
            snap = self.full()
        self.assertEqual(snap["account_usage"]["usage"]["label"], "UNAVAILABLE")

    def test_rate_limit_provider_used_when_present(self):
        calls = []

        class P(tl.UsageProvider):
            def get_usage(self):
                calls.append(1)
                return None
        with mock.patch.object(tl, "RateLimitEventProvider", P, create=True):
            self.full()
        self.assertTrue(calls)

    def test_provider_constructor_failure_tolerated(self):
        def bad():
            raise RuntimeError("x")
        with mock.patch.object(tl, "RateLimitEventProvider", bad, create=True):
            self.assertIn("account_usage", self.full())

    def test_full_run_writes_nothing_into_state_dir(self):
        self.registry()
        st = self.root / "state"
        before = sorted((str(p.relative_to(st)), p.stat().st_mtime_ns) for p in st.rglob("*") if p.is_file())
        self.full()
        after = sorted((str(p.relative_to(st)), p.stat().st_mtime_ns) for p in st.rglob("*") if p.is_file())
        self.assertEqual(before, after)

    def test_cli_entry_writes_json_atomically(self):
        out = self.root / "o" / "snap.json"
        out.parent.mkdir()
        repo = self.root / "repo2"
        repo.mkdir()
        rc = sn.main(["--out", str(out), "--repo", str(repo), "--state-dir", str(self.root / "state"),
                      "--sessions-dir", str(self.session()), "--projects-dir", str(self.root / "projects")])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue({"operation", "supervisor_health", "handoff"} <= set(data))
        self.assertEqual([p.name for p in out.parent.iterdir()], ["snap.json"])

    def test_existing_signature_still_works(self):
        snap = sn.build_snapshot(self.telemetry(), None, [], now=NOW)
        self.assertEqual(snap["schema_version"], 1)

    def test_all_new_values_are_evidence_dicts(self):
        self.registry()
        snap = self.full()
        need = {"label", "value", "timestamp", "source", "freshness"}
        for sec in ("supervisor_health", "handoff"):
            for v in snap[sec].values():
                self.assertTrue(need <= set(v))
        self.assertTrue(need <= set(snap["operation"]["idle_reason"]))

    def test_snapshot_has_no_user_paths(self):
        self.registry()
        self.assertNotIn("Korisnik", json.dumps(self.full()))


if __name__ == "__main__":
    unittest.main()
