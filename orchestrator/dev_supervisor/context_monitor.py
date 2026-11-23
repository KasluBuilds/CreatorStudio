"""Claude Code hook and statusLine commands for unattended context management (T-DEV-030, issue #44).

Run by Claude Code inside Main as ``python -I -B <this file> <command> --state-dir <dir>`` (``-I`` removes the script
folder from the import path, so this module is standard library only and imports nothing from the package):

``statusline``   statusLine command. Reads the JSON Claude Code sends on stdin, writes
                 ``<state>/context/<session_id>.json`` atomically as ``{session_id, used_percentage,
                 context_window_size, model, utc}`` (OBSERVED by Claude Code itself), prints one short status line.
                 ``--chain-file <json>`` names a file ``{"command": "..."}`` holding the user's own statusLine command:
                 ours runs first, then theirs with the same stdin, and THEIR output is printed. If their command fails
                 or prints nothing, ours is printed. It never crashes: bad input prints a minimal line.
``notice``       PostToolUse hook. When the supervisor wrote ``<state>/context/notice-<session_id>.json`` and that notice
                 was not shown yet, prints ``hookSpecificOutput.additionalContext`` once ("context at X%: durable
                 handoff <id> prepared; ..."). Silent otherwise. Never blocks (always exit 0, no decision field).
``precompact``   PreCompact hook. Writes ``<state>/context/compaction-started-<session_id>.json`` so the supervisor
                 knows a compaction began. Never blocks compaction (no output, exit 0).
``live-compaction-probe``  OPT-IN proof that native compaction works: a throwaway headless claude session with
                 CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1, the haiku model and a temp folder as cwd, a few turns, then checks
                 for a compact_boundary record. Needs ``--yes``; unit tests never run it.

Every command exits 0 (the probe exits 1 when no boundary was seen).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

CONTEXT_SUBDIR = "context"
AUTOCOMPACT_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_HANDOFF_ID = re.compile(r"^h-[0-9a-f]{12}$")
MINIMAL_LINE = "ctx n/a"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def context_dir(state_dir: Any) -> Path:
    return Path(state_dir) / CONTEXT_SUBDIR


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _payload(text: str) -> Dict[str, Any]:
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _session_id(payload: Dict[str, Any]) -> Optional[str]:
    sid = payload.get("session_id")
    return sid if isinstance(sid, str) and _SID.match(sid) else None


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ------------------------------------------------------------------ statusLine

def statusline_record(payload: Dict[str, Any], now: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The record to persist, or None when the payload has no usable session id / used_percentage / window size."""
    sid = _session_id(payload)
    cw = payload.get("context_window")
    if sid is None or not isinstance(cw, dict):
        return None
    pct, size = _num(cw.get("used_percentage")), cw.get("context_window_size")
    if pct is None or not 0 <= pct <= 100 or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    model = payload.get("model")
    model_id = model.get("id") if isinstance(model, dict) else model
    return {"session_id": sid, "used_percentage": pct, "context_window_size": size,
            "model": model_id if isinstance(model_id, str) and model_id else None, "utc": now or utc_now()}


