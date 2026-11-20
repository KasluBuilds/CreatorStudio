"""Machine-enforced authority policy for supervised development sessions (T-DEV-024, issue #44).

Lukas's delegation (mission of 2026-11-19) lets the engineering agent act without asking for routine work and keeps a
short list of actions protected. This module turns that boundary into code:

* ``decide(action, ctx)``: a pure, DEFAULT-DENY policy engine. Unknown action kinds are denied.
* ``classify_tool_call(tool, tool_input, cwd)``: CLOSED-WORLD tool table (T-DEV-028). Every tool name is shell, file
  read, file write, session-internal, subagent, web read, publishing, Resolve MCP or IDE MCP; anything else (another
  MCP server, an unlisted or malformed name) is ``UNKNOWN_TOOL`` and denied.
* ``python -B -m dev_supervisor.authority hook``: a Claude Code ``PreToolUse`` hook with two modes
  (``CS_AUTHORITY_MODE``): ``advisory`` (Main, default when unset) prints nothing for ALLOW; ``authoritative``
  (workers) prints ``allow`` only for routine calls (non-shell ALLOW, or shell whose every segment is on the routine
  allowlist) and ``deny`` for everything else. REQUIRE_AUTHORIZATION is queued as a pending approval and denied; only
  a call a human approved with ``approvals approve`` gets ``ask`` (advisory mode only), so the native permission
  prompt is the final confirmation. Malformed input fails CLOSED.
* ``python -B -m dev_supervisor.authority approvals [--dir D] list|approve <id>``: list is read-only; approve refuses
  inside a hooked session (``CS_AUTHORITY_MODE`` set) and without an interactive terminal.
* ``python -B -m dev_supervisor.authority check --kind K [--target T]``: evaluate one action from the command line.

Honest limits (see AUTHORITY.md, STANDING_AUTONOMY.md): in advisory mode shell text is open-world, so the hook is a
deny-list over recognised patterns, not a sandbox. Standard library only; no network; nothing is executed.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, TextIO, Tuple

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
# T-DEV-028 (standing autonomy): closed-world tool classification
SPAWN_SUBAGENT, WEB_READ = "spawn_subagent", "web_read"                         # autonomous
PUBLISH, IDE_EXECUTE = "publish", "ide_execute"                                 # protected, a human may authorise
UNKNOWN_TOOL, AUTHORITY_CHANGE = "unknown_tool", "authority_change"             # protected, never lifted

AUTONOMOUS_KINDS = (READ, RUN_TESTS, EDIT_FILE, CREATE_BRANCH, CREATE_WORKTREE, DISPATCH_WORKER, RECOVER_WORKER,
                    RESTART_PROCESS, RESUME_MAIN, GIT_COMMIT, GIT_PUSH, CREATE_PR, INTEGRATE_PR, UPDATE_RECORD,
                    TERMINATE_PROCESS,  # terminate is autonomous only WITH full identity proof (checked below)
                    SPAWN_SUBAGENT, WEB_READ)
PROTECTED_KINDS = (PAID_API, PURCHASE, SECRET_DISCLOSURE, ORIGINAL_MUTATION, RESOLVE_MUTATION, IRRECOVERABLE_DELETE,
                   SECURITY_BYPASS, FORCE_PUSH, HISTORY_REWRITE, OUTSIDE_ENVIRONMENT, STAGE_ALL,
                   PUBLISH, IDE_EXECUTE, UNKNOWN_TOOL, AUTHORITY_CHANGE)
# protected kinds that no grant can ever lift (subset of PROTECTED_KINDS)
UNCONDITIONAL_KINDS = (PURCHASE, SECRET_DISCLOSURE, ORIGINAL_MUTATION, SECURITY_BYPASS, FORCE_PUSH, HISTORY_REWRITE,
                       STAGE_ALL, OUTSIDE_ENVIRONMENT, UNKNOWN_TOOL, AUTHORITY_CHANGE)

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
    approvals_dir: str = ""                     # <state>/approvals: pending approval records (human-only grants)


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
    if k == UNKNOWN_TOOL:
        return _deny("unknown-tool", f"tool {action.target!r} is not in the closed tool table; default-deny")
    if k == AUTHORITY_CHANGE:
        return _deny("authority-boundary", "approvals, grants, hook settings and the approve command are human-only")

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
    if k in (PUBLISH, IDE_EXECUTE):
        if k in ctx.grants:
            return Decision(ALLOW, f"{k}-granted", "explicit authorisation")
        if k == PUBLISH:
            return Decision(ASK, "publish", "publishing outward-facing content needs explicit authorisation")
        return Decision(ASK, "ide-execute", "executing code in the IDE kernel needs explicit authorisation")
    if k == IRRECOVERABLE_DELETE:
        g = _path_gate(action, ctx, mutating=True)       # never lifted by a grant: originals, secrets, outside env
        if g:
            return g
        if a.get("original"):
            return _deny("originals-immutable", "original footage is never deleted")
        if a.get("recoverable") is True or IRRECOVERABLE_DELETE in ctx.grants:
            return Decision(ALLOW, "delete-ok", "deletion has a recovery path or an explicit grant")
        return Decision(ASK, "irrecoverable-delete", "deleting data without a recovery path needs explicit authorisation")

    # ---- autonomous kinds, each with the guard that keeps it routine
    if k in (SPAWN_SUBAGENT, WEB_READ):
        return Decision(ALLOW, "autonomous", f"{k} is a routine delegated action")
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
        if not action.target:
            return _deny("missing-path", "edit without a target path cannot be evaluated; failing closed")
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
_MV = re.compile(r"^(mv|move|move-item|mi|ren|rename|rename-item|cp|copy|copy-item|set-content|add-content|clear-content|sc|ac|out-file|tee)$", re.I)
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
        for m in re.finditer(r">>?\s*(\"[^\"]+\"|'[^']+'|[^\s;&|]+)", seg):
            tgt = m.group(1).strip("\"'")
            if "/incoming/" in "/" + _strip_dot(tgt).lower() + "/":
                out.append(Action(ORIGINAL_MUTATION, tgt))
        if head == "git" and len(tok) > 1:
            gflags: List[str] = []
            i = 1
            takes_arg = ("-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path")
            while i < len(tok) and tok[i].startswith("-"):      # git global options come before the subcommand
                gflags.append(low[i])
                if tok[i] in takes_arg and i + 1 < len(tok):
                    gflags.append(low[i + 1])
                    i += 1
                i += 1
            sub = low[i] if i < len(tok) else ""
            rest = tok[i + 1:]
            flags = [t.lower() for t in rest if t.startswith("-")]
            if any("hookspath" in g or "gpgsign" in g for g in gflags):
                out.append(Action(SECURITY_BYPASS))
            if sub == "commit" and any(f in ("--no-verify", "-n", "--no-gpg-sign") for f in flags):
                out.append(Action(SECURITY_BYPASS))
            elif sub in ("merge", "rebase", "cherry-pick", "am", "push") and any(f in ("--no-verify", "--no-gpg-sign") for f in flags):
                out.append(Action(SECURITY_BYPASS))
            elif sub == "push":
                force = any(f in ("-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d") or f.startswith("--force") for f in flags)
                refs = [t for t in rest if not t.startswith("-")]
                specs = refs[1:]
                if any(t.startswith("+") for t in specs):
                    force = True
                dsts = [r.split(":")[-1].replace("refs/heads/", "").lstrip("+") for r in specs]
                if "--all" in flags or "--mirror" in flags:
                    dsts.append("main")        # may include main: not delegated
                main_hit = next((d for d in dsts if d.lower() in ("main", "master")), "")
                out.append(Action(FORCE_PUSH if force else GIT_PUSH, attrs={"branch": main_hit or (dsts[-1] if dsts else ""), "force": force}))
            elif sub == "add" and any(t in (".", "-A", "--all", "-a") for t in rest):
                out.append(Action(STAGE_ALL))
            elif sub in ("filter-branch", "filter-repo") or (sub == "reflog" and "expire" in low) or                     (sub == "update-ref" and "-d" in low):
                out.append(Action(HISTORY_REWRITE))
            elif sub == "config" and any(("hookspath" in t or "gpgsign" in t) for t in low) and "--get" not in low and "--get-all" not in low and "--list" not in low:
                out.append(Action(SECURITY_BYPASS))
            elif sub == "clean" and any(("f" in f.lstrip("-") and not f.startswith("--")) or f == "--force" for f in flags):
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
                    out.append(Action(IRRECOVERABLE_DELETE, p, {"recoverable": False}))
            continue
        if _MV.match(head):
            ps = _paths(tok)
            if head in ("cp", "copy", "copy-item"):      # a copy only writes its destination; reading originals is fine
                dest = set(ps[-1:])
                for i2, t2 in enumerate(low):
                    if t2 in ("-t", "--target-directory", "-destination", "-dest") and i2 + 1 < len(tok):
                        dest.add(tok[i2 + 1])
                    elif t2.startswith("--target-directory=") or t2.startswith("-destination:"):
                        dest.add(tok[i2].split("=", 1)[-1].split(":", 1)[-1])
                ps = [p for p in dict.fromkeys(ps + sorted(dest)) if p in dest]
            for p in ps:
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


# Closed tool table (T-DEV-028). Every tool name falls into exactly one category; anything else is UNKNOWN_TOOL.
SHELL_TOOLS = ("Bash", "PowerShell")
READ_TOOLS = {"Read": "file_path", "NotebookRead": "notebook_path", "LS": "path", "Glob": "path", "Grep": "path"}
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
INTERNAL_TOOLS = ("TodoWrite", "ToolSearch", "Monitor", "TaskStop", "TaskOutput", "ScheduleWakeup", "ListAgents",
                  "SendMessage", "Skill", "EnterPlanMode", "ExitPlanMode", "AskUserQuestion", "CronList")
SUBAGENT_TOOLS = ("Agent", "Task")
WEB_TOOLS = ("WebFetch", "WebSearch")
PUBLISH_TOOLS = ("Artifact", "ArtifactComments", "ArtifactData", "DesignSync", "PushNotification", "RemoteTrigger",
                 "CronCreate", "CronDelete")
PUBLISH_PREFIXES = ("mcp__claude-artifacts__", "mcp__claude_ai_Claude_Docs__", "SendToProject")
PUBLISH_READ_ACTIONS = {"Artifact": ("read", "list"), "ArtifactData": ("get", "list", "query")}
DOCS_PREFIX = "mcp__claude_ai_Claude_Docs__"
DOCS_READ_TOOLS = ("read", "query", "guide", "export")
RESOLVE_PREFIX = "mcp__davinci-resolve__"
IDE_PREFIX = "mcp__ide__"
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,127}$")
_MCP_NAME = re.compile(r"^mcp__[A-Za-z0-9_\-]+?__[A-Za-z0-9_\-]+$")


def _read_actions(path: str) -> List[Action]:
    if _norm(path).lower().rsplit("/", 1)[-1] in ("pro.ps1", "backup.ps1"):
        return [Action(SECRET_DISCLOSURE, path)]
    return [Action(READ, path)]


def classify_tool_call(tool: str, tool_input: Dict[str, Any], cwd: str = "") -> List[Action]:
    """Closed-world: every known tool maps to its actions ([] = a session-internal tool); anything else is UNKNOWN_TOOL."""
    t = tool if isinstance(tool, str) else ""
    if not _TOOL_NAME.match(t) or (t.startswith("mcp__") and not _MCP_NAME.match(t)):
        return [Action(UNKNOWN_TOOL, str(tool)[:80])]
    if t in SHELL_TOOLS:
        return classify_command(str(tool_input.get("command", "")))
    if t in WRITE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return [Action(EDIT_FILE, path)]
    if t in READ_TOOLS:
        path = str(tool_input.get(READ_TOOLS[t]) or "")
        if not path and t in ("Glob", "Grep", "LS"):
            path = cwd                                  # no path: the tool searches the working directory
        return _read_actions(path)
    if t in INTERNAL_TOOLS:
        return []
    if t in SUBAGENT_TOOLS:
        return [Action(SPAWN_SUBAGENT, t)]
    if t in WEB_TOOLS:
        url = str(tool_input.get("url") or "")
        return [Action(PAID_API, url, {"via": t})] if _PAID_HOSTS.search(url) else [Action(WEB_READ, url or t)]
    if t in PUBLISH_TOOLS or t.startswith(PUBLISH_PREFIXES):
        verb = str(tool_input.get("action") or "").lower()
        if verb and verb in PUBLISH_READ_ACTIONS.get(t, ()):
            return [Action(READ, "")]
        if t.startswith(DOCS_PREFIX) and t[len(DOCS_PREFIX):] in DOCS_READ_TOOLS:
            return [Action(READ, "")]
        return [Action(PUBLISH, t)]
    if t.startswith(RESOLVE_PREFIX):
        verb = str(tool_input.get("action") or tool_input.get("method") or "")
        return [] if _RESOLVE_READ.match(verb) else [Action(RESOLVE_MUTATION, t, {"production": True, "verb": verb})]
    if t.startswith(IDE_PREFIX):
        name = t[len(IDE_PREFIX):]
        if name == "getDiagnostics":
            return []
        if name == "executeCode":
            return [Action(IDE_EXECUTE, t)]
    return [Action(UNKNOWN_TOOL, t)]


# ------------------------------------------------------------------ authority boundary (approvals, grants, settings)

_APPROVE_CMD = re.compile(r"\b(authority|approvals)\b[^\n;&|]*\bapprove\b", re.I)
_HOOK_SETTINGS = re.compile(r"(^|/)\.claude/settings(\.local)?\.json$", re.I)


def _boundary_dirs(ctx: PolicyContext) -> List[str]:
    """The approvals dir and its sibling policy dir (both under the supervisor state directory)."""
    if not ctx.approvals_dir:
        return []
    a = _norm(ctx.approvals_dir).rstrip("/")
    return [a, a.rsplit("/", 1)[0] + "/policy"] if "/" in a else [a]


def touches_boundary_path(path: str, ctx: PolicyContext) -> bool:
    """True for the approvals dir, the policy settings dir, the grants file and Claude Code settings files."""
    if not path:
        return False
    n = _norm(path)
    if n.lower().rsplit("/", 1)[-1] == GRANTS_FILE or _HOOK_SETTINGS.search(n):
        return True
    p = path if _is_abs(path.replace("\\", "/")) else (ctx.repo_root.rstrip("/\\") + "/" + path if ctx.repo_root else path)
    return any(_inside(p, d) for d in _boundary_dirs(ctx))


def touches_boundary_command(command: str, ctx: PolicyContext) -> bool:
    low = re.sub(r"/+", "/", (command or "").replace("\\", "/").lower())
    if GRANTS_FILE in low or ".claude/settings" in low or "approvals/pending" in low or _APPROVE_CMD.search(command or ""):
        return True
    return any(d.lower() in low for d in _boundary_dirs(ctx))


_ORDER = {ALLOW: 0, ASK: 1, DENY: 2}


def evaluate_tool_call(tool: str, tool_input: Dict[str, Any], ctx: PolicyContext, cwd: str = "") -> Decision:
    """Worst verdict across the actions implied by the call. Unknown tools are denied (closed world)."""
    acts = classify_tool_call(tool, tool_input, cwd)
    if tool in WRITE_TOOLS and touches_boundary_path(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""), ctx):
        acts.append(Action(AUTHORITY_CHANGE, str(tool_input.get("file_path") or "")))
    if tool in SHELL_TOOLS and touches_boundary_command(str(tool_input.get("command", "")), ctx):
        acts.append(Action(AUTHORITY_CHANGE, "shell"))
    worst = Decision(ALLOW, "no-protected-action", "no protected pattern recognised")
    for act in acts:
        d = decide(act, ctx)
        if _ORDER[d.verdict] > _ORDER[worst.verdict]:
            worst = d
    return worst


# ------------------------------------------------------------------ worker routine allowlist (authoritative mode)

_SAFE_REDIRECT = re.compile(r"\s*(2>&1|[12]?>\s*/dev/null|[12]?>\s*\$null)\s*", re.I)
_GIT_ANY = ("status", "diff", "log", "show", "fetch", "pull", "rev-parse", "ls-files", "merge-base")
_GH_VERBS = {"pr": ("view", "list", "create", "comment", "checks", "diff"), "issue": ("view", "list", "create", "comment")}
_SIMPLE_HEADS = ("ls", "dir", "pwd", "cd", "cat", "type", "head", "tail", "wc", "grep", "diff", "echo", "test", "true",
                 "printf")
CREATOR_STATE_READ = ("list", "check", "resume", "log", "checkpoints", "inventory", "ingest-status", "verify-sources",
                      "incoming-list", "incoming-inspect", "shots-plan", "shots-status", "shots-check", "--help", "-h")
_SED_WRITE = re.compile(r"(^|[;{}/\d$!\s])[wWe](\s|$)")


def _short_has(tok: str, letters: str) -> bool:
    """A bundle of short flags (``-am``) that contains any of ``letters``."""
    return bool(re.match(r"^-[A-Za-z]+$", tok)) and any(c in tok[1:] for c in letters)


def _git_routine(tok: List[str]) -> bool:
    i = 1
    while i < len(tok) and tok[i].startswith("-"):
        if tok[i] == "-C" and i + 1 < len(tok):
            i += 2
        elif tok[i] == "--no-pager":
            i += 1
        else:
            return False                              # -c (config override) and every other global option
    if i >= len(tok):
        return False
    sub, rest = tok[i].lower(), tok[i + 1:]
    low = [t.lower() for t in rest]
    if any(t.startswith("--output") for t in low):
        return False
    if sub in _GIT_ANY:
        return True
    if sub == "branch":
        return not any(t in ("--delete", "--move", "--copy", "--force") or _short_has(t, "dDmMcCf") for t in rest)
    if sub in ("checkout", "switch"):
        return not any(t in ("--force", "--discard-changes", "--orphan", "--", ".", "--patch")
                       or _short_has(t, "fBCp") for t in rest)
    if sub == "add":
        paths = [t for t in rest if not t.startswith("-")]
        bad = any(t in (".", "--all", "--update", "--force", "*", ":/", "--patch") or _short_has(t, "Aufp")
                  for t in rest)
        return bool(paths) and not bad
    if sub == "commit":
        return not any(t in ("--no-verify", "--no-gpg-sign", "--all", "--amend") or _short_has(t, "an") for t in rest)
    if sub == "worktree":
        return bool(low) and low[0] in ("list", "add") and not any(t in ("-f", "--force") for t in low)
    if sub == "stash":
        return bool(low) and low[0] in ("list", "show", "push", "apply")
    if sub == "remote":
        return not low or low in (["-v"], ["--verbose"]) or (low[0] == "get-url" and len(low) <= 2)
    if sub == "config":
        return bool(low) and low[0] in ("--get", "--get-all") and len(low) <= 2
    return False


def _gh_routine(tok: List[str]) -> bool:
    low = [t.lower() for t in tok]
    if len(low) >= 3 and low[1] in _GH_VERBS:
        return low[2] in _GH_VERBS[low[1]]
    if len(low) >= 3 and low[1] == "api":
        for j, t in enumerate(tok):
            lt = t.lower()
            if lt in ("-x", "--method"):
                if j + 1 >= len(low) or low[j + 1] != "get":
                    return False
            elif lt.startswith("--method="):
                if lt.split("=", 1)[1] != "get":
                    return False
            elif t.startswith("-X") and len(t) > 2:
                if lt[2:] != "get":
                    return False
            elif lt in ("--input", "--field", "--raw-field") or lt.startswith(("--input=", "--field=", "--raw-field=")) \
                    or (t.startswith(("-f", "-F")) and not t.startswith("--")):
                return False
        return True
    return False


def _python_routine(tok: List[str]) -> bool:
    i = 1
    while i < len(tok) and tok[i] in ("-B", "-I", "-BI", "-IB"):
        i += 1
    if i + 1 >= len(tok) or tok[i] != "-m":
        return False
    mod, rest = tok[i + 1], [t.lower() for t in tok[i + 2:]]
    root = mod.split(".", 1)[0]
    if root in ("unittest", "governance"):
        return True
    if root == "dev_supervisor":
        return mod not in ("dev_supervisor.session_policy", "dev_supervisor.live_autonomy") and "--yes" not in rest
    if root == "creator_state":
        return mod == "creator_state" and bool(rest) and rest[0] in CREATOR_STATE_READ
    return False


def _segment_routine(tok: List[str], ctx: PolicyContext) -> bool:
    head = tok[0].lower().replace(".exe", "")
    if head == "rtk":
        inner = tok[2:] if len(tok) > 1 and tok[1].lower() == "bypass" else tok[1:]
        return bool(inner) and inner[0].lower() != "rtk" and _segment_routine(inner, ctx)
    if any(SECRET_NAME.search(_norm(t)) or _norm(t).lower().rsplit("/", 1)[-1] in ("pro.ps1", "backup.ps1")
           for t in tok[1:] if t and not t.startswith("-")):
        return False                                  # secrets and launchers are never read by a worker shell
    low = [t.lower() for t in tok]
    if head == "git":
        return _git_routine(tok)
    if head == "gh":
        return _gh_routine(tok)
    if head in ("python", "python3", "py"):
        return _python_routine(tok)
    if head in _SIMPLE_HEADS:
        return True
    if head == "rg":
        return not any(t == "--pre" or t.startswith("--pre=") for t in low)
    if head == "find":
        return not any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprint0", "-fprintf")
                       for t in low)
    if head == "sort":
        return not any(t.startswith(("--output", "--compress-program")) or _short_has(t, "o") for t in tok[1:])
    if head == "uniq":
        return len([t for t in tok[1:] if not t.startswith("-")]) <= 1     # a second operand is an output file
    if head == "sed":
        flags = [t for t in tok[1:] if t.startswith("-")]
        if not any(_short_has(f, "n") for f in flags):
            return False
        if any(f.startswith("--in-place") or _short_has(f, "i") for f in flags):
            return False
        return not any(_SED_WRITE.search(t) for t in tok[1:] if not t.startswith("-"))
    if head == "mkdir":
        for p in tok[1:]:
            if p.startswith("-"):
                continue
            if (_is_abs(p.replace("\\", "/")) and not _in_environment(p, ctx)) or _norm(p).startswith(".."):
                return False
        return True
    return False


def routine_shell(command: str, ctx: PolicyContext = PolicyContext()) -> Tuple[bool, str]:
    """(True, "") when the head of EVERY segment is on the worker routine allowlist; else (False, offending segment)."""
    text = command or ""
    if not text.strip():
        return False, "(empty command)"
    if any(x in text for x in ("`", "$(", "<(", ">(", "${")):
        return False, "command substitution"
    for seg in _SPLIT.split(text):
        if not seg.strip():
            continue
        bare = _SAFE_REDIRECT.sub(" ", seg)
        if re.search(r"[<>&]", bare):
            return False, seg.strip()[:120]           # file redirection or background job
        tok = _tokens(bare)
        if not tok or not _segment_routine(tok, ctx):
            return False, seg.strip()[:120]
    return True, ""


# ------------------------------------------------------------------ pending approvals (human-only grants)

APPROVALS_ENV, MODE_ENV, ROLE_ENV = "CS_AUTHORITY_APPROVALS_DIR", "CS_AUTHORITY_MODE", "CS_AUTHORITY_ROLE"
ADVISORY, AUTHORITATIVE = "advisory", "authoritative"
PENDING, APPROVED = "PENDING", "APPROVED"
_REDACT = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
                     r"(?i:(api[_-]?key|token|secret|password)[\"']?\s*[=:]\s*)\S+)")


def call_hash(tool: str, tool_input: Dict[str, Any]) -> str:
    canon = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256((str(tool) + "\n" + canon).encode("utf-8")).hexdigest()


def approval_id(h: str) -> str:
    return h[:16]


def _record_path(approvals_dir: str, aid: str) -> str:
    if not re.match(r"^[0-9a-f]{16}$", aid or ""):
        raise ValueError("approval id must be 16 hex characters")
    return os.path.join(approvals_dir, "pending", aid + ".json")


def input_summary(tool_input: Dict[str, Any], limit: int = 300) -> str:
    text = _REDACT.sub("[REDACTED]", json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str))
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_approval(approvals_dir: str, aid: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_record_path(approvals_dir, aid), encoding="utf-8") as fh:
            rec = json.load(fh)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_json(path: str, data: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".approval.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def queue_approval(approvals_dir: str, tool: str, tool_input: Dict[str, Any], decision: Decision,
                   session_id: str = "", role: str = "") -> Tuple[Dict[str, Any], bool]:
    """Write pending/<id>.json atomically unless a record for the same call exists (dedupe by sha256 of the call).

    Returns (record, created). Raises OSError/ValueError when it cannot write; the hook then still denies."""
    if not approvals_dir:
        raise ValueError(f"{APPROVALS_ENV} is not set")
    h = call_hash(tool, tool_input)
    aid = approval_id(h)
    existing = read_approval(approvals_dir, aid)
    if existing is not None and existing.get("call_hash") == h:
        return existing, False
    rec = {"id": aid, "call_hash": h, "created_utc": _utc(), "session_id": str(session_id or "")[:80],
           "role": str(role or "")[:20], "tool": str(tool)[:120], "input_summary": input_summary(tool_input),
           "rule": decision.rule, "reason": decision.reason, "status": PENDING}
    _atomic_json(_record_path(approvals_dir, aid), rec)
    return rec, True


def list_approvals(approvals_dir: str) -> List[Dict[str, Any]]:
    try:
        names = sorted(os.listdir(os.path.join(approvals_dir, "pending")))
    except OSError:
        return []
    out = []
    for n in names:
        if re.match(r"^[0-9a-f]{16}\.json$", n):
            rec = read_approval(approvals_dir, n[:-5])
            if rec is not None:
                out.append(rec)
    return out


class ApprovalRefused(Exception):
    pass


def approve(approvals_dir: str, aid: str, *, env: Dict[str, str], isatty: bool,
            confirm: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Dict[str, Any]:
    """Human-only: refuses inside a hooked session (CS_AUTHORITY_MODE set) and without an interactive terminal."""
    if MODE_ENV in env:
        raise ApprovalRefused(f"{MODE_ENV} is set: this is a hooked session; only a human terminal may approve")
    if not isatty:
        raise ApprovalRefused("stdin is not an interactive terminal; only a human may approve")
    rec = read_approval(approvals_dir, aid)
    if rec is None:
        raise ApprovalRefused(f"no approval record {aid}")
    if rec.get("status") != PENDING:
        raise ApprovalRefused(f"approval {aid} is {rec.get('status')}, not PENDING")
    if confirm is not None and not confirm(rec):
        raise ApprovalRefused("not confirmed")
    rec = dict(rec, status=APPROVED, approved_utc=_utc())
    _atomic_json(_record_path(approvals_dir, aid), rec)
    return rec


def approved_for(approvals_dir: str, tool: str, tool_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The APPROVED record for exactly this call (same sha256), else None."""
    if not approvals_dir:
        return None
    h = call_hash(tool, tool_input)
    rec = read_approval(approvals_dir, approval_id(h))
    if rec and rec.get("status") == APPROVED and rec.get("call_hash") == h:
        return rec
    return None


