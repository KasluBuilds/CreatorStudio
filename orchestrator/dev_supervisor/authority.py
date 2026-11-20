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
                         r"\.credentials?\.json|credentials(\.json)?|secrets?\.(json|ya?ml|toml)|"
                         r"\.git-credentials|\.claude\.json|gh/hosts\.ya?ml)$", re.I)
# canonical secret file names, used to test whether a search glob could select one (Grep ``glob``)
SECRET_SAMPLES = (".env", ".env.local", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", "x.pem", "x.key", "x.pfx",
                  "x.p12", ".credentials.json", "credentials.json", "credentials", "secrets.json", "secret.yaml",
                  "secrets.toml", ".git-credentials", ".claude.json", "hosts.yml", "hosts.yaml")
BOUNDARY_ENV = "CS_AUTHORITY_BOUNDARY_PATHS"   # extra boundary files (hook script, user settings), os.pathsep-joined


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
    boundary_paths: Tuple[str, ...] = ()        # live authority code, hook script, user settings (never written)
    gh_repo: str = ""                           # own repository for gh -R/--repo (CS_AUTHORITY_GH_REPO); "" = default


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
    tp = t.replace("\\", "/")
    if re.match(r"^[A-Za-z]:(?!/)", tp) or (":" in tp and not _is_abs(tp)) or tp.startswith("~"):
        return _deny("outside-environment", "drive-relative, provider or home path cannot be confined; use an absolute path")
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
        if k == READ and a.get("require_path") and not action.target:
            return _deny("missing-path", "read without a target path cannot be evaluated; failing closed")
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
        if ".git" in [c.lower() for c in _norm(action.target).split("/")]:
            return _deny("git-internals", "files under a .git directory (config, hooks, gitdir) are never written: "
                                          "a crafted git config runs arbitrary commands")
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
_RTK_RUNNERS = ("bypass", "test", "err", "summary", "run", "proxy")
_RESOLVE_READ = re.compile(r"^(get|list|is|read|find|search|describe|inspect|status|info|has|count|export_info)", re.I)


def _short_has(tok: str, letters: str) -> bool:
    """A bundle of short flags (``-am``) that contains any of ``letters``."""
    return bool(re.match(r"^-[A-Za-z]+$", tok)) and any(c in tok[1:] for c in letters)


def _abbr(tok: str, full: str, minlen: int) -> bool:
    """``tok`` (an option, ``=value`` ignored) is ``full`` or an abbreviation of it at least ``minlen`` long."""
    name = tok.split("=", 1)[0]
    return name.startswith("--") and len(name) >= minlen and full.startswith(name)


def _skip_rtk_flags(tok: Sequence[str]) -> List[str]:
    """Drop the leading ``-x`` / ``--xxx`` flags of an rtk invocation (they take no value), so the inner command is
    classified like the command it wraps (``rtk -v git push origin main``)."""
    i = 0
    while i < len(tok) and tok[i].startswith("-") and len(tok[i]) > 1:
        i += 1
    return list(tok[i:])


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
        while head == "rtk" and len(tok) > 1:        # `rtk <cmd>`, and `rtk bypass|test|err|summary|run|proxy <cmd>`
            rest = _skip_rtk_flags(tok[1:])          # rtk global flags (-v, -u, --skip-env ...) precede the subcommand
            if rest and rest[0].lower() in _RTK_RUNNERS:     # which execute <cmd>, wrap a command
                rest = _skip_rtk_flags(rest[1:])
            tok = rest
            head = tok[0].lower().replace(".exe", "") if tok else ""
        if not tok:
            continue
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
            # git accepts unambiguous prefixes of long options (`--no-veri`, `--forc`): match abbreviations too
            bypass = any(_abbr(f, "--no-verify", 5) or _abbr(f, "--no-gpg-sign", 5) for f in flags)
            if sub == "commit" and (bypass or any(f == "-n" or _short_has(f, "n") for f in flags)):
                out.append(Action(SECURITY_BYPASS))
            elif sub in ("merge", "rebase", "cherry-pick", "am", "push") and bypass:
                out.append(Action(SECURITY_BYPASS))
            elif sub == "push":
                force = any(f in ("-f", "-d") or _short_has(f, "fd") or f.startswith("--force") or _abbr(f, "--force", 4)
                            or _abbr(f, "--force-with-lease", 4) or _abbr(f, "--mirror", 4) or _abbr(f, "--delete", 4)
                            for f in flags)
                refs = [t for t in rest if not t.startswith("-")]
                specs = refs[1:]
                if any(t.startswith("+") for t in specs):
                    force = True
                dsts = [r.split(":")[-1].replace("refs/heads/", "").lstrip("+") for r in specs]
                if any(_abbr(f, "--all", 4) or _abbr(f, "--mirror", 4) for f in flags):
                    dsts.append("main")        # may include main: not delegated
                main_hit = next((d for d in dsts if d.lower() in ("main", "master")), "")
                out.append(Action(FORCE_PUSH if force else GIT_PUSH, attrs={"branch": main_hit or (dsts[-1] if dsts else ""), "force": force}))
            elif sub == "add" and any(t in ("-A", "-a", "*") or _short_has(t, "A") or _abbr(t.lower(), "--all", 4)
                                      or (not t.startswith("-") and (t.startswith(":") or _strip_dot(t) == ""))
                                      for t in rest):
                out.append(Action(STAGE_ALL))       # `.`, `./`, `x/..`, `:`, `:/`, `:!x`, `*`, `-A`, `--al(l)`
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


_RESOLVE_MUTATING_NAME = re.compile(
    r"(^|_)(delete|remove|create|add|set|update|import|render|apply|move|replace|clear|append|insert|save|close|open|"
    r"switch|load|quit|restart|export|stop|start|play|grade|link|unlink|relink|sync|merge|split|cut|trim|write|"
    r"duplicate|rename|edit|run|execute|enable|disable|reset|undo|redo|new)(_|$)", re.I)


def resolve_name_is_read(tool: str) -> bool:
    """A Resolve MCP tool whose NAME is a read verb (``get_*``, ``list_*`` ...)."""
    return tool.startswith(RESOLVE_PREFIX) and bool(_RESOLVE_READ.match(tool[len(RESOLVE_PREFIX):]))


def resolve_input_dependent(tool: str, tool_input: Dict[str, Any]) -> bool:
    """A neutrally named Resolve tool (``timeline``) whose ``action``/``method`` field names a read verb: a read only
    if the caller is honest. Mutating names (``delete_timeline``) are never input-dependent reads."""
    if not tool.startswith(RESOLVE_PREFIX) or resolve_name_is_read(tool):
        return False
    verb = str(tool_input.get("action") or tool_input.get("method") or "")
    return bool(verb) and bool(_RESOLVE_READ.match(verb)) and not _RESOLVE_MUTATING_NAME.search(tool[len(RESOLVE_PREFIX):])


