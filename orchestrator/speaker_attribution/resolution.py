"""Derive an attribution for one candidate time range from attribution evidence. Pure, no I/O.

Rules (each one exists so an unknown speaker can never turn into creator evidence by default):
  * Only AUTHORITATIVE evidence (human confirmation/correction, project metadata) can make an identity
    CONFIRMED. SUPPORTING evidence (diarisation clusters, A/V sync, visual context, continuity) can only
    make it PROPOSED, which is never eligible on its own.
  * A diarisation cluster is a grouping of similar voices, not an identity. It contributes an identity only
    through an authoritative cluster->identity mapping, and then only with a supplied membership confidence.
  * A correction overrides earlier evidence only INSIDE its own range (and only where it covers the candidate).
  * Contradictory evidence at the same rank -> CONFLICTED -> identity is treated as unknown.
  * Coverage: evidence must cover the candidate (within a tolerance; several same-identity items may be
    combined). Evidence that overlaps only part of the candidate and names a different identity makes the
    candidate `mixed`.
  * Confidence is derived from what was supplied; if it cannot be quantified it stays None.
"""
from __future__ import annotations

from typing import Sequence

from .models import (AUTHORITATIVE, RANK, AttributionEvidence, AttributionMethod, Channel, Delivery, Identity,
                     PolicyConfig, Resolution, Status)

Item = tuple[str, AttributionEvidence]      # (evidence_id, evidence)


def _overlap(e: AttributionEvidence, start: float, end: float) -> bool:
    if end - start < 1e-6:                                   # a point (e.g. a boundary)
        return e.start_s <= start <= e.end_s
    return min(e.end_s, end) - max(e.start_s, start) > 1e-6


def _covers(e: AttributionEvidence, start: float, end: float, tol: float) -> bool:
    return e.start_s <= start + tol and e.end_s >= end - tol


def _union_covers(intervals: list[tuple[float, float]], start: float, end: float, tol: float) -> bool:
    cur = start
    for s, e in sorted(intervals):
        if s <= cur + tol:
            cur = max(cur, e)
    return cur >= end - tol


def _base_conf(e: AttributionEvidence, policy: PolicyConfig) -> float | None:
    if e.method in (AttributionMethod.HUMAN_CONFIRMATION, AttributionMethod.HUMAN_CORRECTION):
        base = policy.human_confidence
    elif e.method is AttributionMethod.PROJECT_METADATA:
        base = policy.metadata_confidence
    else:
        return e.confidence
    return base if e.confidence is None else min(base, e.confidence)


class _Claim:
    __slots__ = ("eid", "ids", "start", "end", "identity", "person", "delivery", "rank", "conf", "via", "isolated",
                 "method")

    def __init__(self, eid, ids, start, end, identity, person, delivery, rank, conf, via, isolated, method):
        self.eid, self.ids, self.start, self.end = eid, ids, start, end
        self.identity, self.person, self.delivery, self.rank = identity, person, delivery, rank
        self.conf, self.via, self.isolated, self.method = conf, via, isolated, method

    @property
    def key(self):
        return (self.identity, self.person)


