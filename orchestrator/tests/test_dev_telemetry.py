"""Dev supervisor telemetry tests. Synthetic data only; temp dirs; sockets blocked (no network).

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_telemetry.py -v
"""
import json
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import evidence as ev
from dev_supervisor import telemetry as tl
from dev_supervisor.evidence import Evidence, Label

NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
SECRET_TEXT = "SECRET-PROMPT-TEXT-DO-NOT-LEAK"


def _no_net(*a, **k):
    raise AssertionError("network access attempted")


def ms(dt):
    return int(dt.timestamp() * 1000)


def asst(model="claude-sonnet-5-5", i=1, r=0, c=0, text=SECRET_TEXT, **extra):
    m = {"role": "assistant", "model": model, "content": [{"type": "text", "text": text}],
         "usage": {"input_tokens": i, "cache_read_input_tokens": r, "cache_creation_input_tokens": c}}
    m.update(extra)
    return json.dumps({"type": "assistant", "message": m})


def user(text=SECRET_TEXT):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(socket, "socket", _no_net)
        p.start()
        self.addCleanup(p.stop)
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.dir = Path(t.name)

    def session(self, pid=100, **kw):
        d = {"pid": pid, "sessionId": "sess-%d" % pid, "cwd": "C:\\fake\\proj", "name": "lane", "status": "busy",
             "startedAt": ms(NOW - timedelta(hours=1)), "statusUpdatedAt": ms(NOW - timedelta(seconds=10)),
             "messagingSocketPath": "\\\\.\\pipe\\fake-%d" % pid}
        d.update(kw)
        d = {k: v for k, v in d.items() if v is not ...}
        p = self.dir / "sessions"
        p.mkdir(exist_ok=True)
        (p / ("%d.json" % pid)).write_text(json.dumps(d), encoding="utf-8")
        return p

    def transcript(self, lines, name="t.jsonl"):
        p = self.dir / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p


class EvidenceTests(unittest.TestCase):
    def test_observed_has_label_and_timestamp(self):
        e = ev.observed(5, "src")
        self.assertEqual(e.label, Label.OBSERVED)
        self.assertTrue(e.timestamp)

    def test_reported_label(self):
        self.assertEqual(ev.reported(1).label, Label.REPORTED)

    def test_inferred_label(self):
        self.assertEqual(ev.inferred(1).label, Label.INFERRED)

    def test_unavailable_has_no_value_and_reason(self):
        e = ev.unavailable("why")
        self.assertIsNone(e.value)
        self.assertEqual(e.reason, "why")
        self.assertFalse(e.available)

    def test_unavailable_with_value_rejected(self):
        with self.assertRaises(ValueError):
            Evidence(Label.UNAVAILABLE, 3)

    def test_labelled_without_value_rejected(self):
        with self.assertRaises(ValueError):
            Evidence(Label.OBSERVED, None)

    def test_staleness_fresh_and_old(self):
        e = ev.observed(1, "s", NOW.isoformat())
        self.assertFalse(e.is_stale(60, NOW + timedelta(seconds=30)))
        self.assertTrue(e.is_stale(60, NOW + timedelta(seconds=61)))

    def test_staleness_boundary_not_stale(self):
        e = ev.observed(1, "s", NOW.isoformat())
        self.assertFalse(e.is_stale(60, NOW + timedelta(seconds=60)))

    def test_bad_timestamp_is_stale(self):
        e = Evidence(Label.OBSERVED, 1, "garbage", "s")
        self.assertTrue(e.is_stale(10 ** 9))
        self.assertIsNone(e.age_seconds())

    def test_naive_timestamp_treated_utc(self):
        e = Evidence(Label.OBSERVED, 1, "2026-11-18T12:00:00", "s")
        self.assertEqual(e.age_seconds(NOW), 0)

    def test_to_dict_json_serialisable(self):
        d = ev.unavailable("r", "s").to_dict()
        self.assertEqual(json.loads(json.dumps(d))["label"], "UNAVAILABLE")
        self.assertEqual(d["reason"], "r")

    def test_evidence_frozen(self):
        with self.assertRaises(Exception):
            ev.observed(1).value = 2