def _pattern_actions(pattern: str) -> List[Action]:
    """A Glob ``pattern`` or Grep ``glob``: absolute patterns are gated by their literal directory prefix; home,
    drive-relative, provider and ``..`` patterns are outside the environment."""
    p = (pattern or "").replace("\\", "/")
    if not p:
        return []
    if p.startswith("~") or re.match(r"^[A-Za-z]:(?!/)", p) or (":" in p and not re.match(r"^[A-Za-z]:/", p)) \
            or ".." in p.split("/") or p.startswith("//"):
        return [Action(OUTSIDE_ENVIRONMENT, pattern)]
    if _is_abs(p):
        m = re.search(r"[*?\[{]", p)
        lit = p[:m.start()].rsplit("/", 1)[0] if m else p
        return [Action(READ, lit if lit and _is_abs(lit + "/") else p)]
    return []


def _brace_alternatives(g: str) -> List[str]:
    m = re.search(r"\{([^{}]*)\}", g)
    if not m:
        return [g]
    return [x for alt in m.group(1).split(",") for x in _brace_alternatives(g[:m.start()] + alt + g[m.end():])][:64]


_BENIGN_SAMPLES = ("x.py", "config.json", "x.yml", "x.yaml", "x.toml", "readme.md", "x.txt", "x.js", "x.ts", "x")


def glob_may_select_secret(glob: str) -> bool:
    """True when a search glob TARGETS secret files: it matches a canonical secret name (``.env*``, ``*.pem``,
    ``.e?v``, ``.*``) and no ordinary file name. A generic glob (``*``, ``*.json``) is no narrower than searching
    without a glob, which the path gate already governs."""
    import fnmatch
    for alt in _brace_alternatives(glob.replace("\\", "/")):
        last = (alt.rsplit("/", 1)[-1] or alt).lower()
        if any(fnmatch.fnmatchcase(s, last) for s in SECRET_SAMPLES) and \
                not any(fnmatch.fnmatchcase(b, last) for b in _BENIGN_SAMPLES):
            return True
    return False


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
        if t in ("Read", "NotebookRead"):
            if not path:
                return [Action(READ, "", {"require_path": True})]
            return _read_actions(path)
        acts = _read_actions(path)
        if t == "Glob":
            acts += _pattern_actions(str(tool_input.get("pattern") or ""))
        if t == "Grep":
            g = str(tool_input.get("glob") or "")
            acts += _pattern_actions(g)
            if g and glob_may_select_secret(g):
                acts.append(Action(SECRET_DISCLOSURE, g))
        return acts
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
        # The tool NAME decides (review N4). A read-named tool is a read. A tool whose read status would depend on a
        # caller-supplied field (``action``/``method``) is an input-dependent read in advisory mode only; the
        # authoritative hook denies it (``resolve_input_dependent``). Everything else is a production mutation.
        verb = str(tool_input.get("action") or tool_input.get("method") or "")
        if resolve_name_is_read(t) or resolve_input_dependent(t, tool_input):
            return []
        return [Action(RESOLVE_MUTATION, t, {"production": True, "verb": verb})]
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


def live_boundary_files() -> Tuple[str, ...]:
    """The authority code this hook process runs: its own file (the hook script named in the installed hook command)
    and the sibling session_policy.py that installs it."""
    here = _norm(os.path.abspath(__file__))
    return (here, here.rsplit("/", 1)[0] + "/session_policy.py")


def _boundary_files(ctx: PolicyContext) -> List[str]:
    return [_norm(p) for p in (*live_boundary_files(), *ctx.boundary_paths) if p]


def touches_boundary_path(path: str, ctx: PolicyContext) -> bool:
    """True for the approvals dir, the policy settings dir, the grants file, Claude Code settings files, the live
    authority code (authority.py, session_policy.py, the hook script) and the user settings file."""
    if not path:
        return False
    n = _norm(path)
    if n.lower().rsplit("/", 1)[-1] == GRANTS_FILE or _HOOK_SETTINGS.search(n):
        return True
    p = path if _is_abs(path.replace("\\", "/")) else (ctx.repo_root.rstrip("/\\") + "/" + path if ctx.repo_root else path)
    return any(_inside(p, d) for d in _boundary_dirs(ctx)) or any(_inside(p, f) for f in _boundary_files(ctx))


def _rel_from(path: str, base: str) -> str:
    a, b = _norm(path).lower(), _norm(base).rstrip("/").lower()
    return a[len(b) + 1:] if b and a.startswith(b + "/") else ""


def _glob_regex(glob: str) -> "re.Pattern[str]":
    """A shell glob as a regex over a normalised lower-case path: ``*``/``?`` stay inside one path component."""
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and "]" in glob[i + 2:]:
            j = glob.index("]", i + 2)
            body = glob[i + 1:j]
            out.append("[" + ("^" + body[1:] if body[:1] == "!" else body).replace("\\", "\\\\") + "]")
            i = j + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _possible_cwds(command: str, cwd: str, ctx: PolicyContext) -> List[str]:
    """Every directory the command could run in: the payload cwd, the repo root and each ``cd`` target (resolved
    against every earlier candidate, whether or not the ``cd`` would succeed)."""
    bases = [_norm(b) for b in (cwd, ctx.repo_root) if b and _is_abs(b.replace("\\", "/"))]
    for seg in _SPLIT.split(command or ""):
        tok = _tokens(seg)
        if len(tok) >= 2 and tok[0].lower() in ("cd", "pushd", "chdir", "set-location", "sl"):
            target = next((t for t in tok[1:] if not t.startswith("-")), "")
            for b in list(bases):
                r = resolve_shell_path(target, b)
                if r and r not in bases and len(bases) < 32:
                    bases.append(r)
    return bases


def touches_boundary_command(command: str, ctx: PolicyContext, cwd: str = "") -> bool:
    """A shell command that names a boundary path: absolute, or relative to the payload cwd, the repo root or any
    ``cd`` target in the command; ``./`` and ``//`` spellings, globs that could match a boundary file, and a
    directory-level ``git checkout``/``git restore`` that contains one count too (review round 2, N3). Text-based:
    see STANDING_AUTONOMY.md for what this cannot catch."""
    low = re.sub(r"/+", "/", (command or "").replace("\\", "/").lower())
    while "/./" in low:
        low = low.replace("/./", "/")
    if GRANTS_FILE in low or ".claude/settings" in low or "approvals/pending" in low or _APPROVE_CMD.search(command or ""):
        return True
    if any(d.lower() in low for d in _boundary_dirs(ctx)):
        return True
    files = _boundary_files(ctx)
    for f in files:
        forms = {f.lower(), _rel_from(f, cwd), _rel_from(f, ctx.repo_root)}
        if any(x and x in low for x in forms):
            return True
    bases = _possible_cwds(command, cwd, ctx)
    lfiles = [f.lower() for f in files]
    for seg in _SPLIT.split(command or ""):
        tok = _tokens(seg)
        low_tok = [t.lower() for t in tok]
        restore = len(tok) > 1 and low_tok[0] in ("git", "git.exe") and any(t in ("checkout", "restore") for t in low_tok[1:])
        for t in tok[1:]:
            arg = t.split("=", 1)[1] if t.startswith("-") and "=" in t else t
            if not arg or arg.startswith("-"):
                continue
            for b in bases:
                r = resolve_shell_path(arg, b)
                if r is None:
                    continue
                r = r.lower()
                if _GLOB_CHARS.search(arg) or _BRACE_EXPANSION.search(arg):
                    pats = [_glob_regex(_norm(alt).lower()) for alt in _brace_alternatives(r)]
                    if any(p.match(f) for p in pats for f in lfiles):
                        return True
                    continue
                if any(_inside(r, f) for f in lfiles):
                    return True
                if restore and any(_inside(f, r) for f in lfiles):
                    return True               # `git checkout HEAD~1 -- orchestrator/dev_supervisor/` restores them
    return False


