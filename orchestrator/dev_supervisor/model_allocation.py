"""Model allocation by role (T-DEV-038): choose a worker route from the verified zero-cost registry.

Pure and deterministic: the same registry, role and author give the same answer. It never starts anything and never
reads credentials. A route reaches the registry only with recorded evidence of a full Claude Code prompt, tool-result
and final-response round trip (see worker_spawner.load_verified_routes); the optional ``roles`` and ``capability``
fields say what that evidence covers. A route without them is valid for an explicit dispatch but is never chosen by
role. Fail closed: with no fitting route the answer is a refusal with a reason, never a fallback, never a Claude model.

Capability is a recorded rank from 1 (weakest) to 5 (strongest). It is a claim about the route, not a measurement made
here. Rules:

* implementation, research, test_writing: the lowest capability that fits (cheapest sufficient route).
* review: capability at least the author's, so a review is never weaker than the work; when another route exists at
  that level, a route different from the author's is preferred.
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
                     r"|BEGIN [A-Z ]*PRIVATE KEY)")
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

    def to_dict(self) -> Dict[str, Any]:
        """Credential-free record for a worker record or an event."""
        return asdict(self)


def _usable(route: Any) -> bool:
    """A route that may be allocated by role: verified zero cost, a real provider, not a Claude name, and evidence that
    does not look like a credential (an evidence field is a reference, never a secret)."""
    return (isinstance(route, worker_spawner.VerifiedRoute) and route.cost_zero is True
            and route.provider in worker_spawner.ALLOWED_PROVIDERS and not worker_spawner.is_claude_model(route.model)
            and isinstance(route.evidence, str) and bool(route.evidence.strip()) and not _SECRET.search(route.evidence))


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
    fits = [r for r in routes if _usable(r) and role in r.roles and r.capability >= need]
    if not fits:
        raise AllocationRefused(f"no verified zero-cost route is recorded for role {role} at capability {need} or more")

    def key(r: worker_spawner.VerifiedRoute):
        same_as_author = 1 if (role == "review" and author_route == (r.provider, r.model)) else 0
        rank = -r.capability if role in HIGHEST_FIRST else r.capability
        return (rank, same_as_author, r.provider, r.model)

    best = sorted(fits, key=key)[0]
    how = "highest capability available" if role in HIGHEST_FIRST else "lowest capability that fits"
    why = f"{how} for role {role} (needs {need} or more, route has {best.capability}); {len(fits)} route(s) fit"
    if role == "review" and author_route == (best.provider, best.model):
        why += "; the author's own route is the only fit"
    return Allocation(role=role, provider=best.provider, model=best.model, capability=best.capability,
                      evidence=best.evidence.strip()[:MAX_EVIDENCE], reason=why)


def allocate_from_registry(registry_file: Any, role: str, *, author_capability: int = 0,
                           author_route: Optional[Tuple[str, str]] = None) -> Allocation:
    """Read the verified-route registry file and allocate; an unreadable registry is a refusal, never a fallback."""
    try:
        routes = worker_spawner.load_verified_routes(Path(str(registry_file)))
    except worker_spawner.SpawnRefused as exc:
        raise AllocationRefused("verified route registry unavailable: " + str(exc)) from None
    return allocate(role, routes, author_capability=author_capability, author_route=author_route)
