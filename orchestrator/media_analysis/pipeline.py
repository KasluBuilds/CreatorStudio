"""Connects media_analysis to the existing creator_state project mechanism (no new state format).

It only CALLS creator_state's public API:
  * the ingest manifest (`source_manifest.json`) stays the source of truth for originals;
  * `ingest.verify_sources` runs before any step relies on an original (mismatch -> refuse);
  * work is journaled with `Project.begin_operation / complete_operation / fail_operation` under the
    existing `understand` stage, and outputs live under `Projects/<slug>/analysis/media/`
    (inside the project folder, so `complete_stage` can verify and hash them);
  * the stage is NOT completed here: `understand` also covers transcription, which needs an engine
    that is not chosen yet. Completing it is the orchestrator's decision (`outputs()` lists the files).

Connected steps: verify originals -> analysis copy -> audio analysis -> scene detection -> transcription
(provider-dependent) -> transcript review storage -> moment index -> speaker-attribution eligibility decisions
-> Creator DNA handoff manifest (only creator-attributed, eligible records).
Not connected (needs a decision/refactor, see report): CLI commands and automatic DNA learning; the DNA
handoff here PREPARES eligible inputs and writes a manifest; ingesting them into the DNA store is a
separate explicit call (`ingest_handoff_into_dna`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Sequence

from creator_state import ingest
from creator_state.project import Project, StateError

from .adapters import DnaEligibility, DnaHandoff, SourceResolver
from .audio_analysis import analyze_wav, load_audio_analysis, save_audio_analysis
from .models import MediaAnalysisError, MediaRef, Provenance, atomic_write_json, stable_hash
from .moment_index import MomentIndex
from .proxies import AnalysisCopyRegistry
from .review import (ReviewedTranscript, TranscriptReview, apply_review, latest_review, save_review)
from .scene_detection import load_boundaries, save_boundaries
from .transcription import (TranscriptionProvider, TranscriptionRequest, load_transcript, run_transcription,
                            save_transcript)

STAGE = "understand"
HANDOFF_SCHEMA = "media_analysis.dna_handoff/1"


def workspace(project: Project) -> Path:
    return project.dir / "analysis" / "media"


def begin(project: Project) -> dict:
    """Start (or resume) the `understand` stage. Fails if an earlier stage is unfinished or it is done."""
    status = project.load_state()["stages"][STAGE]["status"]
    if status == "in_progress":
        return {"stage": STAGE, "resumed": True}
    return project.start_stage(STAGE)


def _require_running(project: Project) -> None:
    status = project.load_state()["stages"][STAGE]["status"]
    if status != "in_progress":
        raise StateError(f"stage '{STAGE}' is {status}; call pipeline.begin(project) first")


def originals(project: Project, media_ids: Iterable[str] | None = None, deep: bool = True) -> dict[str, dict]:
    """Manifest items for the requested media, after verifying their originals. Raises if any is not 'ok'."""
    manifest = ingest.load_manifest(project)
    if manifest is None:
        raise MediaAnalysisError("no source_manifest.json: run creator_state ingest first")
    items = {i["id"]: i for i in manifest["items"]}
    wanted = list(media_ids) if media_ids is not None else [i for i, it in items.items()
                                                            if it.get("media_type") in ("video", "audio")]
    unknown = [m for m in wanted if m not in items]
    if unknown:
        raise MediaAnalysisError(f"unknown media ids: {unknown}")
    report = ingest.verify_sources(project, deep=deep)
    bad = [r for r in report["rows"] if r["id"] in wanted and r["status"] != "ok"]
    if bad:
        raise MediaAnalysisError("originals failed verification, refusing to analyse: "
                                 + "; ".join(f"{r['name']}: {r['status']} ({r['detail']})" for r in bad))
    for m in wanted:
        if items[m].get("status") != "ok":
            raise MediaAnalysisError(f"{items[m]['name']}: inventory status is {items[m].get('status')!r}")
    return {m: items[m] for m in wanted}


def _run_op(project: Project, op_id: str, description: str, fn: Callable[[], tuple]):
    """Journal `fn` as an operation. `fn` returns (value, result_text). Work is non-destructive and
    idempotent, so an already-completed operation is simply re-evaluated (reusing outputs) without
    being re-journaled."""
    _require_running(project)
    verdict = project.begin_operation(STAGE, op_id, description)
    try:
        value, text = fn()
    except BaseException as exc:
        project.fail_operation(STAGE, op_id, f"{type(exc).__name__}: {exc}")
        raise
    if verdict == "started":
        project.complete_operation(STAGE, op_id, text)
    return value


def _rel(project: Project, p: Path) -> str:
    return p.resolve().relative_to(project.dir).as_posix()


def registry(project: Project, ffmpeg: str | None = None, runner=None) -> AnalysisCopyRegistry:
    kw = {"runner": runner} if runner is not None else {}
    return AnalysisCopyRegistry(workspace(project) / "copies", ffmpeg=ffmpeg, **kw)


# ------------------------------------------------------------------------- steps
def make_analysis_copy(project: Project, media_id: str, ffmpeg: str | None = None, runner=None,
                       deep_verify: bool = True) -> dict:
    item = originals(project, [media_id], deep_verify)[media_id]
    ref, reg = MediaRef.from_item(item), registry(project, ffmpeg, runner)
    plan = reg.plan(ref, "audio_wav")

    def go():
        rec, reused = reg.ensure(ref, deep=deep_verify)
        return (rec, reused), f"{rec['copy_id']} ({'reused' if reused else 'created'}) -> {rec['output']['path']}"
    rec, reused = _run_op(project, f"copy:{plan['copy_id']}", f"analysis copy (audio_wav) of {media_id}", go)
    return {"record": rec, "reused": reused, "path": reg.directory / rec["output"]["path"]}


def analyze_audio(project: Project, media_id: str, params: dict | None = None, ffmpeg: str | None = None,
                  runner=None, deep_verify: bool = True) -> dict:
    copy = make_analysis_copy(project, media_id, ffmpeg, runner, deep_verify)
    rec = copy["record"]

    def go():
        a = analyze_wav(copy["path"], media_id, params, analysis_copy_id=rec["copy_id"])
        path = save_audio_analysis(workspace(project) / "audio", a)
        return (a, path), f"{_rel(project, path)}: {len(a.gaps)} gaps, {len(a.energy_events)} energy rises"
    a, path = _run_op(project, f"audio:{media_id}:{stable_hash(params or {}, rec['copy_id'], n=8)}",
                      f"audio measurements for {media_id}", go)
    return {"analysis": a, "path": path, "copy": copy}


def detect_scenes(project: Project, media_id: str, detector, deep_verify: bool = True) -> dict:
    item = originals(project, [media_id], deep_verify)[media_id]
    ref = MediaRef.from_item(item)

    def go():
        shots = list(detector.detect(ref))
        prov = detector.provenance() if hasattr(detector, "provenance") else \
            Provenance(detector.name, detector.version, "deterministic")
        path = save_boundaries(workspace(project) / "scenes", media_id, prov, shots)
        return (shots, path), f"{_rel(project, path)}: {len(shots)} boundaries"
    shots, path = _run_op(project, f"scenes:{media_id}:{detector.name}@{detector.version}",
                          f"shot boundaries for {media_id}", go)
    return {"boundaries": shots, "path": path}


def transcribe(project: Project, media_id: str, provider: TranscriptionProvider, language: str | None = None,
               ffmpeg: str | None = None, runner=None, deep_verify: bool = True) -> dict:
    """Run a transcription provider on the analysis copy. An unavailable provider fails the journaled
    operation with CapabilityError (visible in the project state); nothing is fabricated."""
    copy = make_analysis_copy(project, media_id, ffmpeg, runner, deep_verify)
    ref = MediaRef.from_item(originals(project, [media_id], False)[media_id])

    def go():
        req = TranscriptionRequest(ref, str(copy["path"]), language, audio_copy_id=copy["record"]["copy_id"])
        t = run_transcription(provider, req)
        path = save_transcript(workspace(project) / "transcripts", t)
        return (t, path), f"{_rel(project, path)}: {len(t.segments)} segments, unverified"
    t, path = _run_op(project, f"transcribe:{media_id}:{provider.name}@{provider.version}",
                      f"transcription of {media_id} with {provider.name}", go)
    return {"transcript": t, "path": path}


def store_review(project: Project, review: TranscriptReview) -> Path:
    """Persist a human review next to (never over) the ASR transcript."""
    def go():
        path = save_review(workspace(project) / "transcripts", review)
        return path, f"{_rel(project, path)}: {review.reviewer} reviewed {len(review.items)} segment(s)"
    return _run_op(project, f"review:{review.review_id}", f"store transcript review by {review.reviewer}", go)


# ----------------------------------------------------------------- loading outputs
def _load_all(project: Project, media_ids: Sequence[str]):
    ws = workspace(project)
    transcripts, audio, shots = [], [], []
    for m in media_ids:
        for p in sorted((ws / "transcripts").glob(f"{m}.*.transcript.json")):
            t = load_transcript(p)
            transcripts.append(apply_review(t, latest_review(ws / "transcripts", t.transcript_id)))
        for p in sorted((ws / "audio").glob(f"{m}.audio-*.json")):
            audio.append(load_audio_analysis(p))
        for p in sorted((ws / "scenes").glob(f"{m}.shots-*.json")):
            prov, b = load_boundaries(p)
            shots.append((prov, b))
    return transcripts, audio, shots


def build_moment_index(project: Project, media_ids: Sequence[str] | None = None) -> Path:
    items = originals(project, media_ids, deep=False)
    transcripts, audio, shots = _load_all(project, list(items))

    def go():
        idx = MomentIndex()
        for rt in transcripts:
            idx.register_transcript(rt.original)         # the ASR output itself, verification "unverified"
        for a in audio:
            idx.register_audio(a)
        for prov, b in shots:
            idx.register_boundaries(b, prov)
        path = workspace(project) / "moments.json"
        idx.save(path)
        return path, f"{_rel(project, path)}: {len(idx.evidence)} evidence records, {len(idx.moments)} moments"
    return _run_op(project, f"moments:{len(transcripts)}-{len(audio)}-{len(shots)}", "build moment index", go)


def attribution_store(project: Project):
    """The project's speaker-attribution store (evidence + persisted eligibility decisions)."""
    from speaker_attribution import AttributionStore
    return AttributionStore(workspace(project) / "attribution")


