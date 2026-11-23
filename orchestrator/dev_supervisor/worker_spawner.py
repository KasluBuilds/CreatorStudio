"""Worker spawner (T-DEV-034): the only code path that starts a worker ``claude`` process.

A worker starts only when ALL of these hold, and every refusal raises ``SpawnRefused`` with a reason that names the
failed check (never a URL, token or path to a credential):

- the route is in a verified registry: model, provider, a non-empty evidence string and ``cost_zero`` true;
- the config directory is worker-owned: non-blank, not the default ``~/.claude`` and not inside it;
- the gateway environment is valid (``provider_env.worker_env``: local OmniRoute gateway, token present, no API key,
  no provider switch, no parent-session markers);
- the worktree exists.

The child is started with ``worker_claude_args()`` (``--setting-sources project``) and the worker environment, in the
worktree, without a shell. Standard library only.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import provider_env

#: Providers that a verified worker route may name. Claude Pro (anthropic) and direct paid APIs are never listed.
ALLOWED_PROVIDERS = ("gemini", "groq")
CLAUDE_EXECUTABLE = "claude"


class SpawnRefused(Exception):
    """A worker was not started. The message names the failed check only."""


@dataclass(frozen=True)
class VerifiedRoute:
    model: str
    provider: str
    evidence: str       # where the verification is recorded (test run, PR, observation id)
    cost_zero: bool     # recorded zero cost on the route


def load_verified_routes(path: Path) -> List[VerifiedRoute]:
    """Read the verified-route registry. Malformed entries are refused, never guessed."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SpawnRefused("verified route registry is missing or unreadable") from None
    routes: List[VerifiedRoute] = []
    for entry in data.get("routes", []):
        model = str(entry.get("model", "")).strip()
        provider = str(entry.get("provider", "")).strip()
        evidence = str(entry.get("evidence", "")).strip()
        cost_zero = entry.get("cost_zero") is True
        if not model or provider not in ALLOWED_PROVIDERS or not evidence or not cost_zero:
            continue   # only a route recorded as zero-cost is loaded
        routes.append(VerifiedRoute(model, provider, evidence, cost_zero))
    return routes


def _refuse_config_dir(config_dir: Optional[str]) -> str:
    if config_dir is None or not str(config_dir).strip():
        raise SpawnRefused("a worker-owned config directory is required")
    cfg = Path(str(config_dir).strip()).expanduser().resolve()
    default = (Path.home() / ".claude").resolve()
    if cfg == default or default in cfg.parents or cfg in default.parents:
        raise SpawnRefused("config directory is the default Claude config location or overlaps it")
    return str(cfg)


def build_worker_command(route: VerifiedRoute, worktree: Path, config_dir: Optional[str], prompt: str,
                         verified: Sequence[VerifiedRoute], base_env: Optional[Mapping[str, str]] = None,
                         *, _executable: str = CLAUDE_EXECUTABLE,
                         _prefix: Sequence[str] = ()) -> Tuple[List[str], Dict[str, str], Path]:
    """Return (argv, env, cwd) for a worker, or raise SpawnRefused. Pure: starts nothing."""
    if route.provider not in ALLOWED_PROVIDERS:
        raise SpawnRefused("route provider is not a worker provider")
    if not route.cost_zero:
        raise SpawnRefused("route cost is not recorded as zero")
    if verified is None or not any(r.model == route.model and r.provider == route.provider and r.cost_zero
                                   for r in verified):
        raise SpawnRefused("route is not in the verified registry")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SpawnRefused("a worker prompt is required")
    if not isinstance(worktree, (str, os.PathLike)):
        raise SpawnRefused("a worktree path is required")
    cwd = Path(worktree)
    if not cwd.is_dir():
        raise SpawnRefused("worktree does not exist")
    cfg = _refuse_config_dir(config_dir)
    try:
        env = provider_env.worker_env(cfg, base_env)
    except provider_env.WorkerEnvError as exc:
        raise SpawnRefused(str(exc)) from None
    # Runtime assertions on the object we will actually pass to the child.
    if env.get("CLAUDE_CONFIG_DIR") != cfg:
        raise SpawnRefused("worker environment does not carry the worker config directory")
    if "ANTHROPIC_API_KEY" in env or "CLAUDE_CODE_OAUTH_TOKEN" in env:
        raise SpawnRefused("worker environment carries a direct credential")
    argv = [_executable, *_prefix, *provider_env.worker_claude_args(), "-p", prompt, "--model", route.model]
    return argv, env, cwd


def spawn_worker(route: VerifiedRoute, worktree: Path, config_dir: Optional[str], prompt: str,
                 verified: Sequence[VerifiedRoute], base_env: Optional[Mapping[str, str]] = None,
                 popen: Any = subprocess.Popen) -> subprocess.Popen:
    """Start the production worker: ``claude`` with the isolation arguments and the worker environment."""
    argv, env, cwd = build_worker_command(route, worktree, config_dir, prompt, verified, base_env)
    return popen(argv, cwd=str(cwd), env=env, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
