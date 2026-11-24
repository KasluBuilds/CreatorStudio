"""Autonomous merge gate for the development supervisor (T-DEV-021, issue #74, parent issue #44).

``python -B -m dev_supervisor.integrate <pr> [--yes] [--repo <root>] [--base main] [--state-dir <dir>]``

Evaluates EVERY gate for a pull request and prints a gate table. Dry run by default (reads only: no lock, no comment,
no event, no merge). With ``--yes`` and every gate PASS it takes the exclusive integration lock, re-evaluates all gates
under the lock, merges with ``gh pr merge <n> --merge --match-head-commit <evaluated head>`` (never ``--admin``, never
``--force``), reads the result back and records the outcome as one PR comment plus one supervisor event.

Gates: 1 pr_state, 2 checks, 3 merge_state, 4 scope, 5 review, 6 secrets, 7 forbidden_files, 8 reserved. A gate is PASS, FAIL or
UNKNOWN; anything but PASS blocks the merge.

gh and git are injected runners (``gh(args, input) -> (rc, text)``, ``git(args) -> (rc, bytes)``) so unit tests never
touch the network. Standard library only; the governance package is imported read-only.

The review-marker parser and diff hash come from dev_supervisor.review (lane L2). The review VERDICT logic stays here
because it differs on purpose: only OWNER/MEMBER/COLLABORATOR comments count, the author set is richer than the PR
author login (commit authors, Co-Authored-By lines, task owners), and ANY reviewer's current CHANGES_REQUESTED blocks
(review.review_status lets the latest valid review win, so a later APPROVE would hide a standing objection).
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

from dev_supervisor import review as _review

__all__ = ["Gate", "Result", "IntegrationLock", "LockHeld", "evaluate", "preflight", "integrate", "parse_review_marker",
           "diff_hash", "render_table", "real_gh_runner", "real_git_runner", "main"]

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
REQUIRED_CHECK = "validate"
BRANCH_PREFIXES = ("task/", "wave/", "chatgpt/")
REGISTRY_FILES = ("ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json", "SESSION_HANDOFF.md", "CHANGELOG.md")
LOCK_NAME = "integration.lock"
DEFAULT_LOCK_TIMEOUT = 900.0

_COAUTHOR_RE = re.compile(r"(?im)^co-authored-by:\s*(.+?)\s*<([^>]*)>")
_TRUSTED_ASSOC = ("OWNER", "MEMBER", "COLLABORATOR")
_PENDING = ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED")
_BAD = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE")
_FORBIDDEN_NAMES = ("pro.ps1", "backup.ps1", ".mcp.json")
_RESERVED_FILES = ("PROJECT_OPERATING_PROTOCOL.md", "DEVELOPMENT_STEP_PROTOCOL.md", "WAVE_PROTOCOL.md", "CLAUDE.md",
                   "dashboard/tracker_check.py")
_RESERVED_DIRS = (".github/", ".githooks/", "orchestrator/governance/")
_LUKAS_WORD_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(issues|pull)/\d+[^\s]*$")
_TID_RE = re.compile(r"T-[A-Z0-9]+-\d{3}")
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
    override: str = ""

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
    """Strict parse of the first line of a review comment (delegates to review.parse_marker); None if not a marker."""
    m = _review.parse_marker(first_line or "")
    if m is None:
        return None
    return {"pr": m.pr, "diff_sha256": m.diff_sha256, "verdict": m.verdict, "reviewer": m.reviewer}


def diff_hash(git: Callable[[Sequence[str]], Tuple[int, bytes]], base: str, head: str) -> Optional[str]:
    """First 16 hex of sha256 over `git diff --binary <merge-base> <head>` (refs are origin/<branch>). None on failure."""
    rc, mb = git(["merge-base", base, head])
    sha = mb.decode("utf-8", "replace").strip()
    if rc != 0 or not sha:
        return None
    rc, data = git(["diff", "--binary", sha, head])
    if rc != 0:
        return None
    return _review.diff_hash(data)


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


def _default_pid_alive(pid: int) -> bool:
    """True if a process with this pid exists (conservative: unknown means alive)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return k.GetLastError() == 5        # access denied: it exists
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return (not ok) or code.value == 259    # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class IntegrationLock:
    """Exclusive lock file: O_EXCL create (holder pid, time, token), verified by reading the token back.

    Staleness: the file mtime is the heartbeat (refreshed with `heartbeat()`). A readable lock whose holder pid is
    ALIVE is never stale; one whose pid is DEAD is stale. A lock with no readable holder (empty or torn: another
    process has just created it and not written yet) is held while its mtime is younger than `timeout`.
    Takeover renames the stale file to a unique name (atomic, one winner), checks that what was renamed is what was
    judged stale (otherwise a live lock is put back), then creates the new lock with O_EXCL."""

    def __init__(self, directory: os.PathLike, timeout: float = DEFAULT_LOCK_TIMEOUT,
                 clock: Callable[[], float] = time.time, pid_alive: Optional[Callable[[int], bool]] = None):
        self.path = Path(directory) / LOCK_NAME
        self.timeout, self.clock = timeout, clock
        self.pid_alive = pid_alive or _default_pid_alive
        self.token = ""

    def _raw(self) -> Optional[str]:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return ""

    @staticmethod
    def _parse(raw: str) -> Dict[str, Any]:
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def _age(self) -> float:
        try:
            return self.clock() - os.path.getmtime(str(self.path))
        except OSError:
            return 0.0

    def _stale(self, raw: str) -> bool:
        pid = self._parse(raw).get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool):
            return not self.pid_alive(pid)
        return self._age() > self.timeout

    def acquire(self, pr: int = 0) -> None:
        os.makedirs(str(self.path.parent), exist_ok=True)
        for _ in range(25):
            token = uuid.uuid4().hex
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raw = self._raw()
                if raw is None:
                    continue
                if not self._stale(raw):
                    d = self._parse(raw)
                    raise LockHeld(f"integration lock held (pid {d.get('pid', '?')})")
                aside = self.path.with_name(f"{LOCK_NAME}.stale.{os.getpid()}.{time.time_ns()}")
                try:
                    os.replace(str(self.path), str(aside))
                except FileNotFoundError:
                    continue
                except PermissionError:          # Windows: another process has it open (reading or taking over)
                    time.sleep(0.01)
                    continue
                try:
                    moved = aside.read_text(encoding="utf-8")
                except (OSError, ValueError):
                    moved = ""
                if moved != raw:                     # we moved somebody's fresh lock: put it back, stay out
                    try:
                        os.link(str(aside), str(self.path))
                    except OSError:
                        pass
                    try:
                        os.unlink(str(aside))
                    except OSError:
                        pass
                    raise LockHeld("integration lock was taken by another process during takeover")
                try:
                    os.unlink(str(aside))
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "time": self.clock(), "pr": pr, "token": token}, f)
                f.flush()
                os.fsync(f.fileno())
            if self._parse(self._raw() or "").get("token") != token:
                raise LockHeld("integration lock not confirmed after create")
            self.token = token
            self.heartbeat()
            return
        raise LockHeld("could not take the integration lock")

    def heartbeat(self) -> None:
        """Refresh the lock mtime; raises LockHeld if this process no longer owns the lock."""
        if not self.token or self._parse(self._raw() or "").get("token") != self.token:
            raise LockHeld("integration lock lost")
        now = self.clock()
        try:
            os.utime(str(self.path), (now, now))
        except OSError:
            pass

    def release(self) -> None:
        if self.token and self._parse(self._raw() or "").get("token") == self.token:
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
    mb: str = ""
    reg_cache: Dict[Any, Tuple[str, str]] = field(default_factory=dict)
    head_ref: str = ""   # the git ref of the head; "" means origin/<headRefName> (a pull request), "HEAD" is a preflight


