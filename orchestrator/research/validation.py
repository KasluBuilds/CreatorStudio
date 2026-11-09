"""Whole-brief validation. Returns a report; never mutates the brief."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .attestation import DEFAULT_TRUST, TrustPolicy
from .formats import check_format_study
from .models import (SCHEMA_VERSION, BriefType, ClaimKind, ClaimStatus, PerspectiveWeight, ResearchBrief,
                     ReviewStatus, SourceType, Stance, TopicKind)
from .safety import NOTE_MAX, unsafe_text_problems
from .sources import (AUTO_CAVEAT, MIN_JUSTIFICATION_CHARS, Issue, assess_claim, citation_counts, independence_groups,
                      is_safety_critical, validate_sources)

CREATOR_NAMES = {"creator", "the creator", "user", "the user", "host", "me", "i", "owner"}
MAX_CONCEPT_CHARS = 280
MAX_CLAIM_CHARS = 1000
RECHECK_DAYS_CURRENT_EVENT = 7
_RANK = {ClaimStatus.UNVERIFIED: 0, ClaimStatus.PARTIAL: 1, ClaimStatus.VERIFIED: 2}


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}

    def raise_if_errors(self) -> None:
        if self.errors:
            raise BriefInvalid(self)


class BriefInvalid(ValueError):
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__("; ".join(str(i) for i in report.errors[:5]))


def _unique(items: Iterable, key, label: str, out: list[Issue]) -> set[str]:
    seen: set[str] = set()
    for i, x in enumerate(items):
        k = key(x)
        if not k:
            out.append(Issue("ID_MISSING", "error", f"{label}[{i}]", "id required"))
        elif k in seen:
            out.append(Issue("DUPLICATE_ID", "error", f"{label}[{i}:{k}]", "duplicate id"))
        seen.add(k)
    return seen


def validate_brief(b: ResearchBrief, today: dt.date, reference_texts: Iterable[str] = (),
                   trust: Optional[TrustPolicy] = None) -> ValidationReport:
    """Validate against a TrustPolicy. The default policy has no key, so nothing is attested and any
    brief that relied on human/process attestations will (correctly) fail to validate."""
    trust = (trust or DEFAULT_TRUST).bound_to(b.brief_id)    # attestations are valid only for THIS brief
    out: list[Issue] = []
    ids = [s.id for s in b.sources]
    if len(ids) != len(set(ids)):                            # reject BEFORE any evidence assessment
        out += validate_sources(b.sources, today, trust)
        out.append(Issue("EVIDENCE_NOT_ASSESSED", "error", "sources",
                         "duplicate source ids: evidence was not assessed (results would depend on list order)"))
        return ValidationReport(out)
    fp = trust.fingerprint()
    if not b.assessed_under:
        out.append(Issue("POLICY_UNRECORDED", "warning", "assessed_under",
                         "the brief records no trust-policy fingerprint (it was not built through the builder)"))
    elif b.assessed_under != fp:
        out.append(Issue("POLICY_MISMATCH", "error", "assessed_under",
                         "the brief was assessed under a different trust policy than the one now in force: "
                         "rebuild it under the active policy"))
    if b.schema_version != SCHEMA_VERSION:
        out.append(Issue("SCHEMA_VERSION", "error", "schema_version", f"expected {SCHEMA_VERSION}"))
    if not b.topic.strip():
        out.append(Issue("TOPIC_MISSING", "error", "topic", "topic required"))
    if not b.audience.description.strip():
        out.append(Issue("AUDIENCE_MISSING", "error", "audience", "audience description required"))
    if b.brief_type is BriefType.TOPIC_OPPORTUNITY and not b.why_now.strip():
        out.append(Issue("WHY_NOW_MISSING", "error", "why_now", "explain why this is interesting now"))
    if b.created_at > today:
        out.append(Issue("CREATED_IN_FUTURE", "error", "created_at", "created after today"))

    out += validate_sources(b.sources, today, trust)
    source_ids = _unique(b.sources, lambda s: s.id, "sources", [])
    claim_ids = _unique(b.claims, lambda c: c.id, "claims", out)
    angle_ids = _unique(b.angles, lambda a: a.id, "angles", out)
    persp_ids = _unique(b.perspectives, lambda p: p.id, "perspectives", out)
    _unique(b.hooks, lambda h: h.id, "hooks", out)
    _unique(b.visuals, lambda v: v.id, "visuals", out)
    if not b.claims:
        out.append(Issue("NO_CLAIMS", "warning", "claims", "brief has no main claims"))

    def ref(kind: str, ids: Iterable[str], valid: set[str], path: str) -> None:
        for x in ids:
            if x not in valid:
                out.append(Issue("UNKNOWN_REFERENCE", "error", path, f"{kind} '{x}' not found"))

    # independence attestations must verify
    for k, ia in enumerate(b.independence_attestations):
        v = trust.verify_independence(ia, {s.id: s for s in b.sources}, today)
        if not v.ok:
            out.append(Issue("ATTESTATION_INVALID", "error", f"independence_attestations[{k}]",
                             "independence attestation does not verify: " + "; ".join(v.reasons)))
    derived_by_claim: dict = {}

    # claims
    for i, c in enumerate(b.claims):
        p = f"claims[{i}:{c.id}]"
        for aname, att, subj_ok in (("evergreen_attestation", c.evergreen_attestation, trust.verify_evergreen),
                                    ("kind_attestation", c.kind_attestation, trust.verify_kind),
                                    ("risk_downgrade_attestation", c.risk_downgrade_attestation, trust.verify_risk_downgrade)):
            if att is not None:
                v = subj_ok(c, today)
                if not v.ok:
                    out.append(Issue("ATTESTATION_INVALID", "error", f"{p}.{aname}",
                                     "attestation does not verify: " + "; ".join(v.reasons)))
        if not c.text.strip():
            out.append(Issue("CLAIM_TEXT_MISSING", "error", p, "claim text required"))
        ref("source", [x.source_id for x in c.citations], source_ids, p + ".citations")
        ref("claim", c.based_on, claim_ids, p + ".based_on")
        for why in unsafe_text_problems(c.text, MAX_CLAIM_CHARS, multiline=True, check_secrets=False):
            out.append(Issue("UNSAFE_TEXT", "error", p + ".text", why))
        for j, x in enumerate(c.citations):
            cp = f"{p}.citations[{j}]"
            for fname, val in (("locator", x.locator), ("note", x.note), ("assessment", x.assessment),
                               ("reviewed_by", x.reviewed_by)):
                for why in unsafe_text_problems(val, NOTE_MAX):
                    out.append(Issue("UNSAFE_TEXT", "error", f"{cp}.{fname}", why))
            csrc = b.source(x.source_id)
            if x.stance is not Stance.CONTEXT:
                counted, why = citation_counts(x, c, csrc, trust, today)
                if not counted:
                    out.append(Issue("CITATION_NOT_COUNTED", "warning", cp,
                                     f"does not count as evidence: {why}"))
            verdict = trust.verify_citation(c, x, csrc, today) if (x.attestation is not None and csrc) else None
            if verdict is not None and not verdict.ok:
                out.append(Issue("ATTESTATION_INVALID", "error", cp,
                                 "citation attestation does not verify: " + "; ".join(verdict.reasons)))
            if x.excerpt_proof is not None and csrc is not None:
                pv = trust.verify_excerpt(x, csrc, today)
                if not pv.ok:
                    out.append(Issue("PROOF_INVALID", "error", cp, "excerpt proof does not verify: " + "; ".join(pv.reasons)))
            if x.assessed_stance in (Stance.SUPPORTS, Stance.CONTRADICTS) and x.stance is not x.assessed_stance:
                out.append(Issue("STANCE_RELABELLED", "warning", cp,
                                 f"stance {x.assessed_stance.value} -> {x.stance.value} after assessment; the original "
                                 "assessment stays in force unless a valid reviewer attestation resolves it"))
            if x.review in (ReviewStatus.HUMAN_REVIEWED, ReviewStatus.PROCESS_VERIFIED):
                want = "human" if x.review is ReviewStatus.HUMAN_REVIEWED else "process"
                if not (verdict is not None and verdict.ok and verdict.kind == want):
                    out.append(Issue("REVIEW_UNATTESTED", "error", cp,
                                     f"review is labelled {x.review.value} but no valid {want} attestation covers this "
                                     "claim, source, stance, locator and assessment; a label alone proves nothing"))
            if x.review is not ReviewStatus.UNREVIEWED and not x.reviewed_by.strip():
                out.append(Issue("REVIEWER_MISSING", "warning", cp, "reviewed citation should name the reviewer"))
        if c.kind is ClaimKind.FACT:
            if c.status is ClaimStatus.NOT_APPLICABLE:
                out.append(Issue("STATUS_INVALID", "error", p, "a fact needs a verification status"))
            else:
                derived = assess_claim(c, b.sources, today=today, topic_kind=b.topic_kind, trust=trust,
                                       independence=b.independence_attestations)
                derived_by_claim[c.id] = derived
                d, s_ = derived.status, c.status
                if d is ClaimStatus.DISPUTED and s_ is not ClaimStatus.DISPUTED:
                    out.append(Issue("DISPUTE_NOT_DECLARED", "error", p,
                                     "comparably credible evidence conflicts; mark the claim DISPUTED and show perspectives"))
                elif d is ClaimStatus.CONTRADICTED and s_ is not ClaimStatus.CONTRADICTED:
                    out.append(Issue("CONTRADICTION_NOT_DECLARED", "error", p,
                                     "stronger credible evidence weighs against the claim; mark it CONTRADICTED"))
                elif s_ in (ClaimStatus.DISPUTED, ClaimStatus.CONTRADICTED) and s_ is not d:
                    out.append(Issue("CONTESTED_STATUS_UNSUPPORTED", "error", p,
                                     f"declared {s_.value} but the evidence gives {d.value}: contested statuses must "
                                     "reflect the real balance of evidence (no false balance)"))
                elif s_ not in (ClaimStatus.DISPUTED, ClaimStatus.CONTRADICTED) and _RANK.get(s_, 0) > _RANK.get(d, 0):
                    out.append(Issue("STATUS_OVERSTATED", "error", p,
                                     f"declared {s_.value} but evidence supports only "
                                     f"{d.value}: {'; '.join(derived.reasons)}"))
                elif s_ is not d:
                    out.append(Issue("STATUS_STALE", "error", p,
                                     f"stored status {s_.value} differs from the recomputed {d.value}: derived fields "
                                     "are recomputed, never trusted"))
                if c.evidence_route != derived.route:
                    out.append(Issue("ROUTE_MISMATCH", "error", p,
                                     f"stored evidence_route '{c.evidence_route}' differs from the recomputed "
                                     f"'{derived.route}'"))
                if list(c.status_reasons) != list(derived.reasons):
                    out.append(Issue("EXPLANATION_MISMATCH", "error", p,
                                     "stored status explanation differs from the recomputed evidence trail"))
                stored_auto = [x for x in c.caveats if x.startswith(AUTO_CAVEAT)]
                if stored_auto != [AUTO_CAVEAT + x for x in derived.caveats]:
                    out.append(Issue("CAVEATS_MISMATCH", "error", p,
                                     "the derived caveats (contradictions, warnings, relabellings) differ from the "
                                     "stored ones: a caveat was deleted or forged"))
                if c.status is ClaimStatus.VERIFIED and not c.citations:
                    out.append(Issue("VERIFIED_WITHOUT_CITATION", "error", p, "verified claim has no citation"))
        else:
            if c.status is not ClaimStatus.NOT_APPLICABLE:
                out.append(Issue("STATUS_INVALID", "error", p,
                                 f"{c.kind.value} must have status not_applicable (it is not a verifiable fact)"))
            if c.kind is ClaimKind.OPINION:
                who = c.attributed_to.strip()
                if not who:
                    out.append(Issue("OPINION_UNATTRIBUTED", "error", p, "state whose opinion this is"))
                elif who.casefold() in CREATOR_NAMES:
                    out.append(Issue("CREATOR_OPINION_FABRICATED", "error", p,
                                     "research must not write opinions on behalf of the creator"))
            if c.kind is ClaimKind.INTERPRETATION and not c.based_on:
                out.append(Issue("INTERPRETATION_UNGROUNDED", "error", p,
                                 "an interpretation must list the claims it rests on; otherwise it may be a factual "
                                 "assertion relabelled to avoid verification"))
            kind_ok = c.kind_attestation is not None and trust.verify_kind(c, today).ok
            if c.original_kind is ClaimKind.FACT and not kind_ok:
                out.append(Issue("KIND_CHANGE_UNREVIEWED", "error", p,
                                 f"claim was reclassified fact -> {c.kind.value} without a valid reviewer attestation"))
            if is_safety_critical(c, trust, today) and not kind_ok:
                out.append(Issue("SAFETY_CLAIM_NOT_FACT_CHECKED", "error", p,
                                 "a safety-critical statement (toxicity, edibility, health, danger) cannot be carried as "
                                 f"{c.kind.value} to avoid verification; state it as a fact claim with evidence"))
            if c.kind is ClaimKind.HYPOTHESIS and not c.testable_by.strip():
                out.append(Issue("HYPOTHESIS_UNTESTABLE", "warning", p, "say how it could be tested"))

    # time-sensitivity of the sources behind the brief
    if b.topic_kind is TopicKind.CURRENT_EVENT:
        old = [s.id for s in b.sources if (today - s.retrieved_at).days > RECHECK_DAYS_CURRENT_EVENT]
        if old:
            out.append(Issue("RETRIEVAL_STALE", "warning", "sources",
                             f"current-event sources retrieved > {RECHECK_DAYS_CURRENT_EVENT} days ago: "
                             f"{', '.join(old)}; re-check before use"))
    if b.topic_kind is not TopicKind.EVERGREEN:
        for i, c in enumerate(b.claims):
            if c.kind is ClaimKind.FACT and c.time_sensitive is False:
                if (len(c.evergreen_justification.strip()) < MIN_JUSTIFICATION_CHARS
                        or not trust.verify_evergreen(c, today).ok):
                    out.append(Issue("EVERGREEN_EXEMPTION_UNJUSTIFIED", "error", f"claims[{i}:{c.id}]",
                                     f"time_sensitive=false on a {b.topic_kind.value} brief needs a justification "
                                     f"(>= {MIN_JUSTIFICATION_CHARS} chars) AND a valid human/process attestation "
                                     "covering this claim and that justification"))
    if b.independence:
        fresh = independence_groups(b.sources, b.independence_attestations, trust, today)
        stored = {g.group_id: sorted(g.source_ids) for g in b.independence}
        if stored != fresh.members:
            out.append(Issue("INDEPENDENCE_MISMATCH", "error", "independence",
                             "stored independence groups differ from the recomputed ones: derived fields are never trusted"))

    # perspectives / controversy
    disputed = [c for c in b.claims if c.status is ClaimStatus.DISPUTED]
    if (b.controversial or disputed) and len(b.perspectives) < 2:
        out.append(Issue("PERSPECTIVES_MISSING", "error", "perspectives",
                         "contested topic needs at least two perspectives"))
    for c in disputed:                                        # perspectives must be about THIS claim
        linked = [pr for pr in b.perspectives if c.id in pr.claim_ids]
        cited = {x.source_id for x in c.citations}
        if len(linked) < 2:
            out.append(Issue("PERSPECTIVES_MISSING", "error", f"claims[{c.id}]",
                             f"disputed claim {c.id} needs at least two perspectives linked to it (found {len(linked)})"))
        for pr in linked:
            if not (set(pr.source_ids) & cited):
                out.append(Issue("PERSPECTIVE_UNSUPPORTED", "error", f"perspectives[{pr.id}]",
                                 f"perspective on disputed claim {c.id} cites none of the sources cited by that claim"))
            if pr.weight is not PerspectiveWeight.COMPARABLE:
                out.append(Issue("DISPUTE_WEIGHTS_INVALID", "error", f"perspectives[{pr.id}]",
                                 f"claim {c.id} is genuinely disputed: its perspectives must be weighted 'comparable' "
                                 f"(found '{pr.weight.value}')"))
    for cid, derived in derived_by_claim.items():             # credible dissent that was outweighed stays visible
        if not derived.outweighed_credible:
            continue
        cl = next(c for c in b.claims if c.id == cid)
        represented = any(cid in pr.claim_ids and pr.weight is PerspectiveWeight.MINORITY
                          and set(pr.source_ids) & set(derived.outweighed_credible) for pr in b.perspectives)
        if not represented and len(cl.dissent_waiver.strip()) < MIN_JUSTIFICATION_CHARS:
            out.append(Issue("DISSENT_NOT_REPRESENTED", "error", f"claims[{cid}]",
                             f"credible but outweighed disagreement from {', '.join(derived.outweighed_credible)} needs a "
                             "'minority' perspective citing it, or an explicit dissent_waiver"))
    if disputed and not b.controversial:
        out.append(Issue("CONTROVERSY_NOT_FLAGGED", "error", "controversial",
                         "disputed claims present; set controversial=true"))
    for i, pr in enumerate(b.perspectives):
        p = f"perspectives[{i}:{pr.id}]"
        ref("source", pr.source_ids, source_ids, p + ".source_ids")
        ref("claim", pr.claim_ids, claim_ids, p + ".claim_ids")
        if not pr.source_ids:
            out.append(Issue("UNSOURCED_PERSPECTIVE", "warning", p, "perspective cites no source"))
        if pr.basis.value == "evidence_based" and not pr.evidence_assessment.strip():
            out.append(Issue("NO_EVIDENCE_ASSESSMENT", "warning", p,
                             "say how well-supported this is (avoid false balance)"))
        for cid in pr.claim_ids:
            cl = next((c for c in b.claims if c.id == cid), None)
            if cl is None:
                continue
            if pr.weight is PerspectiveWeight.COMPARABLE and cl.status is not ClaimStatus.DISPUTED:
                out.append(Issue("FALSE_BALANCE", "error", p,
                                 f"presented as comparable, but claim {cid} is {cl.status.value}, not a genuine "
                                 "dispute; use 'minority' (or 'prevailing') to reflect the evidence"))
            if pr.weight is PerspectiveWeight.MINORITY and cl.status is ClaimStatus.DISPUTED:
                out.append(Issue("PERSPECTIVE_WEIGHT_MISMATCH", "warning", p,
                                 f"marked minority but claim {cid} is genuinely disputed (comparable evidence)"))
    for i, u in enumerate(b.uncertainties):
        ref("claim", u.claim_ids, claim_ids, f"uncertainties[{i}].claim_ids")

    # creative parts: guidance only
    for i, h in enumerate(b.hooks):
        p = f"hooks[{i}:{h.id}]"
        ref("claim", h.claim_ids, claim_ids, p + ".claim_ids")
        if h.angle_id:
            ref("angle", [h.angle_id], angle_ids, p + ".angle_id")
        for fld in ("hook_concept", "payoff_concept"):
            t = getattr(h, fld).strip()
            if not t:
                out.append(Issue("CONCEPT_MISSING", "error", f"{p}.{fld}", "concept required"))
            elif len(t) > MAX_CONCEPT_CHARS or t[0] in "\"“„'":
                out.append(Issue("DIALOGUE_LIKE", "warning", f"{p}.{fld}",
                                 "looks like scripted dialogue; keep it a concept (the creator speaks)"))
    for i, v in enumerate(b.visuals):
        p = f"visuals[{i}:{v.id}]"
        ref("claim", v.claim_ids, claim_ids, p + ".claim_ids")
        ref("source", v.reference_source_ids, source_ids, p + ".reference_source_ids")
    for i, f in enumerate(b.footage):
        if not f.description.strip():
            out.append(Issue("FOOTAGE_DESC_MISSING", "error", f"footage[{i}]", "description required"))

    # format study
    if b.brief_type is BriefType.REFERENCE_VIDEO and b.format_study is None:
        out.append(Issue("FORMAT_STUDY_MISSING", "error", "format_study", "reference_video brief needs a study"))
    if b.format_study is not None:
        fs = b.format_study
        if fs.reference_source_id not in source_ids:
            out.append(Issue("UNKNOWN_REFERENCE", "error", "format_study.reference_source_id",
                             f"source '{fs.reference_source_id}' not found"))
        else:
            src = b.source(fs.reference_source_id)
            if src and src.source_type is not SourceType.VIDEO_PLATFORM:
                out.append(Issue("REFERENCE_NOT_VIDEO", "warning", "format_study.reference_source_id",
                                 "reference source is not typed video_platform"))
        out += check_format_study(fs, reference_texts)
    return ValidationReport(out)
