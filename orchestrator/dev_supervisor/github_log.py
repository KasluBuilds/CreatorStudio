"""Durable GitHub progress log for the development supervisor (T-DEV-006, issue #44 part 5).

The repository is PUBLIC. Every event is scrubbed before it leaves the process, content that still looks sensitive is
REFUSED (recorded locally as a refusal with reasons only, never the content), and an event is never lost: it is written
to a machine-local spool first and removed only after GitHub confirmed the comment. Posting is idempotent: every event
carries an id marker (an HTML comment) and a local ledger of posted ids; before a retry the runner is asked whether the
marker already exists.

Posting goes through an injected `Runner`. `GhRunner` wraps the `gh` CLI with argument lists (never shell strings).
Unit tests use a fake runner. Standard library only. Single-process use per state directory is assumed (no file lock).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

__all__ = ["EventKind", "Event", "Runner", "GhRunner", "RunnerError", "GitHubLogger", "FlushResult",
           "SensitiveContentError", "scrub", "sensitivity_flags", "render_event", "render_comment",
           "build_subtask_body", "MARKER_PREFIX"]

MARKER_PREFIX = "dev-event:"
_MARKER_RE = re.compile(r"<!--\s*dev-event:([0-9a-f]{8,64})\s*-->")
_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


class EventKind(str, Enum):
    TASK_STARTED = "task_started"
    STEP_DONE = "step_done"
    DECISION = "decision"
    TEST_RUN = "test_run"
    FAILURE = "failure"
    RETRY = "retry"
    PROVIDER_CHANGE = "provider_change"
    HANDOFF = "handoff"
    COMPACTION = "compaction"
    BLOCKER = "blocker"
    PR_OPENED = "pr_opened"


# --------------------------------------------------------------------------------------------------
# scrubbing and sensitivity detection
# --------------------------------------------------------------------------------------------------
_PATH_PATTERNS = [
    re.compile(r"(?i)\b[a-z]:[\\/]+[^\s\"'<>|*?]+"),                      # Windows absolute path
    re.compile(r"\\\\[A-Za-z0-9._$-]+\\[^\s\"'<>|*?]+"),                  # UNC path
    re.compile(r"(?<![\w.])/(?:Users|home|root)/[^\s\"'<>|*?]*"),          # POSIX user home
    re.compile(r"(?<![\w.])~[\\/][^\s\"'<>|*?]*"),                         # ~/something
]
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|pwd|"
               r"authorization|auth|signature|sig|credential)s?\b\s*[=:]\s*(?!<redacted>)[^\s,;&\"')]+"),
    re.compile(r"(?i)(https?://)[^/\s@]+@"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_|xox[abprs]-|AKIA)[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_QUERY_RE = re.compile(r"(https?://[^\s?#\"')]+)\?[^\s\"')]*")
# long opaque strings; pure-hex of git-hash/digest length (32..64) is allowed (commit ids are evidence)
_OPAQUE_RE = re.compile(r"(?![0-9a-fA-F]{32,64}\b)[A-Za-z0-9_+/=-]{32,}")
_TRANSCRIPT_RE = re.compile(r"(?i)(\[/?transcript\]|</?transcript\b|\b(?:transcript|verbatim|spoken[ _]text|"
                            r"subtitle[ _]text|dialogue)\s*(?:text)?\s*[:=])")
_SCRUBBERS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 <redacted>"),
    (_SECRET_PATTERNS[1], r"\1=<redacted>"),
    (_SECRET_PATTERNS[2], r"\1<redacted>@"),
    (_QUERY_RE, r"\1?<redacted>"),
    (_SECRET_PATTERNS[3], "<redacted>"),
    (_SECRET_PATTERNS[4], "<redacted>"),
    (_SECRET_PATTERNS[5], "<redacted>"),
]


def sensitivity_flags(text: Any) -> List[str]:
    """Reasons `text` must not be posted as-is: 'secret', 'personal_path', 'transcript'. Empty list = nothing found."""
    s = str(text)
    flags: List[str] = []
    if any(p.search(s) for p in _SECRET_PATTERNS):
        flags.append("secret")
    if any(p.search(s) for p in _PATH_PATTERNS):
        flags.append("personal_path")
    if _TRANSCRIPT_RE.search(s):
        flags.append("transcript")
    return flags


def scrub(text: Any, limit: int = 300) -> str:
    """Redact credential-like text, absolute/personal paths and long opaque strings; cap the length."""
    s = "".join(ch if (ch.isprintable() or ch in "\n") else " " for ch in str(text))
    for pat, rep in _SCRUBBERS:
        s = pat.sub(rep, s)
    for pat in _PATH_PATTERNS:
        s = pat.sub("<path>", s)
    s = _OPAQUE_RE.sub("<redacted>", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:limit]


def _neutralise(text: str) -> str:
    """Stop user text from pinging people or forging an event marker in a public comment."""
    text = text.replace("<!--", "&lt;!--")
    return re.sub(r"@(?=\w)", "@​", text)


# --------------------------------------------------------------------------------------------------
# event model
# --------------------------------------------------------------------------------------------------
_MAX_EVIDENCE = 10


@dataclass(frozen=True)
class Event:
    """One progress event. All text fields are scrubbed on construction; `flags` records what the RAW input contained
    (secret / personal_path / transcript). A flagged event is refused by the logger, never posted."""
    kind: EventKind
    task_id: str
    title: str
    detail: str = ""
    evidence: Tuple[str, ...] = ()
    source: str = "dev-supervisor"
    ts: str = ""
    issue: int = 0
    event_id: str = ""
    flags: Tuple[str, ...] = ()
    explicit_id: bool = field(default=False, compare=False)

    def __post_init__(self):
        sv = object.__setattr__
        sv(self, "kind", EventKind(self.kind))
        raw = [self.task_id, self.title, self.detail, self.source, *self.evidence]
        flags = set(self.flags)
        for r in raw:
            flags.update(sensitivity_flags(r))
        sv(self, "flags", tuple(sorted(flags)))
        sv(self, "task_id", scrub(self.task_id, 60))
        sv(self, "title", scrub(self.title, 140).replace("\n", " "))
        sv(self, "detail", scrub(self.detail, 1200))
        sv(self, "source", scrub(self.source, 60).replace("\n", " "))
        sv(self, "evidence", tuple(scrub(e, 200).replace("\n", " ") for e in list(self.evidence)[:_MAX_EVIDENCE]))
        if not isinstance(self.issue, int) or isinstance(self.issue, bool) or self.issue < 0:
            raise ValueError("issue must be a non-negative integer")
        if self.event_id and not _ID_RE.match(self.event_id):
            raise ValueError("event_id must be 8-64 lowercase hex characters")
        if self.event_id:
            sv(self, "explicit_id", True)
        if not self.event_id:
            sv(self, "event_id", self._compute_id())

    def _compute_id(self) -> str:
        blob = json.dumps([self.kind.value, self.task_id, self.title, self.detail, list(self.evidence),
                           self.source, self.ts, self.issue], sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "task_id": self.task_id, "title": self.title, "detail": self.detail,
                "evidence": list(self.evidence), "source": self.source, "ts": self.ts, "issue": self.issue,
                "event_id": self.event_id, "flags": list(self.flags)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Event":
        return cls(kind=d["kind"], task_id=d.get("task_id", ""), title=d.get("title", ""), detail=d.get("detail", ""),
                   evidence=tuple(d.get("evidence", ())), source=d.get("source", "dev-supervisor"),
                   ts=d.get("ts", ""), issue=int(d.get("issue", 0)), event_id=d.get("event_id", ""),
                   flags=tuple(d.get("flags", ())))


def render_event(ev: Event) -> str:
    """Concise Markdown for one event, ending with its idempotency marker."""
    head = f"**{ev.kind.value}** | {_neutralise(ev.title)}"
    meta = [f"task `{_neutralise(ev.task_id)}`" if ev.task_id else "", f"source `{_neutralise(ev.source)}`",
            ev.ts]
    lines = [head, " | ".join(m for m in meta if m)]
    if ev.detail:
        lines.append("")
        lines.append(_neutralise(ev.detail))
    if ev.evidence:
        lines.append("")
        lines.append("Evidence:")
        lines.extend(f"- {_neutralise(e)}" for e in ev.evidence)
    lines.append("")
    lines.append(f"<!-- {MARKER_PREFIX}{ev.event_id} -->")
    return "\n".join(lines)


def render_comment(events: Sequence[Event]) -> str:
    """One comment body for one or more events (batching)."""
    return "\n\n---\n\n".join(render_event(e) for e in events)


def build_subtask_body(record: Mapping[str, Any], parent_issue: int) -> str:
    """Markdown body of a sub-task issue from a task record: id, owner, scope, dependencies, acceptance criteria,
    branch/worktree (worktree shown by folder name only). Back-references the parent issue."""
    if not isinstance(parent_issue, int) or isinstance(parent_issue, bool) or parent_issue <= 0:
        raise ValueError("parent_issue must be a positive integer")

    def line(label: str, value: Any, limit: int = 300) -> str:
        return f"- **{label}:** {_neutralise(scrub(value, limit)) or 'none'}"

    wt = str(record.get("worktree", "") or "")
    wt_name = re.split(r"[\\/]+", wt.rstrip("\\/"))[-1] if wt else ""
    deps = record.get("dependencies") or []
    acc = record.get("acceptance_criteria") or record.get("acceptance") or []
    out = [f"Parent issue: #{parent_issue}", "",
           line("Task id", record.get("id", "")), line("Owner", record.get("owner", "")),
           line("Scope", record.get("scope", ""), 600),
           line("Dependencies", ", ".join(map(str, deps)) if not isinstance(deps, str) else deps),
           line("Branch", record.get("branch", "")), line("Worktree", wt_name), "", "Acceptance criteria:"]
    if isinstance(acc, str):
        acc = [acc]
    out.extend(f"- [ ] {_neutralise(scrub(a, 300))}" for a in list(acc)[:30]) if acc else out.append("- none recorded")
    return "\n".join(out)


# --------------------------------------------------------------------------------------------------
# runner protocol and the real gh runner
# --------------------------------------------------------------------------------------------------
class RunnerError(Exception):
    """A posting failure. `transient` = worth retrying (network, rate limit, 5xx)."""

    def __init__(self, message: str, transient: bool = True):
        self.message = scrub(message, 200)
        self.transient = transient
        super().__init__(self.message)


class Runner(Protocol):
    def comment(self, issue: int, body: str) -> None: ...
    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int: ...
    def close_issue(self, issue: int, comment: str = "") -> None: ...
    def event_ids(self, issue: int) -> set: ...   # event ids whose marker already exists on the issue


_TRANSIENT_HINTS = ("timeout", "timed out", "connect", "network", "rate limit", "secondary rate", "502", "503",
                    "504", "500", "temporar", "unreachable", "reset", "eof", "could not resolve")


class GhRunner:
    """Runner over the `gh` CLI. Argument lists only (no shell); bodies go through stdin (`--body-file -`)."""

    def __init__(self, repo: str = "", gh: str = "gh", timeout: float = 60.0, run: Optional[Callable] = None):
        self.repo = repo
        self.gh = gh
        self.timeout = timeout
        self._run = run or subprocess.run

    def _base(self, *args: str) -> List[str]:
        cmd = [self.gh, *args]
        if self.repo:
            cmd += ["-R", self.repo]
        return cmd

    def _exec(self, cmd: List[str], stdin: Optional[str] = None) -> str:
        try:
            p = self._run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                          timeout=self.timeout, shell=False)
        except subprocess.TimeoutExpired:
            raise RunnerError("gh timed out", True)
        except OSError as e:
            raise RunnerError(f"gh could not run: {type(e).__name__}", False)
        if p.returncode != 0:
            err = scrub(p.stderr or p.stdout or "gh failed", 200)
            transient = any(h in err.lower() for h in _TRANSIENT_HINTS)
            raise RunnerError(f"gh exit {p.returncode}: {err}", transient)
        return p.stdout or ""

    def comment(self, issue: int, body: str) -> None:
        self._exec(self._base("issue", "comment", str(int(issue)), "--body-file", "-"), stdin=body)

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        cmd = ["issue", "create", "--title", title, "--body-file", "-"]
        for lb in labels:
            cmd += ["--label", lb]
        out = self._exec(self._base(*cmd), stdin=body)
        m = re.search(r"/issues/(\d+)", out)
        if not m:
            raise RunnerError("could not parse created issue number", False)
        return int(m.group(1))

    def close_issue(self, issue: int, comment: str = "") -> None:
        args = ["issue", "close", str(int(issue))]
        if comment:
            args += ["--comment", comment]
        self._exec(self._base(*args))

    def event_ids(self, issue: int) -> set:
        out = self._exec(self._base("issue", "view", str(int(issue)), "--json", "comments"))
        try:
            comments = json.loads(out).get("comments", [])
        except (ValueError, AttributeError):
            raise RunnerError("unparseable gh output", False)
        found = set()
        for c in comments:
            found.update(_MARKER_RE.findall(str(c.get("body", ""))))
        return found


# --------------------------------------------------------------------------------------------------
# durable local files (atomic)
# --------------------------------------------------------------------------------------------------
def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_json(path: Path, default: Any) -> Any:
    """Read a state file; a corrupt file is moved aside (never silently dropped) and the default returned."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        n = 1
        while path.with_name(f"{path.name}.corrupt-{n}").exists():
            n += 1
        try:
            os.replace(path, path.with_name(f"{path.name}.corrupt-{n}"))
        except OSError:
            pass
        return default