def _head_ref(c: "_Ctx") -> str:
    return c.head_ref or f"origin/{c.pr.get('headRefName', '')}"


def _collect_git(git: Callable, pr: Dict[str, Any], base: str, ctx: _Ctx) -> None:
    head = pr.get("headRefName", "")
    rc, _ = git(["fetch", "--quiet", "origin"])
    if rc != 0:
        ctx.git_error = "git fetch origin failed"
        return
    ref = _head_ref(ctx)
    rc, out = git(["rev-parse", ref])
    sha = out.decode("utf-8", "replace").strip()
    if rc != 0 or sha != pr.get("headRefOid"):
        ctx.git_error = "origin/<head> does not match the PR head commit"
        return
    rc, mb = git(["merge-base", f"origin/{base}", ref])
    mbs = mb.decode("utf-8", "replace").strip()
    if rc != 0 or not mbs:
        ctx.git_error = "no merge-base"
        return
    ctx.mb = mbs
    ctx.diff_sha = diff_hash(git, f"origin/{base}", ref) or ""
    if not ctx.diff_sha:
        ctx.git_error = "diff hash failed"
        return
    rc, out = git(["diff", "--name-only", "--no-renames", "-z", mbs, ref])
    if rc != 0:
        ctx.git_error = "git diff --name-only failed"
        return
    ctx.files = [p for p in out.decode("utf-8", "replace").split("\0") if p]
    rc, out = git(["diff", "--name-only", "--no-renames", "--diff-filter=AM", "-z", mbs, ref])
    if rc != 0:
        ctx.git_error = "git diff --diff-filter failed"
        return
    ctx.added_files = [p for p in out.decode("utf-8", "replace").split("\0") if p]
    rc, out = git(["diff", "-U0", "--no-renames", mbs, ref])
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


