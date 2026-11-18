"""Review packet / review validation tests (T-DEV-022). Synthetic data only; fake gh and git runners; no network.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_review.py -v
"""
import io
import json
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import review as rv

H1 = "0123456789abcdef"
H2 = "fedcba9876543210"
MB = "a" * 40


def mk(pr=5, h=H1, v="APPROVE", who="rev-1"):
    return rv.format_marker(pr, h, v, who)


def cm(body, cid=1, login="reviewer-bot", at="2026-01-01T00:00:00Z"):
    return {"id": cid, "body": body, "user": {"login": login}, "created_at": at}


class FakeRunner:
    """Fake gh/git: maps a key of the argv to bytes; records calls."""

    def __init__(self, diff=b"DIFF-A\n", comments=None, author="author-x", base="main", head="task/x",
                 pages=None, files=("a.py", "b.py")):
        self.diff, self.calls, self.files = diff, [], list(files)
        self.info = {"number": 5, "title": "T", "headRefName": head, "baseRefName": base,
                     "headRefOid": "b" * 40, "author": {"login": author}, "state": "OPEN", "url": "u"}
        self.pages = pages if pages is not None else [comments or []]

    def __call__(self, argv, stdin=None):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            return json.dumps(self.info).encode()
        if argv[:2] == ["gh", "api"]:
            return "\n".join(json.dumps(p) for p in self.pages).encode()
        if argv[:2] == ["git", "fetch"]:
            return b""
        if argv[:2] == ["git", "merge-base"]:
            return (MB + "\n").encode()
        if argv[:3] == ["git", "diff", "--binary"]:
            return self.diff
        if argv[:3] == ["git", "diff", "--name-only"]:
            return ("\n".join(self.files) + "\n").encode()
        raise AssertionError(argv)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(socket, "socket", side_effect=AssertionError("network used"))
        p.start()
        self.addCleanup(p.stop)


class ParseMarker(NoNetwork):
    def test_valid(self):
        m = rv.parse_marker(mk())
        self.assertEqual((m.pr, m.diff_sha256, m.verdict, m.reviewer), (5, H1, "APPROVE", "rev-1"))

    def test_changes_requested(self):
        self.assertEqual(rv.parse_marker(mk(v="CHANGES_REQUESTED")).verdict, "CHANGES_REQUESTED")

    def test_body_after_marker(self):
        self.assertIsNotNone(rv.parse_marker(mk() + "\n## BLOCKING\n- none\n"))

    def test_crlf_first_line(self):
        self.assertIsNotNone(rv.parse_marker(mk() + "\r\nbody"))

    def test_not_string(self):
        for v in (None, 5, b"x", ["x"]):
            self.assertIsNone(rv.parse_marker(v))

    def test_marker_not_first_line(self):
        self.assertIsNone(rv.parse_marker("hello\n" + mk()))
        self.assertIn("not on the first line", rv.marker_problem("hello\n" + mk()))

    def test_leading_space_or_blank_line_rejected(self):
        self.assertIsNone(rv.parse_marker(" " + mk()))
        self.assertIsNone(rv.parse_marker("\n" + mk()))

    def test_trailing_text_on_line_rejected(self):
        self.assertIsNone(rv.parse_marker(mk() + " extra"))

    def test_bad_verdict(self):
        self.assertIsNone(rv.parse_marker(mk(v="LGTM")))
        self.assertIsNone(rv.parse_marker(mk().replace("APPROVE", "approve")))

    def test_hash_wrong_length_or_case(self):
        self.assertIsNone(rv.parse_marker(mk(h="0123456789abcde")))
        self.assertIsNone(rv.parse_marker(mk(h="0123456789abcdef0")))
        self.assertIsNone(rv.parse_marker(mk(h="0123456789ABCDEF")))
        self.assertIsNone(rv.parse_marker(mk(h="0123456789abcdeg")))

    def test_pr_number_bad(self):
        self.assertIsNone(rv.parse_marker(mk().replace("pr=5", "pr=0")))
        self.assertIsNone(rv.parse_marker(mk().replace("pr=5", "pr=-1")))
        self.assertIsNone(rv.parse_marker(mk().replace("pr=5", "pr=05")))
        self.assertIsNone(rv.parse_marker(mk().replace("pr=5", "pr=x")))

    def test_wrong_version_or_name(self):
        self.assertIsNone(rv.parse_marker(mk().replace("v1", "v2")))
        self.assertIsNone(rv.parse_marker(mk().replace("dev-review", "dev-reviewer")))

    def test_reviewer_charset(self):
        self.assertIsNone(rv.parse_marker(mk(who="a b")))
        self.assertIsNone(rv.parse_marker(mk(who="-lead")))
        self.assertIsNone(rv.parse_marker(mk(who="x" * 65)))
        self.assertIsNotNone(rv.parse_marker(mk(who="claude:reviewer_2.a-b")))

    def test_missing_field(self):
        self.assertIsNone(rv.parse_marker(mk().replace(" reviewer=rev-1", "")))

    def test_marker_problem_levels(self):
        self.assertEqual(rv.marker_problem("just a comment"), "")
        self.assertEqual(rv.marker_problem(mk()), "")
        self.assertIn("malformed", rv.marker_problem("<!-- dev-review v1 pr=5 -->"))
        self.assertEqual(rv.marker_problem(None), "")