_ORDER = {ALLOW: 0, ASK: 1, DENY: 2}


def evaluate_tool_call(tool: str, tool_input: Dict[str, Any], ctx: PolicyContext, cwd: str = "") -> Decision:
    """Worst verdict across the actions implied by the call. Unknown tools are denied (closed world)."""
    acts: List[Action] = []          # boundary first: on equal verdicts its rule is the one reported
    if tool in WRITE_TOOLS and touches_boundary_path(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""), ctx):
        acts.append(Action(AUTHORITY_CHANGE, str(tool_input.get("file_path") or "")))
    if tool in SHELL_TOOLS and touches_boundary_command(str(tool_input.get("command", "")), ctx, cwd):
        acts.append(Action(AUTHORITY_CHANGE, "shell"))
    acts += classify_tool_call(tool, tool_input, cwd)
    worst = Decision(ALLOW, "no-protected-action", "no protected pattern recognised")
    for act in acts:
        d = decide(act, ctx)
        if _ORDER[d.verdict] > _ORDER[worst.verdict]:
            worst = d
    return worst


# ------------------------------------------------------------------ worker routine allowlist (authoritative mode)
# STRUCTURAL rule (T-DEV-028 security review round 2). Two review rounds found bypasses that came from the policy's
# shell parser disagreeing with the real shell (a failed or piped `cd`, PowerShell scriptblocks, `rtk test <cmd>`).
# The worker allow path therefore no longer models shell semantics at all. A shell call gets an explicit allow only
# when ALL of these hold; anything else is denied:
#   1. the tool is Bash (the PowerShell tool is never allowed for a worker);
#   2. the command is ONE simple command: after stripping one trailing ` 2>&1` it contains none of
#      ; & | < > ` $ ( ) { } [ ] * ? ! ~ # \ CR LF (or any other control character), and every quote is a matched
#      pair of single or double quotes forming a whole token;
#   3. there is no cd/pushd/popd at all, and the payload cwd is inside the environment roots;
#   4. the head is on the closed allowlist with its exact per-subcommand option lists (`rtk` only in front of
#      git, gh, ls, grep, find, wc or diff);
#   5. every path-like operand resolves inside the roots (not the temp dir).
# Workers read files with the Read, Grep and Glob tools, so pipelines are not needed. If in doubt, deny: an explicit
# allow is a promise.

_SHELL_META = re.compile(r"[;&|<>`$(){}\[\]*?!~#\\\x00-\x08\x0a-\x1f\x7f]")
_SIMPLE_WORD = re.compile(r"'[^']*'|\"[^\"]*\"|[^\s'\"]+")
_BRACE_EXPANSION = re.compile(r"\{[^}]*(,|\.\.)[^}]*\}")
_GLOB_CHARS = re.compile(r"[*?\[]")
RTK_PASSTHROUGH = ("git", "gh", "ls", "grep", "find", "wc", "diff")


@dataclass(frozen=True)
class _Tok:
    text: str
    expands: bool = False     # kept for the option parser; the structural rule never produces an expanding token
    globs: bool = False


def simple_tokens(command: str) -> Optional[List[_Tok]]:
    """Words of ONE simple command, or None when it does not tokenise cleanly: words are separated by spaces or
    tabs, and a quoted word is exactly one matched pair of quotes (``a"b"``, ``"a"b`` and unbalanced quotes fail)."""
    toks: List[_Tok] = []
    i, n = 0, len(command)
    while i < n:
        if command[i] in " \t":
            i += 1
            continue
        m = _SIMPLE_WORD.match(command, i)
        if not m:
            return None
        if m.end() < n and command[m.end()] not in " \t":
            return None                                   # glued pieces (a"b", "a"'b') are not a clean token
        w = m.group(0)
        toks.append(_Tok(w[1:-1] if w[0] in "'\"" else w))
        i = m.end()
    return toks


def _env_roots(ctx: PolicyContext) -> List[str]:
    return [r for r in (*ctx.roots, ctx.repo_root) if r]


def within_roots(path: str, ctx: PolicyContext) -> bool:
    """True when ``path`` (absolute) is inside one of the environment roots. The temp directory does not count."""
    return bool(path) and _is_abs(path.replace("\\", "/")) and any(_inside(path, r) for r in _env_roots(ctx))


def resolve_shell_path(arg: str, cwd: str) -> Optional[str]:
    """Absolute normalised path of a shell argument, or None when it cannot be checked (home, drive-relative,
    provider/URL forms such as ``env:X`` or ``HKLM:``, UNC/device paths)."""
    a = (arg or "").replace("\\", "/")
    if not a or a.startswith("~") or a.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:(?!/)", a) or (":" in a and not re.match(r"^[A-Za-z]:/", a)):
        return None
    if _is_abs(a):
        return _norm(a)
    if not cwd:
        return None
    return _norm(cwd.replace("\\", "/").rstrip("/") + "/" + a)


def _sensitive_name(p: str) -> bool:
    n = _norm(p.replace("\\", "/"))
    return bool(SECRET_NAME.search(n)) or n.lower().rsplit("/", 1)[-1] in ("pro.ps1", "backup.ps1")


def _path_ok(tok: _Tok, cwd: str, ctx: PolicyContext, *, write: bool = False) -> bool:
    """A path operand: inside the roots, not a secret or launcher, globs only with a literal leading character and
    never a dot-glob (``.*`` can match ``..``); writes never touch a ``.git`` component."""
    t = tok.text
    if not t or t.startswith("-") or _sensitive_name(t):
        return False
    if tok.globs:
        if _GLOB_CHARS.match(t) or t.startswith(("~", "{")) or _BRACE_EXPANSION.search(t) or write:
            return False
        if any(c.startswith(".") and _GLOB_CHARS.search(c) for c in t.replace("\\", "/").split("/")):
            return False
    r = resolve_shell_path(t, cwd)
    if r is None or not within_roots(r, ctx):
        return False
    if write and ".git" in [c.lower() for c in r.split("/")]:
        return False
    return True


def _git_pos_ok(tok: _Tok, cwd: str, ctx: PolicyContext) -> bool:
    """A git revision or pathspec of a read command: git reads it inside the repository, so revision syntax
    (``HEAD:x``, ``a..b``, ``@{u}``) is fine; absolute, home, drive-relative and escaping paths are not."""
    t = tok.text.replace("\\", "/")
    if not t or t.startswith(("-", "~")) or _sensitive_name(t) or _sensitive_name(t.split(":", 1)[-1]) \
            or _is_abs(t) or re.match(r"^[A-Za-z]:", t):
        return False
    if tok.globs and (_GLOB_CHARS.match(t) or _BRACE_EXPANSION.search(t)):
        return False
    return within_roots(_norm(cwd.replace("\\", "/").rstrip("/") + "/" + t), ctx) if cwd else False


