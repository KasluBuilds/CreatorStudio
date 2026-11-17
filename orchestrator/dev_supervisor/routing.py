"""Task-profile scheduler for the development supervisor (T-DEV-004, wave w03 lane L4). Stdlib only.

Pure, deterministic, fully explained: the same TaskProfile and RoutingConfig always give the same ranked
Decision. There is NO fixed percentage split; every ranking comes from the profile's own signals.
Any signal may be UNAVAILABLE; that is treated as neutral and said so in the reasons.
A paid API route is never ranked unless RoutingConfig.allow_paid_api is True (default False).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class _Unavailable:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "UNAVAILABLE"

    def __bool__(self):
        return False


UNAVAILABLE = _Unavailable()

CRITICALITIES = ("low", "normal", "high", "critical")
COMPLEXITIES = ("mechanical", "repetitive", "search", "routine", "complex", "architecture", "integration")
STRONG_COMPLEXITIES = ("architecture", "integration")
CHEAP_COMPLEXITIES = ("mechanical", "repetitive", "search", "routine")

# route identifiers (order is the deterministic tie-break)
CLAUDE_STRONG = "claude:strongest"
CLAUDE_STANDARD = "claude:standard"
OMNI_GEMINI = "omniroute:gemini"
OMNI_GROQ = "omniroute:groq"
PAID_API = "paid_api"
ROUTE_ORDER = (CLAUDE_STRONG, CLAUDE_STANDARD, OMNI_GEMINI, OMNI_GROQ, PAID_API)
OMNI_ROUTES = (OMNI_GEMINI, OMNI_GROQ)
CLAUDE_ROUTES = (CLAUDE_STRONG, CLAUDE_STANDARD)

QUOTA_EXHAUSTED = 0.05
QUOTA_LOW = 0.20
PRESSURE_HIGH = 0.80
DEADLINE_SHORT_MIN = 5.0
CONCURRENCY_HIGH = 3
REPEATED_FAILURES = 2


@dataclass(frozen=True)
class TaskProfile:
    criticality: object = "normal"            # one of CRITICALITIES or UNAVAILABLE
    complexity: object = "routine"            # one of COMPLEXITIES or UNAVAILABLE
    est_context_tokens: object = UNAVAILABLE  # int or UNAVAILABLE
    deadline_minutes: object = UNAVAILABLE    # float or UNAVAILABLE
    concurrency: object = UNAVAILABLE         # int running now, or UNAVAILABLE
    context_pressure: object = UNAVAILABLE    # 0..1 fraction of the Claude session context used
    quota: dict = field(default_factory=dict)  # route -> remaining fraction 0..1 or UNAVAILABLE
    validation_failed: bool = False
    failures: int = 0


@dataclass(frozen=True)
class RoutingConfig:
    allow_paid_api: bool = False              # default OFF; only an explicit flag authorises a paid route
    omniroute_models: tuple = (("omniroute:gemini", "gemini/gemini-2.5-flash"),
                               ("omniroute:groq", "groq/openai/gpt-oss-120b"))
    context_limits: tuple = ((OMNI_GEMINI, 900_000), (OMNI_GROQ, 120_000),
                             (CLAUDE_STRONG, 200_000), (CLAUDE_STANDARD, 200_000), (PAID_API, 200_000))
    reachable_omniroute: Optional[tuple] = None  # routes proven reachable (e.g. from a benchmark); None = assume


@dataclass(frozen=True)
class RankedRoute:
    route: str
    score: int
    reasons: tuple
    model: Optional[str] = None


@dataclass(frozen=True)
class Decision:
    ranking: tuple      # RankedRoute, best first
    excluded: tuple     # (route, reason)
    notes: tuple        # profile-level explanations

    @property
    def best(self) -> Optional[str]:
        return self.ranking[0].route if self.ranking else None

    def explain(self) -> str:
        lines = list(self.notes)
        for i, r in enumerate(self.ranking, 1):
            lines.append("%d. %s (score %d): %s" % (i, r.route, r.score, "; ".join(r.reasons)))
        lines.extend("excluded %s: %s" % e for e in self.excluded)
        if not self.ranking:
            lines.append("NO_ROUTE: every route excluded")
        return "\n".join(lines)


def _num(v):
    """Return a float, or None if v is UNAVAILABLE / not a usable number."""
    if v is UNAVAILABLE or v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def decide(profile: TaskProfile, config: Optional[RoutingConfig] = None) -> Decision:
    config = config or RoutingConfig()
    notes, excluded = [], []
    scores = {r: 0 for r in ROUTE_ORDER}
    why = {r: [] for r in ROUTE_ORDER}
    out = {}  # route -> exclusion reason

    def add(route, pts, reason):
        scores[route] += pts
        why[route].append("%+d %s" % (pts, reason))

    crit = profile.criticality if profile.criticality in CRITICALITIES else None
    cplx = profile.complexity if profile.complexity in COMPLEXITIES else None
    if crit is None:
        notes.append("criticality UNAVAILABLE: treated as high (fail safe toward the strongest model)")
        crit = "high"
    if cplx is None:
        notes.append("complexity UNAVAILABLE: treated as complex (fail safe)")
        cplx = "complex"

    escalated = bool(profile.validation_failed) or (isinstance(profile.failures, int)
                                                    and profile.failures >= REPEATED_FAILURES)
    strong = crit in ("high", "critical") or cplx in STRONG_COMPLEXITIES
    cheap = (not strong) and cplx in CHEAP_COMPLEXITIES and crit in ("low", "normal")

    # baseline: every route starts equal; the profile moves them
    if escalated:
        why_txt = "validation failed" if profile.validation_failed else "%d failures" % profile.failures
        add(CLAUDE_STRONG, 200, "escalation to Claude: " + why_txt)
        add(CLAUDE_STANDARD, 100, "escalation to Claude: " + why_txt)
        for r in OMNI_ROUTES:
            add(r, -100, "escalation: do not retry on the cheaper route that just failed")
        notes.append("escalation: " + why_txt)
    if strong:
        reason = "criticality %s" % crit if crit in ("high", "critical") else "complexity %s" % cplx
        add(CLAUDE_STRONG, 100, "strongest model for " + reason)
        add(CLAUDE_STANDARD, 40, "Claude acceptable for " + reason)
        for r in OMNI_ROUTES:
            add(r, -60, "not for " + reason)
        if crit == "critical":
            for r in OMNI_ROUTES:
                out[r] = "criticality critical: Claude only"
        notes.append("class: strong (" + reason + ")")
    elif cheap:
        add(OMNI_GEMINI, 60, "routine/mechanical/search work suits the free Gemini route")
        add(OMNI_GROQ, 50, "routine/mechanical/search work suits the free Groq route")
        add(CLAUDE_STANDARD, 20, "capable fallback")
        add(CLAUDE_STRONG, -30, "do not spend the strongest model on cheap work")
        notes.append("class: cheap (%s, %s)" % (cplx, crit))
    else:
        add(CLAUDE_STANDARD, 50, "mid-complexity work: standard Claude")
        add(CLAUDE_STRONG, 30, "strongest model is acceptable")
        add(OMNI_GEMINI, 20, "free route can try mid work")
        add(OMNI_GROQ, 10, "free route can try mid work")
        notes.append("class: mid (%s, %s)" % (cplx, crit))

    # context size
    ctx = _num(profile.est_context_tokens)
    limits = dict(config.context_limits)
    if ctx is None:
        notes.append("estimated context UNAVAILABLE: no context filtering")
    else:
        for r in ROUTE_ORDER:
            if r in limits and ctx > limits[r]:
                out[r] = "estimated context %d exceeds limit %d" % (ctx, limits[r])
        if ctx > limits.get(OMNI_GROQ, 0) * 0.5 and OMNI_GEMINI not in out:
            add(OMNI_GEMINI, 15, "large context favours the larger-window Gemini route")

    # context pressure
    pr = _num(profile.context_pressure)
    if pr is None:
        notes.append("context pressure UNAVAILABLE: neutral")
    elif pr >= PRESSURE_HIGH and not strong:
        for r in CLAUDE_ROUTES:
            add(r, -20, "Claude context pressure %.2f >= %.2f: offload" % (pr, PRESSURE_HIGH))
        for r in OMNI_ROUTES:
            add(r, 20, "offload to free route (Claude context pressure %.2f)" % pr)
    elif pr >= PRESSURE_HIGH:
        notes.append("context pressure %.2f high but task is strong: stays on Claude" % pr)

    # quota signals (per route)
    for r in ROUTE_ORDER:
        q = profile.quota.get(r, UNAVAILABLE) if isinstance(profile.quota, dict) else UNAVAILABLE
        qn = _num(q)
        if qn is None:
            why[r].append("quota UNAVAILABLE: neutral")
        elif qn <= QUOTA_EXHAUSTED:
            out[r] = "quota exhausted (%.2f remaining)" % qn
        elif qn < QUOTA_LOW:
            add(r, -30, "quota low (%.2f remaining)" % qn)

    # deadline and concurrency
    dl = _num(profile.deadline_minutes)
    if dl is None:
        notes.append("deadline UNAVAILABLE: neutral")
    elif dl <= DEADLINE_SHORT_MIN and not strong:
        add(OMNI_GROQ, 15, "short deadline favours the fastest route")
    cc = _num(profile.concurrency)
    if cc is None:
        notes.append("concurrency UNAVAILABLE: neutral")
    elif cc >= CONCURRENCY_HIGH and not strong:
        for r in OMNI_ROUTES:
            add(r, 10, "high concurrency (%d): spread load to free routes" % cc)

    # OmniRoute reachability
    if config.reachable_omniroute is not None:
        for r in OMNI_ROUTES:
            if r not in config.reachable_omniroute:
                out[r] = "not proven reachable"

    # paid route: off unless explicitly authorised, then last resort only
    if not config.allow_paid_api:
        out[PAID_API] = "paid API route disabled (allow_paid_api is False)"
    else:
        add(PAID_API, -1000, "paid route authorised by config: last resort only")

    models = dict(config.omniroute_models)
    ranking = []
    for r in ROUTE_ORDER:
        if r in out:
            excluded.append((r, out[r]))
        else:
            ranking.append(RankedRoute(r, scores[r], tuple(why[r]), models.get(r)))
    ranking.sort(key=lambda x: (-x.score, ROUTE_ORDER.index(x.route)))
    return Decision(tuple(ranking), tuple(excluded), tuple(notes))