class DiffHash(NoNetwork):
    def test_known_value_and_length(self):
        import hashlib
        self.assertEqual(rv.diff_hash(b"abc"), hashlib.sha256(b"abc").hexdigest()[:16])
        self.assertEqual(len(rv.diff_hash(b"")), 16)

    def test_changes_with_bytes(self):
        self.assertNotEqual(rv.diff_hash(b"a"), rv.diff_hash(b"b"))

    def test_binary_bytes(self):
        self.assertRegex(rv.diff_hash(bytes(range(256))), r"^[0-9a-f]{16}$")


class Status(NoNetwork):
    def st(self, comments, cur=H1, authors=("author-x",), pr=5):
        return rv.review_status(pr, comments, cur, authors)

    def test_missing_empty(self):
        self.assertEqual(self.st([])["status"], rv.MISSING)

    def test_missing_unrelated_comments(self):
        self.assertEqual(self.st([cm("looks fine"), cm("<!-- dev-event:abc -->", 2)])["status"], rv.MISSING)

    def test_current_approved(self):
        r = self.st([cm(mk())])
        self.assertEqual(r["status"], rv.CURRENT_APPROVED)
        self.assertEqual((r["reviewer"], r["comment_id"]), ("rev-1", 1))

    def test_changes_requested(self):
        self.assertEqual(self.st([cm(mk(v="CHANGES_REQUESTED"))])["status"], rv.CHANGES_REQUESTED)

    def test_stale(self):
        r = self.st([cm(mk(h=H2))])
        self.assertEqual(r["status"], rv.STALE)
        self.assertEqual((r["review_hash"], r["current_hash"]), (H2, H1))

    def test_stale_changes_requested_is_stale(self):
        self.assertEqual(self.st([cm(mk(h=H2, v="CHANGES_REQUESTED"))])["status"], rv.STALE)

    def test_invalid_only(self):
        r = self.st([cm("<!-- dev-review v1 pr=5 verdict=APPROVE -->")])
        self.assertEqual(r["status"], rv.INVALID)
        self.assertEqual(len(r["rejected"]), 1)

    def test_latest_wins_approve_then_changes(self):
        c = [cm(mk(), 1, at="2026-01-01T00:00:00Z"), cm(mk(v="CHANGES_REQUESTED", who="rev-2"), 2, at="2026-01-02T00:00:00Z")]
        self.assertEqual(self.st(c)["status"], rv.CHANGES_REQUESTED)

    def test_latest_wins_changes_then_approve(self):
        c = [cm(mk(v="CHANGES_REQUESTED"), 1, at="2026-01-01T00:00:00Z"), cm(mk(who="rev-2"), 2, at="2026-01-02T00:00:00Z")]
        self.assertEqual(self.st(c)["status"], rv.CURRENT_APPROVED)

    def test_order_independent_of_input_order(self):
        c = [cm(mk(v="CHANGES_REQUESTED"), 1, at="2026-01-01T00:00:00Z"), cm(mk(who="rev-2"), 2, at="2026-01-02T00:00:00Z")]
        self.assertEqual(self.st(list(reversed(c)))["status"], rv.CURRENT_APPROVED)

    def test_same_timestamp_higher_id_wins(self):
        c = [cm(mk(), 9, at="t"), cm(mk(v="CHANGES_REQUESTED", who="r2"), 3, at="t")]
        self.assertEqual(self.st(c)["status"], rv.CURRENT_APPROVED)

    def test_old_stale_then_new_current(self):
        c = [cm(mk(h=H2), 1, at="2026-01-01T00:00:00Z"), cm(mk(h=H1), 2, at="2026-01-02T00:00:00Z")]
        self.assertEqual(self.st(c)["status"], rv.CURRENT_APPROVED)

    def test_new_push_after_approval_makes_stale(self):
        c = [cm(mk(h=H1))]
        self.assertEqual(self.st(c, cur=H1)["status"], rv.CURRENT_APPROVED)
        self.assertEqual(self.st(c, cur=H2)["status"], rv.STALE)

    def test_author_equals_reviewer_rejected(self):
        r = self.st([cm(mk(who="author-x"))])
        self.assertEqual(r["status"], rv.INVALID)
        self.assertIn("author", r["rejected"][0]["reason"])

    def test_author_comparison_case_and_at_insensitive(self):
        self.assertEqual(self.st([cm(mk(who="Author-X"))])["status"], rv.INVALID)
        self.assertEqual(self.st([cm(mk())], authors=("@AUTHOR-X",), )["status"], rv.CURRENT_APPROVED)

    def test_shared_account_login_is_not_compared(self):
        # all workers post through one GitHub account: only the reviewer id identifies independence
        self.assertEqual(self.st([cm(mk(who="someone"), login="author-x")])["status"], rv.CURRENT_APPROVED)

    def test_worker_id_of_author_can_be_passed(self):
        self.assertEqual(self.st([cm(mk(who="worker-7"))], authors=("author-x", "worker-7"))["status"], rv.INVALID)

    def test_self_review_does_not_override_independent_approval(self):
        c = [cm(mk(), 1, at="2026-01-01T00:00:00Z"),
             cm(mk(v="CHANGES_REQUESTED", who="author-x"), 2, at="2026-01-02T00:00:00Z")]
        r = self.st(c)
        self.assertEqual(r["status"], rv.CURRENT_APPROVED)
        self.assertEqual(len(r["rejected"]), 1)

    def test_forged_marker_for_other_pr_rejected(self):
        r = self.st([cm(mk(pr=6))])
        self.assertEqual(r["status"], rv.INVALID)
        self.assertIn("PR 6", r["rejected"][0]["reason"])

    def test_forged_marker_inside_body_ignored(self):
        self.assertEqual(self.st([cm("quote:\n" + mk())])["status"], rv.INVALID)

    def test_malformed_later_does_not_override_valid(self):
        c = [cm(mk(), 1, at="2026-01-01T00:00:00Z"), cm("<!-- dev-review v1 garbage -->", 2, at="2026-01-02T00:00:00Z")]
        r = self.st(c)
        self.assertEqual(r["status"], rv.CURRENT_APPROVED)
        self.assertEqual(r["review_attempts"], 2)

    def test_no_author_ids_given(self):
        self.assertEqual(rv.review_status(5, [cm(mk())], H1)["status"], rv.CURRENT_APPROVED)

    def test_garbage_comment_entries_tolerated(self):
        c = ["x", None, {"id": 3}, {"id": 4, "body": 5, "user": "no"}, cm(mk(), 5)]
        self.assertEqual(rv.review_status(5, c, H1, [])["status"], rv.CURRENT_APPROVED)

    def test_pure_does_not_mutate_input(self):
        c = [cm(mk(), 1)]
        before = json.dumps(c)
        self.st(c)
        self.assertEqual(json.dumps(c), before)