def _strip_ids(o: Any, ids: set) -> Any:
    if isinstance(o, list):
        return [_strip_ids(x, ids) for x in o if not (isinstance(x, str) and x in ids)]
    if isinstance(o, dict):
        return {k: _strip_ids(v, ids) for k, v in o.items()}
    return o


def _tids(o: Any) -> set:
    return set(_TID_RE.findall(json.dumps(o)))


def registry_problems(base_active: Dict[str, Any], head_active: Dict[str, Any], base_track: Dict[str, Any],
                      head_track: Dict[str, Any], branch: str, wave_ids: Sequence[str] = ()) -> List[str]:
    """Structural comparison of the registries before/after a PR. Empty list = the PR only adds or updates the
    author's OWN task, step and agreement records (protocol section 18 gate 3). Pure."""
    probs: List[str] = []
    bt = {t.get("id"): t for t in base_active.get("tasks", []) if isinstance(t, dict)}
    ht = {t.get("id"): t for t in head_active.get("tasks", []) if isinstance(t, dict)}
    btr = {t.get("task_id"): t for t in base_track.get("tasks", []) if isinstance(t, dict)}
    htr = {t.get("task_id"): t for t in head_track.get("tasks", []) if isinstance(t, dict)}
    added = (set(ht) - set(bt)) | (set(htr) - set(btr))
    mine = {i for i, t in bt.items() if t.get("branch") == branch} | set(wave_ids)
    own = mine | added
    for k in (set(base_active) | set(head_active)) - {"tasks", "integration_agreements", "updated"}:
        if base_active.get(k) != head_active.get(k):
            probs.append(f"ACTIVE_TASKS.json {k} changed")
    for i in sorted(set(bt) - set(ht)):
        probs.append(f"task {i} removed from ACTIVE_TASKS.json")
    for i in sorted(set(bt) & set(ht)):
        if bt[i] != ht[i] and (i not in mine or bt[i].get("branch") != ht[i].get("branch")):
            probs.append(f"record of task {i} changed but it is not this PR's own task")
    ba, ha = base_active.get("integration_agreements") or {}, head_active.get("integration_agreements") or {}
    if not isinstance(ba, dict) or not isinstance(ha, dict):
        probs.append("integration_agreements has an unexpected shape")
    else:
        for a_id in sorted(set(ba) - set(ha)):
            probs.append(f"agreement {a_id} removed")
        for a_id in sorted(ha):
            if a_id not in ba:
                if not _tids(ha[a_id]) <= own:
                    probs.append(f"new agreement {a_id} lists tasks that are not this PR's own")
            elif ba[a_id] != ha[a_id]:
                x, y = ba[a_id], ha[a_id]
                same = (isinstance(x, dict) and isinstance(y, dict)
                        and _strip_ids({k: v for k, v in x.items() if k != "description"}, own)
                        == _strip_ids({k: v for k, v in y.items() if k != "description"}, own)
                        and str(y.get("description", "")).startswith(str(x.get("description", ""))))
                ok = same and _tids(x) <= _tids(y) and (_tids(y) - _tids(x)) <= own
                if not ok:
                    probs.append(f"existing agreement {a_id} changed beyond adding this PR's own task ids")
    for k in (set(base_track) | set(head_track)) - {"tasks"}:
        if base_track.get(k) != head_track.get(k):
            probs.append(f"DEVELOPMENT_TRACKER.json {k} changed")
    for i in sorted(set(btr) - set(htr)):
        probs.append(f"task {i} removed from DEVELOPMENT_TRACKER.json")
    for i in sorted(set(btr) & set(htr)):
        if btr[i] != htr[i] and i not in own:
            probs.append(f"tracker record of task {i} changed but it is not this PR's own task")
    return probs


