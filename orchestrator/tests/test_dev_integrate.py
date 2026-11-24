"""Offline tests for dev_supervisor.integrate (T-DEV-021). gh and git are fakes; no network, synthetic data only."""
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
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


def command_names(calls):
    """The git command name of each recorded call, skipping a leading --no-optional-locks option."""
    return {(c[1] if c and c[0] == "--no-optional-locks" and len(c) > 1 else c[0]) for c in calls}


def marker(pr=7, h=DHASH, verdict="APPROVE", reviewer="claude-reviewer-1"):
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
                                   "messageBody": "x\n\nCo-Authored-By: Claude-Worker <noreply@example.test>"}]}
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


FIRST = "1" * 40
TRACK0 = {"schema_version": 1, "tasks": []}


def active_doc(tasks=(), agreements=None, **extra):
    d = {"schema_version": 1, "updated": "2026-01-01", "lease_policy": {"h": 24}, "package_owners": {"a": "b"},
         "shared_files": {"x": 1}, "integration_agreements": agreements or {}, "tasks": list(tasks)}
    d.update(extra)
    return d


def task_rec(tid, branch, **kw):
    d = {"id": tid, "branch": branch, "status": "ACTIVE", "owner": OWNER, "owned_paths": ["p/"], "title": "t"}
    d.update(kw)
    return d


class FakeGit:
    def __init__(self, files=("orchestrator/demo/a.py",), added=("orchestrator/demo/a.py",), lines=("x = 1",),
                 reg=None, head_sha=HEAD, fetch_rc=0, diff=DIFF, wave=None, mb_rc=0, base_active=None,
                 head_active=None, base_track=None, head_track=None, changelog_diff="", wave_main=None,
                 wave_head=None):
        self.files, self.added, self.lines = list(files), list(added), list(lines)
        self.reg = registry() if reg is None else reg
        self.head_sha, self.fetch_rc, self.diff, self.mb_rc = head_sha, fetch_rc, diff, mb_rc
        self.wave, self.wave_main, self.wave_head = wave, wave_main, wave_head
        self.base_active = base_active if base_active is not None else self.reg
        self.head_active = head_active if head_active is not None else self.base_active
        self.base_track = base_track if base_track is not None else TRACK0
        self.head_track = head_track if head_track is not None else self.base_track
        self.changelog_diff = changelog_diff
        self.calls = []

    @staticmethod
    def _ser(o):
        return (0, o if isinstance(o, bytes) else json.dumps(o).encode()) if o != "bad" else (0, b"{not json")

    def __call__(self, args):
        a = list(args)
        self.calls.append(a)
        if a[0] == "fetch":
            return self.fetch_rc, b""
        if a[0] == "rev-parse":
            return 0, self.head_sha.encode()
        if a[0] == "merge-base":
            return self.mb_rc, MB.encode()
        if a[0] == "log":
            return 0, FIRST.encode()
        if a[:2] == ["diff", "--binary"]:
            return 0, self.diff
        if a[0] == "diff" and "--" in a:
            return 0, self.changelog_diff.encode()
        if a[0] == "diff" and "--name-only" in a:
            return 0, "\0".join(self.added if "--diff-filter=AM" in a else self.files).encode()
        if a[0] == "diff" and "-U0" in a:
            return 0, ("+++ b/f\n" + "\n".join("+" + l for l in self.lines)).encode()
        if a[0] == "show":
            ref, name = a[1].split(":", 1)
            on_base = ref in ("origin/main", MB)
            if name == "ACTIVE_TASKS.json":
                return self._ser(self.base_active if on_base else self.head_active)
            if name == "DEVELOPMENT_TRACKER.json":
                return self._ser(self.base_track if on_base else self.head_track)
            if name.endswith("wave.json"):
                src = self.wave_main if ref == "origin/main" else self.wave if ref == FIRST else \
                    self.wave_head if ref.startswith("origin/") else None
                return (0, json.dumps(src).encode()) if src else (1, b"")
        return 1, b""


def status(gates, name):
    return next(g for g in gates if g.name == name).status


def run_eval(gh=None, git=None):
    gates, info = I.evaluate(7, gh or FakeGh(), git or FakeGit())
    return gates, info