@dataclass(frozen=True)
class _Spec:
    flags: FrozenSet[str] = frozenset()                     # exact option tokens without a value
    values: Dict[str, Any] = field(default_factory=dict)    # option -> "any" | "int" | "path" | "wpath" | tuple
    patterns: Tuple[str, ...] = ()                          # regexes a whole option token may match (``-5``)
    ci: bool = False                                        # also match PowerShell-style options case-insensitively


def _spec(flags: Iterable[str] = (), values: Optional[Dict[str, Any]] = None, patterns: Iterable[str] = (),
          ci: bool = False) -> _Spec:
    return _Spec(frozenset(flags), dict(values or {}), tuple(patterns), ci)


_INT = re.compile(r"^[+-]?\d+$")
_PLAIN_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")      # a unittest file pattern: no path, no glob
OWN_GH_REPO = "KasluBuilds/CreatorStudio"
GH_REPO_ENV = "CS_AUTHORITY_GH_REPO"


def gh_repo_ok(value: str, ctx: PolicyContext) -> bool:
    """``-R/--repo`` of a worker gh read: exactly the own repository (``ctx.gh_repo``, else the constant). Another
    owner/name, a bare name, a host-qualified name and a full URL are refused."""
    want = (ctx.gh_repo or OWN_GH_REPO).strip()
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value or "")) \
        and value.lower() == want.lower()


def _value_ok(val: _Tok, kind: Any, cwd: str, ctx: PolicyContext) -> bool:
    if isinstance(kind, tuple):
        return val.text in kind
    if kind == "int":
        return bool(_INT.match(val.text))
    if kind == "path":
        return _path_ok(val, cwd, ctx)
    if kind == "wpath":
        return _path_ok(val, cwd, ctx, write=True)
    if kind == "rev":
        return _git_pos_ok(val, cwd, ctx)
    if kind == "repo":
        return gh_repo_ok(val.text, ctx)
    if kind == "pattern":
        return bool(_PLAIN_PATTERN.match(val.text))
    return kind == "any" and not val.text.startswith("-") or kind == "any-dash"


def _parse(args: Sequence[_Tok], spec: _Spec, cwd: str, ctx: PolicyContext,
           seen: Optional[Dict[str, List[str]]] = None) -> Optional[List[_Tok]]:
    """Positional operands when every option is exactly on ``spec`` with a valid value; None otherwise."""
    pos: List[_Tok] = []
    i, dashdash = 0, False
    while i < len(args):
        tok = args[i]
        t = tok.text
        if dashdash or not t.startswith("-") or t == "-":
            pos.append(tok)
            i += 1
            continue
        if t == "--":
            dashdash = True
            i += 1
            continue
        key, flags, values = t, spec.flags, spec.values
        if spec.ci and t not in flags and t.partition("=")[0] not in values:
            key = t = t.lower()                           # PowerShell parameters: case-insensitive, still exact
            flags = frozenset(f.lower() for f in spec.flags)
            values = {k.lower(): v for k, v in spec.values.items()}
        if key in flags:
            if seen is not None:
                seen.setdefault(key, [])
            i += 1
            continue
        name, eq, val = t.partition("=")
        if t.startswith("--") and eq and name in values:
            if not _value_ok(_Tok(val, tok.expands, tok.globs), values[name], cwd, ctx):
                return None
            if seen is not None:
                seen.setdefault(name, []).append(val)
            i += 1
            continue
        if key in values:
            if i + 1 >= len(args) or not _value_ok(args[i + 1], values[key], cwd, ctx):
                return None
            if seen is not None:
                seen.setdefault(key, []).append(args[i + 1].text)
            i += 2
            continue
        if any(re.fullmatch(p, t) for p in spec.patterns):
            i += 1
            continue
        return None
    return pos


# ---- git: no global options at all (-C, --git-dir, --work-tree, -c, --exec-path ... are refused)
_DIFF_FLAGS = ("--stat", "--shortstat", "--numstat", "--name-only", "--name-status", "--summary", "--color",
               "--no-color", "--patch", "-p", "--no-patch", "-s", "--raw", "--no-renames", "--find-renames", "-M",
               "-w", "--ignore-all-space", "-b", "--ignore-space-change", "--ignore-blank-lines", "--minimal",
               "--patience", "--histogram", "--word-diff", "--check", "--no-ext-diff", "--no-textconv", "-R",
               "--full-index", "--abbrev", "--exit-code", "--quiet", "--cached", "--staged", "--merge-base")
_DIFF_VALUES = {"--stat": "any", "--color": "any", "--word-diff": "any", "--unified": "int", "-U": "int",
                "--diff-filter": "any", "--abbrev": "int"}
_LOG_FLAGS = ("--oneline", "--graph", "--decorate", "--no-decorate", "--all", "--branches", "--remotes", "--tags",
              "--reverse", "--no-merges", "--merges", "--first-parent", "--follow", "--abbrev-commit",
              "--left-right", "--date-order", "--topo-order", "--full-history", "--cherry-pick")
_LOG_VALUES = {"-n": "int", "--max-count": "int", "--skip": "int", "--since": "any", "--until": "any",
               "--after": "any", "--before": "any", "--author": "any", "--committer": "any", "--grep": "any",
               "--format": "any", "--pretty": "any", "--date": "any", "--decorate": "any"}
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
_FETCH_FLAGS = ("--prune", "-p", "--tags", "--no-tags", "--quiet", "-q", "--verbose", "-v", "--all", "--dry-run",
                "--prune-tags", "--no-recurse-submodules")
_PULL_FLAGS = _FETCH_FLAGS + ("--ff-only", "--ff", "--no-ff", "--rebase", "--no-rebase", "--no-edit", "--stat",
                              "--no-stat")
_GIT_CONFIG_KEY = re.compile(r"^(user\.(name|email)|remote\.[A-Za-z0-9_.-]+\.(url|fetch)|"
                             r"branch\.[A-Za-z0-9_./-]+\.(remote|merge)|init\.defaultbranch)$", re.I)

