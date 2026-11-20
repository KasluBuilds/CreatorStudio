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
import copy
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
MATCHER = ".*"          # every tool: authority.py classifies the closed tool table itself (T-DEV-028)
MODES = {"main": "advisory", "worker": "authoritative", "reviewer": "authoritative"}
SETTING_SOURCES = "project,local"   # the user's settings are merged into the derived file instead (not loaded twice)
COPIED_USER_KEYS = ("hooks", "autoMode", "modelSettings", "model", "statusLine", "permissions")
DROPPED_PERMISSION_KEYS = ("defaultMode", "additionalDirectories", "allow")   # never widened from the user file
PROJECT_SETTINGS = ("settings.json", "settings.local.json")   # loaded by --setting-sources project,local
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
    project_dir: str = ""       # launch cwd whose .claude/settings*.json must carry no hooks (checked again at launch)


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


def default_user_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


_FIRST_WORD = re.compile(r"""^\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")


def _is_rtk(hook: Any) -> bool:
    """A command hook whose executable is rtk (``rtk``, ``rtk.exe``, any path, quoted or not, any case). The RTK
    rewrite hook is ``rtk hook ...``; every other rtk invocation is dropped as well (a superset, never fewer)."""
    if not isinstance(hook, dict):
        return False
    m = _FIRST_WORD.match(str(hook.get("command", "")))
    if not m:
        return False
    exe = next(g for g in m.groups() if g is not None).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return exe in ("rtk", "rtk.exe")


def _strip_rtk(hooks: Any) -> Dict[str, Any]:
    """The user's hooks without any ``rtk ...`` command hook (the RTK rewrite hook defeats auto mode's review)."""
    out: Dict[str, Any] = {}
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            inner = [h for h in (g.get("hooks") or []) if isinstance(h, dict) and not _is_rtk(h)]
            if inner:
                kept.append({**g, "hooks": inner})
        if kept:
            out[event] = kept
    return out


def load_user_settings(path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """Only COPIED_USER_KEYS of the user's settings.json ({} when missing or unreadable); rtk hooks removed.
    env, apiKeyHelper, awsAuthRefresh, otelHeadersHelper and every other key are never copied."""
    p = Path(path) if path is not None else default_user_settings()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {k: raw[k] for k in COPIED_USER_KEYS if k in raw}
    if "hooks" in out:
        out["hooks"] = _strip_rtk(out["hooks"])
    if "permissions" in out:
        perms = out.pop("permissions")
        perms = {k: v for k, v in perms.items() if k not in DROPPED_PERMISSION_KEYS} if isinstance(perms, dict) else {}
        if perms:
            out["permissions"] = perms
    return out


def check_project_settings(project_dir: str) -> None:
    """Refuse (PolicyInstallError) when the launch cwd's ``.claude/settings.json`` or ``settings.local.json`` holds
    ``disableAllHooks`` or any hooks, or cannot be read: ``--setting-sources project,local`` would load them."""
    if not project_dir:
        return
    for name in PROJECT_SETTINGS:
        p = Path(project_dir) / ".claude" / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise PolicyInstallError(f"project settings {p} cannot be read; refusing to launch") from None
        if not isinstance(data, dict):
            raise PolicyInstallError(f"project settings {p} is not a JSON object; refusing to launch")
        if "disableAllHooks" in data or data.get("hooks"):
            raise PolicyInstallError(f"project settings {p} sets disableAllHooks or hooks; refusing to launch")


def build_settings(command: str, user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Derived per-session settings: the copied user keys plus the authority PreToolUse hook, placed first."""
    base = copy.deepcopy(user or {})
    hooks = base.get("hooks") if isinstance(base.get("hooks"), dict) else {}
    pre = [g for g in (hooks.get("PreToolUse") or []) if isinstance(g, dict)]
    hooks["PreToolUse"] = [{"matcher": MATCHER, "hooks": [{"type": "command", "command": command}]}] + pre
    base["hooks"] = hooks
    return base


def approvals_dir(state_dir: os.PathLike) -> Path:
    return Path(state_dir) / "approvals"


def policy_env(repo_root: str, roots: Sequence[str] = (), owned_paths: Optional[Sequence[str]] = None,
               grants: Sequence[str] = (), role: str = "main",
               approvals: Optional[os.PathLike] = None, boundary: Sequence[str] = ()) -> Dict[str, str]:
    if role not in ROLES:
        raise PolicyInstallError(f"unknown role {role!r}")
    env = {"CS_AUTHORITY_REPO_ROOT": _posix(repo_root),
           "CS_AUTHORITY_ROOTS": os.pathsep.join(_posix(r) for r in (list(roots) or [repo_root])),
           "CS_AUTHORITY_MODE": MODES[role], "CS_AUTHORITY_ROLE": role}
    if approvals is not None:
        env["CS_AUTHORITY_APPROVALS_DIR"] = _posix(str(approvals))
    if owned_paths is not None:
        env["CS_AUTHORITY_OWNED_PATHS"] = json.dumps(list(owned_paths))
    if grants:
        env["CS_AUTHORITY_GRANTS"] = ",".join(grants)
    if boundary:
        env[authority.BOUNDARY_ENV] = os.pathsep.join(_posix(str(b)) for b in boundary)
    return env


