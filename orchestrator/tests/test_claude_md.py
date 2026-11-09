"""Guards against CLAUDE.md drifting from reality: files, CLI commands, flags, stages, codes."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creator_state import handoff, ingest, schema
from creator_state.project import Project
from creator_state.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def _subparsers():
    parser = build_parser()
    action = next(a for a in parser._actions if a.choices and hasattr(a.choices, "items")
                  and "create" in a.choices)
    return action.choices


class TestClaudeMd(unittest.TestCase):
    def test_exists_and_is_concise(self):
        self.assertLess(len(TEXT.splitlines()), 90, "CLAUDE.md loads every session; keep it short")

    def test_referenced_files_exist(self):
        doc_dir = ROOT / "AI_Creator_Studio_Documentation"
        checked = 0
        for tok in re.findall(r"`([^`\s]+)`", TEXT):
            if "<" in tok or "*" in tok or tok.startswith("--"):
                continue
            per_project = {"project_state.json": Project.STATE_FILE, "HANDOFF.md": handoff.HANDOFF_FILE,
                           "source_manifest.json": ingest.MANIFEST_FILE,
                           "analysis/ingest_report.md": ingest.REPORT_FILE}
            if tok in per_project:  # lives inside each project folder: check against the code instead
                self.assertEqual(tok, per_project[tok])
                continue
            if tok.endswith(".md") and "/" not in tok:
                path = doc_dir / tok if (doc_dir / tok).exists() else ROOT / tok
            elif tok.endswith((".md", ".ps1", ".json")) or tok in ("orchestrator/", "tools/"):
                path = ROOT / tok
            else:
                continue
            checked += 1
            self.assertTrue(path.exists(), f"CLAUDE.md references missing file: {tok}")
        self.assertGreater(checked, 10)

    def test_documentation_reading_order_starts_with_master(self):
        self.assertLess(TEXT.index("MASTER.md"), TEXT.index("WORKFLOW.md"))
        self.assertLess(TEXT.index("MASTER.md"), TEXT.index("DECISIONS_AND_OPEN_QUESTIONS.md"))

    def test_cli_commands_exist(self):
        subs = _subparsers()
        cmds = set(re.findall(r"creator_state ([a-z]+(?:-[a-z]+)*)", TEXT))
        cmds |= set(re.findall(r"`([a-z]+(?:-[a-z]+)*) <slug>", TEXT))
        cmds |= set(re.findall(r"`(constraint|task|task-done|error|error-resolved|note)`", TEXT))
        cmds.discard("")
        self.assertGreater(len(cmds), 12)
        for c in cmds:
            self.assertIn(c, subs, f"CLAUDE.md mentions CLI command that does not exist: {c}")
        for required in ("list", "check", "resume", "log", "start-stage", "complete-stage", "handoff",
                         "recover", "resolve-op", "begin-op", "finish-op", "decide", "reopen-stage",
                         "switch-provider", "checkpoints", "create", "incoming-list", "incoming-inspect",
                         "ingest", "inventory", "ingest-status", "verify-sources", "proxy-make",
                         "managed-copy"):
            self.assertIn(required, TEXT)

    def test_cli_flags_exist(self):
        all_flags = set()
        for sp in _subparsers().values():
            for a in sp._actions:
                all_flags.update(a.option_strings)
        for flag in set(re.findall(r"(?<![\w-])(--[a-z]+(?:-[a-z]+)*)", TEXT)):
            self.assertIn(flag, all_flags, f"CLAUDE.md mentions unknown flag {flag}")

    def test_stage_list_matches_schema(self):
        m = re.search(r"Stages, in order: `([^`]+)`", TEXT)
        self.assertIsNotNone(m)
        self.assertEqual([s.strip() for s in m.group(1).split(",")], list(schema.STAGE_IDS))

    def test_finding_codes_exist_in_recovery(self):
        src = (ROOT / "orchestrator" / "creator_state" / "recovery.py").read_text(encoding="utf-8")
        for code in re.findall(r"\b(INTERRUPTED_STAGE|UNFINISHED_OPERATION|UNCERTAIN_DESTRUCTIVE_OPERATION|"
                               r"OUTPUT_MISSING|OUTPUT_CHANGED|STATE_UNUSABLE|STATE_BEHIND_CHECKPOINT|"
                               r"SOURCE_MEDIA_)", TEXT):
            self.assertIn(code, src)
        for action in ("verify_before_continuing", "reopen_and_redo", "recover_state_first",
                       "manual_investigation"):
            self.assertIn(action, TEXT)
            self.assertIn(action, src)

    def test_safety_rules_present(self):
        for phrase in ("Never repeat an interrupted", "Nothing resumes automatically", "pro.ps1",
                       "backup.ps1", "Originals are immutable", "Don't invent missing project decisions",
                       "VERIFIED", "DECIDED", "PROPOSED", "OPEN", "NOT TESTED"):
            self.assertIn(phrase, TEXT)

    def test_no_secrets_in_claude_md(self):
        self.assertNotRegex(TEXT, r"sk-[A-Za-z0-9_-]{8,}")


if __name__ == "__main__":
    unittest.main()