GIT_SPECS: Dict[str, _Spec] = {
    "status": _spec(("-s", "--short", "-b", "--branch", "--porcelain", "--long", "-v", "--verbose", "--ignored",
                     "--no-renames", "--show-stash", "--ahead-behind", "--no-ahead-behind"),
                    {"--porcelain": ("v1", "v2"), "--untracked-files": ("no", "normal", "all")}),
    "diff": _spec(_DIFF_FLAGS, _DIFF_VALUES, (r"-U\d+",)),
    "log": _spec(_DIFF_FLAGS + _LOG_FLAGS, {**_DIFF_VALUES, **_LOG_VALUES}, (r"-\d+", r"-n\d+", r"-U\d+")),
    "show": _spec(_DIFF_FLAGS + ("--oneline", "--abbrev-commit"), {**_DIFF_VALUES, "--format": "any",
                                                                   "--pretty": "any", "--date": "any"}, (r"-U\d+",)),
    "rev-parse": _spec(("--abbrev-ref", "--show-toplevel", "--git-dir", "--git-common-dir", "--absolute-git-dir",
                        "--short", "--verify", "--is-inside-work-tree", "--is-inside-git-dir", "--symbolic-full-name",
                        "--show-prefix", "--show-cdup", "--quiet", "-q", "--symbolic"), {"--short": "int"}),
    "ls-files": _spec(("--cached", "-c", "--deleted", "-d", "--modified", "-m", "--others", "-o", "--stage", "-s",
                       "--unmerged", "-u", "-z", "--exclude-standard", "--full-name", "--error-unmatch", "--directory",
                       "--no-empty-directory", "-t", "-v")),
    "merge-base": _spec(("--is-ancestor", "--all", "--fork-point", "--octopus", "--independent")),
    "fetch": _spec(_FETCH_FLAGS),
    "pull": _spec(_PULL_FLAGS),
    "branch": _spec(("-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "-l", "--show-current",
                     "--no-color", "--merged", "--no-merged", "--contains", "--track", "--no-track"),
                    {"--sort": "any", "--format": "any", "--points-at": "any", "--color": "any"}),
    "checkout": _spec(("-q", "--quiet", "--track", "--no-track"), {"-b": "any"}),
    "switch": _spec(("-q", "--quiet", "--track", "--no-track", "--detach"), {"-c": "any", "--create": "any"}),
    "add": _spec(("-v", "--verbose", "-n", "--dry-run", "-N", "--intent-to-add")),
    "commit": _spec(("-q", "--quiet", "-v", "--verbose", "-s", "--signoff"),
                    {"-m": "any-dash", "--message": "any-dash", "-F": "path", "--file": "path", "--author": "any"}),
    "worktree": _spec(("--porcelain", "-v", "--verbose", "-z")),
    "stash": _spec(("--stat", "-p", "--patch", "--name-only", "--name-status", "--oneline")),
    "remote": _spec(("-v", "--verbose")),
    "config": _spec((), {"--get": "any", "--get-all": "any"}),
}
_GIT_READ_POSITIONALS = ("status", "diff", "log", "show", "rev-parse", "ls-files", "merge-base")


def _git_routine(args: Sequence[_Tok], cwd: str, ctx: PolicyContext) -> bool:
    if not args or args[0].text.startswith("-") or args[0].text not in GIT_SPECS:
        return False                                      # any global option (-C, --git-dir, -c ...) is refused
    sub, rest = args[0].text, list(args[1:])
    seen: Dict[str, List[str]] = {}
    pos = _parse(rest, GIT_SPECS[sub], cwd, ctx, seen)
    if pos is None:
        return False
    texts = [p.text for p in pos]
    if sub in _GIT_READ_POSITIONALS:
        return all(_git_pos_ok(p, cwd, ctx) for p in pos)
    if sub in ("fetch", "pull"):
        if not texts:
            return True
        return bool(_REMOTE_NAME.match(texts[0])) and all(_REF_NAME.match(t) and ".." not in t for t in texts[1:])
    if sub == "branch":
        return len(texts) <= 2 and all(_REF_NAME.match(t) and ".." not in t for t in texts)
    if sub == "checkout":                                 # only `checkout -b <new> [<start>]`; never paths or --
        return "-b" in seen and len(seen["-b"]) == 1 and bool(_REF_NAME.match(seen["-b"][0])) and len(texts) <= 1 \
            and all(_REF_NAME.match(t) and ".." not in t for t in texts) and "--" not in [a.text for a in rest]
    if sub == "switch":
        created = seen.get("-c", []) + seen.get("--create", [])
        if "--" in [a.text for a in rest] or len(created) > 1 or not all(_REF_NAME.match(c) for c in created):
            return False
        if not created and len(texts) != 1:
            return False
        return len(texts) <= 1 and all(_REF_NAME.match(t) and ".." not in t for t in texts)
    if sub == "add":
        return bool(pos) and all(_add_pathspec_ok(p, cwd, ctx) for p in pos)
    if sub == "commit":                                   # a message is required; no pathspecs (`commit .` = -a)
        return not texts and any(k in seen for k in ("-m", "--message", "-F", "--file"))
    if sub == "worktree":
        return texts == ["list"]
    if sub == "stash":
        if not texts or texts[0] not in ("list", "show"):
            return False
        return len(texts) == 1 or (texts[0] == "show" and len(texts) == 2
                                   and bool(re.match(r"^(stash@\{\d+\}|\d+)$", texts[1])))
    if sub == "remote":
        return not texts or (not seen and len(texts) == 2 and texts[0] == "get-url"
                             and bool(_REMOTE_NAME.match(texts[1])))
    if sub == "config":
        keys = seen.get("--get", []) + seen.get("--get-all", [])
        return not texts and len(keys) == 1 and bool(_GIT_CONFIG_KEY.match(keys[0]))
    return False


def _add_pathspec_ok(tok: _Tok, cwd: str, ctx: PolicyContext) -> bool:
    """An explicit file or directory: no root pathspec (``.``, ``./``, ``:``, ``:/``, ``*``), no pathspec magic or
    git glob (git globs even quoted pathspecs), nothing normalising to a root or the cwd, nothing outside the roots."""
    t = tok.text.replace("\\", "/")
    if not t or t.startswith((":", "-")) or _GLOB_CHARS.search(t) or tok.globs:
        return False
    if t.rstrip("/") in (".", "") or not _path_ok(tok, cwd, ctx):
        return False
    r = resolve_shell_path(t, cwd) or ""
    return not any(_norm(r).rstrip("/").lower() == _norm(x).rstrip("/").lower() for x in [cwd, *_env_roots(ctx)] if x)


# ---- gh: per-verb option allowlists; --jq/--template (environment access), --web and --repo on writes are refused
_GH_READ_COMMON = {"-R": "repo", "--repo": "repo", "--json": "any"}
GH_SPECS: Dict[Tuple[str, str], _Spec] = {
    ("pr", "view"): _spec(("--comments", "-c"), _GH_READ_COMMON),
    ("pr", "list"): _spec((), {**_GH_READ_COMMON, "--state": "any", "-s": "any", "--limit": "int", "-L": "int",
                               "--author": "any", "-A": "any", "--label": "any", "-l": "any", "--base": "any",
                               "-B": "any", "--head": "any", "-H": "any", "--search": "any", "-S": "any"}),
    ("pr", "checks"): _spec(("--required",), _GH_READ_COMMON),
    ("pr", "diff"): _spec(("--name-only", "--patch"), {"-R": "repo", "--repo": "repo", "--color": "any"}),
    ("pr", "create"): _spec(("--draft", "-d"), {"--title": "any-dash", "-t": "any-dash", "--body": "any-dash",
                                                "-b": "any-dash", "--body-file": "path", "-F": "path", "--base": "any",
                                                "-B": "any", "--head": "any", "-H": "any", "--label": "any",
                                                "-l": "any"}),
    ("pr", "comment"): _spec((), {"--body": "any-dash", "-b": "any-dash", "--body-file": "path", "-F": "path"}),
    ("issue", "view"): _spec(("--comments", "-c"), _GH_READ_COMMON),
    ("issue", "list"): _spec((), {**_GH_READ_COMMON, "--state": "any", "-s": "any", "--limit": "int", "-L": "int",
                                  "--author": "any", "-A": "any", "--assignee": "any", "-a": "any", "--label": "any",
                                  "-l": "any", "--search": "any", "-S": "any"}),
    ("issue", "create"): _spec((), {"--title": "any-dash", "-t": "any-dash", "--body": "any-dash", "-b": "any-dash",
                                    "--body-file": "path", "-F": "path", "--label": "any", "-l": "any"}),
    ("issue", "comment"): _spec((), {"--body": "any-dash", "-b": "any-dash", "--body-file": "path", "-F": "path"}),
}
_GH_API = _spec(("--paginate", "-i", "--include", "--silent"), {"-X": ("GET", "get"), "--method": ("GET", "get")},
                (r"-X(GET|get)",))
