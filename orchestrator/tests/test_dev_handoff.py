"""Development supervisor: handoff, compaction policy and session replacement (T-DEV-003).

Offline only: synthetic data, temp state directories, fake checkers/launcher/heartbeat/retirer and a fake clock; socket
creation is patched to fail and no real process is started.
Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_handoff.py -v
"""
import dataclasses
import json
import socket
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import compaction as cp  # noqa: E402
from dev_supervisor import handoff as ho  # noqa: E402
from dev_supervisor import replacement as rp  # noqa: E402
from dev_supervisor.evidence import inferred, observed, unavailable  # noqa: E402
from dev_supervisor.lifecycle import LaunchHandle  # noqa: E402
from dev_supervisor.state_store import StateStore, utc_iso  # noqa: E402
from dev_supervisor.telemetry import PressureConfig  # noqa: E402
from dev_supervisor.workers import Worker  # noqa: E402

T0 = 1_800_000_000.0
OLD = "11111111-1111-4111-8111-111111111111"
FAKE_KEY = "sk-" + "ant-" + "x" * 24     # assembled at runtime: no literal credential in the repository
SHA = "a" * 40


def _no_net(*a, **k):
    raise AssertionError("network access attempted")


class FakeClock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeCheckers:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = []

    def _r(self, key):
        self.calls.append(key)
        return (key not in self.missing), ("ok" if key not in self.missing else "absent")

    def branch(self, name):
        return self._r(("branch", name))

    def worktree(self, path, branch):
        return self._r(("worktree", path))

    def commit(self, sha):
        return self._r(("commit", sha))

    def pull_request(self, number):
        return self._r(("pr", number))


class RaisingCheckers(FakeCheckers):
    def commit(self, sha):
        raise RuntimeError("boom")


class FakeLauncher:
    def __init__(self, fail_launch=0, exit_code=None):
        self.fail_launch = fail_launch       # number of launches that raise
        self.exit_code = exit_code           # poll result for launched processes (None = running)
        self.launches, self.terminated, self.killed = [], [], []
        self.next_pid = 5000

    def launch(self, spec):
        self.launches.append(spec)
        if self.fail_launch > 0:
            self.fail_launch -= 1
            raise OSError("cannot start")
        self.next_pid += 1
        return LaunchHandle(pid=self.next_pid)

    def owns(self, pid):
        return pid > 5000

    def poll(self, pid):
        return self.exit_code

    def terminate(self, pid):
        self.terminated.append(pid)

    def kill(self, pid):
        self.killed.append(pid)


class FakeHeartbeat:
    def __init__(self, clock, after_s=0.0, never=False, fail_first=0):
        self.clock, self.after_s, self.never, self.fail_first = clock, after_s, never, fail_first
        self.seen = {}
        self.calls = 0

    def first_heartbeat(self, sid, pid):
        self.calls += 1
        if self.never:
            return None
        if pid not in self.seen:
            self.seen[pid] = self.clock()
            if self.fail_first > 0:
                self.fail_first -= 1
                self.seen[pid] = float("inf")
        return self.clock() if self.clock() - self.seen[pid] >= self.after_s else None


class FakeRetirer:
    def __init__(self, ok=True, raises=False):
        self.ok, self.raises = ok, raises
        self.retired = []

    def retire(self, sid):
        if self.raises:
            raise RuntimeError("x")
        self.retired.append(sid)
        return self.ok, "done" if self.ok else "refused"


def _sessions():
    n = [0]

    def make():
        n[0] += 1
        return f"22222222-2222-4222-8222-{n[0]:012d}"
    return make


