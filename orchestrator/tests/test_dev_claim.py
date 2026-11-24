"""Tests for dev_supervisor.claim (T-DEV-045).

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_claim.py
They work on temporary copies of the repository's real ACTIVE_TASKS.json and PROJECT_STATUS.json, so the governance
validation and the handoff renderer are the real ones.
"""
import copy
import datetime as dt
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import claim as C  # noqa: E402
from dev_supervisor import integrate as I  # noqa: E402
from governance import handoff as handoff_mod  # noqa: E402
from governance import tasks as tasks_mod  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 11, 24, 12, 0, 0, tzinfo=dt.timezone.utc)


def spec(**over):
    s = {
        "id": "T-ZZZ-901", "title": "demo task", "description": "A demo task used only by the claim tests.",
        "owner": "claude:main-coordinator", "branch": "task/t-zzz-901-demo", "worktree": "../CreatorStudio-worktrees/t-zzz-901-demo",
        "owned_paths": ["orchestrator/dev_supervisor/zzz_demo.py", "orchestrator/tests/test_zzz_demo.py"],
        "agreement_id": "AGR-ZZZ-901-01",
        "agreement": "The dev_supervisor package is owned by claude:repository-coordinator. T-ZZZ-901 may add a demo module there.",
        "acceptance_criteria": ["it works"], "test_requirements": ["python -B -I -m unittest"], "integration_notes": "Claimed for a test.",
    }
    s.update(over)
    return s


def fake_credential():
    return "sk-" + "a1b2c3d4" * 3          # built by concatenation so this file does not trip the CI secret scan


class Repo:
    """A temporary checkout holding copies of the two registry inputs, optionally with CRLF line endings."""

    def __init__(self, test, crlf=False):
        self.dir = Path(tempfile.mkdtemp(prefix="claim-test-"))
        test.addCleanup(shutil.rmtree, self.dir, True)
        for name in ("ACTIVE_TASKS.json", "PROJECT_STATUS.json"):
            data = (REPO / name).read_bytes().replace(b"\r\n", b"\n")
            (self.dir / name).write_bytes(data.replace(b"\n", b"\r\n") if crlf and name == "ACTIVE_TASKS.json" else data)
        status = json.loads((self.dir / "PROJECT_STATUS.json").read_text(encoding="utf-8"))
        doc = json.loads((self.dir / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
        (self.dir / "SESSION_HANDOFF.md").write_text(handoff_mod.render(status, doc), encoding="utf-8", newline="\n")

    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.dir.iterdir())}

    def doc(self):
        return json.loads((self.dir / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))


