"""Append-only Creator DNA store.

The source of truth is one JSONL event log (`events.jsonl`). Nothing in it is ever
rewritten or deleted; the profile is rebuilt by replaying the events, and
confidence is derived on read (confidence.py). Learning, creator feedback and
corrections are all just more events.

Single writer assumed. A size check before each append refuses to write if the
log changed behind this process's back.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import confidence as conf
from .inputs import TranscriptSegment, quote_in_transcript
from .schema import (LAYER_METHODS, OBSERVED_METHODS, SCHEMA_VERSION, Category, DNAError, Evidence, Layer,
                     Method, Modality, Polarity, Scope, Status, TraitCandidate, normalize_text,
                     trait_id_for)

LOG_NAME = "events.jsonl"

# Events written before schema v4 recorded an attribution revocation as an ordinary creator
# `evidence_review/reject` whose note started with this text. That is ambiguous (a creator could type
# the same words), so it only ever raises a read-only warning flag; it is never migrated.
LEGACY_REVOCATION_NOTE_PREFIX = "attribution revoked ("
REVIEW_ACTIONS = ("reject", "restore")


class DNALogError(DNAError):
    """The event log is corrupt or inconsistent."""


@dataclass
class EvidenceItem:
    evidence: Evidence
    evidence_id: str
    added_by: str
    added_at: str
    creator_rejected: bool = False               # the creator rejected this example (restorable by the creator)
    review_note: str = ""                        # note of the latest review or revocation event
    revoked_decision_id: str | None = None       # set once the upstream attribution was revoked (not restorable)
    revocation_reason: str = ""
    # Read-only warnings for replayed pre-v4 logs; never used to change what counts.
    legacy_possible_revocation: bool = False             # pre-v4 reject whose note looks like a revocation
    legacy_restored_after_possible_revocation: bool = False

    @property
    def attribution_revoked(self) -> bool:
        return self.revoked_decision_id is not None

    @property
    def rejected(self) -> bool:
        """Compatibility: excluded from confidence for either reason."""
        return self.creator_rejected or self.attribution_revoked


@dataclass
class TraitState:
    trait_id: str
    category: Category
    statement: str
    layer: Layer
    created_by: str
    created_at: str
    based_on: tuple[str, ...] = ()
    status: Status = Status.ACTIVE
    creator_authored: bool = False
    creator_scope: Scope | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    evidence_items: list[EvidenceItem] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)   # short audit trail of non-evidence changes

    def assessment(self, cfg: conf.ScoringConfig = conf.DEFAULT_CONFIG) -> conf.Assessment:
        return conf.assess(self, cfg)

    def to_dict(self, cfg: conf.ScoringConfig = conf.DEFAULT_CONFIG) -> dict:
        return {
            "trait_id": self.trait_id, "category": self.category.value,
            "statement": self.statement, "layer": self.layer.value, "status": self.status.value,
            "creator_authored": self.creator_authored, "based_on": list(self.based_on),
            "supersedes": self.supersedes, "superseded_by": self.superseded_by,
            "assessment": self.assessment(cfg).to_dict(),
            "support": [_item_dict(i) for i in self.evidence_items if i.evidence.polarity is Polarity.SUPPORT],
            "counterexamples": [_item_dict(i) for i in self.evidence_items if i.evidence.polarity is Polarity.COUNTER],
            "history": list(self.history),
        }


def _item_dict(i: EvidenceItem) -> dict:
    d = i.evidence.to_dict()
    d.update(evidence_id=i.evidence_id, added_by=i.added_by, added_at=i.added_at,
             rejected_by_creator=i.creator_rejected, review_note=i.review_note,
             attribution_revoked=i.attribution_revoked, revocation_decision_id=i.revoked_decision_id,
             revocation_reason=i.revocation_reason,
             legacy_possible_revocation=i.legacy_possible_revocation,
             legacy_restored_after_possible_revocation=i.legacy_restored_after_possible_revocation)
    return d


@dataclass
class IngestReport:
    traits_created: list[str] = field(default_factory=list)
    evidence_added: int = 0
    duplicates_skipped: int = 0
    refused: list[dict] = field(default_factory=list)    # {"statement","reason"}
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"traits_created": self.traits_created, "evidence_added": self.evidence_added,
                "duplicates_skipped": self.duplicates_skipped, "refused": self.refused, "notes": self.notes}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DNAStore:
    def __init__(self, directory: str | os.PathLike, clock: Callable[[], str] = _utc_now):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.directory / LOG_NAME
        self._clock = clock
        self.traits: dict[str, TraitState] = {}
        self._seq = 0
        self._size = 0
        self._load()

    # ------------------------------------------------------------------ log
    def _load(self) -> None:
        if not self.log_path.exists():
            return
        raw = self.log_path.read_bytes()
        self._size = len(raw)
        if raw and not raw.endswith(b"\n"):
            raise DNALogError(f"{self.log_path}: last line is incomplete (interrupted write); "
                              "inspect the file manually - it is not modified automatically")
        for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DNALogError(f"{self.log_path}:{n}: invalid JSON ({exc})") from exc
            if ev.get("seq") != self._seq + 1:
                raise DNALogError(f"{self.log_path}:{n}: expected seq {self._seq + 1}, found {ev.get('seq')}")
            if ev.get("v", 0) > SCHEMA_VERSION:
                raise DNALogError(f"{self.log_path}:{n}: written by a newer schema version {ev['v']}")
            try:
                self._apply(ev)
            except (KeyError, ValueError) as exc:
                raise DNALogError(f"{self.log_path}:{n}: event cannot be applied ({exc!r})") from exc
            self._seq = ev["seq"]

    def _emit(self, type_: str, actor: str, data: dict) -> dict:
        if not actor or not actor.strip():
            raise DNAError("every event needs an actor")
        actual = self.log_path.stat().st_size if self.log_path.exists() else 0
        if actual != self._size:
            raise DNALogError("event log changed since it was loaded (another writer?); reopen the store")
        ev = {"v": SCHEMA_VERSION, "seq": self._seq + 1, "ts": self._clock(),
              "type": type_, "actor": actor, "data": data}
        line = (json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self._apply(ev)    # apply first: validation errors must not reach the log
        with open(self.log_path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._seq += 1
        self._size = actual + len(line)
        return ev

    # ---------------------------------------------------------------- replay
    def _apply(self, ev: dict) -> None:
        t, d, actor, ts = ev["type"], ev["data"], ev["actor"], ev["ts"]
        if t == "trait_proposed":
            tid = d["trait_id"]
            if tid in self.traits:
                raise ValueError(f"trait {tid} already exists")
            self.traits[tid] = TraitState(
                trait_id=tid, category=Category(d["category"]), statement=d["statement"],
                layer=Layer(d["layer"]), created_by=actor, created_at=ts,
                based_on=tuple(d.get("based_on", ())))
        elif t == "evidence_added":
            self._add_item(self._trait(d["trait_id"]), Evidence.from_dict(d["evidence"]), actor, ts)
        elif t == "evidence_review":
            self._apply_review(ev, d)
        elif t == "attribution_revoked":
            self._apply_revocation(d)
        elif t == "layer_changed":
            tr = self._trait(d["trait_id"])
            tr.history.append({"ts": ts, "actor": actor, "change": f"layer {tr.layer.value} -> {d['layer']}",
                               "reason": d.get("reason", "")})
            tr.layer = Layer(d["layer"])
        elif t == "creator_feedback":
            self._apply_feedback(d, actor, ts)
        else:
            raise ValueError(f"unknown event type {t!r}")

    def _apply_review(self, ev: dict, d: dict) -> None:
        """Creator review of one example. Only `reject` and `restore` exist; anything else is refused
        (it used to be read as a restore). Restore clears the creator's rejection only: it never touches
        an attribution revocation."""
        action = d["action"]
        if action not in REVIEW_ACTIONS:
            raise ValueError(f"unknown evidence_review action {action!r}")
        item = self._item(self._trait(d["trait_id"]), d["evidence_id"])
        note = d.get("note", "")
        legacy = ev.get("v", 0) < 4
        if action == "reject":
            item.creator_rejected = True
            if legacy and note.startswith(LEGACY_REVOCATION_NOTE_PREFIX):
                item.legacy_possible_revocation = True     # warning only; replayed exactly as written
        else:
            if legacy and item.legacy_possible_revocation and item.creator_rejected:
                item.legacy_restored_after_possible_revocation = True
            item.creator_rejected = False
        item.review_note = note

    def _apply_revocation(self, d: dict) -> None:
        item = self._item(self._trait(d["trait_id"]), d["evidence_id"])
        decision_id, reason = d["decision_id"], d["reason"]
        a = item.evidence.attribution
        if a is None or a.decision_id != decision_id:
            raise ValueError(f"evidence {item.evidence_id} is not stamped with decision {decision_id!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("an attribution revocation needs a reason")
        if item.attribution_revoked:
            raise ValueError(f"evidence {item.evidence_id} is already revoked")
        item.revoked_decision_id = decision_id
        item.revocation_reason = reason
        item.review_note = f"attribution revoked ({decision_id}): {reason}"

    def _apply_feedback(self, d: dict, actor: str, ts: str) -> None:
        tr = self._trait(d["trait_id"])
        action = d["action"]
        note = d.get("note", "")
        tr.history.append({"ts": ts, "actor": actor, "change": f"creator {action}", "reason": note})
        if action == "confirm":
            tr.status = Status.CONFIRMED
            tr.creator_scope = Scope(d["scope"]) if d.get("scope") else tr.creator_scope
        elif action == "reject":
            tr.status = Status.REJECTED
        elif action == "reopen":
            tr.status = Status.ACTIVE
        elif action == "correct":
            nd = d["new_trait"]
            if nd["trait_id"] in self.traits:
                raise ValueError("correction target already exists")
            new = TraitState(
                trait_id=nd["trait_id"], category=Category(nd["category"]), statement=nd["statement"],
                layer=Layer(nd["layer"]), created_by=actor, created_at=ts, status=Status.CONFIRMED,
                creator_authored=True, supersedes=tr.trait_id)
            self.traits[new.trait_id] = new
            self._add_item(new, Evidence.from_dict(nd["evidence"]), actor, ts)
            tr.status = Status.CORRECTED
            tr.superseded_by = new.trait_id
        else:
            raise ValueError(f"unknown feedback action {action!r}")

    def _add_item(self, tr: TraitState, ev: Evidence, actor: str, ts: str) -> None:
        eid = ev.evidence_id
        if any(i.evidence_id == eid for i in tr.evidence_items):
            raise ValueError(f"duplicate evidence {eid}")
        tr.evidence_items.append(EvidenceItem(ev, eid, actor, ts))

    def _trait(self, tid: str) -> TraitState:
        try:
            return self.traits[tid]
        except KeyError:
            raise DNAError(f"unknown trait {tid}") from None

    @staticmethod
    def _item(tr: TraitState, eid: str) -> EvidenceItem:
        for i in tr.evidence_items:
            if i.evidence_id == eid:
                return i
        raise DNAError(f"trait {tr.trait_id} has no evidence {eid}")

    # ----------------------------------------------------------- validation
    def _check_attribution(self, ev: Evidence) -> None:
        """Footage-derived evidence must carry an eligible-creator attribution stamp. Evidence about
        someone else, an unknown person, overlapping speakers or playback has no valid stamp, so it
        cannot enter the profile merely because an analyzer produced it."""
        if ev.method is Method.CREATOR_STATEMENT:
            return
        if ev.attribution is None:
            raise DNAError("unattributed footage evidence refused: no eligible creator attribution "
                           "(speaker/visual-subject attribution is required before Creator DNA learning)")

    @staticmethod
    def _check_layer_vs_evidence(layer: Layer, ev: Evidence) -> None:
        if ev.method is Method.CREATOR_STATEMENT and layer is not Layer.SELF_REPORT:
            return          # a remark by the creator on a footage trait: kept apart in scoring
        if ev.method not in LAYER_METHODS[layer]:
            hint = {Layer.OBSERVED: "an observed fact needs footage-derived evidence (reviewed quote or measurement); "
                                    "AI estimates and unreviewed ASR make it an interpretation or hypothesis",
                    Layer.SELF_REPORT: "a self-report trait rests only on the creator's own statements"}.get(layer, "")
            raise DNAError(f"{ev.method.value} evidence is not allowed on a {layer.value} trait; {hint}".rstrip("; "))

    def _check_based_on(self, layer: Layer, based_on: Sequence[str]) -> None:
        if based_on and layer is Layer.OBSERVED:
            raise DNAError("only interpretations/hypotheses can be based_on other traits")
        for b in based_on:
            if self._trait(b).layer is not Layer.OBSERVED:
                raise DNAError(f"based_on {b} is not an observed trait")

    # ------------------------------------------------------------ learning API
    def propose_trait(self, category: Category, statement: str, layer: Layer, actor: str,
                      based_on: Sequence[str] = ()) -> str:
        """Create a trait (idempotent: the same category+statement returns the existing id)."""
        if not statement.strip():
            raise DNAError("empty statement")
        tid = trait_id_for(category, statement)
        if tid in self.traits:
            return tid
        self._check_based_on(layer, based_on)
        self._emit("trait_proposed", actor, {
            "trait_id": tid, "category": category.value, "statement": statement.strip(),
            "layer": layer.value, "based_on": list(based_on)})
        return tid

    def add_evidence(self, trait_id: str, evidence: Evidence, actor: str) -> str | None:
        """Append evidence. Returns the evidence id, or None if identical evidence exists."""
        tr = self._trait(trait_id)
        self._check_attribution(evidence)
        self._check_layer_vs_evidence(tr.layer, evidence)
        if any(i.evidence_id == evidence.evidence_id for i in tr.evidence_items):
            return None
        self._emit("evidence_added", actor, {"trait_id": trait_id, "evidence": evidence.to_dict()})
        return evidence.evidence_id

    def ingest(self, candidates: Iterable[TraitCandidate], actor: str,
               transcript: Sequence[TranscriptSegment] | None = None) -> IngestReport:
        """Incremental learning. Merges candidates into existing traits by
        (category, statement); never overwrites; skips duplicate evidence; will not
        resurrect creator-rejected traits (evidence is still recorded, status stays).
        With `transcript`, verbatim quotes that cannot be found in it are refused."""
        rep = IngestReport()
        for c in candidates:
            tid = trait_id_for(c.category, c.statement)
            try:
                self._ingest_one(c, tid, actor, transcript, rep)
            except DNAError as exc:
                rep.refused.append({"statement": c.statement, "reason": str(exc)})
        return rep

    def _ingest_one(self, c, tid, actor, transcript, rep: IngestReport) -> None:
        good = []
        for ev in c.evidence:
            self._check_attribution(ev)
            self._check_layer_vs_evidence(self.traits[tid].layer if tid in self.traits else c.layer, ev)
            if transcript is not None and ev.method in (Method.VERBATIM, Method.ASR_UNVERIFIED) \
                    and not quote_in_transcript(ev, transcript):
                why = "quote not found" if ev.method is Method.ASR_UNVERIFIED else \
                    "quote not found in a transcript segment reviewed by the same reviewer"
                rep.refused.append({"statement": c.statement,
                                    "reason": f"{why} at {ev.source_id} {ev.start_s}: {ev.quote!r}"})
                continue
            good.append(ev)
        existing = self.traits.get(tid)
        if existing is None:
            if not any(e.polarity is Polarity.SUPPORT for e in good):
                raise DNAError("a new trait needs at least one supporting (non-refused) evidence item")
            self.propose_trait(c.category, c.statement, c.layer, actor, c.based_on)
            rep.traits_created.append(tid)
        else:
            if existing.layer is not c.layer:
                rep.notes.append(f"{tid}: candidate layer {c.layer.value} ignored, trait stays {existing.layer.value}")
            if existing.status is Status.REJECTED:
                rep.notes.append(f"{tid}: creator-rejected; evidence recorded but trait stays rejected")
        for ev in good:
            if self.add_evidence(tid, ev, actor) is None:
                rep.duplicates_skipped += 1
            else:
                rep.evidence_added += 1

    def reclassify(self, trait_id: str, layer: Layer, actor: str, reason: str) -> None:
        """Move a trait between layers (e.g. hypothesis -> interpretation after review)."""
        tr = self._trait(trait_id)
        if not reason.strip():
            raise DNAError("reclassify needs a reason")
        if Layer.SELF_REPORT in (layer, tr.layer) and layer is not tr.layer:
            raise DNAError("self-report traits come only from creator corrections; they cannot be reclassified")
        if layer is Layer.OBSERVED and any(
                i.evidence.method not in OBSERVED_METHODS and i.evidence.method is not Method.CREATOR_STATEMENT
                for i in tr.evidence_items):
            raise DNAError("cannot call a trait observed while it has AI-estimate or unreviewed-ASR evidence")
        if layer is Layer.OBSERVED and tr.based_on:
            raise DNAError("a trait based on other traits is an interpretation, not an observation")
        if layer is not tr.layer:
            self._emit("layer_changed", actor, {"trait_id": trait_id, "layer": layer.value, "reason": reason})

    # ------------------------------------------------------- creator feedback
    def confirm(self, trait_id: str, note: str = "", scope: Scope | None = None, actor: str = "creator") -> None:
        self._trait(trait_id)
        self._emit("creator_feedback", actor, {"trait_id": trait_id, "action": "confirm", "note": note,
                                               "scope": scope.value if scope else None})

    def reject(self, trait_id: str, note: str = "", actor: str = "creator") -> None:
        self._trait(trait_id)
        self._emit("creator_feedback", actor, {"trait_id": trait_id, "action": "reject", "note": note})

    def reopen(self, trait_id: str, note: str = "", actor: str = "creator") -> None:
        tr = self._trait(trait_id)
        if tr.status not in (Status.REJECTED, Status.CONFIRMED):
            raise DNAError("only rejected or confirmed traits can be reopened")
        self._emit("creator_feedback", actor, {"trait_id": trait_id, "action": "reopen", "note": note})

    def correct(self, trait_id: str, new_statement: str, note: str, category: Category | None = None,
                layer: Layer = Layer.SELF_REPORT, actor: str = "creator") -> str:
        """Creator replaces a trait with their own wording. The old trait and its
        evidence are kept (status 'corrected'); the new trait is creator-authored.
        `note` is required: it is the creator's statement and becomes its evidence."""
        old = self._trait(trait_id)
        if old.status is Status.CORRECTED:
            raise DNAError("trait was already corrected; correct its successor instead")
        if layer is not Layer.SELF_REPORT:
            raise DNAError("a creator correction is a self-report, not footage evidence")
        if not new_statement.strip() or not note.strip():
            raise DNAError("a correction needs the new statement and the creator's note")
        cat = category or old.category
        new_id = trait_id_for(cat, new_statement)
        if new_id in self.traits:
            raise DNAError(f"a trait with that statement already exists ({new_id}); confirm it instead")
        ev = Evidence(source_id="creator-feedback", modality=Modality.FEEDBACK,
                      method=Method.CREATOR_STATEMENT, note=note, strength=1.0)
        self._emit("creator_feedback", actor, {
            "trait_id": trait_id, "action": "correct", "note": note,
            "new_trait": {"trait_id": new_id, "category": cat.value, "statement": new_statement.strip(),
                          "layer": layer.value, "evidence": ev.to_dict()}})
        return new_id

    def revoke_attribution(self, decision_id: str, reason: str, actor: str) -> list[tuple[str, str]]:
        """An upstream attribution decision was withdrawn or corrected (e.g. the speaker turned out not to be
        the creator). Every evidence item stamped with that decision stops counting; nothing is deleted.
        The revocation is permanent: restore_evidence cannot undo it. Returns (trait_id, evidence_id)
        pairs newly revoked by this call."""
        if not reason.strip():
            raise DNAError("revoking an attribution needs a reason")
        out = []
        for tr in self.traits.values():
            for item in tr.evidence_items:
                a = item.evidence.attribution
                # Recorded even when the creator already rejected the item; skipped only when this
                # decision's revocation is already on record (idempotent).
                if a is not None and a.decision_id == decision_id and not item.attribution_revoked:
                    self._emit("attribution_revoked", actor, {
                        "trait_id": tr.trait_id, "evidence_id": item.evidence_id,
                        "decision_id": decision_id, "reason": reason})
                    out.append((tr.trait_id, item.evidence_id))
        return out

    def reject_evidence(self, trait_id: str, evidence_id: str, note: str, actor: str = "creator") -> None:
        """Creator says one example is wrong (e.g. 'that was not a joke'). The item is
        kept for the record but no longer counts toward confidence."""
        self._item(self._trait(trait_id), evidence_id)
        self._emit("evidence_review", actor, {"trait_id": trait_id, "evidence_id": evidence_id,
                                              "action": "reject", "note": note})

    def restore_evidence(self, trait_id: str, evidence_id: str, note: str = "", actor: str = "creator") -> None:
        """Undo the creator's rejection of one example. Refused when the example is excluded by a revoked
        attribution, or when an older log holds a reject that may have been a revocation."""
        item = self._item(self._trait(trait_id), evidence_id)
        if item.attribution_revoked:
            raise DNAError(f"evidence {evidence_id} is excluded by a revoked attribution "
                           f"({item.revoked_decision_id}); restoring cannot undo that")
        if item.legacy_possible_revocation:
            raise DNAError(f"evidence {evidence_id} was rejected by a pre-v4 event that may have been an "
                           "attribution revocation; it cannot be restored until that is resolved")
        self._emit("evidence_review", actor, {"trait_id": trait_id, "evidence_id": evidence_id,
                                              "action": "restore", "note": note})

    def add_creator_statement(self, trait_id: str, note: str, polarity: Polarity = Polarity.SUPPORT,
                              actor: str = "creator") -> str | None:
        """Creator remarks on a trait in their own words, supporting it or as a counterexample."""
        ev = Evidence(source_id="creator-feedback", modality=Modality.FEEDBACK,
                      method=Method.CREATOR_STATEMENT, polarity=polarity, note=note)
        return self.add_evidence(trait_id, ev, actor)

    # ---------------------------------------------------------------- reading
    def legacy_warnings(self) -> list[dict]:
        """Read-only report of ambiguous pre-v4 history. Nothing here changes what counts."""
        out = []
        for tr in self.traits.values():
            for i in tr.evidence_items:
                if i.legacy_possible_revocation:
                    out.append({"trait_id": tr.trait_id, "evidence_id": i.evidence_id,
                                "warning": "legacy_possible_revocation",
                                "restored_since": i.legacy_restored_after_possible_revocation,
                                "currently_excluded": i.rejected})
        return out

    def get(self, trait_id: str) -> TraitState:
        return self._trait(trait_id)

    def find(self, category: Category, statement: str) -> TraitState | None:
        return self.traits.get(trait_id_for(category, statement))

    def active_traits(self, min_confidence: float = 0.0, layers: Iterable[Layer] | None = None,
                      scopes: Iterable[Scope] | None = None,
                      cfg: conf.ScoringConfig = conf.DEFAULT_CONFIG) -> list[TraitState]:
        """Traits the rest of the system may use: not rejected, not superseded."""
        layers, scopes = set(layers) if layers else None, set(scopes) if scopes else None
        out = []
        for t in self.traits.values():
            if t.status not in (Status.ACTIVE, Status.CONFIRMED):
                continue
            a = t.assessment(cfg)
            if a.confidence < min_confidence or (layers and t.layer not in layers) or (scopes and a.scope not in scopes):
                continue
            out.append(t)
        out.sort(key=lambda t: (-t.assessment(cfg).confidence, t.trait_id))
        return out

    def explain(self, trait_id: str, cfg: conf.ScoringConfig = conf.DEFAULT_CONFIG) -> str:
        t = self._trait(trait_id)
        a = t.assessment(cfg)
        lines = [f"[{t.layer.value}/{t.category.value}] {t.statement}  ({t.status.value})",
                 f"confidence {a.confidence:.2f} ({a.band}), scope {a.scope.value}"]
        lines += ["  - " + s for s in a.explanation]
        return "\n".join(lines)

    def export(self, cfg: conf.ScoringConfig = conf.DEFAULT_CONFIG) -> dict:
        """Profile snapshot with observed facts, interpretations and hypotheses in
        separate sections, plus creator self-report apart from footage evidence. Derived data only: the event log stays the source of truth."""
        def section(layer: Layer) -> list[dict]:
            return [t.to_dict(cfg) for t in self.active_traits(layers=[layer], cfg=cfg)]
        inactive = [{"trait_id": t.trait_id, "statement": t.statement, "status": t.status.value,
                     "superseded_by": t.superseded_by, "evidence_count": len(t.evidence_items)}
                    for t in self.traits.values() if t.status in (Status.REJECTED, Status.CORRECTED)]
        return {
            "schema_version": SCHEMA_VERSION, "generated_at": self._clock(), "events": self._seq,
            "observed_facts": section(Layer.OBSERVED),
            "ai_interpretations": section(Layer.INTERPRETATION),
            "hypotheses": section(Layer.HYPOTHESIS),
            "creator_self_report": section(Layer.SELF_REPORT),
            "creator_rejected_or_corrected": inactive,
            "legacy_evidence_warnings": self.legacy_warnings(),
        }

    def write_export(self, path: str | os.PathLike | None = None) -> Path:
        """Write the derived snapshot (default: profile.json next to the log). Overwrites
        only this derived file, never the event log."""
        p = Path(path) if path else self.directory / "profile.json"
        p.write_text(json.dumps(self.export(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p
