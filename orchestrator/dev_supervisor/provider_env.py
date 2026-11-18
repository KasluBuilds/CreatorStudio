"""Explicit environment for the ``claude`` processes the supervisor starts.

Why this exists (OBSERVED 2026-11-18): Windows User-scope variables ``ANTHROPIC_BASE_URL`` (the local OmniRoute
gateway) and ``ANTHROPIC_MODEL`` (``auto/coding``) were set persistently, so every new terminal, and therefore the
supervisor's children, inherited a gateway URL while logged in to Claude Pro. ``--model opus`` then resolved to a
model id (``claude-opus-5-5``) that the gateway does not serve. The repository launchers ``pro.ps1`` and ``backup.ps1``
only set process-level variables and are not the cause; they are neither read nor changed here.

Providers (``CREATORSTUDIO_DEV_PROVIDER``, default ``pro``):
- ``pro``: the Claude Pro login. Gateway and credential overrides are removed from the child environment, as
  ``pro.ps1`` does, and ``ANTHROPIC_MODEL`` is removed so the explicit ``--model`` governs.
- ``inherit``: the child gets the caller's environment unchanged (for a terminal started with ``backup.ps1``).
Credential values are never read, logged or returned by this module; only variable names are handled.
"""
from __future__ import annotations

import os
from typing import Dict, Mapping, Optional

ENV_PROVIDER = "CREATORSTUDIO_DEV_PROVIDER"
PROVIDERS = ("pro", "inherit")
_STRIPPED_FOR_PRO = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                     "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
                     "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT", "CLAUDE_CODE_DISABLE_REASONING_EFFORT")


def provider(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    value = (env.get(ENV_PROVIDER) or "pro").strip().lower()
    if value not in PROVIDERS:
        raise ValueError(f"{ENV_PROVIDER} must be one of {', '.join(PROVIDERS)}, not {value!r}")
    return value


def child_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment for a ``claude`` child process."""
    base = dict(os.environ if base is None else base)
    if provider(base) == "inherit":
        return base
    return {k: v for k, v in base.items() if k.upper() not in _STRIPPED_FOR_PRO}