class CheckSpec(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads((REPO / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))

    def refused(self, **over):
        return C.check_spec(spec(**over), self.doc)

    def test_a_valid_spec_has_no_problems(self):
        self.assertEqual(C.check_spec(spec(), self.doc), [])

    def test_each_claim_rule_is_named_when_broken(self):
        taken = self.doc["tasks"][0]
        cases = {
            "duplicate task id": (dict(id=taken["id"]), "already registered"),
            "bad id shape": (dict(id="demo-1"), "must look like T-"),
            "duplicate agreement": (dict(agreement_id=next(iter(self.doc["integration_agreements"]))), "already exists"),
            "bad agreement id": (dict(agreement_id="AGR-1"), "must look like AGR-"),
            "branch outside task/": (dict(branch="claim/x"), "start with task/"),
            "branch used by another task": (dict(branch=taken["branch"]), "already registered to another task"),
            "absolute worktree": (dict(worktree="/tmp/x"), "worktree must be a relative path"),
            "windows worktree": (dict(worktree="C:/x"), "worktree must be a relative path"),
            "parent inside worktree": (dict(worktree="../a/../b"), "worktree must be a relative path"),
            "absolute owned path": (dict(owned_paths=["/etc/x"]), "owned_paths"),
            "parent-relative owned path": (dict(owned_paths=["orchestrator/../x.py"]), "owned_paths"),
            "backslash owned path": (dict(owned_paths=["orchestrator\\x.py"]), "owned_paths"),
            "foreign task id in the agreement": (dict(agreement="Added in T-DEV-038 and extended by T-ZZZ-901."), "other task ids"),
            "credential-shaped text": (dict(integration_notes="key " + fake_credential()), "credential"),
            "empty owned paths": (dict(owned_paths=[]), "owned_paths is required"),
            "unknown field": (dict(extra="x"), "unknown field"),
            "bad owner": (dict(owner="someone"), "owner must look like"),
        }
        for name, (over, expect) in cases.items():
            with self.subTest(case=name):
                probs = self.refused(**over)
                self.assertTrue(probs, "the case must be refused")
                self.assertTrue(any(expect in p for p in probs), probs)

    def test_a_missing_or_mistyped_field_is_refused(self):
        for key in C.STR_FIELDS + C.LIST_FIELDS:
            with self.subTest(missing=key):
                s = spec()
                del s[key]
                self.assertTrue(any(key in p for p in C.check_spec(s, self.doc)))
        self.assertEqual(C.check_spec([], self.doc), ["the spec must be a JSON object"])
        self.assertTrue(C.check_spec(spec(title=5), self.doc))

    def test_the_foreign_id_rule_is_the_one_the_integration_tool_applies(self):
        """A spec that passes must make a claim the integration tool's own ownership check accepts."""
        task, agreement = C.build_records(spec(), NOW)
        self.assertEqual(I._tids(agreement) - {task["id"]}, set())


class Claim(unittest.TestCase):
    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        repo = Repo(self)
        before = repo.snapshot()
        buf = io.StringIO()
        specfile = repo.dir / "spec.json"
        specfile.write_text(json.dumps(spec()), encoding="utf-8")
        before = repo.snapshot()
        rc = C.main([str(specfile), "--repo", str(repo.dir)], out=buf, now=NOW)
        self.assertEqual(rc, 0)
        self.assertIn("dry run, nothing written", buf.getvalue())
        self.assertEqual(repo.snapshot(), before)

    def test_write_adds_exactly_one_task_and_one_agreement_and_regenerates_the_handoff(self):
        repo = Repo(self)
        old = repo.doc()
        probs, task = C.claim(repo.dir, spec(), write=True, now=NOW)
        self.assertEqual(probs, [])
        new = repo.doc()
        self.assertEqual(new["tasks"][:-1], old["tasks"])
        self.assertEqual(new["tasks"][-1], task)
        self.assertEqual(set(new["integration_agreements"]) - set(old["integration_agreements"]), {"AGR-ZZZ-901-01"})
        self.assertEqual({k: v for k, v in new.items() if k not in ("tasks", "integration_agreements")},
                         {k: v for k, v in old.items() if k not in ("tasks", "integration_agreements")})
        self.assertEqual(task["lease"], {"acquired": "2026-11-24T12:00:00+00:00", "expires": "2026-11-25T12:00:00+00:00"})
        self.assertEqual((task["status"], task["owner"], task["exclusive_resources"]), ("ACTIVE", "claude:main-coordinator", []))

    def test_the_written_result_passes_governance_validation_and_the_handoff_check(self):
        repo = Repo(self)
        C.claim(repo.dir, spec(), write=True, now=NOW)
        self.assertEqual(tasks_mod.validate(repo.doc()), [])
        status = json.loads((repo.dir / "PROJECT_STATUS.json").read_text(encoding="utf-8"))
        written = (repo.dir / "SESSION_HANDOFF.md").read_text(encoding="utf-8")
        self.assertEqual(written.replace("\r\n", "\n"), handoff_mod.render(status, repo.doc()).replace("\r\n", "\n"))
        self.assertIn("T-ZZZ-901", written)

    def test_line_endings_of_the_registry_are_preserved(self):
        for crlf in (False, True):
            with self.subTest(crlf=crlf):
                repo = Repo(self, crlf=crlf)
                C.claim(repo.dir, spec(), write=True, now=NOW)
                raw = (repo.dir / "ACTIVE_TASKS.json").read_bytes()
                self.assertEqual(b"\r\n" in raw, crlf)
                if crlf:
                    self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_the_result_is_accepted_by_the_scope_gate_as_a_registry_only_claim(self):
        repo = Repo(self)
        base = repo.doc()
        C.claim(repo.dir, spec(), write=True, now=NOW)
        head = repo.doc()
        track = json.loads((REPO / "DEVELOPMENT_TRACKER.json").read_text(encoding="utf-8"))
        self.assertEqual(I.registry_problems(base, head, track, track, "task/t-zzz-901-claim"), [])

    def test_a_refused_spec_writes_nothing_even_with_write(self):
        repo = Repo(self)
        before = repo.snapshot()
        for over in (dict(branch="claim/x"), dict(id="T-DEV-038"), dict(integration_notes=fake_credential())):
            probs, task = C.claim(repo.dir, spec(**over), write=True, now=NOW)
            self.assertTrue(probs)
            self.assertIsNone(task)
        self.assertEqual(repo.snapshot(), before)

    def test_a_registry_that_fails_governance_validation_after_the_claim_is_refused(self):
        repo = Repo(self)
        before = repo.snapshot()
        clash = json.loads((REPO / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))["package_owners"]
        some_path = next(iter(clash.values()))["paths"][0]
        busy = next(t for t in repo.doc()["tasks"] if t["status"] in tasks_mod.ACTIVE_STATUSES)
        probs, _ = C.claim(repo.dir, spec(owned_paths=list(busy["owned_paths"])), write=True, now=NOW)
        self.assertTrue(any(p.startswith("registry:") for p in probs), probs)
        self.assertEqual(repo.snapshot(), before)

    def test_it_runs_no_git_gh_or_network_and_starts_no_process(self):
        repo = Repo(self)
        boom = mock.Mock(side_effect=AssertionError("claim must not start a process"))
        with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(subprocess, "check_output", boom):
            probs, _ = C.claim(repo.dir, spec(), write=True, now=NOW)
        self.assertEqual(probs, [])
        boom.assert_not_called()

    def test_unreadable_inputs_are_a_refusal_not_a_crash(self):
        empty = Path(tempfile.mkdtemp(prefix="claim-empty-"))
        self.addCleanup(shutil.rmtree, empty, True)
        probs, task = C.claim(empty, spec())
        self.assertIsNone(task)
        self.assertIn("cannot read", probs[0])
        repo = Repo(self)
        (repo.dir / "ACTIVE_TASKS.json").write_text("{not json", encoding="utf-8")
        self.assertIn("cannot read", C.claim(repo.dir, spec())[0][0])
        (repo.dir / "ACTIVE_TASKS.json").write_text('{"tasks": 5}', encoding="utf-8")
        self.assertIn("unexpected shape", C.claim(repo.dir, spec())[0][0])


class Cli(unittest.TestCase):
    def run_cli(self, repo, spec_text, *flags):
        f = repo.dir / "spec.json"
        f.write_text(spec_text, encoding="utf-8")
        buf = io.StringIO()
        rc = C.main([str(f), "--repo", str(repo.dir), *flags], out=buf, now=NOW)
        return rc, buf.getvalue()

    def test_exit_codes_and_messages(self):
        repo = Repo(self)
        rc, text = self.run_cli(repo, json.dumps(spec(branch="claim/x")))
        self.assertEqual(rc, 1)
        self.assertIn("refused", text)
        self.assertIn("start with task/", text)
        rc, text = self.run_cli(repo, "{not json")
        self.assertEqual(rc, 2)
        rc, text = self.run_cli(repo, json.dumps(spec()), "--write")
        self.assertEqual(rc, 0)
        self.assertIn("claimed T-ZZZ-901", text)
        self.assertIn("one task per claim pull request", text)

    def test_run_from_the_orchestrator_directory_finds_the_repository_root(self):
        repo = Repo(self)
        (repo.dir / "orchestrator").mkdir()
        f = repo.dir / "spec.json"
        f.write_text(json.dumps(spec()), encoding="utf-8")
        buf = io.StringIO()
        rc = C.main([str(f), "--repo", str(repo.dir / "orchestrator")], out=buf, now=NOW)
        self.assertEqual(rc, 0, buf.getvalue())

    def test_output_never_echoes_a_credential_shaped_value(self):
        repo = Repo(self)
        rc, text = self.run_cli(repo, json.dumps(spec(integration_notes=fake_credential())))
        self.assertEqual(rc, 1)
        self.assertNotIn(fake_credential(), text)


if __name__ == "__main__":
    unittest.main()