def evaluate_eligibility(project: Project, media_ids: Sequence[str] | None = None, include_level_windows: bool = False,
                         store=None) -> list:
    """Decide Creator DNA eligibility for every analysed record (transcript segments, pauses, energy rises,
    visual detections) from the attribution evidence in the store, persist each decision (ineligible and
    requires_review ones too) and write `eligibility_report.json`. Attributes nobody by itself: with no
    attribution evidence every candidate is ineligible (speaker_unknown). Idempotent."""
    from speaker_attribution import summarize
    from speaker_attribution.handoff import (candidates_from_audio, candidates_from_transcript,
                                             candidates_from_visual)
    items = originals(project, media_ids, deep=False)
    refs = {m: MediaRef.from_item(it) for m, it in items.items()}
    transcripts, audio, shots = _load_all(project, list(items))
    store = store or attribution_store(project)

    def go():
        cands = []
        for t in transcripts:
            cands += candidates_from_transcript(t, refs)
        for a in audio:
            cands += candidates_from_audio(a, include_level_windows, refs)
        cands += candidates_from_visual([b for _, bs in shots for b in bs], refs)
        decisions = store.evaluate(cands)
        path = workspace(project) / "eligibility_report.json"
        atomic_write_json(path, {"schema": "media_analysis.eligibility_report/1",
                                 "policy": store.policy.to_dict(), "summary": summarize(decisions),
                                 "decisions": [d.to_dict() for d in decisions]})
        return decisions, f"{_rel(project, path)}: " + ", ".join(
            f"{k} {v}" for k, v in sorted(summarize(decisions)["by_decision"].items()))
    return _run_op(project, f"eligibility:{len(transcripts)}-{len(audio)}-{len(shots)}",
                   "decide Creator DNA eligibility", go)


