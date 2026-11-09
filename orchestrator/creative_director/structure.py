"""Story structure: candidate arcs, grounded hooks, standalone short-form concepts.

Arcs are PROPOSALS. The chronological arc is always offered because it moves nothing; other arcs are
offered only when the material gives a reason. No arc is a formula applied to every video, and none
creates or alters speech: sections only point at transcript spans.
"""
from __future__ import annotations

from dataclasses import dataclass

from .analysis import Analysis, depends_on_context, terms
from .schema import (Arc, Function, Hook, Moment, MomentKind, RangeUse, Section, ShortConcept, SourceRef,
                     StandaloneCheck, SUGGESTED)


@dataclass(frozen=True)
class DirectorConfig:
    """Defaults chosen by the implementer for this foundation, NOT creator decisions or platform facts."""
    section_gap_s: float = 60.0         # a bigger gap between moments starts a new section
    min_short_s: float = 5.0
    max_short_s: float = 60.0
    reaction_beat_s: float = 3.0        # include a following segment this close to a joke/reveal
    max_short_concepts: int = 5
    cold_open_min_position: float = 0.25    # only reuse a moment as a teaser if it sits after this fraction
    delayed_reaction_s: float = 8.0     # a reaction this far after a payoff is included (if known) or flagged
    setup_gap_s: float = 4.0            # preceding line this close to a joke is taken as its probable setup
    answer_gap_s: float = 6.0           # speech this soon after a question is treated as its answer
    max_answer_segments: int = 3


class _Ids:
    def __init__(self, p: str):
        self.p, self.n = p, 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.p}{self.n}"


# ----------------------------------------------------------------------------- clusters

def _clusters(a: Analysis, cfg: DirectorConfig) -> list[list[Moment]]:
    out: list[list[Moment]] = []
    for m in a.moments:
        if out and out[-1][-1].ref.source_id == m.ref.source_id and \
                m.ref.start_s - out[-1][-1].ref.end_s <= cfg.section_gap_s:
            out[-1].append(m)
        else:
            out.append([m])
    return out


def _cluster_ref(a: Analysis, ms: list[Moment]) -> SourceRef:
    ref = ms[0].ref
    for m in ms[1:]:
        ref = a.index.ref_between(ref, m.ref)
    return ref


def _preserve(a: Analysis, ms: list[Moment], ref: SourceRef) -> tuple[str, ...]:
    out: list[str] = []
    ids = set(ref.segment_ids)
    for p in a.pauses:
        if p.before_segment in ids or p.after_segment in ids:
            out.append(f"pause {p.pause_id} ({p.duration_s:.1f}s): keep unless audio shows it is dead air")
    for m in ms:
        if m.kind is MomentKind.HUMOR_OR_REACTION:
            out.append(f"{m.moment_id}: keep the setup, the beat before and the reaction after; do not cut into it")
        if m.kind in (MomentKind.CALLBACK_SETUP, MomentKind.CALLBACK_PAYOFF):
            out.append(f"{m.moment_id}: callback element; keep setup before payoff and neither without context")
    return tuple(out)


def _function(ms: list[Moment], last: bool) -> Function:
    kinds = {m.kind for m in ms}
    if MomentKind.CALLBACK_PAYOFF in kinds:
        return Function.PAYOFF
    if kinds & {MomentKind.CALLBACK_SETUP, MomentKind.QUESTION}:
        return Function.SETUP
    return Function.RESOLUTION if last else Function.DEVELOPMENT


def _sections(a: Analysis, clusters: list[list[Moment]], sid: _Ids, why: str) -> list[Section]:
    secs = []
    for i, ms in enumerate(clusters):
        ref = _cluster_ref(a, ms)
        dom = max({m.kind for m in ms}, key=lambda k: sum(m.kind is k for m in ms)).value
        secs.append(Section(sid(), f"Part {i + 1}: {dom.replace('_', ' ')}", _function(ms, i == len(clusters) - 1),
                            f"carry the material around {', '.join(m.moment_id for m in ms)}",
                            tuple(m.moment_id for m in ms), (RangeUse(ref),), why, _preserve(a, ms, ref)))
    return secs


def _setup_before_payoff_ok(a: Analysis, secs: list[Section]) -> bool:
    pos = {}
    for i, s in enumerate(secs):
        for mid in s.moment_ids:
            pos.setdefault(mid, i)
    return all(pos.get(cb.setup_moment_id, -1) <= pos.get(cb.payoff_moment_id, 10**6) for cb in a.callbacks
               if cb.setup_moment_id in pos and cb.payoff_moment_id in pos)


