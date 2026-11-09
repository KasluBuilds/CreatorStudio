"""Project: all reads and writes of one video project's persistent state.

Every mutation: lock -> reload fresh state -> apply change -> bump revision ->
atomic save -> (optionally) append-only checkpoint -> refresh HANDOFF.md.
If the change function raises, nothing is written.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from . import schema, util
from .logbook import ActivityLog
from .redact import redact_obj, redact_text
from .schema import (COMPLETED, DONE_STATUSES, FAILED, IN_PROGRESS, OP_COMPLETED,
                     OP_FAILED, OP_NOT_APPLIED, OP_STARTED, PENDING, SKIPPED)


class StateError(Exception):
    """Invalid request or unreadable/inconsistent state."""


class VerificationError(StateError):
    """A stage's outputs could not be verified, so it was not marked complete."""


class UncertainOperationError(StateError):
    """A destructive operation was started but never confirmed; a human/AI must check."""


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "project"


def fingerprint(path: Path) -> dict:
    """Describe an output file or directory: kind, size, sha256 (tree hash for dirs)."""
    if path.is_file():
        return {"kind": "file", "size": path.stat().st_size, "sha256": util.sha256_file(path)}
    files = sorted(p for p in path.rglob("*") if p.is_file())
    lines = [f"{p.relative_to(path).as_posix()}:{util.sha256_file(p)}" for p in files]
    return {"kind": "dir", "size": sum(p.stat().st_size for p in files),
            "files": len(files), "sha256": util.sha256_text("\n".join(lines))}


