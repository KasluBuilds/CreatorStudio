"""Telemetry for Claude Code sessions: session files, context pressure, usage-provider interface.

IMPORTANT: nothing here is an account-usage reading (plan limits, reset windows) unless a UsageProvider
supplies one. With no provider, usage is UNAVAILABLE with the reason. Context pressure is a per-session
figure computed from the last assistant usage record in the transcript against a configured model window;
it is not an account figure. No transcript text is ever returned: only token counts and the model id.

Stdlib only. No network.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .evidence import Evidence, inferred, now_iso, observed, reported, unavailable

SCHEMA_VERSION = 1

# Documented defaults. Windows are configuration, not facts about any account; unknown model -> UNAVAILABLE.
DEFAULT_CONTEXT_WINDOWS = {
    # Claude 5 family: 1M context. Observed locally: opus-5-5 at 419,585 and sonnet-5-5 at 898,255 tokens (issue #58).
    "claude-fable-5-1": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-haiku-5-5": 1_000_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
}
# Ratios of the window: warn at 60%, compact at 80%, replace the session at 92%.
DEFAULT_THRESHOLDS = {"warn": 0.60, "compact": 0.80, "replace": 0.92}

TAIL_BYTES = 256 * 1024           # initial tail window
MAX_TAIL_BYTES = 8 * 1024 * 1024  # never read more than this from the end


@dataclass
class PressureConfig:
    windows: dict = field(default_factory=lambda: dict(DEFAULT_CONTEXT_WINDOWS))
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))

    def window_for(self, model: Optional[str]) -> Optional[int]:
        """Exact model-id lookup only; no guessing for unknown ids."""
        if not model:
            return None
        return self.windows.get(model)

    def level(self, ratio: float) -> str:
        t = self.thresholds
        if ratio >= t["replace"]:
            return "replace"
        if ratio >= t["compact"]:
            return "compact"
        if ratio >= t["warn"]:
            return "warn"
        return "ok"


def default_pid_alive(pid: int) -> bool:
    """Liveness probe. On Windows os.kill(pid, 0) is unreliable, so OpenProcess is used."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _iso_from_ms(ms) -> Optional[str]:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _str(raw, key, src, ts):
    v = raw.get(key)
    return observed(v, src, ts) if isinstance(v, str) and v else unavailable("%s missing" % key, src, ts)


def read_sessions(sessions_dir, pid_alive: Callable[[int], bool] = default_pid_alive,
                  stale_after_seconds: float = 300.0, now: Optional[datetime] = None) -> list:
    """Read per-process session files (<pid>.json). Missing dir -> []. Corrupt files yield an entry with
    an `error` Evidence instead of raising. `stale` is True when the status was last updated more than
    stale_after_seconds ago, or no update time exists. `alive` is the (injectable) pid probe result."""
    d = Path(sessions_dir)
    out = []
    try:
        files = sorted(p for p in d.iterdir() if p.suffix == ".json")
    except (OSError, ValueError):
        return out
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat()
    for p in files:
        src = "session_file:" + p.name
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as e:
            out.append({"file": p.name, "error": unavailable("unreadable session file: %s" % type(e).__name__,
                                                             src, ts)})
            continue
        pid = raw.get("pid")
        pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        try:
            alive = bool(pid_alive(pid)) if pid is not None else False
        except Exception:
            alive = False
        upd = _iso_from_ms(raw.get("statusUpdatedAt") or raw.get("updatedAt"))
        status = raw.get("status")
        if status in ("busy", "idle"):
            status_ev = observed(status, src, upd or ts)
        elif isinstance(status, str) and status:
            status_ev = reported(status, src, upd or ts)
        else:
            status_ev = unavailable("status missing", src, ts)
        started = _iso_from_ms(raw.get("startedAt"))
        upd_ev = observed(upd, src, upd) if upd else unavailable("no update timestamp", src, ts)
        out.append({
            "file": p.name,
            "pid": observed(pid, src, ts) if pid is not None else unavailable("pid missing", src, ts),
            "session_id": _str(raw, "sessionId", src, ts),
            "cwd": _str(raw, "cwd", src, ts),
            "name": _str(raw, "name", src, ts),
            "status": status_ev,
            "started_at": observed(started, src, ts) if started else unavailable("startedAt missing", src, ts),
            "messaging_socket": _str(raw, "messagingSocketPath", src, ts),
            "alive": observed(alive, "pid_probe", ts),
            "updated_at": upd_ev,
            "stale": inferred(True if not upd else upd_ev.is_stale(stale_after_seconds, now), src, ts),
        })
    return out