class Base(unittest.TestCase):
    def setUp(self):
        self._net = mock.patch.object(socket, "socket", _no_net)
        self._net.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = StateStore(Path(self.tmp.name) / "state", clock=self.clock, sleep=lambda s: None)
        self.wt = str(Path(self.tmp.name) / "wt")

    def tearDown(self):
        self._net.stop()
        self.tmp.cleanup()

    def build(self, **over):
        kw = dict(session_id=OLD, role="main", objective="Ship lane L3 safely",
                  decisions=["handoff before replacement"], plan=["write", "verify", "replace"],
                  task_graph=[{"task_id": "T-DEV-003", "title": "handoff", "status": "IN_PROGRESS",
                               "depends_on": ["T-DEV-001", "T-DEV-002"]}],
                  workers=[{"worker_id": "w-1", "role": "worker", "task_id": "T-X", "status": "RUNNING",
                            "branch": "lane/x", "worktree": self.wt, "session_id": None}],
                  branches=["lane/w03-dev-supervisor/L3"],
                  worktrees=[{"path": self.wt, "branch": "lane/w03-dev-supervisor/L3"}],
                  commits=[{"sha": SHA, "branch": "lane/w03-dev-supervisor/L3", "subject": "add handoff"}],
                  pull_requests=[{"number": 42, "state": "MERGED"}],
                  tests=[{"command": "unittest test_dev_handoff.py", "result": "Ran 50 tests: OK",
                          "label": "VERIFIED"}],
                  evidence=[{"claim": "live launch", "label": "NOT TESTED"}],
                  blockers=[], next_actions=["submit lane"],
                  context={"tokens": observed(150000, "transcript_tail", T_ISO),
                           "ratio": inferred(0.75, "transcript_tail", T_ISO)},
                  usage=None, created_at=T_ISO, handoff_id="h-0123456789ab")
        kw.update(over)
        return ho.build_handoff(**kw)

    def write_verify(self, checkers=None, **over):
        rec = ho.write_handoff(self.store, self.build(**over))
        rep = ho.verify_handoff(self.store, rec.handoff_id, checkers or FakeCheckers(), expected_hash=rec.content_hash)
        return rec, rep


T_ISO = utc_iso(T0)


# ====================================================================== handoff

class TestBuild(Base):
    def test_build_captures_all_sections(self):
        h = self.build()
        for k in ("objective", "decisions", "plan", "task_graph", "workers", "branches", "worktrees", "commits",
                  "pull_requests", "tests", "evidence", "blockers", "next_actions", "resources"):
            self.assertIn(k, h)
        self.assertEqual(ho.validate_schema(h), [])

    def test_resource_labels_preserved_and_missing_is_unavailable(self):
        h = self.build()
        ctx = h["resources"]["context"]
        self.assertEqual(ctx["tokens"]["label"], "OBSERVED")
        self.assertEqual(ctx["ratio"]["label"], "INFERRED")
        self.assertEqual(ctx["window"]["label"], "UNAVAILABLE")
        self.assertIsNone(ctx["window"]["value"])
        self.assertEqual(h["resources"]["usage"]["label"], "UNAVAILABLE")
        self.assertIn("does NOT replenish", h["resources"]["note"])

    def test_worker_entries_from_l2_registry_objects(self):
        w = Worker(worker_id="w-abc", role="worker", task_id="T-1", branch="b", worktree=self.wt,
                   owner_stream="s", model="m", status="RUNNING", prompt_file="secret-prompt.txt")
        entries = ho.worker_entries([w])
        self.assertEqual(entries[0]["worker_id"], "w-abc")
        self.assertNotIn("prompt_file", entries[0])
        h = self.build(workers=entries)
        self.assertEqual(h["workers"][0]["status"], "RUNNING")

    def test_refuses_secret(self):
        with self.assertRaises(ho.HandoffError):
            self.build(decisions=["use key " + FAKE_KEY])

    def test_refuses_transcript_like_text(self):
        with self.assertRaises(ho.HandoffError):
            self.build(plan=["transcript: hello everyone and welcome back"])

    def test_refuses_long_text(self):
        with self.assertRaises(ho.HandoffError):
            self.build(objective="x" * (ho.MAX_TEXT + 1))

    def test_refuses_bad_label(self):
        with self.assertRaises(ho.HandoffError):
            self.build(evidence=[{"claim": "c", "label": "PROBABLY"}])

    def test_verified_test_needs_output(self):
        with self.assertRaises(ho.HandoffError):
            self.build(tests=[{"command": "x", "result": "", "label": "VERIFIED"}])

    def test_unavailable_usage_cannot_carry_value(self):
        with self.assertRaises(ho.HandoffError):
            self.build(usage={"label": "UNAVAILABLE", "value": 0.5})

    def test_refuses_bad_sha_and_pr(self):
        with self.assertRaises(ho.HandoffError):
            self.build(commits=[{"sha": "not-a-sha"}])
        with self.assertRaises(ho.HandoffError):
            self.build(pull_requests=[{"number": -1}])

    def test_objective_required(self):
        with self.assertRaises(ho.HandoffError):
            self.build(objective="  ")