class GitGh(NoNetwork):
    def test_compute_diff_uses_origin_refs_and_fetches(self):
        f = FakeRunner(diff=b"BYTES")
        diff, h, mb, files = rv.compute_diff(f, "main", "task/x")
        self.assertEqual((diff, h, mb, files), (b"BYTES", rv.diff_hash(b"BYTES"), MB, ["a.py", "b.py"]))
        self.assertEqual(f.calls[0], ["git", "fetch", "--quiet", "origin", "main", "task/x"])
        self.assertIn(["git", "merge-base", "origin/main", "origin/task/x"], f.calls)
        self.assertIn(["git", "diff", "--binary", MB, "origin/task/x"], f.calls)

    def test_compute_diff_no_fetch(self):
        f = FakeRunner()
        rv.compute_diff(f, "main", "task/x", fetch=False)
        self.assertNotIn("fetch", [c[1] for c in f.calls])

    def test_unsafe_ref_rejected(self):
        for bad in ("--upload-pack=x", "a b", "a..b", "", "x;y"):
            with self.assertRaises(rv.ReviewError):
                rv.compute_diff(FakeRunner(), "main", bad)

    def test_bad_merge_base_rejected(self):
        f = FakeRunner()
        orig = f.__call__
        f2 = lambda argv, stdin=None: b"nothex\n" if argv[:2] == ["git", "merge-base"] else orig(argv, stdin)
        with self.assertRaises(rv.ReviewError):
            rv.compute_diff(f2, "main", "task/x")

    def test_fetch_comments_paginated_concatenated_pages(self):
        f = FakeRunner(pages=[[cm("a", 1)], [cm("b", 2)], []])
        self.assertEqual([c["id"] for c in rv.fetch_comments(f, 5)], [1, 2])
        self.assertEqual(f.calls[0][:3], ["gh", "api", "--paginate"])

    def test_fetch_comments_unparseable(self):
        with self.assertRaises(rv.ReviewError):
            rv.fetch_comments(lambda a, stdin=None: b"not json{", 5)

    def test_fetch_pr_requires_head_base(self):
        with self.assertRaises(rv.ReviewError):
            rv.fetch_pr(lambda a, stdin=None: b"{}", 5)

    def test_fetch_pr_unparseable(self):
        with self.assertRaises(rv.ReviewError):
            rv.fetch_pr(lambda a, stdin=None: b"<html>", 5)

    def test_check_pr_end_to_end_current(self):
        digest = rv.diff_hash(b"DIFF-A\n")
        f = FakeRunner(comments=[cm(mk(h=digest))])
        r = rv.check_pr(5, f, f)
        self.assertEqual(r["status"], rv.CURRENT_APPROVED)
        self.assertEqual((r["files"], r["merge_base"]), (2, MB))

    def test_check_pr_stale_after_diff_change(self):
        stale = rv.diff_hash(b"OLD")
        f = FakeRunner(comments=[cm(mk(h=stale))])
        self.assertEqual(rv.check_pr(5, f, f)["status"], rv.STALE)

    def test_check_pr_self_review_rejected(self):
        digest = rv.diff_hash(b"DIFF-A\n")
        f = FakeRunner(comments=[cm(mk(h=digest, who="author-x"))], author="author-x")
        self.assertEqual(rv.check_pr(5, f, f)["status"], rv.INVALID)

    def test_check_pr_missing(self):
        f = FakeRunner()
        self.assertEqual(rv.check_pr(5, f, f)["status"], rv.MISSING)