def _tail(path: Path, nbytes: int):
    size = path.stat().st_size
    start = max(0, size - nbytes)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    return data, start > 0


def _usage_from_line(line: bytes) -> Optional[dict]:
    if b'"usage"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict) or (rec.get("type") != "assistant" and msg.get("role") != "assistant"):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    vals = [usage.get(k, 0) for k in keys]
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in vals):
        return None
    model = msg.get("model")
    out = dict(zip(keys, vals))
    out["total"] = sum(vals)
    out["model"] = model if isinstance(model, str) and model else None
    return out


def read_last_usage(transcript_path) -> Optional[dict]:
    """Return token counts and model id from the LAST assistant record that has usage, reading only the file
    tail (growing up to MAX_TAIL_BYTES). Never returns text. None if not found. Raises OSError if unreadable."""
    path = Path(transcript_path)
    nbytes = TAIL_BYTES
    while True:
        data, truncated = _tail(path, nbytes)
        lines = data.split(b"\n")
        if truncated:
            lines = lines[1:]  # first line is probably partial
        for line in reversed(lines):
            u = _usage_from_line(line)
            if u:
                return u
        if not truncated or nbytes >= MAX_TAIL_BYTES:
            return None
        nbytes = min(nbytes * 4, MAX_TAIL_BYTES)


def _all_unavailable(reason, src, ts):
    r = unavailable(reason, src, ts)
    return {"tokens": r, "model": r, "window": r, "ratio": r, "level": r}


def context_pressure(transcript_path, config: Optional[PressureConfig] = None) -> dict:
    """Labelled per-session context pressure. Fields tokens, model, window, ratio, level (each Evidence)."""
    cfg = config or PressureConfig()
    ts = now_iso()
    src = "transcript_tail"
    if transcript_path is None or not Path(transcript_path).is_file():
        return _all_unavailable("transcript not found (an interactive claude started with inherited parent-session markers such as CLAUDE_CODE_CHILD_SESSION writes none; see issue #58)", src, ts)
    try:
        u = read_last_usage(transcript_path)
    except OSError as e:
        return _all_unavailable("transcript unreadable: %s" % type(e).__name__, src, ts)
    if u is None:
        return _all_unavailable("no assistant usage record found", src, ts)
    tokens = observed(u["total"], src, ts)
    model = observed(u["model"], src, ts) if u["model"] else unavailable("model id missing", src, ts)
    win = cfg.window_for(u["model"])
    if win is None:
        why = "no model id" if not u["model"] else "model not in configured window table"
        r = unavailable("pressure unavailable: " + why, src, ts)
        return {"tokens": tokens, "model": model, "window": r, "ratio": r, "level": r}
    if u["total"] > win:
        # A session cannot hold more than its window: the configured window is wrong. Never turn that into REPLACE.
        r = unavailable("pressure unavailable: observed tokens exceed configured window %d (window table wrong)" % win, src, ts)
        return {"tokens": tokens, "model": model, "window": r, "ratio": r, "level": r}
    ratio = u["total"] / win
    return {"tokens": tokens, "model": model, "window": inferred(win, "config", ts),
            "ratio": inferred(round(ratio, 4), src, ts), "level": inferred(cfg.level(ratio), "config", ts)}


