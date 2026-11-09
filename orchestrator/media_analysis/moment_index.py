"""Evidence-linked moment index.

A *moment* is a time range on one recording plus the evidence found there. The model keeps
three things apart:

  Evidence       records produced by engines: transcript segments, audio gaps, energy rises,
                 shot boundaries... each with provenance (which engine, which settings).
  Moment         a time range pointing at >= 1 evidence records. Its `moment_type` is
                 FACTUAL ONLY (spoken_statement, pause, scene_change, ...). "composite" means
                 "these records co-occur in time" and says nothing about what it means.
  Interpretation a subjective reading ("joke", "reaction", "awkward pause") with an author
                 ("ai:<model>" or "human:<name>"), a rationale and the evidence it rests on.
                 Interpretations are stored beside moments, never as their type, and nothing in
                 this package creates one automatically.

Moment ids derive from (media, type, range, evidence ids), so adding the same thing twice is
a no-op (duplicate detection); several interpretations may attach to one moment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .audio_analysis import AudioAnalysis
from .models import (MediaAnalysisError, Provenance, RecordKind, atomic_write_json, check_range,
                     check_unit, read_json, stable_hash, t_key, utc_now)
from .scene_detection import ShotBoundary, VisualEvent
from .transcription import Transcript

SCHEMA = "media_analysis.moments/1"

# evidence kind -> (record kind, moment type it yields on its own)
EVIDENCE_KINDS = {
    "transcript_segment": (RecordKind.TRANSCRIPTION, "spoken_statement"),
    "audio_gap": (RecordKind.MEASUREMENT, "pause"),
    "energy_event": (RecordKind.MEASUREMENT, "energy_rise"),
    "speech_region": (RecordKind.MEASUREMENT, "speech_region"),
    "shot_boundary": (RecordKind.DETECTION, "scene_change"),
    "visual_event": (RecordKind.DETECTION, "visual_event"),
}
FACTUAL_TYPES = frozenset(t for _, t in EVIDENCE_KINDS.values()) | {"composite", "candidate"}
# "candidate" = a range someone (usually an AI agent) nominated; carries >= 1 interpretation.


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    record_id: str
    media_id: str
    start_s: float
    end_s: float
    source: str                     # "engine@version"

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise MediaAnalysisError(f"unknown evidence kind {self.kind!r}")
        check_range(self.start_s, self.end_s)

    @property
    def record_kind(self) -> RecordKind:
        return EVIDENCE_KINDS[self.kind][0]

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class Interpretation:
    label: str                      # free vocabulary: "joke", "reaction", "awkward_pause", ...
    author: str                     # "ai:<model>" | "human:<name>"
    rationale: str
    confidence: float | None = None  # the author's own; not computed here
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.rationale.strip():
            raise MediaAnalysisError("an interpretation needs a label and a rationale")
        if not (self.author.startswith("ai:") or self.author.startswith("human:")) or self.author.split(":", 1)[1] == "":
            raise MediaAnalysisError("author must look like 'ai:<model>' or 'human:<name>'")
        check_unit(self.confidence, "confidence")

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class Moment:
    media_id: str
    start_s: float
    end_s: float
    moment_type: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        check_range(self.start_s, self.end_s)
        if self.moment_type not in FACTUAL_TYPES:
            raise MediaAnalysisError(
                f"{self.moment_type!r} is not a factual moment type; subjective labels must be Interpretations")
        if not self.evidence_ids:
            raise MediaAnalysisError("a moment needs at least one evidence record")
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))

    @property
    def moment_id(self) -> str:
        return "mom_" + stable_hash(self.media_id, self.moment_type, t_key(self.start_s), t_key(self.end_s),
                                    self.evidence_ids)

    def to_dict(self) -> dict:
        return {"moment_id": self.moment_id, "media_id": self.media_id, "start_s": self.start_s,
                "end_s": self.end_s, "moment_type": self.moment_type, "evidence_ids": list(self.evidence_ids)}


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


class MomentIndex:
    def __init__(self) -> None:
        self.evidence: dict[str, EvidenceRef] = {}
        self.payloads: dict[str, dict] = {}
        self.moments: dict[str, Moment] = {}
        self.interpretations: dict[str, list[Interpretation]] = {}
        self.duplicates_skipped = 0

    # ------------------------------------------------------------ registration
    def _register(self, ref: EvidenceRef, payload: dict) -> EvidenceRef:
        if ref.record_id in self.evidence:
            self.duplicates_skipped += 1
            return self.evidence[ref.record_id]
        self.evidence[ref.record_id] = ref
        self.payloads[ref.record_id] = payload
        self.add_moment(Moment(ref.media_id, ref.start_s, ref.end_s, EVIDENCE_KINDS[ref.kind][1], (ref.record_id,)))
        return ref

    def register_transcript(self, t: Transcript) -> list[EvidenceRef]:
        src = f"{t.provenance.engine}@{t.provenance.version}"
        return [self._register(EvidenceRef("transcript_segment", s.record_id, s.media_id, s.start_s, s.end_s, src),
                               {**s.to_dict(), "verification": t.verification}) for s in t.segments]

    def register_audio(self, a: AudioAnalysis) -> list[EvidenceRef]:
        src = f"{a.provenance.engine}@{a.provenance.version}"
        refs = []
        for g in a.gaps:
            refs.append(self._register(EvidenceRef("audio_gap", g.record_id, a.media_id, g.start_s, g.end_s, src),
                                       {**vars(g), "duration_s": g.duration_s}))
        for e in a.energy_events:
            refs.append(self._register(EvidenceRef("energy_event", e.record_id, a.media_id, e.start_s, e.end_s, src),
                                       dict(vars(e))))
        for s in a.speech_regions:
            refs.append(self._register(EvidenceRef("speech_region", s.record_id, a.media_id, s.start_s, s.end_s, src),
                                       dict(vars(s))))
        return refs

    def register_boundaries(self, boundaries: Iterable[ShotBoundary], provenance: Provenance) -> list[EvidenceRef]:
        src = f"{provenance.engine}@{provenance.version}"
        return [self._register(EvidenceRef("shot_boundary", b.record_id, b.media_id, b.time_s, b.time_s, src),
                               b.to_dict()) for b in boundaries]

    def register_visual_events(self, events: Iterable[VisualEvent], provenance: Provenance) -> list[EvidenceRef]:
        src = f"{provenance.engine}@{provenance.version}"
        return [self._register(EvidenceRef("visual_event", v.record_id, v.media_id, v.start_s, v.end_s, src),
                               {k: (list(map(vars, x)) if k == "frame_refs" else x) for k, x in vars(v).items()})
                for v in events]

    # ----------------------------------------------------------------- moments
    def add_moment(self, m: Moment) -> tuple[Moment, bool]:
        """Returns (moment, created). An identical moment is not added twice."""
        for eid in m.evidence_ids:
            ref = self.evidence.get(eid)
            if ref is None:
                raise MediaAnalysisError(f"unknown evidence record {eid}")
            if ref.media_id != m.media_id:
                raise MediaAnalysisError("evidence belongs to a different recording than the moment")
        if m.moment_id in self.moments:
            self.duplicates_skipped += 1
            return self.moments[m.moment_id], False
        self.moments[m.moment_id] = m
        return m, True

    def correlate(self, window_s: float = 1.0, anchor_kinds: Sequence[str] = ("transcript_segment",)) -> list[Moment]:
        """Group records that co-occur with each anchor record (within `window_s` of its range) into a
        'composite' moment. Purely temporal: no meaning is implied."""
        created = []
        for a in [r for r in self.evidence.values() if r.kind in anchor_kinds]:
            near = [r for r in self.evidence.values()
                    if r.media_id == a.media_id and r.record_id != a.record_id
                    and r.start_s <= a.end_s + window_s and r.end_s >= a.start_s - window_s]
            if not near:
                continue
            group = [a] + near
            m, new = self.add_moment(Moment(a.media_id, min(r.start_s for r in group), max(r.end_s for r in group),
                                            "composite", tuple(r.record_id for r in group)))
            if new:
                created.append(m)
        return created

    def add_candidate(self, media_id: str, evidence_ids: Sequence[str], interpretation: Interpretation,
                      start_s: float | None = None, end_s: float | None = None) -> Moment:
        """Nominate a range as something (e.g. 'joke'). Requires evidence and an author; the label is stored
        as an interpretation, the moment itself stays factual ('candidate')."""
        refs = [self.evidence[e] for e in evidence_ids if e in self.evidence]
        if len(refs) != len(set(evidence_ids)) or not refs:
            raise MediaAnalysisError("candidate must cite existing evidence records")
        s = min(r.start_s for r in refs) if start_s is None else start_s
        e = max(r.end_s for r in refs) if end_s is None else end_s
        m, _ = self.add_moment(Moment(media_id, s, e, "candidate", tuple(evidence_ids)))
        self.interpret(m.moment_id, interpretation)
        return m

    def interpret(self, moment_id: str, interp: Interpretation) -> bool:
        if moment_id not in self.moments:
            raise MediaAnalysisError(f"unknown moment {moment_id}")
        lst = self.interpretations.setdefault(moment_id, [])
        if any((i.label, i.author) == (interp.label, interp.author) for i in lst):
            self.duplicates_skipped += 1
            return False
        lst.append(interp)
        return True

    # ------------------------------------------------------------------ queries
    def query(self, media_id: str | None = None, moment_type: str | None = None,
              start_s: float | None = None, end_s: float | None = None,
              evidence_kind: str | None = None, label: str | None = None) -> list[Moment]:
        out = []
        for m in self.moments.values():
            if media_id and m.media_id != media_id:
                continue
            if moment_type and m.moment_type != moment_type:
                continue
            if start_s is not None and m.end_s < start_s:
                continue
            if end_s is not None and m.start_s > end_s:
                continue
            if evidence_kind and not any(self.evidence[e].kind == evidence_kind for e in m.evidence_ids):
                continue
            if label and not any(i.label == label for i in self.interpretations.get(m.moment_id, [])):
                continue
            out.append(m)
        return sorted(out, key=lambda m: (m.media_id, m.start_s, m.end_s, m.moment_id))

    def at(self, media_id: str, t: float) -> list[Moment]:
        return self.query(media_id, start_s=t, end_s=t)

    def resolve(self, moment_id: str) -> dict:
        """Moment with its evidence payloads (measurements/transcriptions/detections) and,
        separately, any interpretations."""
        m = self.moments[moment_id]
        return {**m.to_dict(),
                "evidence": [{**self.evidence[e].to_dict(), "record_kind": self.evidence[e].record_kind.value,
                              "payload": self.payloads[e]} for e in m.evidence_ids],
                "interpretations": [i.to_dict() for i in self.interpretations.get(moment_id, [])]}

    def repeated_statements(self, min_chars: int = 12) -> list[list[str]]:
        """Groups of transcript record ids whose text is identical (case/space-insensitive) at
        different times - candidates for retakes or repeated material. A textual fact only."""
        groups: dict[tuple, list[str]] = {}
        for rid, ref in self.evidence.items():
            if ref.kind == "transcript_segment":
                text = _norm(self.payloads[rid]["text"])
                if len(text) >= min_chars:
                    groups.setdefault((ref.media_id, text), []).append(rid)
        return sorted(sorted(g) for g in groups.values() if len(g) > 1)

    # -------------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        return {"schema": SCHEMA,
                "evidence": [{**r.to_dict(), "payload": self.payloads[r.record_id]} for r in self.evidence.values()],
                "moments": [m.to_dict() for m in self.moments.values()],
                "interpretations": {k: [i.to_dict() for i in v] for k, v in self.interpretations.items()}}

    def save(self, path: str | os.PathLike) -> None:
        atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | os.PathLike) -> "MomentIndex":
        d = read_json(path)
        if d.get("schema") != SCHEMA:
            raise MediaAnalysisError(f"unrecognised moment index schema {d.get('schema')!r}")
        idx = cls()
        for e in d["evidence"]:
            payload = e.pop("payload")
            idx.evidence[e["record_id"]] = EvidenceRef(**e)
            idx.payloads[e["record_id"]] = payload
        for m in d["moments"]:
            m = dict(m)
            mid = m.pop("moment_id")
            mom = Moment(m["media_id"], m["start_s"], m["end_s"], m["moment_type"], tuple(m["evidence_ids"]))
            if mom.moment_id != mid:
                raise MediaAnalysisError(f"moment id mismatch for {mid} (index edited or corrupt)")
            idx.moments[mid] = mom
        for k, v in d["interpretations"].items():
            if k not in idx.moments:
                raise MediaAnalysisError(f"interpretation for unknown moment {k}")
            idx.interpretations[k] = [Interpretation(**i) for i in v]
        return idx
