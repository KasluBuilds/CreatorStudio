"""Machine-enforced authority policy for supervised development sessions (T-DEV-024, issue #44).

Lukas's delegation (mission of 2026-11-19) lets the engineering agent act without asking for routine work and keeps a
short list of actions protected. This module turns that boundary into code:

* ``decide(action, ctx)``: a pure, DEFAULT-DENY policy engine. Unknown action kinds are denied.
* ``classify_tool_call(tool, tool_input)``: maps a Claude Code Bash/PowerShell/Write/Edit/MCP call to ``Action`` records.
* ``python -B -m dev_supervisor.authority hook``: a Claude Code ``PreToolUse`` hook. It reads the hook JSON from stdin
  and prints a ``permissionDecision`` of ``deny`` (protected) or ``ask`` (needs explicit authorization); it prints
  nothing for an allowed call. Malformed input for a guarded tool fails CLOSED.
* ``python -B -m dev_supervisor.authority check --kind K [--target T]``: evaluate one action from the command line.

Honest limits (see AUTHORITY.md): shell text is open-world, so the hook is a deny-list over recognised patterns, not a
sandbox. It stops the mistakes and shortcuts we know about; it does not stop a determined adversary. The hook is
NOT installed into any settings file by this module. Standard library only; no network; nothing is executed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, TextIO, Tuple

ALLOW, DENY, ASK = "ALLOW", "DENY", "REQUIRE_AUTHORIZATION"

# ------------------------------------------------------------------ action kinds
# autonomous (no human approval)
READ, RUN_TESTS, EDIT_FILE, CREATE_BRANCH, CREATE_WORKTREE = "read", "run_tests", "edit_file", "create_branch", "create_worktree"
DISPATCH_WORKER, RECOVER_WORKER, RESTART_PROCESS, RESUME_MAIN = "dispatch_worker", "recover_worker", "restart_process", "resume_main"
GIT_COMMIT, GIT_PUSH, CREATE_PR, INTEGRATE_PR, UPDATE_RECORD = "git_commit", "git_push", "create_pr", "integrate_pr", "update_record"
# protected
PAID_API, PURCHASE, SECRET_DISCLOSURE = "paid_api", "purchase", "secret_disclosure"
ORIGINAL_MUTATION, RESOLVE_MUTATION, IRRECOVERABLE_DELETE = "original_mutation", "resolve_mutation", "irrecoverable_delete"
SECURITY_BYPASS, FORCE_PUSH, HISTORY_REWRITE = "security_bypass", "force_push", "history_rewrite"
OUTSIDE_ENVIRONMENT, TERMINATE_PROCESS, STAGE_ALL = "outside_environment", "terminate_process", "stage_all"

AUTONOMOUS_KINDS = (READ, RUN_TESTS, EDIT_FILE, CREATE_BRANCH, CREATE_WORKTREE, DISPATCH_WORKER, RECOVER_WORKER,
                    RESTART_PROCESS, RESUME_MAIN, GIT_COMMIT, GIT_PUSH, CREATE_PR, INTEGRATE_PR, UPDATE_RECORD,
                    TERMINATE_PROCESS)  # terminate is autonomous only WITH full identity proof (checked below)
PROTECTED_KINDS = (PAID_API, PURCHASE, SECRET_DISCLOSURE, ORIGINAL_MUTATION, RESOLVE_MUTATION, IRRECOVERABLE_DELETE,
                   SECURITY_BYPASS, FORCE_PUSH, HISTORY_REWRITE, OUTSIDE_ENVIRONMENT, STAGE_ALL)

# repo-relative prefixes. Anything here is never edited silently.
ORIGINALS_PREFIXES = ("Incoming/",)
LAUNCHER_FILES = ("pro.ps1", "backup.ps1", ".mcp.json")
LAUNCHER_PREFIXES = ("tools/",)
GRANTS_FILE = "authority_grants.json"           # only a human edits this; the policy refuses writes to it
SECRET_NAME = re.compile(r"(^|/)(\.env(\..*)?|\.netrc|id_(rsa|ed25519|ecdsa)|[^/]*\.(pem|key|pfx|p12)|"
                         r"\.credentials?\.json|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$", re.I)


@dataclass(frozen=True)
class Decision:
    verdict: str
    rule: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW


@dataclass(frozen=True)
class Action:
    kind: str
    target: str = ""
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyContext:
    roots: Tuple[str, ...] = ()                 # directories that make up the authorised project environment
    owned_paths: Optional[Tuple[str, ...]] = None   # repo-relative prefixes the task may edit (None = not enforced here)
    allow_paid_api: bool = False                # mirrors Config.allow_paid_api; must stay False
    grants: FrozenSet[str] = frozenset()        # protected kinds a human explicitly authorised (never autonomous kinds)
    repo_root: str = ""


class AuthorityDenied(Exception):
    def __init__(self, decision: Decision, action: Action) -> None:
        super().__init__(f"{decision.verdict} [{decision.rule}] {action.kind}: {decision.reason}")
        self.decision, self.action = decision, action


# ------------------------------------------------------------------ path helpers

def _norm(p: str) -> str:
    return os.path.normpath(p.replace("\\", "/")).replace("\\", "/")


def _is_abs(p: str) -> bool:
    return bool(re.match(r"^([A-Za-z]:[\\/]|/|\\\\)", p))


def _strip_dot(p: str) -> str:
    n = _norm(p)
    while n.startswith("./"):
        n = n[2:]
    return "" if n == "." else n


def _inside(path: str, root: str) -> bool:
    a, b = _norm(path).rstrip("/").lower(), _norm(root).rstrip("/").lower()
    return a == b or a.startswith(b + "/")


def relative_to_repo(path: str, ctx: PolicyContext) -> Optional[str]:
    """Repo-relative POSIX path, or None when ``path`` is outside the repo root (or no root is configured)."""
    p = path.replace("\\", "/")
    if not _is_abs(p):
        rel = _norm(p)
        return None if rel.startswith("..") else _strip_dot(rel)
    if ctx.repo_root and _inside(p, ctx.repo_root):
        rel = _norm(p)[len(_norm(ctx.repo_root).rstrip("/")):].lstrip("/")
        return rel
    return None


def _in_environment(path: str, ctx: PolicyContext) -> bool:
    if not _is_abs(path.replace("\\", "/")):
        return not _norm(path).startswith("..")
    roots = list(ctx.roots) + ([ctx.repo_root] if ctx.repo_root else []) + [tempfile.gettempdir()]
    return any(_inside(path, r) for r in roots if r)


def _starts(rel: str, prefixes: Iterable[str]) -> bool:
    low = rel.lower()
    return any(low == x.rstrip("/").lower() or low.startswith(x.lower()) for x in prefixes)


# ------------------------------------------------------------------ the policy

def _deny(rule: str, why: str) -> Decision:
    return Decision(DENY, rule, why)


def _path_gate(action: Action, ctx: PolicyContext, *, mutating: bool) -> Optional[Decision]:
    """Rules shared by every action that names a path. Returns a decision to stop, or None to continue."""
    t = action.target
    if not t:
        return None
    rel = relative_to_repo(t, ctx)
    leaf = _norm(t).rsplit("/", 1)[-1].lower()
    if SECRET_NAME.search(_norm(t)) and mutating:
        return _deny("secret-file-mutation", "secret/credential files are never written or committed")
    if leaf == GRANTS_FILE:
        return _deny("grants-file", "the authorisation grants file is edited only by a human")
    if rel is not None and _starts(rel, ORIGINALS_PREFIXES) and mutating:
        return _deny("originals-immutable", "Incoming/ and registered originals are never modified, moved or deleted")
    if _is_abs(t.replace("\\", "/")) and not _in_environment(t, ctx):
        return _deny("outside-environment", "path is outside the authorised project environment")
    if rel is None and not _is_abs(t.replace("\\", "/")):
        return _deny("outside-environment", "relative path escapes the project environment")
    return None


def _launcher(rel: Optional[str]) -> bool:
    return rel is not None and (rel.lower() in LAUNCHER_FILES or _starts(rel, LAUNCHER_PREFIXES))


def decide(action: Action, ctx: PolicyContext = PolicyContext()) -> Decision:
    k, a = action.kind, action.attrs
    if k not in AUTONOMOUS_KINDS and k not in PROTECTED_KINDS:
        return _deny("default-deny", f"unknown action kind {k!r}")

    # ---- unconditional protections: no grant can lift these
    if k == PURCHASE:
        return _deny("no-spend", "purchases, subscriptions and financial commitments are never made")
    if k == SECRET_DISCLOSURE:
        return _deny("no-secret-disclosure", "keys, tokens and credentials are never printed, copied or stored")
    if k == ORIGINAL_MUTATION:
        return _deny("originals-immutable", "original footage is never modified, moved or deleted")
    if k in (SECURITY_BYPASS, FORCE_PUSH, HISTORY_REWRITE):
        return _deny(k.replace("_", "-"), "repository security controls and shared history are not bypassed or rewritten")
    if k == STAGE_ALL:
        return _deny("stage-explicit-files", "stage explicit paths; `git add .`/`-A` is forbidden by project rules")
    if k == OUTSIDE_ENVIRONMENT:
        return _deny("outside-environment", "actions outside the authorised project environment are not delegated")

    # ---- protected kinds that a human may explicitly authorise
    if k == PAID_API:
        if not ctx.allow_paid_api:
            return _deny("paid-api-off", "allow_paid_api is false; paid API usage is not authorised")
        return Decision(ALLOW, "paid-api-granted", "allow_paid_api is true and an explicit grant exists") \
            if PAID_API in ctx.grants else Decision(ASK, "paid-api-needs-grant", "paid API needs an explicit authorisation")
    if k == RESOLVE_MUTATION:
        if a.get("production", True) is False and a.get("fixture") is True:
            return Decision(ALLOW, "resolve-isolated-fixture", "mutation of a synthetic fixture project")
        return Decision(ALLOW, "resolve-granted", "explicit production authorisation") if RESOLVE_MUTATION in ctx.grants \
            else Decision(ASK, "resolve-production", "Resolve mutation needs a validated plan and explicit authorisation")
    if k == IRRECOVERABLE_DELETE:
        if a.get("recoverable") is True and not a.get("original"):
            g = _path_gate(action, ctx, mutating=True)
            return g or Decision(ALLOW, "recoverable-delete", "deletion has a recovery path")
        return Decision(ASK if IRRECOVERABLE_DELETE not in ctx.grants else ALLOW, "irrecoverable-delete",
                        "deleting data without a recovery path needs explicit authorisation")

    # ---- autonomous kinds, each with the guard that keeps it routine
    if k in (READ, RUN_TESTS, CREATE_BRANCH, CREATE_WORKTREE, DISPATCH_WORKER, RECOVER_WORKER, RESUME_MAIN,
             CREATE_PR, UPDATE_RECORD, GIT_COMMIT):
        if k == READ and SECRET_NAME.search(_norm(action.target or "")):
            return _deny("no-secret-disclosure", "secret/credential files are not read into a session")
        mutating = k in (UPDATE_RECORD, GIT_COMMIT)
        g = _path_gate(action, ctx, mutating=mutating)
        if g:
            return g
        if k == RESUME_MAIN and not a.get("owned"):
            return _deny("resume-only-owned", "only a Main recorded as owned by this supervisor may be resumed")
        return Decision(ALLOW, "autonomous", f"{k} is a routine delegated action")
    if k == EDIT_FILE:
        g = _path_gate(action, ctx, mutating=True)
        if g:
            return g
        rel = relative_to_repo(action.target, ctx)
        if _launcher(rel) and EDIT_FILE not in ctx.grants:
            return Decision(ASK, "launcher-files", "launchers, .mcp.json and tools/ need a stated reason before editing")
        if ctx.owned_paths is not None and rel is not None and not _starts(rel, ctx.owned_paths):
            return _deny("not-owned", f"{rel} is outside this task's owned paths")
        return Decision(ALLOW, "autonomous", "edit inside owned paths")
    if k == GIT_PUSH:
        if a.get("force"):
            return _deny("force-push", "force pushes are never allowed")
        if str(a.get("branch", "")).lower() in ("main", "master", "refs/heads/main", "refs/heads/master"):
            return _deny("push-main", "never push directly to main; integrate through a pull request")
        return Decision(ALLOW, "autonomous", "push of a task/lane branch")
    if k == INTEGRATE_PR:
        if a.get("gates_passed") is True and a.get("independent_review") is True and a.get("admin_bypass") is not True:
            return Decision(ALLOW, "gated-merge", "all integration gates passed with an independent review")
        return _deny("merge-needs-gates", "merge requires every gate PASS, an independent review and no bypass")
    if k == RESTART_PROCESS:
        return _identity(action, ctx, verb="restart")
    if k == TERMINATE_PROCESS:
        return _identity(action, ctx, verb="terminate")
    return _deny("default-deny", f"no rule for {k!r}")  # unreachable guard


IDENTITY_FIELDS = ("pid", "exe", "cmdline", "parent_pid", "session_id")


def _identity(action: Action, ctx: PolicyContext, *, verb: str) -> Decision:
    """A process may be controlled only with full identity proof. A PID alone is never enough."""
    a = action.attrs
    missing = [f for f in IDENTITY_FIELDS if a.get(f) in (None, "")]
    if missing:
        return _deny("process-identity", f"cannot {verb}: missing identity proof ({', '.join(missing)}); a PID alone is never enough")
    if not a.get("owned_marker") or str(a["owned_marker"]) not in str(a["cmdline"]):
        return _deny("process-not-owned", "command line does not carry this environment's ownership marker")
    if a.get("parent_pid") not in (a.get("owned_parent_pids") or ()):
        return _deny("process-not-owned", "parent process is not one of the environment's own hosts/supervisors")
    if a.get("pid") in (a.get("protected_pids") or ()):
        return _deny("protected-process", "this process belongs to the real development environment")
    if a.get("session_id") in (a.get("protected_sessions") or ()):
        return _deny("protected-process", "this session belongs to the real development environment")
    return Decision(ALLOW, "owned-process", f"identity proven; {verb} is delegated")


def enforce(action: Action, ctx: PolicyContext = PolicyContext()) -> Decision:
    """Like ``decide`` but raises ``AuthorityDenied`` unless the verdict is ALLOW. Use at choke points."""
    d = decide(action, ctx)
    if not d.allowed:
        raise AuthorityDenied(d, action)
    return d


# ------------------------------------------------------------------ classifying tool calls

_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\r?\n)\s*")
_TOKEN = re.compile(r"""("[^"]*"|'[^']*'|\S+)""")
_RM = re.compile(r"^(rm|del|erase|rd|rmdir|remove-item|ri)$", re.I)
_MV = re.compile(r"^(mv|move|move-item|mi|ren|rename|rename-item|cp|copy|copy-item|set-content|out-file|tee)$", re.I)
_READERS = re.compile(r"^(cat|type|get-content|gc|less|more|head|tail|bat|cp|copy|copy-item|xxd|strings)$", re.I)
_KILLERS = re.compile(r"^(taskkill|stop-process|kill|pkill|killall|spps)$", re.I)
_PAID_HOSTS = re.compile(r"(api\.anthropic\.com|api\.openai\.com|generativelanguage\.googleapis\.com|api\.cohere\.|api\.mistral\.ai)", re.I)
_SECRET_ENV = re.compile(r"(\$env:|\$\{?env:|%|\$)\w*(API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\w*", re.I)
_RESOLVE_READ = re.compile(r"^(get|list|is|read|find|search|describe|inspect|status|info|has|count|export_info)", re.I)