# ----------------------------------------------------------------------------- arcs

def build_arcs(a: Analysis, cfg: DirectorConfig) -> tuple[list[Arc], list[dict], str | None, str]:
    """Returns (arcs, notes about arcs not offered, recommended arc id, recommendation basis)."""
    clusters = _clusters(a, cfg)
    if not clusters:
        return [], [], None, "no moments to structure"
    notes: list[dict] = []
    arcs: list[Arc] = []
    aid = _Ids("arc")
    multi = len({m.ref.source_id for m in a.moments}) > 1
    order_risk = ("sources are ordered by id, which is not a story decision; confirm the order",) if multi else ()

    # 1. chronological: always
    sid = _Ids("s1_")
    chrono = _sections(a, clusters, sid, "keeps the creator's own order, so context and timing are untouched")
    arcs.append(Arc(aid(), "chronological", "chronological",
                    "follow the material in the order it was recorded, grouped into sections around its moments",
                    "the order of events already carries the story, or nothing stronger is evident", tuple(chrono),
                    "moves nothing, so it cannot distort meaning or comedic timing; the baseline to beat",
                    ("may start slowly or bury the strongest moment",) + order_risk, 0.5, False))
    chrono_arc_id = arcs[0].arc_id

    strong = a.strongest(1)
    # 2. cold open from a real moment
    if strong:
        m = strong[0]
        first_start = a.index.segments_of(m.ref.source_id)[0].start_s
        last_end = a.index.segments_of(m.ref.source_id)[-1].end_s
        pos = (m.ref.start_s - first_start) / max(last_end - first_start, 1e-9)
        if m.kind is MomentKind.CALLBACK_PAYOFF:
            notes.append({"arc": "cold_open", "reason": f"strongest moment {m.moment_id} is a callback payoff; "
                                                        "opening on it would spoil the setup"})
        elif depends_on_context(m.excerpt):
            notes.append({"arc": "cold_open", "reason": f"{m.moment_id} may depend on earlier context"})
        elif m.kind is MomentKind.REVEAL:
            notes.append({"arc": "cold_open", "reason": f"{m.moment_id} is a reveal; showing it first would spoil it"})
        elif pos < cfg.cold_open_min_position:
            notes.append({"arc": "cold_open", "reason": f"{m.moment_id} already sits near the start; "
                                                        "a teaser would add nothing"})
        else:
            sid = _Ids("s2_")
            hook = Section(sid(), "Teaser", Function.HOOK,
                           f"show the {m.kind.value} at {m.moment_id} first, as a real preview",
                           (m.moment_id,), (RangeUse(m.ref, reused=True),),
                           "gives the viewer a reason to stay, taken from actual footage rather than invented",
                           _preserve(a, [m], m.ref))
            body = _sections(a, clusters, sid, "unchanged chronological order after the teaser")
            arcs.append(Arc(aid(), "cold_open", "non_linear",
                            f"open on {m.moment_id} (real footage), then continue chronologically",
                            "one moment is clearly stronger than the opening and understandable out of context",
                            (hook, *body),
                            "the teaser is only justified if the video then delivers on it in place",
                            ("the viewer sees this footage twice; the second showing needs a reason",
                             "a teaser that is not recontextualised is just a repeat",
                             "interest comes from the strongest moment, not from invented stimulation") + order_risk,
                            round(min(m.confidence, 0.5), 3), True))

    # 3. question-led
    q = next((m for m in a.moments if m.kind is MomentKind.QUESTION and not depends_on_context(m.excerpt)
              and a.index.next_segment(m.ref.segment_ids[-1]) is not None and
              m.ref.start_s > a.index.segments_of(m.ref.source_id)[0].start_s), None)
    if q:
        sid = _Ids("s3_")
        hook = Section(sid(), "Opening question", Function.SETUP,
                       f"open on the question at {q.moment_id}; the answer follows in place",
                       (q.moment_id,), (RangeUse(q.ref, reused=True),),
                       "an open question gives the viewer something to wait for, if the video answers it",
                       _preserve(a, [q], q.ref))
        body = _sections(a, clusters, sid, "unchanged chronological order after the opening question")
        arcs.append(Arc(aid(), "question_led", "non_linear",
                        f"open on the question at {q.moment_id}, then continue chronologically",
                        "a clear question appears mid-material and is followed by material that may answer it",
                        (hook, *body),
                        "curiosity is created from a real question in the footage",
                        ("whether the later material actually answers the question is NOT verified",
                         "taken out of place the question may lose its original context") + order_risk,
                        round(min(q.confidence, 0.4), 3), True))

    # 4. thematic regroup (non-linear), only if it actually reorders and is safe
    if len(clusters) >= 3:
        profile = [terms(" ".join(m.excerpt for m in c)) for c in clusters]
        groups: list[list[int]] = []
        for i, t in enumerate(profile):
            for g in groups:
                if t & profile[g[0]]:
                    g.append(i)
                    break
            else:
                groups.append([i])
        order = [i for g in groups for i in g]
        if order != sorted(order) and len(groups) < len(clusters):
            sid = _Ids("s4_")
            secs = _sections(a, [clusters[i] for i in order], sid,
                             "grouped with sections that share vocabulary, so related ideas sit together")
            if _setup_before_payoff_ok(a, secs):
                arcs.append(Arc(aid(), "thematic_regroup", "non_linear",
                                "regroup sections by shared vocabulary instead of recording order",
                                "the material wanders between topics and returns to them",
                                tuple(secs),
                                "related ideas are heard together; useful for tangent-heavy material",
                                ("shared words are weak evidence of a shared topic",
                                 "each section must still make sense when its original neighbours are gone",
                                 "regrouping can change what a statement seems to mean") + order_risk,
                                0.3, True))
            else:
                notes.append({"arc": "thematic_regroup", "reason": "regrouping would put a payoff before its setup"})
        elif order == sorted(order):
            notes.append({"arc": "thematic_regroup", "reason": "grouping by vocabulary changes nothing"})

    # recommendation: default to the least invasive structure unless the evidence is firm
    rec, basis = chrono_arc_id, ("chronological is the default: it distorts nothing. Other structures are offered "
                                 "as options, not as a formula")
    cold = next((x for x in arcs if x.name == "cold_open"), None)
    if cold and strong and strong[0].detected_by != "heuristic" and strong[0].confidence >= 0.6:
        rec = cold.arc_id
        basis = (f"cold_open recommended because {strong[0].moment_id} was judged by an annotator "
                 f"(confidence {strong[0].confidence}) and is understandable out of context; "
                 "chronological remains a valid alternative")
    return arcs, notes, rec, basis


