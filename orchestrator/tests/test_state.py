"""Tests for creator_state. Run from orchestrator/:  python -m unittest discover -s tests -v"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creator_state import Project, StateError, UncertainOperationError, VerificationError, recovery, handoff, schema
from creator_state import util
from creator_state.cli import main as cli_main
from creator_state.redact import redact_text

FAKE_TOKEN = "sk-TESTONLY-SYNTHETIC-NOT-A-CREDENTIAL"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.projects = self.root / "Projects"
        self.media = self.root / "originals"
        self.media.mkdir()
        self.src = self.media / "clip.mov"
        self.src.write_bytes(b"ORIGINAL-MEDIA-BYTES" * 100)
        self.src_bytes = self.src.read_bytes()
        self.p = Project.create(self.projects, "Mushroom Vlog", source_media=[self.src])

    def write(self, rel, content="data"):
        f = self.p.dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        return rel

    def finish(self, stage, rel=None):
        """Start and properly complete a stage with a real output file."""
        self.p.start_stage(stage)
        rel = self.write(rel or f"analysis/{stage}.json", json.dumps({"stage": stage}))
        return self.p.complete_stage(stage, [rel], summary=f"{stage} done")


class TestCreation(Base):
    def test_structure_and_schema(self):
        s = self.p.load_state()
        self.assertEqual(s["schema_version"], schema.SCHEMA_VERSION)
        self.assertEqual(s["slug"], "mushroom-vlog")
        self.assertEqual(s["status"], "created")
        self.assertEqual(list(s["stages"]), list(schema.STAGE_IDS))
        for d in schema.PROJECT_DIRS:
            self.assertTrue((self.p.dir / d).is_dir(), d)
        self.assertTrue((self.p.dir / "HANDOFF.md").exists())
        self.assertGreaterEqual(len(s["constraints"]), 1)
        self.assertEqual(schema.validate_state(s), [])

    def test_initial_checkpoint_and_log(self):
        self.assertEqual(len(self.p.list_checkpoints()), 1)
        self.assertIn("project.created", (self.p.dir / "logs" / "activity.log").read_text(encoding="utf-8"))

    def test_refuses_to_overwrite_existing_project(self):
        with self.assertRaises(StateError):
            Project.create(self.projects, "Mushroom Vlog")

    def test_source_media_registered_by_reference_and_untouched(self):
        s = self.p.load_state()
        self.assertEqual(s["source_media"][0]["path"], str(self.src.resolve()))
        self.assertEqual(self.src.read_bytes(), self.src_bytes)
        self.assertFalse(any(self.p.dir.rglob("clip.mov")), "original must not be copied into the project")

    def test_missing_source_rejected(self):
        with self.assertRaises(StateError):
            self.p.add_source_media(self.media / "nope.mov")

    def test_secret_in_title_is_redacted(self):
        p = Project.create(self.projects, f"Test api_key={FAKE_TOKEN}")
        self.assertNotIn(FAKE_TOKEN, p.state_path.read_text(encoding="utf-8"))


class TestUpdates(Base):
    def test_decisions_tasks_errors_notes(self):
        d = self.p.add_decision("Open with the failed grow bag", "funniest real moment", stage="creative_plan")
        t = self.p.add_task("Pick 3 candidate shorts")
        e = self.p.add_error("whisper timed out")
        self.p.add_note("creator prefers informal tone")
        self.p.complete_task(t)
        self.p.resolve_error(e, "retried OK")
        s = self.p.load_state()
        self.assertEqual((d, t, e), ("D001", "T001", "E001"))
        self.assertEqual(s["tasks"][0]["status"], "done")
        self.assertTrue(s["errors"][0]["resolved"])
        self.assertGreater(s["revision"], 5)

    def test_unknown_ids_rejected(self):
        with self.assertRaises(StateError):
            self.p.complete_task("T999")
        with self.assertRaises(StateError):
            self.p.resolve_error("E999", "x")

    def test_secrets_never_persisted(self):
        self.p.add_note(f"set ANTHROPIC_AUTH_TOKEN={FAKE_TOKEN} and Bearer abcdefgh12345678")
        self.p.add_error(f"auth failed with {FAKE_TOKEN}")
        self.p.add_decision(f"use key {FAKE_TOKEN}")
        self.p.record_provider_switch("Claude Pro", "OmniRoute", f"token {FAKE_TOKEN}")
        for f in list(self.p.dir.rglob("*")):
            if f.is_file():
                self.assertNotIn(FAKE_TOKEN, f.read_text(encoding="utf-8", errors="ignore"), str(f))
        self.assertIn("[REDACTED]", self.p.state_path.read_text(encoding="utf-8"))

    def test_redactor_cases(self):
        self.assertNotIn("abcd1234efgh5678", redact_text('{"api_key": "abcd1234efgh5678"}'))
        self.assertNotIn("xyz98765432", redact_text("Authorization: Bearer xyz98765432"))
        self.assertEqual(redact_text("plain harmless text"), "plain harmless text")

    def test_revision_increments(self):
        r0 = self.p.load_state()["revision"]
        self.p.add_note("x")
        self.assertEqual(self.p.load_state()["revision"], r0 + 1)

    def test_failed_change_writes_nothing(self):
        before = self.p.state_path.read_text(encoding="utf-8")
        with self.assertRaises(StateError):
            self.p.complete_task("T404")
        self.assertEqual(self.p.state_path.read_text(encoding="utf-8"), before)


class TestStagesAndCheckpoints(Base):
    def test_complete_requires_verified_output(self):
        self.p.start_stage("ingest")
        with self.assertRaises(VerificationError):
            self.p.complete_stage("ingest", [])
        with self.assertRaises(VerificationError):
            self.p.complete_stage("ingest", ["analysis/missing.json"])
        (self.p.dir / "analysis").mkdir(exist_ok=True)
        (self.p.dir / "analysis" / "empty.txt").write_text("")
        with self.assertRaises(VerificationError):
            self.p.complete_stage("ingest", ["analysis/empty.txt"])
        self.write("analysis/bad.json", "{not json")
        with self.assertRaises(VerificationError):
            self.p.complete_stage("ingest", ["analysis/bad.json"])
        s = self.p.load_state()
        self.assertEqual(s["stages"]["ingest"]["status"], "in_progress", "must NOT be marked complete")
        self.assertTrue(any(e["kind"] == "verification_failed" for e in s["errors"]))

    def test_verification_error_auto_resolved_on_later_success(self):
        self.p.start_stage("ingest")
        with self.assertRaises(VerificationError):
            self.p.complete_stage("ingest", ["analysis/late.json"])
        self.assertFalse(self.p.load_state()["errors"][0]["resolved"])
        rel = self.write("analysis/late.json", "{}")
        self.p.complete_stage("ingest", [rel])
        self.assertTrue(self.p.load_state()["errors"][0]["resolved"])

    def test_output_outside_project_or_original_media_rejected(self):
        self.p.start_stage("ingest")
        with self.assertRaises(VerificationError):
            self.p.complete_stage("ingest", [str(self.src)])
        outside = self.root / "outside.json"
        outside.write_text("{}")
        with self.assertRaises(VerificationError):
            self.p.complete_stage("ingest", [str(outside)])
        self.assertEqual(self.src.read_bytes(), self.src_bytes)

    def test_directory_output(self):
        self.p.start_stage("ingest")
        (self.p.dir / "transcripts").mkdir(exist_ok=True)
        with self.assertRaises(VerificationError):  # empty dir
            self.p.complete_stage("ingest", ["transcripts"])
        self.write("transcripts/a.txt", "hello")
        res = self.p.complete_stage("ingest", ["transcripts"])
        self.assertEqual(res["outputs"][0]["kind"], "dir")

    def test_stage_order_enforced(self):
        with self.assertRaises(StateError):
            self.p.start_stage("research")
        with self.assertRaises(StateError):
            self.p.skip_stage("research", "later")
        with self.assertRaises(StateError):
            self.p.complete_stage("ingest", ["x"])  # not started

    def test_completed_stage_cannot_be_restarted_or_recompleted(self):
        self.finish("ingest")
        with self.assertRaises(StateError):
            self.p.start_stage("ingest")
        with self.assertRaises(StateError):
            self.p.complete_stage("ingest", ["analysis/ingest.json"])

    def test_checkpoints_saved_preserved_and_valid(self):
        self.finish("ingest")
        self.finish("understand")
        cps = self.p.list_checkpoints()
        self.assertEqual([c["reason"] for c in cps],
                         ["project created", "completed ingest", "completed understand"])
        self.assertTrue(all(c["valid"] for c in cps))
        first = (self.p.dir / "checkpoints" / cps[0]["file"]).read_bytes()
        self.finish("research")
        self.assertEqual((self.p.dir / "checkpoints" / cps[0]["file"]).read_bytes(), first,
                         "earlier checkpoints must never be rewritten")
        body = json.loads((self.p.dir / "checkpoints" / cps[1]["file"]).read_text(encoding="utf-8"))
        self.assertEqual(body["state"]["stages"]["ingest"]["status"], "completed")
        self.assertEqual(body["state"]["stages"]["understand"]["status"], "pending")
        self.assertIn("created_at", body)

    def test_tampered_checkpoint_detected(self):
        self.finish("ingest")
        f = self.p.dir / "checkpoints" / self.p.list_checkpoints()[1]["file"]
        f.write_text(f.read_text(encoding="utf-8").replace("ingest done", "FORGED"), encoding="utf-8")
        self.assertFalse(self.p.list_checkpoints()[1]["valid"])
        info, _ = self.p.load_checkpoint_state()
        self.assertEqual(info["seq"], 1, "tampered checkpoint must be skipped")

    def test_failed_stage_then_retry(self):
        self.p.start_stage("ingest")
        self.p.fail_stage("ingest", "ffprobe crashed")
        self.assertEqual(self.p.load_state()["status"], "failed")
        r = recovery.analyze(self.p)
        self.assertEqual(r["resume"]["action"], "retry_failed")
        res = self.p.start_stage("ingest")
        self.assertTrue(res["resumed"])
        self.assertEqual(res["attempts"], 2)

    def test_skip_requires_reason_and_unblocks(self):
        self.finish("ingest")
        with self.assertRaises(StateError):
            self.p.skip_stage("understand", " ")
        self.p.skip_stage("understand", "no speech in this clip")
        self.p.start_stage("research")

    def test_full_run_completes(self):
        for s in schema.STAGE_IDS:
            self.finish(s)
        s = self.p.load_state()
        self.assertEqual(s["status"], "completed")
        self.assertEqual(recovery.analyze(self.p)["resume"]["action"], "none")

    def test_reopen_invalidates_later_stages_keeps_history(self):
        self.finish("ingest"); self.finish("understand"); self.finish("research")
        reopened = self.p.reopen_stage("understand", "transcript was wrong")
        self.assertEqual(reopened, ["understand", "research"])
        s = self.p.load_state()
        self.assertEqual(s["stages"]["ingest"]["status"], "completed")
        self.assertEqual(s["stages"]["understand"]["status"], "pending")
        self.assertEqual(s["stages"]["understand"]["history"][-1]["event"], "reopened")
        self.assertTrue(s["stages"]["understand"]["history"][-1]["previous_outputs"])
        with self.assertRaises(StateError):
            self.p.reopen_stage("understand", "")


class TestHandoff(Base):
    def test_contains_everything_needed(self):
        self.p.add_decision("Keep the long pause after the joke", "comedic timing", stage="creative_plan")
        self.p.add_constraint("Max 60s per short")
        self.p.add_task("Choose music")
        self.finish("ingest")
        self.p.start_stage("understand")
        self.p.fail_stage("understand", "transcription engine unavailable")
        text = (self.p.dir / "HANDOFF.md").read_text(encoding="utf-8")
        for needle in ["Keep the long pause after the joke", "comedic timing", "Max 60s per short",
                       "Choose music", "analysis/ingest.json", "transcription engine unavailable",
                       str(self.src.resolve()), "Where to resume", "retry_failed",
                       "private reasoning", "originals"]:
            self.assertIn(needle, text, needle)

    def test_handoff_has_no_secrets(self):
        self.p.add_note(f"token={FAKE_TOKEN}")
        self.p.record_provider_switch("Claude Pro", "OmniRoute", f"limit hit; key {FAKE_TOKEN}")
        text = handoff.write(self.p, deep=True).read_text(encoding="utf-8")
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertIn("Claude Pro → OmniRoute", text)

    def test_handoff_refreshes_after_each_change(self):
        a = (self.p.dir / "HANDOFF.md").read_text(encoding="utf-8")
        self.p.add_decision("brand new decision")
        b = (self.p.dir / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertNotIn("brand new decision", a)
        self.assertIn("brand new decision", b)

    def test_handoff_warns_on_integrity_problem(self):
        self.finish("ingest")
        (self.p.dir / "analysis" / "ingest.json").unlink()
        text = handoff.write(self.p, deep=True).read_text(encoding="utf-8")
        self.assertIn("OUTPUT_MISSING", text)
        self.assertIn("reopen_and_redo", text)
        self.assertIn("BLOCKED", text)

    def test_handoff_from_checkpoint_when_state_corrupt(self):
        self.finish("ingest")
        self.p.state_path.write_text("{ truncated", encoding="utf-8")
        text = handoff.write(self.p).read_text(encoding="utf-8")
        self.assertIn("STATE UNUSABLE", text)
        self.assertIn("Mushroom Vlog", text)


class TestRecovery(Base):
    def test_fresh_project_resume(self):
        r = recovery.analyze(self.p)
        self.assertEqual((r["resume"]["stage"], r["resume"]["action"]), ("ingest", "start"))
        self.assertFalse(recovery.has_critical(r))

    def test_interrupted_stage_detected(self):
        self.finish("ingest")
        self.p.start_stage("understand")  # session dies here
        r = recovery.analyze(Project(self.p.dir))  # fresh process view
        self.assertEqual((r["resume"]["stage"], r["resume"]["action"]), ("understand", "resume_interrupted"))
        self.assertIn("INTERRUPTED_STAGE", [f["code"] for f in r["findings"]])
        self.assertFalse(r["resume"]["blocking"])

    def test_missing_output_after_completion(self):
        self.finish("ingest"); self.finish("understand")
        (self.p.dir / "analysis" / "ingest.json").unlink()
        r = recovery.analyze(self.p)
        self.assertEqual((r["resume"]["stage"], r["resume"]["action"]), ("ingest", "reopen_and_redo"))
        self.assertTrue(r["resume"]["blocking"])
        self.assertTrue(recovery.has_critical(r))

    def test_modified_output_detected_by_hash_and_size(self):
        self.finish("ingest")
        f = self.p.dir / "analysis" / "ingest.json"
        original = f.read_text(encoding="utf-8")
        f.write_text(original.replace("ingest", "ingesX"), encoding="utf-8")  # same size, new content
        deep = recovery.analyze(self.p, deep=True)
        self.assertIn("OUTPUT_CHANGED", [x["code"] for x in deep["findings"]])
        quick = recovery.analyze(self.p, deep=False)
        self.assertNotIn("OUTPUT_CHANGED", [x["code"] for x in quick["findings"]])  # documented limit of quick
        f.write_text(original + " ", encoding="utf-8")
        self.assertIn("OUTPUT_CHANGED", [x["code"] for x in recovery.analyze(self.p, deep=False)["findings"]])

    def test_hand_edited_state_inconsistencies(self):
        self.finish("ingest")
        s = json.loads(self.p.state_path.read_text(encoding="utf-8"))
        s["stages"]["research"]["status"] = "completed"       # skipped over 'understand'
        s["stages"]["ingest"]["outputs"] = []                  # completed with no output
        self.p.state_path.write_text(json.dumps(s), encoding="utf-8")
        codes = [f["code"] for f in recovery.analyze(self.p)["findings"]]
        self.assertIn("ORDER_VIOLATION", codes)
        self.assertIn("COMPLETED_WITHOUT_OUTPUT", codes)

    def test_multiple_active_stages_flagged(self):
        s = json.loads(self.p.state_path.read_text(encoding="utf-8"))
        s["stages"]["ingest"]["status"] = "in_progress"
        s["stages"]["understand"]["status"] = "in_progress"
        self.p.state_path.write_text(json.dumps(s), encoding="utf-8")
        self.assertIn("MULTIPLE_ACTIVE_STAGES", [f["code"] for f in recovery.analyze(self.p)["findings"]])

    def test_invalid_state_structure(self):
        self.p.state_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        r = recovery.analyze(self.p)
        self.assertFalse(r["state_ok"])
        self.assertEqual(r["resume"]["action"], "recover_state_first")

    def test_future_schema_refused(self):
        s = json.loads(self.p.state_path.read_text(encoding="utf-8"))
        s["schema_version"] = 999
        self.p.state_path.write_text(json.dumps(s), encoding="utf-8")
        with self.assertRaises(StateError) as cm:
            self.p.load_state()
        self.assertIn("newer", str(cm.exception))

    def test_corrupt_state_recovers_from_checkpoint_and_keeps_damaged_copy(self):
        self.finish("ingest")
        self.p.state_path.write_text('{"half written', encoding="utf-8")
        r = recovery.analyze(self.p)
        self.assertEqual(r["resume"]["action"], "recover_state_first")
        self.assertIn("CHECKPOINT_AVAILABLE", [f["code"] for f in r["findings"]])
        with self.assertRaises(StateError):
            self.p.add_note("must not write on top of corrupt state")
        info = self.p.restore_from_checkpoint()
        self.assertEqual(info["reason"], "completed ingest")
        s = self.p.load_state()
        self.assertEqual(s["stages"]["ingest"]["status"], "completed")
        damaged = list((self.p.dir / "backups").glob("project_state.damaged_*.json"))
        self.assertEqual(len(damaged), 1)
        self.assertEqual(damaged[0].read_text(encoding="utf-8"), '{"half written')

    def test_state_behind_checkpoint_detected(self):
        self.finish("ingest")
        old = json.loads(self.p.state_path.read_text(encoding="utf-8"))
        old["revision"] = 1
        self.p.state_path.write_text(json.dumps(old), encoding="utf-8")  # stale file restored from backup
        self.assertIn("STATE_BEHIND_CHECKPOINT", [f["code"] for f in recovery.analyze(self.p)["findings"]])

    def test_no_checkpoint_and_corrupt_state_needs_manual_investigation(self):
        for f in (self.p.dir / "checkpoints").glob("*.json"):
            f.unlink()
        self.p.state_path.write_text("garbage", encoding="utf-8")
        self.assertEqual(recovery.analyze(self.p)["resume"]["action"], "manual_investigation")

    def test_source_media_missing_or_changed(self):
        self.src.write_bytes(b"x")  # simulate external change
        self.assertIn("SOURCE_MEDIA_CHANGED", [f["code"] for f in recovery.analyze(self.p)["findings"]])
        self.src.unlink()
        self.assertIn("SOURCE_MEDIA_MISSING", [f["code"] for f in recovery.analyze(self.p)["findings"]])

    def test_analysis_is_read_only(self):
        self.finish("ingest")
        snapshot = {str(f): f.read_bytes() for f in self.p.dir.rglob("*") if f.is_file()
                    and f.name not in ("HANDOFF.md",)}
        recovery.analyze(self.p, deep=True)
        after = {str(f): f.read_bytes() for f in self.p.dir.rglob("*") if f.is_file()
                 and f.name not in ("HANDOFF.md",)}
        self.assertEqual(snapshot, after)


class TestDestructiveOperations(Base):
    def test_completed_destructive_op_is_not_repeated(self):
        self.p.start_stage("ingest")
        self.assertEqual(self.p.begin_operation("ingest", "purge-cache", "delete render cache", destructive=True), "started")
        self.p.complete_operation("ingest", "purge-cache", "cache removed")
        self.assertEqual(self.p.begin_operation("ingest", "purge-cache", "delete render cache", destructive=True),
                         "already_done")
        self.assertEqual(self.p.load_state()["stages"]["ingest"]["operations"]["purge-cache"]["attempts"], 1)

    def test_interrupted_destructive_op_blocks_until_resolved(self):
        self.p.start_stage("ingest")
        self.p.begin_operation("ingest", "overwrite-timeline", "replace timeline", destructive=True)
        # --- session dies; new session ---
        p2 = Project(self.p.dir)
        r = recovery.analyze(p2)
        self.assertEqual(r["resume"]["action"], "verify_before_continuing")
        self.assertTrue(r["resume"]["blocking"])
        self.assertIn("UNCERTAIN_DESTRUCTIVE_OPERATION", [f["code"] for f in r["findings"]])
        with self.assertRaises(UncertainOperationError):
            p2.start_stage("ingest")
        with self.assertRaises(UncertainOperationError):
            p2.begin_operation("ingest", "overwrite-timeline", "replace timeline", destructive=True)
        with self.assertRaises(StateError):
            p2.complete_stage("ingest", ["x"])
        with self.assertRaises(StateError):
            p2.resolve_operation("ingest", "overwrite-timeline", applied=True, note="  ")
        p2.resolve_operation("ingest", "overwrite-timeline", applied=True, note="checked Resolve: timeline already replaced")
        self.assertEqual(p2.begin_operation("ingest", "overwrite-timeline", "x", destructive=True), "already_done")
        self.assertFalse(recovery.analyze(p2)["resume"]["blocking"])

    def test_resolved_not_applied_can_be_retried(self):
        self.p.start_stage("ingest")
        self.p.begin_operation("ingest", "op", "d", destructive=True)
        self.p.resolve_operation("ingest", "op", applied=False, note="checked: nothing changed")
        self.assertEqual(self.p.begin_operation("ingest", "op", "d", destructive=True), "started")

    def test_non_destructive_interrupted_op_is_retryable(self):
        self.p.start_stage("ingest")
        self.p.begin_operation("ingest", "probe", "ffprobe all files")
        r = recovery.analyze(self.p)
        self.assertIn("UNFINISHED_OPERATION", [f["code"] for f in r["findings"]])
        self.assertEqual(self.p.begin_operation("ingest", "probe", "ffprobe all files"), "started")
        self.assertEqual(self.p.load_state()["stages"]["ingest"]["operations"]["probe"]["attempts"], 2)

    def test_stage_cannot_complete_with_open_operation(self):
        self.p.start_stage("ingest")
        self.p.begin_operation("ingest", "probe", "ffprobe")
        rel = self.write("analysis/x.json", "{}")
        with self.assertRaises(StateError):
            self.p.complete_stage("ingest", [rel])

    def test_resume_message_lists_completed_ops(self):
        self.p.start_stage("ingest")
        self.p.begin_operation("ingest", "step-a", "first", destructive=True)
        self.p.complete_operation("ingest", "step-a")
        r = recovery.analyze(self.p)
        self.assertIn("step-a", r["resume"]["reason"])

    def test_reopen_keeps_destructive_journal(self):
        self.p.start_stage("ingest")
        self.p.begin_operation("ingest", "once", "d", destructive=True)
        self.p.complete_operation("ingest", "once")
        rel = self.write("analysis/i.json", "{}")
        self.p.complete_stage("ingest", [rel])
        self.p.reopen_stage("ingest", "redo analysis")
        self.p.start_stage("ingest")
        self.assertEqual(self.p.begin_operation("ingest", "once", "d", destructive=True), "already_done")


class TestAtomicityAndLocking(Base):
    def test_failed_replace_leaves_original_state_intact(self):
        before = self.p.state_path.read_bytes()
        with mock.patch("creator_state.util.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.p.add_note("this write fails")
        self.assertEqual(self.p.state_path.read_bytes(), before)
        self.assertEqual(self.p.load_state()["notes"], [])
        self.assertFalse(list(self.p.dir.glob(".*.tmp")), "temp file must be cleaned up")
        self.assertFalse((self.p.dir / ".state.lock").exists(), "lock must be released after failure")

    def test_stale_lock_taken_over(self):
        lock = self.p.dir / ".state.lock"
        lock.write_text("pid=1 at=long-ago")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        self.p.add_note("works despite stale lock")
        self.assertEqual(len(self.p.load_state()["notes"]), 1)
        self.assertIn("lock.stale", (self.p.dir / "logs" / "activity.log").read_text(encoding="utf-8"))

    def test_live_lock_times_out(self):
        lock = self.p.dir / ".state.lock"
        lock.write_text("pid=1 at=now")
        with mock.patch.object(Project, "LOCK_TIMEOUT", 0.2):
            with self.assertRaises(util.LockTimeout):
                self.p.add_note("blocked")
        lock.unlink()

    def test_atomic_write_text_roundtrip(self):
        f = self.root / "a.txt"
        util.atomic_write_text(f, "one")
        util.atomic_write_text(f, "two")
        self.assertEqual(f.read_text(), "two")


class TestLogs(Base):
    def test_human_and_machine_logs(self):
        self.finish("ingest")
        human = (self.p.dir / "logs" / "activity.log").read_text(encoding="utf-8")
        self.assertIn("stage.completed", human)
        self.assertIn("[ingest]", human)
        lines = [json.loads(l) for l in (self.p.dir / "logs" / "activity.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all({"ts", "level", "kind", "message"} <= set(l) for l in lines))

    def test_errors_logged_at_error_level(self):
        self.p.start_stage("ingest")
        self.p.fail_stage("ingest", f"crash with token {FAKE_TOKEN}")
        text = (self.p.dir / "logs" / "activity.log").read_text(encoding="utf-8")
        self.assertIn("ERROR", text)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(FAKE_TOKEN, (self.p.dir / "logs" / "activity.jsonl").read_text(encoding="utf-8"))


class TestProviderSwitchHandoff(Base):
    """End-to-end scenario: Claude Pro hits its limit mid-stage, OmniRoute session takes over."""

    def test_switch_scenario(self):
        self.p.add_decision("Long-form first, shorts derived after", "per WORKFLOW.md", made_by="user")
        self.finish("ingest")
        self.p.start_stage("understand")
        self.p.begin_operation("understand", "transcribe", "transcribe clip.mov")   # limit hit here
        self.p.record_provider_switch("Claude Pro", "OmniRoute (auto/coding)", "Pro usage limit reached")

        # --- brand new process / provider: knows nothing except the files ---
        new = Project(self.p.dir)
        text = (new.dir / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn("Long-form first, shorts derived after", text)
        self.assertIn("Claude Pro → OmniRoute (auto/coding)", text)
        rep = recovery.analyze(new)
        self.assertEqual((rep["resume"]["stage"], rep["resume"]["action"]), ("understand", "resume_interrupted"))
        self.assertEqual(new.begin_operation("understand", "transcribe", "transcribe clip.mov"), "started")
        new.complete_operation("understand", "transcribe", "ok")
        rel = self.write("transcripts/clip.json", '{"segments": []}')
        new.complete_stage("understand", [rel])
        self.assertEqual(recovery.analyze(new)["resume"]["stage"], "research")
        self.assertEqual(self.src.read_bytes(), self.src_bytes)


class TestCLI(Base):
    def run_cli(self, *argv):
        buf = io.StringIO()
        code = cli_main(["--root", str(self.root), *argv], out=buf)
        return code, buf.getvalue()

    def test_create_status_flow(self):
        code, out = self.run_cli("create", "Test Video Two", "--source", str(self.src))
        self.assertEqual(code, 0)
        code, out = self.run_cli("status", "Test Video Two")
        self.assertEqual(code, 0)
        self.assertIn("action=start", out)
        self.assertIn("[ ] ingest", out)
        code, out = self.run_cli("list")
        self.assertIn("test-video-two", out)

    def test_stage_commands_and_verification_failure_exit(self):
        self.run_cli("start-stage", "mushroom-vlog", "ingest")
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            code, _ = self.run_cli("complete-stage", "mushroom-vlog", "ingest", "--output", "analysis/none.json")
        self.assertEqual(code, 2)
        self.assertIn("does not exist", buf.getvalue())
        self.write("analysis/ok.json", "{}")
        code, out = self.run_cli("complete-stage", "mushroom-vlog", "ingest", "--output", "analysis/ok.json")
        self.assertEqual(code, 0)
        self.assertIn("verified", out)

    def test_check_exit_code_on_critical(self):
        self.assertEqual(self.run_cli("check", "mushroom-vlog")[0], 0)
        self.run_cli("start-stage", "mushroom-vlog", "ingest")
        self.run_cli("begin-op", "mushroom-vlog", "ingest", "wipe", "wipe thing", "--destructive")
        code, out = self.run_cli("check", "mushroom-vlog")
        self.assertEqual(code, 1)
        self.assertIn("UNCERTAIN_DESTRUCTIVE_OPERATION", out)
        code, out = self.run_cli("resume", "mushroom-vlog")
        self.assertEqual(code, 1)
        self.assertIn("verify_before_continuing", out)

    def test_recover_requires_confirmation(self):
        self.p.state_path.write_text("junk", encoding="utf-8")
        code, out = self.run_cli("recover", "mushroom-vlog")
        self.assertIn("Re-run with --yes", out)
        self.assertEqual(self.p.state_path.read_text(encoding="utf-8"), "junk")
        code, out = self.run_cli("recover", "mushroom-vlog", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.p.load_state()["slug"], "mushroom-vlog")

    def test_switch_provider_and_handoff_command(self):
        self.run_cli("switch-provider", "mushroom-vlog", "Claude Pro", "OmniRoute", "--reason", "limit")
        code, out = self.run_cli("handoff", "mushroom-vlog")
        self.assertTrue(out.strip().endswith("HANDOFF.md"))
        self.assertIn("Claude Pro → OmniRoute", Path(out.strip()).read_text(encoding="utf-8"))

    def test_status_json(self):
        code, out = self.run_cli("status", "mushroom-vlog", "--json")
        data = json.loads(out)
        self.assertTrue(data["state_ok"])


if __name__ == "__main__":
    unittest.main()