# ------------------------------------------------------------------ PreToolUse hook


def hook_mode(env: Dict[str, str]) -> str:
    """advisory (Main, default when unset) or authoritative (workers). An unrecognised value is authoritative."""
    v = env.get(MODE_ENV)
    if v is None or v.strip().lower() == ADVISORY:
        return ADVISORY
    return AUTHORITATIVE


def context_from_env(env: Dict[str, str], cwd: str) -> PolicyContext:
    roots = tuple(r for r in env.get("CS_AUTHORITY_ROOTS", "").split(os.pathsep) if r) or ((cwd,) if cwd else ())
    owned: Optional[Tuple[str, ...]] = None
    try:
        raw = env.get("CS_AUTHORITY_OWNED_PATHS")
        if raw:
            v = json.loads(raw)
            owned = tuple(str(x) for x in v) if isinstance(v, list) else ()   # not a list: nothing owned
    except ValueError:
        owned = ()   # unparseable ownership: nothing is owned (fail closed)
    return PolicyContext(roots=roots, owned_paths=owned, allow_paid_api=False,
                         grants=frozenset(x for x in env.get("CS_AUTHORITY_GRANTS", "").split(",") if x in PROTECTED_KINDS
                                          and x not in UNCONDITIONAL_KINDS),
                         repo_root=env.get("CS_AUTHORITY_REPO_ROOT", cwd),
                         approvals_dir=env.get(APPROVALS_ENV, ""))


