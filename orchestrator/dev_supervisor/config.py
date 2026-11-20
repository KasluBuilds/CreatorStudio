"""Machine-local configuration of the development supervisor (T-DEV-008, wave w03 lane L8).

Where the file lives (never in the repository):
- ``--config <file>`` on the command line, else the environment variable ``CREATORSTUDIO_DEV_SUPERVISOR_CONFIG``,
  else ``<default state directory>/config.json`` (see ``state_store.default_directory``: on Windows
  ``%LOCALAPPDATA%\\CreatorStudio\\dev_supervisor``).
- A missing file is not an error: every field has a documented default. A file inside a git working tree is refused,
  so a config can never be committed by accident.

Template (print it with ``python -B -m dev_supervisor config --example``; values in angle brackets are placeholders,
nothing here is a real machine value)::

    {
      "state_dir": "<machine-local folder outside any repository, or omit for the default>",
      "repo_root": "<path of the CreatorStudio checkout Main works in, or omit to use this checkout>",
      "github_repo": "<owner>/<repository>",
      "github_issue": 0,
      "wave_id": "<wave id whose lane worktrees doctor should check, or omit>",
      "models": {"main": "opus", "worker": "sonnet", "cheap": "haiku", "strongest": "opus", "standard": "sonnet"},
      "context_windows": {"<full model id as written in the transcript>": 200000},
      "thresholds": {"warn": 0.60, "prepare_handoff": 0.70, "compact": 0.80, "replace": 0.90},
      "compact_tokens": 400000,
      "compact_failed_observations": 6,
      "main_permission_mode": null,
      "worker_permission_mode": null,
      "dangerously_skip_permissions": false,
      "omniroute_base_url": "http://localhost:20128",
      "allow_paid_api": false,
      "claude_executable": "claude",
      "sessions_dir": "<folder holding Claude Code per-process session files, or omit for ~/.claude/sessions>",
      "projects_dir": "<folder holding Claude Code transcripts, or omit for ~/.claude/projects>",
      "resolve_identity_config": "<machine-local Resolve identity JSON, or omit>",
      "poll_interval_s": 15,
      "worker_max_runtime_s": 14400,
      "auto_replace_main": false,
      "exit_with_main": true
    }

Rules (IMPLEMENTED, tested offline):
- No credential is ever stored here. A key that looks like a credential (``api_key``, ``token``, ``password`` ...) or a
  credential-shaped value makes the load fail (``ConfigError``); the OmniRoute key stays where OmniRoute keeps it and is
  read by ``omniroute.read_api_key`` at call time only.
- ``allow_paid_api`` defaults to False (routing never ranks a paid API route unless it is True).
- ``dangerously_skip_permissions`` defaults to False. Only when it is True is ``--dangerously-skip-permissions``
  passed to Main, and only then may the ``bypassPermissions`` permission mode be configured for Main or workers.
- Models are explicit ids or CLI aliases (``claude --help``: "Provide an alias for the latest model (e.g. 'fable',
  'opus', or 'sonnet') or a model's full name").
- Claude Pro versus OmniRoute: this file does NOT choose the provider. The provider of the Main session is decided by
  which local launcher the user runs to open the terminal (the user's own Claude Pro launcher or the OmniRoute one);
  the supervisor inherits that environment and never reads, copies or edits the launchers or ``.mcp.json``.

The documented single launch command (run from ``orchestrator/`` in a terminal opened by the launcher of choice)::

    python -B -m dev_supervisor start --yes
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .state_store import (UnsafeStoreLocation, check_location, contains_secret, default_directory,
                          this_repository_root)

ENV_CONFIG = "CREATORSTUDIO_DEV_SUPERVISOR_CONFIG"
CONFIG_NAME = "config.json"
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")  # claude 2.1.316
DANGEROUS_MODES = ("bypassPermissions",)
DEFAULT_MODELS = {"main": "opus", "worker": "sonnet", "cheap": "haiku", "strongest": "opus", "standard": "sonnet"}
DEFAULT_THRESHOLDS = {"warn": 0.60, "prepare_handoff": 0.70, "compact": 0.80, "replace": 0.90}
DEFAULT_COMPACT_TOKENS = 400_000       # T-DEV-030 absolute cost guard (None disables); see CONTEXT_MANAGEMENT.md
DEFAULT_COMPACT_FAILED_OBSERVATIONS = 6
DEFAULT_OMNIROUTE = "http://localhost:20128"


class ConfigError(ValueError):
    """The machine-local configuration is unusable (nothing is guessed)."""


@dataclass
class Config:
    state_dir: Optional[str] = None
    repo_root: Optional[str] = None
    github_repo: str = ""
    github_issue: int = 0
    wave_id: str = ""
    models: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    context_windows: Dict[str, int] = field(default_factory=dict)   # merged over telemetry.DEFAULT_CONTEXT_WINDOWS
    thresholds: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    compact_tokens: Optional[int] = DEFAULT_COMPACT_TOKENS
    compact_failed_observations: int = DEFAULT_COMPACT_FAILED_OBSERVATIONS
    main_permission_mode: Optional[str] = None
    worker_permission_mode: Optional[str] = None
    dangerously_skip_permissions: bool = False
    omniroute_base_url: str = DEFAULT_OMNIROUTE
    allow_paid_api: bool = False
    claude_executable: str = "claude"
    sessions_dir: Optional[str] = None
    projects_dir: Optional[str] = None
    resolve_identity_config: Optional[str] = None
    poll_interval_s: float = 15.0
    worker_max_runtime_s: float = 4 * 3600.0
    auto_replace_main: bool = False
    exit_with_main: bool = True
    source: str = "defaults"          # where the values came from (a path, or "defaults"); not a config key

    # ------------------------------------------------------------ derived values

    def state_directory(self, env: Optional[Mapping[str, str]] = None) -> Path:
        return Path(self.state_dir) if self.state_dir else default_directory(dict(env) if env is not None else None)

    def repository_root(self) -> Optional[Path]:
        return Path(self.repo_root) if self.repo_root else this_repository_root()

    def claude_home(self) -> Path:
        return Path(os.path.expanduser("~")) / ".claude"

    def sessions_directory(self) -> Path:
        return Path(self.sessions_dir) if self.sessions_dir else self.claude_home() / "sessions"

    def projects_directory(self) -> Path:
        return Path(self.projects_dir) if self.projects_dir else self.claude_home() / "projects"

    def model(self, role: str) -> str:
        m = self.models.get(role) or DEFAULT_MODELS.get(role)
        if not m:
            raise ConfigError(f"no model configured for role {role!r}")
        return m

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("source", None)
        return d


_KEYS = {f for f in Config.__dataclass_fields__ if f != "source"}


def config_path(explicit: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    if explicit:
        return Path(explicit)
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG])
    return default_directory(dict(env)) / CONFIG_NAME


def validate(cfg: Config) -> None:
    """Raise ``ConfigError`` on any unsafe or malformed value."""
    for name in ("main_permission_mode", "worker_permission_mode"):
        mode = getattr(cfg, name)
        if mode is None:
            continue
        if mode not in PERMISSION_MODES:
            raise ConfigError(f"{name} {mode!r} is not one of {', '.join(PERMISSION_MODES)}")
        if mode in DANGEROUS_MODES and not cfg.dangerously_skip_permissions:
            raise ConfigError(f"{name} {mode!r} needs dangerously_skip_permissions: true in the machine-local config")
    if not isinstance(cfg.dangerously_skip_permissions, bool) or not isinstance(cfg.allow_paid_api, bool):
        raise ConfigError("dangerously_skip_permissions and allow_paid_api must be true or false")
    if not isinstance(cfg.models, dict) or not all(isinstance(v, str) and v for v in cfg.models.values()):
        raise ConfigError("models must map roles to non-empty model ids")
    if not isinstance(cfg.context_windows, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in cfg.context_windows.values()):
        raise ConfigError("context_windows must map model ids to positive integers")
    t = cfg.thresholds
    if not isinstance(t, dict) or set(t) != {"warn", "prepare_handoff", "compact", "replace"}:
        raise ConfigError("thresholds need exactly warn, prepare_handoff, compact and replace")
    try:
        ok = (0 < float(t["warn"]) <= float(t["prepare_handoff"]) <= float(t["compact"]) <= float(t["replace"]) <= 1)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise ConfigError("thresholds must satisfy 0 < warn <= prepare_handoff <= compact <= replace <= 1")
    ct = cfg.compact_tokens
    if ct is not None and (isinstance(ct, bool) or not isinstance(ct, int) or ct <= 0):
        raise ConfigError("compact_tokens must be a positive integer or null")
    k = cfg.compact_failed_observations
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ConfigError("compact_failed_observations must be a positive integer")
    if not isinstance(cfg.github_issue, int) or isinstance(cfg.github_issue, bool) or cfg.github_issue < 0:
        raise ConfigError("github_issue must be a non-negative integer")
    if cfg.github_repo and (cfg.github_repo.count("/") != 1 or any(c.isspace() for c in cfg.github_repo)):
        raise ConfigError("github_repo must look like <owner>/<repository>")
    for name in ("poll_interval_s", "worker_max_runtime_s"):
        v = getattr(cfg, name)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(f"{name} must be a positive number")


def from_dict(data: Mapping[str, Any], source: str = "dict") -> Config:
    if not isinstance(data, Mapping):
        raise ConfigError("the configuration must be a JSON object")
    if contains_secret(dict(data)):
        raise ConfigError("the configuration contains a credential-like key or value; credentials are never stored "
                          "in the supervisor config (remove it)")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise ConfigError(f"unknown configuration keys: {', '.join(unknown)}")
    kw = dict(data)
    if "models" in kw and isinstance(kw["models"], dict):
        kw["models"] = {**DEFAULT_MODELS, **kw["models"]}
    if "thresholds" in kw and isinstance(kw["thresholds"], dict):
        kw["thresholds"] = {**DEFAULT_THRESHOLDS, **kw["thresholds"]}
    cfg = Config(**kw, source=source)
    validate(cfg)
    return cfg


def load(explicit: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> Config:
    """Load the machine-local config (defaults when the file does not exist). Raises ``ConfigError``."""
    path = config_path(explicit, env)
    try:
        check_location(path.parent, [r for r in [this_repository_root()] if r is not None])
    except UnsafeStoreLocation as e:
        raise ConfigError(f"refusing a configuration file inside a repository working tree ({e})") from None
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"configuration file not found: {path}") from None
        cfg = Config()
        validate(cfg)
        return cfg
    except OSError as e:
        raise ConfigError(f"configuration file unreadable ({e.__class__.__name__})") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConfigError("configuration file is not valid JSON") from None
    return from_dict(data, source=str(path))


def example() -> Dict[str, Any]:
    """The documented template with placeholders only (no machine values)."""
    return {
        "state_dir": "<machine-local folder outside any repository, or omit for the default>",
        "repo_root": "<path of the checkout Main works in, or omit to use this checkout>",
        "github_repo": "<owner>/<repository>",
        "github_issue": 0,
        "wave_id": "<wave id whose lane worktrees doctor should check, or omit>",
        "models": dict(DEFAULT_MODELS),
        "context_windows": {"<full model id as written in the transcript>": 200000},
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "compact_tokens": DEFAULT_COMPACT_TOKENS,
        "compact_failed_observations": DEFAULT_COMPACT_FAILED_OBSERVATIONS,
        "main_permission_mode": None,
        "worker_permission_mode": None,
        "dangerously_skip_permissions": False,
        "omniroute_base_url": DEFAULT_OMNIROUTE,
        "allow_paid_api": False,
        "claude_executable": "claude",
        "sessions_dir": "<folder of Claude Code session files, or omit for the default>",
        "projects_dir": "<folder of Claude Code transcripts, or omit for the default>",
        "resolve_identity_config": "<machine-local Resolve identity JSON, or omit>",
        "poll_interval_s": 15,
        "worker_max_runtime_s": 14400,
        "auto_replace_main": False,
        "exit_with_main": True,
    }
