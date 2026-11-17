"""EditorView: the Editor-facing hand-off, with its own versioned schema and validator.

This is STORY GUIDANCE for a future Editor Agent. It is not the shared edit-plan schema: no frames,
tracks, transitions or cut decisions. It reuses the brief's own dataclasses for every shared concept, so a
reference, a moment or a research claim means the same thing in both documents and is validated by the
same rules (safety.Checker).

What it carries so downstream editing can be responsible: the chosen story (arc or short concept), the
central idea and audience, hooks and their payoff references, beginning/ending options, every moment the
story touches (with its transcript excerpt and warnings), pauses to preserve, callbacks, research claims
with their category and provenance, Creator DNA considerations (informational), all relevant issues,
filming guidance (clearly not footage), warnings, and whether the transcript evidence was re-verified.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import schema as S
from .inputs import Material
from .safety import Checker, check_arc, check_hook_claims, check_research_contract, check_short, _segset
from .schema import (Arc, Audience, BoundaryOption, Callback, CentralIdea, GuidanceItem, Hook, Issue, Moment, Pause,
                     ResearchItem, ShortConcept, StoryBrief, StyleConsideration, to_plain)
from .serialize import from_plain
from .textsafety import norm

EDITOR_NOTICE = ("Story guidance, not an edit plan. The Editor decides every cut and may not write or alter "
                 "speech. Times are source seconds; frame conversion is the Editor's job.")
EDITOR_CONSTRAINTS = (
    "never write or rewrite creator dialogue; the transcript is the only source of creator speech",
    "preserve the pauses and notes listed under 'preserve' unless audio shows dead air",
    "keep setup before payoff",
    "claims in research_context are context, not creator statements; respect each claim's usage rule",
    "filming_guidance is future, optional material and not existing footage",
    "creator_dna considerations are informational and do not dictate cuts",
    "if evidence_verified is false, re-verify every reference against the transcript before editing",
)
KINDS = ("long_form", "short_form")


@dataclass(frozen=True)
class EditorView:
    contract: str
    brief_id: str
    brief_schema: str
    kind: str                                   # long_form | short_form
    notice: str
    constraints: tuple[str, ...]
    evidence_verified: bool                     # True only if the brief was re-verified against the transcript
    material_digest: dict[str, Any]
    central_idea: CentralIdea | None
    audience: Audience
    recommended_arc_id: str | None
    recommendation_basis: str
    arc: Arc | None
    short_concept: ShortConcept | None
    hooks: tuple[Hook, ...]
    beginnings: tuple[BoundaryOption, ...]
    endings: tuple[BoundaryOption, ...]
    moments: tuple[Moment, ...]
    pauses: tuple[Pause, ...]
    callbacks: tuple[Callback, ...]
    research_context: tuple[ResearchItem, ...]
    research_meta: dict[str, Any]
    style_considerations: tuple[StyleConsideration, ...]
    dna_notice: str
    issues: tuple[Issue, ...]
    open_blockers: tuple[str, ...]
    filming_guidance: tuple[GuidanceItem, ...]
    warnings: tuple[str, ...]
    confidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


def build_editor_view(brief: StoryBrief, *, arc_id: str | None = None, concept_id: str | None = None,
                      evidence_verified: bool = False) -> EditorView:
    if brief.status != "ok":
        raise ValueError("this brief has no usable plan (insufficient material)")
    arc = concept = None
    if concept_id is not None:
        concept = next((c for c in brief.short_concepts if c.concept_id == concept_id), None)
        if concept is None:
            raise KeyError(f"unknown short concept {concept_id!r}")
        kind, story_ids, segs = "short_form", set(concept.moment_ids), set(concept.ranges[0].segment_ids)
        for r in concept.ranges[1:]:
            segs |= set(r.segment_ids)
    else:
        aid = arc_id or brief.recommended_arc_id
        arc = next((a for a in brief.arcs if a.arc_id == aid), None)
        if arc is None:
            raise KeyError(f"unknown arc {aid!r}")
        kind = "long_form"
        story_ids = {m for s in arc.sections for m in s.moment_ids}
        segs = {i for s in arc.sections for u in s.ranges for i in u.ref.segment_ids}
    # hooks belonging to this arc: the teaser/opening section reuses a moment, and the hook is about it
    hooks: tuple[Hook, ...] = ()
    if arc and arc.sections and any(u.reused for u in arc.sections[0].ranges):
        first = set(arc.sections[0].moment_ids)
        hooks = tuple(h for h in brief.hooks if set(h.moment_ids) == first)
    callbacks = tuple(c for c in brief.callbacks if c.setup_moment_id in story_ids or c.payoff_moment_id in story_ids)
    ids = set(story_ids)
    for c in callbacks:
        ids |= {c.setup_moment_id, c.payoff_moment_id}
    for h in hooks:
        ids |= set(h.moment_ids) | set(h.promise_delivered_by)
    if concept:
        ids |= {x for x in (concept.hook_moment_id, concept.payoff_moment_id) if x}
    begin = brief.beginnings if kind == "long_form" else ()
    end = brief.endings if kind == "long_form" else ()
    for o in (*begin, *end):
        ids |= set(o.moment_ids)
    if brief.central_idea:
        ids |= set(brief.central_idea.supporting_moment_ids)

    def relevant(i: Issue) -> bool:
        return i.severity in ("blocking", "high") or not i.moment_ids or bool(set(i.moment_ids) & story_ids)
    issues = tuple(i for i in brief.issues if relevant(i))
    for i in issues:
        ids |= set(i.moment_ids)
    issue_ids = {i.issue_id for i in issues}
    guidance = tuple(g for g in brief.filming_guidance
                     if (g.issue_id in issue_ids) or (set(g.related_moment_ids) & story_ids)
                     or (not g.related_moment_ids and g.issue_id is None))
    for g in guidance:
        ids |= set(g.related_moment_ids)
    # guidance may point at an issue that is not otherwise relevant: keep it resolvable
    extra = {g.issue_id for g in guidance if g.issue_id and g.issue_id not in issue_ids}
    issues = issues + tuple(i for i in brief.issues if i.issue_id in extra)
    for i in issues:
        ids |= set(i.moment_ids)
    moments = tuple(m for m in brief.moments if m.moment_id in ids)
    pauses = tuple(p for p in brief.pauses if p.before_segment in segs and p.after_segment in segs)
    return EditorView(
        S.EDITOR_VIEW_CONTRACT, brief.brief_id, brief.schema_version, kind, EDITOR_NOTICE, EDITOR_CONSTRAINTS,
        evidence_verified, dict(brief.material_digest), brief.central_idea, brief.audience,
        brief.recommended_arc_id, brief.recommendation_basis, arc, concept, hooks, tuple(begin), tuple(end), moments,
        pauses, callbacks, brief.research_context, dict(brief.research_meta), brief.style_considerations,
        brief.dna_notice, issues, tuple(i.issue_id for i in issues if i.severity in ("blocking", "high")),
        guidance, brief.warnings, dict(brief.confidence))


def view_cross(c: Checker, d: dict, brief: StoryBrief | None) -> None:
    if d["contract"] != S.EDITOR_VIEW_CONTRACT:
        c.err("/contract", f"{d['contract']!r} != {S.EDITOR_VIEW_CONTRACT!r}")
    if d["brief_schema"] != S.SCHEMA_VERSION:
        c.err("/brief_schema", f"{d['brief_schema']!r} != {S.SCHEMA_VERSION!r}")
    if d["notice"] != EDITOR_NOTICE:
        c.err("/notice", "must carry the editor notice")
    if tuple(d["constraints"]) != EDITOR_CONSTRAINTS:
        c.err("/constraints", "must carry the standard constraints")
    if d["dna_notice"] != S.DNA_NOTICE:
        c.err("/dna_notice", "must carry the Creator DNA informational notice")
    if d["kind"] not in KINDS:
        c.err("/kind", f"must be one of {KINDS}")
    elif (d["kind"] == "long_form") != (d["arc"] is not None) or \
            (d["kind"] == "short_form") != (d["short_concept"] is not None):
        c.err("/kind", "a long_form view carries exactly one arc and a short_form view exactly one short concept")
    if d["kind"] == "short_form" and (d["hooks"] or d["beginnings"] or d["endings"] or d["arc"]):
        c.err("/kind", "a short_form view carries no long-form hooks, boundaries or arc")
    if d["evidence_verified"] is True and c.material is None:
        c.err("/evidence_verified", "claims verified evidence but no transcript was supplied to re-verify it")
    c.digest(d["material_digest"], "/material_digest")
    if d["confidence"].keys() != {"overall", "central_idea", "median_moment", "basis"}:
        c.err("/confidence", "unexpected keys")
    mids = c.unique(d["moments"], "moment_id", "/moments")
    byid = {m["moment_id"]: m for m in d["moments"]}
    issue_ids = c.unique(d["issues"], "issue_id", "/issues")
    c.unique(d["research_context"], "claim_id", "/research_context")
    c.unique(d["callbacks"], "callback_id", "/callbacks")
    c.unique(d["filming_guidance"], "guidance_id", "/filming_guidance")
    c.unique(d["hooks"], "hook_id", "/hooks")
    if d["central_idea"]:
        c.need(d["central_idea"]["supporting_moment_ids"], mids, "/central_idea")
    for cb in d["callbacks"]:
        c.need([cb["setup_moment_id"], cb["payoff_moment_id"]], mids, "/callbacks")
    check_research_contract(c, d)
    for h in d["hooks"]:
        c.need(h["moment_ids"] + h["promise_delivered_by"], mids, "/hooks")
        check_hook_claims(c, h, f"/hooks[{h['hook_id']}]", d["research_context"])
    for o in (*d["beginnings"], *d["endings"]):
        c.need(o["moment_ids"], mids, "/boundaries")
    for i in d["issues"]:
        c.need(i["moment_ids"], mids, f"/issues[{i['issue_id']}]")
    for g in d["filming_guidance"]:
        c.need(g["related_moment_ids"], mids, "/filming_guidance")
        if g["issue_id"] is not None and g["issue_id"] not in issue_ids:
            c.err("/filming_guidance", f"unknown issue id {g['issue_id']!r}")
    expect = [i["issue_id"] for i in d["issues"] if i["severity"] in ("blocking", "high")]
    if sorted(d["open_blockers"]) != sorted(expect):
        c.err("/open_blockers", "must list exactly the blocking and high-severity issues in this view")
    if d["arc"] is not None:
        check_arc(c, d["arc"], d, byid, mids)
    if d["short_concept"] is not None:
        check_short(c, d["short_concept"], byid, mids)
    segs = _segset(u["ref"] for s in (d["arc"] or {"sections": []})["sections"] for u in s["ranges"])
    if d["short_concept"] is not None:
        segs |= _segset(d["short_concept"]["ranges"])
    for p in d["pauses"]:
        if p["before_segment"] not in segs or p["after_segment"] not in segs:
            c.err("/pauses", f"pause {p['pause_id']} is outside the story's ranges")
    if brief is not None:
        _against_brief(c, d, brief)


def _against_brief(c: Checker, d: dict, brief: StoryBrief) -> None:
    b = brief.to_dict()
    if d["brief_id"] != b["brief_id"] or d["material_digest"] != b["material_digest"]:
        c.err("/brief_id", "does not belong to this brief (id or transcript digest differs)")
    if d["arc"] is not None and d["arc"] not in b["arcs"]:
        c.err("/arc", "differs from the arc in the brief")
    if d["short_concept"] is not None and d["short_concept"] not in b["short_concepts"]:
        c.err("/short_concept", "differs from the short concept in the brief")
    for key in ("research_context", "style_considerations"):
        if d[key] != b[key]:
            c.err(f"/{key}", "differs from the brief")
    for key, bkey in (("moments", "moments"), ("issues", "issues"), ("callbacks", "callbacks"),
                      ("hooks", "hooks"), ("filming_guidance", "filming_guidance"), ("pauses", "pauses")):
        for x in d[key]:
            if x not in b[bkey]:
                c.err(f"/{key}", "contains an entry that is not in the brief (or was altered)")
                break
    if d["central_idea"] != b["central_idea"] or d["audience"] != b["audience"]:
        c.err("/central_idea", "differs from the brief")
    if d["recommended_arc_id"] != b["recommended_arc_id"]:
        c.err("/recommended_arc_id", "differs from the brief")


def validate_editor_view_dict(d: Any, *, brief: StoryBrief | None = None,
                              material: Material | None = None) -> list[str]:
    """Problems with an editor-view document, or []. With `material` the evidence is re-verified; with
    `brief` the view is checked against the brief it claims to come from."""
    c = Checker(material)
    try:
        if not isinstance(d, dict):
            return ["/: an editor view must be an object"]
        rc = d.get("research_context")
        if isinstance(rc, list):
            c.research_texts = [norm(r["text"]) for r in rc if isinstance(r, dict) and isinstance(r.get("text"), str)
                                and len(norm(r["text"])) >= 20]
        c.walk(d, EditorView, "")
        if not c.errs:
            view_cross(c, d, brief)
    except Exception as e:
        c.errs.append(f"/: validator could not process this document ({type(e).__name__}); treated as invalid")
    return c.errs


def editor_view_from_dict(d: Any, *, brief: StoryBrief | None = None, material: Material | None = None) -> EditorView:
    from .director import BriefInvalid
    problems = validate_editor_view_dict(d, brief=brief, material=material)
    if problems:
        raise BriefInvalid(problems)
    return from_plain(EditorView, d)