class Packet(NoNetwork):
    def make_root(self, d):
        root = Path(d) / "repo"
        (root / "waves" / "w9").mkdir(parents=True)
        (root / "ACTIVE_TASKS.json").write_text(json.dumps({"tasks": [
            {"id": "T-1", "title": "Other", "branch": "task/other", "owned_paths": ["x/"], "acceptance_criteria": ["c0"]},
            {"id": "T-2", "title": "Reg task", "branch": "task/x", "owned_paths": ["p/q.py"],
             "acceptance_criteria": ["crit one", "crit two"]}]}), encoding="utf-8")
        (root / "waves" / "w9" / "wave.json").write_text(json.dumps({"lanes": [
            {"id": "L1", "task_id": "T-W", "title": "Lane", "branch": "lane/w9/L1", "owned_paths": ["z.py"],
             "acceptance_criteria": ["lane crit"]}]}), encoding="utf-8")
        return root

    def test_find_task_active_and_lane_and_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.make_root(d)
            self.assertEqual(rv.find_registered_task(root, "task/x")["id"], "T-2")
            lane = rv.find_registered_task(root, "lane/w9/L1")
            self.assertEqual((lane["id"], lane["owned_paths"]), ("T-W", ["z.py"]))
            self.assertIsNone(rv.find_registered_task(root, "nope"))

    def test_find_task_missing_registry(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(rv.find_registered_task(Path(d), "task/x"))

    def test_packet_files(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = self.make_root(d), Path(d) / "out"
            f = FakeRunner(diff=b"\x00binary\xff diff")
            res = rv.build_packet(5, out, f, f, root)
            self.assertEqual((out / "diff.patch").read_bytes(), b"\x00binary\xff diff")
            self.assertEqual((out / "files.txt").read_text(), "a.py\nb.py\n")
            task = (out / "task.md").read_text()
            self.assertIn("p/q.py", task)
            self.assertIn("crit two", task)
            prompt = (out / "reviewer-prompt.md").read_text()
            digest = rv.diff_hash(b"\x00binary\xff diff")
            self.assertEqual(res["diff_sha256"], digest)
            self.assertEqual(res["task"], "T-2")
            self.assertIn(f"<!-- dev-review v1 pr=5 diff_sha256={digest} verdict=APPROVE reviewer=", prompt)
            for word in ("BLOCKING", "NON-BLOCKING", "EXACTLY ONE", "security", "RUN them", "architecture",
                         "regressions", "Correctness", "author-x"):
                self.assertIn(word, prompt.replace("Security", "security").replace("Architecture", "architecture")
                              .replace("Regressions", "regressions"))

    def test_packet_without_registered_task(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "empty"
            root.mkdir()
            out = Path(d) / "o"
            f = FakeRunner()
            self.assertIsNone(rv.build_packet(5, out, f, f, root)["task"])
            self.assertIn("No registered task", (out / "task.md").read_text())
            self.assertIn("NONE FOUND", (out / "reviewer-prompt.md").read_text())

    def test_packet_has_no_absolute_user_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = self.make_root(d), Path(d) / "out"
            f = FakeRunner()
            rv.build_packet(5, out, f, f, root)
            for name in ("reviewer-prompt.md", "task.md", "files.txt"):
                text = (out / name).read_text()
                self.assertNotIn(d, text)
                self.assertNotRegex(text, r"(?i)[a-z]:\\\\Users")


class Cli(NoNetwork):
    def run_main(self, argv, f, root=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rv.main(argv, gh=f, git=f, root=root or Path("."))
        return code, out.getvalue(), err.getvalue()

    def test_check_current_exit0(self):
        digest = rv.diff_hash(b"DIFF-A\n")
        code, out, _ = self.run_main(["check", "5"], FakeRunner(comments=[cm(mk(h=digest))]))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("CURRENT_APPROVED pr=5"))

    def test_check_missing_exit1(self):
        code, out, _ = self.run_main(["check", "5"], FakeRunner())
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("MISSING"))

    def test_check_json(self):
        code, out, _ = self.run_main(["check", "5", "--json"], FakeRunner(comments=[cm(mk(h=H2))]))
        self.assertEqual(json.loads(out)["status"], rv.STALE)
        self.assertEqual(code, 1)

    def test_check_prints_rejections(self):
        _, out, _ = self.run_main(["check", "5"], FakeRunner(comments=[cm(mk(pr=9), 77)]))
        self.assertIn("rejected comment 77", out)

    def test_error_exit2_no_traceback(self):
        def boom(argv, stdin=None):
            raise rv.ReviewError("gh pr view exit 1: not found")
        code, out, err = self.run_main(["check", "5"], boom)
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_packet_cli(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = self.run_main(["packet", "5", "--out", str(Path(d) / "pk")], FakeRunner(), Path(d))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["pr"], 5)
            self.assertTrue((Path(d) / "pk" / "reviewer-prompt.md").exists())

    def test_bad_pr_argument(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rv.main(["check", "abc"], gh=FakeRunner(), git=FakeRunner())


class RunnerErrors(NoNetwork):
    def test_subprocess_runner_failure_is_scrubbed_review_error(self):
        r = rv.SubprocessRunner()
        with self.assertRaises(rv.ReviewError):
            r([sys.executable, "-c", "import sys; sys.exit(3)"])

    def test_subprocess_runner_missing_binary(self):
        with self.assertRaises(rv.ReviewError):
            rv.SubprocessRunner()(["definitely-not-a-real-binary-xyz"])

    def test_subprocess_runner_stdout(self):
        self.assertEqual(rv.SubprocessRunner()([sys.executable, "-c", "print('ok')"]).strip(), b"ok")


if __name__ == "__main__":
    unittest.main()