class SessionTests(Base):
    def read(self, **kw):
        return tl.read_sessions(self.dir / "sessions", now=NOW, **kw)

    def test_missing_dir_empty(self):
        self.assertEqual(tl.read_sessions(self.dir / "nope"), [])

    def test_reads_fields(self):
        self.session()
        s = self.read(pid_alive=lambda p: True)[0]
        self.assertEqual(s["pid"].value, 100)
        self.assertEqual(s["session_id"].value, "sess-100")
        self.assertEqual(s["name"].value, "lane")
        self.assertEqual(s["status"].value, "busy")
        self.assertTrue(s["messaging_socket"].available)
        self.assertEqual(s["started_at"].label, Label.OBSERVED)

    def test_dead_pid(self):
        self.session()
        s = self.read(pid_alive=lambda p: False)[0]
        self.assertFalse(s["alive"].value)

    def test_probe_receives_pid(self):
        self.session(pid=77)
        seen = []
        self.read(pid_alive=lambda p: seen.append(p) or True)
        self.assertEqual(seen, [77])

    def test_probe_exception_means_dead(self):
        self.session()

        def boom(p):
            raise RuntimeError("x")
        self.assertFalse(self.read(pid_alive=boom)[0]["alive"].value)

    def test_corrupt_file_reported_not_raised(self):
        p = self.session()
        (p / "bad.json").write_text("{not json", encoding="utf-8")
        res = self.read(pid_alive=lambda p: True)
        errs = [r for r in res if "error" in r]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["error"].label, Label.UNAVAILABLE)
        self.assertEqual(len(res), 2)

    def test_non_object_json(self):
        p = self.session()
        (p / "arr.json").write_text("[1,2]", encoding="utf-8")
        self.assertEqual(sum(1 for r in self.read(pid_alive=lambda p: True) if "error" in r), 1)

    def test_invalid_utf8_file(self):
        p = self.session()
        (p / "bin.json").write_bytes(b"\xff\xfe\x00\x00garbage")
        self.assertEqual(sum(1 for r in self.read(pid_alive=lambda p: True) if "error" in r), 1)

    def test_non_json_files_ignored(self):
        p = self.session()
        (p / "x.key").write_text("k", encoding="utf-8")
        self.assertEqual(len(self.read(pid_alive=lambda p: True)), 1)

    def test_missing_fields_unavailable(self):
        self.session(sessionId=..., name=..., cwd=..., messagingSocketPath=...)
        s = self.read(pid_alive=lambda p: True)[0]
        for k in ("session_id", "name", "cwd", "messaging_socket"):
            self.assertFalse(s[k].available, k)

    def test_missing_status(self):
        self.session(status=...)
        self.assertFalse(self.read(pid_alive=lambda p: True)[0]["status"].available)

    def test_unknown_status_reported_not_observed(self):
        self.session(status="weird")
        st = self.read(pid_alive=lambda p: True)[0]["status"]
        self.assertEqual(st.label, Label.REPORTED)

    def test_idle_status(self):
        self.session(status="idle")
        self.assertEqual(self.read(pid_alive=lambda p: True)[0]["status"].value, "idle")

    def test_pid_wrong_type(self):
        self.session(pid=100)
        p = self.dir / "sessions" / "100.json"
        d = json.loads(p.read_text())
        d["pid"] = "100"
        p.write_text(json.dumps(d))
        s = self.read(pid_alive=lambda p: True)[0]
        self.assertFalse(s["pid"].available)
        self.assertFalse(s["alive"].value)

    def test_fresh_not_stale(self):
        self.session()
        self.assertFalse(self.read(pid_alive=lambda p: True)[0]["stale"].value)

    def test_old_update_stale(self):
        self.session(statusUpdatedAt=ms(NOW - timedelta(hours=2)))
        self.assertTrue(self.read(pid_alive=lambda p: True)[0]["stale"].value)

    def test_no_update_time_stale(self):
        self.session(statusUpdatedAt=..., updatedAt=...)
        s = self.read(pid_alive=lambda p: True)[0]
        self.assertTrue(s["stale"].value)
        self.assertFalse(s["updated_at"].available)

    def test_stale_threshold_configurable(self):
        self.session(statusUpdatedAt=ms(NOW - timedelta(seconds=100)))
        self.assertTrue(self.read(pid_alive=lambda p: True, stale_after_seconds=50)[0]["stale"].value)
        self.assertFalse(self.read(pid_alive=lambda p: True, stale_after_seconds=500)[0]["stale"].value)

    def test_absurd_startedat_unavailable(self):
        self.session(startedAt=10 ** 30)
        self.assertFalse(self.read(pid_alive=lambda p: True)[0]["started_at"].available)

    def test_multiple_sessions_sorted(self):
        self.session(pid=2)
        self.session(pid=1)
        self.assertEqual([s["pid"].value for s in self.read(pid_alive=lambda p: True)], [1, 2])

    def test_default_probe_own_pid_alive(self):
        import os
        self.assertTrue(tl.default_pid_alive(os.getpid()))

    def test_default_probe_invalid_pids(self):
        for bad in (0, -5, None, "x", True):
            self.assertFalse(tl.default_pid_alive(bad))

    def test_default_probe_dead_pid(self):
        # an exited child process: its pid must not report alive
        import subprocess
        pr = subprocess.Popen([sys.executable, "-c", "pass"])
        pr.wait()
        self.assertFalse(tl.default_pid_alive(pr.pid))


