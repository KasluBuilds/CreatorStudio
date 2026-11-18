"""Offline tests for dev_supervisor.integrate (T-DEV-021). gh and git are fakes; no network, synthetic data only."""
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dev_supervisor import integrate as I  # noqa: E402

HEAD = "a" * 40
MB = "b" * 40
DIFF = b"synthetic diff bytes\n"
DHASH = hashlib.sha256(DIFF).hexdigest()[:16]
TASK_BRANCH = "task/demo"
OWNER = "claude:demo-worker"


def fake_secret():
    return "gh" + "p_" + "A" * 24          # built at runtime so no literal key-like string is committed


def marker(pr=7, h=DHASH, verdict="APPROVE", reviewer="claude:reviewer-1"):
    return f"<!-- dev-review v1 pr={pr} diff_sha256={h} verdict={verdict} reviewer={reviewer} -->"


def comment(body, cid=1, when="2026-01-01T00:00:00Z", assoc="OWNER"):
    return {"id": cid, "body": body, "created_at": when, "author_association": assoc, "user": {"login": "someone"}}


def registry(branch=TASK_BRANCH, status="ACTIVE", owned=("orchestrator/demo/",)):
    return {"tasks": [{"id": "T-X-001", "branch": branch, "status": status, "owner": OWNER,
                       "owned_paths": list(owned)}]}


def pr_json(**kw):
    d = {"number": 7, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": TASK_BRANCH,
         "headRefOid": HEAD, "mergeStateStatus": "CLEAN",
         "statusCheckRollup": [{"__typename": "CheckRun", "name": "validate", "status": "COMPLETED",
                                "conclusion": "SUCCESS"}],
         "files": [], "commits": [{"authors": [{"login": "dev", "name": "Dev One", "email": "dev@example.test"}],
                                   "messageBody": "x\n\nCo-Authored-By: Claude Worker <noreply@example.test>"}]}
    d.update(kw)
    return d