# ----------------------------------------------------------------------------- hooks

def build_hooks(a: Analysis, arcs: list[Arc], cfg: DirectorConfig) -> list[Hook]:
    hid = _Ids("h")
    hooks: list[Hook] = []
    chrono = next((x for x in arcs if x.name == "chronological"), None)
    for arc in arcs:
        if arc.name not in ("cold_open", "question_led"):
            continue
        sec = arc.sections[0]
        m = a.moment(sec.moment_ids[0])
        later = ()
        if arc.name == "question_led":
            nxt = a.index.next_segment(m.ref.segment_ids[-1])
            later = tuple(x.moment_id for x in a.moments if x.ref.source_id == m.ref.source_id
                          and x.ref.start_s >= (nxt.start_s if nxt else 1e18))[:2]
            risks = ("no moment after the question is known to answer it" if not later else
                     "answer not verified; confirm before using this as a hook",)
        else:
            later = (m.moment_id,)
            risks = ("must not be used if the same footage gives away a payoff",)
        hooks.append(Hook(hid(), "existing_moment",
                          f"{m.kind.value.replace('_', ' ')} from source {m.ref.source_id} "
                          f"({m.ref.start_s:g}-{m.ref.end_s:g}s), shown before the main sequence",
                          (m.moment_id,), f"grounded in real footage ({m.moment_id}); " + sec.position_rationale,
                          later, risks, round(min(m.confidence, 0.5), 3)))
    return hooks


# ----------------------------------------------------------------------------- short-form

_PAYOFF_KINDS = {MomentKind.HUMOR_OR_REACTION, MomentKind.REVEAL, MomentKind.CALLBACK_PAYOFF,
                 MomentKind.CENTRAL_STATEMENT, MomentKind.DEMONSTRATION}
_HOOK_KINDS = (MomentKind.QUESTION, MomentKind.CENTRAL_STATEMENT, MomentKind.OPINION, MomentKind.DEMONSTRATION,
               MomentKind.CALLBACK_SETUP)