def _registry_check(c: _Ctx, git: Callable, wave_ids: Sequence[str] = ()) -> Tuple[str, str]:
    """(PASS|FAIL|UNKNOWN, detail): do the registry/handoff/changelog changes only record the author's own work?"""
    key = tuple(wave_ids)
    if key in c.reg_cache:
        return c.reg_cache[key]
    head = c.pr.get("headRefName", "")

    def load(ref: str, name: str) -> Any:
        rc, out = git(["show", f"{ref}:{name}"])
        if rc != 0:
            raise ValueError(f"{name} not readable at {ref[:12]}")
        d = json.loads(out.decode("utf-8"))
        if not isinstance(d, dict):
            raise ValueError(f"{name} is not an object")
        return d

    try:
        hr = _head_ref(c)
        probs = registry_problems(load(c.mb, "ACTIVE_TASKS.json"), load(hr, "ACTIVE_TASKS.json"),
                                  load(c.mb, "DEVELOPMENT_TRACKER.json"),
                                  load(hr, "DEVELOPMENT_TRACKER.json"), head, wave_ids)
        if "CHANGELOG.md" in c.files:
            rc, out = git(["diff", "-U0", "--no-renames", c.mb, hr, "--", "CHANGELOG.md"])
            if rc != 0:
                raise ValueError("CHANGELOG.md diff failed")
            if any(l.startswith("-") and not l.startswith("---") for l in out.decode("utf-8", "replace").splitlines()):
                probs.append("CHANGELOG.md has removed or edited lines (append-only)")
        res = (FAIL, "; ".join(probs[:3])) if probs else (PASS, "only the author's own task/step/agreement records")
    except Exception as e:
        res = (UNKNOWN, _clean(f"registry not comparable: {e}"))
    c.reg_cache[key] = res
    return res