_GH_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_GH_OPERAND = re.compile(r"^(\d+|[A-Za-z0-9][A-Za-z0-9_./-]*)$")     # a number or a branch name; never a URL (N2)
_GH_ENDPOINT = re.compile(r"^/?" + _GH_NAME + r"(/" + _GH_NAME + r")*$")


def _gh_operand_ok(t: str) -> bool:
    """A PR/issue number or branch name. ``:`` (a URL scheme) is refused, as are a host-like first segment and a
    ``/pull/`` or ``/issues/`` path, so another repository's full URL never gets an allow (N2)."""
    if not _GH_OPERAND.match(t) or ".." in t.split("/"):
        return False
    first = t.split("/", 1)[0]
    return not ("/" in t and "." in first) and not re.search(r"/(pull|pulls|issues)/", t)


def _gh_endpoint_ok(t: str) -> bool:
    """A relative REST endpoint (``user``, ``rate_limit``); a ``repos/...`` endpoint must start with
    ``repos/<owner>/<repo>``. No URL, no host, no ``..``, no graphql (N2)."""
    if not _GH_ENDPOINT.match(t) or t.startswith("//"):
        return False
    segs = t.lstrip("/").split("/")
    if any(x in ("..", ".") for x in segs) or segs[0].lower() == "graphql" or "." in segs[0]:
        return False
    return segs[0] != "repos" or len(segs) >= 3


def _gh_routine(args: Sequence[_Tok], cwd: str, ctx: PolicyContext) -> bool:
    texts = [a.text for a in args]
    if len(texts) >= 2 and texts[0] == "api":
        pos = _parse(args[1:], _GH_API, cwd, ctx)
        return pos is not None and len(pos) == 1 and _gh_endpoint_ok(pos[0].text)
    if len(texts) < 2 or (texts[0], texts[1]) not in GH_SPECS:
        return False
    pos = _parse(args[2:], GH_SPECS[(texts[0], texts[1])], cwd, ctx)
    return pos is not None and len(pos) <= 1 and all(_gh_operand_ok(p.text) for p in pos)


# ---- python -m: closed module and subcommand allowlists (B4)
_UNITTEST = _spec(("-v", "--verbose", "-q", "--quiet", "-f", "--failfast", "-b", "--buffer", "-c", "--catch",
                   "--locals"), {"-k": "any"})
_UNITTEST_DISCOVER = _spec(_UNITTEST.flags, {"-k": "any", "-s": "path", "--start-directory": "path", "-p": "pattern",
                                             "--pattern": "pattern", "-t": "path", "--top-level-directory": "path"})
GOVERNANCE_SPECS: Dict[Tuple[str, ...], _Spec] = {
    ("validate",): _spec((), {"--against": "rev"}),
    ("leases",): _spec((), {"--now": "any"}),
    ("handoff",): _spec(("--check",)),                    # never --write
    ("wave", "submit"): _spec((), {"--attempts": "int"}),
    ("wave", "status"): _spec(("--json",)),
}
DEV_SUPERVISOR_SPECS: Dict[str, _Spec] = {               # never --yes, never --config
    "status": _spec(("--json",)), "workers": _spec(("--json",)), "doctor": _spec(("--json",)),
    "report": _spec(("--json",)),
    "compact-check": _spec(("--json",), {"--session-id": "any", "--handoff-id": "any"}),
    "snapshot": _spec(("--json",), {"--out": "wpath"}),
    "route": _spec(("--json", "--validation-failed"),
                   {"--criticality": "any", "--complexity": "any", "--context-tokens": "int",
                    "--deadline-minutes": "any", "--concurrency": "int", "--pressure": "any", "--failures": "int"}),
}
CREATOR_STATE_SPECS: Dict[str, Tuple[_Spec, int]] = {    # command -> (options, number of project/source operands)
    "list": (_spec(), 0), "incoming-list": (_spec(), 0),
    "check": (_spec(("--quick",)), 1), "resume": (_spec(), 1), "checkpoints": (_spec(), 1),
    "log": (_spec((), {"-n": "int"}), 1), "inventory": (_spec(("--json",), {"--type": "any"}), 1),
    "ingest-status": (_spec(), 1), "verify-sources": (_spec(("--deep",)), 1),
    "incoming-inspect": (_spec(("--json",)), 1), "shots-plan": (_spec(("--json",), {"--id": "any"}), 1),
    "shots-status": (_spec(("--json",), {"--id": "any"}), 1), "shots-check": (_spec(("--json",), {"--id": "any"}), 1),
}
CREATOR_STATE_READ = tuple(CREATOR_STATE_SPECS) + ("--help", "-h")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _is_test_module_path(path: str, ctx: PolicyContext) -> bool:
    """``path`` (absolute) is ``<root>/orchestrator/tests/**/test_*.py`` for one of the environment roots."""
    p = _norm(path)
    if not re.fullmatch(r"test_[A-Za-z0-9_]*\.py", p.rsplit("/", 1)[-1]):
        return False
    return any(p.lower().startswith(_norm(r).rstrip("/").lower() + "/orchestrator/tests/") for r in _env_roots(ctx))


def _unittest_target_ok(tok: _Tok, cwd: str, ctx: PolicyContext) -> bool:
    """A ``python -m unittest`` operand (not ``discover``): a test module file under ``orchestrator/tests`` named
    ``test_*.py``, given as a path or as a dotted name (``test_x``, ``tests.test_x.Case.test_m``) that resolves to
    one relative to the cwd. Any other dotted name (a standard-library or third-party module) imports arbitrary code."""
    t = tok.text
    if t.endswith(".py") and ("/" in t or "\\" in t):
        r = resolve_shell_path(t, cwd)
        return r is not None and _is_test_module_path(r, ctx)
    if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", t):
        return False
    segs = t.split(".")
    for k in range(1, len(segs) + 1):                     # the longest prefix is the module, the rest class/method
        r = resolve_shell_path("/".join(segs[:k]) + ".py", cwd)
        if r is not None and _is_test_module_path(r, ctx):
            return True
    return False