def _status_text(rec: Optional[Dict[str, Any]]) -> str:
    if rec is None:
        return MINIMAL_LINE
    return "ctx %d%% of %dk" % (round(rec["used_percentage"]), rec["context_window_size"] // 1000)


def _chain(chain_file: Optional[str], stdin_text: str, run: Callable[..., Any]) -> Optional[str]:
    """Output of the user's own statusLine command (same stdin), or None when there is none or it failed."""
    if not chain_file:
        return None
    try:
        spec = json.loads(Path(chain_file).read_text(encoding="utf-8"))
        cmd = spec.get("command") if isinstance(spec, dict) else None
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        r = run(cmd, input=stdin_text, capture_output=True, text=True, shell=True, timeout=10)
        out = (r.stdout or "").strip()
        return out if r.returncode == 0 and out else None
    except Exception:  # noqa: BLE001 - the user's command must never break ours
        return None


def run_statusline(stdin_text: str, state_dir: Any, chain_file: Optional[str] = None,
                   run: Callable[..., Any] = subprocess.run, now: Optional[str] = None) -> str:
    rec = None
    try:
        rec = statusline_record(_payload(stdin_text), now)
        if rec is not None:
            _atomic_write(context_dir(state_dir) / (rec["session_id"] + ".json"), json.dumps(rec, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 - never crash a status line
        pass
    return _chain(chain_file, stdin_text, run) or _status_text(rec)


# ------------------------------------------------------------------ PostToolUse notice

def notice_path(state_dir: Any, sid: str) -> Path:
    return context_dir(state_dir) / ("notice-%s.json" % sid)


def seen_path(state_dir: Any, sid: str) -> Path:
    return context_dir(state_dir) / ("notice-%s.seen" % sid)


def write_notice(state_dir: Any, sid: str, handoff_id: str, percent: float, refreshed: bool = False) -> Path:
    """Supervisor side: a new notice file (the notice id is the handoff id, so a refresh shows once more)."""
    p = notice_path(state_dir, sid)
    _atomic_write(p, json.dumps({"notice_id": handoff_id, "handoff_id": handoff_id, "percent": round(percent, 1),
                                 "refreshed": bool(refreshed), "utc": utc_now()}, sort_keys=True) + "\n")
    return p


def run_notice(stdin_text: str, state_dir: Any) -> Optional[str]:
    """Hook stdout (JSON) the first time a notice is seen, else None."""
    try:
        sid = _session_id(_payload(stdin_text))
        if sid is None:
            return None
        n = json.loads(notice_path(state_dir, sid).read_text(encoding="utf-8"))
        hid, pct, nid = n.get("handoff_id"), _num(n.get("percent")), n.get("notice_id")
        if not isinstance(hid, str) or not _HANDOFF_ID.match(hid) or pct is None or not isinstance(nid, str):
            return None
        try:
            if seen_path(state_dir, sid).read_text(encoding="utf-8").strip() == nid:
                return None
        except OSError:
            pass
        _atomic_write(seen_path(state_dir, sid), nid + "\n")
        text = ("context at %d%%: durable handoff %s prepared; add your own notes with dev_supervisor handoff "
                "if needed" % (round(pct), hid))
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}})
    except Exception:  # noqa: BLE001 - a notice is a courtesy; never block
        return None


# ------------------------------------------------------------------ PreCompact marker

def started_path(state_dir: Any, sid: str) -> Path:
    return context_dir(state_dir) / ("compaction-started-%s.json" % sid)