def _wave_scope(c: _Ctx, git: Callable, head: str) -> Tuple[List[str], List[str], List[str], str]:
    """(allowed paths, lane owners, lane task ids, error). The manifest comes from the base branch or from the FIRST
    commit of the wave branch after the merge-base (`wave init`); NEVER from the PR head."""
    wid = head[len("wave/"):]
    doc = None
    refs = [f"origin/{c.base}"]
    rc, out = git(["log", "--reverse", "--format=%H", f"{c.mb}..{_head_ref(c)}"])
    first = out.decode("utf-8", "replace").split() if rc == 0 else []
    if first:
        refs.append(first[0])
    for ref in refs:
        rc, out = git(["show", f"{ref}:waves/{wid}/wave.json"])
        if rc == 0:
            try:
                doc = json.loads(out.decode("utf-8"))
                break
            except ValueError:
                pass
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), list):
        return [], [], [], f"wave manifest for {wid} not readable from base or the wave's first commit"
    allowed = [f"waves/{wid}/"]
    owners, tids = [], []
    for lane in doc["lanes"]:
        if isinstance(lane, dict):
            allowed.extend(p for p in lane.get("owned_paths", []) if isinstance(p, str))
            if lane.get("owner"):
                owners.append(str(lane["owner"]))
            if lane.get("task_id"):
                tids.append(str(lane["task_id"]))
    return allowed, owners, tids, ""


def _reg_only(c: _Ctx) -> bool:
    return bool(c.files) and all(f in REGISTRY_FILES for f in c.files)


def _gate_scope(c: _Ctx, git: Callable, owners_out: List[str]) -> Gate:
    if not c.files:
        return Gate(4, "scope", FAIL, "no changed files")
    head = c.pr.get("headRefName", "")
    from governance import tasks
    regs = [f for f in c.files if f in REGISTRY_FILES]
    if head.startswith("wave/"):
        allowed, owners, tids, err = _wave_scope(c, git, head)
        if err:
            return Gate(4, "scope", UNKNOWN, err)
        owners_out.extend(owners)
        out = [f for f in c.files if f not in REGISTRY_FILES and not any(tasks.covers(a, f) for a in allowed)]
        if out:
            return Gate(4, "scope", FAIL, f"{len(out)} file(s) outside wave lanes' owned paths: " + ", ".join(out[:5]))
        if regs:
            st, detail = _registry_check(c, git, tids)
            if st != PASS:
                return Gate(4, "scope", st, "wave registry change: " + detail)
        return Gate(4, "scope", PASS, f"{len(c.files)} file(s) inside the wave's lane paths and own records")
    if _reg_only(c):
        st, detail = _registry_check(c, git)
        if st == PASS:
            return Gate(4, "scope", PASS, "registry-only record: " + detail)
        return Gate(4, "scope", st, "registry-only PR but " + detail)
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
    for o in owners:   # reviewer ids cannot contain ':' (review.py), so also match 'claude:x' as 'x' and 'claude-x'
        if o:
            ids.add(_norm(str(o).split(":", 1)[-1]))
            ids.add(_norm(str(o).replace(":", "-")))
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


def _gate_review(c: _Ctx, git: Callable, comments: Optional[List[Dict[str, Any]]], owners: List[str], n: int) -> Gate:
    if _reg_only(c) and not str(c.pr.get("headRefName", "")).startswith("wave/"):
        st, detail = _registry_check(c, git)
        if st == PASS:
            return Gate(5, "review", PASS, "registry-only record: no code review required (protocol section 18 gate 4)")
        if st == UNKNOWN:
            return Gate(5, "review", UNKNOWN, detail)
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


def _gate_reserved(c: _Ctx, override: str) -> Gate:
    hit = []
    for f in c.files:
        low = f.lower()
        if (f in _RESERVED_FILES or low.startswith(_RESERVED_DIRS) or low.rsplit("/", 1)[-1] in _FORBIDDEN_NAMES):
            hit.append(f)
    if not hit:
        return Gate(8, "reserved", PASS, "no governance, CI, hook or launcher path changed")
    if override:
        return Gate(8, "reserved", PASS, f"{len(hit)} reserved path(s) changed; lukas-direct-word: {override}")
    return Gate(8, "reserved", FAIL, f"{len(hit)} reserved path(s): " + ", ".join(hit[:4]) +
                " (needs --lukas-direct-word <comment URL>)")


