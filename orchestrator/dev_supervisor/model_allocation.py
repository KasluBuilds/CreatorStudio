"""Model allocation by role (T-DEV-038): choose a worker route from the verified zero-cost registry.

Pure and deterministic: the same registry, role and author give the same answer. It never starts anything and never
reads credentials. A route reaches the registry only with recorded evidence of a full Claude Code prompt, tool-result
and final-response round trip (see worker_spawner.load_verified_routes); the optional ``roles`` and ``capability``
fields say what that evidence covers. A route without them is valid for an explicit dispatch but is never chosen by
role. Fail closed: with no fitting route the answer is a refusal with a reason, never a fallback, never a Claude model.

Capability is a recorded rank from 1 (weakest) to 5 (strongest). It is a claim about the route, not a measurement made
here. Rules:

* implementation, research, test_writing: the lowest capability that fits (cheapest sufficient route).
* review: capability at least the author's, so a review is never weaker than the work, and the route must differ from
  the author's: without the author's route, or when the author's own route is the only one that fits, the allocation
  is refused (T-DEV-054). This compares routes only; it does not prove a reviewer was independent in fact.
* architecture: the highest capability available, because the hard work should not get the weakest route.
"""
from __future__ import annotations

import re
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from . import worker_spawner

ROLES = ("implementation", "research", "test_writing", "review", "architecture")
MIN_CAPABILITY = {"implementation": 2, "research": 2, "test_writing": 2, "review": 3, "architecture": 4}
HIGHEST_FIRST = ("architecture",)
_SECRET = re.compile(r"((^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
                     r"|BEGIN [A-Z ]*PRIVATE KEY|gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[bap]-[A-Za-z0-9-]{10,}"
                     r"|[Bb]earer +[A-Za-z0-9._~+/-]{20,})")
MAX_EVIDENCE = 160


class AllocationRefused(Exception):
    """No route was chosen. The message names the reason only."""


@dataclass(frozen=True)
class Allocation:
    role: str
    provider: str
    model: str
    capability: int
    evidence: str
    reason: str
    independent: Optional[bool] = None
    """True = a review route that is not the author's route; None = not a review. A review whose route would be the author's, or whose author route is unknown, is refused instead of returned (T-DEV-054), so this is never False."""

    def to_dict(self) -> Dict[str, Any]:
        """Credential-free record for a worker record or an event."""
        return asdict(self)


def _norm_route(route: Any) -> Optional[Tuple[str, str]]:
    """(provider, model) compared case-insensitively and without surrounding space; None when not a pair of strings."""
    if (isinstance(route, (tuple, list)) and len(route) == 2 and all(isinstance(x, str) for x in route)):
        return (route[0].strip().casefold(), route[1].strip().casefold())
    return None


def _usable(route: Any) -> bool:
    """A route that may be allocated by role: verified zero cost, a real provider, not a Claude name, and evidence that
    does not look like a credential (an evidence field is a reference, never a secret)."""
    return (isinstance(route, worker_spawner.VerifiedRoute) and route.cost_zero is True
            and route.provider in worker_spawner.ALLOWED_PROVIDERS and not worker_spawner.is_claude_model(route.model)
            and isinstance(route.evidence, str) and bool(route.evidence.strip()) and not _SECRET.search(route.evidence)
            and not _SECRET.search(route.model))


def allocate(role: str, routes: Sequence[Any], *, author_capability: int = 0,
             author_route: Optional[Tuple[str, str]] = None) -> Allocation:
    """Choose the route for ``role`` or raise AllocationRefused.

    ``author_capability`` and ``author_route`` (provider, model) describe the work being reviewed and apply to the
    review role only; an unknown author capability (0) means the role minimum applies."""
    if role not in ROLES:
        raise AllocationRefused(f"unknown role {role!r}")
    need = MIN_CAPABILITY[role]
    if role == "review":
        if isinstance(author_capability, bool) or not isinstance(author_capability, int) or author_capability < 0:
            raise AllocationRefused("the author capability must be a non-negative integer")
        need = max(need, author_capability)
    usable = [r for r in routes if _usable(r)]
    # Duplicate (provider, model) entries are merged conservatively: the lowest recorded capability and only the
    # roles every entry covers, so a pair can never claim more than its weakest entry (and registry order is moot).
    merged: Dict[Tuple[str, str], worker_spawner.VerifiedRoute] = {}
    for r in sorted(usable, key=lambda x: (x.provider, x.model, x.capability, x.evidence)):
        key_ = (r.provider, r.model)
        if key_ in merged:
            old = merged[key_]
            merged[key_] = worker_spawner.VerifiedRoute(old.model, old.provider, old.evidence, True,
                                                        tuple(sorted(set(old.roles) & set(r.roles))),
                                                        min(old.capability, r.capability))
        else:
            merged[key_] = r
    fits = [r for r in merged.values() if role in r.roles and r.capability >= need]
    if not fits:
        raise AllocationRefused(f"no verified zero-cost route is recorded for role {role} at capability {need} or more")
    author = _norm_route(author_route) if role == "review" else None
    if role == "review" and author is None:        # T-DEV-054: without the author's route independence cannot be shown
        raise AllocationRefused("a review needs the author's route (provider, model) to show the reviewer is independent")

    def is_author(r: worker_spawner.VerifiedRoute) -> bool:
        return author is not None and _norm_route((r.provider, r.model)) == author

    def key(r: worker_spawner.VerifiedRoute):
        rank = -r.capability if role in HIGHEST_FIRST else r.capability
        # Review: a route other than the author's always comes first; capability only orders routes within each group.
        return (1 if is_author(r) else 0, rank, r.provider, r.model)

    if role == "review" and all(is_author(r) for r in fits):    # T-DEV-054: the author's own route is never a reviewer
        raise AllocationRefused("no independent review route: the only route that fits is the author's own")
    best = sorted(fits, key=key)[0]
    how = "highest capability available" if role in HIGHEST_FIRST else "lowest capability that fits"
    why = f"{how} for role {role} (needs {need} or more, route has {best.capability}); {len(fits)} route(s) fit"
    independent = (not is_author(best)) if author is not None else None
    return Allocation(role=role, provider=best.provider, model=best.model, capability=best.capability,
                      evidence=best.evidence.strip()[:MAX_EVIDENCE], reason=why, independent=independent)


def allocate_from_registry(registry_file: Any, role: str, *, author_capability: int = 0,
                           author_route: Optional[Tuple[str, str]] = None) -> Allocation:
    """Read the verified-route registry file and allocate; an unreadable registry is a refusal, never a fallback."""
    try:
        routes = worker_spawner.load_verified_routes(Path(str(registry_file)))
    except worker_spawner.SpawnRefused as exc:
        raise AllocationRefused("verified route registry unavailable: " + str(exc)) from None
    return allocate(role, routes, author_capability=author_capability, author_route=author_route)