class MarkerAndHash(unittest.TestCase):
    def test_marker_valid(self):
        m = I.parse_review_marker(marker())
        self.assertEqual((m["pr"], m["verdict"], m["reviewer"]), (7, "APPROVE", "claude-reviewer-1"))

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

    def test_registry_only_own_task_update_passes(self):
        base = active_doc([task_rec("T-X-001", TASK_BRANCH)])
        head = active_doc([task_rec("T-X-001", TASK_BRANCH, status="UNDER_REVIEW")])
        git = FakeGit(files=["ACTIVE_TASKS.json", "CHANGELOG.md"], base_active=base, head_active=head)
        self.assertEqual(self.scope(git), I.PASS)

    def test_registry_only_unregistered_branch_without_records_still_passes_when_nothing_foreign_changes(self):
        git = FakeGit(files=["SESSION_HANDOFF.md"], base_active=active_doc(), head_active=active_doc())
        self.assertEqual(self.scope(git), I.PASS)

    def test_registry_only_claim_adds_new_task_on_other_branch(self):
        head = active_doc([task_rec("T-N-001", "task/other")])
        git = FakeGit(files=["ACTIVE_TASKS.json"], base_active=active_doc(), head_active=head)
        self.assertEqual(self.scope(git), I.PASS)

    def test_registry_only_other_tasks_record_changed_fails(self):
        base = active_doc([task_rec("T-Y-001", "task/someone-else")])
        head = active_doc([task_rec("T-Y-001", "task/someone-else", status="COMPLETED")])
        git = FakeGit(files=["ACTIVE_TASKS.json"], base_active=base, head_active=head)
        self.assertEqual(self.scope(git), I.FAIL)

    def test_registry_only_task_removed_fails(self):
        base = active_doc([task_rec("T-Y-001", "task/someone-else")])
        git = FakeGit(files=["ACTIVE_TASKS.json"], base_active=base, head_active=active_doc())
        self.assertEqual(self.scope(git), I.FAIL)

    def test_registry_only_own_task_branch_change_fails(self):
        base = active_doc([task_rec("T-X-001", TASK_BRANCH)])
        head = active_doc([task_rec("T-X-001", "task/stolen")])
        git = FakeGit(files=["ACTIVE_TASKS.json"], base_active=base, head_active=head)
        self.assertEqual(self.scope(git), I.FAIL)

    def test_registry_only_shared_sections_changed_fail(self):
        for key, val in (("package_owners", {"a": "me"}), ("shared_files", {}), ("lease_policy", {"h": 99})):
            head = active_doc(**{key: val})
            git = FakeGit(files=["ACTIVE_TASKS.json"], base_active=active_doc(), head_active=head)
            self.assertEqual(self.scope(git), I.FAIL, key)

    def test_registry_only_updated_field_ok(self):
        head = active_doc(updated="2030-01-01")
        git = FakeGit(files=["ACTIVE_TASKS.json"], base_active=active_doc(), head_active=head)
        self.assertEqual(self.scope(git), I.PASS)

    def test_agreement_new_with_own_ids_ok_foreign_ids_fail(self):
        head_tasks = [task_rec("T-N-001", "task/other")]
        ok = active_doc(head_tasks, {"AGR-1": {"task_ids": ["T-N-001"], "paths": ["a"]}})
        bad = active_doc(head_tasks, {"AGR-1": {"task_ids": ["T-N-001", "T-Z-009"], "paths": ["a"]}})
        self.assertEqual(self.scope(FakeGit(files=["ACTIVE_TASKS.json"], base_active=active_doc(), head_active=ok)),
                         I.PASS)
        self.assertEqual(self.scope(FakeGit(files=["ACTIVE_TASKS.json"], base_active=active_doc(), head_active=bad)),
                         I.FAIL)

    def test_agreement_extended_with_own_id_ok(self):
        base = active_doc([], {"AGR-1": {"description": "d", "task_ids": ["T-A-001"], "paths": ["p"]}})
        head = active_doc([task_rec("T-N-001", "task/other")],
                          {"AGR-1": {"description": "d plus", "task_ids": ["T-A-001", "T-N-001"], "paths": ["p"]}})
        self.assertEqual(self.scope(FakeGit(files=["ACTIVE_TASKS.json"], base_active=base, head_active=head)), I.PASS)

    def test_agreement_paths_widened_or_removed_fail(self):
        base = active_doc([], {"AGR-1": {"description": "d", "task_ids": ["T-A-001"], "paths": ["p"]}})
        wide = active_doc([], {"AGR-1": {"description": "d", "task_ids": ["T-A-001"], "paths": ["p", "q"]}})
        self.assertEqual(self.scope(FakeGit(files=["ACTIVE_TASKS.json"], base_active=base, head_active=wide)), I.FAIL)
        self.assertEqual(self.scope(FakeGit(files=["ACTIVE_TASKS.json"], base_active=base, head_active=active_doc())),
                         I.FAIL)

    def test_tracker_own_vs_foreign_records(self):
        base_a = active_doc([task_rec("T-X-001", TASK_BRANCH), task_rec("T-Y-001", "task/else")])
        bt = {"tasks": [{"task_id": "T-X-001", "steps": []}, {"task_id": "T-Y-001", "steps": []}]}
        own = {"tasks": [{"task_id": "T-X-001", "steps": [1]}, {"task_id": "T-Y-001", "steps": []}]}
        foreign = {"tasks": [{"task_id": "T-X-001", "steps": []}, {"task_id": "T-Y-001", "steps": [1]}]}
        ok = FakeGit(files=["DEVELOPMENT_TRACKER.json"], base_active=base_a, base_track=bt, head_track=own)
        bad = FakeGit(files=["DEVELOPMENT_TRACKER.json"], base_active=base_a, base_track=bt, head_track=foreign)
        self.assertEqual(self.scope(ok), I.PASS)
        self.assertEqual(self.scope(bad), I.FAIL)

    def test_tracker_top_level_change_fails(self):
        git = FakeGit(files=["DEVELOPMENT_TRACKER.json"], base_active=active_doc(),
                      base_track={"tasks": [], "step_statuses": {"a": 1}}, head_track={"tasks": [], "step_statuses": {}})
        self.assertEqual(self.scope(git), I.FAIL)

    def test_changelog_append_only(self):
        add = "@@ -1,0 +2 @@\n+new entry\n"
        rem = "@@ -2 +2 @@\n-old\n+new\n"
        for diff, want in ((add, I.PASS), (rem, I.FAIL)):
            git = FakeGit(files=["CHANGELOG.md"], base_active=active_doc(), changelog_diff=diff)
            self.assertEqual(self.scope(git), want, diff)

    def test_unparseable_registry_unknown(self):
        git = FakeGit(files=["ACTIVE_TASKS.json"], base_active="bad")
        self.assertEqual(self.scope(git), I.UNKNOWN)

    def test_registry_plus_other_file_not_a_record(self):
        git = FakeGit(files=["ACTIVE_TASKS.json", "evil.py"], reg=registry(branch="task/none"))
        self.assertEqual(self.scope(git), I.FAIL)

    def test_no_changed_files(self):
        self.assertEqual(self.scope(FakeGit(files=[])), I.FAIL)

    def wave_manifest(self):
        return {"lanes": [{"id": "L1", "owner": "claude:coord", "owned_paths": ["orchestrator/a.py"]},
                          {"id": "L2", "owner": "claude:w2", "owned_paths": ["orchestrator/tests/test_a.py"]}]}

    def wave_manifest(self):
        return {"lanes": [{"id": "L1", "task_id": "T-W-001", "owner": "claude:coord",
                           "owned_paths": ["orchestrator/a.py"]},
                          {"id": "L2", "task_id": "T-W-002", "owner": "claude:w2",
                           "owned_paths": ["orchestrator/tests/test_a.py"]}]}

    def wave_gh(self):
        return FakeGh(pr=pr_json(headRefName="wave/w01-x"))

    def test_wave_inside_lane_paths_and_closeout(self):
        base = active_doc([task_rec("T-W-001", "lane/w01-x/L1"), task_rec("T-W-002", "lane/w01-x/L2")])
        head = active_doc([task_rec("T-W-001", "lane/w01-x/L1", status="COMPLETED"),
                           task_rec("T-W-002", "lane/w01-x/L2", status="COMPLETED")])
        git = FakeGit(files=["orchestrator/a.py", "orchestrator/tests/test_a.py", "waves/w01-x/lanes/L1/report.json",
                             "ACTIVE_TASKS.json", "CHANGELOG.md"], wave=self.wave_manifest(),
                      base_active=base, head_active=head)
        self.assertEqual(self.scope(git, self.wave_gh()), I.PASS)

    def test_wave_registry_change_to_foreign_task_fails(self):
        base = active_doc([task_rec("T-W-001", "lane/w01-x/L1"), task_rec("T-Y-001", "task/else")])
        head = active_doc([task_rec("T-W-001", "lane/w01-x/L1"), task_rec("T-Y-001", "task/else", status="COMPLETED")])
        git = FakeGit(files=["orchestrator/a.py", "ACTIVE_TASKS.json"], wave=self.wave_manifest(),
                      base_active=base, head_active=head)
        self.assertEqual(self.scope(git, self.wave_gh()), I.FAIL)

    def test_wave_registry_change_unrelated_to_lane_tasks_without_registry_files_ok(self):
        git = FakeGit(files=["orchestrator/a.py"], wave=self.wave_manifest())
        self.assertEqual(self.scope(git, self.wave_gh()), I.PASS)

    def test_wave_outside_lane_paths(self):
        git = FakeGit(files=["orchestrator/a.py", "orchestrator/other.py"], wave=self.wave_manifest())
        self.assertEqual(self.scope(git, self.wave_gh()), I.FAIL)

    def test_wave_manifest_from_main_accepted(self):
        git = FakeGit(files=["orchestrator/a.py"], wave_main=self.wave_manifest())
        self.assertEqual(self.scope(git, self.wave_gh()), I.PASS)

    def test_wave_manifest_never_taken_from_head(self):
        widened = {"lanes": [{"id": "L1", "task_id": "T-W-001", "owner": "o", "owned_paths": ["orchestrator/"]}]}
        git = FakeGit(files=["orchestrator/anything.py"], wave=self.wave_manifest(), wave_head=widened)
        self.assertEqual(self.scope(git, self.wave_gh()), I.FAIL)
        git2 = FakeGit(files=["orchestrator/a.py"], wave_head=self.wave_manifest())
        self.assertEqual(self.scope(git2, self.wave_gh()), I.UNKNOWN)

    def test_wave_manifest_missing_unknown(self):
        self.assertEqual(self.scope(FakeGit(files=["orchestrator/a.py"]), self.wave_gh()), I.UNKNOWN)


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
        cs = [comment(marker()), comment(marker(verdict="CHANGES_REQUESTED", reviewer="claude-reviewer-2"), 2)]
        self.assertEqual(self.review(cs), I.FAIL)

    def test_reviewer_equal_task_owner_fails(self):
        self.assertEqual(self.review([comment(marker(reviewer='demo-worker'))]), I.FAIL)

    def test_reviewer_equal_coauthor_fails(self):
        self.assertEqual(self.review([comment(marker(reviewer="Claude-Worker"))]), I.FAIL)

    def test_reviewer_equal_author_login_fails(self):
        self.assertEqual(self.review([comment(marker(reviewer="dev"))]), I.FAIL)

    def test_reviewer_equal_author_email_fails(self):
        self.assertEqual(self.review([comment(marker(reviewer="dev"))]), I.FAIL)

    def test_independent_approve_beside_author_approve_passes(self):
        self.assertEqual(self.review([comment(marker(reviewer='demo-worker')), comment(marker(), 2)]), I.PASS)

    def test_comments_unreadable_unknown(self):
        self.assertEqual(self.review([], {"comments_rc": 1}), I.UNKNOWN)

    def test_wave_review_required_too(self):
        gh = FakeGh(pr=pr_json(headRefName="wave/w01-x"), comments=[comment("no marker")])
        git = FakeGit(files=["orchestrator/a.py"], wave={"lanes": [{"owner": "claude:c", "owned_paths": ["orchestrator/a.py"]}]})
        g, _ = run_eval(gh, git)
        self.assertEqual(status(g, "review"), I.FAIL)


