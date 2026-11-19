"""Automatic, verified installation of the authority hook into launched sessions (T-DEV-025, issue #44).

``authority.py`` decides; this module makes sure a launched or resumed Claude Code session actually runs that hook:

* ``install(...)``   writes a per-session settings file (deterministic path, so a resume reuses it), then VERIFIES it by
                     executing the exact hook command against probe payloads. Any failure raises ``PolicyInstallError``.
* ``argv_with_policy(argv, inst)``  adds ``--settings <file>`` and refuses when verification has not passed, so an
                     unverified policy can never be launched silently.
* ``live_probe(...)`` / ``python -B -m dev_supervisor.session_policy live-probe --yes``: runs a REAL headless ``claude``
                     session in a throwaway git repo (no remote) with the settings file and reads the transcript to see
                     the hook allow ``git status`` and block ``git push --force``. Opt-in; uses the configured model.

Wiring into ``launch.py`` is a separate, owner-coordinated change; this module only returns the argv/env additions.
Standard library only. Unit tests never start ``claude``.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from . import authority
except ImportError:  # pragma: no cover
    import authority  # type: ignore

ROLES = ("main", "worker", "reviewer")
MATCHER = "Bash|PowerShell|Write|Edit|NotebookEdit|MultiEdit|Read|mcp__davinci-resolve__.*"
DENY_PROBE = "git push --force origin task/policy-probe"
ALLOW_PROBE = "git status --short"


class PolicyInstallError(Exception):
    pass


@dataclass(frozen=True)
class PolicyInstall:
    settings_path: str
    hook_command: str
    env: Dict[str, str]
    session_id: str
    role: str
    verified: bool = False


def _posix(p: str) -> str:
    return str(p).replace("\\", "/")


_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./:+~@ ()\-]+$")


def _quote(s: str) -> str:
    """Always double-quote; refuse any character that a shell could interpret (so cmd and bash agree)."""
    if not _SAFE_PATH.match(s):
        raise PolicyInstallError(f"unsafe characters in hook path {s!r}; use a plain install path")
    return '"' + s + '"'


def hook_command(python: Optional[str] = None, script: Optional[str] = None) -> str:
    py = _posix(python or sys.executable)
    sc = _posix(script or authority.__file__)
    return f"{_quote(py)} -I -B {_quote(sc)} hook"


def settings_path(state_dir: os.PathLike, role: str, session_id: str) -> Path:
    if role not in ROLES:
        raise PolicyInstallError(f"unknown role {role!r}")
    try:
        sid = str(uuid.UUID(str(session_id)))
    except ValueError:
        raise PolicyInstallError("session id must be a UUID") from None
    return Path(state_dir) / "policy" / f"{role}-{sid}.settings.json"


def build_settings(command: str) -> Dict[str, Any]:
    return {"hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [{"type": "command", "command": command}]}]}}


def policy_env(repo_root: str, roots: Sequence[str] = (), owned_paths: Optional[Sequence[str]] = None,
               grants: Sequence[str] = ()) -> Dict[str, str]:
    env = {"CS_AUTHORITY_REPO_ROOT": _posix(repo_root),
           "CS_AUTHORITY_ROOTS": os.pathsep.join(_posix(r) for r in (list(roots) or [repo_root]))}
    if owned_paths is not None:
        env["CS_AUTHORITY_OWNED_PATHS"] = json.dumps(list(owned_paths))
    if grants:
        env["CS_AUTHORITY_GRANTS"] = ",".join(grants)
    return env


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _run_hook(command: str, env: Dict[str, str], stdin: str, run: Callable[..., Any]) -> Tuple[int, str]:
    full = dict(os.environ)
    full.update(env)
    r = run(command, input=stdin, capture_output=True, text=True, shell=True, env=full, timeout=30)
    return r.returncode, r.stdout or ""


def _decision(out: str) -> Optional[str]:
    """permissionDecision of a hook's stdout when it is exactly one valid PreToolUse JSON object, else None."""
    try:
        d = json.loads(out)
        h = d["hookSpecificOutput"]
        if h.get("hookEventName") == "PreToolUse" and isinstance(h.get("permissionDecision"), str):
            return h["permissionDecision"]
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def verify_hook(command: str, env: Dict[str, str], repo_root: str, run: Callable[..., Any] = subprocess.run) -> None:
    """Execute the exact hook command: it must deny a force push, deny malformed input, and stay silent on git status."""
    def payload(cmd: str) -> str:
        return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": repo_root})
    try:
        rc, out = _run_hook(command, env, payload(DENY_PROBE), run)
        if rc != 0 or _decision(out) != "deny":
            raise PolicyInstallError(f"hook did not deny the force-push probe with valid JSON (exit {rc}, output {out[:120]!r})")
        rc, out = _run_hook(command, env, payload(ALLOW_PROBE), run)
        if rc != 0 or out.strip():
            raise PolicyInstallError(f"hook did not allow the routine probe (exit {rc}, output {out[:120]!r})")
        rc, out = _run_hook(command, env, "not json", run)
        if rc != 0 or _decision(out) != "deny":
            raise PolicyInstallError("hook did not fail closed on malformed input")
    except (OSError, subprocess.SubprocessError) as exc:
        raise PolicyInstallError(f"hook command could not be executed: {exc}") from exc


_VERIFIED: set = set()      # (settings path, hook command) pairs that install() verified in this process