def _python_routine(args: Sequence[_Tok], cwd: str, ctx: PolicyContext) -> bool:
    texts = [a.text for a in args]
    i = 0
    while i < len(texts) and texts[i] in ("-B", "-I", "-BI", "-IB"):
        i += 1
    if i + 1 >= len(texts) or texts[i] != "-m":
        return False
    mod, rest = texts[i + 1], list(args[i + 2:])
    rt = [a.text for a in rest]
    if mod == "unittest":
        if rt[:1] == ["discover"]:
            pos = _parse(rest[1:], _UNITTEST_DISCOVER, cwd, ctx)
            return pos == []
        pos = _parse(rest, _UNITTEST, cwd, ctx)
        return bool(pos) and all(_unittest_target_ok(p, cwd, ctx) for p in pos)    # bare `unittest` discovers: no
    if mod == "governance":
        for key, spec in GOVERNANCE_SPECS.items():
            if tuple(rt[:len(key)]) == key:
                pos = _parse(rest[len(key):], spec, cwd, ctx)
                if key == ("wave", "status"):
                    return pos is not None and len(pos) <= 1 and all(_SLUG.match(p.text) for p in pos)
                return pos == []
        return False
    if mod == "dev_supervisor":
        if not rt or rt[0] not in DEV_SUPERVISOR_SPECS:
            return False
        return _parse(rest[1:], DEV_SUPERVISOR_SPECS[rt[0]], cwd, ctx) == []
    if mod == "creator_state":
        if rt in (["--help"], ["-h"]):
            return True
        if not rt or rt[0] not in CREATOR_STATE_SPECS:
            return False
        spec, n = CREATOR_STATE_SPECS[rt[0]]
        pos = _parse(rest[1:], spec, cwd, ctx)
        return pos is not None and len(pos) == n and all(
            _SLUG.match(p.text) if not re.search(r"[/\\:]", p.text) else _path_ok(p, cwd, ctx) for p in pos)
    return False


# ---- simple read/inspect commands
_LS = _spec(("-force", "-recurse", "-name", "-file", "-directory"), {"-filter": "any", "-depth": "int"},
            (r"-[laAhR1trSdFGp]+",), ci=True)
_CAT = _spec(("-n", "-A", "-v", "-e", "-E", "-T", "-s", "-b", "-raw"),
             {"-totalcount": "int", "-tail": "int", "-head": "int", "-first": "int"}, ci=True)
_HEAD = _spec(("-q", "-v"), {"-n": "int", "-c": "int", "--lines": "int", "--bytes": "int"},
              (r"-\d+", r"-n[+-]?\d+", r"-c[+-]?\d+"))
_WC = _spec(("-l", "-w", "-c", "-m", "-L", "--lines", "--words", "--bytes", "--chars"))
_GREP = _spec(("--recursive", "--ignore-case", "--line-number", "--files-with-matches", "--files-without-match",
               "--count", "--invert-match", "--word-regexp", "--extended-regexp", "--fixed-strings",
               "--with-filename", "--no-filename", "--only-matching", "--quiet", "--no-messages"),
              {"-e": "any-dash", "--regexp": "any-dash", "-m": "int", "--max-count": "int", "-A": "int", "-B": "int",
               "-C": "int", "--include": "any", "--exclude": "any", "--exclude-dir": "any", "--color": "any",
               "--colour": "any"}, (r"-[rinlLvcwxEFHhoqsI]+", r"-[ABCm]\d+"))
_RG = _spec(("--files", "--hidden", "--no-ignore", "--ignore-case", "--smart-case", "--line-number",
             "--files-with-matches", "--count", "--fixed-strings", "--word-regexp", "--no-heading", "--heading",
             "--with-filename", "--no-filename", "--only-matching", "--vimgrep", "--multiline", "--json"),
            {"-e": "any-dash", "--regexp": "any-dash", "-g": "any", "--glob": "any", "-t": "any", "--type": "any",
             "-T": "any", "--type-not": "any", "-m": "int", "--max-count": "int", "-A": "int", "-B": "int",
             "-C": "int", "--max-depth": "int", "--color": "any"}, (r"-[inlvcwxFHoqsSuU]+", r"-[ABCm]\d+"))
_SORT = _spec(("--reverse", "--numeric-sort", "--unique", "--ignore-case", "--human-numeric-sort", "--version-sort",
               "--stable"), {"-k": "any", "--key": "any", "-t": "any", "--field-separator": "any"},
              (r"-[rnufhVbsdgiM]+",))
_UNIQ = _spec(("-c", "-d", "-u", "-i", "--count", "--repeated", "--unique", "--ignore-case"), {"-f": "int", "-s": "int"})
_DIFF = _spec(("--brief", "--recursive", "--new-file", "--ignore-all-space", "--ignore-space-change", "--text"),
              {"-U": "int", "--unified": "int", "--color": "any"}, (r"-[uNrqwbBiat]+", r"-U\d+"))
_MKDIR = _spec(("-p", "-v", "--parents", "--verbose", "-force"), ci=True)
_SED = _spec(("-n", "-E", "-r", "--quiet", "--silent", "-nE", "-En", "-nr", "-rn"), {"-e": "any-dash"})
_SED_SCRIPT = re.compile(r"^(?:(?:\d+|\$)(?:,(?:\d+|\$|\+\d+))?p)(?:;(?:(?:\d+|\$)(?:,(?:\d+|\$|\+\d+))?p))*$")
_FIND_FLAGS = ("-empty", "-print", "-print0", "-prune", "-not", "!", "-o", "-or", "-a", "-and", "(", ")", "\\(",
               "\\)", "-readable", "-writable")
_FIND_VALUES = {"-name": "any", "-iname": "any", "-path": "any", "-ipath": "any", "-type": ("f", "d", "l"),
                "-maxdepth": "int", "-mindepth": "int", "-size": "any-dash", "-mtime": "any-dash",
                "-mmin": "any-dash", "-newer": "path", "-regex": "any", "-iregex": "any"}
_TEST_OPS = ("=", "!=", "==", "!", "-eq", "-ne", "-lt", "-le", "-gt", "-ge", "-a", "-o", "-f", "-d", "-e", "-s", "-z",
             "-n", "-r", "-w", "-x", "-L", "-h")


def _paths_ok(pos: Optional[Sequence[_Tok]], cwd: str, ctx: PolicyContext, *, write: bool = False) -> bool:
    return pos is not None and all(_path_ok(p, cwd, ctx, write=write) for p in pos)


def _pattern_then_paths(args: Sequence[_Tok], spec: _Spec, cwd: str, ctx: PolicyContext) -> bool:
    seen: Dict[str, List[str]] = {}
    pos = _parse(args, spec, cwd, ctx, seen)
    if pos is None:
        return False
    if not any(k in seen for k in ("-e", "--regexp")):
        if not pos:
            return False
        pos = pos[1:]                                     # the first operand is the pattern, not a path
    return _paths_ok(pos, cwd, ctx)


def _find_routine(args: Sequence[_Tok], cwd: str, ctx: PolicyContext) -> bool:
    i = 0
    while i < len(args) and not args[i].text.startswith(("-", "(", "\\(", "!")):
        if not _path_ok(args[i], cwd, ctx):
            return False
        i += 1
    while i < len(args):
        t = args[i].text
        if t in _FIND_FLAGS:
            i += 1
        elif t in _FIND_VALUES and i + 1 < len(args) and _value_ok(args[i + 1], _FIND_VALUES[t], cwd, ctx):
            i += 2
        else:
            return False                                  # -delete, -exec, -execdir, -ok, -fprint, -fls ... are refused
    return True


