"""Worker dispatch gate (T-DEV-032): the one place that decides whether a worker may be assigned and started.

Fail closed. A worker is assigned only when ALL of these hold, and every refusal carries a reason that names the failed
check (never a URL, token or credential):

- the provider is not Anthropic (Claude Pro) and the model is not a Claude model;
- the (provider, model) pair is in the verified-route registry with a recorded zero cost (``worker_spawner``);
- the local OmniRoute gateway environment is valid (``provider_env.worker_env``: no API key, no Pro login, no provider
  switch, a worker-owned config directory);
- fewer than MAX_ACTIVE_WORKERS workers are assigned or live;
- free physical memory is at least the memory-guard floor (T-DEV-053; an unreadable value refuses).

The same checks run again at launch time inside the launcher's environment builder, so a worker that is somehow
assigned still cannot start without a verified route and a safe environment. The registry lives at
``<state dir>/verified_routes.json``; if it is missing or unreadable nothing is dispatched.

Standard library plus this package; starts no process by itself.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Union

from . import memory_guard, provider_env, worker_spawner
from .lifecycle import ClaudeCliLauncher, LaunchSpec, LifecycleError, forbidden_worker_env

MAX_ACTIVE_WORKERS = 1
_READ_MEMORY = object()      # sentinel: read the machine's free memory when the caller did not pass a reading
REGISTRY_FILE = "verified_routes.json"
WORKER_CONFIG_DIR = "worker-config"

PathLike = Union[str, "os.PathLike[str]"]


@dataclass(frozen=True)
class Dispatch:
    allowed: bool
    provider: Optional[str]     # the verified route's provider when allowed, else None
    route: Optional[str]        # the gateway route (model id) when allowed, else None
    model: Optional[str]        # the same model id, recorded on the worker
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "provider": self.provider, "route": self.route, "model": self.model,
                "reason": self.reason}


def registry_path(state_dir: PathLike) -> Path:
    return Path(state_dir) / REGISTRY_FILE


def worker_config_dir(state_dir: PathLike) -> Path:
    return Path(state_dir) / WORKER_CONFIG_DIR


def _refuse(reason: str) -> Dispatch:
    return Dispatch(False, None, None, None, reason)


def _check_route(provider: Any, model: Any, state_dir: PathLike) -> "tuple[Optional[worker_spawner.VerifiedRoute], str]":
    """(route, "") when verified; (None, reason) otherwise."""
    if not isinstance(provider, str) or not provider.strip():
        return None, "a provider is required: workers have no default provider"
    if provider.strip().lower() == "anthropic":
        return None, "provider anthropic (Claude Pro) is never a worker provider"
    if not isinstance(model, str) or not model.strip():
        return None, "a model is required: workers have no default model"
    if worker_spawner.is_claude_model(model):
        return None, "a Claude model is never a worker model"
    try:
        routes = worker_spawner.load_verified_routes(registry_path(state_dir))
    except worker_spawner.SpawnRefused as exc:
        return None, "verified-route registry unavailable: " + str(exc)
    for r in routes:
        if r.model == model and r.provider == provider and r.cost_zero is True:
            return r, ""
    return None, "no verified zero-cost route for this provider and model"


def decide_worker_dispatch(*, provider: Any, model: Any, active_workers: int, state_dir: PathLike,
                           base_env: Optional[Mapping[str, str]] = None, free_memory_mb: Any = _READ_MEMORY) -> Dispatch:
    """The single decision used at assignment time. Refuses without a verified route and a safe gateway environment."""
    if not isinstance(active_workers, int) or isinstance(active_workers, bool) or active_workers < 0:
        return _refuse("the number of active workers is not known: refusing to dispatch")
    if active_workers >= MAX_ACTIVE_WORKERS:
        return _refuse(f"concurrency cap: {active_workers} worker(s) active, the cap is {MAX_ACTIVE_WORKERS}")
    free = memory_guard.free_memory_mb() if free_memory_mb is _READ_MEMORY else free_memory_mb
    mem_ok, mem_why = memory_guard.check(free, memory_guard.min_free_mb())
    if not mem_ok:
        return _refuse(mem_why)
    route, why = _check_route(provider, model, state_dir)
    if route is None:
        return _refuse(why)
    try:
        env = provider_env.worker_env(str(worker_config_dir(state_dir)), base_env)
    except provider_env.WorkerEnvError as exc:
        return _refuse("worker environment is not safe: " + str(exc))
    if any(forbidden_worker_env(k) for k in env):          # the same rule the launcher applies: gate and launch agree
        return _refuse("worker environment is not safe: it carries a credential variable")
    return Dispatch(True, route.provider, route.model, route.model,
                    f"verified zero-cost route {route.provider}/{route.model} (evidence: {route.evidence})")


def make_worker_env_builder(state_dir: PathLike, base_env: Optional[Mapping[str, str]] = None
                            ) -> Callable[[LaunchSpec], Dict[str, str]]:
    """The environment builder for ClaudeCliLauncher: repeats the route check against the model actually on the
    command line and returns the isolated worker environment, or raises LifecycleError (fail closed)."""
    def build(spec: LaunchSpec) -> Dict[str, str]:
        argv = list(spec.argv)
        if argv.count("--model") != 1 or any(a.startswith("--model=") for a in argv) \
                or argv.index("--model") + 1 >= len(argv):
            raise LifecycleError("worker launch needs exactly one --model: refusing")
        model = argv[argv.index("--model") + 1]
        provider = spec.provider
        if not isinstance(provider, str) or not provider.strip() or provider.strip().lower() == "anthropic":
            raise LifecycleError("the worker record has no usable provider (never anthropic): refusing to launch")
        try:
            routes = worker_spawner.load_verified_routes(registry_path(state_dir))
        except worker_spawner.SpawnRefused as exc:
            raise LifecycleError("verified-route registry unavailable: refusing to launch (" + str(exc) + ")") from None
        # The (provider, model) PAIR must be verified: a restarted old record cannot ride on another provider's route.
        if worker_spawner.is_claude_model(model) or not any(
                r.model == model and r.provider == provider and r.cost_zero is True for r in routes):
            raise LifecycleError("the worker provider and model are not a verified zero-cost route: refusing to launch")
        cfg = worker_config_dir(state_dir)
        try:
            env = provider_env.worker_env(str(cfg), base_env)
        except provider_env.WorkerEnvError as exc:
            raise LifecycleError("worker environment is not safe: refusing to launch (" + str(exc) + ")") from None
        try:
            cfg.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise LifecycleError("worker config directory could not be created: refusing to launch") from None
        return env
    return build


def make_worker_launcher(executable: str, state_dir: PathLike,
                         base_env: Optional[Mapping[str, str]] = None) -> ClaudeCliLauncher:
    """The only ClaudeCliLauncher production code builds: isolated environment, project-only settings, verified route."""
    return ClaudeCliLauncher(executable, env_builder=make_worker_env_builder(state_dir, base_env),
                             extra_args=provider_env.worker_claude_args())