def cwd_slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_transcript(projects_dir, session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """Locate <projects_dir>/<slug>/<sessionId>.jsonl; fall back to scanning project dirs."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
        return None
    base = Path(projects_dir)
    if cwd:
        p = base / cwd_slug(cwd) / (session_id + ".jsonl")
        if p.is_file():
            return p
    try:
        for d in base.iterdir():
            p = d / (session_id + ".jsonl")
            if p.is_file():
                return p
    except OSError:
        pass
    return None


# ---- account usage: pluggable provider ---------------------------------------------------------------

class UsageProvider:
    """Interface. get_usage() returns a dict of Evidence (e.g. five_hour_used_percentage) or None."""
    name = "provider"

    def get_usage(self) -> Optional[dict]:
        raise NotImplementedError


class UnavailableProvider(UsageProvider):
    name = "none"

    def __init__(self, reason="no account-usage source is configured"):
        self.reason = reason

    def get_usage(self):
        return None


class StatuslineProvider(UsageProvider):
    """Ingests a statusline JSON payload (dict or JSON string) supplied by the caller. Values are REPORTED:
    taken as stated, only keys present are returned. Optional shape:
    {"rate_limits": {"five_hour": {"used_percentage": n, "resets_at": t}, "seven_day": {...}}}.
    Anything else yields no usage (UNAVAILABLE at snapshot level)."""
    name = "statusline"

    def __init__(self, payload=None):
        self.payload = payload
        self.reason = "statusline payload missing, invalid or without rate_limits"

    def get_usage(self):
        p = self.payload
        if isinstance(p, (str, bytes)):
            try:
                p = json.loads(p)
            except ValueError:
                return None
        if not isinstance(p, dict):
            return None
        rl = p.get("rate_limits")
        if not isinstance(rl, dict):
            return None
        ts = now_iso()
        out = {}
        for key, name in (("five_hour", "five_hour"), ("seven_day", "weekly")):
            w = rl.get(key)
            if not isinstance(w, dict):
                continue
            pct, rs = w.get("used_percentage"), w.get("resets_at")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                out[name + "_used_percentage"] = reported(pct, "statusline", ts)
            if isinstance(rs, (int, float, str)) and not isinstance(rs, bool) and rs != "":
                out[name + "_resets_at"] = reported(rs, "statusline", ts)
        return out or None


def account_usage(provider: Optional[UsageProvider]) -> dict:
    ts = now_iso()
    if provider is None:
        provider = UnavailableProvider()
    try:
        u = provider.get_usage()
    except Exception as e:
        return {"provider": provider.name,
                "usage": unavailable("provider failed: %s" % type(e).__name__, provider.name, ts)}
    if not u:
        reason = getattr(provider, "reason", "provider returned no usage data")
        return {"provider": provider.name, "usage": unavailable(reason, provider.name, ts)}
    return {"provider": provider.name, "usage": u}


def _ser(x):
    if isinstance(x, Evidence):
        return x.to_dict()
    if isinstance(x, dict):
        return {k: _ser(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_ser(v) for v in x]
    return x


def snapshot(sessions_dir, projects_dir=None, config: Optional[PressureConfig] = None,
             usage_provider: Optional[UsageProvider] = None,
             pid_alive: Callable[[int], bool] = default_pid_alive, stale_after_seconds: float = 300.0,
             now: Optional[datetime] = None) -> dict:
    """JSON-serialisable snapshot: {schema_version, generated_at, sessions[], account_usage}."""
    sessions = []
    for s in read_sessions(sessions_dir, pid_alive, stale_after_seconds, now):
        if "error" not in s:
            sid = s["session_id"].value if s["session_id"].available else None
            cwd = s["cwd"].value if s["cwd"].available else None
            if projects_dir and sid:
                s["context"] = context_pressure(find_transcript(projects_dir, sid, cwd), config)
            else:
                s["context"] = _all_unavailable("no projects dir or session id", "config", now_iso())
        sessions.append(s)
    return _ser({"schema_version": SCHEMA_VERSION, "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
                 "sessions": sessions, "account_usage": account_usage(usage_provider)})