class RegistryOnlyReview(unittest.TestCase):
    def review(self, files, head=TASK_BRANCH, wave=None, **kw):
        gh = FakeGh(pr=pr_json(headRefName=head), comments=[])
        g, _ = run_eval(gh, FakeGit(files=files, wave=wave, **{"base_active": active_doc(), **kw}))
        return next(x for x in g if x.name == "review")

    def test_registry_only_exempt(self):
        g = self.review(["ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json", "SESSION_HANDOFF.md"])
        self.assertEqual(g.status, I.PASS)
        self.assertIn("registry-only record: no code review required (protocol section 18 gate 4)", g.detail)

    def test_registry_only_but_foreign_change_needs_review(self):
        base = active_doc([task_rec("T-Y-001", "task/else")])
        head = active_doc([task_rec("T-Y-001", "task/else", status="COMPLETED")])
        g = self.review(["ACTIVE_TASKS.json"], base_active=base, head_active=head)
        self.assertEqual(g.status, I.FAIL)

    def test_registry_only_unparseable_unknown(self):
        self.assertEqual(self.review(["ACTIVE_TASKS.json"], head_active="bad").status, I.UNKNOWN)

    def test_registry_plus_code_file_needs_review(self):
        self.assertEqual(self.review(["ACTIVE_TASKS.json", "orchestrator/x.py"]).status, I.FAIL)

    def test_wave_pr_still_needs_review(self):
        w = {"lanes": [{"task_id": "T-W-001", "owner": "claude:c", "owned_paths": ["orchestrator/a.py"]}]}
        self.assertEqual(self.review(["ACTIVE_TASKS.json", "CHANGELOG.md"], "wave/w01-x", w).status, I.FAIL)