def evaluate(pr: int, gh: Callable, git: Callable, base: str = "main",
             lukas_word: str = "") -> Tuple[List[Gate], Dict[str, Any]]:
    """Evaluate all seven gates. Never raises: any failure turns the affected gate into UNKNOWN."""
    info: Dict[str, Any] = {"head": "", "diff_sha": "", "comments": None}
    try:
        view = _pr_view(gh, pr)
    except Exception as e:
        return [Gate(i, nm, UNKNOWN, _clean(f"cannot read PR: {e}")) for i, nm in enumerate(
            ("pr_state", "checks", "merge_state", "scope", "review", "secrets", "forbidden_files", "reserved"), 1)], info
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
        for num, name in ((4, "scope"), (5, "review"), (6, "secrets"), (7, "forbidden_files"),
                          (8, "reserved")):
            gates.append(Gate(num, name, UNKNOWN, ctx.git_error))
        return gates, info
    gates.append(safe(4, "scope", lambda: _gate_scope(ctx, git, owners)))
    try:
        comments: Optional[List[Dict[str, Any]]] = _comments(gh, pr)
    except Exception:
        comments = None
    info["comments"] = comments
    gates.append(safe(5, "review", lambda: _gate_review(ctx, git, comments, owners, pr)))
    gates.append(safe(6, "secrets", lambda: _gate_secrets(ctx)))
    gates.append(safe(7, "forbidden_files", lambda: _gate_forbidden(ctx)))
    gates.append(safe(8, "reserved", lambda: _gate_reserved(ctx, lukas_word)))
    return gates, info


def _gate_branch_name(branch: str) -> Gate:
    """The offline part of the real pr_state gate: the head branch must start with one of BRANCH_PREFIXES."""
    if str(branch).startswith(BRANCH_PREFIXES):
        return Gate(1, "branch_name", PASS, "head branch prefix ok")
    return Gate(1, "branch_name", FAIL, f"head branch prefix not in {list(BRANCH_PREFIXES)}")


def _gate_handoff_current(git: Callable) -> Gate:
    """What CI's ``governance handoff --check`` decides, from the committed HEAD: SESSION_HANDOFF.md must equal the
    handoff rendered from PROJECT_STATUS.json and ACTIVE_TASKS.json. UNKNOWN when anything cannot be read or rendered."""
    try:
        from governance import handoff as handoff_mod
    except Exception as e:
        return Gate(2, "handoff_current", UNKNOWN, _clean(f"governance handoff not importable: {type(e).__name__}"))
    blobs: Dict[str, str] = {}
    for name in ("PROJECT_STATUS.json", "ACTIVE_TASKS.json", "SESSION_HANDOFF.md"):
        rc, out = git(["show", f"HEAD:{name}"])
        if rc != 0:
            return Gate(2, "handoff_current", UNKNOWN, f"{name} is not readable at HEAD")
        blobs[name] = out.decode("utf-8", "replace")
    try:
        rendered = handoff_mod.render(json.loads(blobs["PROJECT_STATUS.json"]), json.loads(blobs["ACTIVE_TASKS.json"]))
    except Exception as e:
        return Gate(2, "handoff_current", UNKNOWN, _clean(f"cannot render the handoff: {type(e).__name__}"))
    crlf, lf = chr(13) + chr(10), chr(10)    # the same normalisation as governance/cli.py _norm: only CRLF becomes LF
    if blobs["SESSION_HANDOFF.md"].replace(crlf, lf) != rendered.replace(crlf, lf):
        return Gate(2, "handoff_current", FAIL, "SESSION_HANDOFF.md is stale; run: python -B -m governance handoff --write")
    return Gate(2, "handoff_current", PASS, "SESSION_HANDOFF.md matches the registry and status files")


PREFLIGHT_GATES = ((1, "branch_name"), (2, "handoff_current"), (4, "scope"), (6, "secrets"), (7, "forbidden_files"), (8, "reserved"))


