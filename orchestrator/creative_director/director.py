"""CreativeDirector: analysis -> structure -> story brief, plus loading and the Editor-facing view.

Pure and offline: no network, no files written, no Resolve, no other subsystem modified.

Creator DNA status: INFORMATIONAL. `plan(dna=...)` copies labelled considerations into the brief
(`style_considerations`, with `eligibility`). They do not control arcs, hooks, short-form concepts, preserve
notes or guidance (schema.DNA_NOTICE). Confidence-weighted structural influence is a later integration stage.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from typing import Any

from . import schema as S
from .analysis import Analysis, analyze
from .editor_view import EditorView, build_editor_view, validate_editor_view_dict
from .inputs import CreatorProfileView, Material, ResearchContext, material_digest
from .inputs import compute_research_digest
from .safety import validate_brief_dict
from .schema import (AUTHENTICITY_STATEMENT, SCHEMA_VERSION, GuidanceItem, Issue, Mode, MomentKind,
                     ResearchCategory, SourceSummary, StoryBrief, to_plain)
from .serialize import from_plain
from .structure import DirectorConfig, build_arcs, build_hooks, build_short_concepts


class BriefInvalid(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
        self.problems = problems


@dataclass(frozen=True)
class LoadedBrief:
    """A stored brief that passed validation. `evidence_verified` is True only when the transcript was
    supplied in the same call and every reference, excerpt and the digest matched."""
    brief: StoryBrief
    evidence_verified: bool


class CreativeDirector:
    def __init__(self, config: DirectorConfig | None = None):
        self.cfg = config or DirectorConfig()

    # ------------------------------------------------------------------------------------------ plan
    def plan(self, material: Material, *, mode: Mode | str = Mode.BOTH, dna: CreatorProfileView | None = None,
             research: ResearchContext | None = None, brief_id: str = "brief-1") -> StoryBrief:
        mode = Mode(mode)
        a = analyze(material, research_audience=research.audience_description if research else None)
        iss: list[Issue] = []
        n = [0]

        def issue(sev: str, kind: str, text: str, ms: tuple[str, ...] = (), needs: str = "") -> str:
            n[0] += 1
            iss.append(Issue(f"i{n[0]}", sev, kind, text, ms, needs))
            return f"i{n[0]}"

        warnings = list(a.warnings)
        for r in a.rejected:
            issue("low", "input_rejected", f"{r['input']} rejected: {r['reason']}", needs="fix or drop the input")
            warnings.append(f"rejected input: {r['input']}")
        if not a.moments or not any(s.text for s in material.segments):
            issue("blocking", "insufficient_material", "not enough transcript material to plan a story",
                  needs="supply transcript segments (reviewed if possible)")
            return self._finish(material, a, mode, brief_id, iss, [], [], None, "", [], [], dna, research, [],
                                warnings, status="insufficient_material")

        # ---- analysis-level issues
        if a.central_idea and a.central_idea.detected_by == "heuristic":
            issue("medium", "central_idea_unconfirmed", "the central idea is a heuristic guess from recurring words",
                  needs="creator or annotator to state what the video is about")
        if a.audience.basis == "unresolved":
            issue("medium", "audience_unresolved", "no audience or audience promise has been established",
                  needs="creator, or a research brief that names the audience")
        elif a.audience.basis == "research_brief":
            issue("low", "audience_from_research", "the audience comes from the research brief and is unconfirmed",
                  needs="creator confirmation")
        elif not a.audience.promise:
            issue("low", "promise_missing", "an audience is named but no audience promise")
        if any(s.verification != "reviewed" for s in material.segments if s.text):
            issue("medium", "unreviewed_transcript", "some transcript text is unreviewed ASR; excerpts may differ "
                  "from what was said", needs="human transcript review before quoting or captioning")
        for f in a.findings:
            if f.kind == "missing_context":
                issue("low" if f.confidence < 0.5 else "medium", "missing_context", f.text, f.moment_ids,
                      "creator (pickup) or editor judgement")
        for m in a.moments:
            if m.attribution != "creator":
                warnings.append(f"{m.moment_id}: speech not attributed to the creator ({m.attribution})")

        # ---- research reconciliation (research is context; it never becomes a creator statement)
        r_items = list(research.items) if research else []
        if research:
            warnings.extend(research.warnings)
        by_claim: dict[str, list[tuple[Any, str]]] = {}
        for cl, mid in a.claim_moments:
            by_claim.setdefault(cl.claim_id, []).append((cl, mid))
            item = research.item(cl.claim_id) if research else None
            if item is None:
                issue("medium", "unknown_claim", f"{mid} is linked to claim {cl.claim_id}, which is not in the "
                      "supplied research" if research else f"{mid} is linked to claim {cl.claim_id} but no "
                      "research was supplied", (mid,), "supply the research brief or fix the link")
                continue
            if cl.relation != "asserts":
                continue
            cat = item.category
            if cat in (ResearchCategory.CONTRADICTED, ResearchCategory.DISPUTED, ResearchCategory.UNVERIFIED_FACT,
                       ResearchCategory.PARTIALLY_SUPPORTED):
                issue("high" if (cat is ResearchCategory.CONTRADICTED or item.restricted) else "medium",
                      "creator_claim_not_supported",
                      f"the creator's span {mid} asserts claim {item.claim_id}, which research marks as "
                      f"{cat.value}; the words are left untouched",
                      (mid,), "creator decision: leave as opinion, add a sourced correction card, or re-record")
        for cid, links in by_claim.items():
            rel = {cl.relation for cl, _ in links}
            if "asserts" in rel and "disputes" in rel:
                mids = tuple(mid for _, mid in links)
                issue("high", "creator_statements_conflict",
                      f"spans {', '.join(mids)} take opposing positions on claim {cid}; may be a change of mind, "
                      "a joke or a quoted view. Not resolved automatically.", mids, "creator to clarify intent")
        if research:
            for s in research.suggestions:
                if s.get("type") == "uncertainty" and s.get("kind") in ("gap", "disagreement"):
                    issue("low", "research_uncertainty", f"research notes a {s.get('kind')}: {s.get('text')}",
                          needs="research or creator")
            for ri in research.issues:
                issue(ri["severity"], ri["kind"], ri["text"], needs="research agent to fix the source records")
            for it in research.items:
                if it.conflicts:
                    issue("medium", "research_conflict", f"claim {it.claim_id}: {it.conflicts[0]}",
                          needs="research agent to resolve the conflicting entries")

        # ---- structure
        arcs, arc_notes, rec, basis = ([], [], None, "") if mode is Mode.SHORT_FORM else build_arcs(a, self.cfg)
        for note in arc_notes:
            warnings.append(f"arc '{note['arc']}' not offered: {note['reason']}")
        hooks = build_hooks(a, arcs, self.cfg) if arcs else []
        shorts, short_notes = (build_short_concepts(a, self.cfg) if mode in (Mode.SHORT_FORM, Mode.BOTH)
                               else ([], []))
        warnings.extend(short_notes)
        if mode in (Mode.SHORT_FORM, Mode.BOTH) and not shorts:
            issue("low", "no_short_concept", "the material does not support a standalone short-form concept",
                  needs="more material or a pickup")
        if mode is not Mode.SHORT_FORM and arcs and not hooks:
            issue("low", "no_footage_hook", "no hook grounded in the footage was found; see filming guidance",
                  needs="creator pickup or editor judgement")
        for sc in shorts:
            sev = "low" if sc.standalone.context_assessment == "none_detected" else "medium"
            for p in sc.standalone.issues:
                issue(sev, "short_not_standalone", f"{sc.concept_id}: {p}", sc.moment_ids)
            for u in sc.standalone.uncertainties:
                issue("low", "short_uncertainty", f"{sc.concept_id}: {u}", sc.moment_ids,
                      "human judgement before editing")

        guidance = self._guidance(a, iss)
        return self._finish(material, a, mode, brief_id, iss, arcs, hooks, rec, basis, shorts, guidance, dna,
                            research, r_items, warnings)

    # ------------------------------------------------------------------------------------------
    def _guidance(self, a: Analysis, iss: list[Issue]) -> list[GuidanceItem]:
        out: list[GuidanceItem] = []

        def add(kind: str, reason: str, points: tuple[str, ...], ms: tuple[str, ...], issue_id: str | None) -> None:
            out.append(GuidanceItem(f"g{len(out) + 1}", kind, reason, points, ms, issue_id))
        for i in iss:
            if i.kind == "missing_context":
                add("context_pickup", i.text,
                    ("explain what the opening refers to, as you would to someone who just arrived",
                     "name the situation, place or object involved"), i.moment_ids, i.issue_id)
            elif i.kind == "short_not_standalone":
                add("context_pickup", f"{i.text}: a short opening that gives context may let this clip stand alone",
                    ("say in your own words what the viewer needs to know first",), i.moment_ids, i.issue_id)
            elif i.kind in ("creator_claim_not_supported", "creator_statements_conflict"):
                add("clarification_pickup", i.text,
                    ("state your actual position and why",
                     "say whether this was a joke, a quote or a change of mind"), i.moment_ids, i.issue_id)
        for f in a.findings:
            if f.kind == "missing_context" and "question is the last thing" in f.text:
                add("answer_pickup", f.text, ("answer the question you raised, in your own words",),
                    f.moment_ids, None)
        for m in a.moments:
            if m.kind is MomentKind.DEMONSTRATION:
                add("demonstration_check",
                    f"{m.moment_id} sounds like a demonstration, but nothing here shows whether it is visible",
                    ("confirm the thing being shown is clearly on camera",
                     "consider a close-up or a labelled diagram if it is not"), (m.moment_id,), None)
        seen, uniq = set(), []
        for g in out:
            k = (g.kind, g.related_moment_ids, g.reason)
            if k not in seen:
                seen.add(k)
                uniq.append(g)
        return [GuidanceItem(f"g{i + 1}", g.kind, g.reason, g.talking_points, g.related_moment_ids, g.issue_id)
                for i, g in enumerate(uniq)]

    def _finish(self, material, a, mode, brief_id, iss, arcs, hooks, rec, basis, shorts, guidance, dna, research,
                r_items, warnings, status="ok") -> StoryBrief:
        src = []
        for sid, segs in a.index.by_source.items():
            src.append(SourceSummary(sid, len(segs), segs[0].start_s, segs[-1].end_s,
                                     sum(s.verification == "reviewed" for s in segs),
                                     sum(s.verification != "reviewed" for s in segs)))
        style, dna_warn = [], []
        if dna:
            style = dna.considerations()
            dna_warn = list(dna.warnings)
        mconf = [m.confidence for m in a.moments]
        cidea = a.central_idea.confidence if a.central_idea else 0.0
        overall = round(min(cidea, statistics.median(mconf)), 3) if mconf else 0.0
        conf = {"overall": overall, "central_idea": cidea,
                "median_moment": round(statistics.median(mconf), 3) if mconf else None,
                "basis": "min(central idea, median moment confidence); a rough indicator, not a probability"}
        brief = StoryBrief(
            schema_version=SCHEMA_VERSION, brief_id=brief_id, mode=mode, project_id=material.project_id,
            status=status, sources=tuple(src), central_idea=a.central_idea, audience=a.audience,
            moments=tuple(a.moments), pauses=tuple(a.pauses), callbacks=tuple(a.callbacks),
            findings=tuple(a.findings), beginnings=tuple(a.beginnings), endings=tuple(a.endings),
            arcs=tuple(arcs), recommended_arc_id=rec, recommendation_basis=basis, hooks=tuple(hooks),
            short_concepts=tuple(shorts), style_considerations=tuple(style), research_context=tuple(r_items),
            research_suggestions=tuple(research.suggestions) if research else (),
            filming_guidance=tuple(guidance), rejected_inputs=tuple(a.rejected), issues=tuple(iss),
            warnings=tuple(dict.fromkeys(warnings + dna_warn)), confidence=conf,
            authenticity=dict(AUTHENTICITY_STATEMENT), material_digest=material_digest(material),
            research_meta=dict(research.meta) if research else {},
            research_digest=compute_research_digest(research.meta.get("contract") if research else None,
                                                  [to_plain(i) for i in r_items]),
            dna_notice=S.DNA_NOTICE)
        problems = validate_brief_dict(brief.to_dict(), material)
        if problems:   # a planner bug, never silently emitted
            raise BriefInvalid(problems)
        return brief

    # ------------------------------------------------------------------------------------------ validate / load
    @staticmethod
    def validate(brief: StoryBrief | dict, material: Material | None = None) -> list[str]:
        """Problems with a brief (object or dict). Without `material` only structure and internal
        consistency are checked: that is NOT evidence verification."""
        return validate_brief_dict(brief.to_dict() if isinstance(brief, StoryBrief) else brief, material)

    @staticmethod
    def load(data: dict | str, material: Material | None = None) -> LoadedBrief:
        """Load a stored brief (dict or JSON text). It is fully revalidated first; invalid or tampered
        documents raise BriefInvalid. Pass the transcript to also verify every reference, excerpt and the
        digest (then `evidence_verified` is True)."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError as e:
                raise BriefInvalid([f"/: not valid JSON ({e})"]) from None
        problems = validate_brief_dict(data, material)
        if problems:
            raise BriefInvalid(problems)
        return LoadedBrief(from_plain(StoryBrief, data), material is not None)

    @staticmethod
    def to_json(brief: StoryBrief) -> str:
        return json.dumps(brief.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)

    # ------------------------------------------------------------------------------------------ editor view
    @staticmethod
    def editor_view_object(brief: StoryBrief | LoadedBrief, *, arc_id: str | None = None,
                           concept_id: str | None = None, material: Material | None = None) -> EditorView:
        """Build the Editor-facing view. The brief is revalidated first (against the transcript when given,
        which also sets `evidence_verified`). The result is validated against its own schema before return."""
        b = brief.brief if isinstance(brief, LoadedBrief) else brief
        problems = validate_brief_dict(b.to_dict(), material)
        if problems:
            raise BriefInvalid(problems)
        view = build_editor_view(b, arc_id=arc_id, concept_id=concept_id, evidence_verified=material is not None)
        problems = validate_editor_view_dict(view.to_dict(), brief=b, material=material)
        if problems:
            raise BriefInvalid(problems)
        return view

    @staticmethod
    def editor_view(brief: StoryBrief | LoadedBrief, *, arc_id: str | None = None, concept_id: str | None = None,
                    material: Material | None = None) -> dict:
        return CreativeDirector.editor_view_object(brief, arc_id=arc_id, concept_id=concept_id,
                                                   material=material).to_dict()