class FakeGh:
    def __init__(self, pr=..., comments=None, merge_rc=0, merge_out="", after_state="MERGED", view_fail_after=False,
                 comments_rc=0):
        self.pr = pr_json() if pr is ... else pr
        self.comments = comments if comments is not None else [comment(marker())]
        self.merge_rc, self.merge_out, self.after_state = merge_rc, merge_out, after_state
        self.merged = False
        self.view_fail_after = view_fail_after
        self.comments_rc = comments_rc
        self.calls, self.posted = [], []
        self.merge_results = None   # optional list of rc values per merge call
        self.on_merge = None

    def __call__(self, args, input=None):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["pr", "view"] and "state,mergeCommit" in args:
            if self.view_fail_after:
                return 1, "boom"
            st = "MERGED" if self.merged else ("OPEN" if self.after_state == "MERGED" else self.after_state)
            return 0, json.dumps({"state": st, "mergeCommit": {"oid": "c" * 40}})
        if args[:2] == ["pr", "view"]:
            return (0, json.dumps(self.pr)) if self.pr is not None else (1, "nope")
        if args[0] == "api":
            return (0, json.dumps(self.comments)) if not self.comments_rc else (1, "err")
        if args[:2] == ["pr", "merge"]:
            if self.on_merge:
                self.on_merge()
            rc = self.merge_results.pop(0) if self.merge_results else self.merge_rc
            if rc == 0:
                self.merged = self.after_state == "MERGED"
            return rc, self.merge_out
        if args[:2] == ["pr", "comment"]:
            self.posted.append(input)
            return 0, ""
        return 1, "unexpected"

    def merges(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


class FakeGit:
    def __init__(self, files=("orchestrator/demo/a.py",), added=("orchestrator/demo/a.py",), lines=("x = 1",),
                 reg=None, head_sha=HEAD, fetch_rc=0, diff=DIFF, wave=None, mb_rc=0):
        self.files, self.added, self.lines = list(files), list(added), list(lines)
        self.reg = registry() if reg is None else reg
        self.head_sha, self.fetch_rc, self.diff, self.wave, self.mb_rc = head_sha, fetch_rc, diff, wave, mb_rc
        self.calls = []

    def __call__(self, args):
        a = list(args)
        self.calls.append(a)
        if a[0] == "fetch":
            return self.fetch_rc, b""
        if a[0] == "rev-parse":
            return 0, self.head_sha.encode()
        if a[0] == "merge-base":
            return self.mb_rc, MB.encode()
        if a[:2] == ["diff", "--binary"]:
            return 0, self.diff
        if a[0] == "diff" and "--name-only" in a:
            return 0, "\0".join(self.added if "--diff-filter=AM" in a else self.files).encode()
        if a[0] == "diff" and "-U0" in a:
            return 0, ("+++ b/f\n" + "\n".join("+" + l for l in self.lines)).encode()
        if a[0] == "show":
            if a[1].endswith("ACTIVE_TASKS.json"):
                return (0, json.dumps(self.reg).encode()) if self.reg != "bad" else (0, b"{not json")
            if "wave.json" in a[1]:
                return (0, json.dumps(self.wave).encode()) if self.wave else (1, b"")
        return 1, b""


def status(gates, name):
    return next(g for g in gates if g.name == name).status


def run_eval(gh=None, git=None):
    gates, info = I.evaluate(7, gh or FakeGh(), git or FakeGit())
    return gates, info


class MarkerAndHash(unittest.TestCase):
    def test_marker_valid(self):
        m = I.parse_review_marker(marker())
        self.assertEqual((m["pr"], m["verdict"], m["reviewer"]), (7, "APPROVE", "claude:reviewer-1"))

    def test_marker_rejects_bad_forms(self):
        for bad in ("", "hello", marker().replace("v1", "v2"), marker(h="abc"), marker(verdict="MAYBE"),
                    " x" + marker(), marker().replace(" -->", "-->"), marker(h="G" * 16), marker().upper()):
            self.assertIsNone(I.parse_review_marker(bad), bad)

    def test_diff_hash_matches_sha256_prefix(self):
        self.assertEqual(I.diff_hash(FakeGit(), "origin/main", "origin/x"), DHASH)

    def test_diff_hash_none_on_failure(self):
        self.assertIsNone(I.diff_hash(FakeGit(mb_rc=1), "origin/main", "origin/x"))


class GateOne(unittest.TestCase):
    def check(self, **kw):
        g, _ = run_eval(FakeGh(pr=pr_json(**kw)))
        return status(g, "pr_state")

    def test_pass(self):
        self.assertEqual(self.check(), I.PASS)

    def test_closed(self):
        self.assertEqual(self.check(state="CLOSED"), I.FAIL)

    def test_merged(self):
        self.assertEqual(self.check(state="MERGED"), I.FAIL)

    def test_draft(self):
        self.assertEqual(self.check(isDraft=True), I.FAIL)

    def test_wrong_base(self):
        self.assertEqual(self.check(baseRefName="dev"), I.FAIL)

    def test_bad_prefix(self):
        self.assertEqual(self.check(headRefName="feature/x"), I.FAIL)

    def test_lane_prefix_not_allowed(self):
        self.assertEqual(self.check(headRefName="lane/w01-x/L1"), I.FAIL)

    def test_chatgpt_and_wave_prefix_ok(self):
        self.assertEqual(self.check(headRefName="chatgpt/x"), I.PASS)
        self.assertEqual(self.check(headRefName="wave/w01-x"), I.PASS)


class GateTwo(unittest.TestCase):
    def check(self, rollup):
        g, _ = run_eval(FakeGh(pr=pr_json(statusCheckRollup=rollup)))
        return status(g, "checks")

    def ck(self, name="validate", status_="COMPLETED", concl="SUCCESS"):
        return {"__typename": "CheckRun", "name": name, "status": status_, "conclusion": concl}

    def test_success(self):
        self.assertEqual(self.check([self.ck()]), I.PASS)

    def test_failure(self):
        self.assertEqual(self.check([self.ck(concl="FAILURE")]), I.FAIL)

    def test_pending(self):
        self.assertEqual(self.check([self.ck(status_="IN_PROGRESS", concl="")]), I.FAIL)

    def test_missing_validate(self):
        self.assertEqual(self.check([self.ck(name="other")]), I.FAIL)

    def test_empty_rollup(self):
        self.assertEqual(self.check([]), I.FAIL)

    def test_other_failing_check_blocks(self):
        self.assertEqual(self.check([self.ck(), self.ck(name="lint", concl="FAILURE")]), I.FAIL)

    def test_other_pending_check_blocks(self):
        self.assertEqual(self.check([self.ck(), self.ck(name="lint", status_="QUEUED", concl="")]), I.FAIL)

    def test_other_skipped_check_ok(self):
        self.assertEqual(self.check([self.ck(), self.ck(name="doc", concl="SKIPPED")]), I.PASS)

    def test_status_context_success(self):
        self.assertEqual(self.check([{"__typename": "StatusContext", "context": "validate", "state": "SUCCESS"}]),
                         I.PASS)

    def test_missing_rollup_is_unknown(self):
        g, _ = run_eval(FakeGh(pr=pr_json(statusCheckRollup=None)))
        self.assertEqual(status(g, "checks"), I.UNKNOWN)


class GateThree(unittest.TestCase):
    def check(self, s):
        g, _ = run_eval(FakeGh(pr=pr_json(mergeStateStatus=s)))
        return status(g, "merge_state")

    def test_clean(self):
        self.assertEqual(self.check("CLEAN"), I.PASS)

    def test_behind_dirty_blocked_unstable(self):
        for s in ("BEHIND", "DIRTY", "BLOCKED", "UNSTABLE", "DRAFT"):
            self.assertEqual(self.check(s), I.FAIL, s)

    def test_unknown(self):
        self.assertEqual(self.check("UNKNOWN"), I.UNKNOWN)
        self.assertEqual(self.check(""), I.UNKNOWN)


class GateFour(unittest.TestCase):
    def scope(self, git, gh=None):
        g, _ = run_eval(gh, git)
        return status(g, "scope")

    def test_inside_owned(self):
        self.assertEqual(self.scope(FakeGit()), I.PASS)

    def test_outside_owned(self):
        self.assertEqual(self.scope(FakeGit(files=["orchestrator/demo/a.py", "README.md"])), I.FAIL)

    def test_unregistered_branch(self):
        self.assertEqual(self.scope(FakeGit(reg=registry(branch="task/other"))), I.FAIL)

    def test_inactive_task(self):
        self.assertEqual(self.scope(FakeGit(reg=registry(status="COMPLETED"))), I.FAIL)

    def test_under_review_counts(self):
        self.assertEqual(self.scope(FakeGit(reg=registry(status="UNDER_REVIEW"))), I.PASS)

    def test_registry_unreadable_is_unknown(self):
        self.assertEqual(self.scope(FakeGit(reg="bad")), I.UNKNOWN)

    def test_registry_only_pr_passes_as_record(self):
        git = FakeGit(files=["ACTIVE_TASKS.json", "CHANGELOG.md"], reg=registry(branch="task/none"))
        self.assertEqual(self.scope(git), I.PASS)

    def test_registry_plus_other_file_not_a_record(self):
        git = FakeGit(files=["ACTIVE_TASKS.json", "evil.py"], reg=registry(branch="task/none"))
        self.assertEqual(self.scope(git), I.FAIL)

    def test_no_changed_files(self):
        self.assertEqual(self.scope(FakeGit(files=[])), I.FAIL)

    def wave_manifest(self):
        return {"lanes": [{"id": "L1", "owner": "claude:coord", "owned_paths": ["orchestrator/a.py"]},
                          {"id": "L2", "owner": "claude:w2", "owned_paths": ["orchestrator/tests/test_a.py"]}]}

    def test_wave_inside_lane_paths_and_closeout(self):
        gh = FakeGh(pr=pr_json(headRefName="wave/w01-x"))
        git = FakeGit(files=["orchestrator/a.py", "orchestrator/tests/test_a.py", "waves/w01-x/lanes/L1/report.json",
                             "CHANGELOG.md"], wave=self.wave_manifest())
        self.assertEqual(self.scope(git, gh), I.PASS)

    def test_wave_outside_lane_paths(self):
        gh = FakeGh(pr=pr_json(headRefName="wave/w01-x"))
        git = FakeGit(files=["orchestrator/a.py", "orchestrator/other.py"], wave=self.wave_manifest())
        self.assertEqual(self.scope(git, gh), I.FAIL)

    def test_wave_manifest_missing_unknown(self):
        gh = FakeGh(pr=pr_json(headRefName="wave/w01-x"))
        self.assertEqual(self.scope(FakeGit(files=["orchestrator/a.py"]), gh), I.UNKNOWN)


class GateFive(unittest.TestCase):
    def review(self, comments, gh_kw=None, git=None):
        gh = FakeGh(comments=comments, **(gh_kw or {}))
        g, _ = run_eval(gh, git)
        return status(g, "review")

    def test_approve(self):
        self.assertEqual(self.review([comment(marker())]), I.PASS)

    def test_no_marker(self):
        self.assertEqual(self.review([comment("LGTM")]), I.FAIL)

    def test_stale_hash_ignored(self):
        self.assertEqual(self.review([comment(marker(h="0" * 16))]), I.FAIL)

    def test_other_pr_marker_ignored(self):
        self.assertEqual(self.review([comment(marker(pr=8))]), I.FAIL)

    def test_marker_must_be_first_line(self):
        self.assertEqual(self.review([comment("hi\n" + marker())]), I.FAIL)

    def test_untrusted_commenter_ignored(self):
        self.assertEqual(self.review([comment(marker(), assoc="NONE")]), I.FAIL)

    def test_changes_requested_blocks(self):
        cs = [comment(marker(), 1, "2026-01-01T00:00:00Z"),
              comment(marker(verdict="CHANGES_REQUESTED"), 2, "2026-01-02T00:00:00Z")]
        self.assertEqual(self.review(cs), I.FAIL)

    def test_changes_requested_then_newer_approve_same_reviewer_passes(self):
        cs = [comment(marker(verdict="CHANGES_REQUESTED"), 1, "2026-01-01T00:00:00Z"),
              comment(marker(), 2, "2026-01-02T00:00:00Z")]
        self.assertEqual(self.review(cs), I.PASS)

    def test_other_reviewer_changes_requested_blocks(self):
        cs = [comment(marker()), comment(marker(verdict="CHANGES_REQUESTED", reviewer="claude:reviewer-2"), 2)]
        self.assertEqual(self.review(cs), I.FAIL)

    def test_reviewer_equal_task_owner_fails(self):
        self.assertEqual(self.review([comment(marker(reviewer=OWNER))]), I.FAIL)

    def test_reviewer_equal_coauthor_fails(self):
        self.assertEqual(self.review([comment(marker(reviewer="Claude Worker"))]), I.FAIL)

    def test_reviewer_equal_author_login_fails(self):
        self.assertEqual(self.review([comment(marker(reviewer="dev"))]), I.FAIL)

    def test_reviewer_equal_author_email_fails(self):
        self.assertEqual(self.review([comment(marker(reviewer="dev@example.test"))]), I.FAIL)

    def test_independent_approve_beside_author_approve_passes(self):
        self.assertEqual(self.review([comment(marker(reviewer=OWNER)), comment(marker(), 2)]), I.PASS)

    def test_comments_unreadable_unknown(self):
        self.assertEqual(self.review([], {"comments_rc": 1}), I.UNKNOWN)

    def test_wave_review_required_too(self):
        gh = FakeGh(pr=pr_json(headRefName="wave/w01-x"), comments=[comment("no marker")])
        git = FakeGit(files=["orchestrator/a.py"], wave={"lanes": [{"owner": "claude:c", "owned_paths": ["orchestrator/a.py"]}]})
        g, _ = run_eval(gh, git)
        self.assertEqual(status(g, "review"), I.FAIL)


class RegistryOnlyReview(unittest.TestCase):
    def review(self, files, head=TASK_BRANCH, wave=None):
        gh = FakeGh(pr=pr_json(headRefName=head), comments=[])
        g, _ = run_eval(gh, FakeGit(files=files, wave=wave, reg=registry(branch="task/none")))
        return next(x for x in g if x.name == "review")

    def test_registry_only_exempt(self):
        g = self.review(["ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json", "SESSION_HANDOFF.md"])
        self.assertEqual(g.status, I.PASS)
        self.assertIn("registry-only record: no code review required (protocol section 18 gate 4)", g.detail)

    def test_registry_plus_code_file_needs_review(self):
        self.assertEqual(self.review(["ACTIVE_TASKS.json", "orchestrator/x.py"]).status, I.FAIL)

    def test_wave_pr_still_needs_review(self):
        w = {"lanes": [{"owner": "claude:c", "owned_paths": ["orchestrator/a.py"]}]}
        self.assertEqual(self.review(["ACTIVE_TASKS.json", "CHANGELOG.md"], "wave/w01-x", w).status, I.FAIL)


class GatesSixSeven(unittest.TestCase):
    def test_clean_lines_pass(self):
        g, _ = run_eval()
        self.assertEqual(status(g, "secrets"), I.PASS)

    def test_secret_line_fails_without_leaking(self):
        s = fake_secret()
        g, _ = run_eval(git=FakeGit(lines=[f"token = '{s}'"]))
        gate = next(x for x in g if x.name == "secrets")
        self.assertEqual(gate.status, I.FAIL)
        self.assertNotIn(s, gate.detail)

    def test_private_key_marker_fails(self):
        g, _ = run_eval(git=FakeGit(lines=["-----BEGIN " + "RSA PRIVATE KEY-----"]))
        self.assertEqual(status(g, "secrets"), I.FAIL)

    def test_launcher_added_fails(self):
        for f in ("pro.ps1", "tools/backup.ps1", ".mcp.json"):
            g, _ = run_eval(git=FakeGit(added=[f]))
            self.assertEqual(status(g, "forbidden_files"), I.FAIL, f)

    def test_credential_and_media_added_fail(self):
        for f in (".env", "cfg/credentials.json", "a/key.pem", "clip.mp4", "Incoming/x/notes.txt", "Projects/p/a.json"):
            g, _ = run_eval(git=FakeGit(added=[f]))
            self.assertEqual(status(g, "forbidden_files"), I.FAIL, f)

    def test_normal_file_passes(self):
        g, _ = run_eval(git=FakeGit(added=["orchestrator/demo/a.py", "docs/readme.md"]))
        self.assertEqual(status(g, "forbidden_files"), I.PASS)


class Unknowns(unittest.TestCase):
    def test_gh_pr_view_failure_all_unknown(self):
        g, _ = run_eval(FakeGh(pr=None))
        self.assertTrue(all(x.status == I.UNKNOWN for x in g) and len(g) == 7)

    def test_git_fetch_failure_unknown(self):
        g, _ = run_eval(git=FakeGit(fetch_rc=1))
        self.assertEqual([x.status for x in g[3:]], [I.UNKNOWN] * 4)

    def test_origin_head_mismatch_unknown(self):
        g, _ = run_eval(git=FakeGit(head_sha="d" * 40))
        self.assertEqual(status(g, "review"), I.UNKNOWN)


class Lock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.t = [1000.0]

    def tearDown(self):
        self.tmp.cleanup()

    def lock(self, **kw):
        return I.IntegrationLock(self.dir, 60, lambda: self.t[0], **kw)

    def test_acquire_release(self):
        a = self.lock()
        a.acquire(7)
        self.assertTrue((self.dir / I.LOCK_NAME).exists())
        data = json.loads((self.dir / I.LOCK_NAME).read_text())
        self.assertEqual((data["pid"], data["pr"]), (os.getpid(), 7))
        a.release()
        self.assertFalse((self.dir / I.LOCK_NAME).exists())

    def test_contention(self):
        a, b = self.lock(), self.lock()
        a.acquire()
        with self.assertRaises(I.LockHeld):
            b.acquire()

    def test_stale_lock_taken_over(self):
        a, b = self.lock(), self.lock()
        a.acquire()
        self.t[0] += 61
        b.acquire()
        self.assertTrue(b.token)
        a.release()   # a no longer owns it: must not delete b's lock
        self.assertTrue((self.dir / I.LOCK_NAME).exists())

    def test_dead_pid_is_stale(self):
        a, b = self.lock(), self.lock(pid_alive=lambda pid: False)
        a.acquire()
        b.acquire()
        self.assertTrue(b.token)

    def test_corrupt_lock_is_stale(self):
        (self.dir / I.LOCK_NAME).write_text("{bad")
        self.lock().acquire()

    def test_release_without_acquire_is_safe(self):
        self.lock().release()


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, gh=None, git=None, yes=True):
        gh = gh or FakeGh()
        res = I.integrate(7, yes=yes, gh=gh, git=git or FakeGit(), state_dir=self.state)
        return res, gh

    def events(self):
        p = self.state / "events.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_dry_run_changes_nothing(self):
        res, gh = self.go(yes=False)
        self.assertEqual(res.outcome, "DRY_RUN")
        self.assertTrue(res.all_pass)
        self.assertEqual((gh.merges(), gh.posted, self.events()), ([], [], []))
        self.assertFalse((self.state / I.LOCK_NAME).exists())

    def test_merge_success(self):
        res, gh = self.go()
        self.assertEqual(res.outcome, "MERGED")
        self.assertEqual(gh.merges(), [["pr", "merge", "7", "--merge", "--match-head-commit", HEAD]])
        self.assertEqual(len(gh.posted), 1)
        self.assertTrue(gh.posted[0].startswith(f"<!-- dev-integrate v1 pr=7 head={HEAD} result=MERGED -->"))
        ev = self.events()
        self.assertEqual((len(ev), ev[0]["kind"], ev[0]["data"]["result"]), (1, "integrate", "MERGED"))
        self.assertFalse((self.state / I.LOCK_NAME).exists())

    def test_never_uses_admin_or_force(self):
        _, gh = self.go()
        for c in gh.calls:
            self.assertFalse({"--admin", "--force", "--auto", "-f"} & set(c))

    def test_gate_fail_no_merge_and_refused_comment(self):
        res, gh = self.go(gh=FakeGh(comments=[]))
        self.assertEqual(res.outcome, "REFUSED")
        self.assertEqual(gh.merges(), [])
        self.assertIn("result=REFUSED -->", gh.posted[0])

    def test_unknown_gate_blocks(self):
        res, gh = self.go(git=FakeGit(fetch_rc=1))
        self.assertEqual((res.outcome, gh.merges()), ("REFUSED", []))

    def test_lock_contention_no_merge(self):
        held = I.IntegrationLock(self.state)
        held.acquire()
        res, gh = self.go()
        self.assertEqual((res.outcome, gh.merges()), ("LOCKED", []))
        self.assertEqual(res.exit_code, 2)
        self.assertTrue((self.state / I.LOCK_NAME).exists())

    def test_stale_lock_does_not_block(self):
        lk = self.state / I.LOCK_NAME
        lk.write_text(json.dumps({"pid": 1, "time": 1.0, "token": "x"}))
        res, _ = self.go()
        self.assertEqual(res.outcome, "MERGED")

    def test_head_moved_between_evaluation_and_merge(self):
        gh = FakeGh()
        git = FakeGit()
        n = {"v": 0}
        orig = gh.__call__

        def moving(args, input=None):
            if args[:2] == ["pr", "view"] and "state,mergeCommit" not in args:
                n["v"] += 1
                if n["v"] >= 2:   # second evaluation (under the lock) sees a new head
                    gh.pr = pr_json(headRefOid="e" * 40)
            return orig(args, input)
        gh.__call__ = moving
        res = I.integrate(7, yes=True, gh=lambda a, i=None: moving(a, i), git=git, state_dir=self.state)
        self.assertEqual(res.outcome, "REFUSED")
        self.assertIn("head moved", res.note)
        self.assertEqual(gh.merges(), [])

    def test_match_head_commit_failure_is_refused_no_blind_retry(self):
        gh = FakeGh(merge_rc=1, merge_out="head branch was modified")
        gh_calls = gh.__call__

        def drift(args, input=None):
            r = gh_calls(args, input)
            if args[:2] == ["pr", "merge"]:
                gh.pr = pr_json(headRefOid="f" * 40)   # head really moved
            return r
        res = I.integrate(7, yes=True, gh=drift, git=FakeGit(), state_dir=self.state)
        self.assertEqual(res.outcome, "REFUSED")
        self.assertEqual(len(gh.merges()), 1)
        self.assertIn("merge failed", res.note)

    def test_failed_merge_retried_at_most_once(self):
        gh = FakeGh(merge_rc=1, merge_out="timeout")
        res, _ = self.go(gh=gh)
        self.assertEqual(res.outcome, "REFUSED")
        self.assertEqual(len(gh.merges()), 2)

    def test_retry_succeeds(self):
        gh = FakeGh()
        gh.merge_results = [1, 0]
        res, _ = self.go(gh=gh)
        self.assertEqual((res.outcome, len(gh.merges())), ("MERGED", 2))

    def test_readback_not_merged_is_refused(self):
        res, gh = self.go(gh=FakeGh(after_state="OPEN"))
        self.assertEqual(res.outcome, "REFUSED")
        self.assertIn("unconfirmed", res.note)
        self.assertEqual(len(gh.merges()), 1)

    def test_readback_failure_is_refused(self):
        res, _ = self.go(gh=FakeGh(view_fail_after=True))
        self.assertEqual(res.outcome, "REFUSED")
        self.assertIn("unconfirmed", res.note)

    def test_lock_released_after_failure(self):
        self.go(gh=FakeGh(merge_rc=1))
        self.assertFalse((self.state / I.LOCK_NAME).exists())

    def test_comment_has_no_secret_or_user_path(self):
        _, gh = self.go()
        self.assertNotIn("Users", gh.posted[0])
        self.assertIn("| 1 | pr_state | PASS |", gh.posted[0])


class Cli(unittest.TestCase):
    def test_dry_run_prints_table_and_exit_code(self):
        buf = io.StringIO()
        rc = I.main(["7"], gh=FakeGh(), git=FakeGit(), out=buf)
        self.assertEqual(rc, 0)
        self.assertIn("| 5 | review | PASS |", buf.getvalue())
        self.assertIn("result: DRY_RUN", buf.getvalue())

    def test_failing_dry_run_exit_one(self):
        rc = I.main(["7"], gh=FakeGh(comments=[]), git=FakeGit(), out=io.StringIO())
        self.assertEqual(rc, 1)

    def test_render_table_escapes_pipes(self):
        t = I.render_table([I.Gate(1, "x", I.FAIL, "a|b")])
        self.assertNotIn("a|b", t)


if __name__ == "__main__":
    unittest.main()