def _out(decision: str, reason: str) -> Dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def hook_output(decision: Decision) -> Optional[Dict[str, Any]]:
    """Advisory rendering of a bare decision (no approval queue): silence for ALLOW, else deny/ask."""
    if decision.allowed:
        return None
    return _out("deny" if decision.verdict == DENY else "ask", f"authority policy [{decision.rule}]: {decision.reason}")


def respond(tool: str, tool_input: Dict[str, Any], decision: Decision, ctx: PolicyContext, mode: str,
            session_id: str = "", role: str = "") -> Optional[Dict[str, Any]]:
    """The hook's answer for one evaluated call in the given mode (None = print nothing)."""
    why = f"authority policy [{decision.rule}]: {decision.reason}"
    if decision.verdict == DENY:
        return _out("deny", why)
    if decision.verdict == ASK:
        if mode == ADVISORY:
            rec = approved_for(ctx.approvals_dir, tool, tool_input)
            if rec is not None:
                return _out("ask", f"{why}; approval {rec.get('id')} is APPROVED by a human; confirm in the native prompt")
        try:
            rec, _created = queue_approval(ctx.approvals_dir, tool, tool_input, decision, session_id, role)
            return _out("deny", f"{why}; queued as approval {rec['id']}; continue with unrelated work")
        except Exception as exc:  # noqa: BLE001 - a queue failure must never crash the hook; it still denies
            return _out("deny", f"{why}; the approval could not be queued ({exc.__class__.__name__}); "
                                "continue with unrelated work")
    if mode == ADVISORY:
        return None
    if tool in SHELL_TOOLS:
        ok, seg = routine_shell(str(tool_input.get("command", "")), ctx)
        if not ok:
            return _out("deny", f"authority policy [worker-routine-allowlist]: not on the worker routine allowlist: {seg}")
        return _out("allow", "authority policy [worker-routine-allowlist]: routine command")
    return _out("allow", f"authority policy [{decision.rule}]: routine tool call")