class UsageReadTests(Base):
    def test_last_assistant_record_wins(self):
        p = self.transcript([asst(i=1), user(), asst(i=5, r=10, c=20)])
        u = tl.read_last_usage(p)
        self.assertEqual(u["total"], 35)

    def test_sum_of_three_fields(self):
        u = tl.read_last_usage(self.transcript([asst(i=3, r=4, c=5)]))
        self.assertEqual((u["input_tokens"], u["cache_read_input_tokens"], u["cache_creation_input_tokens"]),
                         (3, 4, 5))

    def test_model_returned(self):
        self.assertEqual(tl.read_last_usage(self.transcript([asst(model="m-x")]))["model"], "m-x")

    def test_multiple_models_uses_last(self):
        p = self.transcript([asst(model="claude-opus-4-5", i=1), asst(model="claude-sonnet-5-5", i=2)])
        self.assertEqual(tl.read_last_usage(p)["model"], "claude-sonnet-5-5")

    def test_corrupt_lines_skipped(self):
        p = self.transcript([asst(i=7), "{broken", "", "not json at all"])
        self.assertEqual(tl.read_last_usage(p)["total"], 7)

    def test_trailing_partial_line_skipped(self):
        p = self.dir / "t.jsonl"
        p.write_text(asst(i=9) + "\n" + asst(i=100)[:40], encoding="utf-8")
        self.assertEqual(tl.read_last_usage(p)["total"], 9)

    def test_missing_usage_skipped(self):
        nousage = json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "m"}})
        self.assertEqual(tl.read_last_usage(self.transcript([asst(i=4), nousage]))["total"], 4)

    def test_no_usage_at_all_none(self):
        self.assertIsNone(tl.read_last_usage(self.transcript([user(), user()])))

    def test_empty_file_none(self):
        p = self.dir / "e.jsonl"
        p.write_text("", encoding="utf-8")
        self.assertIsNone(tl.read_last_usage(p))

    def test_user_record_with_usage_ignored(self):
        rec = json.dumps({"type": "user", "message": {"role": "user", "usage": {"input_tokens": 999}}})
        self.assertEqual(tl.read_last_usage(self.transcript([asst(i=2), rec]))["total"], 2)

    def test_negative_or_string_tokens_rejected(self):
        bad = json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "m",
                                                            "usage": {"input_tokens": "5"}}})
        self.assertEqual(tl.read_last_usage(self.transcript([asst(i=3), bad]))["total"], 3)

    def test_missing_cache_fields_default_zero(self):
        rec = json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "m",
                                                            "usage": {"input_tokens": 12}}})
        self.assertEqual(tl.read_last_usage(self.transcript([rec]))["total"], 12)

    def test_missing_model_none(self):
        rec = json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                            "usage": {"input_tokens": 1}}})
        self.assertIsNone(tl.read_last_usage(self.transcript([rec]))["model"])

    def test_huge_line_before_usage_record(self):
        huge = json.dumps({"type": "user", "message": {"content": "x" * (2 * 1024 * 1024)}})
        p = self.transcript([asst(i=11), huge])
        self.assertEqual(tl.read_last_usage(p)["total"], 11)  # tail grows to find it

    def test_huge_line_with_usage_only_partly_in_tail_skipped(self):
        # a >MAX tail with no usage at the end must stop, not load everything
        junk = [user("y" * 1000) for _ in range(10)]
        p = self.transcript([asst(i=1)] + junk)
        self.assertEqual(tl.read_last_usage(p)["total"], 1)

    def test_only_tail_read(self):
        big = [user("z" * 4000) for _ in range(300)]  # ~1.2MB
        p = self.transcript(big + [asst(i=21)])
        reads = []
        real = tl._tail

        def spy(path, n):
            reads.append(n)
            return real(path, n)
        with mock.patch.object(tl, "_tail", spy):
            self.assertEqual(tl.read_last_usage(p)["total"], 21)
        self.assertEqual(reads, [tl.TAIL_BYTES])

    def test_result_has_no_text(self):
        u = tl.read_last_usage(self.transcript([asst()]))
        self.assertNotIn(SECRET_TEXT, json.dumps(u))

    def test_unreadable_raises_oserror(self):
        with self.assertRaises(OSError):
            tl.read_last_usage(self.dir / "missing.jsonl")