class Reserved(unittest.TestCase):
    URL = "https://github.com/owner/repo/issues/12#issuecomment-345"

    def gate(self, files, word=""):
        g, _ = I.evaluate(7, FakeGh(), FakeGit(files=files), "main", word)
        return next(x for x in g if x.name == "reserved")

    def test_ordinary_paths_pass(self):
        self.assertEqual(self.gate(["orchestrator/demo/a.py"]).status, I.PASS)

    def test_each_reserved_path_fails(self):
        for f in ("PROJECT_OPERATING_PROTOCOL.md", "DEVELOPMENT_STEP_PROTOCOL.md", "WAVE_PROTOCOL.md", "CLAUDE.md",
                  ".github/workflows/validate.yml", ".githooks/pre-push", "orchestrator/governance/waves.py",
                  "dashboard/tracker_check.py", "pro.ps1", "x/backup.ps1", ".mcp.json"):
            self.assertEqual(self.gate([f]).status, I.FAIL, f)

    def test_override_recorded_in_table(self):
        g = self.gate(["CLAUDE.md"], self.URL)
        self.assertEqual(g.status, I.PASS)
        self.assertIn(self.URL, g.detail)

    def test_override_url_validated(self):
        with self.assertRaises(ValueError):
            I.integrate(7, lukas_word="not a url", gh=FakeGh(), git=FakeGit())
        with self.assertRaises(SystemExit):
            I.main(["7", "--lukas-direct-word", "javascript:alert(1)"], gh=FakeGh(), git=FakeGit(), out=io.StringIO())

    def test_override_in_comment_and_gate_count(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            git = FakeGit(files=["orchestrator/demo/a.py", "CLAUDE.md"])
            git.base_active = registry(owned=("orchestrator/demo/", "CLAUDE.md"))
            git.reg = git.base_active
            git.head_active = git.base_active
            res = I.integrate(7, yes=True, gh=gh, git=git, state_dir=d, lukas_word=self.URL)
            self.assertEqual(res.outcome, "MERGED")
            self.assertIn(f"lukas-direct-word: {self.URL}", gh.posted[0])
            self.assertEqual(len(res.gates), 8)

    def test_reserved_blocks_merge_without_override(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            git = FakeGit(files=["orchestrator/demo/a.py", "CLAUDE.md"])
            res = I.integrate(7, yes=True, gh=gh, git=git, state_dir=d)
            self.assertEqual((res.outcome, gh.merges()), ("REFUSED", []))


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
        self.assertTrue(all(x.status == I.UNKNOWN for x in g) and len(g) == 8)

    def test_git_fetch_failure_unknown(self):
        g, _ = run_eval(git=FakeGit(fetch_rc=1))
        self.assertEqual([x.status for x in g[3:]], [I.UNKNOWN] * 5)

    def test_origin_head_mismatch_unknown(self):
        g, _ = run_eval(git=FakeGit(head_sha="d" * 40))
        self.assertEqual(status(g, "review"), I.UNKNOWN)


DEAD = 2 ** 30 + 7


def alive_me_only(pid):
    return pid == os.getpid()


class Lock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / I.LOCK_NAME
        self.t = [time.time()]

    def tearDown(self):
        self.tmp.cleanup()

    def lock(self, alive=alive_me_only):
        return I.IntegrationLock(self.dir, 60, lambda: self.t[0], alive)

    def write_lock(self, text, age=0.0):
        self.path.write_text(text, encoding="utf-8")
        os.utime(str(self.path), (self.t[0] - age, self.t[0] - age))

    def test_acquire_release(self):
        a = self.lock()
        a.acquire(7)
        data = json.loads(self.path.read_text())
        self.assertEqual((data["pid"], data["pr"]), (os.getpid(), 7))
        a.release()
        self.assertFalse(self.path.exists())

    def test_contention(self):
        a, b = self.lock(), self.lock()
        a.acquire()
        with self.assertRaises(I.LockHeld):
            b.acquire()

    def test_dead_holder_taken_over_and_old_owner_cannot_release(self):
        self.write_lock(json.dumps({"pid": DEAD, "time": 1.0, "token": "old"}))
        b = self.lock()
        b.acquire()
        a = self.lock()
        a.token = "old"
        a.release()
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text())["token"], b.token)
        self.assertEqual([p.name for p in self.dir.iterdir() if ".stale." in p.name], [])

    def test_live_long_holder_not_stolen(self):
        self.write_lock(json.dumps({"pid": os.getpid(), "time": 1.0, "token": "t"}), age=100000)
        with self.assertRaises(I.LockHeld):
            self.lock().acquire()

    def test_empty_fresh_lock_is_respected(self):
        self.write_lock("", age=1)
        with self.assertRaises(I.LockHeld):
            self.lock().acquire()
        self.assertEqual(self.path.read_text(), "")

    def test_torn_fresh_lock_is_respected(self):
        self.write_lock("{\"pid\": 12", age=1)
        with self.assertRaises(I.LockHeld):
            self.lock().acquire()

    def test_empty_old_lock_is_stale(self):
        self.write_lock("", age=61)
        self.lock().acquire()

    def test_heartbeat_refreshes_mtime_and_detects_loss(self):
        a = self.lock()
        a.acquire()
        os.utime(str(self.path), (self.t[0] - 50, self.t[0] - 50))
        a.heartbeat()
        self.assertLess(abs(os.path.getmtime(str(self.path)) - self.t[0]), 2)
        self.path.write_text(json.dumps({"pid": os.getpid(), "token": "someone-else"}))
        with self.assertRaises(I.LockHeld):
            a.heartbeat()

    def test_concurrent_takeover_leaves_exactly_one_holder(self):
        self.write_lock(json.dumps({"pid": DEAD, "time": 1.0, "token": "old"}))
        n, won, barrier = 8, [], threading.Barrier(8)

        def worker():
            lk = self.lock()
            barrier.wait()
            try:
                lk.acquire()
                won.append(lk)
            except I.LockHeld:
                pass
        ts = [threading.Thread(target=worker) for _ in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(won), 1)
        self.assertEqual(json.loads(self.path.read_text())["token"], won[0].token)

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
        lk.write_text(json.dumps({"pid": DEAD, "time": 1.0, "token": "x"}))
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