def _tokens(segment: str) -> List[str]:
    return [t.strip("\"'") for t in _TOKEN.findall(segment)]


def _paths(tokens: Sequence[str]) -> List[str]:
    return [t for t in tokens[1:] if t and not t.startswith("-") and not re.match(r"^/[A-Za-z?]$", t)]


def classify_command(command: str) -> List[Action]:
    """Actions implied by a shell command line (open-world deny-list; unknown commands yield no actions)."""
    out: List[Action] = []
    for seg in filter(None, _SPLIT.split(command or "")):
        tok = _tokens(seg)
        if not tok:
            continue
        head = tok[0].lower().replace(".exe", "")
        if head == "rtk" and len(tok) > 1:           # `rtk <cmd>` and `rtk bypass <cmd>` wrap another command
            tok = tok[2:] if tok[1].lower() == "bypass" else tok[1:]
            if not tok:
                continue
            head = tok[0].lower().replace(".exe", "")
        low = [t.lower() for t in tok]
        if head == "git" and len(tok) > 1:
            sub = next((t.lower() for t in tok[1:] if not t.startswith("-")), "")
            flags = [t for t in low[1:] if t.startswith("-")]
            if sub in ("commit", "merge", "rebase", "cherry-pick", "am", "push") and                     any(f in ("--no-verify", "-n", "--no-gpg-sign") for f in flags) and not (sub == "push" and "-n" in flags and "--no-verify" not in flags):
                out.append(Action(SECURITY_BYPASS))
            elif sub == "push":
                force = any(f in ("-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d") or f.startswith("--force") for f in flags)
                refs = [t for t in tok[2:] if not t.startswith("-")]
                branch = ""
                for r in refs[1:] if refs else []:
                    branch = r.split(":")[-1].replace("refs/heads/", "")
                if "+" in "".join(refs):
                    force = True
                out.append(Action(FORCE_PUSH if force else GIT_PUSH, attrs={"branch": branch, "force": force}))
            elif sub == "add" and any(t in (".", "-A", "--all", "-a") for t in tok[2:]):
                out.append(Action(STAGE_ALL))
            elif sub in ("filter-branch", "filter-repo") or (sub == "reflog" and "expire" in low) or \
                    (sub == "update-ref" and "-d" in low):
                out.append(Action(HISTORY_REWRITE))
            elif sub == "config" and any("hookspath" in t or "gpgsign" in t for t in low):
                out.append(Action(SECURITY_BYPASS))
            elif sub == "clean" and any("f" in f.lstrip("-") for f in flags if not f.startswith("--")):
                out.append(Action(IRRECOVERABLE_DELETE, attrs={"recoverable": False}))
            continue
        if head == "gh" and "merge" in low and "--admin" in low:
            out.append(Action(SECURITY_BYPASS))
            continue
        if head == "gh" and low[1:2] == ["api"] and "-x" in low and any(x in low for x in ("delete",)) and any("protection" in t for t in low):
            out.append(Action(SECURITY_BYPASS))
            continue
        if _RM.match(head):
            for p in _paths(tok):
                rel = relative_to_repo(p, PolicyContext())
                if _starts(_strip_dot(p), ORIGINALS_PREFIXES) or "/incoming/" in _norm(p).lower() + "/" or _norm(p).lower().endswith("/incoming"):
                    out.append(Action(ORIGINAL_MUTATION, p))
                else:
                    out.append(Action(IRRECOVERABLE_DELETE, p, {"recoverable": False, "recursive": any(x in ("-r", "-rf", "-fr", "/s", "-recurse") for x in low)}))
            continue
        if _MV.match(head):
            for p in _paths(tok):
                if "/incoming/" in "/" + _strip_dot(p).lower() + "/":
                    out.append(Action(ORIGINAL_MUTATION, p))
        if _KILLERS.match(head):
            out.append(Action(TERMINATE_PROCESS, attrs={"via": "shell"}))   # no identity proof is possible from shell text
            continue
        if _READERS.match(head):
            for p in _paths(tok):
                if SECRET_NAME.search(_norm(p)) or _norm(p).lower().endswith(("/pro.ps1", "/backup.ps1")) or _norm(p).lower() in ("pro.ps1", "backup.ps1"):
                    out.append(Action(SECRET_DISCLOSURE, p))
        if head in ("printenv", "env", "set") and len(tok) == 1 or low[:2] == ["get-childitem", "env:"] or low[:2] == ["dir", "env:"]:
            out.append(Action(SECRET_DISCLOSURE, "environment"))
        if head in ("echo", "write-output", "write-host", "printf") and _SECRET_ENV.search(seg):
            out.append(Action(SECRET_DISCLOSURE, "environment variable"))
        if head in ("curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "http") and _PAID_HOSTS.search(seg):
            out.append(Action(PAID_API, attrs={"via": "direct http"}))
    return out


def classify_tool_call(tool: str, tool_input: Dict[str, Any]) -> List[Action]:
    t = (tool or "")
    if t in ("Bash", "PowerShell"):
        return classify_command(str(tool_input.get("command", "")))
    if t in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return [Action(EDIT_FILE, path)]
    if t == "Read":
        return [Action(READ, str(tool_input.get("file_path", "")))]
    if t.startswith("mcp__davinci-resolve__"):
        verb = str(tool_input.get("action") or tool_input.get("method") or "")
        return [] if _RESOLVE_READ.match(verb) else [Action(RESOLVE_MUTATION, t, {"production": True, "verb": verb})]
    return []


_ORDER = {ALLOW: 0, ASK: 1, DENY: 2}


def evaluate_tool_call(tool: str, tool_input: Dict[str, Any], ctx: PolicyContext) -> Decision:
    """Worst verdict across the actions implied by the call; ALLOW when none is recognised."""
    worst = Decision(ALLOW, "no-protected-action", "no protected pattern recognised")
    for act in classify_tool_call(tool, tool_input):
        d = decide(act, ctx)
        if _ORDER[d.verdict] > _ORDER[worst.verdict]:
            worst = d
    return worst


# ------------------------------------------------------------------ PreToolUse hook

GUARDED = ("Bash", "PowerShell", "Write", "Edit", "NotebookEdit", "MultiEdit", "Read")


def context_from_env(env: Dict[str, str], cwd: str) -> PolicyContext:
    roots = tuple(r for r in env.get("CS_AUTHORITY_ROOTS", "").split(os.pathsep) if r) or ((cwd,) if cwd else ())
    owned: Optional[Tuple[str, ...]] = None
    try:
        raw = env.get("CS_AUTHORITY_OWNED_PATHS")
        if raw:
            v = json.loads(raw)
            owned = tuple(str(x) for x in v) if isinstance(v, list) else None
    except ValueError:
        owned = ()   # unparseable ownership: nothing is owned (fail closed)
    return PolicyContext(roots=roots, owned_paths=owned, allow_paid_api=False,
                         grants=frozenset(x for x in env.get("CS_AUTHORITY_GRANTS", "").split(",") if x in PROTECTED_KINDS
                                          and x not in (PURCHASE, SECRET_DISCLOSURE, ORIGINAL_MUTATION, SECURITY_BYPASS,
                                                        FORCE_PUSH, HISTORY_REWRITE, STAGE_ALL, OUTSIDE_ENVIRONMENT)),
                         repo_root=env.get("CS_AUTHORITY_REPO_ROOT", cwd))


def hook_output(decision: Decision) -> Optional[Dict[str, Any]]:
    if decision.allowed:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny" if decision.verdict == DENY else "ask",
                                   "permissionDecisionReason": f"authority policy [{decision.rule}]: {decision.reason}"}}