class PressureTests(Base):
    def cfg(self, **kw):
        return tl.PressureConfig(windows={"m": 1000}, **kw)

    def lvl(self, tokens, cfg=None):
        p = self.transcript([asst(model="m", i=tokens)])
        return tl.context_pressure(p, cfg or self.cfg())

    def test_ok_level(self):
        r = self.lvl(100)
        self.assertEqual(r["level"].value, "ok")
        self.assertEqual(r["ratio"].value, 0.1)

    def test_warn_edge_below(self):
        self.assertEqual(self.lvl(599)["level"].value, "ok")

    def test_warn_edge_at(self):
        self.assertEqual(self.lvl(600)["level"].value, "warn")

    def test_compact_edges(self):
        self.assertEqual(self.lvl(799)["level"].value, "warn")
        self.assertEqual(self.lvl(800)["level"].value, "compact")

    def test_replace_edges(self):
        self.assertEqual(self.lvl(919)["level"].value, "compact")
        self.assertEqual(self.lvl(920)["level"].value, "replace")

    def test_over_window(self):
        r = self.lvl(5000)
        self.assertEqual(r["level"].value, "replace")
        self.assertEqual(r["ratio"].value, 5.0)

    def test_custom_thresholds(self):
        c = self.cfg(thresholds={"warn": 0.1, "compact": 0.2, "replace": 0.3})
        self.assertEqual(self.lvl(250, c)["level"].value, "compact")

    def test_unknown_model_unavailable_not_guessed(self):
        p = self.transcript([asst(model="mystery-9", i=100)])
        r = tl.context_pressure(p, self.cfg())
        for k in ("window", "ratio", "level"):
            self.assertEqual(r[k].label, Label.UNAVAILABLE, k)
        self.assertEqual(r["tokens"].value, 100)  # tokens are still observed
        self.assertEqual(r["model"].value, "mystery-9")

    def test_missing_model_unavailable(self):
        rec = json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 5}}})
        r = tl.context_pressure(self.transcript([rec]), self.cfg())
        self.assertFalse(r["model"].available)
        self.assertFalse(r["ratio"].available)

    def test_labels(self):
        r = self.lvl(100)
        self.assertEqual(r["tokens"].label, Label.OBSERVED)
        self.assertEqual(r["model"].label, Label.OBSERVED)
        self.assertEqual(r["ratio"].label, Label.INFERRED)

    def test_missing_transcript(self):
        r = tl.context_pressure(self.dir / "none.jsonl")
        self.assertEqual(r["level"].label, Label.UNAVAILABLE)

    def test_none_transcript(self):
        self.assertFalse(tl.context_pressure(None)["tokens"].available)

    def test_no_usage_transcript(self):
        r = tl.context_pressure(self.transcript([user()]))
        self.assertFalse(r["tokens"].available)

    def test_defaults_documented_and_sane(self):
        t = tl.DEFAULT_THRESHOLDS
        self.assertTrue(0 < t["warn"] < t["compact"] < t["replace"] <= 1)
        self.assertIn("claude-sonnet-5-5", tl.DEFAULT_CONTEXT_WINDOWS)

    def test_default_config_not_shared(self):
        a, b = tl.PressureConfig(), tl.PressureConfig()
        a.windows["x"] = 1
        self.assertNotIn("x", b.windows)

    def test_window_lookup_exact_only(self):
        c = tl.PressureConfig(windows={"claude-x": 10})
        self.assertIsNone(c.window_for("claude-x-2"))
        self.assertIsNone(c.window_for(None))

    def test_no_text_in_pressure(self):
        self.assertNotIn(SECRET_TEXT, json.dumps({k: v.to_dict() for k, v in self.lvl(10).items()}))