class PreGit(FakeGit):
    """FakeGit that also answers the extra reads preflight makes: the branch name and the working-tree status."""

    def __init__(self, branch=TASK_BRANCH, dirty="", boom=None, status_rc=0, handoff="fresh", **kw):
        super().__init__(**kw)
        self.branch, self.dirty, self.boom, self.status_rc, self.handoff = branch, dirty, boom, status_rc, handoff

    def __call__(self, args):
        a = list(args)
        if self.boom and a[0] == self.boom:
            raise RuntimeError("synthetic git failure")
        if a[0] == "show" and a[1] == "HEAD:PROJECT_STATUS.json":
            self.calls.append(a)
            return 0, b"{}"
        if a[0] == "show" and a[1] == "HEAD:SESSION_HANDOFF.md":
            self.calls.append(a)
            return {"fresh": (0, RENDERED.encode()), "crlf": (0, RENDERED.replace("\n", "\r\n").encode()),
                    "stale": (0, b"an old handoff\n"), "missing": (1, b"")}[self.handoff]
        if a[:2] == ["rev-parse", "--abbrev-ref"]:
            self.calls.append(a)
            return 0, self.branch.encode()
        if a[:3] == ["--no-optional-locks", "status", "--porcelain"]:
            self.calls.append(a)
            return self.status_rc, self.dirty.encode()
        return super().__call__(args)


