"""The high-level executor: validated, journaled, read-after-write Resolve operations.

Lifecycle of every request (`Executor.execute`):

1. **Capability gate** (`capabilities`): an unknown request type, and any NOT_TESTED or UNSUPPORTED kind, is refused
   explicitly as UNSUPPORTED, before anything else.
2. **Validate** (pure, `validation.validate`): a bad request never reaches Resolve.
3. **Idempotency** (journal): a key already done returns ALREADY_DONE; a key with an unfinished attempt is refused
   (UNCERTAIN_PREVIOUS_ATTEMPT) until `resolve_uncertain` records what Resolve actually did.
4. **Identity and preconditions** (adapter reads): the expected project is open, the timeline exists with the
   expected rate, tracks exist, the slot is free, the source range lies inside the media.
5. **Journal `begin`** with the pre-state, then the mutation.
6. **Read back and verify**. SUCCEEDED only if the readback shows exactly the requested change. A readback showing
   no change after a transport error may be retried (bounded); anything else is FAILED (state known) or UNCERTAIN
   (state unknown; the journal entry stays open).

Nothing here retries a mutation whose effect is unknown, and nothing exposes the raw Resolve API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import operations as ops
from .adapter import ResolveAdapter
from .capabilities import EXECUTABLE, capability_for
from .errors import AdapterError, ExecutorError, FailureClass, NOTHING_SENT, RECOVERY, TRANSIENT
from .journal import Journal, MemoryJournal
from .probe import ProbeError, Prober, ffprobe, report as probe_report, verify_media
from .timebase import (FrameRange, TimebaseError, parse_rate, source_frames_for_duration, timecode_to_frames,
                       timeline_duration_for_source)
from .validation import Policy, normalise, validate

TOLERANCE = 1e-4


class Outcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    ALREADY_DONE = "ALREADY_DONE"
    REFUSED = "REFUSED"          # nothing was sent to Resolve
    FAILED = "FAILED"            # sent; the resulting state is known (readback) and is not the requested one
    UNCERTAIN = "UNCERTAIN"      # sent; the resulting state could not be established


@dataclass
class OperationResult:
    op_id: str
    kind: str
    outcome: Outcome
    failure: Optional[FailureClass] = None
    message: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.SUCCEEDED, Outcome.ALREADY_DONE)

    @property
    def recovery(self) -> Optional[str]:
        return RECOVERY[self.failure] if self.failure else None

    def to_dict(self) -> Dict[str, Any]:
        return {"op_id": self.op_id, "kind": self.kind, "outcome": self.outcome.value,
                "failure": self.failure.value if self.failure else None, "message": self.message,
                "recovery": self.recovery, "attempts": self.attempts, "evidence": self.evidence}


@dataclass
class _Prepared:
    pre: Dict[str, Any]
    apply: Callable[[], Any]
    check: Callable[[Any], Tuple[str, Dict[str, Any]]]    # -> ("applied" | "unchanged" | "mismatch", evidence)
    expect: Dict[str, Any] = field(default_factory=dict)


class _Precondition(Exception):
    def __init__(self, failure: FailureClass, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.failure, self.message, self.details = failure, message, details or {}


def _num_equal(a: Any, b: Any) -> bool:
    if isinstance(b, bool) or isinstance(a, bool):
        return bool(a) == bool(b) if isinstance(a, (bool, int)) and isinstance(b, (bool, int)) else a == b
    try:
        return abs(float(a) - float(b)) <= TOLERANCE
    except (TypeError, ValueError):
        return a == b


def _item_key(it: Dict[str, Any]) -> Tuple:
    return (it.get("start"), it.get("end"), it.get("media_id"), it.get("name"))


class Executor:
    def __init__(self, adapter: ResolveAdapter, policy: Policy, journal: Optional[Journal] = None, *,
                 expected_project: Optional[str] = None, prober: Prober = ffprobe,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 max_read_retries: int = 2, max_mutation_retries: int = 1, render_poll_seconds: float = 1.0,
                 checkpoint_before_destructive: bool = True) -> None:
        self.adapter = adapter
        self.policy = policy
        self.journal = journal if journal is not None else MemoryJournal()
        self.expected_project = expected_project
        self.prober = prober
        self.sleep, self.clock = sleep, clock
        self.max_read_retries = max(0, int(max_read_retries))
        self.max_mutation_retries = max(0, int(max_mutation_retries))
        self.render_poll_seconds = render_poll_seconds
        self.checkpoint_before_destructive = checkpoint_before_destructive

    # ── public API ───────────────────────────────────────────────────────────

    def execute(self, op: ops.Operation) -> OperationResult:
        kind = getattr(op, "KIND", type(op).__name__)
        op_id = getattr(op, "op_id", "?")
        if not isinstance(op, ops.Operation) or type(op) not in ops.ALL_OPERATIONS:
            return OperationResult(op_id, kind, Outcome.REFUSED, FailureClass.UNSUPPORTED,
                                   "not a supported operation request: only the typed requests in operations.py are "
                                   "accepted; there is no raw-call request", {"type": type(op).__name__})
        # The capability gate runs BEFORE detailed validation so that an unsupported kind is always reported as
        # UNSUPPORTED, whatever else is wrong with its fields.
        cap = capability_for(op.KIND)
        if cap.status not in EXECUTABLE:
            return OperationResult(op_id, kind, Outcome.REFUSED, FailureClass.UNSUPPORTED,
                                   f"{cap.name} is {cap.status}: {cap.limitations}", {"capability": cap.to_dict()})
        problems = validate(op, self.policy)
        if problems:
            return OperationResult(op_id, kind, Outcome.REFUSED, FailureClass.VALIDATION, "; ".join(problems),
                                   {"problems": problems})
        key = getattr(op, "idempotency_key", None)
        if not key and op.MUTATING:
            # Without an explicit key the journal still records the attempt under "op:<op_id>"; an unfinished one
            # blocks the same request being resubmitted, exactly like an explicit key.
            implicit = f"op:{op.op_id}"
            if self.journal.state_of(implicit)["state"] == "uncertain":
                return OperationResult(op_id, kind, Outcome.REFUSED, FailureClass.UNCERTAIN_PREVIOUS_ATTEMPT,
                                       f"a previous attempt of {implicit!r} never finished; inspect Resolve and call "
                                       f"resolve_uncertain({implicit!r}, ...) first",
                                       {"idempotency_key": implicit})
        if key:
            st = self.journal.state_of(key)
            if st["state"] == "done":
                return OperationResult(op_id, kind, Outcome.ALREADY_DONE, None,
                                       "this idempotency key already completed; nothing sent",
                                       {"previous": st["record"]})
            if st["state"] == "uncertain":
                return OperationResult(op_id, kind, Outcome.REFUSED, FailureClass.UNCERTAIN_PREVIOUS_ATTEMPT,
                                       "a previous attempt with this key never finished; inspect and resolve it first",
                                       {"previous": st["record"]})
        if not op.MUTATING:
            return self._read(op)
        if isinstance(op, ops.RenderTimeline):
            from .render import run_render     # local import: render depends on this module
            return run_render(self, op)
        return self._mutate(op)

    def run_plan(self, plan: Sequence[ops.Operation], *, stop_on_failure: bool = True) -> List[OperationResult]:
        results = []
        for op in plan:
            r = self.execute(op)
            results.append(r)
            if stop_on_failure and not r.ok:
                break
        return results

    def snapshot(self, timeline: Optional[str] = None) -> Dict[str, Any]:
        """Structure of the project and (optionally) one timeline, read back from Resolve."""
        snap: Dict[str, Any] = {"project": self._retry_read(self.adapter.current_project)}
        if timeline:
            info = self._retry_read(lambda: self.adapter.timeline_info(timeline))
            snap["timeline"] = info
            if info:
                tracks: Dict[str, Any] = {}
                for k in ("video", "audio"):
                    for i in range(1, int(info["track_counts"].get(k, 0)) + 1):
                        tracks[f"{k}{i}"] = self._retry_read(lambda k=k, i=i: self.adapter.track_items(timeline, k, i))
                snap["tracks"] = tracks
                snap["markers"] = {str(f): m for f, m in self._retry_read(lambda: self.adapter.markers(timeline)).items()}
        return snap

    def checkpoint(self, label: str, timeline: Optional[str] = None, *, save: bool = True) -> Dict[str, Any]:
        saved = None
        if save:
            try:
                saved = bool(self.adapter.save_project())
            except AdapterError as exc:
                saved = False
                self.journal.append("checkpoint_save_failed", label=label, error=exc.to_dict())
        snap = self.snapshot(timeline)
        snap["saved"] = saved
        path = self.journal.write_checkpoint(label, snap)
        return {"label": label, "saved": saved, "file": path}

    def resolve_uncertain(self, key: str, applied: bool, note: str) -> Dict[str, Any]:
        """Record, after inspecting Resolve, whether an unfinished attempt was applied. Required before retrying."""
        if not note or not note.strip():
            raise ValueError("say what was inspected")
        st = self.journal.state_of(key)
        if st["state"] != "uncertain":
            raise ValueError(f"key {key!r} is {st['state']}, not uncertain")
        return self.journal.append("resolved", key=key, applied=bool(applied), note=note.strip())

    # ── reads ────────────────────────────────────────────────────────────────

    def _retry_read(self, fn: Callable[[], Any]) -> Any:
        attempt = 0
        while True:
            try:
                return fn()
            except AdapterError as exc:
                if exc.failure in TRANSIENT and attempt < self.max_read_retries:
                    attempt += 1
                    self.sleep(0.5 * attempt)
                    continue
                raise

    def _read(self, op: ops.Operation) -> OperationResult:
        try:
            if isinstance(op, ops.InspectProject):
                ev = {"health": self._retry_read(self.adapter.health),
                      "project": self._retry_read(self.adapter.current_project),
                      "timelines": self._retry_read(self.adapter.timeline_names),
                      "current_timeline": self._retry_read(self.adapter.current_timeline),
                      "media": self._retry_read(self.adapter.media_items)}
                if self.expected_project and ev["project"] != self.expected_project:
                    return OperationResult(op.op_id, op.KIND, Outcome.FAILED, FailureClass.IDENTITY_MISMATCH,
                                           f"open project is {ev['project']!r}, expected {self.expected_project!r}", ev)
                return OperationResult(op.op_id, op.KIND, Outcome.SUCCEEDED, evidence=ev, attempts=1)
            if isinstance(op, ops.InspectTimeline):
                self._check_project()
                snap = self.snapshot(op.timeline)
                if not snap.get("timeline"):
                    return OperationResult(op.op_id, op.KIND, Outcome.FAILED, FailureClass.PRECONDITION,
                                           "timeline not found", snap)
                return OperationResult(op.op_id, op.KIND, Outcome.SUCCEEDED, evidence=snap, attempts=1)
            if isinstance(op, ops.VerifyExport):
                try:
                    info = self.prober(op.path)
                except ProbeError as exc:
                    return OperationResult(op.op_id, op.KIND, Outcome.FAILED, FailureClass.EXPORT_INVALID,
                                           str(exc), probe_report(None, [], exc.code))
                problems = verify_media(info, op.expect)
                rep = probe_report(info, problems)
                if problems:
                    return OperationResult(op.op_id, op.KIND, Outcome.FAILED, FailureClass.EXPORT_INVALID,
                                           "; ".join(problems), rep)
                return OperationResult(op.op_id, op.KIND, Outcome.SUCCEEDED, evidence=rep, attempts=1)
        except _Precondition as exc:
            return OperationResult(op.op_id, op.KIND, Outcome.REFUSED, exc.failure, exc.message, exc.details)
        except AdapterError as exc:
            return OperationResult(op.op_id, op.KIND, Outcome.FAILED, exc.failure, exc.message, exc.details)
        raise AssertionError(f"unhandled read {op.KIND}")   # pragma: no cover

    # ── mutations ────────────────────────────────────────────────────────────

    def _check_project(self) -> None:
        if self.expected_project is None:
            raise _Precondition(FailureClass.IDENTITY_MISMATCH,
                                "no expected project is set; refusing to touch whatever project happens to be open")
        current = self._retry_read(self.adapter.current_project)
        if current != self.expected_project:
            raise _Precondition(FailureClass.IDENTITY_MISMATCH,
                                f"open project is {current!r}, expected {self.expected_project!r}",
                                {"open": current, "expected": self.expected_project})

    def _timeline(self, name: str) -> Dict[str, Any]:
        info = self._retry_read(lambda: self.adapter.timeline_info(name))
        if not info:
            raise _Precondition(FailureClass.PRECONDITION, f"timeline {name!r} does not exist")
        try:
            info["_rate"] = parse_rate(info["frame_rate"])
        except TimebaseError as exc:
            raise _Precondition(FailureClass.PRECONDITION, f"timeline rate unreadable: {exc}")
        return info

    def _media(self, path: str) -> Dict[str, Any]:
        want = normalise(path)
        found = [m for m in self._retry_read(self.adapter.media_items) if m.get("path") and normalise(m["path"]) == want]
        if not found:
            raise _Precondition(FailureClass.PRECONDITION, "media is not in the media pool; import it first",
                                {"path": path})
        if len(found) > 1:
            raise _Precondition(FailureClass.PRECONDITION, "media is in the pool more than once; ambiguous",
                                {"path": path, "ids": [m["id"] for m in found]})
        return found[0]

    def _track_items(self, timeline: str, kind: str, index: int) -> List[Dict[str, Any]]:
        return self._retry_read(lambda: self.adapter.track_items(timeline, kind, index))

    def _mutate(self, op: ops.Operation) -> OperationResult:
        key = getattr(op, "idempotency_key", None) or f"op:{op.op_id}"
        try:
            if not isinstance(op, ops.CreateProject):
                self._check_project()
            prep = getattr(self, f"_prep_{op.KIND}")(op)
        except _Precondition as exc:
            return OperationResult(op.op_id, op.KIND, Outcome.REFUSED, exc.failure, exc.message, exc.details)
        except AdapterError as exc:
            out = Outcome.REFUSED if exc.failure in NOTHING_SENT or exc.failure in TRANSIENT else Outcome.FAILED
            return OperationResult(op.op_id, op.KIND, out, exc.failure, f"preflight read failed: {exc.message}",
                                   exc.details)
        except Exception as exc:     # a bug before anything was sent: nothing changed, say so
            return OperationResult(op.op_id, op.KIND, Outcome.REFUSED, FailureClass.INTERNAL,
                                   f"preflight failed unexpectedly ({type(exc).__name__}); nothing was sent",
                                   {"exception": type(exc).__name__})
        if op.DESTRUCTIVE and self.checkpoint_before_destructive:
            try:
                self.checkpoint(f"before_{op.op_id}", getattr(op, "timeline", None))
            except AdapterError as exc:
                return OperationResult(op.op_id, op.KIND, Outcome.REFUSED, exc.failure,
                                       f"could not checkpoint before a destructive operation: {exc.message}")

        self.journal.append("begin", key=key, op_id=op.op_id, kind=op.KIND, request=op.to_dict(), pre=prep.pre,
                            expect=prep.expect)
        attempts = 0
        while True:
            attempts += 1
            returned: Any = None
            call_error: Optional[AdapterError] = None
            try:
                returned = prep.apply()
            except AdapterError as exc:
                call_error = exc
                if exc.failure is FailureClass.BRIDGE_UNAVAILABLE:
                    return self._finish(key, op, Outcome.FAILED, exc.failure, "bridge unavailable; nothing was sent",
                                        {"error": exc.to_dict()}, attempts)
            except Exception as exc:
                # An unexpected error while the mutation may have been sent: effect unknown, journal entry stays
                # OPEN, never repeated automatically.
                return OperationResult(op.op_id, op.KIND, Outcome.UNCERTAIN, FailureClass.INTERNAL,
                                       f"unexpected {type(exc).__name__} while applying; the effect is unknown",
                                       {"exception": type(exc).__name__, "idempotency_key": key}, attempts)
            try:
                verdict, evidence = prep.check(returned)
            except Exception as exc:
                # The mutation may have landed and we cannot see. Leave the journal entry OPEN.
                msg = exc.message if hasattr(exc, "message") else str(exc)
                return OperationResult(op.op_id, op.KIND, Outcome.UNCERTAIN, FailureClass.INTERRUPTED,
                                       f"readback after the mutation failed: {msg}",
                                       {"call_error": call_error.to_dict() if call_error else None,
                                        "returned": returned, "idempotency_key": key}, attempts)
            evidence = {"returned": returned, **evidence}
            if call_error is not None:
                evidence["call_error"] = call_error.to_dict()
            if verdict == "applied":
                note = "applied (confirmed by readback despite a transport error)" if call_error else "applied"
                return self._finish(key, op, Outcome.SUCCEEDED, None, note, evidence, attempts)
            if verdict == "unchanged":
                if call_error is not None and call_error.failure in TRANSIENT and attempts <= self.max_mutation_retries:
                    self.journal.append("retry", key=key, op_id=op.op_id, attempt=attempts,
                                        reason="readback proved the attempt was not applied")
                    self.sleep(1.0)
                    continue
                failure = call_error.failure if call_error else FailureClass.RESOLVE_REFUSED
                return self._finish(key, op, Outcome.FAILED, failure,
                                    "Resolve did not apply the change (readback unchanged)", evidence, attempts)
            return self._finish(key, op, Outcome.FAILED, FailureClass.READBACK_MISMATCH,
                                evidence.get("message") or "readback differs from the request", evidence, attempts)

    def _finish(self, key: str, op: ops.Operation, outcome: Outcome, failure: Optional[FailureClass], message: str,
                evidence: Dict[str, Any], attempts: int) -> OperationResult:
        self.journal.append("finish", key=key, op_id=op.op_id, kind=op.KIND, outcome=outcome.value,
                            failure=failure.value if failure else None, message=message, evidence=evidence)
        return OperationResult(op.op_id, op.KIND, outcome, failure, message, evidence, attempts)

    # ── per-operation preparation (preflight + apply + check) ───────────────

    def _prep_create_project(self, op: ops.CreateProject) -> _Prepared:
        names = self._retry_read(self.adapter.project_names)
        if op.name in names:
            raise _Precondition(FailureClass.PRECONDITION, f"project {op.name!r} already exists; refusing to reuse it")
        before = self._retry_read(self.adapter.current_project)

        def check(returned):
            now = self.adapter.current_project()
            if now == op.name:
                self.expected_project = op.name
                return "applied", {"current_project": now}
            if op.name in self.adapter.project_names():
                return "mismatch", {"message": "project created but not open", "current_project": now}
            return "unchanged", {"current_project": now}

        return _Prepared({"current_project": before, "project_count": len(names)},
                         lambda: self.adapter.create_project(op.name), check)

    def _prep_save_project(self, op: ops.SaveProject) -> _Prepared:
        def check(returned):
            return ("applied" if returned is True else "unchanged"), {"note": "Resolve offers no save-state readback"}
        return _Prepared({}, self.adapter.save_project, check)

    def _prep_import_media(self, op: ops.ImportMedia) -> _Prepared:
        before = self._retry_read(self.adapter.media_items)
        present = {normalise(m["path"]) for m in before if m.get("path")}
        missing = [p for p in op.paths if normalise(p) not in present]

        def apply():
            return self.adapter.import_media(missing) if missing else []

        def check(returned):
            after = self.adapter.media_items()
            counts: Dict[str, int] = {}
            for m in after:
                if m.get("path"):
                    counts[normalise(m["path"])] = counts.get(normalise(m["path"]), 0) + 1
            got = {p: counts.get(normalise(p), 0) for p in op.paths}
            ev = {"pool_counts": got, "already_present": [p for p in op.paths if p not in missing],
                  "imported": [m for m in after if m.get("path") and normalise(m["path"]) in
                               {normalise(x) for x in missing}]}
            if all(v == 1 for v in got.values()):
                return "applied", ev
            if all(got[p] == (0 if p in missing else 1) for p in op.paths):
                return "unchanged", ev
            return "mismatch", {**ev, "message": "media pool does not hold each path exactly once"}

        return _Prepared({"pool_size": len(before), "missing": missing}, apply, check)

    def _prep_create_timeline(self, op: ops.CreateTimeline) -> _Prepared:
        names = self._retry_read(self.adapter.timeline_names)
        if op.name in names:
            raise _Precondition(FailureClass.PRECONDITION, f"timeline {op.name!r} already exists")
        rate = parse_rate(op.frame_rate)
        start = timecode_to_frames(op.start_timecode, rate)
        steps: List[Dict[str, Any]] = []
        # Measured on Resolve Free 21.0.4.5: Timeline.SetSetting('timelineFrameRate') accepts integer rates only
        # (24/25/30/48/50/60); NTSC rates come only from the PROJECT rate, which can change only while the project
        # has no timelines. New timelines inherit the project rate.
        try:
            project_rate = parse_rate(str(self._retry_read(lambda: self.adapter.project_setting("timelineFrameRate"))))
        except TimebaseError:
            project_rate = None
        set_project_rate = not names and project_rate != rate
        if not rate.is_integer and not set_project_rate and project_rate != rate:
            raise _Precondition(FailureClass.PRECONDITION,
                                f"a {rate} fps timeline needs the project rate to be {rate}, but the project is at "
                                f"{project_rate} and already has timelines (Resolve cannot set NTSC rates per "
                                f"timeline); use a new project")

        def apply():
            if set_project_rate:
                ok = self.adapter.set_project_setting("timelineFrameRate", rate.resolve_text())
                steps.append({"project_rate": ok})
                if not ok and not rate.is_integer:
                    return steps
            ok = self.adapter.create_timeline(op.name)
            steps.append({"create": ok})
            if not ok:
                return steps
            settings = [("useCustomSettings", "1")]
            if rate.is_integer:
                settings.append(("timelineFrameRate", rate.resolve_text()))
            settings += [("timelineResolutionWidth", str(op.width)), ("timelineResolutionHeight", str(op.height))]
            for k, v in settings:
                steps.append({k: self.adapter.set_timeline_setting(op.name, k, v)})
            steps.append({"start_timecode": self.adapter.set_timeline_start_timecode(op.name, op.start_timecode)})
            return steps

        def check(returned):
            info = self.adapter.timeline_info(op.name)
            if not info:
                return "unchanged", {"timeline": None}
            problems = []
            try:
                if parse_rate(info["frame_rate"]) != rate:
                    problems.append(f"rate {info['frame_rate']} != {rate}")
            except TimebaseError:
                problems.append(f"rate unreadable: {info['frame_rate']!r}")
            if info.get("width") != op.width or info.get("height") != op.height:
                problems.append(f"resolution {info.get('width')}x{info.get('height')} != {op.width}x{op.height}")
            if info.get("start_frame") != start:
                problems.append(f"start frame {info.get('start_frame')} != {start}")
            ev = {"timeline": info, "steps": steps}
            if problems:
                return "mismatch", {**ev, "message": "timeline created with wrong settings: " + "; ".join(problems)}
            return "applied", ev

        return _Prepared({"timelines": names}, apply, check,
                         {"frame_rate": str(rate), "width": op.width, "height": op.height, "start_frame": start})

    def _prep_add_track(self, op: ops.AddTrack) -> _Prepared:
        info = self._timeline(op.timeline)
        before = int(info["track_counts"].get(op.track_kind, 0))
        if before >= self.policy.max_tracks:
            raise _Precondition(FailureClass.PRECONDITION, f"already {before} {op.track_kind} tracks")
        subtype = op.audio_format if op.track_kind == "audio" else None

        def check(returned):
            after = int((self.adapter.timeline_info(op.timeline) or {}).get("track_counts", {}).get(op.track_kind, 0))
            ev = {"before": before, "after": after}
            if after == before + 1:
                return "applied", ev
            if after == before:
                return "unchanged", ev
            return "mismatch", {**ev, "message": "track count changed by more than one"}

        return _Prepared({"count": before}, lambda: self.adapter.add_track(op.timeline, op.track_kind, subtype), check,
                         {"count": before + 1})

    def _placement(self, timeline: str, info: Dict[str, Any], kind: str, index: int) -> List[Dict[str, Any]]:
        if index > int(info["track_counts"].get(kind, 0)):
            raise _Precondition(FailureClass.PRECONDITION, f"{kind} track {index} does not exist (has "
                                f"{info['track_counts'].get(kind, 0)}); add it first")
        return self._track_items(timeline, kind, index)

    def _prep_append_segment(self, op: ops.AppendSegment) -> _Prepared:
        info = self._timeline(op.timeline)
        tl_rate = info["_rate"]
        media = self._media(op.media_path)
        try:
            src_rate = parse_rate(media.get("fps") or "")
        except TimebaseError:
            raise _Precondition(FailureClass.PRECONDITION, f"media frame rate unreadable: {media.get('fps')!r}")
        frames = media.get("frames")
        if not isinstance(frames, int) or frames <= 0:
            raise _Precondition(FailureClass.PRECONDITION, "media frame count unreadable", {"media": media})
        src: FrameRange = op.source
        if not src.within(frames):
            raise _Precondition(FailureClass.PRECONDITION,
                                f"source range [{src.start}, {src.end}) exceeds the media ({frames} frames, last {frames - 1})")
        mtype = str(media.get("type") or "")
        if mtype:
            need = {"video": ("Video",), "audio": ("Audio",), "video_audio": ("Video", "Audio")}[op.media]
            missing_parts = [part for part in need if part not in mtype]
            if missing_parts:
                raise _Precondition(FailureClass.PRECONDITION,
                                    f"media type {mtype!r} has no {' or '.join(missing_parts).lower()} part")
        duration = timeline_duration_for_source(src.length, src_rate, tl_rate)
        if duration < 1:
            raise _Precondition(FailureClass.PRECONDITION, "source range is shorter than one timeline frame")
        targets: List[Tuple[str, int, Optional[int]]] = []
        if op.media in ("video", "video_audio"):
            targets.append(("video", op.track_index, 1 if op.media == "video" else None))
        if op.media == "audio":
            targets.append(("audio", op.track_index, 2))
        if op.media == "video_audio":
            targets.append(("audio", int(op.audio_track_index), None))
        before = {f"{k}{i}": self._placement(op.timeline, info, k, i) for k, i, _ in targets}
        if op.record_frame is not None:
            record = op.record_frame
        else:
            ends = [it["end"] for items in before.values() for it in items if isinstance(it.get("end"), int)]
            record = max(ends) if ends else int(info["start_frame"])
        if record < int(info["start_frame"]):
            raise _Precondition(FailureClass.PRECONDITION,
                                f"record_frame {record} is before the timeline start {info['start_frame']} (absolute frames)")
        slot = FrameRange(record, record + duration)
        for tk, items in before.items():
            for it in items:
                if isinstance(it.get("start"), int) and isinstance(it.get("end"), int) and \
                        slot.overlaps(FrameRange(it["start"], max(it["end"], it["start"] + 1))):
                    raise _Precondition(FailureClass.PRECONDITION,
                                        f"slot [{slot.start}, {slot.end}) on {tk} overlaps an existing item "
                                        f"[{it['start']}, {it['end']})")
        if op.media == "video_audio":
            infos = [{"media_id": media["id"], "start": src.start, "end": src.end, "record": record,
                      "track_index": op.track_index, "media_type": 1},
                     {"media_id": media["id"], "start": src.start, "end": src.end, "record": record,
                      "track_index": int(op.audio_track_index), "media_type": 2}]
        else:
            infos = [{"media_id": media["id"], "start": src.start, "end": src.end, "record": record,
                      "track_index": op.track_index, "media_type": targets[0][2]}]
        expect = {"record": record, "duration": duration, "end": record + duration, "source_start": src.start,
                  "source_end": src.end, "source_rate": str(src_rate), "timeline_rate": str(tl_rate),
                  "tracks": list(before)}

        def check(returned):
            ev: Dict[str, Any] = {"expect": expect, "placed": {}}
            changed_any = False
            problems = []
            for k, i, _ in targets:
                tk = f"{k}{i}"
                after = self.adapter.track_items(op.timeline, k, i)
                old = {_item_key(x) for x in before[tk]}
                new = [x for x in after if _item_key(x) not in old]
                removed = [x for x in before[tk] if _item_key(x) not in {_item_key(y) for y in after}]
                ev["placed"][tk] = new
                if new or removed:
                    changed_any = True
                if removed:
                    problems.append(f"{tk}: {len(removed)} existing item(s) changed or disappeared")
                if len(new) != 1:
                    problems.append(f"{tk}: expected exactly one new item, found {len(new)}")
                    continue
                it = new[0]
                if it.get("start") != record or it.get("end") != record + duration:
                    problems.append(f"{tk}: landed at [{it.get('start')}, {it.get('end')}), expected "
                                    f"[{record}, {record + duration})")
                if it.get("media_id") not in (None, media["id"]):
                    problems.append(f"{tk}: wrong media {it.get('media_id')}")
                if k == "video" and it.get("source_start") is not None and it["source_start"] != src.start:
                    problems.append(f"{tk}: source starts at frame {it['source_start']}, expected {src.start}")
            if not changed_any:
                return "unchanged", ev
            if problems:
                return "mismatch", {**ev, "message": "; ".join(problems)}
            return "applied", ev

        return _Prepared({"tracks": before, "media": media, "timeline": {k: v for k, v in info.items() if k != "_rate"}},
                         lambda: self.adapter.append(op.timeline, infos), check, expect)

    def _prep_set_transform(self, op: ops.SetTransform) -> _Prepared:
        self._timeline(op.timeline)
        ref = op.item
        keys = sorted(op.values)
        pre = self._retry_read(lambda: self.adapter.item_properties(op.timeline, ref.track_kind, ref.track_index,
                                                                    ref.start_frame, keys))
        results: Dict[str, Any] = {}

        def apply():
            for k in keys:
                results[k] = self.adapter.set_item_property(op.timeline, ref.track_kind, ref.track_index,
                                                            ref.start_frame, k, op.values[k])
            return results

        def check(returned):
            post = self.adapter.item_properties(op.timeline, ref.track_kind, ref.track_index, ref.start_frame, keys)
            ev = {"before": pre, "after": post, "set_returns": dict(results)}
            ok = [k for k in keys if _num_equal(post.get(k), op.values[k])]
            same = [k for k in keys if _num_equal(post.get(k), pre.get(k))]
            if len(ok) == len(keys):
                return "applied", ev
            if len(same) == len(keys):
                return "unchanged", ev
            return "mismatch", {**ev, "message": "not applied: " + ", ".join(k for k in keys if k not in ok)}

        return _Prepared({"properties": pre}, apply, check, {"values": dict(op.values)})

    def _prep_add_marker(self, op: ops.AddMarker) -> _Prepared:
        info = self._timeline(op.timeline)
        length = int(info["end_frame"]) - int(info["start_frame"])
        if length > 0 and op.frame >= length:
            raise _Precondition(FailureClass.PRECONDITION,
                                f"marker frame {op.frame} is past the timeline end (relative frames 0..{length - 1})")
        before = self._retry_read(lambda: self.adapter.markers(op.timeline))
        if op.frame in before:
            raise _Precondition(FailureClass.PRECONDITION, f"a marker already exists at relative frame {op.frame}")

        def check(returned):
            after = self.adapter.markers(op.timeline)
            m = after.get(op.frame)
            ev = {"marker": m, "count_before": len(before), "count_after": len(after)}
            if m is None:
                return "unchanged", ev
            problems = [f"{k} {m.get(k)!r} != {v!r}" for k, v in (("color", op.color), ("name", op.name),
                                                                    ("note", op.note), ("duration", op.duration))
                        if m.get(k) != v]
            if problems:
                return "mismatch", {**ev, "message": "; ".join(problems)}
            return "applied", ev

        return _Prepared({"count": len(before)},
                         lambda: self.adapter.add_marker(op.timeline, op.frame, op.color, op.name, op.note, op.duration),
                         check, {"frame": op.frame})

    def _prep_place_title(self, op: ops.PlaceTitle) -> _Prepared:
        info = self._timeline(op.timeline)
        if op.record_frame < int(info["start_frame"]):
            raise _Precondition(FailureClass.PRECONDITION, "record_frame is before the timeline start (absolute frames)")
        before = self._placement(op.timeline, info, "video", op.track_index)
        slot = FrameRange(op.record_frame, op.record_frame + op.duration)
        for it in before:
            if isinstance(it.get("start"), int) and isinstance(it.get("end"), int) and \
                    slot.overlaps(FrameRange(it["start"], max(it["end"], it["start"] + 1))):
                raise _Precondition(FailureClass.PRECONDITION, f"title slot overlaps an item at {it['start']}")
        title_tl = f"Title {op.op_id}"[:100].replace(":", "-")
        if title_tl in self._retry_read(self.adapter.timeline_names):
            raise _Precondition(FailureClass.PRECONDITION, f"title timeline {title_tl!r} already exists")
        state: Dict[str, Any] = {}

        def apply():
            media_id = self.adapter.create_title_clip(title_tl, op.text, op.duration, op.size, op.center,
                                                      info["_rate"].resolve_text() if info["_rate"].is_integer else None)
            state["media_id"] = media_id
            if not media_id:
                return {"title_clip": None}
            tinfo = self.adapter.timeline_info(title_tl) or {}
            state["title_length"] = (tinfo.get("end_frame") or 0) - (tinfo.get("start_frame") or 0)
            # The nested title timeline may run at another rate (measured: a new timeline inherits the project
            # rate), so the source length is converted with the same floor rule as any clip and must hit exactly.
            title_rate = parse_rate(str(tinfo.get("frame_rate") or info["frame_rate"]))
            state["title_rate"] = str(title_rate)
            n = source_frames_for_duration(op.duration, title_rate, info["_rate"])
            state["source_frames"] = n
            if timeline_duration_for_source(n, title_rate, info["_rate"]) != op.duration:
                state["too_short"] = True
                state["message"] = f"no whole number of {title_rate} fps title frames lasts exactly {op.duration} frames"
                return {"title_clip": media_id, "inexact": True}
            if state["title_length"] < n:
                state["too_short"] = True
                state["message"] = f"title clip is only {state['title_length']} frames; requested {op.duration}"
                return {"title_clip": media_id, "too_short": state["title_length"]}
            n = self.adapter.append(op.timeline, [{"media_id": media_id, "start": 0, "end": n,
                                                   "record": op.record_frame, "track_index": op.track_index,
                                                   "media_type": 1}])
            return {"title_clip": media_id, "appended": n}

        def check(returned):
            after = self.adapter.track_items(op.timeline, "video", op.track_index)
            old = {_item_key(x) for x in before}
            new = [x for x in after if _item_key(x) not in old]
            text = self.adapter.title_text(op.timeline, op.track_index, op.record_frame) if new else None
            ev = {"title_timeline": title_tl, "placed": new, "text": text, **state}
            if not new:
                if state.get("too_short"):
                    return "mismatch", {**ev, "message": state["message"]}
                return ("mismatch" if state.get("media_id") else "unchanged"), \
                    {**ev, "message": "title timeline created but not placed" if state.get("media_id") else ""}
            it = new[0]
            problems = []
            if len(new) != 1:
                problems.append(f"{len(new)} new items")
            if it.get("start") != op.record_frame or it.get("end") != op.record_frame + op.duration:
                problems.append(f"landed at [{it.get('start')}, {it.get('end')})")
            if text != op.text:
                problems.append(f"text reads back {text!r}")
            return ("mismatch", {**ev, "message": "; ".join(problems)}) if problems else ("applied", ev)

        return _Prepared({"track": before}, apply, check, {"record": op.record_frame, "duration": op.duration})

    def _prep_delete_items(self, op: ops.DeleteItems) -> _Prepared:
        self._timeline(op.timeline)
        tracks = sorted({(r.track_kind, r.track_index) for r in op.items})
        before = {f"{k}{i}": self._track_items(op.timeline, k, i) for k, i in tracks}
        for r in op.items:
            if not any(it.get("start") == r.start_frame for it in before[f"{r.track_kind}{r.track_index}"]):
                raise _Precondition(FailureClass.PRECONDITION, f"no item starts at {r.start_frame} on "
                                    f"{r.track_kind} {r.track_index}")
        targets = {(f"{r.track_kind}{r.track_index}", r.start_frame) for r in op.items}

        def check(returned):
            after = {f"{k}{i}": self.adapter.track_items(op.timeline, k, i) for k, i in tracks}
            still = [t for t in targets if any(it.get("start") == t[1] for it in after[t[0]])]
            others_before = {tk: sorted(map(_item_key, [x for x in its if (tk, x.get("start")) not in targets]),
                                        key=str) for tk, its in before.items()}
            others_after = {tk: sorted(map(_item_key, [x for x in its if (tk, x.get("start")) not in targets]),
                                       key=str) for tk, its in after.items()}
            ev = {"remaining_targets": [list(t) for t in still], "after": after}
            if others_before != others_after:
                return "mismatch", {**ev, "message": "items other than the targets changed"}
            if not still:
                return "applied", ev
            if len(still) == len(targets):
                return "unchanged", ev
            return "mismatch", {**ev, "message": "only some targets were removed"}

        refs = [r.to_dict() for r in op.items]
        return _Prepared({"tracks": before}, lambda: self.adapter.delete_items(op.timeline, refs), check)