def run_hook(stdin: TextIO, out: TextIO, env: Optional[Dict[str, str]] = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        payload = json.loads(stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        tool = str(payload.get("tool_name", ""))
        tin = payload.get("tool_input") or {}
        if not isinstance(tin, dict):
            raise ValueError("tool_input is not an object")
        decision = evaluate_tool_call(tool, tin, context_from_env(env, str(payload.get("cwd") or os.getcwd())))
    except (ValueError, TypeError) as exc:
        decision = Decision(DENY, "hook-malformed-input", f"could not evaluate the call ({exc}); failing closed")
    res = hook_output(decision)
    if res is not None:
        out.write(json.dumps(res) + "\n")
    return 0


def build_hook_command(python: str = "python", script: Optional[str] = None) -> str:
    q = lambda s: '"' + s.replace('"', '\\"') + '"' if " " in s else s
    return f"{q(python)} -B {q(script or os.path.abspath(__file__))} hook"


def hook_settings() -> Dict[str, Any]:
    """The ``hooks`` fragment a supervised session's settings file would hold (this module never writes it)."""
    return {"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell|Write|Edit|NotebookEdit|MultiEdit|Read|mcp__davinci-resolve__.*",
                                      "hooks": [{"type": "command", "command": build_hook_command()}]}]}}


