"""Render subsystem: validate, configure, queue, start, monitor, detect failure/completion, verify the file.

Rules (from the capability inventory and RESOLVE_INTEGRATION.md):

* Never overwrite: the output file must not exist before the job is queued.
* Every render setting is sent in its own `SetRenderSettings` payload and its boolean checked, so one rejected key
  cannot silently void the others.
* The queued job is read back from `GetRenderJobList` (the only render-settings readback Resolve has).
* Completion is CompletionPercentage == 100 with an empty Error, never the localized JobStatus text.
* The FILE decides success: ffprobe must show the requested codec, size, rate, frame count and audio stream.
* A timeout stops rendering and reports the partial file as invalid; nothing is restarted automatically.
* The page shown before the render is restored afterwards (render calls switch Resolve to Deliver).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import operations as ops
from .errors import AdapterError, FailureClass
from .probe import ProbeError, report as probe_report, verify_media

EXTENSIONS = {"mp4": "mp4", "mov": "mov"}
MAX_SAMPLES = 200


def output_path(spec: ops.RenderSpec) -> str:
    return os.path.join(spec.target_dir, f"{spec.file_name}.{EXTENSIONS.get(spec.format, spec.format)}")


def render_settings(spec: ops.RenderSpec) -> List[Dict[str, Any]]:
    """One payload per key, in a fixed order. MarkOut is Resolve-inclusive, so a half-open [in, out) sends out - 1.
    Measured on Free 21.0.4.5: {"SelectAllFrames": False} alone is rejected, while MarkIn/MarkOut alone are accepted
    and replace an inherited all-frames setting (the queued job reads them back)."""
    payloads: List[Dict[str, Any]] = []
    if spec.mark_in is not None:
        payloads += [{"MarkIn": spec.mark_in}, {"MarkOut": spec.mark_out - 1}]
    else:
        payloads.append({"SelectAllFrames": True})
    payloads += [{"TargetDir": spec.target_dir}, {"CustomName": spec.file_name},
                 {"FormatWidth": spec.width}, {"FormatHeight": spec.height},
                 {"ExportVideo": bool(spec.export_video)}, {"ExportAudio": bool(spec.export_audio)}]
    return payloads


def _same_dir(a: Any, b: str) -> bool:
    return isinstance(a, str) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def run_render(ex, op: ops.RenderTimeline):
    """Executed by `Executor.execute` after validation, capability and idempotency checks."""
    from .executor import Outcome, OperationResult, _Precondition   # avoid an import cycle at module load

    spec: ops.RenderSpec = op.spec
    key = op.idempotency_key or f"op:{op.op_id}"
    a = ex.adapter
    out_path = output_path(spec)
    report: Dict[str, Any] = {"spec": spec.to_dict(), "output": out_path, "steps": [], "samples": []}

    def refused(failure: FailureClass, message: str) -> "OperationResult":
        return OperationResult(op.op_id, op.KIND, Outcome.REFUSED, failure, message, report)

    # ── preflight (nothing changes) ──
    try:
        ex._check_project()
        info = ex._timeline(op.timeline)
    except _Precondition as exc:
        return refused(exc.failure, exc.message)
    except AdapterError as exc:
        return refused(exc.failure, exc.message)
    tl_rate = info["_rate"]
    length = int(info["end_frame"]) - int(info["start_frame"])
    if length <= 0:
        return refused(FailureClass.PRECONDITION, "timeline is empty")
    if spec.mark_in is not None:
        if spec.mark_in < info["start_frame"] or spec.mark_out > info["end_frame"]:
            return refused(FailureClass.PRECONDITION,
                           f"render range [{spec.mark_in}, {spec.mark_out}) is outside the timeline "
                           f"[{info['start_frame']}, {info['end_frame']}) (absolute frames)")
        expected_frames = spec.mark_out - spec.mark_in
    else:
        expected_frames = length
    if os.path.exists(out_path):
        return refused(FailureClass.PRECONDITION, f"output already exists; refusing to overwrite {out_path}")
    try:
        if a.is_rendering():
            return refused(FailureClass.PRECONDITION, "Resolve is already rendering")
        codecs = a.render_codecs(spec.format)
    except AdapterError as exc:
        return refused(exc.failure, exc.message)
    if spec.codec not in codecs.values():
        return refused(FailureClass.UNSUPPORTED, f"codec id {spec.codec!r} is not offered for {spec.format} "
                                                 f"(offered: {sorted(codecs.values())})")
    expect = {"video": bool(spec.export_video), "video_codec": spec.codec, "width": spec.width, "height": spec.height,
              "frame_rate": str(tl_rate), "frames": expected_frames, "audio": bool(spec.export_audio)}
    if not spec.export_video:
        expect = {"video": False, "audio": True}
    report["expect"] = expect

    ex.journal.append("begin", key=key, op_id=op.op_id, kind=op.KIND, request=op.to_dict(),
                      pre={"timeline": {k: v for k, v in info.items() if k != "_rate"}}, expect=expect)

    def finish(outcome, failure: Optional[FailureClass], message: str) -> "OperationResult":
        ex.journal.append("finish", key=key, op_id=op.op_id, kind=op.KIND, outcome=outcome.value,
                          failure=failure.value if failure else None, message=message, evidence=report)
        return OperationResult(op.op_id, op.KIND, outcome, failure, message, report, 1)

    def uncertain(message: str, exc: AdapterError) -> "OperationResult":
        report["error"] = exc.to_dict()
        return OperationResult(op.op_id, op.KIND, Outcome.UNCERTAIN, FailureClass.INTERRUPTED, message, report, 1)

    page = None
    job_id: Optional[str] = None
    try:
        page = a.current_page()
        report["page_before"] = page
        # ── configure ──
        if not a.set_current_timeline(op.timeline):
            return finish(Outcome.FAILED, FailureClass.RESOLVE_REFUSED, "could not make the timeline current")
        ok = a.set_render_format_and_codec(spec.format, spec.codec)
        report["steps"].append({"format_and_codec": ok})
        if not ok:
            return finish(Outcome.FAILED, FailureClass.RESOLVE_REFUSED, f"{spec.format}/{spec.codec} rejected")
        for payload in render_settings(spec):
            ok = a.set_render_settings(payload)
            report["steps"].append({"settings": payload, "ok": ok})
            if not ok:
                return finish(Outcome.FAILED, FailureClass.RESOLVE_REFUSED, f"render setting rejected: {payload}")
        # ── queue and read the job back ──
        job_id = a.add_render_job()
        report["job_id"] = job_id
        if not job_id:
            return finish(Outcome.FAILED, FailureClass.RESOLVE_REFUSED, "AddRenderJob returned no job id")
        job = next((j for j in a.render_jobs() if str(j.get("JobId")) == job_id), None)
        report["job"] = job
        problems = []
        if job is None:
            problems.append("job not in the render queue")
        else:
            if not _same_dir(job.get("TargetDir"), spec.target_dir):
                problems.append(f"job TargetDir {job.get('TargetDir')!r}")
            if job.get("OutputFilename") != os.path.basename(out_path):
                problems.append(f"job OutputFilename {job.get('OutputFilename')!r}")
            want_in = spec.mark_in if spec.mark_in is not None else int(info["start_frame"])
            want_out = (spec.mark_out if spec.mark_out is not None else int(info["end_frame"])) - 1
            for k, want in (("MarkIn", want_in), ("MarkOut", want_out), ("FormatWidth", spec.width),
                            ("FormatHeight", spec.height), ("IsExportVideo", bool(spec.export_video)),
                            ("IsExportAudio", bool(spec.export_audio)), ("TimelineName", op.timeline)):
                if k in job and job[k] != want:
                    problems.append(f"job {k} {job[k]!r} != {want!r}")
        if problems:
            a.delete_render_job(job_id)
            return finish(Outcome.FAILED, FailureClass.READBACK_MISMATCH, "queued job differs: " + "; ".join(problems))
        # ── start and monitor ──
        started = a.start_render([job_id])
        report["steps"].append({"start": started})
        deadline = ex.clock() + spec.timeout_seconds
        status: Dict[str, Any] = {}
        while True:
            status = a.render_status(job_id)
            if len(report["samples"]) < MAX_SAMPLES:
                report["samples"].append({"pct": status.get("CompletionPercentage"), "status": status.get("JobStatus")})
            if status.get("Error"):
                report["final_status"] = status
                return finish(Outcome.FAILED, FailureClass.RENDER_FAILED, f"render error: {status.get('Error')}")
            pct = status.get("CompletionPercentage")
            if pct == 100 and not a.is_rendering():
                break
            if not started and not a.is_rendering() and not pct:
                report["final_status"] = status
                return finish(Outcome.FAILED, FailureClass.RENDER_FAILED, "StartRendering returned False and nothing rendered")
            if ex.clock() >= deadline:
                a.stop_render()
                stop_deadline = ex.clock() + 30
                while a.is_rendering() and ex.clock() < stop_deadline:
                    ex.sleep(ex.render_poll_seconds)
                report["still_rendering_after_stop"] = a.is_rendering()
                report["final_status"] = a.render_status(job_id)
                report["partial_file_exists"] = os.path.exists(out_path)
                report["job_deleted"] = a.delete_render_job(job_id)
                return finish(Outcome.FAILED, FailureClass.RENDER_TIMEOUT,
                              f"render did not finish within {spec.timeout_seconds}s; stopped")
            ex.sleep(ex.render_poll_seconds)
        report["final_status"] = status
    except AdapterError as exc:
        if exc.failure is FailureClass.BRIDGE_UNAVAILABLE and job_id is None:
            return finish(Outcome.FAILED, exc.failure, "bridge unavailable before the job was queued")
        return uncertain("bridge failed during the render; inspect the render queue and output before anything else", exc)
    finally:
        if page:
            try:
                a.open_page(page)
            except AdapterError:
                pass

    # ── verify the file ──
    try:
        media = ex.prober(out_path)
    except ProbeError as exc:
        report["verification"] = probe_report(None, [], exc.code)
        return finish(Outcome.FAILED, FailureClass.EXPORT_INVALID, f"output not verifiable: {exc}")
    problems = verify_media(media, expect)
    report["verification"] = probe_report(media, problems)
    try:
        report["job_deleted"] = a.delete_render_job(job_id)
    except AdapterError:
        report["job_deleted"] = False
    if problems:
        return finish(Outcome.FAILED, FailureClass.EXPORT_INVALID, "; ".join(problems))
    return finish(Outcome.SUCCEEDED, None, "rendered and verified")
