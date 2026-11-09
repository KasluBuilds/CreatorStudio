"""Story analysis: observed structure + clearly labelled inferences.

Two sources of moments:
- ANNOTATIONS supplied from outside (a human or a model), each naming its annotator;
- a small set of transparent HEURISTICS (question marks, laughter markers, cue words, repeated
  rare words). Heuristic confidence is capped low on purpose: they find candidates, they do not
  understand humor or meaning.
Nothing here writes or rewrites speech. Excerpts are copied from the transcript by code.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .inputs import (Annotation, CallbackAnnotation, ClaimLink, EvidenceError, Judgement, Material, Segment,
                     TranscriptIndex, clean_text, valid_confidence, valid_identifier)
from .textsafety import attribution_risk, transcript_norm
from .schema import (INFERRED, OBSERVED, SUGGESTED, Audience, BoundaryOption, Callback, CentralIdea, Finding,
                     Moment, MomentKind, Pause, SourceRef)

HEURISTIC_CAP = 0.5
UNVERIFIED_CAP = 0.75   # cap on interpretation confidence when the underlying text is unreviewed ASR

_STOP = set("""about above after again against because before being below between could does doing during
each from further have having here herself himself into itself just more most other ought over same should
since some such than that their theirs them then there these they this those through under until very were what
when where which while whom with would your yours yourself also like really actually kind thing things going
gonna want know okay right well yeah""".split()) | set("""ovdje onda kada koje koji koja koju kako samo
jako puno malo ovako onako tako dakle znači jesam jesmo nisam nismo bilo bila bili biti mogu može želim
ali ili jer nego kao još već sada sad""".split())
_CONTEXT_OPENERS = {"this", "that", "it", "they", "he", "she", "these", "those", "so", "and", "but",
                    "ovo", "to", "ono", "oni", "ona", "on", "ali", "pa", "i"}
_LAUGH = re.compile(r"\[(?:laugh\w*|smij\w*)\]|\((?:laugh\w*|smij\w*)\)|\bha\s?ha\b|\bhaha\w*|\bhehe\w*", re.I)
_DEMO = re.compile(r"\b(?:look|watch|see how|as you can see|here you can see|evo|gledaj|pogledaj|vidi)\b", re.I)
_WORD = re.compile(r"[^\W\d_]{5,}", re.UNICODE)


def depends_on_context(text: str) -> bool:
    m = re.match(r"\W*([^\W\d_]+)", text, re.UNICODE)
    return bool(m) and m.group(1).lower() in _CONTEXT_OPENERS


def terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP}


@dataclass
class Analysis:
    index: TranscriptIndex
    moments: list[Moment] = field(default_factory=list)
    pauses: list[Pause] = field(default_factory=list)
    callbacks: list[Callback] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    beginnings: list[BoundaryOption] = field(default_factory=list)
    endings: list[BoundaryOption] = field(default_factory=list)
    central_idea: CentralIdea | None = None
    audience: Audience = field(default_factory=lambda: Audience(None, None, "unresolved", 0.0))
    claim_moments: list[tuple] = field(default_factory=list)   # (ClaimLink, moment_id)
    judgement: Judgement | None = None   # validated; None if absent or rejected
    rejected: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def moment(self, mid: str) -> Moment:
        return next(m for m in self.moments if m.moment_id == mid)

    def strongest(self, k: int = 3) -> list[Moment]:
        kinds = {MomentKind.HUMOR_OR_REACTION, MomentKind.REVEAL, MomentKind.CENTRAL_STATEMENT,
                 MomentKind.DEMONSTRATION, MomentKind.CALLBACK_PAYOFF}
        c = [m for m in self.moments if m.kind in kinds]
        c.sort(key=lambda m: (m.detected_by == "heuristic", -m.confidence, m.ref.source_id, m.ref.start_s))
        return c[:k]


class _Ids:
    def __init__(self, prefix: str):
        self.p, self.n = prefix, 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.p}{self.n}"


def analyze(material: Material, *, pause_min_s: float = 1.5, callback_min_gap_s: float = 30.0,
            max_heuristic_callbacks: int = 3, research_audience: str | None = None) -> Analysis:
    ix = material.index()
    a = Analysis(ix, warnings=list(material.warnings))
    usable = [s for s in material.segments if s.text]
    if not usable:
        n_in = sum(len(getattr(material, k, None) or []) for k in
                   ("annotations", "callback_annotations", "claim_links"))
        if n_in:
            a.warnings.append(f"{n_in} annotation/claim-link input(s) ignored: no usable transcript text")
        return a
    mid = _Ids("m")

    def cap_for(ref: SourceRef, conf: float, why: list[str]) -> float:
        if ref.verification != "reviewed" and conf > UNVERIFIED_CAP:
            why.append(f"confidence capped at {UNVERIFIED_CAP}: text is unreviewed ASR")
            return UNVERIFIED_CAP
        return conf

    def add_moment(ref: SourceRef, kind: MomentKind, interp: str, conf: float, rationale: str,
                   by: str, extra: tuple[str, ...] = ()) -> Moment:
        w = list(extra)
        conf = cap_for(ref, conf, w)
        if ref.verification != "reviewed":
            w.append("transcript text is unreviewed ASR: the excerpt may differ from what was said")
        att = ix.attribution(ref)
        if att != "creator":
            w.append(f"speech attribution is '{att}': do not present as the creator's words")
        if depends_on_context(ix.excerpt(ref)):
            w.append("span opens with a word that often refers back to earlier context (heuristic)")
        m = Moment(mid(), kind, ref, ix.excerpt(ref), att, interp, round(conf, 3), rationale, by, tuple(w))
        a.moments.append(m)
        return m

    def find_overlap(ref: SourceRef) -> Moment | None:
        ids = set(ref.segment_ids)
        return next((m for m in a.moments if ids & set(m.ref.segment_ids)), None)

    # ---- intake helpers: nothing malformed is coerced; it is rejected with a reason
    tnorm = transcript_norm(s.text for s in usable)

    def need_text(v: object, label: str, maxlen: int = 2000) -> str:
        if not isinstance(v, str) or not v.strip() or len(v) > maxlen or clean_text(v) != v:
            raise ValueError(f"{label} must be non-empty text (max {maxlen} chars, no control characters)")
        risk = attribution_risk(v, tnorm)
        if risk:
            raise ValueError(f"{label} rejected: {risk}")
        return v.strip()

    def need_ident(v: object, label: str) -> str:
        if not valid_identifier(v):
            raise ValueError(f"{label} must be a plain label (no quotes/control characters, max 200 chars)")
        return str(v)

    def need_conf(v: object) -> float:
        if not valid_confidence(v):
            raise ValueError("confidence must be a finite number within 0..1")
        return float(v)

    def need_span(sid: object, s0: object, s1: object) -> SourceRef:
        return ix.ref_for_span(sid, s0, s1)

    def seq(name: str) -> list:
        v = getattr(material, name, None)
        if v is None:
            return []
        if not isinstance(v, (list, tuple)):
            a.rejected.append({"input": name, "reason": f"{name} must be a list; all entries ignored"})
            return []
        return list(v)

    def reject(tag: str, e: Exception) -> None:
        msg = str(e) if isinstance(e, (ValueError, EvidenceError)) else \
            f"malformed input ({type(e).__name__}); fields must have the documented types"
        a.rejected.append({"input": tag, "reason": msg})

    BAD = (ValueError, EvidenceError, TypeError, AttributeError, KeyError, IndexError)

    # ---- judgement (validated once; an invalid judgement is ignored entirely)
    j = getattr(material, "judgement", None)
    if j is not None:
        try:
            if not isinstance(j, Judgement):
                raise ValueError("judgement is not a Judgement")
            need_conf(j.confidence)
            need_ident(j.annotator, "annotator")
            given = [x for x in (j.central_idea, j.audience, j.audience_promise) if x is not None]
            if not given:
                raise ValueError("a judgement must state a central idea, an audience or a promise")
            need_text(j.central_idea, "central_idea") if j.central_idea is not None else None
            need_text(j.audience, "audience") if j.audience is not None else None
            need_text(j.audience_promise, "audience_promise") if j.audience_promise is not None else None
        except BAD as e:
            reject("judgement", e)
            j = None
    a.judgement = j

    # ---- annotations (interpretations from outside)
    for n, an in enumerate(seq("annotations")):
        tag = f"annotation[{n}]"
        try:
            if not isinstance(an, Annotation):
                raise ValueError("entry is not an Annotation")
            who = need_ident(an.annotator, "annotator")
            tag = f"annotation[{n}] by {who}"
            if not isinstance(an.kind, str):
                raise ValueError("kind must be text")
            kind = MomentKind(an.kind)
            conf = need_conf(an.confidence)
            rationale = need_text(an.rationale, "rationale")
            ref = need_span(an.source_id, an.start_s, an.end_s)
        except BAD as e:
            reject(tag, e)
            continue
        add_moment(ref, kind, f"{kind.value} (judged by {an.annotator})", conf, rationale, f"annotation:{an.annotator}")

    # ---- claim links (creator span <-> research claim; annotator's judgement)
    for n, cl in enumerate(seq("claim_links")):
        tag = f"claim_link[{n}]"
        try:
            if not isinstance(cl, ClaimLink):
                raise ValueError("entry is not a ClaimLink")
            who = need_ident(cl.annotator, "annotator")
            tag = f"claim_link[{n}] by {who}"
            need_ident(cl.claim_id, "claim_id")
            if cl.relation not in ("asserts", "questions", "disputes"):
                raise ValueError("relation must be asserts, questions or disputes")
            need_conf(cl.confidence)
            ref = need_span(cl.source_id, cl.start_s, cl.end_s)
        except BAD as e:
            reject(tag, e)
            continue
        m = find_overlap(ref) or add_moment(
            ref, MomentKind.OTHER, f"creator span that {cl.relation} research claim {cl.claim_id}",
            float(cl.confidence), f"linked to claim {cl.claim_id} by {cl.annotator}", f"annotation:{cl.annotator}")
        a.claim_moments.append((cl, m.moment_id))

    # ---- heuristics (candidates only)
    segs_by_src = ix.by_source
    for s in usable:
        ref = ix.ref_for_ids([s.segment_id])
        if find_overlap(ref):
            continue
        if s.text.rstrip().endswith("?"):
            add_moment(ref, MomentKind.QUESTION, "possible question or open thread", 0.35,
                       "text ends with a question mark", "heuristic")
        elif _LAUGH.search(s.text):
            add_moment(ref, MomentKind.HUMOR_OR_REACTION, "possible laughter or reaction", 0.4,
                       "transcript contains a laughter marker; whether anything is funny is a judgement "
                       "this heuristic cannot make", "heuristic")
        elif _DEMO.search(s.text):
            add_moment(ref, MomentKind.DEMONSTRATION, "possible demonstration cue", 0.3,
                       "contains a show/look cue word; the transcript cannot tell whether anything is "
                       "actually visible", "heuristic")

    # ---- callbacks: annotated, then heuristic
    cid = _Ids("cb")
    callback_inputs = seq("callback_annotations")
    for n, ca in enumerate(callback_inputs):
        tag = f"callback_annotation[{n}]"
        try:
            if not isinstance(ca, CallbackAnnotation):
                raise ValueError("entry is not a CallbackAnnotation")
            who = need_ident(ca.annotator, "annotator")
            tag = f"callback_annotation[{n}] by {who}"
            for nm, part in (("setup", ca.setup), ("payoff", ca.payoff)):
                if not isinstance(part, (tuple, list)) or len(part) != 3:
                    raise ValueError(f"{nm} must be (source_id, start_s, end_s)")
            sref = need_span(*ca.setup)
            pref = need_span(*ca.payoff)
            conf = need_conf(ca.confidence)
            rationale = need_text(ca.rationale, "rationale")
            if sref.source_id == pref.source_id and pref.start_s < sref.end_s:
                raise ValueError("payoff does not come after setup in the source")
        except BAD as e:
            reject(tag, e)
            continue
        sm = find_overlap(sref) or add_moment(sref, MomentKind.CALLBACK_SETUP, "setup for a later payoff",
                                              conf, rationale, f"annotation:{ca.annotator}")
        pm = find_overlap(pref) or add_moment(pref, MomentKind.CALLBACK_PAYOFF, "payoff of an earlier setup",
                                              conf, rationale, f"annotation:{ca.annotator}")
        extra = () if sref.source_id == pref.source_id else ("setup and payoff are in different sources; "
                                                            "their order in time is unknown",)
        a.callbacks.append(Callback(cid(), sm.moment_id, pm.moment_id, (),
                                   round(min(conf, UNVERIFIED_CAP if "reviewed" != sref.verification else 1), 3),
                                   rationale + (" [" + "; ".join(extra) + "]" if extra else ""),
                                   f"annotation:{ca.annotator}"))
    if not callback_inputs:
        cands = []
        for src, segs in segs_by_src.items():
            occ: dict[str, list[Segment]] = {}
            for s in segs:
                for t in terms(s.text):
                    if len(t) >= 6:
                        occ.setdefault(t, []).append(s)
            for t, ss in occ.items():
                if len(ss) == 2 and ss[1].start_s - ss[0].end_s >= callback_min_gap_s:
                    cands.append((ss[1].start_s - ss[0].end_s, t, ss[0], ss[1]))
        cands.sort(key=lambda c: (-c[0], c[1]))
        used: set[str] = set()
        for gap, t, s0, s1 in cands:
            if len(a.callbacks) >= max_heuristic_callbacks or s0.segment_id in used or s1.segment_id in used:
                continue
            used |= {s0.segment_id, s1.segment_id}
            r0, r1 = ix.ref_for_ids([s0.segment_id]), ix.ref_for_ids([s1.segment_id])
            sm = find_overlap(r0) or add_moment(r0, MomentKind.CALLBACK_SETUP, "possible setup", 0.3,
                                                f"rare word '{t}' recurs {gap:.0f}s later", "heuristic")
            pm = find_overlap(r1) or add_moment(r1, MomentKind.CALLBACK_PAYOFF, "possible payoff", 0.3,
                                                f"rare word '{t}' recurs {gap:.0f}s later", "heuristic")
            a.callbacks.append(Callback(cid(), sm.moment_id, pm.moment_id, (t,), 0.3,
                                        f"the word '{t}' appears exactly twice, {gap:.0f}s apart; a shared word is "
                                        "weak evidence of a callback", "heuristic"))

    a.moments.sort(key=lambda m: (m.ref.source_id, m.ref.start_s))
    # ids stay as assigned; order of the list is source order

    # ---- pauses (measured from timestamps only)
    pid = _Ids("p")
    for src, segs in segs_by_src.items():
        for s0, s1 in zip(segs, segs[1:]):
            if s1.start_s - s0.end_s >= pause_min_s:
                a.pauses.append(Pause(pid(), src, s0.end_s, s1.start_s, s0.segment_id, s1.segment_id))

    # ---- central idea, audience
    a.central_idea = _central_idea(material, a, usable)
    a.audience = _audience(a, research_audience)

    # ---- findings
    _findings(material, a, usable)
    _boundaries(a, usable)
    return a


def _central_idea(material: Material, a: Analysis, usable: list[Segment]) -> CentralIdea:
    j = a.judgement
    counts: dict[str, int] = {}
    for s in usable:
        for t in terms(s.text):
            counts[t] = counts.get(t, 0) + 1
    top = [t for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if n >= 2][:4]
    support = tuple(m.moment_id for m in a.moments if terms(m.excerpt) & set(top))[:5]
    if j and j.central_idea and j.central_idea.strip():
        conf = j.confidence if isinstance(j.confidence, (int, float)) and 0 <= j.confidence <= 1 else 0.0
        if any(s.verification != "reviewed" for s in usable):
            conf = min(conf, UNVERIFIED_CAP)
        return CentralIdea(j.central_idea.strip(), round(conf, 3), INFERRED, f"annotation:{j.annotator}",
                           support, tuple(top), (), "supplied by the annotator; not derived from the transcript here")
    if not top:
        return CentralIdea("not established: no term recurs across the supplied transcript",
                           0.0, INFERRED, "heuristic", (), (), (),
                           "needs an annotator's judgement or more material")
    return CentralIdea(f"candidate focus (heuristic, from recurring terms): {', '.join(top)}", 0.3, INFERRED,
                       "heuristic", support, tuple(top), (),
                       "recurring words suggest a topic, not what the creator wants to communicate")


def _audience(a: Analysis, research_audience: str | None = None) -> Audience:
    j = a.judgement
    if j and (j.audience or j.audience_promise):
        return Audience(j.audience, j.audience_promise, "annotation", round(j.confidence, 3),
                        f"supplied by {j.annotator}")
    if research_audience:
        return Audience(research_audience, None, "research_brief", 0.3,
                        "taken from the research brief; not confirmed by the creator or an annotator")
    return Audience(None, None, "unresolved", 0.0, "no audience or promise was supplied or observable")


def _findings(material: Material, a: Analysis, usable: list[Segment]) -> None:
    ix, fid = a.index, _Ids("f")

    def F(kind, text, ms=(), refs=(), conf=0.3, rationale=""):
        a.findings.append(Finding(fid(), kind, text, tuple(ms), tuple(refs), round(conf, 3), INFERRED, rationale))

    unrev = [s for s in usable if s.verification != "reviewed"]
    if unrev:
        F("weakness", f"{len(unrev)} of {len(usable)} segments are unreviewed ASR; wording may be wrong",
          conf=1.0, rationale="verification status is recorded on every segment")
    if len(usable) < 3:
        F("weakness", "very little material: structure suggestions are weak", conf=0.9,
          rationale=f"{len(usable)} usable segment(s)")
    if a.moments and all(m.detected_by == "heuristic" for m in a.moments):
        F("weakness", "all moments come from heuristics; no annotator judged the material", conf=0.9,
          rationale="heuristics only find candidates")
    if not a.moments:
        F("weakness", "no candidate moments were found", conf=0.9, rationale="no annotations and no heuristic hits")
    for src, segs in ix.by_source.items():
        first = segs[0]
        if first.text and depends_on_context(first.text):
            F("missing_context", f"source {src} opens mid-thought: its first segment may refer to earlier context",
              refs=[ix.ref_for_ids([first.segment_id])], conf=0.35,
              rationale="first word is a pronoun/connective (heuristic)")
    for m in a.moments:
        if m.kind is MomentKind.QUESTION:
            nxt = ix.next_segment(m.ref.segment_ids[-1])
            if nxt is None:
                F("missing_context", "a question is the last thing in the source; no answer is in the material",
                  ms=[m.moment_id], refs=[m.ref], conf=0.5, rationale="no later segment in this source")
            else:
                F("opportunity", "a question is followed by further speech; may form a question-then-answer beat",
                  ms=[m.moment_id], refs=[m.ref], conf=0.3, rationale="adjacency only; whether it is answered is unknown")
    for cb in a.callbacks:
        F("opportunity", "possible setup/payoff pair across the video", ms=[cb.setup_moment_id, cb.payoff_moment_id],
          conf=cb.confidence, rationale=cb.rationale)
    for p in a.pauses:
        F("opportunity", f"{p.duration_s:.1f}s gap between segments; keep it unless audio shows it is dead air",
          refs=[ix.ref_for_ids([p.before_segment])], conf=0.3,
          rationale="pauses before/after humor or reveals are part of timing (EDITING_ENGINE §4)")
    # repetition
    seen: dict[str, str] = {}
    for s in usable:
        k = re.sub(r"\W+", " ", s.text.lower()).strip()
        if len(k.split()) >= 4:
            if k in seen:
                F("weakness", "near-identical segment text appears twice; may be repetition or a restart",
                  refs=[ix.ref_for_ids([seen[k]]), ix.ref_for_ids([s.segment_id])], conf=0.4,
                  rationale="exact text match after normalisation; may also be deliberate emphasis")
            else:
                seen[k] = s.segment_id
    if not a.callbacks and a.moments:
        F("weakness", "no setup/payoff pairs found", conf=0.3, rationale="none annotated, none detected")


def _boundaries(a: Analysis, usable: list[Segment]) -> None:
    ix = a.index
    first, last = usable[0], usable[-1]
    n = _Ids("b")
    r0 = ix.ref_for_ids([first.segment_id])
    r1 = ix.ref_for_ids([last.segment_id])
    a.beginnings.append(BoundaryOption(
        n(), "beginning", "start where the recording starts", (), r0,
        "keeps the original order, so no meaning is moved" + (
            "; but the first segment may rely on earlier context" if depends_on_context(first.text) else ""),
        0.4, SUGGESTED))
    for m in a.strongest(2):
        if not depends_on_context(m.excerpt) and m.ref.start_s > first.start_s:
            a.beginnings.append(BoundaryOption(
                n(), "beginning", f"open on the {m.kind.value} at {m.moment_id}, then return to the start",
                (m.moment_id,), m.ref,
                "a hook taken from real footage; only honest if the video delivers on it", min(m.confidence, 0.5),
                SUGGESTED))
    for m in a.moments:
        if m.kind is MomentKind.QUESTION and not depends_on_context(m.excerpt):
            a.beginnings.append(BoundaryOption(n(), "beginning", f"open on the question at {m.moment_id}",
                                               (m.moment_id,), m.ref, "an open question gives the viewer a reason "
                                               "to stay, if it is answered later", min(m.confidence, 0.4)))
            break
    a.endings.append(BoundaryOption(n(), "ending", "end where the recording ends", (), r1,
                                    "keeps the original order; check that it lands on a conclusion, not mid-thought",
                                    0.4))
    for cb in a.callbacks[:2]:
        pm = a.moment(cb.payoff_moment_id)
        a.endings.append(BoundaryOption(n(), "ending", f"end on the payoff at {pm.moment_id}", (pm.moment_id,),
                                        pm.ref, "closing on a payoff gives a sense of completion",
                                        min(cb.confidence, 0.5)))