def prepare_dna_handoff(project: Project, policy: DnaEligibility = DnaEligibility(),
                        media_ids: Sequence[str] | None = None, deep_verify: bool = False, attribution=None) -> DnaHandoff:
    """Build the Creator DNA inputs that are BOTH allowed by `policy` and attributed to the creator by an eligible
    decision, and write `dna_handoff.json` (what was included or skipped, with the reason). Learns nothing.
    Pipeline order: media analysis -> attribution -> eligibility -> (human review) -> this handoff."""
    from speaker_attribution import build_attributed_handoff, summarize
    items = originals(project, media_ids, deep_verify)
    refs = {m: MediaRef.from_item(it) for m, it in items.items()}
    transcripts, audio, shots = _load_all(project, list(items))
    resolver = SourceResolver(refs, registry(project), include_paths=policy.include_source_paths)
    store = attribution or attribution_store(project)

    def go():
        h = build_attributed_handoff(resolver, store, policy, transcripts, audio, [b for _, bs in shots for b in bs])
        path = workspace(project) / "dna_handoff.json"
        atomic_write_json(path, {"schema": HANDOFF_SCHEMA, "policy": dict(vars(policy)), **h.report.to_dict(),
                                 "eligibility_summary": summarize(store.decisions()),
                                 "counts": {"transcript_segments": len(h.transcript_segments),
                                            "audio_windows": len(h.audio_windows),
                                            "visual_observations": len(h.visual_observations)}})
        return h, f"{_rel(project, path)}: {len(h.report.included)} included, {len(h.report.skipped)} skipped"
    return _run_op(project, f"dna_handoff:{len(transcripts)}-{len(audio)}-{len(shots)}", "prepare Creator DNA handoff", go)