class TestWriteVerify(Base):
    def test_write_creates_json_and_markdown_in_store_with_hash(self):
        rec = ho.write_handoff(self.store, self.build())
        jp, mp = Path(rec.json_path), Path(rec.md_path)
        self.assertTrue(jp.is_file() and mp.is_file())
        self.assertTrue(str(jp).startswith(str(self.store.directory)))
        doc = json.loads(jp.read_text(encoding="utf-8"))
        self.assertEqual(doc["content_hash"], ho.content_hash(doc["handoff"]))
        self.assertIn(rec.content_hash, mp.read_text(encoding="utf-8"))
        kinds = [e["kind"] for e in self.store.read_events()[0]]
        self.assertIn("handoff_written", kinds)

    def test_verify_safe(self):
        rec, rep = self.write_verify()
        self.assertEqual(rep.status, ho.SAFE, rep.reasons)
        self.assertTrue(rep.safe)
        self.assertEqual(rep.checked, {"branches": 1, "worktrees": 1, "commits": 1, "pull_requests": 1})
        self.assertIn("handoff_verified", [e["kind"] for e in self.store.read_events()[0]])

    def test_missing_branch_not_safe(self):
        _, rep = self.write_verify(FakeCheckers(missing={("branch", "lane/w03-dev-supervisor/L3")}))
        self.assertEqual(rep.status, ho.NOT_SAFE)
        self.assertIsNone(rep.verified)
        self.assertTrue(any("branch" in r for r in rep.reasons))

    def test_missing_worktree_commit_pr_each_not_safe(self):
        for key in [("worktree", None), ("commit", SHA), ("pr", 42)]:
            if key[0] == "worktree":
                key = ("worktree", self.wt)
            _, rep = self.write_verify(FakeCheckers(missing={key}))
            self.assertEqual(rep.status, ho.NOT_SAFE, key)

    def test_checker_exception_not_safe(self):
        _, rep = self.write_verify(RaisingCheckers())
        self.assertEqual(rep.status, ho.NOT_SAFE)
        self.assertTrue(any("checker failed" in r for r in rep.reasons))

    def test_no_checker_not_safe(self):
        class Partial:
            def branch(self, n):
                return True, "ok"
        _, rep = self.write_verify(Partial())
        self.assertEqual(rep.status, ho.NOT_SAFE)
        self.assertTrue(any("no checker" in r for r in rep.reasons))

    def test_corrupt_json_not_safe(self):
        rec = ho.write_handoff(self.store, self.build())
        Path(rec.json_path).write_bytes(b"{ not json")
        rep = ho.verify_handoff(self.store, rec.handoff_id, FakeCheckers())
        self.assertEqual(rep.status, ho.NOT_SAFE)
        self.assertTrue(any("corrupt" in r for r in rep.reasons))

    def test_missing_json_not_safe(self):
        rep = ho.verify_handoff(self.store, "h-ffffffffffff", FakeCheckers())
        self.assertEqual(rep.status, ho.NOT_SAFE)

    def test_tampered_content_hash_mismatch(self):
        rec = ho.write_handoff(self.store, self.build())
        doc = json.loads(Path(rec.json_path).read_text(encoding="utf-8"))
        doc["handoff"]["objective"] = "something else"
        Path(rec.json_path).write_text(json.dumps(doc), encoding="utf-8")
        rep = ho.verify_handoff(self.store, rec.handoff_id, FakeCheckers())
        self.assertEqual(rep.status, ho.NOT_SAFE)
        self.assertTrue(any("hash mismatch" in r for r in rep.reasons))

    def test_expected_hash_mismatch(self):
        rec = ho.write_handoff(self.store, self.build())
        rep = ho.verify_handoff(self.store, rec.handoff_id, FakeCheckers(), expected_hash="sha256:" + "0" * 64)
        self.assertEqual(rep.status, ho.NOT_SAFE)

    def test_schema_invalid_not_safe_and_skips_checkers(self):
        rec = ho.write_handoff(self.store, self.build())
        doc = json.loads(Path(rec.json_path).read_text(encoding="utf-8"))
        del doc["handoff"]["next_actions"]
        doc["content_hash"] = ho.content_hash(doc["handoff"])
        Path(rec.json_path).write_text(json.dumps(doc), encoding="utf-8")
        ch = FakeCheckers()
        rep = ho.verify_handoff(self.store, rec.handoff_id, ch)
        self.assertEqual(rep.status, ho.NOT_SAFE)
        self.assertTrue(any(r.startswith("schema:") for r in rep.reasons))
        self.assertEqual(ch.calls, [])

    def test_missing_markdown_not_safe(self):
        rec = ho.write_handoff(self.store, self.build())
        Path(rec.md_path).unlink()
        rep = ho.verify_handoff(self.store, rec.handoff_id, FakeCheckers())
        self.assertEqual(rep.status, ho.NOT_SAFE)

    def test_written_files_hold_no_secret_after_injection_attempt(self):
        rec = ho.write_handoff(self.store, self.build())
        doc = json.loads(Path(rec.json_path).read_text(encoding="utf-8"))
        doc["handoff"]["plan"].append("token=" + FAKE_KEY)
        doc["content_hash"] = ho.content_hash(doc["handoff"])
        Path(rec.json_path).write_text(json.dumps(doc), encoding="utf-8")
        rep = ho.verify_handoff(self.store, rec.handoff_id, FakeCheckers())
        self.assertEqual(rep.status, ho.NOT_SAFE)
        self.assertNotIn(FAKE_KEY, json.dumps(rep.to_dict()))

    def test_write_refuses_malformed(self):
        h = self.build()
        h["kind"] = "other"
        with self.assertRaises(ho.HandoffError):
            ho.write_handoff(self.store, h)


