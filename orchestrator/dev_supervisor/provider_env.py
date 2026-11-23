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
Parent-session markers (OBSERVED 2026-11-18, issue #58): when the supervisor is started from a shell inside
another Claude Code session, the children inherit that session's ``CLAUDECODE`` / ``CLAUDE_CODE_CHILD_SESSION``
markers and its session id, pid and messaging socket. An interactive ``claude`` started with those markers writes
no transcript and no ``sessions/<pid>.json``, so context telemetry and automatic handover cannot see Main. These
variables are removed for every provider, ``inherit`` included; they describe the parent, never the child.
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

AUTHORITY_PREFIX = "CS_AUTHORITY_"     # policy variables come only from session_policy, never from the parent

PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
                       "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
                       "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_EXECPATH")


def parent_session_markers(env: Optional[Mapping[str, str]] = None) -> list:
    """Names (never values) of parent Claude Code session variables present in ``env``."""
    env = os.environ if env is None else env
    return sorted(k for k in env if k.upper() in PARENT_SESSION_VARS)


def provider(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    value = (env.get(ENV_PROVIDER) or "pro").strip().lower()
    if value not in PROVIDERS:
        raise ValueError(f"{ENV_PROVIDER} must be one of {', '.join(PROVIDERS)}, not {value!r}")
    return value


def child_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment for a ``claude`` child process."""
    base = dict(os.environ if base is None else base)
    stripped = PARENT_SESSION_VARS if provider(base) == "inherit" else PARENT_SESSION_VARS + _STRIPPED_FOR_PRO
    return {k: v for k, v in base.items() if k.upper() not in stripped and not k.upper().startswith(AUTHORITY_PREFIX)}


# ---------------------------------------------------------------------------------------------------------------------
# Worker environment (T-DEV-033). Explicit, separate from the Main/default provider above.
# A worker is never started on Claude Pro through this function: it requires the local OmniRoute gateway and refuses
# otherwise. Errors name variables that are missing, never their values or the URL.
# Residual risk (not closed here): a settings.json "env" block could reintroduce provider or base-URL settings that an
# environment filter cannot see. ANTHROPIC_MODEL is kept on purpose: the dispatcher chooses the model per worker.
# ---------------------------------------------------------------------------------------------------------------------
WORKER_REQUIRED_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
WORKER_GATEWAY_FLAGS = {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
                        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
                        "CLAUDE_CODE_DISABLE_REASONING_EFFORT": "1"}
WORKER_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
WORKER_SCHEMES = ("http", "https")
# Direct Anthropic credentials, provider switches and proxies that would send a worker around the gateway.
_WORKER_STRIPPED = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_UNIX_SOCKET",
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_WORKER_STRIPPED_PREFIXES = ("CLAUDE_CODE_USE_",)


class WorkerEnvError(Exception):
    """Refusal to build a worker environment. The message names variables, never values or the URL."""


def _gateway_url_is_local(url: str) -> bool:
    """Strict check: only a plain http(s) URL whose host, as both Python and WHATWG parsers see it, is local."""
    import urllib.parse
    if any(c == "\\" or c.isspace() or ord(c) < 32 for c in url):
        return False                       # backslash and whitespace parse differently in WHATWG (Claude Code)
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port                         # raises ValueError on a bad or out-of-range port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme in WORKER_SCHEMES and bool(host) and host in WORKER_LOCAL_HOSTS
            and parts.netloc.count("@") == 0 and not parts.netloc.endswith(":"))


def worker_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment for a worker's ``claude`` child, routed through the local OmniRoute gateway only.

    Refuses (WorkerEnvError) when the gateway variables are absent or the base URL is not a plain local http(s) URL.
    Removes direct Anthropic credentials, Claude Code provider switches, proxies and parent-session markers.
    Never falls back to Claude Pro.
    """
    base = dict(os.environ if base is None else base)
    values = {k: (base.get(k) or "").strip() for k in WORKER_REQUIRED_VARS}
    missing = [k for k in WORKER_REQUIRED_VARS if not values[k]]
    if missing:
        raise WorkerEnvError("OmniRoute gateway not configured; missing variable(s): " + ", ".join(missing))
    if not _gateway_url_is_local(values["ANTHROPIC_BASE_URL"]):
        raise WorkerEnvError("OmniRoute gateway must be a plain local http(s) URL (ANTHROPIC_BASE_URL is refused)")

    def _drop(name: str) -> bool:
        upper = name.upper()
        return (upper in PARENT_SESSION_VARS or upper in _WORKER_STRIPPED
                or upper.startswith(_WORKER_STRIPPED_PREFIXES) or upper.startswith(AUTHORITY_PREFIX)
                or upper in {f.upper() for f in WORKER_GATEWAY_FLAGS}
                or upper in {r.upper() for r in WORKER_REQUIRED_VARS})

    out = {k: v for k, v in base.items() if not _drop(k)}
    out.update(values)                     # the stripped values are what the child receives
    out.update(WORKER_GATEWAY_FLAGS)       # forced, and no parent spelling of these names survives
    return out