class UsageProviderTests(Base):
    def test_no_provider_unavailable_with_reason(self):
        r = tl.account_usage(None)
        self.assertEqual(r["usage"].label, Label.UNAVAILABLE)
        self.assertIn("no account-usage source", r["usage"].reason)

    def test_statusline_reported(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": 1790000000},
                                   "seven_day": {"used_percentage": 7.5}}}
        r = tl.account_usage(tl.StatuslineProvider(payload))
        u = r["usage"]
        self.assertEqual(u["five_hour_used_percentage"].label, Label.REPORTED)
        self.assertEqual(u["five_hour_used_percentage"].value, 42)
        self.assertEqual(u["weekly_used_percentage"].value, 7.5)
        self.assertNotIn("weekly_resets_at", u)

    def test_statusline_json_string(self):
        s = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 1}}})
        self.assertIn("five_hour_used_percentage", tl.account_usage(tl.StatuslineProvider(s))["usage"])

    def test_statusline_invalid_unavailable(self):
        for bad in (None, "{x", [], {"rate_limits": 3}, {}, {"rate_limits": {"five_hour": {"used_percentage": "a"}}}):
            r = tl.account_usage(tl.StatuslineProvider(bad))
            self.assertEqual(r["usage"].label, Label.UNAVAILABLE, bad)

    def test_bool_percentage_rejected(self):
        r = tl.account_usage(tl.StatuslineProvider({"rate_limits": {"five_hour": {"used_percentage": True}}}))
        self.assertEqual(r["usage"].label, Label.UNAVAILABLE)

    def test_provider_exception_unavailable(self):
        class Boom(tl.UsageProvider):
            name = "boom"

            def get_usage(self):
                raise RuntimeError("secret detail")
        r = tl.account_usage(Boom())
        self.assertEqual(r["usage"].label, Label.UNAVAILABLE)
        self.assertNotIn("secret detail", r["usage"].reason)

    def test_custom_provider(self):
        class P(tl.UsageProvider):
            name = "p"

            def get_usage(self):
                return {"x": ev.reported(1, "p")}
        self.assertEqual(tl.account_usage(P())["usage"]["x"].value, 1)

    def test_base_provider_not_implemented_is_unavailable(self):
        self.assertEqual(tl.account_usage(tl.UsageProvider())["usage"].label, Label.UNAVAILABLE)