RENDERED = "# handoff\nrendered text\n"


def pre(git=None, **kw):
    git = git or PreGit(**kw)
    gates, info = I.preflight(git)
    return gates, info, git


class Preflight(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        patcher = mock.patch("governance.handoff.render", return_value=RENDERED)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_clean_branch_passes_the_four_offline_gates_and_reports_the_diff_hash(self):
        gates, info, _ = pre()
        self.assertEqual([(g.num, g.name, g.status) for g in gates],
                         [(1, "branch_name", I.PASS), (2, "handoff_current", I.PASS), (4, "scope", I.PASS),
                          (6, "secrets", I.PASS), (7, "forbidden_files", I.PASS), (8, "reserved", I.PASS)])
        self.assertEqual((info["head"], info["branch"], info["diff_sha"], info["dirty"]), (HEAD, TASK_BRANCH, DHASH, False))

    def test_it_reads_the_local_head_not_the_remote_branch_and_only_reads(self):
        _, _, git = pre()
        flat = [x for call in git.calls for x in call]
        self.assertIn("HEAD", flat)
        self.assertFalse([c for c in git.calls if any(x == "origin/" + TASK_BRANCH or x.startswith("origin/" + TASK_BRANCH + ":")
                                                      for x in c)], "preflight must not read origin/<branch>")
        self.assertLessEqual(command_names(git.calls), {"fetch", "rev-parse", "status", "merge-base", "diff", "show", "log"})

    def test_the_real_evaluation_still_reads_the_remote_branch(self):
        git = FakeGit()
        I.evaluate(7, FakeGh(), git)
        self.assertTrue([c for c in git.calls if "origin/" + TASK_BRANCH in c], "the default path must be unchanged")
        self.assertFalse([c for c in git.calls if c[:2] == ["rev-parse", "HEAD"]])

    def test_each_offline_gate_fails_for_the_same_reason_as_a_real_run(self):
        cases = {
            "outside owned paths": dict(files=["orchestrator/demo/a.py", "README.md"], added=[]),
            "credential-shaped line": dict(lines=["key = " + fake_secret()]),
            "launcher file added": dict(files=["backup.ps1"], added=["backup.ps1"]),
            "reserved path changed": dict(files=["orchestrator/governance/x.py"], added=[]),
        }
        for name, kw in cases.items():
            with self.subTest(case=name):
                gates, _, _ = pre(**kw)
                real, _ = I.evaluate(7, FakeGh(), FakeGit(**kw))
                self.assertFalse(all(g.status == I.PASS for g in gates), "the case must fail somewhere")
                for g in gates:
                    if g.name in ("branch_name", "handoff_current"):
                        continue          # no counterpart in a real evaluation; covered by their own tests
                    r = next(x for x in real if x.name == g.name)
                    self.assertEqual((g.status, g.detail), (r.status, r.detail), g.name)

    def test_a_passing_case_matches_the_real_evaluation_gate_by_gate(self):
        gates, _, _ = pre()
        real, _ = I.evaluate(7, FakeGh(), FakeGit())
        for g in gates:
            if g.name in ("branch_name", "handoff_current"):
                continue
            r = next(x for x in real if x.name == g.name)
            self.assertEqual((g.status, g.detail), (r.status, r.detail), g.name)

    def test_a_close_out_on_a_branch_that_is_not_the_tasks_own_fails_scope_like_a_real_run(self):
        base = active_doc([task_rec("T-X", "task/x")])
        done = active_doc([task_rec("T-X", "task/x", status="COMPLETED")])
        kw = dict(files=["ACTIVE_TASKS.json", "SESSION_HANDOFF.md"], added=[], base_active=base, head_active=done)
        wrong, _, _ = pre(branch="task/closeout", **kw)
        right, _, _ = pre(branch="task/x", **kw)
        self.assertEqual(status(wrong, "scope"), I.FAIL)
        self.assertIn("not this PR's own task", next(g for g in wrong if g.name == "scope").detail)
        self.assertEqual(status(right, "scope"), I.PASS)

    def test_git_failures_make_every_gate_unknown_and_never_raise(self):
        for label, kw in (("fetch fails", dict(fetch_rc=1)), ("no merge base", dict(mb_rc=1)),
                          ("detached HEAD", dict(branch="HEAD")), ("empty branch", dict(branch="")),
                          ("git raises", dict(boom="fetch")), ("git raises on rev-parse", dict(boom="rev-parse"))):
            with self.subTest(case=label):
                gates, _, _ = pre(**kw)
                self.assertEqual([g.num for g in gates], [1, 2, 4, 6, 7, 8])
                self.assertTrue(all(g.status == I.UNKNOWN for g in gates), [(g.name, g.status) for g in gates])

    def test_an_exception_inside_one_gate_makes_that_gate_unknown_and_leaves_the_others(self):
        from unittest import mock
        with mock.patch.object(I, "_gate_secrets", side_effect=RuntimeError("synthetic gate failure")):
            gates, _, _ = pre()
        self.assertEqual([(g.name, g.status) for g in gates], [("branch_name", I.PASS), ("handoff_current", I.PASS),
                                                               ("scope", I.PASS), ("secrets", I.UNKNOWN),
                                                               ("forbidden_files", I.PASS), ("reserved", I.PASS)])
        self.assertIn("RuntimeError", next(g for g in gates if g.name == "secrets").detail)

    def test_uncommitted_changes_are_reported_not_evaluated(self):
        _, info, _ = pre(dirty=" M orchestrator/demo/a.py")
        self.assertTrue(info["dirty"])
        buf = io.StringIO()
        rc = I.main(["--preflight"], git=PreGit(dirty=" M x"), out=buf)
        self.assertEqual(rc, 0)
        self.assertIn("uncommitted changes are NOT evaluated", buf.getvalue())

    def test_a_failing_git_status_counts_as_dirty_so_the_warning_is_never_dropped(self):
        _, info, _ = pre(status_rc=1)
        self.assertTrue(info["dirty"])
        _, clean, _ = pre()
        self.assertFalse(clean["dirty"])
        buf = io.StringIO()
        I.main(["--preflight"], git=PreGit(status_rc=128), out=buf)
        self.assertIn("uncommitted changes are NOT evaluated", buf.getvalue())

    def test_the_cli_prints_the_table_the_hash_and_the_limits(self):
        buf = io.StringIO()
        rc = I.main(["--preflight"], git=PreGit(), out=buf)
        text = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("diff " + DHASH, text)
        self.assertIn("| 4 | scope | PASS |", text)
        self.assertIn("result: PREFLIGHT_PASS", text)
        self.assertIn("CI, merge state and review need the pull request", text)
        self.assertNotIn("| 5 | review |", text)

    def test_the_cli_exits_one_on_a_failing_gate(self):
        buf = io.StringIO()
        rc = I.main(["--preflight"], git=PreGit(lines=["key = " + fake_secret()]), out=buf)
        self.assertEqual(rc, 1)
        self.assertIn("PREFLIGHT_FAIL", buf.getvalue())
        self.assertIn("| 6 | secrets | FAIL |", buf.getvalue())

    def test_the_cli_usage_rules(self):
        for argv in (["--preflight", "7"], ["--preflight", "--yes"], []):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm:
                I.main(argv, gh=FakeGh(), git=PreGit(), out=io.StringIO())
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(I.main(["7"], gh=FakeGh(), git=FakeGit(), out=io.StringIO()), 0)   # unchanged

    def test_the_secret_patterns_equal_the_regex_ci_runs(self):
        """Preflight only prevents CI failures while the gate is at least as strict as CI's scan; this pins them equal."""
        import re
        wf = Path(__file__).resolve().parents[2] / ".github" / "workflows"
        text = "".join(p.read_text(encoding="utf-8") for p in sorted(wf.glob("*.yml"))) if wf.is_dir() else ""
        found = re.findall(r'grep -E "(\(\(\^\|\[\^A-Za-z0-9\]\)sk-.*?)"', text)
        if not text:
            self.skipTest("no workflow files next to the tests")
        self.assertEqual(len(found), 1, "the CI secret-scan line was not found or is ambiguous; update this test")
        from governance import waves
        self.assertEqual(waves.SECRET.pattern, found[0])
        self.assertEqual(I._FALLBACK_SECRET.pattern, found[0])


class PreflightBranchAndHandoff(unittest.TestCase):
    """T-DEV-044: the two checks CI or the real pr_state gate would otherwise make only after a push."""

    def setUp(self):
        from unittest import mock
        patcher = mock.patch("governance.handoff.render", return_value=RENDERED)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_allowed_branch_prefixes_pass_and_others_fail_like_the_real_pr_state_gate(self):
        for branch in ("task/x", "wave/w1-l1", "chatgpt/x", "claim/x", "main", "feature/y", "tasks/x", ""):
            with self.subTest(branch=branch):
                gates, _, _ = pre(branch=branch) if branch else pre(branch="x")
                got = next(g for g in gates if g.name == "branch_name")
                ctx = I._Ctx(pr={"state": "OPEN", "isDraft": False, "baseRefName": "main",
                                 "headRefName": branch if branch else "x"}, base="main")
                real = I._gate_pr_state(ctx)
                self.assertEqual(got.status, real.status)
                if got.status == I.FAIL:
                    for prefix in I.BRANCH_PREFIXES:
                        self.assertIn(prefix, got.detail)

    def test_a_fresh_handoff_passes_and_line_endings_do_not_matter(self):
        for mode in ("fresh", "crlf"):
            with self.subTest(mode=mode):
                gates, _, _ = pre(handoff=mode)
                self.assertEqual(status(gates, "handoff_current"), I.PASS)

    def test_a_stale_handoff_fails_and_names_the_command_that_fixes_it(self):
        gates, _, _ = pre(handoff="stale")
        g = next(x for x in gates if x.name == "handoff_current")
        self.assertEqual(g.status, I.FAIL)
        self.assertIn("governance handoff --write", g.detail)

    def test_the_comparison_is_as_strict_as_ci_a_missing_trailing_newline_is_stale(self):
        class NoFinalNewline(PreGit):
            def __call__(self, args):
                if list(args)[:2] == ["show", "HEAD:SESSION_HANDOFF.md"]:
                    return 0, RENDERED.rstrip().encode()
                return super().__call__(args)
        gates, _, _ = pre(git=NoFinalNewline())
        self.assertEqual(status(gates, "handoff_current"), I.FAIL)

    def test_an_unreadable_or_unrenderable_handoff_is_unknown_never_pass(self):
        from unittest import mock
        gates, _, _ = pre(handoff="missing")
        self.assertEqual(status(gates, "handoff_current"), I.UNKNOWN)
        with mock.patch("governance.handoff.render", side_effect=KeyError("title")):
            gates, _, _ = pre()
        g = next(x for x in gates if x.name == "handoff_current")
        self.assertEqual(g.status, I.UNKNOWN)
        self.assertIn("KeyError", g.detail)

    def test_the_cli_exits_one_and_reports_the_stale_handoff_and_a_bad_branch(self):
        buf = io.StringIO()
        rc = I.main(["--preflight"], git=PreGit(handoff="stale", branch="claim/x"), out=buf)
        text = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("| 1 | branch_name | FAIL |", text)
        self.assertIn("| 2 | handoff_current | FAIL |", text)
        self.assertIn("PREFLIGHT_FAIL", text)

    def test_the_new_gates_only_read_git(self):
        _, _, git = pre()
        shown = [c for c in git.calls if c[0] == "show" and c[1].startswith("HEAD:")]
        self.assertTrue({"HEAD:PROJECT_STATUS.json", "HEAD:ACTIVE_TASKS.json", "HEAD:SESSION_HANDOFF.md"} <= {c[1] for c in shown})
        self.assertLessEqual(command_names(git.calls), {"fetch", "rev-parse", "status", "merge-base", "diff", "show", "log"})

    def test_preflight_uses_no_optional_locks(self):
        _, _, git = pre()
        self.assertTrue(any(c == ["--no-optional-locks", "status", "--porcelain"] for c in git.calls))

    def test_command_names_skip_the_option_and_still_report_other_commands(self):
        self.assertEqual(command_names([["--no-optional-locks", "status", "--porcelain"], ["diff", "x"]]), {"status", "diff"})
        self.assertEqual(command_names([["--no-optional-locks", "status"], ["push", "origin"]]), {"status", "push"})


class PreflightHandoffOnTheRealRepository(unittest.TestCase):
    def test_the_gate_agrees_with_what_governance_would_render_from_the_committed_files(self):
        root = Path(__file__).resolve().parents[2]
        gate = I._gate_handoff_current(I.real_git_runner(root))
        if gate.status == I.UNKNOWN:
            self.skipTest("the repository history is not readable here: " + gate.detail)
        self.assertEqual(gate.status, I.PASS, gate.detail)


if __name__ == "__main__":
    unittest.main()