class TestVerifiedSeal(Base):
    def test_cannot_construct_verified_handoff_directly(self):
        with self.assertRaises(ho.UnverifiedHandoffError):
            ho.VerifiedHandoff("h-0123456789ab", "sha256:x", "a", "b", OLD, T0)

    def test_copy_with_replace_is_rejected(self):
        _, rep = self.write_verify()
        forged = dataclasses.replace(rep.verified, verified_at=self.clock() + 1000)
        with self.assertRaises(ho.UnverifiedHandoffError):
            ho.require_verified(forged, now=self.clock(), max_age_s=300)

    def test_stale_verification_rejected(self):
        _, rep = self.write_verify()
        ho.require_verified(rep.verified, now=self.clock() + 300, max_age_s=300)  # edge: exactly at the limit
        with self.assertRaises(ho.UnverifiedHandoffError):
            ho.require_verified(rep.verified, now=self.clock() + 301, max_age_s=300)

    def test_none_rejected(self):
        with self.assertRaises(ho.UnverifiedHandoffError):
            ho.require_verified(None, now=T0, max_age_s=300)


class TestRealCheckers(Base):
    def test_git_ref_checker_uses_argument_lists(self):
        calls = []

        class R:
            def __init__(self, code, out=""):
                self.returncode, self.stdout = code, out

        def run(argv, **kw):
            calls.append((argv, kw))
            return R(0)
        c = ho.GitRefChecker("repo", run=run, pr_checker=lambda n: (True, "ok"))
        self.assertTrue(c.branch("lane/x")[0])
        self.assertTrue(c.commit(SHA)[0])
        self.assertFalse(c.branch("bad..name")[0])
        self.assertFalse(c.commit("zz")[0])
        self.assertTrue(all(kw.get("shell") is False and isinstance(a, list) for a, kw in calls))

    def test_gh_pr_checker(self):
        class R:
            returncode, stdout = 0, '{"number": 7}'
        self.assertTrue(ho.GhPrChecker(run=lambda a, **k: R())(7)[0])
        self.assertFalse(ho.GhPrChecker(run=lambda a, **k: R())(8)[0])
        self.assertFalse(ho.GhPrChecker(run=lambda a, **k: R())(0)[0])


