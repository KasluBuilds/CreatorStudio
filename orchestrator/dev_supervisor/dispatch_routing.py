"""Worker dispatch gate (T-DEV-032): which provider a worker assignment may use, and when to refuse.

Workers are Claude Code processes. Routing decides which route is best, but this module decides whether a worker may
be dispatched at all. Rules:

- Claude Pro (``anthropic``) is never a worker route. Claude routes ranked by ``routing.decide`` are excluded, and a
  refusal never falls back to them.
- Only OmniRoute routes are eligible, and only when ALL of these hold:
  1. ``CLAUDE_CODE_OVER_OMNIROUTE_VERIFIED`` is True. It is False because Claude Code running through OmniRoute is
     NOT TESTED (OMNIROUTE_BENCHMARK.md: "Whether Claude Code itself works through OmniRoute ... NOT TESTED").
  2. ``OMNIROUTE_WORKER_ENV_IMPLEMENTED`` is True. It is False because no code yet points a worker's ``claude`` child at
     the OmniRoute gateway; without it a worker labelled OmniRoute would silently run on Claude Pro.
- At most ``MAX_ACTIVE_WORKERS`` workers may be active (assigned or live) at once.

Every refusal carries a reason. Every decision reports the provider and route, so what handled a task is recorded.
Standard library only; no process is started here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from . import routing

#: Claude Code running through OmniRoute is NOT TESTED. Flip only with live evidence recorded in the registry.
CLAUDE_CODE_OVER_OMNIROUTE_VERIFIED = False
#: The worker environment does not yet point ``claude`` at the OmniRoute gateway. Flip only when it does.
OMNIROUTE_WORKER_ENV_IMPLEMENTED = False
MAX_ACTIVE_WORKERS = 1

PROVIDER_OMNIROUTE = "omniroute"
PROVIDER_ANTHROPIC = "anthropic"   # Claude Pro: never a dispatch target through this gate


@dataclass(frozen=True)
class Dispatch:
    allowed: bool
    provider: Optional[str]     # PROVIDER_OMNIROUTE when allowed, else None
    route: Optional[str]        # routing route id when allowed, else None
    model: Optional[str]        # route model name when allowed, else None
    reason: str
    excluded: Tuple[Tuple[str, str], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "provider": self.provider, "route": self.route, "model": self.model,
                "reason": self.reason, "excluded": [list(e) for e in self.excluded]}


def _refuse(reason: str, excluded: Tuple[Tuple[str, str], ...] = ()) -> Dispatch:
    return Dispatch(False, None, None, None, reason, excluded)


def decide_worker_dispatch(profile: Optional[routing.TaskProfile] = None, *, active_workers: int = 0,
                           config: Optional[routing.RoutingConfig] = None,
                           verified: Optional[bool] = None,
                           env_implemented: Optional[bool] = None) -> Dispatch:
    """The single decision used at assignment time and again before spawning. Flags are read at call time."""
    if verified is None:
        verified = CLAUDE_CODE_OVER_OMNIROUTE_VERIFIED
    if env_implemented is None:
        env_implemented = OMNIROUTE_WORKER_ENV_IMPLEMENTED
    if active_workers >= MAX_ACTIVE_WORKERS:
        return _refuse(f"concurrency cap: {active_workers} worker(s) active, the cap is {MAX_ACTIVE_WORKERS}")
    decision = routing.decide(profile or routing.TaskProfile(), config)
    omni = [r for r in decision.ranking if r.route in routing.OMNI_ROUTES]
    excluded = tuple(decision.excluded) + tuple(
        (r.route, "not a worker route: workers never run on Claude Pro or a paid API")
        for r in decision.ranking if r.route not in routing.OMNI_ROUTES)
    if not verified:
        return _refuse("no verified free worker route: Claude Code over OmniRoute is NOT TESTED; refusing rather than "
                       "falling back to Claude Pro", excluded)
    if not env_implemented:
        return _refuse("OmniRoute worker environment is not implemented: refusing rather than running the worker on "
                       "Claude Pro", excluded)
    if not omni:
        return _refuse("no OmniRoute route available: every OmniRoute route was excluded", excluded)
    best = omni[0]
    return Dispatch(True, PROVIDER_OMNIROUTE, best.route, best.model,
                    f"OmniRoute route {best.route} (score {best.score}); " + "; ".join(best.reasons), excluded)


def gate_provider(provider: Any, **kwargs: Any) -> Dispatch:
    """Refuse any assignment whose recorded provider is not OmniRoute, before the route decision is even consulted."""
    if provider != PROVIDER_OMNIROUTE:
        return _refuse(f"provider {provider!r} is not a worker provider: only {PROVIDER_OMNIROUTE} may dispatch")
    return decide_worker_dispatch(**kwargs)
