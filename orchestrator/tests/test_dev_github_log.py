"""Tests for dev_supervisor.github_log. Synthetic data only; fake runner; no gh, no network, no real paths/secrets.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_github_log.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import github_log as gl
from dev_supervisor.github_log import (Event, EventKind, FlushResult, GhRunner, GitHubLogger, RunnerError,
                                       SensitiveContentError, build_subtask_body, render_comment, render_event,
                                       scrub, sensitivity_flags)

CLK = lambda: "2026-11-18T00:00:00Z"
# synthetic secret-looking values assembled at runtime so no secret-shaped literal sits in the file
FAKE_GH = "ghp_" + "A1b2C3d4E5f6G7h8"
FAKE_SK = "sk-" + "abcdefgh12345678"
WIN_PATH = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "proj" + "\\" + "f.txt"
POSIX_PATH = "/" + "home" + "/someone/proj/f.txt"


class FakeRunner:
    def __init__(self):
        self.comments = []        # (issue, body)
        self.issues = []          # (title, body, labels)
        self.closed = []
        self.fail = []            # queue of exceptions raised by the next comment() calls
        self.fail_after_post = 0  # ambiguous failures: comment is stored, then an error is raised
        self.next_issue = 100
        self.view_calls = 0
        self.view_fails = False

    def comment(self, issue, body):
        if self.fail:
            raise self.fail.pop(0)
        self.comments.append((issue, body))
        if self.fail_after_post:
            self.fail_after_post -= 1
            raise RunnerError("connection reset", True)

    def create_issue(self, title, body, labels=()):
        if self.fail:
            raise self.fail.pop(0)
        self.issues.append((title, body, list(labels)))
        self.next_issue += 1
        return self.next_issue

    def close_issue(self, issue, comment=""):
        if self.fail:
            raise self.fail.pop(0)
        self.closed.append((issue, comment))

    def event_ids(self, issue):
        self.view_calls += 1
        if self.view_fails:
            raise RunnerError("view failed", True)
        found = set()
        for i, b in self.comments:
            if i == issue:
                found.update(gl._MARKER_RE.findall(b))
        return found


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name) / "state"
        self.runner = FakeRunner()
        self.sleeps = []
        self.t = [0.0]

    def sleep(self, s):
        self.sleeps.append(s)
        self.t[0] += s

    def logger(self, **kw):
        kw.setdefault("min_interval", 0)
        kw.setdefault("clock", CLK)
        return GitHubLogger(self.runner, self.dir, default_issue=50, sleep=self.sleep,
                            monotonic=lambda: self.t[0], **kw)

    def ev(self, title="step one", **kw):
        kw.setdefault("kind", EventKind.STEP_DONE)
        kw.setdefault("task_id", "T-DEV-006")
        return Event(title=title, **kw)


class ScrubTests(unittest.TestCase):
    def test_github_token(self):
        self.assertNotIn(FAKE_GH, scrub("pushed with " + FAKE_GH))

    def test_sk_key(self):
        self.assertNotIn(FAKE_SK, scrub("key " + FAKE_SK))

    def test_key_value(self):
        out = scrub("password=hunter2xx and api_key: abc123")
        self.assertNotIn("hunter2xx", out)
        self.assertNotIn("abc123", out)

    def test_bearer(self):
        self.assertNotIn("abcdef123456", scrub("Authorization: Bearer abcdef123456"))

    def test_url_userinfo_and_query(self):
        out = scrub("https://user:pw@example.com/p?sig=zzz&x=1")
        self.assertNotIn("user:pw", out)
        self.assertNotIn("zzz", out)

    def test_windows_path(self):
        out = scrub("see " + WIN_PATH + " ok")
        self.assertNotIn("someone", out)
        self.assertIn("<path>", out)

    def test_posix_path(self):
        self.assertNotIn("someone", scrub("at " + POSIX_PATH))

    def test_long_opaque(self):
        self.assertIn("<redacted>", scrub("blob " + "Zq9" * 20))

    def test_hex_commit_kept(self):
        sha = "0123456789abcdef" * 2 + "01234567"
        self.assertIn(sha, scrub("commit " + sha))

    def test_length_cap(self):
        self.assertEqual(len(scrub("x " * 1000, 50)), 50)

    def test_control_chars_removed(self):
        self.assertNotIn("\x00", scrub("a\x00b\x07c"))

    def test_idempotent(self):
        once = scrub("token=abc123 " + WIN_PATH)
        self.assertEqual(once, scrub(once))

    def test_plain_text_untouched(self):
        self.assertEqual(scrub("Implemented the spool, 12 tests pass"), "Implemented the spool, 12 tests pass")


class FlagTests(unittest.TestCase):
    def test_secret(self):
        self.assertIn("secret", sensitivity_flags("token=abc123"))

    def test_redacted_value_not_flagged(self):
        self.assertEqual(sensitivity_flags("token=<redacted>"), [])

    def test_path(self):
        self.assertIn("personal_path", sensitivity_flags(WIN_PATH))

    def test_transcript_marker(self):
        self.assertIn("transcript", sensitivity_flags("transcript: hello there"))
        self.assertIn("transcript", sensitivity_flags("[transcript] words"))

    def test_clean(self):
        self.assertEqual(sensitivity_flags("tests: 40 passed, branch lane/w03/L6"), [])

    def test_relative_path_ok(self):
        self.assertEqual(sensitivity_flags("orchestrator/dev_supervisor/github_log.py"), [])


class EventTests(Base):
    def test_all_kinds_exist(self):
        want = {"task_started", "step_done", "decision", "test_run", "failure", "retry", "provider_change",
                "handoff", "compaction", "blocker", "pr_opened"}
        self.assertEqual({k.value for k in EventKind}, want)

    def test_kind_from_string(self):
        self.assertIs(Event("decision", "T", "x").kind, EventKind.DECISION)

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            Event("nonsense", "T", "x")

    def test_fields_scrubbed(self):
        e = Event("failure", "T", "boom " + FAKE_GH, detail="at " + WIN_PATH, evidence=("log " + FAKE_SK,))
        blob = json.dumps(e.to_dict())
        for bad in (FAKE_GH, FAKE_SK, "someone"):
            self.assertNotIn(bad, blob)
        self.assertEqual(set(e.flags), {"secret", "personal_path"})

    def test_deterministic_id(self):
        a = Event("step_done", "T", "x", ts="t1", issue=5)
        b = Event("step_done", "T", "x", ts="t1", issue=5)
        self.assertEqual(a.event_id, b.event_id)
        self.assertNotEqual(a.event_id, Event("step_done", "T", "y", ts="t1", issue=5).event_id)

    def test_explicit_id_validated(self):
        with self.assertRaises(ValueError):
            Event("step_done", "T", "x", event_id="not hex!")

    def test_bad_issue(self):
        with self.assertRaises(ValueError):
            Event("step_done", "T", "x", issue=-1)

    def test_evidence_capped(self):
        e = Event("step_done", "T", "x", evidence=tuple(f"e{i}" for i in range(50)))
        self.assertEqual(len(e.evidence), 10)

    def test_roundtrip(self):
        e = Event("test_run", "T", "x", detail="d", evidence=("a", "b"), source="s", ts="t", issue=7)
        self.assertEqual(Event.from_dict(e.to_dict()), e)

    def test_render_has_marker_source_evidence(self):
        e = Event("pr_opened", "T-1", "PR ready", evidence=("pr:#12", "commit:abc1234"), source="worker-a",
                  ts="2026-11-18T00:00:00Z")
        md = render_event(e)
        self.assertIn("**pr_opened**", md)
        self.assertIn("worker-a", md)
        self.assertIn("- pr:#12", md)
        self.assertIn(f"<!-- dev-event:{e.event_id} -->", md)

    def test_render_neutralises_mentions_and_forged_marker(self):
        e = Event("decision", "T", "hi @someoneelse", detail="<!-- dev-event:deadbeefdeadbeef -->")
        md = render_event(e)
        self.assertNotIn("@someoneelse", md)
        self.assertEqual(len(gl._MARKER_RE.findall(md)), 1)

    def test_render_comment_batches(self):
        md = render_comment([self.ev("a"), self.ev("b")])
        self.assertEqual(len(gl._MARKER_RE.findall(md)), 2)

    def test_render_concise(self):
        self.assertLess(len(render_event(self.ev("x"))), 400)


class LogAndFlushTests(Base):
    def test_post_success(self):
        lg = self.logger()
        self.assertEqual(lg.log(self.ev()), "posted")
        self.assertEqual(len(self.runner.comments), 1)
        self.assertEqual(self.runner.comments[0][0], 50)
        self.assertEqual(lg.pending(), [])

    def test_explicit_issue_used(self):
        lg = self.logger()
        lg.log(self.ev(issue=77))
        self.assertEqual(self.runner.comments[0][0], 77)

    def test_no_issue_no_default(self):
        lg = GitHubLogger(self.runner, self.dir, clock=CLK)
        with self.assertRaises(ValueError):
            lg.log(self.ev())

    def test_posted_body_is_clean(self):
        lg = self.logger()
        lg.log(self.ev("done", detail="ran 40 tests"))
        self.assertEqual(sensitivity_flags(self.runner.comments[0][1]), [])

    def test_spooled_before_network(self):
        lg = self.logger()
        self.assertEqual(lg.log(self.ev(), flush=False), "spooled")
        self.assertEqual(len(lg.pending()), 1)
        self.assertEqual(self.runner.comments, [])

    def test_batching_one_comment(self):
        lg = self.logger(max_batch=5)
        for i in range(3):
            lg.log(self.ev(f"s{i}"), flush=False)
        res = lg.flush()
        self.assertEqual(len(res.posted), 3)
        self.assertEqual(len(self.runner.comments), 1)

    def test_batch_size_limit(self):
        lg = self.logger(max_batch=2)
        for i in range(5):
            lg.log(self.ev(f"s{i}"), flush=False)
        lg.flush()
        self.assertEqual(len(self.runner.comments), 3)

    def test_batch_char_limit(self):
        lg = self.logger(max_batch=50, max_chars=300)
        for i in range(4):
            lg.log(self.ev(f"s{i}", detail="d" * 150), flush=False)
        lg.flush()
        self.assertGreater(len(self.runner.comments), 1)

    def test_batches_split_per_issue(self):
        lg = self.logger()
        lg.log(self.ev("a", issue=1), flush=False)
        lg.log(self.ev("b", issue=2), flush=False)
        lg.flush()
        self.assertEqual(sorted(i for i, _ in self.runner.comments), [1, 2])

    def test_order_preserved(self):
        lg = self.logger(max_batch=10)
        for t in ("first", "second", "third"):
            lg.log(self.ev(t), flush=False)
        lg.flush()
        body = self.runner.comments[0][1]
        self.assertLess(body.index("first"), body.index("second"))
        self.assertLess(body.index("second"), body.index("third"))

    def test_rate_limit_sleeps(self):
        lg = self.logger(min_interval=5.0, max_batch=1)
        lg.log(self.ev("a"), flush=False)
        lg.log(self.ev("b"), flush=False)
        lg.flush()
        self.assertEqual(len(self.runner.comments), 2)
        self.assertTrue(any(abs(s - 5.0) < 1e-6 for s in self.sleeps))

    def test_no_rate_wait_if_interval_elapsed(self):
        lg = self.logger(min_interval=5.0, max_batch=1)
        lg.log(self.ev("a"))
        self.t[0] += 10
        lg.log(self.ev("b"))
        self.assertEqual(self.sleeps, [])


class RetryAndOutageTests(Base):
    def test_transient_retry_then_success(self):
        self.runner.fail = [RunnerError("timeout", True), RunnerError("502", True)]
        lg = self.logger()
        self.assertEqual(lg.log(self.ev()), "posted")
        self.assertEqual(len(self.runner.comments), 1)

    def test_backoff_grows_and_is_capped(self):
        self.runner.fail = [RunnerError("x", True)] * 3
        lg = self.logger(max_attempts=4, backoff_base=1.0, backoff_cap=3.0)
        lg.log(self.ev())
        self.assertEqual(self.sleeps, [1.0, 2.0, 3.0])

    def test_bounded_attempts(self):
        self.runner.fail = [RunnerError("down", True)] * 50
        lg = self.logger(max_attempts=3)
        self.assertEqual(lg.log(self.ev()), "spooled")
        self.assertEqual(len(self.runner.fail), 47)

    def test_outage_spools_then_flush_delivers(self):
        self.runner.fail = [RunnerError("network", True)] * 50
        lg = self.logger(max_attempts=2)
        for i in range(3):
            lg.log(self.ev(f"s{i}"))
        self.assertEqual(len(lg.pending()), 3)
        self.assertEqual(self.runner.comments, [])
        self.runner.fail = []
        res = lg.flush()
        self.assertEqual(len(res.posted), 3)
        self.assertEqual(res.remaining, 0)
        self.assertEqual(lg.pending(), [])

    def test_spool_survives_new_logger_instance(self):
        self.runner.fail = [RunnerError("network", True)] * 50
        self.logger(max_attempts=1).log(self.ev("kept"))
        self.runner.fail = []
        res = self.logger().flush()
        self.assertEqual(len(res.posted), 1)
        self.assertIn("kept", self.runner.comments[0][1])

    def test_permanent_error_not_retried(self):
        self.runner.fail = [RunnerError("issue not found", False)]
        lg = self.logger(max_attempts=5)
        lg.log(self.ev())
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(lg.pending()), 1)

    def test_failure_recorded_in_spool_scrubbed(self):
        self.runner.fail = [RunnerError("denied token=abc123secret", False)]
        lg = self.logger()
        lg.log(self.ev())
        blob = self.spool_text()
        self.assertNotIn("abc123secret", blob)
        self.assertEqual(json.loads(blob)["events"][0]["attempts"], 1)

    def test_unexpected_exception_contained(self):
        self.runner.fail = [ValueError("weird " + FAKE_GH)] * 10
        lg = self.logger(max_attempts=2)
        self.assertEqual(lg.log(self.ev()), "spooled")
        self.assertNotIn(FAKE_GH, self.spool_text())

    def test_failed_chunk_blocks_later_chunks_same_issue(self):
        self.runner.fail = [RunnerError("down", False)]
        lg = self.logger(max_batch=1)
        for i in range(3):
            lg.log(self.ev(f"s{i}"), flush=False)
        res = lg.flush()
        self.assertEqual(self.runner.comments, [])
        self.assertEqual(res.remaining, 3)

    def test_other_issue_still_posts(self):
        self.runner.fail = [RunnerError("down", False)]
        lg = self.logger(max_batch=1)
        lg.log(self.ev("a", issue=1), flush=False)
        lg.log(self.ev("b", issue=2), flush=False)
        res = lg.flush()
        self.assertEqual([i for i, _ in self.runner.comments], [2])
        self.assertEqual(res.remaining, 1)

    def spool_text(self):
        return (self.dir / "spool.json").read_text(encoding="utf-8")


class IdempotencyTests(Base):
    def test_same_event_twice_posts_once(self):
        lg = self.logger()
        e = self.ev(ts="t", issue=50)
        self.assertEqual(lg.log(e), "posted")
        self.assertEqual(lg.log(e), "duplicate")
        self.assertEqual(len(self.runner.comments), 1)

    def test_duplicate_in_spool(self):
        lg = self.logger()
        e = self.ev(ts="t", issue=50)
        lg.log(e, flush=False)
        self.assertEqual(lg.log(e, flush=False), "duplicate")
        self.assertEqual(len(lg.pending()), 1)

    def test_duplicate_across_restart(self):
        e = self.ev(ts="t", issue=50)
        self.logger().log(e)
        self.assertEqual(self.logger().log(e), "duplicate")
        self.assertEqual(len(self.runner.comments), 1)

    def test_ambiguous_failure_not_duplicated(self):
        self.runner.fail_after_post = 1   # comment is stored, then the reply is "lost"
        lg = self.logger(max_attempts=3)
        self.assertEqual(lg.log(self.ev()), "posted")
        self.assertEqual(len(self.runner.comments), 1)
        self.assertGreaterEqual(self.runner.view_calls, 1)

    def test_marker_lookup_failure_falls_back_to_repost(self):
        self.runner.view_fails = True
        self.runner.fail = [RunnerError("down", True)]
        lg = self.logger(max_attempts=3)
        self.assertEqual(lg.log(self.ev()), "posted")

    def test_crash_between_post_and_spool_update(self):
        lg = self.logger()
        e = self.ev(ts="t", issue=50)
        lg.log(e, flush=False)
        lg._save_ledger([e.event_id])      # simulate: posted, ledger written, spool not yet cleaned
        res = self.logger().flush()
        self.assertEqual(self.runner.comments, [])
        self.assertEqual(res.already_posted, [e.event_id])
        self.assertEqual(self.logger().pending(), [])

    def test_explicit_id_sticky_when_filling_ts(self):
        lg = self.logger()
        e1 = Event("step_done", "T", "x", event_id="abcdef0123456789")
        self.assertEqual(lg.log(e1), "posted")
        self.assertEqual(lg.log(Event("step_done", "T", "x", event_id="abcdef0123456789")), "duplicate")

    def test_ledger_capped(self):
        lg = self.logger()
        lg.LEDGER_CAP = 3
        lg._save_ledger([f"{i:08x}" for i in range(10)])
        self.assertEqual(len(lg._ledger()), 3)


class RefusalTests(Base):
    def test_secret_refused_not_posted(self):
        lg = self.logger()
        self.assertEqual(lg.log(self.ev("leak", detail="token=abc123xyz")), "refused")
        self.assertEqual(self.runner.comments, [])
        self.assertEqual(lg.pending(), [])

    def test_personal_path_refused(self):
        self.assertEqual(self.logger().log(self.ev("p", detail=WIN_PATH)), "refused")
        self.assertEqual(self.runner.comments, [])

    def test_transcript_refused(self):
        self.assertEqual(self.logger().log(self.ev("t", detail="transcript: some spoken words")), "refused")
        self.assertEqual(self.runner.comments, [])

    def test_refusal_recorded_without_content(self):
        lg = self.logger()
        lg.log(self.ev("leak", detail="password=hunter2xx"))
        r = lg.refusals()
        self.assertEqual(len(r), 1)
        self.assertIn("secret", r[0]["reasons"])
        self.assertNotIn("hunter2xx", (self.dir / "refusals.json").read_text(encoding="utf-8"))

    def test_evidence_field_checked(self):
        self.assertEqual(self.logger().log(self.ev("e", evidence=(FAKE_GH,))), "refused")

    def test_refusal_does_not_block_others(self):
        lg = self.logger()
        lg.log(self.ev("bad", detail="secret=zzzzzz"))
        self.assertEqual(lg.log(self.ev("good")), "posted")

    def test_last_moment_check_on_tampered_spool(self):
        lg = self.logger()
        lg.log(self.ev("ok"), flush=False)
        data = json.loads((self.dir / "spool.json").read_text(encoding="utf-8"))
        # tampering that bypasses Event: a body containing a bare token cannot be built via Event (it is scrubbed
        # on load), so assert the post body itself is checked
        with mock.patch.object(gl, "render_comment", return_value="password=hunter2xx"):
            res = lg.flush()
        self.assertEqual(self.runner.comments, [])
        self.assertEqual(len(res.errors), 1)
        self.assertTrue(lg.refusals())
        self.assertEqual(len(data["events"]), 1)


class SubtaskTests(Base):
    REC = {"id": "T-DEV-006", "owner": "claude:dev-github-log", "scope": "GitHub progress log",
           "dependencies": ["T-DEV-001"], "acceptance_criteria": ["40 offline tests", "scrubbing"],
           "branch": "lane/w03-dev-supervisor/L6", "worktree": "C:" + "\\" + "wt" + "\\" + "w03-L6"}

    def test_body_contents(self):
        body = build_subtask_body(self.REC, 44)
        for s in ("#44", "T-DEV-006", "claude:dev-github-log", "GitHub progress log", "T-DEV-001",
                  "lane/w03-dev-supervisor/L6", "- [ ] 40 offline tests"):
            self.assertIn(s, body)

    def test_worktree_basename_only(self):
        body = build_subtask_body(self.REC, 44)
        self.assertIn("w03-L6", body)
        self.assertNotIn("wt", body.replace("worktree", "").replace("Worktree", "").split("w03-L6")[0][-6:])

    def test_parent_validated(self):
        with self.assertRaises(ValueError):
            build_subtask_body(self.REC, 0)

    def test_create_subtask(self):
        n = self.logger().create_subtask(self.REC, 44, labels=["sub-task"])
        self.assertEqual(n, 101)
        title, body, labels = self.runner.issues[0]
        self.assertIn("T-DEV-006", title)
        self.assertIn("Parent issue: #44", body)
        self.assertEqual(labels, ["sub-task"])

    def test_create_subtask_idempotent(self):
        lg = self.logger()
        a = lg.create_subtask(self.REC, 44)
        self.assertEqual(self.logger().create_subtask(self.REC, 44), a)
        self.assertEqual(len(self.runner.issues), 1)

    def test_subtask_sensitive_refused(self):
        bad = dict(self.REC, scope="uses token=abc123xyz")
        lg = self.logger()
        with self.assertRaises(SensitiveContentError):
            lg.create_subtask(bad, 44)
        self.assertEqual(self.runner.issues, [])
        self.assertTrue(lg.refusals())

    def test_subtask_acceptance_checked(self):
        bad = dict(self.REC, acceptance_criteria=["see " + WIN_PATH])
        with self.assertRaises(SensitiveContentError):
            self.logger().create_subtask(bad, 44)

    def test_subtask_retry(self):
        self.runner.fail = [RunnerError("timeout", True)]
        self.assertEqual(self.logger().create_subtask(self.REC, 44), 101)

    def test_subtask_outage_raises_and_not_recorded(self):
        self.runner.fail = [RunnerError("down", True)] * 10
        lg = self.logger(max_attempts=2)
        with self.assertRaises(RunnerError):
            lg.create_subtask(self.REC, 44)
        self.runner.fail = []
        self.assertEqual(lg.create_subtask(self.REC, 44), 101)

    def test_subtask_needs_id(self):
        with self.assertRaises(ValueError):
            self.logger().create_subtask({"scope": "x"}, 44)

    def test_close_issue(self):
        self.logger().close_issue(101, "done, tests pass")
        self.assertEqual(self.runner.closed, [(101, "done, tests pass")])

    def test_close_issue_refuses_sensitive(self):
        lg = self.logger()
        with self.assertRaises(SensitiveContentError):
            lg.close_issue(101, "password=abc123xx")
        self.assertEqual(self.runner.closed, [])


class FilesTests(Base):
    def test_atomic_write_no_temp_left(self):
        self.logger().log(self.ev(), flush=False)
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [])

    def test_atomic_write_failure_keeps_old_file(self):
        lg = self.logger()
        lg.log(self.ev("one"), flush=False)
        before = (self.dir / "spool.json").read_text(encoding="utf-8")
        with mock.patch.object(gl.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                lg.log(self.ev("two"), flush=False)
        self.assertEqual((self.dir / "spool.json").read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [])

    def test_corrupt_spool_moved_aside(self):
        self.dir.mkdir(parents=True)
        (self.dir / "spool.json").write_text("{not json", encoding="utf-8")
        lg = self.logger()
        self.assertEqual(lg.pending(), [])
        self.assertTrue((self.dir / "spool.json.corrupt-1").exists())

    def test_creates_state_dir(self):
        self.assertFalse(self.dir.exists())
        self.logger().log(self.ev(), flush=False)
        self.assertTrue(self.dir.exists())

    def test_spool_has_no_raw_secret(self):
        self.logger().log(self.ev("x", detail="ok"), flush=False)
        self.assertEqual(sensitivity_flags((self.dir / "spool.json").read_text(encoding="utf-8")), [])


class GhRunnerTests(unittest.TestCase):
    def fake(self, rc=0, out="", err=""):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, rc, out, err)
        return run, calls

    def test_comment_args_list_no_shell(self):
        run, calls = self.fake()
        GhRunner(repo="o/r", run=run).comment(5, "body; rm -rf x")
        cmd, kw = calls[0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[:5], ["gh", "issue", "comment", "5", "--body-file"])
        self.assertIn("-R", cmd)
        self.assertFalse(kw["shell"])
        self.assertEqual(kw["input"], "body; rm -rf x")
        self.assertNotIn("body; rm -rf x", cmd)

    def test_create_parses_number(self):
        run, calls = self.fake(out="https://github.com/o/r/issues/321\n")
        n = GhRunner(run=run).create_issue("t", "b", ["l1", "l2"])
        self.assertEqual(n, 321)
        self.assertEqual(calls[0][0].count("--label"), 2)

    def test_create_unparseable(self):
        run, _ = self.fake(out="weird")
        with self.assertRaises(RunnerError):
            GhRunner(run=run).create_issue("t", "b")

    def test_close_args(self):
        run, calls = self.fake()
        GhRunner(run=run).close_issue(9, "done")
        self.assertEqual(calls[0][0], ["gh", "issue", "close", "9", "--comment", "done"])

    def test_nonzero_transient_vs_permanent(self):
        run, _ = self.fake(rc=1, err="HTTP 502 bad gateway")
        with self.assertRaises(RunnerError) as c:
            GhRunner(run=run).comment(1, "b")
        self.assertTrue(c.exception.transient)
        run, _ = self.fake(rc=1, err="Could not find issue")
        with self.assertRaises(RunnerError) as c:
            GhRunner(run=run).comment(1, "b")
        self.assertFalse(c.exception.transient)

    def test_error_text_scrubbed(self):
        run, _ = self.fake(rc=1, err="bad token=abc123secret")
        with self.assertRaises(RunnerError) as c:
            GhRunner(run=run).comment(1, "b")
        self.assertNotIn("abc123secret", str(c.exception))

    def test_timeout_transient(self):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        with self.assertRaises(RunnerError) as c:
            GhRunner(run=run).comment(1, "b")
        self.assertTrue(c.exception.transient)

    def test_missing_gh_permanent(self):
        def run(cmd, **kw):
            raise FileNotFoundError("gh")
        with self.assertRaises(RunnerError) as c:
            GhRunner(run=run).comment(1, "b")
        self.assertFalse(c.exception.transient)

    def test_event_ids_from_view(self):
        out = json.dumps({"comments": [{"body": "x <!-- dev-event:abcdef0123456789 -->"}, {"body": "plain"}]})
        run, calls = self.fake(out=out)
        self.assertEqual(GhRunner(run=run).event_ids(3), {"abcdef0123456789"})
        self.assertEqual(calls[0][0][:6], ["gh", "issue", "view", "3", "--json", "comments"])

    def test_event_ids_bad_json(self):
        run, _ = self.fake(out="nope")
        with self.assertRaises(RunnerError):
            GhRunner(run=run).event_ids(3)


class NoNetworkTests(Base):
    """Whole-flow run with subprocess and sockets patched to fail: the fake runner must never touch them."""

    def test_flow_without_subprocess_or_sockets(self):
        def boom(*a, **k):
            raise AssertionError("external access attempted")
        with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(socket, "socket", boom), mock.patch.object(socket, "create_connection", boom):
            lg = self.logger()
            self.assertEqual(lg.log(self.ev("a")), "posted")
            lg.create_subtask(SubtaskTests.REC, 44)
            lg.close_issue(101)
        self.assertEqual(len(self.runner.comments), 1)

    def test_real_runner_not_used_by_default_tests(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("gh called")):
            self.logger().log(self.ev())


if __name__ == "__main__":
    unittest.main()