# ====================================================================== compaction

def _pressure(ratio, label="INFERRED", ts=None):
    ts = ts or datetime.now(timezone.utc).isoformat()
    if label == "UNAVAILABLE":
        r = unavailable("model not in configured window table", "transcript_tail", ts)
        return {"tokens": r, "ratio": r, "level": r}
    return {"tokens": observed(int(ratio * 200000), "transcript_tail", ts), "ratio": inferred(ratio, "t", ts)}


class TestCompaction(Base):
    def setUp(self):
        super().setUp()
        self.now = datetime.now(timezone.utc).timestamp()
        self.clock.t = self.now
        _, rep = self.write_verify()
        self.v = rep.verified

    def d(self, ratio, **kw):
        kw.setdefault("idle", True)
        kw.setdefault("verified", self.v)
        return cp.decide(_pressure(ratio) if not isinstance(ratio, dict) else ratio, now=self.now, **kw)

    def test_threshold_edges_default(self):
        self.assertEqual(self.d(0.5999).action, cp.NONE)
        self.assertEqual(self.d(0.60).action, cp.WARN)
        self.assertEqual(self.d(0.7999).action, cp.WARN)
        self.assertEqual(self.d(0.80).action, cp.COMPACT)
        self.assertEqual(self.d(0.9199).action, cp.COMPACT)
        self.assertEqual(self.d(0.92).action, cp.REPLACE)

    def test_configured_thresholds_used(self):
        cfg = PressureConfig(thresholds={"warn": 0.3, "compact": 0.5, "replace": 0.7})
        self.assertEqual(self.d(0.55, config=cfg).action, cp.COMPACT)
        self.assertEqual(self.d(0.55, config=cfg).thresholds["compact"], 0.5)

    def test_compact_allowed_when_idle_and_verified(self):
        dec = self.d(0.85)
        self.assertTrue(dec.allowed)
        self.assertIn("verified just now", dec.reason)
        self.assertEqual(dec.handoff_id, self.v.handoff_id)

    def test_compact_blocked_when_busy(self):
        dec = self.d(0.85, idle=False)
        self.assertEqual(dec.action, cp.COMPACT)
        self.assertFalse(dec.allowed)
        self.assertIn("session_not_idle", dec.blocked_by)

    def test_compact_blocked_without_verified_handoff(self):
        dec = self.d(0.85, verified=None)
        self.assertFalse(dec.allowed)
        self.assertIn("no_fresh_verified_handoff", dec.blocked_by)

    def test_compact_blocked_when_handoff_old(self):
        dec = cp.decide(_pressure(0.85), idle=True, verified=self.v, now=self.now + 301, handoff_max_age_s=300,
                        pressure_max_age_s=10_000)
        self.assertIn("no_fresh_verified_handoff", dec.blocked_by)

    def test_idle_unavailable_is_not_idle(self):
        dec = self.d(0.85, idle=unavailable("no activity probe"))
        self.assertFalse(dec.allowed)
        self.assertIn("idle_state_unavailable", dec.blocked_by)

    def test_unavailable_pressure_says_so_and_never_guesses(self):
        dec = self.d(_pressure(0, label="UNAVAILABLE"))
        self.assertEqual(dec.action, cp.NONE)
        self.assertIsNone(dec.ratio)
        self.assertEqual(dec.pressure_label, "UNAVAILABLE")
        self.assertIn("UNAVAILABLE", dec.reason)
        self.assertFalse(dec.allowed)

    def test_no_pressure_at_all(self):
        dec = cp.decide(None, idle=True, verified=self.v, now=self.now)
        self.assertEqual(dec.action, cp.NONE)
        self.assertIn("UNAVAILABLE", dec.reason)

    def test_stale_pressure_gives_none(self):
        old = datetime.fromtimestamp(self.now - 3600, tz=timezone.utc).isoformat()
        dec = self.d(_pressure(0.95, ts=old))
        self.assertEqual(dec.action, cp.NONE)
        self.assertIn("pressure_stale", dec.blocked_by)

    def test_request_compaction_records_and_calls_requester(self):
        calls = []

        class Req:
            def request_compaction(self, sid, hid):
                calls.append((sid, hid))
                return True, "accepted"
        out = cp.request_compaction(self.store, self.d(0.85, session_id=OLD), Req())
        self.assertTrue(out["requested"] and out["ok"])
        self.assertEqual(calls, [(OLD, self.v.handoff_id)])
        kinds = [e["kind"] for e in self.store.read_events()[0]]
        self.assertIn("compaction_decision", kinds)
        self.assertIn("compaction_requested", kinds)
        self.assertIn("does NOT replenish", out["usage_note"])

    def test_request_compaction_refused_when_blocked(self):
        calls = []

        class Req:
            def request_compaction(self, sid, hid):
                calls.append(sid)
                return True, ""
        for dec in (self.d(0.85, idle=False), self.d(0.5), self.d(0.95), self.d(_pressure(0, "UNAVAILABLE"))):
            out = cp.request_compaction(self.store, dec, Req())
            self.assertFalse(out["requested"])
        self.assertEqual(calls, [])
        ev = self.store.read_events()[0]
        self.assertEqual(sum(1 for e in ev if e["kind"] == "compaction_refused"), 4)
        recorded = [e for e in ev if e["kind"] == "compaction_decision"]
        self.assertTrue(all(e["data"]["reason"] for e in recorded))