def preflight(git: Callable, base: str = "main", lukas_word: str = "") -> Tuple[List[Gate], Dict[str, Any]]:
    """The offline gates (branch name, handoff currency, scope, secrets, forbidden files, reserved paths) on the LOCAL branch against origin/<base>.

    Needs no pull request, no gh, no lock, and changes neither the working tree nor any branch (like a dry run it runs
    ``git fetch``, which only updates remote-tracking refs). It uses the same gate functions as a real evaluation, on the
    committed HEAD (uncommitted changes are reported in ``info['dirty']`` and are NOT evaluated). A PASS here means those
    gates would pass on the pushed head. CI, merge state and review need the pull request and are not checked.
    Never raises: a failure to read git state turns every gate into UNKNOWN."""
    info: Dict[str, Any] = {"head": "", "diff_sha": "", "branch": "", "dirty": False}

    def unknown(msg: str) -> Tuple[List[Gate], Dict[str, Any]]:
        return [Gate(n, nm, UNKNOWN, _clean(msg)) for n, nm in PREFLIGHT_GATES], info

    try:
        rc, out = git(["rev-parse", "HEAD"])
        sha = out.decode("utf-8", "replace").strip()
        if rc != 0 or not sha:
            return unknown("cannot read HEAD")
        rc, out = git(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = out.decode("utf-8", "replace").strip()
        if rc != 0 or not branch or branch == "HEAD":
            return unknown("detached HEAD: preflight needs a named branch")
        info["head"], info["branch"] = sha, branch
        rc, out = git(["status", "--porcelain"])
        info["dirty"] = not (rc == 0 and not out.strip())   # a failing status counts as dirty: warn, never assume clean
        ctx = _Ctx(pr={"headRefName": branch, "headRefOid": sha}, base=base, head_ref="HEAD")
        try:
            _collect_git(git, ctx.pr, base, ctx)
        except Exception as e:
            ctx.git_error = _clean(f"{type(e).__name__}: {e}")
        info["diff_sha"] = ctx.diff_sha
        if ctx.git_error:
            return unknown(ctx.git_error)
        owners: List[str] = []
        checks = ((1, "branch_name", lambda: _gate_branch_name(branch)), (2, "handoff_current", lambda: _gate_handoff_current(git)),
                  (4, "scope", lambda: _gate_scope(ctx, git, owners)), (6, "secrets", lambda: _gate_secrets(ctx)),
                  (7, "forbidden_files", lambda: _gate_forbidden(ctx)),
                  (8, "reserved", lambda: _gate_reserved(ctx, lukas_word)))
        gates: List[Gate] = []
        for num, name, fn in checks:
            try:
                gates.append(fn())
            except Exception as e:
                gates.append(Gate(num, name, UNKNOWN, _clean(f"{type(e).__name__}: {e}")))
        return gates, info
    except Exception as e:
        return unknown(f"{type(e).__name__}: {e}")


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
                      (f" ({_clean(res.note)})" if res.note else ""),
                      *([f"lukas-direct-word: {_clean(res.override)}"] if res.override else []), "",
                      render_table(res.gates)])
    rc, _ = gh(["pr", "comment", str(res.pr), "--body-file", "-"], body)
    return rc == 0


def _record_event(store: Any, res: Result) -> None:
    if store is not None:
        store.append_event("integrate", pr=res.pr, head=res.head, result=res.outcome, note=_clean(res.note, 160),
                           gates={g.name: g.status for g in res.gates})


