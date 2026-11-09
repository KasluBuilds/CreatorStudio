"""Append-only store for attribution evidence and eligibility decisions.

One JSONL event log (`events.jsonl`); nothing is rewritten or deleted. Resolutions are derived on demand
from the evidence; decisions are persisted (including ineligible and requires_review ones) so the system
can always explain an exclusion. A changed attribution produces a NEW decision that names the one it
supersedes. Single writer assumed (a size check refuses to append if the file changed underneath).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .eligibility import decide
from .models import (AUTHORITATIVE, AttributionError, AttributionEvidence, AttributionMethod, Candidate, Channel,
                     Decision, Delivery, EligibilityDecision, Identity, PolicyConfig, Resolution)
from .resolution import resolve

LOG_NAME = "events.jsonl"
SCHEMA_VERSION = 1


class AttributionLogError(AttributionError):
    """The event log is corrupt or inconsistent."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AttributionStore:
    def __init__(self, directory: str | os.PathLike, policy: PolicyConfig | None = None,
                 clock: Callable[[], str] = _utc_now):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.directory / LOG_NAME
        self.policy = policy or PolicyConfig()
        self._clock = clock
        self._evidence: list[tuple[str, AttributionEvidence]] = []
        self._by_id: dict[str, AttributionEvidence] = {}
        self._decisions: list[EligibilityDecision] = []          # in log order (full history)
        self._latest: dict[str, EligibilityDecision] = {}        # candidate key -> current decision
        self.provider_runs: list[dict] = []
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
            raise AttributionLogError(f"{self.log_path}: last line is incomplete (interrupted write); not modified automatically")
        for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AttributionLogError(f"{self.log_path}:{n}: invalid JSON ({exc})") from exc
            if ev.get("seq") != self._seq + 1:
                raise AttributionLogError(f"{self.log_path}:{n}: expected seq {self._seq + 1}, found {ev.get('seq')}")
            if ev.get("v", 0) > SCHEMA_VERSION:
                raise AttributionLogError(f"{self.log_path}:{n}: written by a newer version")
            try:
                self._apply(ev)
            except (KeyError, ValueError) as exc:
                raise AttributionLogError(f"{self.log_path}:{n}: event cannot be applied ({exc!r})") from exc
            self._seq = ev["seq"]

    def _emit(self, type_: str, data: dict) -> None:
        actual = self.log_path.stat().st_size if self.log_path.exists() else 0
        if actual != self._size:
            raise AttributionLogError("event log changed since it was loaded (another writer?); reopen the store")
        ev = {"v": SCHEMA_VERSION, "seq": self._seq + 1, "ts": self._clock(), "type": type_, "data": data}
        line = (json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self._apply(ev)                                    # validate first: errors must not reach the log
        with open(self.log_path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._seq += 1
        self._size = actual + len(line)

    def _apply(self, ev: dict) -> None:
        t, d = ev["type"], ev["data"]
        if t == "evidence_added":
            e = AttributionEvidence.from_dict(d["evidence"])
            eid = e.evidence_id
            if eid in self._by_id:
                raise ValueError(f"duplicate evidence {eid}")
            for s in e.supersedes:
                if s not in self._by_id:
                    raise ValueError(f"supersedes unknown evidence {s}")
            self._by_id[eid] = e
            self._evidence.append((eid, e))
        elif t == "decision_recorded":
            dec = EligibilityDecision.from_dict(d["decision"])
            prior = self._latest.get(dec.candidate.key)
            if prior is not None and dec.supersedes != prior.decision_id:
                raise ValueError("decision does not supersede the current decision of its candidate")
            if prior is None and dec.supersedes is not None:
                raise ValueError("decision supersedes an unknown decision")
            self._decisions.append(dec)
            self._latest[dec.candidate.key] = dec
        elif t == "provider_run":
            self.provider_runs.append(dict(d))
        else:
            raise ValueError(f"unknown event type {t!r}")

    # --------------------------------------------------------------- evidence
    def add_evidence(self, ev: AttributionEvidence) -> str | None:
        """Append evidence. Returns its id, or None if identical evidence already exists (idempotent)."""
        eid = ev.evidence_id
        if eid in self._by_id:
            return None
        for s in ev.supersedes:
            if s not in self._by_id:
                raise AttributionError(f"cannot override unknown evidence {s}")
            if self._by_id[s].media_id != ev.media_id or self._by_id[s].channel is not ev.channel:
                raise AttributionError("a correction can only override evidence of the same recording and channel")
        self._emit("evidence_added", {"evidence": ev.to_dict()})
        return eid

    def evidence(self, evidence_id: str) -> AttributionEvidence:
        try:
            return self._by_id[evidence_id]
        except KeyError:
            raise AttributionError(f"unknown attribution evidence {evidence_id}") from None

    def all_evidence(self) -> list[tuple[str, AttributionEvidence]]:
        return list(self._evidence)

    # ------------------------------------------------- human / metadata inputs
    def human_confirm_span(self, media_id: str, start_s: float, end_s: float, identity: Identity, actor: str,
                           rationale: str, *, channel: Channel = Channel.SPEECH, delivery: Delivery | None = None,
                           person_id: str | None = None, isolated: bool = False, confidence: float | None = None,
                           refs: Sequence[str] = (), source_ref: str = "human review") -> str | None:
        """A person confirms who is behind a range. Additive: if it contradicts earlier authoritative
        evidence the result is CONFLICTED until a correction is made."""
        return self.add_evidence(AttributionEvidence(
            media_id=media_id, channel=channel, method=AttributionMethod.HUMAN_CONFIRMATION, actor=actor,
            source_ref=source_ref, rationale=rationale, start_s=start_s, end_s=end_s, identity=identity,
            person_id=person_id, delivery=delivery, confidence=confidence, isolated=isolated, refs=tuple(refs)))

    def human_correct_span(self, media_id: str, start_s: float, end_s: float, identity: Identity, actor: str,
                           rationale: str, *, channel: Channel = Channel.SPEECH, delivery: Delivery | None = None,
                           person_id: str | None = None, isolated: bool = False, confidence: float | None = None,
                           refs: Sequence[str] = (), source_ref: str = "human correction") -> str | None:
        """A person corrects an attribution. It overrides every earlier span evidence that overlaps the range,
        but only where the correction covers the candidate; the overridden evidence stays on record."""
        overlapping = tuple(i for i, e in self._evidence if e.media_id == media_id and e.channel is channel
                            and e.scope == "span" and min(e.end_s, end_s) - max(e.start_s, start_s) > 1e-6
                            and not self._is_overridden_everywhere(i))
        if not overlapping:
            raise AttributionError("nothing to correct: no earlier attribution evidence overlaps this range")
        return self.add_evidence(AttributionEvidence(
            media_id=media_id, channel=channel, method=AttributionMethod.HUMAN_CORRECTION, actor=actor,
            source_ref=source_ref, rationale=rationale, start_s=start_s, end_s=end_s, identity=identity,
            person_id=person_id, delivery=delivery, confidence=confidence, isolated=isolated,
            supersedes=overlapping, refs=tuple(refs)))

    def _is_overridden_everywhere(self, evidence_id: str) -> bool:
        e = self._by_id[evidence_id]
        return any(evidence_id in f.supersedes and f.start_s <= e.start_s and f.end_s >= e.end_s
                   for _, f in self._evidence if f.scope == "span")

    def project_metadata(self, media_id: str, start_s: float, end_s: float, identity: Identity, actor: str,
                         source_ref: str, rationale: str, *, channel: Channel = Channel.SPEECH,
                         delivery: Delivery | None = None, person_id: str | None = None,
                         confidence: float | None = None) -> str | None:
        """An explicit statement recorded in the project (e.g. 'this clip is the creator narrating')."""
        return self.add_evidence(AttributionEvidence(
            media_id=media_id, channel=channel, method=AttributionMethod.PROJECT_METADATA, actor=actor,
            source_ref=source_ref, rationale=rationale, start_s=start_s, end_s=end_s, identity=identity,
            person_id=person_id, delivery=delivery, confidence=confidence))

    def map_cluster(self, media_id: str, provider: str, cluster_id: str, identity: Identity, actor: str,
                    rationale: str, *, person_id: str | None = None, delivery: Delivery | None = None,
                    confidence: float | None = None, supersedes: Sequence[str] = (),
                    source_ref: str = "human cluster review") -> str | None:
        """A person says which identity a provider's cluster is, for this recording only."""
        method = AttributionMethod.HUMAN_CORRECTION if supersedes else AttributionMethod.HUMAN_CONFIRMATION
        return self.add_evidence(AttributionEvidence(
            media_id=media_id, channel=Channel.SPEECH, method=method, actor=actor, source_ref=source_ref,
            rationale=rationale, scope="cluster", identity=identity, person_id=person_id, delivery=delivery,
            cluster_id=cluster_id, provider=provider, confidence=confidence, supersedes=tuple(supersedes)))

    # ------------------------------------------------------------- resolution
    def resolve(self, media_id: str, channel: Channel, start_s: float, end_s: float) -> Resolution:
        return resolve(self._evidence, media_id, channel, start_s, end_s, self.policy)

    # --------------------------------------------------------------- decisions
    def evaluate(self, candidates: Iterable[Candidate]) -> list[EligibilityDecision]:
        """Decide every candidate and persist the decision. Idempotent: an unchanged decision is not re-recorded;
        a changed one is recorded as a new decision superseding the previous one. Returns the CURRENT decisions."""
        out = []
        for c in candidates:
            new = decide(c, self.resolve, self.policy)
            prior = self._latest.get(c.key)
            if prior is not None and prior.decision_id == new.decision_id:
                out.append(prior)
                continue
            d = EligibilityDecision(
                candidate=new.candidate, decision=new.decision, reason_code=new.reason_code, reason=new.reason,
                resolution=new.resolution, policy_version=new.policy_version,
                attribution_evidence_ids=new.attribution_evidence_ids, identity_basis=new.identity_basis,
                confidence=new.confidence, max_evidence_grade=new.max_evidence_grade,
                transcript_review=new.transcript_review, supersedes=prior.decision_id if prior else None,
                recorded_at=self._clock())
            self._emit("decision_recorded", {"decision": d.to_dict()})
            out.append(d)
        return out

    def current_decision(self, media_id: str, kind: str, record_id: str) -> EligibilityDecision | None:
        return self._latest.get(f"{media_id}|{kind}|{record_id}")

    def decisions(self, history: bool = False) -> list[EligibilityDecision]:
        return list(self._decisions) if history else list(self._latest.values())

    def withdrawn_eligibility(self) -> list[tuple[EligibilityDecision, EligibilityDecision]]:
        """(earlier eligible decision, current non-eligible decision) pairs: evidence stamped with the earlier
        decision must stop counting in Creator DNA (see `handoff.sync_revocations`)."""
        out = []
        for d in self._decisions:
            cur = self._latest[d.candidate.key]
            if d.decision is Decision.ELIGIBLE and cur.decision is not Decision.ELIGIBLE:
                out.append((d, cur))
        return out

    # --------------------------------------------------------------- providers
    def record_provider_run(self, provider: str, version: str, status: str, detail: str = "",
                            media_id: str | None = None, evidence_added: int = 0) -> None:
        if status not in ("ok", "unavailable", "failed"):
            raise AttributionError("provider run status must be ok, unavailable or failed")
        self._emit("provider_run", {"provider": provider, "version": version, "status": status, "detail": detail,
                                    "media_id": media_id, "evidence_added": evidence_added, "at": self._clock()})