def resolve(evidence: Sequence[Item], media_id: str, channel: Channel, start: float, end: float,
            policy: PolicyConfig) -> Resolution:
    tol = policy.coverage_tolerance_s
    items = [(i, e) for i, e in evidence if e.media_id == media_id and e.channel is channel]
    spans = [(i, e) for i, e in items if e.scope == "span" and _overlap(e, start, end)]
    cluster_items = [(i, e) for i, e in items if e.scope == "cluster"]
    cluster_overridden = {s for _, e in cluster_items for s in e.supersedes}
    maps = [(i, e) for i, e in cluster_items if i not in cluster_overridden]

    # Evidence overridden HERE: a correction that itself covers the candidate overrides what it names.
    overridden = {sid for i, f in spans if _covers(f, start, end, tol) for sid in f.supersedes}
    live = [(i, e) for i, e in spans if i not in overridden]
    overridden_here = sorted(i for i, _ in spans if i in overridden)

    claims: list[_Claim] = []
    cluster_without_identity = False
    for i, e in live:
        if e.method is AttributionMethod.DIARIZATION_CLUSTER:
            matched = [(mi, m) for mi, m in maps if m.cluster_id == e.cluster_id and m.provider == e.provider]
            if not matched and _covers(e, start, end, tol):
                cluster_without_identity = True
            for mi, m in matched:
                base = _base_conf(m, policy)
                conf = base * e.confidence if (base is not None and e.confidence is not None) else None
                claims.append(_Claim(i, (i, mi), e.start_s, e.end_s, m.identity, m.person_id, m.delivery,
                                     RANK[m.method], conf, True, m.isolated, m.method))
            continue
        rank = RANK.get(e.method, 0)
        conf = _base_conf(e, policy)
        claims.append(_Claim(i, (i,), e.start_s, e.end_s, e.identity, e.person_id, e.delivery, rank, conf, False,
                             e.isolated, e.method))

    expl: list[str] = []

    def covered_groups(cs: list[_Claim], keyfn):
        groups: dict = {}
        for c in cs:
            groups.setdefault(keyfn(c), []).append(c)
        out = {}
        for k, g in groups.items():
            out[k] = (g, _union_covers([(c.start, c.end) for c in g], start, end, tol))
        return out

    # ------------------------------------------------------------- identity
    ident_claims = [c for c in claims if c.identity is not None]
    auth = covered_groups([c for c in ident_claims if c.rank > 0], lambda c: c.key)
    covered_auth = {k: g for k, (g, ok) in auth.items() if ok}
    identity, person, status, conf = Identity.UNKNOWN, None, Status.UNATTRIBUTED, None
    covered_sup: dict = {}
    basis: list[str] = []
    used: list[str] = []
    via = False
    isolated = False
    resolved_key = None
    if covered_auth:
        top = max(max(c.rank for c in g) for g in covered_auth.values())
        tops = {k: g for k, g in covered_auth.items() if max(c.rank for c in g) == top}
        if len(tops) == 1:
            (resolved_key, g), = tops.items()
            identity, person = resolved_key
            via = all(c.via for c in g)
            status = Status.CONFIRMED_VIA_CLUSTER if via else Status.CONFIRMED
            confs = [c.conf for c in g if c.conf is not None]
            conf = max(confs) if confs else None
            if conf is None:
                expl.append("identity confirmed but its confidence cannot be quantified (e.g. cluster membership confidence not supplied)")
            isolated = any(c.isolated and c.start <= start + tol and c.end >= end - tol for c in g)
            basis = sorted({c.method.value for c in g})
            used = sorted({x for c in g for x in c.ids})
            expl.append(f"{identity.value} {'via a human-mapped diarisation cluster' if via else 'by ' + '+'.join(basis)}")
            lower = [k for k in covered_auth if k not in tops]
            if lower:
                expl.append("lower-ranked authoritative evidence disagrees and is overruled: "
                            + ", ".join(sorted(k[0].value for k in lower)))
        else:
            status = Status.CONFLICTED
            used = sorted({x for g in tops.values() for c in g for x in c.ids})
            basis = sorted({c.method.value for g in tops.values() for c in g})
            expl.append("contradictory authoritative evidence: " + ", ".join(sorted(k[0].value for k in tops)))
    else:
        sup = covered_groups([c for c in ident_claims if c.rank == 0], lambda c: c.key)
        covered_sup = {k: g for k, (g, ok) in sup.items() if ok}
        if len(covered_sup) == 1:
            (resolved_key, g), = covered_sup.items()
            identity, person = resolved_key
            status = Status.PROPOSED
            confs = [c.conf for c in g if c.conf is not None]
            if confs:
                miss = 1.0
                for x in confs:
                    miss *= (1.0 - x)
                conf = min(1 - miss, policy.supporting_cap)
            basis = sorted({c.method.value for c in g})
            used = sorted({x for c in g for x in c.ids})
            expl.append(f"only supporting evidence ({'+'.join(basis)}) points to {identity.value}: a proposal, never confirmed")
        elif len(covered_sup) > 1:
            status = Status.CONFLICTED
            used = sorted({x for g in covered_sup.values() for c in g for x in c.ids})
            basis = sorted({c.method.value for g in covered_sup.values() for c in g})
            expl.append("supporting evidence disagrees: " + ", ".join(sorted(k[0].value for k in covered_sup)))
        else:
            expl.append("no attribution evidence covers this range")
            if cluster_without_identity:
                expl.append("a diarisation cluster covers it but no human has mapped that cluster to an identity")
        if status is Status.CONFLICTED:
            identity, person = Identity.UNKNOWN, None

    # ---------------------------------------------------------- partial / mixed
    partial_ids: set[str] = set()
    partial_keys: set = set()
    mixed = False
    if covered_auth:
        tier = [c for c in ident_claims if c.rank > 0]                  # authoritative tier decided the result
        covered_ids = {id(c) for g in covered_auth.values() for c in g}
    else:
        tier = ident_claims                                             # nothing authoritative covers the range
        covered_ids = {id(c) for g in covered_sup.values() for c in g}
    for c in tier:
        if id(c) in covered_ids:
            continue
        partial_ids.update(c.ids)
        if resolved_key is not None and c.key != resolved_key:
            mixed = True
        partial_keys.add(c.key)
    if resolved_key is None and len(partial_keys) > 1:
        mixed = True                              # several different identities each cover only part of the range
    if mixed:
        expl.append("another identity overlaps part of this range")

    # ---------------------------------------------------------------- delivery
    delivery = Delivery.UNKNOWN
    delivery_conflict = False
    proposed: set[str] = set()
    if channel is Channel.SPEECH:
        dclaims = [c for c in claims if c.delivery is not None]
        dauth = covered_groups([c for c in dclaims if c.rank > 0], lambda c: c.delivery)
        dcov = {k: g for k, (g, ok) in dauth.items() if ok}
        if dcov:
            top = max(max(c.rank for c in g) for g in dcov.values())
            tops = [k for k, g in dcov.items() if max(c.rank for c in g) == top]
            if len(tops) == 1:
                delivery = tops[0]
                used = sorted(set(used) | {x for c in dcov[tops[0]] for x in c.ids})
            else:
                delivery_conflict = True
                expl.append("contradictory delivery context: " + ", ".join(sorted(d.value for d in tops)))
        proposed = {c.delivery.value for c in dclaims if c.rank == 0}
    return Resolution(
        media_id=media_id, channel=channel, start_s=start, end_s=end, identity=identity, person_id=person,
        delivery=delivery, status=status, confidence=conf, basis=tuple(basis), evidence_ids=tuple(used),
        partial_ids=tuple(sorted(partial_ids)), overridden_ids=tuple(overridden_here), via_cluster=via, mixed=mixed,
        delivery_conflict=delivery_conflict, cluster_without_identity=cluster_without_identity,
        proposed_delivery=tuple(sorted(proposed)), isolated=isolated, explanation=tuple(expl))