def run_precompact(stdin_text: str, state_dir: Any, now: Optional[str] = None) -> None:
    try:
        p = _payload(stdin_text)
        sid = _session_id(p)
        if sid is None:
            return
        trig = p.get("trigger")
        _atomic_write(started_path(state_dir, sid), json.dumps(
            {"session_id": sid, "utc": now or utc_now(), "trigger": trig if isinstance(trig, str) else None},
            sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        return


def read_started(state_dir: Any, sid: str) -> Optional[float]:
    """Epoch seconds of the PreCompact marker for ``sid`` (supervisor side), None when absent or unreadable."""
    try:
        rec = json.loads(started_path(state_dir, sid).read_text(encoding="utf-8"))
        t = datetime.fromisoformat(rec["utc"])
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ------------------------------------------------------------------ live proof (opt-in, never run by tests)

def _has_boundary(text: str) -> bool:
    for line in text.splitlines():
        if "compact_boundary" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "compact_boundary" in (rec.get("subtype"), rec.get("type")):
            return True
    return False


_PROBE_TOPICS = ("rivers", "bridges", "bread", "clocks", "lighthouses", "orchards")


def live_compaction_probe(claude: str = "claude", model: str = "haiku", turns: int = 4,
                          run: Callable[..., Any] = subprocess.run, projects_dir: Optional[Any] = None,
                          workdir: Optional[str] = None, timeout: float = 300.0) -> Dict[str, Any]:
    """A throwaway headless session: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1 makes Claude Code compact itself at 1% of the
    window, so a few turns must produce a ``compact_boundary`` record. Reports where it was seen."""
    if run is subprocess.run:
        # Starting claude from the caller's environment would use the Claude login (T-DEV-032): refused.
        raise RuntimeError("live compaction probe refused: it would start claude on the caller's Claude login")
    work = Path(workdir or tempfile.mkdtemp(prefix="cs-compaction-probe-"))
    work.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    env = dict(os.environ)
    env[AUTOCOMPACT_ENV] = "1"
    in_stream, codes = False, []
    for i in range(max(1, int(turns))):
        topic = _PROBE_TOPICS[i % len(_PROBE_TOPICS)]
        argv = [claude, "-p", "--model", model, "--output-format", "stream-json", "--verbose",
                "--session-id" if i == 0 else "--resume", sid,
                "Write about 120 words about %s. No tools." % topic]
        r = run(argv, cwd=str(work), env=env, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL)
        codes.append(r.returncode)
        in_stream = in_stream or _has_boundary(r.stdout or "")
    base = Path(projects_dir) if projects_dir else Path.home() / ".claude" / "projects"
    in_transcript, transcript = False, None
    try:
        for d in base.iterdir():
            p = d / (sid + ".jsonl")
            if p.is_file():
                transcript = str(p)
                in_transcript = _has_boundary(p.read_text(encoding="utf-8", errors="replace"))
                break
    except OSError:
        pass
    return {"session_id": sid, "workdir": str(work), "model": model, "turns": len(codes), "exit_codes": codes,
            "autocompact_pct_override": 1, "boundary_in_stream": in_stream, "boundary_in_transcript": in_transcript,
            "transcript": transcript, "passed": bool(in_stream or in_transcript)}


# ------------------------------------------------------------------ entry point

def _read_stdin(stdin: Any) -> str:
    try:
        buf = getattr(stdin, "buffer", None)
        data = buf.read() if buf is not None else stdin.read()
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    except Exception:  # noqa: BLE001
        return ""


class _QuietParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:       # a hook must not write usage text to the session's stderr
        raise SystemExit(2)


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None, stdout: Any = None) -> int:
    out = stdout or sys.stdout
    try:
        ap = _QuietParser(prog="context_monitor")
        ap.add_argument("command", choices=("statusline", "notice", "precompact", "live-compaction-probe"))
        ap.add_argument("--state-dir", default=None)
        ap.add_argument("--chain-file", default=None)
        ap.add_argument("--yes", action="store_true")
        ap.add_argument("--model", default="haiku")
        ap.add_argument("--claude", default="claude")
        ap.add_argument("--turns", type=int, default=4)
        a = ap.parse_args(list(argv) if argv is not None else None)
    except SystemExit:
        out.write(MINIMAL_LINE + "\n")       # bad invocation of a hook: stay quiet and harmless
        return 0
    try:
        if a.command == "live-compaction-probe":
            if not a.yes:
                out.write("dry run: would start %d headless claude turns (model %s) in a temp folder with %s=1; "
                          "pass --yes\n" % (a.turns, a.model, AUTOCOMPACT_ENV))
                return 0
            try:
                res = live_compaction_probe(a.claude, a.model, a.turns)
            except RuntimeError as exc:
                out.write(str(exc) + "\n")
                return 2
            out.write(json.dumps(res, indent=2) + "\n")
            return 0 if res["passed"] else 1
        text = _read_stdin(stdin or sys.stdin)
        if a.command == "statusline":
            out.write((run_statusline(text, a.state_dir, a.chain_file) if a.state_dir else MINIMAL_LINE) + "\n")
        elif a.command == "notice" and a.state_dir:
            res = run_notice(text, a.state_dir)
            if res:
                out.write(res + "\n")
        elif a.command == "precompact" and a.state_dir:
            run_precompact(text, a.state_dir)
    except Exception:  # noqa: BLE001 - hooks never crash
        if a.command == "statusline":
            try:
                out.write(MINIMAL_LINE + "\n")
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