def ingest_handoff_into_dna(store, handoff: DnaHandoff, analyzers: Sequence, actor_prefix: str = "media_analysis") -> list:
    """Run the given DNA analyzers over the handoff and ingest their candidates into the DNA store.
    Explicit and caller-chosen: the pipeline never learns on its own. Idempotent (DNA dedups evidence)."""
    import creator_dna as dna
    reports = []
    for an in analyzers:
        actor = dna.inputs.actor_name(an)
        if isinstance(an, dna.inputs.TranscriptAnalyzer):
            cands = an.analyze_transcript(handoff.transcript_segments)
        elif isinstance(an, dna.inputs.AudioAnalyzer):
            cands = an.analyze_audio(handoff.audio_windows)
        elif isinstance(an, dna.inputs.VisualAnalyzer):
            cands = an.analyze_visual(handoff.visual_observations)
        else:
            raise MediaAnalysisError(f"{an!r} is not a DNA analyzer")
        reports.append(store.ingest(cands, f"{actor_prefix}/{actor}",
                                    transcript=handoff.transcript_segments or None))
    return reports


def outputs(project: Project) -> list[str]:
    """Project-relative paths of existing analysis outputs (for `complete_stage`, when the orchestrator decides)."""
    ws = workspace(project)
    if not ws.exists():
        return []
    return sorted(_rel(project, p) for p in ws.rglob("*") if p.is_file() and not p.name.endswith(".part"))
