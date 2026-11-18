"""Tests for DEVELOPMENT_OPERATING_SYSTEM.md and the reconciled agent docs. Offline, reads tracked files only.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_docs.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "DEVELOPMENT_OPERATING_SYSTEM.md"
DOCS = ROOT / "AI_Creator_Studio_Documentation"
LABELS = ("IMPLEMENTED", "TESTED", "OBSERVED", "REPORTED", "NOT TESTED", "PROPOSED")

REQUIRED_SECTIONS = [
    "Principles", "Roles", "How Main controls the operation", "Proposing ideas and agreeing a large build",
    "Communication between agents", "The Python supervisor", "Context rotation, handoff and recovery",
    "GitHub as the durable log", "Claude Pro versus OmniRoute", "Orbit", "Windows and VS Code setup",
    "Onboarding a new repository", "Development agents versus product agents", "Capability status",
]


def text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def subcommands() -> set:
    parser = build_parser()
    for a in parser._actions:
        if a.__class__.__name__ == "_SubParsersAction":
            return set(a.choices)
    return set()


class DevOsDocTests(unittest.TestCase):
    def setUp(self):
        self.t = text(DOC)

    def test_required_sections_present(self):
        headings = [re.sub(r"^\d+\.\s*", "", h).strip() for h in re.findall(r"^## (.+)$", self.t, re.M)]
        for s in REQUIRED_SECTIONS:
            self.assertTrue(any(h.startswith(s) for h in headings), f"missing section: {s}")

    def test_key_statements_present(self):
        low = self.t.lower()
        for phrase in ("does not replenish account usage", "no paid api", "unavailable", "not tested",
                       "resolve_bridge", "config --example", "doctor", "essential clarifying questions"):
            self.assertIn(phrase, low, phrase)

    def _capability_rows(self):
        sec = self.t.split("## 14. Capability status", 1)[1].split("\n## ", 1)[0]
        rows = [l for l in sec.splitlines() if l.startswith("|")][2:]
        return rows

    def test_every_capability_row_has_a_status_label(self):
        rows = self._capability_rows()
        self.assertGreaterEqual(len(rows), 20)
        for r in rows:
            cells = [c.strip() for c in r.strip("|").split("|")]
            self.assertEqual(len(cells), 3, r)
            self.assertTrue(any(re.search(r"\b" + re.escape(l) + r"\b", cells[1]) for l in LABELS), r)

    def test_nothing_claimed_verified(self):
        for r in self._capability_rows():
            self.assertNotRegex(r, r"\bVERIFIED\b")

    def test_no_secret_patterns(self):
        for pat in (r"sk-[A-Za-z0-9]{16,}", r"ghp_[A-Za-z0-9]{20,}", r"github_pat_", r"AIza[0-9A-Za-z_-]{20,}",
                    r"gsk_[A-Za-z0-9]{20,}", r"Bearer\s+[A-Za-z0-9._-]{16,}", r"xox[bp]-", r"-----BEGIN"):
            for f in (DOC, DOCS / "AGENTS.md", DOCS / "WORKFLOW.md", DOCS / "ENVIRONMENT.md"):
                self.assertIsNone(re.search(pat, text(f)), f"{pat} in {f.name}")

    def test_no_absolute_user_paths_in_guide(self):
        for pat in (r"[A-Za-z]:\\Users\\", r"[A-Za-z]:/Users/", r"/home/\w", r"/Users/\w", r"%USERPROFILE%\\[A-Za-z]"):
            self.assertIsNone(re.search(pat, self.t), pat)

    def test_no_launcher_contents(self):
        for pat in (r"ANTHROPIC_(AUTH_TOKEN|API_KEY|BASE_URL)\s*=", r"\$env:ANTHROPIC", r"Set-Item\s+Env:",
                    r"\$env:[A-Z_]*(KEY|TOKEN)"):
            self.assertIsNone(re.search(pat, self.t), pat)

    def test_placeholders_used(self):
        for p in ("<REPO_ROOT>", "<STATE_DIR>", "<OMNIROUTE_URL>"):
            self.assertIn(p, self.t)

    def test_referenced_commands_exist(self):
        subs = subcommands()
        self.assertIn("doctor", subs)
        found = re.findall(r"python -B -m dev_supervisor(?!\.)\s+([a-z][a-z-]*)", self.t)
        self.assertTrue(found)
        for c in found:
            self.assertIn(c, subs, c)
        for row in re.findall(r"^\| `([a-z-]+)`(?: / `([a-z-]+)`)? \|", self.t.split("## 6.", 1)[1].split("## 7.", 1)[0], re.M):
            for c in row:
                if c:
                    self.assertIn(c, subs, c)

    def test_referenced_repo_paths_exist(self):
        for rel in ("orchestrator/dev_supervisor/OMNIROUTE_BENCHMARK.md", "orchestrator/dev_supervisor/RESOLVE_BOOTSTRAP.md",
                    "AI_Creator_Studio_Documentation/AGENTS.md"):
            self.assertIn(rel, self.t)
            self.assertTrue((ROOT / rel).exists(), rel)


class ReconciledDocsTests(unittest.TestCase):
    def test_agents_and_workflow_point_to_dev_os(self):
        for n in ("AGENTS.md", "WORKFLOW.md"):
            t = text(DOCS / n)
            self.assertIn("DEVELOPMENT_OPERATING_SYSTEM.md", t, n)
        self.assertIn("video-production agents only", text(DOCS / "AGENTS.md"))

    def test_environment_points_to_local_config_without_secrets(self):
        t = text(DOCS / "ENVIRONMENT.md")
        self.assertIn("config --example", t)
        self.assertIn("omniroute serve --daemon --no-open", t)
        self.assertIn("pro.ps1", t)  # the two launchers rule is kept
        self.assertEqual(len(re.findall(r"Exactly two launchers", t)), 1)


if __name__ == "__main__":
    unittest.main()