def run_hook(stdin: TextIO, out: TextIO, env: Optional[Dict[str, str]] = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        mode = hook_mode(env)
        payload = json.loads(stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        tool = str(payload.get("tool_name", ""))
        tin = payload.get("tool_input") or {}
        if not isinstance(tin, dict):
            raise ValueError("tool_input is not an object")
        cwd = str(payload.get("cwd") or os.getcwd())
        ctx = context_from_env(env, cwd)
        decision = evaluate_tool_call(tool, tin, ctx, cwd)
        res = respond(tool, tin, decision, ctx, mode, str(payload.get("session_id") or ""), env.get(ROLE_ENV, ""))
    except Exception as exc:  # noqa: BLE001 - any failure must fail closed (RecursionError, OSError, ...)
        res = _out("deny", f"authority policy [hook-malformed-input]: could not evaluate the call ({exc}); failing closed")
    if res is not None:
        out.write(json.dumps(res) + "\n")
    return 0


def build_hook_command(python: str = "python", script: Optional[str] = None) -> str:
    q = lambda s: '"' + s.replace('"', '\\"') + '"' if " " in s else s
    return f"{q(python)} -B {q(script or os.path.abspath(__file__))} hook"


HOOK_MATCHER = ".*"     # every tool: the hook itself is the closed-world classifier


def hook_settings() -> Dict[str, Any]:
    """The ``hooks`` fragment a supervised session's settings file would hold (this module never writes it)."""
    return {"hooks": {"PreToolUse": [{"matcher": HOOK_MATCHER,
                                      "hooks": [{"type": "command", "command": build_hook_command()}]}]}}


def _cmd_approvals(a: Any, out: TextIO, env: Dict[str, str], isatty: bool,
                   ask: Callable[[str], str]) -> int:
    d = a.dir or env.get(APPROVALS_ENV, "")
    if not d:
        out.write(f"approvals directory unknown: pass --dir <state>/approvals or set {APPROVALS_ENV}\n")
        return 2
    if a.acmd == "list":
        recs = list_approvals(d)
        for r in recs:
            out.write(f"{r.get('id')} {r.get('status')} {r.get('created_utc')} {r.get('role')} {r.get('tool')} "
                      f"[{r.get('rule')}] {r.get('input_summary')}\n")
        if not recs:
            out.write("no approval records\n")
        return 0

    def confirm(rec: Dict[str, Any]) -> bool:
        out.write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        out.flush()
        return ask("Type APPROVE to approve this exact call: ").strip() == "APPROVE"
    try:
        rec = approve(d, a.id, env=env, isatty=isatty, confirm=confirm)
    except (ApprovalRefused, ValueError, OSError) as exc:
        out.write(f"refused: {exc}\n")
        return 2
    out.write(f"approved {rec['id']} at {rec['approved_utc']}; the session still confirms it in the native prompt\n")
    return 0


def main(argv: Optional[Sequence[str]] = None, stdin: Optional[TextIO] = None, out: Optional[TextIO] = None,
         env: Optional[Dict[str, str]] = None, ask: Optional[Callable[[str], str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dev_supervisor.authority", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="Claude Code PreToolUse hook (JSON on stdin)")
    c = sub.add_parser("check", help="evaluate one action")
    c.add_argument("--kind", required=True)
    c.add_argument("--target", default="")
    c.add_argument("--attr", action="append", default=[], help="key=value (value parsed as JSON when possible)")
    sub.add_parser("settings", help="print the hooks settings fragment")
    ap_ = sub.add_parser("approvals", help="pending approvals: list (read-only) or approve (human terminal only)")
    ap_.add_argument("--dir", default="", help="<state>/approvals (default: $" + APPROVALS_ENV + ")")
    asub = ap_.add_subparsers(dest="acmd", required=True)
    asub.add_parser("list")
    ag = asub.add_parser("approve")
    ag.add_argument("id")
    a = ap.parse_args(argv)
    stdin, out = stdin or sys.stdin, out or sys.stdout
    env = dict(os.environ if env is None else env)
    if a.cmd == "hook":
        return run_hook(stdin, out)
    if a.cmd == "settings":
        out.write(json.dumps(hook_settings(), indent=2) + "\n")
        return 0
    if a.cmd == "approvals":
        try:
            tty = bool(stdin.isatty())
        except (AttributeError, ValueError, OSError):
            tty = False
        return _cmd_approvals(a, out, env, tty, ask or input)
    attrs: Dict[str, Any] = {}
    for kv in a.attr:
        k, _, v = kv.partition("=")
        try:
            attrs[k] = json.loads(v)
        except ValueError:
            attrs[k] = v
    d = decide(Action(a.kind, a.target, attrs), context_from_env(env, os.getcwd()))
    out.write(f"{d.verdict} [{d.rule}] {d.reason}\n")
    return 0 if d.allowed else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