class Project:
    STATE_FILE = "project_state.json"
    LOCK_FILE = ".state.lock"
    LOCK_TIMEOUT = 10.0

    def __init__(self, path):
        self.dir = Path(path).resolve()
        self.state_path = self.dir / self.STATE_FILE
        self.log = ActivityLog(self.dir)

    # ------------------------------------------------------------------ creation
    @classmethod
    def create(cls, projects_root, title: str, slug: str | None = None,
               source_media=(), hash_sources: bool = False) -> "Project":
        slug = slugify(slug or title)
        pdir = Path(projects_root) / slug
        if (pdir / cls.STATE_FILE).exists():
            raise StateError(f"project '{slug}' already exists at {pdir}; refusing to overwrite")
        pdir.mkdir(parents=True, exist_ok=True)
        for d in schema.PROJECT_DIRS:
            (pdir / d).mkdir(exist_ok=True)
        now = util.utcnow()
        state = schema.new_state(str(uuid.uuid4()), slug, redact_text(title), now)
        proj = cls(pdir)
        util.atomic_write_text(proj.state_path, util.pretty_json(redact_obj(state)))
        proj.log.event("project.created", f"Project '{title}' created (id {state['project_id']}).")
        proj._write_checkpoint(state, "project created")
        for src in source_media:
            proj.add_source_media(src, hash_file=hash_sources)
        proj._refresh_handoff()
        return proj

    # ------------------------------------------------------------------ load/save
    def load_state(self) -> dict:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise StateError(f"no project state at {self.state_path}")
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as e:
            raise StateError(f"state file is corrupt (invalid JSON: {e}); see `recover`")
        try:
            state = schema.migrate(state)
        except ValueError as e:
            raise StateError(str(e))
        problems = schema.validate_state(state)
        if problems:
            raise StateError("state file is structurally invalid: " + "; ".join(problems))
        return state

    def _mutate(self, fn, checkpoint: str | None = None):
        lock = util.FileLock(self.dir / self.LOCK_FILE, timeout=self.LOCK_TIMEOUT)
        with lock:
            if lock.took_over_stale:
                self.log.event("lock.stale", "Removed a stale lock left by a crashed process.", "WARN")
            state = self.load_state()
            result = fn(state)
            state["revision"] += 1
            state["updated_at"] = util.utcnow()
            state["status"] = schema.derive_status(state)
            state = redact_obj(state)
            util.atomic_write_text(self.state_path, util.pretty_json(state))
            if checkpoint:
                self._write_checkpoint(state, checkpoint)
        self._refresh_handoff()
        return result

    # ------------------------------------------------------------------ checkpoints
    def _checkpoint_files(self):
        d = self.dir / "checkpoints"
        return sorted(d.glob("[0-9][0-9][0-9][0-9]_*.json")) if d.exists() else []

    def _write_checkpoint(self, state: dict, reason: str) -> Path:
        """Append-only: exclusive create, never overwrites an earlier checkpoint."""
        (self.dir / "checkpoints").mkdir(exist_ok=True)
        slug = slugify(reason)[:40]
        for _ in range(50):
            files = self._checkpoint_files()
            seq = int(files[-1].name[:4]) + 1 if files else 1
            path = self.dir / "checkpoints" / f"{seq:04d}_{slug}_{util.compact_ts(util.utcnow())}.json"
            body = {
                "checkpoint_version": 1, "seq": seq, "created_at": util.utcnow(),
                "reason": reason, "state_revision": state["revision"],
                "state_sha256": util.sha256_text(util.canonical_json(state)),
                "state": state,
            }
            try:
                util.write_new_text(path, util.pretty_json(body))
            except FileExistsError:
                continue
            self.log.event("checkpoint.saved", f"Checkpoint {seq:04d} saved: {reason}.")
            return path
        raise StateError("could not allocate a checkpoint number")

    def list_checkpoints(self) -> list:
        """Each checkpoint with an integrity verdict (state hash matches its content)."""
        out = []
        for p in self._checkpoint_files():
            info = {"file": p.name, "valid": False}
            try:
                body = json.loads(p.read_text(encoding="utf-8"))
                ok = body["state_sha256"] == util.sha256_text(util.canonical_json(body["state"]))
                info.update(seq=body["seq"], created_at=body["created_at"], reason=body["reason"],
                            state_revision=body["state_revision"], valid=ok)
                if not ok:
                    info["problem"] = "content does not match recorded hash"
            except (OSError, ValueError, KeyError) as e:
                info["problem"] = f"unreadable: {e}"
            out.append(info)
        return out

    def load_checkpoint_state(self, seq: int | None = None):
        """Return (info, state) of the newest valid checkpoint (or the given seq)."""
        for p in reversed(self._checkpoint_files()):
            try:
                body = json.loads(p.read_text(encoding="utf-8"))
                if seq is not None and body["seq"] != seq:
                    continue
                if body["state_sha256"] != util.sha256_text(util.canonical_json(body["state"])):
                    continue
                return {"seq": body["seq"], "file": p.name, "reason": body["reason"],
                        "created_at": body["created_at"],
                        "state_revision": body["state_revision"]}, body["state"]
            except (OSError, ValueError, KeyError):
                continue
        return None, None

    def restore_from_checkpoint(self, seq: int | None = None) -> dict:
        """Replace a damaged state file with a checkpoint. The damaged file is preserved."""
        info, state = self.load_checkpoint_state(seq)
        if state is None:
            raise StateError("no valid checkpoint available to restore from")
        with util.FileLock(self.dir / self.LOCK_FILE):
            if self.state_path.exists():
                backup = self.dir / "backups" / f"project_state.damaged_{util.compact_ts(util.utcnow())}.json"
                n = 0
                while backup.exists():
                    n += 1
                    backup = backup.with_name(f"{backup.stem.split('__')[0]}__{n}.json")
                backup.write_bytes(self.state_path.read_bytes())
                self.log.event("recovery.backup", f"Damaged state preserved as backups/{backup.name}.", "WARN")
            state = schema.migrate(state)
            state["revision"] += 1
            state["updated_at"] = util.utcnow()
            util.atomic_write_text(self.state_path, util.pretty_json(redact_obj(state)))
        self.log.event("recovery.restored",
                       f"State restored from checkpoint {info['seq']:04d} ({info['reason']}). "
                       "Work done after that checkpoint must be re-verified.", "WARN")
        self._refresh_handoff()
        return info

    # ------------------------------------------------------------------ handoff
    def _refresh_handoff(self):
        try:
            from . import handoff
            handoff.write(self, deep=False)
        except Exception as e:  # never let a handoff problem break a state update
            self.log.event("handoff.failed", f"Could not refresh HANDOFF.md: {e}", "ERROR")

    # ------------------------------------------------------------------ source media
    def add_source_media(self, path, hash_file: bool = False) -> dict:
        """Register original media BY REFERENCE. The file is only read (stat / optional hash)."""
        p = Path(path)
        if not p.is_file():
            raise StateError(f"source media not found: {p}")
        st = p.stat()
        rec = {"path": str(p.resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
               "sha256": util.sha256_file(p) if hash_file else None, "registered_at": util.utcnow()}

        def fn(state):
            if any(m["path"] == rec["path"] for m in state["source_media"]):
                raise StateError(f"source media already registered: {rec['path']}")
            state["source_media"].append(rec)
        self._mutate(fn)
        self.log.event("source.registered", f"Registered source media (read-only reference): {rec['path']}")
        return rec

    def register_sources(self, records) -> dict:
        """Register many originals BY REFERENCE in one atomic update (no hashing, no file access).

        `records`: dicts with path, size, mtime_ns and optional sha256. Paths already
        registered are left exactly as first recorded, so a later change to an
        original still shows up as SOURCE_MEDIA_CHANGED instead of being papered over.
        """
        def fn(state):
            known = {m["path"] for m in state["source_media"]}
            added = skipped = 0
            for r in records:
                if r["path"] in known:
                    skipped += 1
                    continue
                state["source_media"].append({
                    "path": r["path"], "size": r["size"], "mtime_ns": r.get("mtime_ns"),
                    "sha256": r.get("sha256"), "registered_at": util.utcnow()})
                known.add(r["path"])
                added += 1
            return {"added": added, "already_registered": skipped}
        res = self._mutate(fn)
        self.log.event("source.registered_batch",
                       f"Registered {res['added']} original(s) by reference "
                       f"({res['already_registered']} already registered).")
        return res

    # ------------------------------------------------------------------ stages
    @staticmethod
    def _stage(state, stage_id):
        if stage_id not in state["stages"]:
            raise StateError(f"unknown stage '{stage_id}'. Known: {', '.join(state['stage_order'])}")
        return state["stages"][stage_id]

    @staticmethod
    def _open_ops(stage: dict) -> list:
        return [k for k, op in stage["operations"].items() if op["status"] == OP_STARTED]

    def start_stage(self, stage_id: str) -> dict:
        """Begin (or resume/retry) a stage. Completed stages are never silently redone."""
        def fn(state):
            st = self._stage(state, stage_id)
            if st["status"] in DONE_STATUSES:
                raise StateError(f"stage '{stage_id}' is already {st['status']}; use reopen_stage "
                                 "with a reason if it genuinely must be redone")
            idx = state["stage_order"].index(stage_id)
            for prev in state["stage_order"][:idx]:
                if state["stages"][prev]["status"] not in DONE_STATUSES:
                    raise StateError(f"cannot start '{stage_id}': earlier stage '{prev}' is "
                                     f"{state['stages'][prev]['status']}")
            for op_id in self._open_ops(st):
                if st["operations"][op_id]["destructive"]:
                    raise UncertainOperationError(
                        f"destructive operation '{op_id}' in '{stage_id}' was started but never "
                        "confirmed. Inspect the real result, then call resolve_operation.")
            resumed = st["status"] in (IN_PROGRESS, FAILED)
            st["history"].append({"at": util.utcnow(), "event": "resumed" if resumed else "started",
                                  "from_status": st["status"]})
            st["status"] = IN_PROGRESS
            st["attempts"] += 1
            st["started_at"] = st["started_at"] or util.utcnow()
            state["current_stage"] = stage_id
            return resumed, st["attempts"]
        resumed, attempts = self._mutate(fn)
        self.log.event("stage.resumed" if resumed else "stage.started",
                       f"Stage started (attempt {attempts}).", stage=stage_id)
        return {"stage": stage_id, "resumed": resumed, "attempts": attempts}

    def _verify_outputs(self, state, outputs) -> list:
        if not outputs:
            raise VerificationError("a stage cannot be completed without at least one output to verify")
        sources = {m["path"] for m in state["source_media"]}
        records, problems = [], []
        for raw in outputs:
            p = Path(raw)
            p = (p if p.is_absolute() else self.dir / p).resolve()
            try:
                rel = p.relative_to(self.dir).as_posix()
            except ValueError:
                problems.append(f"{raw}: output must be inside the project folder")
                continue
            if str(p) in sources:
                problems.append(f"{raw}: original source media cannot be a stage output")
                continue
            if not p.exists():
                problems.append(f"{rel}: does not exist")
                continue
            if p.is_file() and p.stat().st_size == 0:
                problems.append(f"{rel}: file is empty")
                continue
            if p.is_dir() and not any(x.is_file() for x in p.rglob("*")):
                problems.append(f"{rel}: directory contains no files")
                continue
            if p.is_file() and p.suffix.lower() == ".json":
                try:
                    json.loads(p.read_text(encoding="utf-8"))
                except (ValueError, OSError) as e:
                    problems.append(f"{rel}: invalid JSON ({e})")
                    continue
            rec = {"path": rel, "verified_at": util.utcnow(), **fingerprint(p)}
            if p.is_file():
                rec["mtime_ns"] = p.stat().st_mtime_ns
            records.append(rec)
        if problems:
            raise VerificationError("; ".join(problems))
        return records

    def complete_stage(self, stage_id: str, outputs, summary: str = "", verification_note: str = "") -> dict:
        """Mark a stage complete ONLY if every output exists, is non-empty/valid and hashed."""
        def fn(state):
            st = self._stage(state, stage_id)
            if st["status"] == COMPLETED:
                raise StateError(f"stage '{stage_id}' is already completed")
            if st["status"] != IN_PROGRESS:
                raise StateError(f"stage '{stage_id}' is {st['status']}; call start_stage first")
            open_ops = self._open_ops(st)
            if open_ops:
                raise StateError(f"stage '{stage_id}' has unfinished operations: {', '.join(open_ops)}")
            records = self._verify_outputs(state, outputs)
            now = util.utcnow()
            st.update(status=COMPLETED, completed_at=now, summary=redact_text(summary) or None,
                      outputs=records,
                      verification={"method": "exists+non-empty+json-parse+sha256", "at": now,
                                    "note": verification_note or None})
            st["history"].append({"at": now, "event": "completed", "outputs": len(records)})
            for e in state["errors"]:  # earlier failed attempts at this stage are now moot
                if e["stage"] == stage_id and e["kind"] == "verification_failed" and not e["resolved"]:
                    e.update(resolved=True, resolved_at=now,
                             resolution="stage later completed with verified outputs")
            state["current_stage"] = schema.first_unfinished(state) or stage_id
            return records
        try:
            records = self._mutate(fn, checkpoint=f"completed {stage_id}")
        except VerificationError as e:
            self.log.event("stage.verify_failed", f"Not marked complete: {e}", "ERROR", stage=stage_id)
            self.add_error(f"Stage '{stage_id}' output verification failed: {e}", stage=stage_id,
                           kind="verification_failed")
            raise
        self.log.event("stage.completed", f"Stage completed; {len(records)} output(s) verified.", stage=stage_id)
        return {"stage": stage_id, "outputs": records}

    def fail_stage(self, stage_id: str, message: str, retryable: bool = True) -> None:
        def fn(state):
            st = self._stage(state, stage_id)
            if st["status"] in DONE_STATUSES:
                raise StateError(f"stage '{stage_id}' is already {st['status']}")
            st["status"] = FAILED
            st["history"].append({"at": util.utcnow(), "event": "failed", "retryable": retryable})
            state["current_stage"] = stage_id
            self._append_error(state, redact_text(message), stage_id, "stage_failed", retryable)
        self._mutate(fn)
        self.log.event("stage.failed", message, "ERROR", stage=stage_id)

    def skip_stage(self, stage_id: str, reason: str) -> None:
        if not reason.strip():
            raise StateError("skipping a stage requires a reason")

        def fn(state):
            st = self._stage(state, stage_id)
            if st["status"] in DONE_STATUSES:
                raise StateError(f"stage '{stage_id}' is already {st['status']}")
            idx = state["stage_order"].index(stage_id)
            for prev in state["stage_order"][:idx]:
                if state["stages"][prev]["status"] not in DONE_STATUSES:
                    raise StateError(f"cannot skip '{stage_id}': earlier stage '{prev}' is unfinished")
            st.update(status=SKIPPED, skip_reason=redact_text(reason), completed_at=util.utcnow())
            st["history"].append({"at": util.utcnow(), "event": "skipped", "reason": redact_text(reason)})
            state["current_stage"] = schema.first_unfinished(state) or stage_id
        self._mutate(fn, checkpoint=f"skipped {stage_id}")
        self.log.event("stage.skipped", f"Stage skipped: {reason}", stage=stage_id)

    def reopen_stage(self, stage_id: str, reason: str) -> list:
        """Deliberately invalidate a stage and every later finished stage.

        Old outputs are kept in stage history; checkpoints are untouched. The
        operation journal is kept so completed destructive operations still
        cannot be repeated by accident.
        """
        if not reason.strip():
            raise StateError("reopening a stage requires a reason")

        def fn(state):
            self._stage(state, stage_id)
            idx = state["stage_order"].index(stage_id)
            reopened = []
            for sid in state["stage_order"][idx:]:
                st = state["stages"][sid]
                if st["status"] in DONE_STATUSES or sid == stage_id:
                    st["history"].append({"at": util.utcnow(), "event": "reopened",
                                          "reason": redact_text(reason),
                                          "previous_status": st["status"],
                                          "previous_outputs": st["outputs"]})
                    st.update(status=PENDING, completed_at=None, outputs=[], verification=None,
                              summary=None, skip_reason=None)
                    reopened.append(sid)
            state["current_stage"] = stage_id
            return reopened
        reopened = self._mutate(fn, checkpoint=f"reopened {stage_id}")
        self.log.event("stage.reopened", f"Reopened {', '.join(reopened)}: {reason}", "WARN", stage=stage_id)
        return reopened

    # ------------------------------------------------------------------ operation journal
    def begin_operation(self, stage_id: str, op_id: str, description: str,
                        destructive: bool = False) -> str:
        """Journal an operation BEFORE doing it. Returns 'started' or 'already_done'.

        An operation that already completed returns 'already_done' (do not repeat
        it). A destructive one that was started but never confirmed raises
        UncertainOperationError: the real world may or may not have changed.
        """
        def fn(state):
            st = self._stage(state, stage_id)
            if st["status"] != IN_PROGRESS:
                raise StateError(f"stage '{stage_id}' is {st['status']}; start it first")
            op = st["operations"].get(op_id)
            if op:
                if op["status"] == OP_COMPLETED:
                    return "already_done"
                if op["status"] == OP_STARTED and op["destructive"]:
                    raise UncertainOperationError(
                        f"destructive operation '{op_id}' was started at {op['started_at']} and never "
                        "confirmed. Verify the real state, then resolve_operation.")
                op["attempts"] += 1
                op.update(status=OP_STARTED, started_at=util.utcnow(), finished_at=None)
                return "started"
            st["operations"][op_id] = {
                "description": redact_text(description), "destructive": destructive,
                "status": OP_STARTED, "attempts": 1, "started_at": util.utcnow(),
                "finished_at": None, "result": None}
            return "started"
        verdict = self._mutate(fn)
        if verdict == "started":
            self.log.event("op.started",
                           f"{'DESTRUCTIVE ' if destructive else ''}operation '{op_id}': {description}",
                           "WARN" if destructive else "INFO", stage=stage_id)
        else:
            self.log.event("op.skipped", f"Operation '{op_id}' already completed earlier; not repeating.",
                           stage=stage_id)
        return verdict

    def complete_operation(self, stage_id: str, op_id: str, result: str = "") -> None:
        self._finish_op(stage_id, op_id, OP_COMPLETED, result)

    def fail_operation(self, stage_id: str, op_id: str, error: str) -> None:
        self._finish_op(stage_id, op_id, OP_FAILED, error)

    def resolve_operation(self, stage_id: str, op_id: str, applied: bool, note: str) -> None:
        """After inspecting reality: record whether an interrupted operation took effect."""
        if not note.strip():
            raise StateError("resolving an operation requires a note describing what was checked")
        self._finish_op(stage_id, op_id, OP_COMPLETED if applied else OP_NOT_APPLIED,
                        f"manually verified: {note}", must_be_open=True)

    def _finish_op(self, stage_id, op_id, status, result, must_be_open=False):
        def fn(state):
            st = self._stage(state, stage_id)
            op = st["operations"].get(op_id)
            if not op:
                raise StateError(f"unknown operation '{op_id}' in stage '{stage_id}'")
            if must_be_open and op["status"] != OP_STARTED:
                raise StateError(f"operation '{op_id}' is {op['status']}, nothing to resolve")
            op.update(status=status, finished_at=util.utcnow(), result=redact_text(result) or None)
        self._mutate(fn)
        level = "ERROR" if status == OP_FAILED else "INFO"
        self.log.event(f"op.{status}", f"Operation '{op_id}' {status}. {result}".strip(), level, stage=stage_id)

    # ------------------------------------------------------------------ decisions, tasks, errors, notes
    @staticmethod
    def _next_id(items, prefix):
        return f"{prefix}{len(items) + 1:03d}"

    def add_decision(self, text: str, rationale: str = "", category: str = "creative",
                     stage: str | None = None, made_by: str = "user") -> str:
        def fn(state):
            did = self._next_id(state["decisions"], "D")
            state["decisions"].append({"id": did, "text": redact_text(text),
                                       "rationale": redact_text(rationale), "category": category,
                                       "stage": stage, "made_by": made_by, "at": util.utcnow()})
            return did
        did = self._mutate(fn)
        self.log.event("decision.recorded", f"{did}: {text}", stage=stage)
        return did

    def add_constraint(self, text: str) -> str:
        def fn(state):
            cid = self._next_id(state["constraints"], "C")
            state["constraints"].append({"id": cid, "text": redact_text(text), "added_at": util.utcnow()})
            return cid
        cid = self._mutate(fn)
        self.log.event("constraint.added", f"{cid}: {text}")
        return cid

    def add_task(self, text: str, stage: str | None = None) -> str:
        def fn(state):
            tid = self._next_id(state["tasks"], "T")
            state["tasks"].append({"id": tid, "text": redact_text(text), "stage": stage,
                                   "status": "pending", "created_at": util.utcnow(), "done_at": None})
            return tid
        tid = self._mutate(fn)
        self.log.event("task.added", f"{tid}: {text}", stage=stage)
        return tid

    def complete_task(self, task_id: str) -> None:
        def fn(state):
            for t in state["tasks"]:
                if t["id"] == task_id:
                    t.update(status="done", done_at=util.utcnow())
                    return
            raise StateError(f"unknown task '{task_id}'")
        self._mutate(fn)
        self.log.event("task.done", task_id)

    @staticmethod
    def _append_error(state, message, stage, kind, retryable=True):
        eid = f"E{len(state['errors']) + 1:03d}"
        state["errors"].append({"id": eid, "message": message, "stage": stage, "kind": kind,
                                "retryable": retryable, "at": util.utcnow(),
                                "resolved": False, "resolved_at": None, "resolution": None})
        return eid

    def add_error(self, message: str, stage: str | None = None, kind: str = "error",
                  retryable: bool = True) -> str:
        eid = self._mutate(lambda s: self._append_error(s, redact_text(message), stage, kind, retryable))
        self.log.event("error.recorded", f"{eid}: {message}", "ERROR", stage=stage)
        return eid

    def resolve_error(self, error_id: str, resolution: str) -> None:
        def fn(state):
            for e in state["errors"]:
                if e["id"] == error_id:
                    e.update(resolved=True, resolved_at=util.utcnow(), resolution=redact_text(resolution))
                    return
            raise StateError(f"unknown error '{error_id}'")
        self._mutate(fn)
        self.log.event("error.resolved", f"{error_id}: {resolution}")

    def add_note(self, text: str) -> None:
        self._mutate(lambda s: s["notes"].append({"text": redact_text(text), "at": util.utcnow()}))
        self.log.event("note.added", text)

    def record_provider_switch(self, from_provider: str, to_provider: str, reason: str = "") -> None:
        """Record that a human switched AI provider. This does NOT switch anything itself."""
        self._mutate(lambda s: s["provider_history"].append({
            "at": util.utcnow(), "from": redact_text(from_provider), "to": redact_text(to_provider),
            "reason": redact_text(reason)}))
        self.log.event("provider.switch_recorded",
                       f"Recorded switch {from_provider} -> {to_provider}. {reason}".strip())