def integrate(pr: int, *, yes: bool = False, base: str = "main", gh: Callable, git: Callable,
              state_dir: Optional[os.PathLike] = None, lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
              clock: Callable[[], float] = time.time, lukas_word: str = "",
              pid_alive: Optional[Callable[[int], bool]] = None) -> Result:
    if lukas_word and not _LUKAS_WORD_RE.match(lukas_word):
        raise ValueError("--lukas-direct-word must be a GitHub issue or pull request comment URL")
    gates, info = evaluate(pr, gh, git, base, lukas_word)
    res = Result(pr=pr, head=info["head"], gates=gates, diff_sha=info["diff_sha"], override=lukas_word)
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
    lock = IntegrationLock(store.directory, lock_timeout, clock, pid_alive)
    try:
        lock.acquire(pr)
    except LockHeld as e:
        res.outcome, res.note = "LOCKED", _clean(e)
        return res
    try:
        # final gate evaluation under the lock
        gates, info = evaluate(pr, gh, git, base, lukas_word)
        res.gates, res.diff_sha = gates, info["diff_sha"]
        try:
            lock.heartbeat()
        except LockHeld:
            res.outcome, res.note = "REFUSED", "integration lock lost before merge"
            _safe(lambda: _post_comment(gh, res, info["comments"]))
            _record_event(store, res)
            return res
        if not res.all_pass or info["head"] != res.head:
            res.outcome = "REFUSED"
            res.note = "head moved before merge" if info["head"] != res.head else "gates changed before merge"
            res.head = info["head"] or res.head
        else:
            _merge(pr, res, gh, base, lock.heartbeat)
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


def _merge(pr: int, res: Result, gh: Callable, base: str, heartbeat: Callable[[], None] = lambda: None) -> None:
    """Merge once; at most ONE retry, and only after confirming the PR is not merged and the head is unchanged."""
    cmd = ["pr", "merge", str(pr), "--merge", "--match-head-commit", res.head]
    err = ""
    for attempt in range(2):
        heartbeat()
        rc, out = gh(cmd, None)
        if rc == 0:
            _safe(heartbeat)
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
    ap.add_argument("pr", type=int, nargs="?", help="pull request number (not used with --preflight)")
    ap.add_argument("--preflight", action="store_true",
                    help="evaluate only the offline gates (scope, secrets, forbidden files, reserved) on the local "
                         "branch against origin/<base>; no pull request, no gh, no merge; changes no branch or file "
                         "(the fetch only updates remote-tracking refs)")
    ap.add_argument("--yes", action="store_true", help="merge when every gate passes (default: dry run)")
    ap.add_argument("--repo", default=".", help="repository root (default: current directory)")
    ap.add_argument("--base", default="main")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--lukas-direct-word", default="", metavar="URL",
                    help="URL of the issue/PR comment where Lukas directly authorised a reserved-path change "
                         "(recorded, not verified)")
    a = ap.parse_args(argv)
    if a.lukas_direct_word and not _LUKAS_WORD_RE.match(a.lukas_direct_word):
        ap.error("--lukas-direct-word must be a GitHub issue or pull request comment URL")
    out = out or sys.stdout
    root = Path(a.repo)
    if a.preflight:
        if a.pr is not None or a.yes:
            ap.error("--preflight takes no pull request number and no --yes")
        gates, info = preflight(git or real_git_runner(root), a.base, a.lukas_direct_word)
        print(f"preflight {info['head'][:12] or '?'} branch {info['branch'] or '?'} diff {info['diff_sha'] or '?'}", file=out)
        print(render_table(gates), file=out)
        if info["dirty"]:
            print("warning: uncommitted changes are NOT evaluated; commit first", file=out)
        ok = bool(gates) and all(g.status == PASS for g in gates)
        print("result: " + ("PREFLIGHT_PASS" if ok else "PREFLIGHT_FAIL") +
              " (offline gates only; CI, merge state and review need the pull request)", file=out)
        return 0 if ok else 1
    if a.pr is None:
        ap.error("a pull request number is required (or use --preflight)")
    res = integrate(a.pr, yes=a.yes, base=a.base, gh=gh or real_gh_runner(root), git=git or real_git_runner(root),
                    state_dir=a.state_dir, lukas_word=a.lukas_direct_word)
    print(f"PR #{res.pr} head {res.head[:12] or '?'} diff {res.diff_sha or '?'}", file=out)
    print(render_table(res.gates), file=out)
    if res.override:
        print(f"lukas-direct-word: {_clean(res.override)}", file=out)
    print(f"result: {res.outcome}" + (f" ({_clean(res.note)})" if res.note else ""), file=out)
    return res.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