def boundary_paths(script: Optional[str] = None, user_settings: Optional[os.PathLike] = None) -> List[str]:
    """Files no session may write or name in a shell command: the hook script of the installed command, the live
    authority.py and session_policy.py, and the user settings file the derived settings are built from."""
    out = [_posix(os.path.abspath(script or authority.__file__)), _posix(os.path.abspath(authority.__file__)),
           _posix(os.path.abspath(__file__)),
           _posix(str(Path(user_settings) if user_settings is not None else default_user_settings()))]
    return list(dict.fromkeys(out))


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
    full = {k: v for k, v in os.environ.items() if not k.upper().startswith("CS_AUTHORITY_")}
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
    """Execute the exact hook command: it must deny a force push, deny malformed input, and pass git status (silence in
    advisory mode, an explicit ``allow`` in authoritative mode)."""
    authoritative = env.get("CS_AUTHORITY_MODE", "advisory") != "advisory"
    def payload(cmd: str) -> str:
        return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": repo_root})
    try:
        rc, out = _run_hook(command, env, payload(DENY_PROBE), run)
        if rc != 0 or _decision(out) != "deny":
            raise PolicyInstallError(f"hook did not deny the force-push probe with valid JSON (exit {rc}, output {out[:120]!r})")
        rc, out = _run_hook(command, env, payload(ALLOW_PROBE), run)
        if rc != 0 or (_decision(out) != "allow" if authoritative else bool(out.strip())):
            raise PolicyInstallError(f"hook did not allow the routine probe (exit {rc}, output {out[:120]!r})")
        rc, out = _run_hook(command, env, "not json", run)
        if rc != 0 or _decision(out) != "deny":
            raise PolicyInstallError("hook did not fail closed on malformed input")
    except (OSError, subprocess.SubprocessError) as exc:
        raise PolicyInstallError(f"hook command could not be executed: {exc}") from exc


_VERIFIED: Dict[Tuple[str, str], Dict[str, Any]] = {}   # (settings path, hook command) -> content install() verified


def install(state_dir: os.PathLike, session_id: str, role: str, repo_root: str, roots: Sequence[str] = (),
            owned_paths: Optional[Sequence[str]] = None, grants: Sequence[str] = (), python: Optional[str] = None,
            run: Callable[..., Any] = subprocess.run, verify: bool = True,
            user_settings: Optional[os.PathLike] = None) -> PolicyInstall:
    path = settings_path(state_dir, role, session_id)
    check_project_settings(repo_root)
    cmd = hook_command(python)
    env = policy_env(repo_root, roots, owned_paths, grants, role=role, approvals=approvals_dir(state_dir),
                     boundary=boundary_paths(None, user_settings))
    wanted = build_settings(cmd, load_user_settings(user_settings))
    try:
        _atomic_write(path, json.dumps(wanted, indent=2) + "\n")
        back = json.loads(path.read_text(encoding="utf-8"))  # read the file back: it must say what we meant
    except (OSError, ValueError) as exc:
        raise PolicyInstallError(f"settings file could not be written or read back: {exc.__class__.__name__}") from None
    if back != wanted:
        raise PolicyInstallError("settings file read-back differs from what was written")
    if verify:
        verify_hook(cmd, env, repo_root, run)
        _VERIFIED[(str(path), cmd)] = wanted
    return PolicyInstall(str(path), cmd, env, str(uuid.UUID(str(session_id))), role, verified=bool(verify),
                         project_dir=str(repo_root))


def argv_with_policy(argv: Sequence[str], inst: PolicyInstall) -> List[str]:
    """``argv`` with ``--settings <file> --setting-sources project,local`` inserted after the executable (the user's
    settings are merged into the derived file, so they are not loaded twice); refuses an unverified installation."""
    if not inst.verified or (inst.settings_path, inst.hook_command) not in _VERIFIED:
        raise PolicyInstallError("policy installation was not verified by install() in this process; refusing to launch")
    if not argv:
        raise PolicyInstallError("empty argv")
    if any(a == "--settings" or a.startswith("--settings=") for a in argv):
        raise PolicyInstallError("argv already carries --settings; merge it explicitly instead")
    if any(a == "--setting-sources" or a.startswith("--setting-sources=") for a in argv):
        raise PolicyInstallError("argv already carries --setting-sources; refusing to guess the merge")
    check_project_settings(inst.project_dir)          # re-checked at launch: the files may have changed since install
    try:
        on_disk = json.loads(Path(inst.settings_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PolicyInstallError("settings file is missing or unreadable") from None
    if on_disk != _VERIFIED[(inst.settings_path, inst.hook_command)]:
        raise PolicyInstallError("settings file content changed since verification")
    return [argv[0], "--settings", inst.settings_path, "--setting-sources", SETTING_SOURCES, *argv[1:]]


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