class SnapshotTests(Base):
    def make(self):
        self.session(pid=5, sessionId="abc", cwd="C:\\fake\\proj")
        proj = self.dir / "projects" / tl.cwd_slug("C:\\fake\\proj")
        proj.mkdir(parents=True)
        (proj / "abc.jsonl").write_text(asst(model="claude-sonnet-5-5", i=50000) + "\n", encoding="utf-8")
        return self.dir / "sessions", self.dir / "projects"

    def test_schema_and_serialisable(self):
        sd, pd = self.make()
        s = tl.snapshot(sd, pd, pid_alive=lambda p: True, now=NOW)
        self.assertEqual(s["schema_version"], 1)
        self.assertEqual(s["generated_at"], NOW.isoformat())
        json.dumps(s)
        self.assertEqual(len(s["sessions"]), 1)

    def test_snapshot_context_values(self):
        sd, pd = self.make()
        c = tl.snapshot(sd, pd, pid_alive=lambda p: True, now=NOW)["sessions"][0]["context"]
        self.assertEqual(c["tokens"]["value"], 50000)
        self.assertEqual(c["level"]["value"], "ok")
        self.assertEqual(c["ratio"]["label"], "INFERRED")

    def test_snapshot_no_text_leak(self):
        sd, pd = self.make()
        self.assertNotIn(SECRET_TEXT, json.dumps(tl.snapshot(sd, pd, now=NOW)))

    def test_snapshot_every_value_labelled(self):
        sd, pd = self.make()
        s = tl.snapshot(sd, pd, pid_alive=lambda p: False, now=NOW)["sessions"][0]
        for k, v in s.items():
            if k == "file":
                continue
            vals = v.values() if k == "context" else [v]
            for x in vals:
                self.assertIn(x["label"], {"OBSERVED", "REPORTED", "INFERRED", "UNAVAILABLE"}, k)

    def test_snapshot_without_transcript(self):
        self.session()
        s = tl.snapshot(self.dir / "sessions", self.dir / "projects", pid_alive=lambda p: True, now=NOW)
        self.assertEqual(s["sessions"][0]["context"]["level"]["label"], "UNAVAILABLE")

    def test_snapshot_without_projects_dir(self):
        self.session()
        s = tl.snapshot(self.dir / "sessions", None, pid_alive=lambda p: True, now=NOW)
        self.assertEqual(s["sessions"][0]["context"]["tokens"]["label"], "UNAVAILABLE")

    def test_snapshot_account_usage_unavailable_default(self):
        s = tl.snapshot(self.dir / "empty", now=NOW)
        self.assertEqual(s["sessions"], [])
        self.assertEqual(s["account_usage"]["usage"]["label"], "UNAVAILABLE")

    def test_snapshot_with_statusline(self):
        p = tl.StatuslineProvider({"rate_limits": {"five_hour": {"used_percentage": 10}}})
        s = tl.snapshot(self.dir / "empty", usage_provider=p, now=NOW)
        self.assertEqual(s["account_usage"]["usage"]["five_hour_used_percentage"]["label"], "REPORTED")

    def test_snapshot_corrupt_session_entry(self):
        sd, pd = self.make()
        (sd / "bad.json").write_text("nope", encoding="utf-8")
        s = tl.snapshot(sd, pd, pid_alive=lambda p: True, now=NOW)
        self.assertEqual(len(s["sessions"]), 2)
        self.assertTrue(any("error" in x for x in s["sessions"]))

    def test_find_transcript_fallback_scan(self):
        d = self.dir / "projects" / "other-slug"
        d.mkdir(parents=True)
        (d / "zzz.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertEqual(tl.find_transcript(self.dir / "projects", "zzz", "C:\\unrelated"), d / "zzz.jsonl")

    def test_find_transcript_rejects_path_traversal(self):
        self.assertIsNone(tl.find_transcript(self.dir, "../evil", None))
        self.assertIsNone(tl.find_transcript(self.dir, "", None))

    def test_cwd_slug(self):
        self.assertEqual(tl.cwd_slug("C:\\a b\\c"), "C--a-b-c")


if __name__ == "__main__":
    unittest.main()