def _command_routine(tok: List[_Tok], cwd: str, ctx: PolicyContext) -> bool:
    """One simple command (already structurally checked) against the closed allowlist."""
    if not tok or any(t.text == "--%" for t in tok):
        return False
    head = tok[0].text.lower()
    head = head[:-4] if head.endswith(".exe") else head
    args = tok[1:]
    texts = [a.text for a in args]
    if head == "rtk":                                     # only pass-through wrappers of allowlisted commands
        if not args or args[0].text.lower() not in RTK_PASSTHROUGH:
            return False                                  # rtk test/err/summary/run/proxy/bypass ... execute anything
        return _command_routine(list(args), cwd, ctx)
    if any(_sensitive_name(t) for t in texts if t and not t.startswith("-")):
        return False                                      # secrets and launchers are never named by a worker shell
    if head == "git":
        return _git_routine(args, cwd, ctx)
    if head == "gh":
        return _gh_routine(args, cwd, ctx)
    if head in ("python", "python3", "py"):
        return _python_routine(args, cwd, ctx)
    if head == "pwd":
        return all(t in ("-P", "-L") for t in texts)
    if head in ("echo", "printf", "true"):
        return True
    if head in ("ls", "dir"):
        return _paths_ok(_parse(args, _LS, cwd, ctx), cwd, ctx)
    if head in ("cat", "type"):
        return _paths_ok(_parse(args, _CAT, cwd, ctx), cwd, ctx)
    if head in ("head", "tail"):
        return _paths_ok(_parse(args, _HEAD, cwd, ctx), cwd, ctx)
    if head == "wc":
        return _paths_ok(_parse(args, _WC, cwd, ctx), cwd, ctx)
    if head == "grep":
        return _pattern_then_paths(args, _GREP, cwd, ctx)
    if head == "rg":
        return _pattern_then_paths(args, _RG, cwd, ctx)
    if head == "find":
        return _find_routine(args, cwd, ctx)
    if head == "sort":
        return _paths_ok(_parse(args, _SORT, cwd, ctx), cwd, ctx)
    if head == "uniq":
        pos = _parse(args, _UNIQ, cwd, ctx)
        return pos is not None and len(pos) <= 1 and _paths_ok(pos, cwd, ctx)   # a second operand is an output file
    if head == "diff":
        pos = _parse(args, _DIFF, cwd, ctx)
        return pos is not None and len(pos) == 2 and _paths_ok(pos, cwd, ctx)
    if head == "mkdir":
        pos = _parse(args, _MKDIR, cwd, ctx)
        return bool(pos) and _paths_ok(pos, cwd, ctx, write=True)
    if head == "test":
        return all(t in _TEST_OPS if t.startswith("-") or t in _TEST_OPS else _path_ok(a, cwd, ctx)
                   for a, t in zip(args, texts))
    if head == "sed":
        seen: Dict[str, List[str]] = {}
        pos = _parse(args, _SED, cwd, ctx, seen)
        if pos is None or not any(k in seen for k in ("-n", "-nE", "-En", "-nr", "-rn", "--quiet", "--silent")):
            return False
        scripts = seen.get("-e", [])
        if not scripts:
            if not pos:
                return False
            scripts, pos = [pos[0].text], pos[1:]
        return all(_SED_SCRIPT.match(s) for s in scripts) and _paths_ok(pos, cwd, ctx)
    return False


def routine_shell(command: str, ctx: PolicyContext = PolicyContext(), cwd: str = "",
                  tool: str = "Bash") -> Tuple[bool, str]:
    """(True, "") when the call satisfies the STRUCTURAL worker rule (see the section comment); else (False, why)."""
    if tool != "Bash":
        return False, f"the {tool or '(unknown)'} tool is never allowed for a worker; use Bash"
    text = command or ""
    if text.endswith(" 2>&1"):
        text = text[:-len(" 2>&1")]
    if not text.strip():
        return False, "(empty command)"
    m = _SHELL_META.search(text)
    if m:
        return False, f"not one simple command (character {m.group(0)!r}): {text.strip()[:120]}"
    tok = simple_tokens(text)
    if not tok:
        return False, f"does not tokenise cleanly: {text.strip()[:120]}"
    start = (cwd or ctx.repo_root or "").replace("\\", "/")
    if not within_roots(start, ctx):
        return False, f"working directory {start or '(unknown)'} is outside the environment roots"
    if not _command_routine(tok, _norm(start), ctx):
        return False, text.strip()[:120]
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
                         approvals_dir=env.get(APPROVALS_ENV, ""), gh_repo=env.get(GH_REPO_ENV, "").strip(),
                         boundary_paths=tuple(p for p in env.get(BOUNDARY_ENV, "").split(os.pathsep) if p))


def _out(decision: str, reason: str) -> Dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def hook_output(decision: Decision) -> Optional[Dict[str, Any]]:
    """Advisory rendering of a bare decision (no approval queue): silence for ALLOW, else deny/ask."""
    if decision.allowed:
        return None
    return _out("deny" if decision.verdict == DENY else "ask", f"authority policy [{decision.rule}]: {decision.reason}")


def respond(tool: str, tool_input: Dict[str, Any], decision: Decision, ctx: PolicyContext, mode: str,
            session_id: str = "", role: str = "", cwd: str = "") -> Optional[Dict[str, Any]]:
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
    if resolve_input_dependent(tool, tool_input):
        return _out("deny", "authority policy [resolve-input-dependent]: this Resolve tool is a read only according to "
                            "a caller-supplied field; workers get no allow for it")
    if tool in SHELL_TOOLS:
        ok, seg = routine_shell(str(tool_input.get("command", "")), ctx, cwd, tool)
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
        res = respond(tool, tin, decision, ctx, mode, str(payload.get("session_id") or ""), env.get(ROLE_ENV, ""), cwd)
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


def default_approvals_dir(env: Dict[str, str], config_file: Optional[str] = None) -> str:
    """``<supervisor state dir>/approvals``, the state directory coming from the same configuration loading as the
    dev_supervisor CLI (``--config``, ``CREATORSTUDIO_DEV_SUPERVISOR_CONFIG`` or ``<state dir>/config.json``)."""
    try:
        from . import config as config_mod
    except ImportError:                                   # run as a plain script: the package is not importable
        from dev_supervisor import config as config_mod
    cfg = config_mod.load(config_file, env)
    return str(cfg.state_directory(env) / "approvals")


def _cmd_approvals(a: Any, out: TextIO, env: Dict[str, str], isatty: bool,
                   ask: Callable[[str], str]) -> int:
    d = a.dir or env.get(APPROVALS_ENV, "")
    if not d:
        try:
            d = default_approvals_dir(env, getattr(a, "config", None))
        except Exception as exc:  # noqa: BLE001 - a bad config must give a message, never a traceback
            out.write(f"approvals directory unknown ({exc}): pass --dir <state>/approvals or set {APPROVALS_ENV}\n")
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
    ap_.add_argument("--dir", default="", help="<state>/approvals (default: $" + APPROVALS_ENV +
                                               ", else <supervisor state dir>/approvals)")
    ap_.add_argument("--config", default=None, help="machine-local supervisor config used to find the state directory")
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
