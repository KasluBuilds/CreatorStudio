"""ResearchAgent (coordinator) and BriefBuilder.

The agent does the deterministic work: call providers, classify, rank, score, derive claim
status, validate. Judgement work (reading sources, extracting claims, assessing whether a
citation really bears on a claim, proposing angles) is done by an LLM/human *through the
builder* and is recorded explicitly (locator + assessment + review) - this module never
fabricates claims and never lets a caller declare a claim verified.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Callable, Optional, Union

from . import ledger as _ledger
from .attestation import DEFAULT_TRUST, TrustPolicy
from .discovery import (DiscoveryRequest, TopicCandidate, WeeklyReport, rank_candidates)
from .models import (Attestation, AudienceProfile, BriefStatus, BriefType, Citation, Claim, ClaimKind, ClaimStatus,
                     ContradictionResolution,
                     CreativeAngle, FootageConnection, FormatStudy, HookPayoff, IndependenceGroup, Perspective,
                     QualityTier, ResearchBrief, ReviewStatus, Source, SourceType, Stance, TopicKind,
                     Uncertainty, VisualOpportunity)
from .providers import Gathered, ProviderRegistry, ResearchBudget, SearchQuery
from .safety import (AUTHOR_MAX, MAX_AUTHORS, NOTE_MAX, PUBLISHER_MAX, TITLE_MAX, Redactor, sanitize_text)
from .scoring import (CreatorInterests, ScoreInputs, ScoreWeights, TopicSignals, evidence_from_claims,
                      evidence_from_sources, lead_reliability, reliability_from_claims, score_topic)
from .sources import (AUTO_CAVEAT, SourceCatalog, assess_all, assess_claim, confirm_source, independence_groups,
                      newest_partial_date)
from .validation import ValidationReport, validate_brief


class ResearchAgent:
    def __init__(self, registry: ProviderRegistry, interests: CreatorInterests,
                 weights: Optional[ScoreWeights] = None,
                 clock: Callable[[], dt.date] = dt.date.today,
                 redactor: Optional[Redactor] = None, trust: Optional[TrustPolicy] = None) -> None:
        """`trust`: verification policy supplied by the orchestrator (key + registered methods). The
        default has no key, so nothing is attested and no claim can reach VERIFIED via a human or
        process route (only via two independent tier-B origins)."""
        self.trust = trust or DEFAULT_TRUST
        self.registry = registry
        self.interests = interests
        self.weights = weights or ScoreWeights()
        self.catalog = SourceCatalog()      # cross-call dedupe for gather_sources
        self.redactor = redactor
        self._clock = clock

    def today(self) -> dt.date:
        return self._clock()

    def weekly_report(self, request: Optional[DiscoveryRequest] = None) -> WeeklyReport:
        """Discover -> validate -> dedupe -> score -> rank. Provider failures and malformed
        records are reported, never raised."""
        req = request or DiscoveryRequest(mode="weekly")
        today = req.as_of or self.today()
        found, errors = self.registry.discover(req, today)
        if not req.include_foreign_formats:
            found = [c for c in found if c.reference_format is None]
        ranked, excluded = rank_candidates(found, self.interests, today, self.weights,
                                           limit=req.max_candidates, errors=errors)
        report = WeeklyReport(req, today, ranked, excluded, errors)
        if not self.registry.discovery_providers:
            report.notes.append("no discovery providers registered; nothing to rank")
        return report

    def gather_sources(self, query: SearchQuery, budget: Optional[ResearchBudget] = None) -> Gathered:
        """On-demand evidence gathering. Returns classified, deduplicated (across calls) sources;
        claims are NOT extracted."""
        return self.registry.gather(query, self.today(), budget, catalog=self.catalog)

    def start_brief(self, candidate: TopicCandidate, audience: AudienceProfile,
                    brief_id: str, brief_type: BriefType = BriefType.TOPIC_OPPORTUNITY) -> "BriefBuilder":
        b = BriefBuilder(brief_id, candidate.title, audience, self, brief_type, candidate.topic_kind,
                         category=candidate.category, keywords=list(candidate.keywords),
                         signals=candidate.signals, event_date=candidate.event_date)
        for s in candidate.lead_sources:
            b.add_source(s)
        if candidate.reference_format is not None:
            b.set_format_study(candidate.reference_format)
        return b


def sanitize_source(s: Source, redactor: Optional[Redactor] = None) -> Source:
    """Copy of `s` with every free-text field bounded, control-character-free and redacted."""
    return dataclasses.replace(
        s, title=sanitize_text(s.title, TITLE_MAX, redactor=redactor),
        publisher=sanitize_text(s.publisher, PUBLISHER_MAX, redactor=redactor),
        authors=[a for a in (sanitize_text(x, AUTHOR_MAX, redactor=redactor) for x in s.authors[:MAX_AUTHORS]) if a],
        notes=sanitize_text(s.notes, NOTE_MAX, redactor=redactor),
        classification_warnings=[sanitize_text(w, NOTE_MAX, redactor=redactor) for w in s.classification_warnings])


CiteArg = Union[Citation, tuple[str, Stance]]


class BriefBuilder:
    """Accumulates parts, derives claim status from evidence, scores, validates."""

    def __init__(self, brief_id: str, topic: str, audience: AudienceProfile, agent: ResearchAgent,
                 brief_type: BriefType = BriefType.TOPIC_OPPORTUNITY,
                 topic_kind: TopicKind = TopicKind.EVERGREEN, *, category: str = "",
                 keywords: Optional[list[str]] = None, signals=None, event_date: Optional[str] = None) -> None:
        self._agent = agent
        self._category = category
        self._keywords = keywords or []
        self._signals = signals or TopicSignals()
        self._event_date = event_date
        self._catalog = SourceCatalog()
        self._alias: dict[str, str] = {}
        self._brief_id = brief_id
        self.brief = ResearchBrief(brief_id=brief_id, topic=topic, audience=audience,
                                   created_at=agent.today(), brief_type=brief_type, topic_kind=topic_kind)

    @property
    def _trust(self) -> TrustPolicy:
        """Verification only. Attestations are valid for THIS brief id and the brief's CURRENT topic kind, so a
        change of kind after review makes them stop verifying instead of silently keeping them alive."""
        return self._agent.trust.bound_to(self._brief_id, self.brief.topic_kind)

    # --- evidence
    def add_source(self, s: Source) -> str:
        """Add a source and return the id to cite. The same page/DOI added twice keeps ONE entry
        (the second id becomes an alias of the first, so existing citations stay correct); an id
        already used by a DIFFERENT source raises SourceIdConflict."""
        clean = sanitize_source(s, self._agent.redactor)
        canonical, is_new = self._catalog.add(clean)
        if is_new:
            self.brief.sources.append(canonical)
        elif clean.id != canonical.id:
            self._alias[clean.id] = canonical.id
        return canonical.id

    def confirm_source(self, source_id: str, *, by: str, source_type: Optional[SourceType] = None,
                       peer_reviewed: Optional[bool] = None, note: str = "",
                       attestation: Optional[Attestation] = None) -> "BriefBuilder":
        """Record a classification decision (labels). It only raises the source's tier if the supplied
        `attestation` (issued by the orchestrator-side AttestationAuthority for exactly these fields)
        verifies under the agent's TrustPolicy. Labels alone change nothing."""
        sid = self._alias.get(source_id, source_id)
        cur = self._catalog.get(sid)
        if cur is None:
            raise KeyError(f"unknown source '{source_id}'")
        new = confirm_source(cur, by=by, source_type=source_type, peer_reviewed=peer_reviewed, note=note,
                             attestation=attestation)
        for f in dataclasses.fields(new):          # update in place: catalog and brief share the object
            setattr(cur, f.name, getattr(new, f.name))
        return self

    def add_query(self, text: str) -> "BriefBuilder":
        self.brief.queries.append(sanitize_text(text, 300, redactor=self._agent.redactor))
        return self

    def cite(self, source_id: str, stance: Stance = Stance.SUPPORTS, *, locator: str = "",
             assessment: str = "", review: ReviewStatus = ReviewStatus.UNREVIEWED,
             reviewed_by: str = "", note: str = "", proof=None) -> Citation:
        """Build a citation. To count as evidence it needs a locator, a recorded assessment of what
        the source establishes about THIS claim, and a review other than 'unreviewed'."""
        assessed = stance if review is not ReviewStatus.UNREVIEWED else None
        return Citation(self._alias.get(source_id, source_id), stance, locator, note, assessment,
                        review, reviewed_by, assessed_stance=assessed, excerpt_proof=proof)

    def _resolve(self, item: CiteArg) -> Citation:
        if isinstance(item, Citation):
            return dataclasses.replace(item, source_id=self._alias.get(item.source_id, item.source_id))
        sid, stance = item
        return self.cite(sid, stance)       # bare (id, stance): unreviewed, will not count

    def add_claim(self, id: str, text: str, kind: ClaimKind = ClaimKind.FACT, *,
                  cites: Optional[list[CiteArg]] = None, **kw) -> "BriefBuilder":
        """Status is always derived from the evidence; callers cannot declare it here."""
        kw.pop("status", None)
        claim = Claim(id=id, text=text, kind=kind, citations=[self._resolve(c) for c in (cites or [])], **kw)
        self.brief.claims.append(claim)
        _ledger.record(self.brief)          # every citation (contradictions above all) enters the append-only ledger
        return self

    def restance(self, claim_id: str, source_id: str, new_stance: Stance, *, locator: str,
                 assessment: str, review: ReviewStatus, reviewed_by: str, proof=None) -> "BriefBuilder":
        """Change a citation's stance. The change is RECORDED and the ORIGINAL assessed stance stays in
        force (so a contradiction cannot be turned into 'context' by relabelling): only a valid human
        attestation over the changed citation, issued by the review boundary, resolves it."""
        sid = self._alias.get(source_id, source_id)
        claim = next(c for c in self.brief.claims if c.id == claim_id)
        cit = next(x for x in claim.citations if x.source_id == sid)
        cit.stance_history.append(f"{cit.stance.value}>{new_stance.value}")
        cit.previous_stance = cit.stance if cit.previous_stance is None else cit.previous_stance
        cit.stance, cit.locator, cit.assessment = new_stance, locator, assessment
        cit.review, cit.reviewed_by = review, reviewed_by
        cit.attestation = None          # the reviewed subject changed: any earlier attestation no longer applies
        if proof is not None:
            cit.excerpt_proof = proof
        if cit.assessed_stance is None and review is not ReviewStatus.UNREVIEWED:
            cit.assessed_stance = new_stance
        _ledger.record(self.brief)
        return self

    def resolve_contradiction(self, entry_id: str, rationale: str, attestation: Attestation) -> "BriefBuilder":
        """Attach a human's resolution of one recorded contradiction (see ledger.py). Refused unless it verifies
        now; the contradiction stays in the ledger and in the caveats either way."""
        entry = next((e for e in self.brief.evidence_ledger if e.entry_id == entry_id), None)
        if entry is None:
            raise KeyError(f"unknown ledger entry '{entry_id}'")
        claim = next((c for c in self.brief.claims if c.id == entry.claim_id), None)
        from .attestation import sha256_text
        res = ContradictionResolution(entry_id, rationale, attestation)
        v = self._trust.verify_resolution(res, entry, sha256_text(claim.text) if claim is not None else "",
                                          self._agent.today())
        if not v.ok:
            raise ValueError("resolution rejected: " + "; ".join(v.reasons))
        self.brief.resolutions.append(res)
        return self

    def attach_ledger_seal(self, seal: Attestation) -> "BriefBuilder":
        """Attach the orchestrator-signed seal over the ledger head (call after build(); any later change to the
        ledger invalidates it). Refused unless it verifies for the ledger as it is now."""
        _ledger.record(self.brief)
        v = self._trust.verify_ledger_seal(seal, self.brief.brief_id, _ledger.head(self.brief.evidence_ledger),
                                           len(self.brief.evidence_ledger), self._agent.today())
        if not v.ok:
            raise ValueError("ledger seal rejected: " + "; ".join(v.reasons))
        self.brief.ledger_seal = seal
        return self

    def attach_retrieval(self, source_id: str, record) -> "BriefBuilder":
        """Attach a retrieval record issued by the INGESTION boundary. Refused unless it verifies for this
        source under the active policy; research cannot create or alter a receipt."""
        self._catalog.attach_retrieval(self._alias.get(source_id, source_id), record, self._trust,
                                       self._agent.today())
        return self

    def add_independence_attestation(self, ia) -> "BriefBuilder":
        """Record a reviewer-signed statement that the named sources have independent origins."""
        self.brief.independence_attestations.append(ia)
        return self

    def attach_evergreen_attestation(self, claim_id: str, attestation: Attestation) -> "BriefBuilder":
        next(c for c in self.brief.claims if c.id == claim_id).evergreen_attestation = attestation
        return self

    def attach_kind_attestation(self, claim_id: str, attestation: Attestation) -> "BriefBuilder":
        next(c for c in self.brief.claims if c.id == claim_id).kind_attestation = attestation
        return self

    def attach_risk_downgrade_attestation(self, claim_id: str, attestation: Attestation) -> "BriefBuilder":
        next(c for c in self.brief.claims if c.id == claim_id).risk_downgrade_attestation = attestation
        return self

    def reclassify_claim(self, claim_id: str, new_kind: ClaimKind) -> "BriefBuilder":
        """Change a claim's kind. A FACT turned into an interpretation/opinion/hypothesis is RECORDED
        (original_kind) and stays invalid until a reviewer attests the reclassification."""
        c = next(c for c in self.brief.claims if c.id == claim_id)
        if c.kind is ClaimKind.FACT and new_kind is not ClaimKind.FACT and c.original_kind is None:
            c.original_kind = ClaimKind.FACT
        c.kind = new_kind
        return self

    def set_dissent_waiver(self, claim_id: str, reason: str) -> "BriefBuilder":
        next(c for c in self.brief.claims if c.id == claim_id).dissent_waiver = reason
        return self

    def attach_citation_attestation(self, claim_id: str, source_id: str, attestation: Attestation) -> "BriefBuilder":
        """Attach an attestation issued on the review side for this exact citation. The review LABEL is
        set from the attestation kind, but the label counts only if the attestation verifies at build time
        (otherwise validation reports ATTESTATION_INVALID / REVIEW_UNATTESTED)."""
        sid = self._alias.get(source_id, source_id)
        claim = next(c for c in self.brief.claims if c.id == claim_id)
        cit = next(x for x in claim.citations if x.source_id == sid)
        cit.attestation = attestation
        cit.review = ReviewStatus.HUMAN_REVIEWED if attestation.kind == "human" else ReviewStatus.PROCESS_VERIFIED
        cit.reviewed_by = attestation.attestor
        return self

    def add_perspective(self, p: Perspective) -> "BriefBuilder":
        self.brief.perspectives.append(p)
        return self

    def add_uncertainty(self, u: Uncertainty) -> "BriefBuilder":
        self.brief.uncertainties.append(u)
        return self

    # --- creative hand-off parts
    def add_angle(self, a: CreativeAngle) -> "BriefBuilder":
        self.brief.angles.append(a)
        return self

    def add_hook(self, h: HookPayoff) -> "BriefBuilder":
        self.brief.hooks.append(h)
        return self

    def add_visual(self, v: VisualOpportunity) -> "BriefBuilder":
        v.reference_source_ids = [self._alias.get(x, x) for x in v.reference_source_ids]
        self.brief.visuals.append(v)
        return self

    def add_footage(self, f: FootageConnection) -> "BriefBuilder":
        self.brief.footage.append(f)
        return self

    def set_why_now(self, text: str) -> "BriefBuilder":
        self.brief.why_now = text
        return self

    def set_format_study(self, fs: FormatStudy) -> "BriefBuilder":
        fs.reference_source_id = self._alias.get(fs.reference_source_id, fs.reference_source_id)
        self.brief.format_study = fs
        return self

    # --- finish
    def build(self, reference_texts=()) -> tuple[ResearchBrief, ValidationReport]:
        b, today, trust = self.brief, self._agent.today(), self._trust
        _ledger.record(b)                                   # nothing assessed below is outside the ledger
        for c in b.claims:                                  # (a manually set `controversial` flag is never reset)
            a = assess_claim(c, b.sources, today=today, topic_kind=b.topic_kind, trust=trust,
                             independence=b.independence_attestations, ledger=b.evidence_ledger,
                             resolutions=b.resolutions)
            c.status = a.status
            c.status_reasons = list(a.reasons)
            c.evidence_route = a.route
            # derived caveats are rewritten on every build; hand-written ones are kept
            c.caveats = [x for x in c.caveats if not x.startswith(AUTO_CAVEAT)] + [AUTO_CAVEAT + x for x in a.caveats]
            if a.status is ClaimStatus.DISPUTED:
                b.controversial = True
        ind = independence_groups(b.sources, b.independence_attestations, trust, today)
        b.assessed_under = trust.fingerprint()
        b.independence = [IndependenceGroup(g, ids, ind.reasons[g]) for g, ids in sorted(ind.members.items())]
        # Freshness: the triggering event date if known, else the newest credible (non-D) source.
        tiers = assess_all(b.sources, trust, today)
        newest = self._event_date or newest_partial_date(
            s.published for s in b.sources if tiers[s.id].tier is not QualityTier.D)
        ev = evidence_from_claims(b.claims)
        reliability = reliability_from_claims(b.claims)
        if ev is None:
            ev = evidence_from_sources(b.sources, trust, today)
            reliability = lead_reliability(ev)
        b.relevance = score_topic(
            ScoreInputs(self._category, " ".join([b.topic, *self._keywords]), b.topic_kind, newest,
                        self._signals, ev, reliability), self._agent.interests, today, self._agent.weights)
        report = validate_brief(b, today, reference_texts, trust=trust)
        b.status = BriefStatus.VALIDATED if report.ok else BriefStatus.DRAFT
        return b, report