def main(argv: Optional[Sequence[str]] = None, stdin: Optional[TextIO] = None, out: Optional[TextIO] = None) -> int:
    ap = argparse.ArgumentParser(prog="dev_supervisor.authority", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="Claude Code PreToolUse hook (JSON on stdin)")
    c = sub.add_parser("check", help="evaluate one action")
    c.add_argument("--kind", required=True)
    c.add_argument("--target", default="")
    c.add_argument("--attr", action="append", default=[], help="key=value (value parsed as JSON when possible)")
    sub.add_parser("settings", help="print the hooks settings fragment")
    a = ap.parse_args(argv)
    stdin, out = stdin or sys.stdin, out or sys.stdout
    if a.cmd == "hook":
        return run_hook(stdin, out)
    if a.cmd == "settings":
        out.write(json.dumps(hook_settings(), indent=2) + "\n")
        return 0
    attrs: Dict[str, Any] = {}
    for kv in a.attr:
        k, _, v = kv.partition("=")
        try:
            attrs[k] = json.loads(v)
        except ValueError:
            attrs[k] = v
    d = decide(Action(a.kind, a.target, attrs), context_from_env(dict(os.environ), os.getcwd()))
    out.write(f"{d.verdict} [{d.rule}] {d.reason}\n")
    return 0 if d.allowed else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
