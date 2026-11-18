"""Autonomous merge gate for the development supervisor (T-DEV-021, issue #74, parent issue #44).

``python -B -m dev_supervisor.integrate <pr> [--yes] [--repo <root>] [--base main] [--state-dir <dir>]``

Evaluates EVERY gate for a pull request and prints a gate table. Dry run by default (reads only: no lock, no comment,
no event, no merge). With ``--yes`` and every gate PASS it takes the exclusive integration lock, re-evaluates all gates
under the lock, merges with ``gh pr merge <n> --merge --match-head-commit <evaluated head>`` (never ``--admin``, never
``--force``), reads the result back and records the outcome as one PR comment plus one supervisor event.

Gates: 1 pr_state, 2 checks, 3 merge_state, 4 scope, 5 review, 6 secrets, 7 forbidden_files. A gate is PASS, FAIL or
UNKNOWN; anything but PASS blocks the merge.

gh and git are injected runners (``gh(args, input) -> (rc, text)``, ``git(args) -> (rc, bytes)``) so unit tests never
touch the network. Standard library only; the governance package is imported read-only.

The review-marker parser and diff hash below are a minimal local version (PROPOSED: replace by dev_supervisor.review
once lane L2 of wave w07 is merged).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = ["Gate", "Result", "IntegrationLock", "LockHeld", "evaluate", "integrate", "parse_review_marker",
           "diff_hash", "render_table", "real_gh_runner", "real_git_runner", "main"]

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
REQUIRED_CHECK = "validate"
BRANCH_PREFIXES = ("task/", "wave/", "chatgpt/")
REGISTRY_FILES = ("ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json", "SESSION_HANDOFF.md", "CHANGELOG.md")
LOCK_NAME = "integration.lock"
DEFAULT_LOCK_TIMEOUT = 900.0

_REVIEW_RE = re.compile(r"^<!-- dev-review v1 pr=(\d+) diff_sha256=([0-9a-f]{16}) "
                        r"verdict=(APPROVE|CHANGES_REQUESTED) reviewer=([A-Za-z0-9:._@-]{1,80}) -->$")
_COAUTHOR_RE = re.compile(r"(?im)^co-authored-by:\s*(.+?)\s*<([^>]*)>")
_TRUSTED_ASSOC = ("OWNER", "MEMBER", "COLLABORATOR")
_PENDING = ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED")
_BAD = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE")
_FORBIDDEN_NAMES = ("pro.ps1", "backup.ps1", ".mcp.json")
_FORBIDDEN_DIRS = ("incoming/", "projects/")
_CRED_NAME_RE = re.compile(r"(?i)(^|/)(\.env(\..*)?|id_rsa.*|id_ed25519.*|.*\.(pem|key|p12|pfx|keystore)|"
                           r"credentials?(\..*)?|secrets?(\..*)?|.*token.*\.(txt|json))$")
_MEDIA_RE = re.compile(r"(?i)\.(mp4|mov|mkv|avi|mxf|m4v|webm|mts|braw|r3d|arw|wav|mp3|flac|aac|m4a|aif|aiff|prproj)$")
_FALLBACK_SECRET = re.compile(r"((^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
                              r"|BEGIN [A-Z ]*PRIVATE KEY)")


# ------------------------------------------------------------------ model

@dataclass
class Gate:
    num: int
    name: str
    status: str
    detail: str = ""


@dataclass
class Result:
    pr: int
    head: str = ""
    gates: List[Gate] = field(default_factory=list)
    outcome: str = "DRY_RUN"        # DRY_RUN | MERGED | REFUSED | LOCKED | ERROR
    note: str = ""
    diff_sha: str = ""

    @property
    def all_pass(self) -> bool:
        return bool(self.gates) and all(g.status == PASS for g in self.gates)

    @property
    def exit_code(self) -> int:
        if self.outcome == "MERGED" or (self.outcome == "DRY_RUN" and self.all_pass):
            return 0
        return {"DRY_RUN": 1, "REFUSED": 1, "LOCKED": 2, "ERROR": 2}.get(self.outcome, 3)


def _clean(text: Any, limit: int = 220) -> str:
    """Single-line, credential- and path-scrubbed text for tables and comments."""
    try:
        from dev_supervisor.github_log import scrub
        s = scrub(text, limit)
    except Exception:  # pragma: no cover - defensive
        s = str(text)[:limit]
    return s.replace("\n", " ").replace("|", "/")


# ------------------------------------------------------------------ review marker and diff hash (minimal, local)

def parse_review_marker(first_line: str) -> Optional[Dict[str, Any]]:
    """Strict parse of the first line of a review comment; None if it is not exactly a dev-review v1 marker."""
    m = _REVIEW_RE.match((first_line or "").strip("\r\n "))
    if not m:
        return None
    return {"pr": int(m.group(1)), "diff_sha256": m.group(2), "verdict": m.group(3), "reviewer": m.group(4)}


def diff_hash(git: Callable[[Sequence[str]], Tuple[int, bytes]], base: str, head: str) -> Optional[str]:
    """First 16 hex of sha256 over `git diff --binary <merge-base> <head>` (refs are origin/<branch>). None on failure."""
    rc, mb = git(["merge-base", base, head])
    sha = mb.decode("utf-8", "replace").strip()
    if rc != 0 or not sha:
        return None
    rc, data = git(["diff", "--binary", sha, head])
    if rc != 0:
        return None
    return hashlib.sha256(data).hexdigest()[:16]


# ------------------------------------------------------------------ runners

def real_gh_runner(root: Path, timeout: float = 60.0) -> Callable:
    def run(args: Sequence[str], input: Optional[str] = None) -> Tuple[int, str]:
        try:
            p = subprocess.run(["gh", *args], cwd=str(root), input=input, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, shell=False)
        except (OSError, subprocess.SubprocessError):
            return 127, ""
        return p.returncode, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout or ""))
    return run


def real_git_runner(root: Path, timeout: float = 120.0) -> Callable:
    def run(args: Sequence[str]) -> Tuple[int, bytes]:
        try:
            p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout, shell=False)
        except (OSError, subprocess.SubprocessError):
            return 127, b""
        return p.returncode, p.stdout
    return run


# ------------------------------------------------------------------ lock

class LockHeld(Exception):
    pass


class IntegrationLock:
    """Exclusive lock file (atomic O_EXCL create) holding pid/time/token. Stale after `timeout` seconds
    (or when `pid_alive` says the holder is gone). Known limit: two processes that both judge the SAME stale lock stale
    at the same instant can race on the takeover; the O_EXCL create still lets only one of them hold it."""

    def __init__(self, directory: os.PathLike, timeout: float = DEFAULT_LOCK_TIMEOUT,
                 clock: Callable[[], float] = time.time, pid_alive: Optional[Callable[[int], bool]] = None):
        self.path = Path(directory) / LOCK_NAME
        self.timeout, self.clock, self.pid_alive = timeout, clock, pid_alive
        self.token = ""

    def _read(self) -> Optional[Dict[str, Any]]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {}

    def _stale(self, d: Dict[str, Any]) -> bool:
        t = d.get("time")
        if not isinstance(t, (int, float)) or self.clock() - t > self.timeout:
            return True
        pid = d.get("pid")
        return bool(self.pid_alive and isinstance(pid, int) and not self.pid_alive(pid))

    def acquire(self, pr: int = 0) -> None:
        os.makedirs(str(self.path.parent), exist_ok=True)
        for _ in range(2):
            token = uuid.uuid4().hex
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                d = self._read()
                if d is None:
                    continue
                if not self._stale(d):
                    raise LockHeld(f"integration lock held by pid {d.get('pid')} since {d.get('time')}")
                try:
                    os.unlink(str(self.path))
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "time": self.clock(), "pr": pr, "token": token}, f)
                f.flush()
                os.fsync(f.fileno())
            self.token = token
            return
        raise LockHeld("could not take the integration lock")

    def release(self) -> None:
        d = self._read()
        if self.token and d and d.get("token") == self.token:
            try:
                os.unlink(str(self.path))
            except OSError:
                pass
        self.token = ""


# ------------------------------------------------------------------ gh / git helpers

def _json_stream(text: str) -> List[Any]:
    """Parse one JSON value or several concatenated ones (gh --paginate prints one array per page)."""
    dec, i, out, text = json.JSONDecoder(), 0, [], text.strip()
    while i < len(text):
        v, j = dec.raw_decode(text, i)
        out.append(v)
        i = j
        while i < len(text) and text[i].isspace():
            i += 1
    return out


def _pr_view(gh: Callable, pr: int) -> Dict[str, Any]:
    rc, out = gh(["pr", "view", str(pr), "--json", "number,state,isDraft,baseRefName,headRefName,headRefOid,"
                  "mergeStateStatus,statusCheckRollup,files,commits"], None)
    if rc != 0:
        raise RuntimeError(f"gh pr view failed (rc {rc})")
    d = json.loads(out)
    if not isinstance(d, dict):
        raise RuntimeError("unexpected gh pr view output")
    return d


def _comments(gh: Callable, pr: int) -> List[Dict[str, Any]]:
    rc, out = gh(["api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{pr}/comments"], None)
    if rc != 0:
        raise RuntimeError(f"gh api comments failed (rc {rc})")
    items: List[Dict[str, Any]] = []
    for page in _json_stream(out or "[]"):
        items.extend(c for c in (page if isinstance(page, list) else [page]) if isinstance(c, dict))
    return items


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


@dataclass
class _Ctx:
    pr: Dict[str, Any]
    base: str
    files: List[str] = field(default_factory=list)
    added_files: List[str] = field(default_factory=list)
    added_lines: List[str] = field(default_factory=list)
    registry: Optional[Dict[str, Any]] = None
    git_error: str = ""
    diff_sha: str = ""
    head_oid: str = ""


def _collect_git(git: Callable, pr: Dict[str, Any], base: str, ctx: _Ctx) -> None:
    head = pr.get("headRefName", "")
    rc, _ = git(["fetch", "--quiet", "origin"])
    if rc != 0:
        ctx.git_error = "git fetch origin failed"
        return
    rc, out = git(["rev-parse", f"origin/{head}"])
    sha = out.decode("utf-8", "replace").strip()
    if rc != 0 or sha != pr.get("headRefOid"):
        ctx.git_error = "origin/<head> does not match the PR head commit"
        return
    rc, mb = git(["merge-base", f"origin/{base}", f"origin/{head}"])
    mbs = mb.decode("utf-8", "replace").strip()
    if rc != 0 or not mbs:
        ctx.git_error = "no merge-base"
        return
    ctx.diff_sha = diff_hash(git, f"origin/{base}", f"origin/{head}") or ""
    if not ctx.diff_sha:
        ctx.git_error = "diff hash failed"
        return
    rc, out = git(["diff", "--name-only", "--no-renames", "-z", mbs, f"origin/{head}"])
    if rc != 0:
        ctx.git_error = "git diff --name-only failed"
        return
    ctx.files = [p for p in out.decode("utf-8", "replace").split("\0") if p]
    rc, out = git(["diff", "--name-only", "--no-renames", "--diff-filter=AM", "-z", mbs, f"origin/{head}"])
    if rc != 0:
        ctx.git_error = "git diff --diff-filter failed"
        return
    ctx.added_files = [p for p in out.decode("utf-8", "replace").split("\0") if p]
    rc, out = git(["diff", "-U0", "--no-renames", mbs, f"origin/{head}"])
    if rc != 0:
        ctx.git_error = "git diff -U0 failed"
        return
    ctx.added_lines = [l[1:] for l in out.decode("utf-8", "replace").splitlines()
                       if l.startswith("+") and not l.startswith("+++")]
    rc, out = git(["show", f"origin/{base}:ACTIVE_TASKS.json"])
    if rc == 0:
        try:
            doc = json.loads(out.decode("utf-8"))
            ctx.registry = doc if isinstance(doc, dict) else None
        except ValueError:
            ctx.registry = None


# ------------------------------------------------------------------ gates

def _gate_pr_state(c: _Ctx) -> Gate:
    p, bad = c.pr, []
    if p.get("state") != "OPEN":
        bad.append(f"state is {p.get('state')}")
    if p.get("isDraft") is not False:
        bad.append("draft" if p.get("isDraft") else "draft flag missing")
    if p.get("baseRefName") != c.base:
        bad.append(f"base is {p.get('baseRefName')}, need {c.base}")
    if not str(p.get("headRefName", "")).startswith(BRANCH_PREFIXES):
        bad.append(f"head branch prefix not in {list(BRANCH_PREFIXES)}")
    return Gate(1, "pr_state", FAIL if bad else PASS, "; ".join(bad) or f"open, not draft, base {c.base}, head ok")


def _gate_checks(c: _Ctx) -> Gate:
    rollup = c.pr.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return Gate(2, "checks", UNKNOWN, "no statusCheckRollup")
    found, problems = None, []
    for e in rollup:
        if not isinstance(e, dict):
            continue
        name = e.get("name") or e.get("context") or ""
        status = str(e.get("status") or "").upper()
        concl = str(e.get("conclusion") or e.get("state") or "").upper()
        done = status in ("", "COMPLETED") and concl not in _PENDING and concl != ""
        if name == REQUIRED_CHECK:
            found = (done, concl)
        if not done or concl in _PENDING:
            problems.append(f"{name or '?'} pending")
        elif concl in _BAD:
            problems.append(f"{name or '?'} {concl}")
    if found is None:
        return Gate(2, "checks", FAIL, f"required check {REQUIRED_CHECK!r} not reported on head {c.pr.get('headRefOid', '')[:12]}")
    if found[1] != "SUCCESS" or not found[0]:
        return Gate(2, "checks", FAIL, f"{REQUIRED_CHECK} is {found[1] or 'pending'}")
    if problems:
        return Gate(2, "checks", FAIL, "; ".join(problems))
    return Gate(2, "checks", PASS, f"{REQUIRED_CHECK} SUCCESS on head {c.pr.get('headRefOid', '')[:12]}")


def _gate_merge_state(c: _Ctx) -> Gate:
    s = str(c.pr.get("mergeStateStatus") or "").upper()
    if s == "CLEAN":
        return Gate(3, "merge_state", PASS, "CLEAN")
    if s in ("", "UNKNOWN"):
        return Gate(3, "merge_state", UNKNOWN, "mergeStateStatus not computed yet")
    return Gate(3, "merge_state", FAIL, s)


def _wave_scope(c: _Ctx, git: Callable, head: str) -> Tuple[List[str], List[str], str]:
    """(allowed path prefixes, lane owners, error) for a wave/<id> branch from its manifest (base first, then head)."""
    wid = head[len("wave/"):]
    doc = None
    for ref in (f"origin/{c.base}", f"origin/{head}"):
        rc, out = git(["show", f"{ref}:waves/{wid}/wave.json"])
        if rc == 0:
            try:
                doc = json.loads(out.decode("utf-8"))
                break
            except ValueError:
                pass
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), list):
        return [], [], f"wave manifest for {wid} not readable"
    allowed = [f"waves/{wid}/", *REGISTRY_FILES]
    owners = []
    for lane in doc["lanes"]:
        if isinstance(lane, dict):
            allowed.extend(p for p in lane.get("owned_paths", []) if isinstance(p, str))
            if lane.get("owner"):
                owners.append(str(lane["owner"]))
    return allowed, owners, ""


def _gate_scope(c: _Ctx, git: Callable, owners_out: List[str]) -> Gate:
    if not c.files:
        return Gate(4, "scope", FAIL, "no changed files")
    if all(f in REGISTRY_FILES for f in c.files):
        return Gate(4, "scope", PASS, "registry-only PR, passes as a record")
    head = c.pr.get("headRefName", "")
    from governance import tasks
    if head.startswith("wave/"):
        allowed, owners, err = _wave_scope(c, git, head)
        if err:
            return Gate(4, "scope", UNKNOWN, err)
        owners_out.extend(owners)
        out = [f for f in c.files if not any(tasks.covers(a, f) for a in allowed)]
        return Gate(4, "scope", FAIL if out else PASS,
                    (f"{len(out)} file(s) outside wave lanes' owned paths: " + ", ".join(out[:5])) if out
                    else f"{len(c.files)} file(s) inside the wave's lane paths")
    if c.registry is None:
        return Gate(4, "scope", UNKNOWN, f"ACTIVE_TASKS.json on origin/{c.base} not readable")
    probs = tasks.scope_problems(c.registry, head, c.files)
    for t in c.registry.get("tasks", []):
        if t.get("branch") == head and t.get("status") in tasks.ACTIVE_STATUSES and t.get("owner"):
            owners_out.append(str(t["owner"]))
    return Gate(4, "scope", FAIL if probs else PASS, "; ".join(probs[:3]) if probs
                else f"{len(c.files)} file(s) inside the owned_paths of the registered task")


def _author_ids(pr: Dict[str, Any], owners: List[str]) -> set:
    ids = {_norm(o) for o in owners if o}
    for cm in pr.get("commits") or []:
        if not isinstance(cm, dict):
            continue
        for a in cm.get("authors") or []:
            if isinstance(a, dict):
                ids.update(_norm(a.get(k)) for k in ("login", "name", "email"))
        for name, email in _COAUTHOR_RE.findall(str(cm.get("messageBody") or "")):
            ids.update((_norm(name), _norm(email)))
    ids.discard("")
    return ids


def _gate_review(c: _Ctx, comments: Optional[List[Dict[str, Any]]], owners: List[str], n: int) -> Gate:
    if (c.files and all(f in REGISTRY_FILES for f in c.files)
            and not str(c.pr.get("headRefName", "")).startswith("wave/")):
        return Gate(5, "review", PASS, "registry-only record: no code review required (protocol section 18 gate 4)")
    if comments is None:
        return Gate(5, "review", UNKNOWN, "comments not readable")
    if not c.diff_sha:
        return Gate(5, "review", UNKNOWN, "diff hash not available")
    authors = _author_ids(c.pr, owners)
    latest: Dict[str, Tuple[Tuple[str, int], str]] = {}
    stale = 0
    for cm in comments:
        if cm.get("author_association") not in _TRUSTED_ASSOC:
            continue
        m = parse_review_marker(str(cm.get("body") or "").split("\n", 1)[0])
        if not m or m["pr"] != n:
            continue
        if m["diff_sha256"] != c.diff_sha:
            stale += 1
            continue
        key = (str(cm.get("created_at") or ""), int(cm.get("id") or 0))
        cur = latest.get(m["reviewer"])
        if cur is None or key > cur[0]:
            latest[m["reviewer"]] = (key, m["verdict"])
    blockers = sorted(r for r, (_, v) in latest.items() if v == "CHANGES_REQUESTED")
    if blockers:
        return Gate(5, "review", FAIL, f"CHANGES_REQUESTED by {', '.join(blockers)} for diff {c.diff_sha}")
    ok = [r for r, (_, v) in latest.items() if v == "APPROVE" and _norm(r) not in authors]
    if ok:
        return Gate(5, "review", PASS, f"APPROVE by {ok[0]} for diff {c.diff_sha}")
    if any(v == "APPROVE" for _, v in latest.values()):
        return Gate(5, "review", FAIL, "the only APPROVE is from a reviewer recorded as an author or the task owner")
    return Gate(5, "review", FAIL, f"no dev-review APPROVE for diff {c.diff_sha}" +
                (f" ({stale} marker(s) for an older diff ignored)" if stale else ""))


def _secret_hits(lines: Sequence[str]) -> int:
    try:
        from governance import waves
        return len(waves.secret_problems(lines))
    except Exception:
        return sum(1 for l in lines if _FALLBACK_SECRET.search(l))


def _gate_secrets(c: _Ctx) -> Gate:
    n = _secret_hits(c.added_lines)
    return Gate(6, "secrets", FAIL if n else PASS,
                f"{n} added line(s) match credential patterns" if n else f"{len(c.added_lines)} added line(s) scanned, none match")


def _gate_forbidden(c: _Ctx) -> Gate:
    bad = []
    for f in c.added_files:
        low = f.lower()
        if (low.rsplit("/", 1)[-1] in _FORBIDDEN_NAMES or low.startswith(_FORBIDDEN_DIRS)
                or _CRED_NAME_RE.search(f) or _MEDIA_RE.search(f)):
            bad.append(f)
    return Gate(7, "forbidden_files", FAIL if bad else PASS,
                ("launcher/credential/media file(s): " + ", ".join(bad[:5])) if bad else "none added or modified")


def evaluate(pr: int, gh: Callable, git: Callable, base: str = "main") -> Tuple[List[Gate], Dict[str, Any]]:
    """Evaluate all seven gates. Never raises: any failure turns the affected gate into UNKNOWN."""
    info: Dict[str, Any] = {"head": "", "diff_sha": "", "comments": None}
    try:
        view = _pr_view(gh, pr)
    except Exception as e:
        return [Gate(i, nm, UNKNOWN, _clean(f"cannot read PR: {e}")) for i, nm in enumerate(
            ("pr_state", "checks", "merge_state", "scope", "review", "secrets", "forbidden_files"), 1)], info
    ctx = _Ctx(pr=view, base=base)
    info["head"] = str(view.get("headRefOid") or "")
    gates: List[Gate] = []

    def safe(num: int, name: str, fn: Callable[[], Gate]) -> Gate:
        try:
            return fn()
        except Exception as e:
            return Gate(num, name, UNKNOWN, _clean(f"{type(e).__name__}: {e}"))

    gates.append(safe(1, "pr_state", lambda: _gate_pr_state(ctx)))
    gates.append(safe(2, "checks", lambda: _gate_checks(ctx)))
    gates.append(safe(3, "merge_state", lambda: _gate_merge_state(ctx)))
    try:
        _collect_git(git, view, base, ctx)
    except Exception as e:
        ctx.git_error = _clean(f"{type(e).__name__}: {e}")
    info["diff_sha"] = ctx.diff_sha
    owners: List[str] = []
    if ctx.git_error:
        for num, name in ((4, "scope"), (5, "review"), (6, "secrets"), (7, "forbidden_files")):
            gates.append(Gate(num, name, UNKNOWN, ctx.git_error))
        return gates, info
    gates.append(safe(4, "scope", lambda: _gate_scope(ctx, git, owners)))
    try:
        comments: Optional[List[Dict[str, Any]]] = _comments(gh, pr)
    except Exception:
        comments = None
    info["comments"] = comments
    gates.append(safe(5, "review", lambda: _gate_review(ctx, comments, owners, pr)))
    gates.append(safe(6, "secrets", lambda: _gate_secrets(ctx)))
    gates.append(safe(7, "forbidden_files", lambda: _gate_forbidden(ctx)))
    return gates, info


def render_table(gates: Sequence[Gate]) -> str:
    rows = [f"| {g.num} | {g.name} | {g.status} | {_clean(g.detail)} |" for g in gates]
    return "\n".join(["| # | gate | status | detail |", "|---|---|---|---|", *rows])


# ------------------------------------------------------------------ orchestration

def _marker(pr: int, head: str, result: str) -> str:
    return f"<!-- dev-integrate v1 pr={pr} head={head} result={result} -->"


def _post_comment(gh: Callable, res: Result, comments: Optional[List[Dict[str, Any]]]) -> bool:
    result = "MERGED" if res.outcome == "MERGED" else "REFUSED"
    marker = _marker(res.pr, res.head, result)
    if any(str(c.get("body") or "").startswith(marker) for c in (comments or [])) and result == "REFUSED" \
            and not res.note.startswith("merge"):
        return False  # identical refusal already recorded for this head
    body = "\n".join([marker, f"**dev-integrate** PR #{res.pr}: **{result}**" +
                      (f" ({_clean(res.note)})" if res.note else ""), "", render_table(res.gates)])
    rc, _ = gh(["pr", "comment", str(res.pr), "--body-file", "-"], body)
    return rc == 0


def _record_event(store: Any, res: Result) -> None:
    if store is not None:
        store.append_event("integrate", pr=res.pr, head=res.head, result=res.outcome, note=_clean(res.note, 160),
                           gates={g.name: g.status for g in res.gates})


def integrate(pr: int, *, yes: bool = False, base: str = "main", gh: Callable, git: Callable,
              state_dir: Optional[os.PathLike] = None, lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
              clock: Callable[[], float] = time.time) -> Result:
    gates, info = evaluate(pr, gh, git, base)
    res = Result(pr=pr, head=info["head"], gates=gates, diff_sha=info["diff_sha"])
    if not yes:
        res.note = "dry run: nothing changed"
        return res
    from dev_supervisor.state_store import StateStore
    store = StateStore(state_dir, clock=clock)
    if not res.all_pass or not res.head:
        res.outcome, res.note = "REFUSED", "not all gates PASS"
        _safe(lambda: _post_comment(gh, res, info["comments"]))
        _record_event(store, res)
        return res
    lock = IntegrationLock(store.directory, lock_timeout, clock)
    try:
        lock.acquire(pr)
    except LockHeld as e:
        res.outcome, res.note = "LOCKED", _clean(e)
        return res
    try:
        # final gate evaluation under the lock
        gates, info = evaluate(pr, gh, git, base)
        res.gates, res.diff_sha = gates, info["diff_sha"]
        if not res.all_pass or info["head"] != res.head:
            res.outcome = "REFUSED"
            res.note = "head moved before merge" if info["head"] != res.head else "gates changed before merge"
            res.head = info["head"] or res.head
        else:
            _merge(pr, res, gh, base)
        _safe(lambda: _post_comment(gh, res, info["comments"]))
        _record_event(store, res)
        return res
    finally:
        lock.release()


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:
        return None


def _read_back(gh: Callable, pr: int) -> Tuple[bool, str]:
    rc, out = gh(["pr", "view", str(pr), "--json", "state,mergeCommit"], None)
    if rc != 0:
        return False, "read-back failed"
    try:
        d = json.loads(out)
    except ValueError:
        return False, "read-back unparseable"
    mc = d.get("mergeCommit") or {}
    if d.get("state") == "MERGED":
        return True, str(mc.get("oid", ""))[:12] if isinstance(mc, dict) else ""
    return False, f"state after merge is {d.get('state')}"


def _merge(pr: int, res: Result, gh: Callable, base: str) -> None:
    """Merge once; at most ONE retry, and only after confirming the PR is not merged and the head is unchanged."""
    cmd = ["pr", "merge", str(pr), "--merge", "--match-head-commit", res.head]
    err = ""
    for attempt in range(2):
        rc, out = gh(cmd, None)
        if rc == 0:
            ok, detail = _read_back(gh, pr)
            res.outcome = "MERGED" if ok else "REFUSED"
            res.note = f"merge commit {detail}" if ok else f"merge unconfirmed: {detail}"
            return
        err = _clean(out, 120)
        if attempt == 0:
            ok, _ = _read_back(gh, pr)
            if ok:
                res.outcome, res.note = "MERGED", "merged (confirmed after reported failure)"
                return
            try:
                view = _pr_view(gh, pr)
            except Exception:
                break
            if view.get("headRefOid") != res.head or view.get("state") != "OPEN":
                break
    res.outcome, res.note = "REFUSED", f"merge failed: {err}"


# ------------------------------------------------------------------ CLI

def main(argv: Optional[Sequence[str]] = None, *, gh: Optional[Callable] = None, git: Optional[Callable] = None,
         out: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="dev_supervisor.integrate", description=__doc__.split("\n\n")[0])
    ap.add_argument("pr", type=int)
    ap.add_argument("--yes", action="store_true", help="merge when every gate passes (default: dry run)")
    ap.add_argument("--repo", default=".", help="repository root (default: current directory)")
    ap.add_argument("--base", default="main")
    ap.add_argument("--state-dir", default=None)
    a = ap.parse_args(argv)
    out = out or sys.stdout
    root = Path(a.repo)
    res = integrate(a.pr, yes=a.yes, base=a.base, gh=gh or real_gh_runner(root), git=git or real_git_runner(root),
                    state_dir=a.state_dir)
    print(f"PR #{res.pr} head {res.head[:12] or '?'} diff {res.diff_sha or '?'}", file=out)
    print(render_table(res.gates), file=out)
    print(f"result: {res.outcome}" + (f" ({_clean(res.note)})" if res.note else ""), file=out)
    return res.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