def build_short_concepts(a: Analysis, cfg: DirectorConfig) -> tuple[list[ShortConcept], list[str]]:
    """Returns (concepts, notes about candidates that were NOT offered and why).

    A concept is one contiguous, unreordered span of one source. Context, setup, answer and reaction
    decisions are WORD-LIST / TIMING HEURISTICS and are reported as such (StandaloneCheck.context_basis);
    whatever the planner could not decide is listed under `uncertainties`, never dropped silently.
    A candidate that has neither a hook nor a payoff is not offered."""
    ix = a.index
    cid = _Ids("sc")
    out: list[ShortConcept] = []
    notes: list[str] = []
    by_seg: dict[str, list[Moment]] = {}
    for mo in a.moments:
        for sid in mo.ref.segment_ids:
            by_seg.setdefault(sid, []).append(mo)

    def is_question(seg) -> bool:
        return seg.text.rstrip().endswith("?") or any(x.kind is MomentKind.QUESTION for x in by_seg.get(seg.segment_id, []))

    cands = sorted([m for m in a.moments if m.kind in _PAYOFF_KINDS | {MomentKind.QUESTION}],
                   key=lambda m: (m.detected_by == "heuristic", -m.confidence, m.ref.source_id, m.ref.start_s))
    for m in cands:
        if len(out) >= cfg.max_short_concepts:
            break
        segs = ix.segments_of(m.ref.source_id)
        n = len(segs)
        pos = {s.segment_id: i for i, s in enumerate(segs)}
        lo, hi = pos[m.ref.segment_ids[0]], pos[m.ref.segment_ids[-1]]
        issues: list[str] = []
        unc: list[str] = []
        warns: list[str] = []

        def gap(i: int, j: int) -> float:       # seconds between segment i end and segment j start
            return segs[j].start_s - segs[i].end_s

        # -- reaction after a joke / reveal / payoff: keep the beat, never drop a later reaction silently
        if m.kind in (MomentKind.HUMOR_OR_REACTION, MomentKind.REVEAL, MomentKind.CALLBACK_PAYOFF) and hi + 1 < n:
            g = gap(hi, hi + 1)
            if g <= cfg.reaction_beat_s:
                hi += 1
            elif g <= cfg.delayed_reaction_s:
                known = any(x.kind is MomentKind.HUMOR_OR_REACTION for x in by_seg.get(segs[hi + 1].segment_id, []))
                if known:
                    hi += 1
                    unc.append(f"includes a delayed reaction that starts {g:.1f}s after the payoff")
                else:
                    unc.append(f"a segment starts {g:.1f}s after the payoff and was not included; check whether "
                               "it is a delayed reaction")
        # -- a question needs its answer, which may span several segments
        taken = 0
        if m.kind is MomentKind.QUESTION:
            while (hi + 1 < n and taken < cfg.max_answer_segments and gap(hi, hi + 1) <= cfg.answer_gap_s
                   and not is_question(segs[hi + 1])
                   and segs[hi + 1].end_s - segs[lo].start_s <= cfg.max_short_s):
                hi += 1
                taken += 1
            if taken == 0:
                issues.append("no speech follows the question closely enough to be its answer in this window")
            else:
                unc.append(f"the answer is assumed to be the {taken} segment(s) after the question; its real "
                           "extent is unknown")
            if hi + 1 < n and gap(hi, hi + 1) <= cfg.answer_gap_s and not is_question(segs[hi + 1]):
                unc.append(f"the answer may continue beyond the window (more speech follows within "
                           f"{cfg.answer_gap_s:g}s)")
        # -- callback payoff needs its setup, even when that exceeds the duration limit
        cb = next((c for c in a.callbacks if c.payoff_moment_id == m.moment_id), None)
        if cb:
            sm = a.moment(cb.setup_moment_id)
            if sm.ref.source_id == m.ref.source_id:
                lo = min(lo, pos[sm.ref.segment_ids[0]])
            else:
                issues.append("the setup for this payoff is in another source and cannot be included in one "
                              "contiguous span")
        # -- humor without an annotated setup: flag, and take the adjacent preceding line as a probable setup
        if m.kind is MomentKind.HUMOR_OR_REACTION and not cb:
            if lo == 0:
                unc.append("humor at the start of the source: its setup, if any, is not in the material")
            elif gap(lo - 1, lo) <= cfg.setup_gap_s:
                lo -= 1
                unc.append("the preceding line was included as a probable setup; no setup was annotated")
            else:
                unc.append(f"no setup was annotated and the preceding line ends {gap(lo - 1, lo):.1f}s earlier; "
                           "the setup is unknown")
        # -- walk back while the opening leans on earlier context
        while lo > 0 and depends_on_context(segs[lo].text) and segs[hi].end_s - segs[lo - 1].start_s <= cfg.max_short_s:
            lo -= 1
            a_sp, b_sp = segs[lo].speaker, segs[lo + 1].speaker
            if a_sp and b_sp and a_sp != b_sp:
                unc.append(f"context reconstruction crossed a speaker change ({a_sp} / {b_sp}); the added line may "
                           "belong to someone else")
        ref = ix.ref_for_ids(s.segment_id for s in segs[lo:hi + 1])
        dur = ref.duration_s
        speakers = {s.speaker for s in segs[lo:hi + 1]}
        named = speakers - {None}
        if len(named) > 1:
            unc.append(f"more than one speaker label inside the span ({', '.join(sorted(named))})")
        if None in speakers and named:
            unc.append("some segments in the span have no speaker label")
        att = ix.attribution(ref)
        if att != "creator":
            warns.append(f"the span contains speech not attributed to the creator (attribution: {att})")
        # -- context heuristic (never a verification)
        dep = depends_on_context(segs[lo].text) or bool(cb and a.moment(cb.setup_moment_id).ref.source_id != m.ref.source_id)
        if depends_on_context(segs[lo].text):
            issues.append("the opening still refers to earlier context that fits in no window"
                          + ("; the source itself opens mid-thought" if lo == 0 else ""))
        assess = "dependency_detected" if dep else "none_detected"
        if dur > cfg.max_short_s:
            if cb:
                issues.append(f"trade-off: the callback setup was kept, so the span is {dur:.0f}s, over the "
                              f"{cfg.max_short_s:g}s working limit; shortening needs editorial cuts not decided here")
            else:
                issues.append(f"span is {dur:.0f}s, over the {cfg.max_short_s:g}s working limit; shortening needs "
                              "editorial cuts that are not decided here")
        if dur < cfg.min_short_s:
            issues.append(f"span is only {dur:.1f}s; may be too short to carry context and payoff")
        ids = set(ref.segment_ids)
        inside = [x for x in a.moments if set(x.ref.segment_ids) <= ids]
        payoff = m if m.kind in _PAYOFF_KINDS else next(
            (x for x in inside if x.kind in _PAYOFF_KINDS and x is not m), None)
        hook = next((x for x in inside if x is not payoff and x.kind in _HOOK_KINDS
                     and x.ref.start_s - ref.start_s <= max(5.0, 0.25 * dur)), None)
        if hook is None:
            issues.append("no moment in the opening of this window works as a hook; the footage itself starts "
                          "without one")
        if payoff is None:
            issues.append("no payoff or complete thought identified in the window")
        if hook is None and payoff is None:
            notes.append(f"no short-form concept for {m.moment_id}: the material gives neither a hook nor a payoff")
            continue
        if payoff is None and not (m.kind is MomentKind.QUESTION and taken > 0):
            notes.append(f"no short-form concept for {m.moment_id}: no payoff, and no answer follows the question "
                         "in this window")
            continue
        if m.detected_by == "heuristic":
            warns.append("moment found by heuristic only; judge it before relying on it")
        warns.extend(m.warnings)
        pres = tuple(f"pause {p.pause_id} ({p.duration_s:.1f}s): keep unless audio shows it is dead air"
                     for p in a.pauses if p.before_segment in ids and p.after_segment in ids)
        pres += ((f"{m.moment_id}: keep the beat after it; the reaction is part of the timing",)
                 if m.kind in (MomentKind.HUMOR_OR_REACTION, MomentKind.REVEAL) else ())
        # skip near-duplicates
        if any(len(set(c.ranges[0].segment_ids) & ids) >= 0.5 * min(len(ids), len(c.ranges[0].segment_ids))
               and set(c.ranges[0].segment_ids) & ids for c in out if c.ranges[0].source_id == ref.source_id):
            continue
        check = StandaloneCheck(hook is not None, payoff is not None, assess, "heuristic",
                                tuple(issues), tuple(unc))
        conf = max(0.05, round(min(m.confidence, 0.6) - 0.15 * len(issues) - 0.05 * len(unc), 3))
        out.append(ShortConcept(
            cid(), f"Standalone {m.kind.value.replace('_', ' ')} from {ref.source_id}", (ref,),
            tuple(x.moment_id for x in inside) or (m.moment_id,), hook.moment_id if hook else None,
            payoff.moment_id if payoff else None, check, round(dur, 3), False,
            ("one contiguous, unreordered span chosen so the opening is understandable and the beat after the "
             "payoff is kept (context judged by heuristic only)") if not (issues or unc) else
            "candidate span; the standalone check lists issues and uncertainties that need a human decision",
            conf, tuple(dict.fromkeys(warns)), pres))
    if not out:
        notes.append("no short-form concept is supported by the supplied material")
    return out, notes