def install(state_dir: os.PathLike, session_id: str, role: str, repo_root: str, roots: Sequence[str] = (),
            owned_paths: Optional[Sequence[str]] = None, grants: Sequence[str] = (), python: Optional[str] = None,
            run: Callable[..., Any] = subprocess.run, verify: bool = True) -> PolicyInstall:
    path = settings_path(state_dir, role, session_id)
    cmd = hook_command(python)
    env = policy_env(repo_root, roots, owned_paths, grants)
    _atomic_write(path, json.dumps(build_settings(cmd), indent=2) + "\n")
    back = json.loads(path.read_text(encoding="utf-8"))      # read the file back: it must say what we meant
    if back != build_settings(cmd):
        raise PolicyInstallError("settings file read-back differs from what was written")
    if verify:
        verify_hook(cmd, env, repo_root, run)
    if verify:
        _VERIFIED.add((str(path), cmd))
    return PolicyInstall(str(path), cmd, env, str(uuid.UUID(str(session_id))), role, verified=bool(verify))


def argv_with_policy(argv: Sequence[str], inst: PolicyInstall) -> List[str]:
    """``argv`` with ``--settings <file>`` inserted after the executable; refuses an unverified installation."""
    if not inst.verified or (inst.settings_path, inst.hook_command) not in _VERIFIED:
        raise PolicyInstallError("policy installation was not verified by install() in this process; refusing to launch")
    if not argv:
        raise PolicyInstallError("empty argv")
    if any(a == "--settings" or a.startswith("--settings=") for a in argv):
        raise PolicyInstallError("argv already carries --settings; merge it explicitly instead")
    try:
        on_disk = json.loads(Path(inst.settings_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PolicyInstallError("settings file is missing or unreadable") from None
    if on_disk != build_settings(inst.hook_command):
        raise PolicyInstallError("settings file content changed since verification")
    return [argv[0], "--settings", inst.settings_path, *argv[1:]]


def check_resume(inst: PolicyInstall, argv: Sequence[str]) -> bool:
    """True when a launch argv really carries this installation's settings file (use after building a resume argv)."""
    a = list(argv)
    return "--settings" in a and a.index("--settings") + 1 < len(a) and a[a.index("--settings") + 1] == inst.settings_path


# ------------------------------------------------------------------ live probe (real claude, opt-in)

_FORCE = re.compile(r"\bpush\b.*(--force|--force-with-lease|\s-f\b)")


def parse_probe_transcript(lines: Sequence[str]) -> Dict[str, Any]:
    """From stream-json lines: did git status run, and was git push --force blocked by the hook?"""
    uses: Dict[str, str] = {}
    results: Dict[str, Tuple[str, bool]] = {}
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        content = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uses[b.get("id", "")] = str((b.get("input") or {}).get("command", ""))
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id", "")] = (json.dumps(b.get("content")), bool(b.get("is_error")))
    status_ok = any("git status" in c and not results.get(i, ("", True))[1] for i, c in uses.items())
    blocked = any(_FORCE.search(c) and results.get(i, ("", False))[1] and "authority policy" in results[i][0]
                  for i, c in uses.items())
    forced_ran = any(_FORCE.search(c) and i in results and not results[i][1] for i, c in uses.items())
    return {"status_ran": status_ok, "force_push_blocked": blocked, "force_push_ran": forced_ran,
            "tool_uses": len(uses)}


def live_probe(claude: str = "claude", model: str = "haiku", python: Optional[str] = None,
               run: Callable[..., Any] = subprocess.run, workdir: Optional[str] = None) -> Dict[str, Any]:
    base = Path(workdir or tempfile.mkdtemp(prefix="cs-policy-probe-"))
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for c in (["git", "init", "-q"], ["git", "config", "user.email", "probe@example.invalid"],
              ["git", "config", "user.name", "probe"]):
        run(c, cwd=str(repo), capture_output=True, text=True, timeout=30)
    inst = install(base / "state", str(uuid.uuid4()), "worker", str(repo), owned_paths=[""], python=python, run=run)
    env = dict(os.environ)
    env.update(inst.env)
    argv = argv_with_policy([claude, "-p", "--model", model, "--allowedTools", "Bash", "--output-format", "stream-json",
                             "--verbose", "Use the Bash tool to run these two commands one at a time and report what each "
                             "returned: (1) git status --short  (2) git push --force origin task/x"], inst)
    r = run(argv, cwd=str(repo), capture_output=True, text=True, env=env, timeout=300, stdin=subprocess.DEVNULL)
    res = parse_probe_transcript((r.stdout or "").splitlines())
    res.update(exit_code=r.returncode, passed=bool(res["status_ran"] and res["force_push_blocked"] and not res["force_push_ran"]),
               settings_path=inst.settings_path, model=model)
    return res


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dev_supervisor.session_policy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("live-probe", help="run a real headless claude session against the installed hook (needs --yes)")
    lp.add_argument("--yes", action="store_true")
    lp.add_argument("--model", default="haiku")
    lp.add_argument("--claude", default="claude")
    a = ap.parse_args(argv)
    if not a.yes:
        print("dry run: would start one headless claude session in a throwaway repo; pass --yes")
        return 0
    res = live_probe(a.claude, a.model)
    print(json.dumps(res, indent=2))
    return 0 if res["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