# ====================================================================== replacement

class TestReplacement(Base):
    def setUp(self):
        super().setUp()
        rp.register_session(self.store, OLD, "main")
        self.checkers = FakeCheckers()
        self.rec, rep = self.write_verify(self.checkers)
        self.v = rep.verified
        self.launcher = FakeLauncher()
        self.hb = FakeHeartbeat(self.clock, after_s=10)
        self.retirer = FakeRetirer()

    def sleep(self, s):
        self.clock.advance(s)

    def replace(self, verified="default", **kw):
        args = dict(old_session_id=OLD, role="main", model="claude-opus-4-5", cwd=self.wt,
                    verified=self.v if verified == "default" else verified, checkers=self.checkers,
                    heartbeat=self.hb, retirer=self.retirer, clock=self.clock, sleep=self.sleep,
                    session_factory=_sessions(),
                    policy=rp.ReplacementPolicy(heartbeat_timeout_s=60, heartbeat_poll_s=5))
        args.update(kw)
        return rp.replace_session(self.store, self.launcher, **args)

    def sessions(self):
        return rp.load_sessions(self.store)["sessions"]

    def test_happy_path(self):
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.REPLACED)
        self.assertTrue(res.old_session_cleared)
        self.assertEqual(self.retirer.retired, [OLD])
        s = self.sessions()
        self.assertEqual(s[OLD]["status"], rp.SessionStatus.RETIRED)
        self.assertEqual(s[res.new_session_id]["status"], rp.SessionStatus.ACTIVE)
        self.assertTrue(s[res.new_session_id]["authoritative"])
        self.assertFalse(s[OLD]["authoritative"])
        self.assertEqual(res.authoritative_session_id, res.new_session_id)

    def test_result_and_module_say_usage_not_replenished(self):
        res = self.replace()
        self.assertIn("does NOT replenish account usage", res.usage_note)
        self.assertIn("does NOT replenish account usage", res.to_dict()["usage_note"])
        self.assertIn("does NOT replenish account usage", rp.__doc__)

    def test_prompt_points_to_verified_handoff(self):
        self.replace()
        spec = self.launcher.launches[0]
        text = Path(spec.stdin_path).read_text(encoding="utf-8")
        self.assertIn(self.v.json_path, text)
        self.assertIn(self.v.content_hash, text)
        self.assertNotIn("Ship lane L3", text)  # pointer only, no handoff content
        self.assertIn("--session-id", spec.argv)
        self.assertNotIn(text, " ".join(spec.argv))

    def test_never_clear_without_verified_handoff(self):
        for bad in (None, "h-0123456789ab", {"handoff_id": "h-0123456789ab"}):
            with self.assertRaises(ho.UnverifiedHandoffError):
                self.replace(verified=bad)
        self.assertEqual(self.retirer.retired, [])
        self.assertEqual(self.launcher.launches, [])
        self.assertEqual(self.sessions()[OLD]["status"], rp.SessionStatus.ACTIVE)

    def test_forged_copy_rejected(self):
        forged = dataclasses.replace(self.v)
        with self.assertRaises(ho.UnverifiedHandoffError):
            self.replace(verified=forged)
        self.assertEqual(self.retirer.retired, [])

    def test_not_safe_report_cannot_be_used(self):
        _, rep = self.write_verify(FakeCheckers(missing={("pr", 42)}))
        with self.assertRaises(ho.UnverifiedHandoffError):
            self.replace(verified=rep.verified)
        self.assertEqual(self.retirer.retired, [])

    def test_stale_verification_refused(self):
        self.clock.advance(301)
        with self.assertRaises(ho.UnverifiedHandoffError):
            self.replace()
        self.assertEqual(self.launcher.launches, [])

    def test_handoff_for_other_session_refused(self):
        _, rep = self.write_verify(session_id="33333333-3333-4333-8333-333333333333", handoff_id="h-aaaaaaaaaaaa")
        with self.assertRaises(ho.UnverifiedHandoffError):
            self.replace(verified=rep.verified)

    def test_handoff_tampered_after_verify_refused(self):
        doc = json.loads(Path(self.rec.json_path).read_text(encoding="utf-8"))
        doc["handoff"]["next_actions"] = ["something different"]
        doc["content_hash"] = ho.content_hash(doc["handoff"])
        Path(self.rec.json_path).write_text(json.dumps(doc), encoding="utf-8")
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.REFUSED_HANDOFF_NOT_SAFE)
        self.assertEqual(self.launcher.launches, [])
        self.assertEqual(self.retirer.retired, [])
        self.assertEqual(res.authoritative_session_id, OLD)

    def test_branch_deleted_after_verify_refused(self):
        self.checkers.missing.add(("branch", "lane/w03-dev-supervisor/L3"))
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.REFUSED_HANDOFF_NOT_SAFE)
        self.assertEqual(self.retirer.retired, [])

    def test_heartbeat_timeout_rolls_back(self):
        self.hb.never = True
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.ROLLED_BACK)
        self.assertEqual(len(res.attempts), 2)
        self.assertEqual(self.retirer.retired, [])
        s = self.sessions()
        self.assertEqual(s[OLD]["status"], rp.SessionStatus.ACTIVE)
        self.assertTrue(s[OLD]["authoritative"])
        self.assertEqual(res.authoritative_session_id, OLD)
        self.assertEqual(len(self.launcher.terminated), 2)
        for a in res.attempts:
            self.assertEqual(s[a["session_id"]]["status"], rp.SessionStatus.FAILED)

    def test_old_is_retiring_and_authoritative_during_launch(self):
        seen = {}
        orig = self.launcher.launch

        def spy(spec):
            seen.update(self.sessions()[OLD])
            return orig(spec)
        self.launcher.launch = spy
        self.replace()
        self.assertEqual(seen["status"], rp.SessionStatus.RETIRING)
        self.assertTrue(seen["authoritative"])

    def test_launch_error_then_retry_succeeds(self):
        self.launcher.fail_launch = 1
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.REPLACED)
        self.assertEqual(len(res.attempts), 2)
        self.assertIn("launch_error", res.attempts[0]["reason"])

    def test_early_exit_fails_attempt(self):
        self.launcher.exit_code = 1
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.ROLLED_BACK)
        self.assertTrue(all(a["reason"] == "exited:1" for a in res.attempts))

    def test_bounded_attempts(self):
        self.hb.never = True
        res = self.replace(policy=rp.ReplacementPolicy(max_attempts=3, heartbeat_timeout_s=10, heartbeat_poll_s=5))
        self.assertEqual(len(res.attempts), 3)
        self.assertEqual(len(self.launcher.launches), 3)

    def test_repeated_failure_stops(self):
        self.hb.never = True
        for _ in range(3):
            _, rep = self.write_verify(self.checkers)
            self.assertEqual(self.replace(verified=rep.verified).status, rp.ReplacementStatus.ROLLED_BACK)
        launches = len(self.launcher.launches)
        _, rep = self.write_verify(self.checkers)
        res = self.replace(verified=rep.verified)
        self.assertEqual(res.status, rp.ReplacementStatus.BLOCKED)
        self.assertEqual(len(self.launcher.launches), launches)
        kinds = [e["kind"] for e in self.store.read_events()[0]]
        self.assertIn("replacement_blocked", kinds)
        rp.reset_failures(self.store, OLD, "human checked the launcher")
        self.hb.never = False
        _, rep = self.write_verify(self.checkers)
        self.assertEqual(self.replace(verified=rep.verified).status, rp.ReplacementStatus.REPLACED)

    def test_reset_needs_reason(self):
        with self.assertRaises(rp.ReplacementError):
            rp.reset_failures(self.store, OLD, " ")

    def test_retirer_refuses_old_not_cleared(self):
        self.retirer.ok = False
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.REPLACED_OLD_NOT_CLEARED)
        self.assertFalse(res.old_session_cleared)
        self.assertEqual(self.sessions()[OLD]["status"], rp.SessionStatus.RETIRE_FAILED)

    def test_retirer_exception_old_not_cleared(self):
        self.retirer.raises = True
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.REPLACED_OLD_NOT_CLEARED)

    def test_handoff_removed_before_retire_keeps_old(self):
        orig = self.hb.first_heartbeat

        def hb(sid, pid):
            r = orig(sid, pid)
            if r is not None:
                Path(self.rec.json_path).unlink()
            return r
        self.hb.first_heartbeat = hb
        res = self.replace()
        self.assertEqual(res.status, rp.ReplacementStatus.REPLACED_OLD_NOT_CLEARED)
        self.assertEqual(self.retirer.retired, [])

    def test_events_recorded_in_order(self):
        self.replace()
        kinds = [e["kind"] for e in self.store.read_events()[0]]
        order = ["session_retiring", "replacement_launch_begin", "replacement_confirmed", "session_retired"]
        idx = [kinds.index(k) for k in order]
        self.assertEqual(idx, sorted(idx))

    def test_worker_role_replacement(self):
        res = self.replace(role="worker")
        self.assertEqual(res.status, rp.ReplacementStatus.REPLACED)
        self.assertEqual(self.sessions()[res.new_session_id]["role"], "worker")

    def test_no_secrets_in_store_files(self):
        self.replace()
        for p in Path(self.store.directory).rglob("*"):
            if p.is_file():
                self.assertNotIn(FAKE_KEY, p.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