# --------------------------------------------------------------------------------------------------
# the logger
# --------------------------------------------------------------------------------------------------
class SensitiveContentError(Exception):
    """Content refused because it looked sensitive; only the reasons are kept."""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = list(reasons)
        super().__init__("refused: " + ",".join(self.reasons))


@dataclass
class FlushResult:
    posted: List[str] = field(default_factory=list)       # event ids confirmed on GitHub this flush
    remaining: int = 0                                    # events still in the spool
    errors: List[str] = field(default_factory=list)       # scrubbed error strings
    already_posted: List[str] = field(default_factory=list)  # dropped as duplicates (ledger or marker on GitHub)


class GitHubLogger:
    LEDGER_CAP = 5000

    def __init__(self, runner: Runner, state_dir: os.PathLike, default_issue: int = 0, *,
                 max_batch: int = 5, max_chars: int = 50000, min_interval: float = 1.0, max_attempts: int = 4,
                 backoff_base: float = 1.0, backoff_cap: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], str] = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())):
        if max_batch < 1 or max_attempts < 1:
            raise ValueError("max_batch and max_attempts must be >= 1")
        self.runner = runner
        self.dir = Path(state_dir)
        self.default_issue = default_issue
        self.max_batch, self.max_chars = max_batch, max_chars
        self.min_interval, self.max_attempts = min_interval, max_attempts
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self._sleep, self._mono, self._clock = sleep, monotonic, clock
        self._last_post: Optional[float] = None
        self.spool_path = self.dir / "spool.json"
        self.ledger_path = self.dir / "posted.json"
        self.refusal_path = self.dir / "refusals.json"
        self.subtask_path = self.dir / "subtasks.json"

    # ---- local state
    def _spool(self) -> List[Dict[str, Any]]:
        data = _load_json(self.spool_path, {"events": []})
        return list(data.get("events", [])) if isinstance(data, dict) else []

    def _save_spool(self, events: List[Dict[str, Any]]) -> None:
        _atomic_write(self.spool_path, {"version": 1, "events": events})

    def _ledger(self) -> List[str]:
        data = _load_json(self.ledger_path, {"ids": []})
        return list(data.get("ids", [])) if isinstance(data, dict) else []

    def _save_ledger(self, ids: List[str]) -> None:
        _atomic_write(self.ledger_path, {"version": 1, "ids": ids[-self.LEDGER_CAP:]})

    def pending(self) -> List[Event]:
        return [Event.from_dict(d) for d in self._spool()]

    def refusals(self) -> List[Dict[str, Any]]:
        data = _load_json(self.refusal_path, {"refusals": []})
        return list(data.get("refusals", [])) if isinstance(data, dict) else []

    def _record_refusal(self, what: str, ident: str, task_id: str, reasons: Sequence[str]) -> None:
        items = self.refusals()
        items.append({"what": what, "id": ident, "task_id": scrub(task_id, 60), "reasons": sorted(set(reasons)),
                      "ts": self._clock()})
        _atomic_write(self.refusal_path, {"version": 1, "refusals": items})

    # ---- logging
    def log(self, event: Event, *, flush: bool = True) -> str:
        """Record an event. Returns 'refused', 'duplicate', 'spooled' (queued, not yet confirmed) or 'posted'.
        The event is spooled BEFORE any network call, so it survives a crash or outage."""
        if not event.ts or not event.issue:
            if not event.issue and not self.default_issue:
                raise ValueError("event has no issue and the logger has no default_issue")
            d = event.to_dict()
            d["ts"] = event.ts or self._clock()
            d["issue"] = event.issue or self.default_issue
            if not event.explicit_id:
                d["event_id"] = ""
            event = Event.from_dict(d)
        reasons = list(event.flags) + [f for f in sensitivity_flags(render_event(event)) if f not in event.flags]
        if reasons:
            self._record_refusal("event", event.event_id, event.task_id, reasons)
            return "refused"
        if event.event_id in self._ledger() or any(d.get("event_id") == event.event_id for d in self._spool()):
            return "duplicate"
        spool = self._spool()
        spool.append({**event.to_dict(), "attempts": 0, "last_error": ""})
        self._save_spool(spool)
        if not flush:
            return "spooled"
        res = self.flush()
        return "posted" if event.event_id in res.posted else "spooled"

    # ---- flushing
    def _wait_rate_limit(self) -> None:
        if self._last_post is not None and self.min_interval > 0:
            gap = self.min_interval - (self._mono() - self._last_post)
            if gap > 0:
                self._sleep(gap)

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_cap, self.backoff_base * (2 ** attempt))

    def _chunks(self, events: List[Event]) -> Iterable[List[Event]]:
        cur: List[Event] = []
        size = 0
        for e in events:
            n = len(render_event(e)) + 10
            if cur and (len(cur) >= self.max_batch or size + n > self.max_chars):
                yield cur
                cur, size = [], 0
            cur.append(e)
            size += n
        if cur:
            yield cur

    def flush(self) -> FlushResult:
        """Post everything in the spool, oldest first, batched per issue. Idempotent and safe to call any time."""
        res = FlushResult()
        ledger = self._ledger()
        done = set(ledger)
        raw = self._spool()
        # purge events the ledger already knows (crash between post and spool update)
        keep = []
        for d in raw:
            if d.get("event_id") in done:
                res.already_posted.append(d["event_id"])
            else:
                keep.append(d)
        if len(keep) != len(raw):
            self._save_spool(keep)
        by_id = {d["event_id"]: d for d in keep}
        issues: Dict[int, List[Event]] = {}
        for d in keep:
            ev = Event.from_dict(d)
            issues.setdefault(ev.issue, []).append(ev)
        for issue, evs in issues.items():
            for chunk in self._chunks(evs):
                err = self._post_chunk(issue, chunk, res)
                if err is None:
                    for e in chunk:
                        ledger.append(e.event_id)
                        done.add(e.event_id)
                        by_id.pop(e.event_id, None)
                    self._save_ledger(ledger)
                    self._save_spool(list(by_id.values()))
                else:
                    for e in chunk:
                        by_id[e.event_id]["attempts"] = int(by_id[e.event_id].get("attempts", 0)) + 1
                        by_id[e.event_id]["last_error"] = err
                    self._save_spool(list(by_id.values()))
                    res.errors.append(err)
                    break  # keep per-issue ordering: do not post later chunks past a failed one
        res.remaining = len(by_id)
        return res

    def _post_chunk(self, issue: int, chunk: List[Event], res: FlushResult) -> Optional[str]:
        body = render_comment(chunk)
        flags = sensitivity_flags(body)
        if flags:  # defence in depth: refuse at the last moment
            for e in chunk:
                self._record_refusal("event", e.event_id, e.task_id, flags)
            return "refused at flush: " + ",".join(flags)
        last = ""
        for attempt in range(self.max_attempts):
            if attempt:
                self._sleep(self._backoff(attempt - 1))
                # a failure can be ambiguous (comment created, reply lost): look for the marker before re-posting
                try:
                    seen = self.runner.event_ids(issue)
                except Exception:
                    seen = set()
                if all(e.event_id in seen for e in chunk):
                    res.posted.extend(e.event_id for e in chunk)
                    return None
            self._wait_rate_limit()
            try:
                self.runner.comment(issue, body)
            except RunnerError as e:
                last = e.message or "runner error"
                self._last_post = self._mono()
                if not e.transient:
                    return last
                continue
            except Exception as e:  # unknown runner failure: contained, scrubbed, treated as transient
                last = scrub(f"{type(e).__name__}: {e}", 200)
                continue
            self._last_post = self._mono()
            res.posted.extend(e.event_id for e in chunk)
            return None
        return last or "retries exhausted"

    # ---- issues
    def _retry(self, fn: Callable[[], Any]) -> Any:
        last: Optional[RunnerError] = None
        for attempt in range(self.max_attempts):
            if attempt:
                self._sleep(self._backoff(attempt - 1))
            self._wait_rate_limit()
            try:
                out = fn()
                self._last_post = self._mono()
                return out
            except RunnerError as e:
                self._last_post = self._mono()
                last = e
                if not e.transient:
                    break
        raise last if last else RunnerError("retries exhausted")

    def create_subtask(self, record: Mapping[str, Any], parent_issue: int, labels: Sequence[str] = ()) -> int:
        """Create a sub-task issue from a task record (idempotent per task id: a second call returns the same number).
        Raises SensitiveContentError (and records a refusal) if the content looks sensitive; RunnerError if GitHub
        stays unreachable after bounded retries."""
        task_id = scrub(record.get("id", ""), 60)
        if not task_id:
            raise ValueError("record needs an id")
        known = _load_json(self.subtask_path, {"tasks": {}}).get("tasks", {})
        if task_id in known:
            return int(known[task_id])
        raw_flags = set()
        for k in ("id", "owner", "scope", "branch", "worktree"):
            v = record.get(k, "")
            raw_flags.update(f for f in sensitivity_flags(v) if not (k == "worktree" and f == "personal_path"))
        for k in ("dependencies", "acceptance_criteria", "acceptance"):
            v = record.get(k) or []
            for item in ([v] if isinstance(v, str) else v):
                raw_flags.update(sensitivity_flags(item))
        body = build_subtask_body(record, parent_issue)
        raw_flags.update(sensitivity_flags(body))
        if raw_flags:
            self._record_refusal("subtask", task_id, task_id, sorted(raw_flags))
            raise SensitiveContentError(sorted(raw_flags))
        title = scrub(f"[{task_id}] {record.get('scope', '') or 'sub-task'}", 120).replace("\n", " ")
        number = self._retry(lambda: self.runner.create_issue(title, body, list(labels)))
        known[task_id] = int(number)
        _atomic_write(self.subtask_path, {"version": 1, "tasks": known})
        return int(number)

    def close_issue(self, issue: int, comment: str = "") -> None:
        """Close an issue (optionally with a scrubbed comment); refuses sensitive comments."""
        flags = sensitivity_flags(comment) if comment else []
        if flags:
            self._record_refusal("close", str(issue), "", flags)
            raise SensitiveContentError(flags)
        self._retry(lambda: self.runner.close_issue(int(issue), _neutralise(scrub(comment, 500))))
