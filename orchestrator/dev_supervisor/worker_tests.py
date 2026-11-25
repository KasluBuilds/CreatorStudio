"""Supervisor-triggered task tests for finished workers (T-DEV-073, issue #44).

    scanner = WorkerTestScanner(store, registry, repo_root)
    changes = scanner.scan()          # one pass; never blocks on a test run

A worker that exited 0 on its ``task/`` branch has produced a commit. This module notices that and starts the EXISTING
runner (``dev_supervisor.testreq run``, a child process) for the task's own ``test_requirements``, then reports the
result. It never executes a test command itself, never publishes, reviews or merges.

Rules (IMPLEMENTED, tested offline with injected git, spawn and clock):
- Candidates: role ``worker``, status EXITED, exit code 0, branch ``task/...``, ended after the feature was first
  enabled (``enabled_since`` in the state document) and within 48 hours, task ACTIVE or UNDER_REVIEW on ``origin/main``
  (as of the last fetch; this module never fetches). Failed, stopped, reviewer and historical workers are never tested.
- Preconditions, checked on every pass: the worktree exists, is clean and is on the registered branch, and the branch
  has at least one commit ahead of the base. The requirement list is the task's, read from the base ref; an empty
  list or a manual requirement is BLOCKED (nothing partial is run). A cause that clears is picked up by a later pass.
- The verdict comes only from the sealed evidence record for the current head, diff hash and requirement list
  (``testreq.check_evidence``). A record for another head or diff, or a damaged one, is never reused. ``skipped`` is
  never ``passed``.
- One runner at a time; no launch below the memory floor. The launch is written to the state document BEFORE the
  process starts and the pid right after, so a crash between the two is recovered without a second runner: a live
  record in the evidence store is adopted, an unrecorded launch is given a grace period.
- A dead runner whose record is not complete is relaunched with ``--resume`` (same head only), at most three attempts,
  then BLOCKED. A runner started by an earlier Supervisor process keeps running and is found again by its pid.
- Events ``worker_tests_started|passed|failed|blocked|interrupted`` are appended when the state or its detail changes.

State document ``worker_tests`` (in the Supervisor state directory): per worker the task, branch, head, diff hash, state
(PENDING, RUNNING, PASSED, FAILED, BLOCKED), attempts, runner pid and a short detail. No output, environment value or
secret is stored (the store scrubs credential shapes); the runner's own evidence record holds the output tails.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import integrate, memory_guard, testreq
from .state_store import StateStore, parse_iso, scrub, utc_iso
from .workers import WorkerRegistry, WorkerStatus

__all__ = ["WorkerTestScanner", "enabled", "State", "DOC"]

DOC = "worker_tests"
SCHEMA = 1
ENV_SWITCH = "CREATORSTUDIO_WORKER_TESTS"
MAX_ATTEMPTS = 3
MAX_AGE_S = 48 * 3600.0
ADOPT_GRACE_S = 60.0
SPAWN_RETRY_S = 300.0
OPEN_TASK_STATUSES = ("ACTIVE", "UNDER_REVIEW")
DETAIL_MAX = 400
MAX_ATTEMPT_KEYS = 100


class State:
    PENDING = "PENDING"      # eligible, waiting (another run, or memory)
    RUNNING = "RUNNING"      # a runner process is working on this head
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"      # a stated cause stops it; cleared causes are re-checked on every pass
    FINAL = (PASSED, FAILED)


def enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """On unless ``CREATORSTUDIO_WORKER_TESTS`` is 0, off, false or no."""
    raw = (os.environ if env is None else env).get(ENV_SWITCH, "")
    return str(raw).strip().lower() not in ("0", "off", "false", "no")


def _short(text: Any) -> str:
    return " ".join(str(text).split())[:DETAIL_MAX]


class WorkerTestScanner:
    def __init__(self, store: StateStore, registry: WorkerRegistry, repo_root: Path, *,
                 evstore: Optional[testreq.EvidenceStore] = None,
                 git_for: Optional[Callable[[str], Callable]] = None,
                 spawn: Optional[Callable[..., Any]] = None,
                 pid_alive: Optional[Callable[[Any], bool]] = None,
                 free_memory: Callable[[], Optional[int]] = memory_guard.free_memory_mb,
                 floor: Optional[int] = None,
                 clock: Callable[[], float] = time.time,
                 base: str = "origin/main", timeout: float = testreq.DEFAULT_TIMEOUT) -> None:
        self.store, self.registry, self.root = store, registry, Path(repo_root)
        self.evstore = evstore or testreq.EvidenceStore(store.directory, clock=clock)
        self.git_for = git_for or (lambda path: integrate.real_git_runner(Path(path)))
        self.spawn = spawn or testreq.spawn_run
        self.pid_alive = pid_alive or testreq._pid_alive
        self.free_memory = free_memory
        self.floor = floor
        self.clock = clock
        self.base = base
        self.timeout = timeout
        self._snapshot = ""
        self._handles: Dict[int, Any] = {}          # runners started by this process (to reap them and poll exactly)

    # ------------------------------------------------------------ state document

    def _load(self) -> Dict[str, Any]:
        doc = self.store.read(DOC, default=None)
        if not isinstance(doc, dict) or not isinstance(doc.get("entries"), dict):
            doc = {"schema": SCHEMA, "enabled_since": None, "entries": {}}
        doc["entries"] = {k: v for k, v in doc["entries"].items() if isinstance(v, dict)}
        if not isinstance(doc.get("attempts"), dict):
            doc["attempts"] = {}
        return doc

    def _runner_alive(self, pid: Any) -> bool:
        h = self._handles.get(pid) if isinstance(pid, int) else None
        if h is not None:
            return h.poll() is None
        return bool(isinstance(pid, int) and self.pid_alive(pid))

    def _live(self, entry: Optional[Dict[str, Any]]) -> bool:
        """Is a runner of this entry still working, whatever state the entry has now? (A head that moved or a blocked
        worktree must never free the slot while the old runner is alive.) A launch whose pid was not saved yet counts
        for a grace period."""
        if not entry:
            return False
        pid = entry.get("runner_pid")
        if pid is not None and entry.get("task_id") and entry.get("head"):
            rec, _ = self.evstore.load(str(entry["task_id"]), str(entry["head"]))
            if isinstance(rec, dict) and rec.get("state") == "complete" and rec.get("pid") == pid:
                return False                          # the runner finished; a pid that looks alive now was reused
        if pid is None:
            started = parse_iso(entry.get("launched_at"))
            return entry.get("state") == State.RUNNING and started is not None and self.clock() - started < ADOPT_GRACE_S
        return self._runner_alive(pid)

    def _reap(self) -> None:
        for pid in [p for p, h in self._handles.items() if h.poll() is not None]:
            del self._handles[pid]

    # ------------------------------------------------------------ one pass

    def scan(self) -> List[Dict[str, Any]]:
        """One pass. Returns the changes made: [{worker_id, task_id, state, detail}].

        No git command runs while no worker is a candidate. One worker's failure never stops the others; the state
        document is read and written outside that protection, so a damaged or unwritable store raises to the caller."""
        self._reap()
        doc = self._load()
        self._snapshot = json.dumps(doc, sort_keys=True)               # what is on disk now
        now = self.clock()
        if not doc.get("enabled_since"):
            doc["enabled_since"] = utc_iso(now)
        since = parse_iso(doc["enabled_since"]) or now
        changes: List[Dict[str, Any]] = []
        workers = [w for w in sorted(self.registry.workers.values(), key=lambda w: (w.assigned_at or "", w.worker_id))
                   if self._candidate(w, since, now)]
        tasks = self._registry_tasks() if workers else None
        for w in workers if tasks is not None else []:
            task = tasks.get(w.task_id)
            if task is not None and task.get("status") not in OPEN_TASK_STATUSES:
                continue                                           # a finished task is not tested
            try:
                self._evaluate(w, task, doc, changes)
            except Exception as e:                                 # noqa: BLE001 - recorded on the entry, never raised
                self._set(doc, changes, w, State.BLOCKED, f"the evaluation failed ({type(e).__name__})")
        if json.dumps(doc, sort_keys=True) != self._snapshot:
            self.store.write(DOC, doc)
        return changes

    def _registry_tasks(self) -> Optional[Dict[str, Dict[str, Any]]]:
        rc, out = self.git_for(str(self.root))(["show", f"{self.base}:ACTIVE_TASKS.json"])
        if rc != 0:
            return None
        try:
            doc = json.loads(out.decode("utf-8"))
            return {str(t["id"]): t for t in doc["tasks"] if isinstance(t, dict) and t.get("id")}
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def _candidate(self, w: Any, since: float, now: float) -> bool:
        if w.role != "worker" or w.status != WorkerStatus.EXITED or w.exit_code != 0:
            return False
        if not str(w.branch or "").startswith("task/"):
            return False
        ended = parse_iso(w.ended_at)
        return ended is not None and ended >= since and now - ended <= MAX_AGE_S

    # ------------------------------------------------------------ one worker

    def _set(self, doc: Dict[str, Any], changes: List[Dict[str, Any]], w: Any, state: str, detail: str, *,
             event: Optional[str] = None, emit: bool = True, **fields: Any) -> Dict[str, Any]:
        """Record a state. Nothing is rewritten, and no event appended, when state, detail and fields are unchanged."""
        entries = doc["entries"]
        old = entries.get(w.worker_id) or {}
        detail = _short(detail)
        new = scrub({**old, "worker_id": w.worker_id, "task_id": w.task_id, "branch": w.branch, "worktree": w.worktree,
                     "state": state, "detail": scrub(detail), **fields})
        detail = new["detail"]
        if {k: v for k, v in new.items() if k != "updated"} == {k: v for k, v in old.items() if k != "updated"}:
            return old
        new["updated"] = utc_iso(self.clock())
        entries[w.worker_id] = new
        if old.get("state") != state or old.get("detail") != detail:
            changes.append({"worker_id": w.worker_id, "task_id": w.task_id, "state": state, "detail": detail})
            if emit and state != State.PENDING:
                self.store.append_event(event or f"worker_tests_{state.lower()}", worker_id=w.worker_id,
                                        task_id=w.task_id, head=str(new.get("head") or "")[:12], detail=detail)
        return new

    def _evaluate(self, w: Any, task: Optional[Dict[str, Any]], doc: Dict[str, Any], changes: List[Dict[str, Any]]) -> None:
        entry = doc["entries"].get(w.worker_id)
        if self._live(entry):
            return                                                       # a runner of this worker is still working: wait
        dead_pid = entry.get("runner_pid") if entry else None
        if entry and dead_pid is not None:
            entry["runner_pid"] = None                                   # never keep a dead pid: it may be reused later
        blocked = lambda why, **kw: self._set(doc, changes, w, State.BLOCKED, why, **kw)   # noqa: E731
        if task is None:
            blocked(f"task {w.task_id} is not on {self.base} (as of the last fetch; run git fetch)")
            return
        if task.get("branch") != w.branch:
            blocked(f"the registered branch of {w.task_id} is {task.get('branch')}, the worker's is {w.branch}")
            return
        if not isinstance(w.worktree, str) or not os.path.isdir(w.worktree):
            blocked(f"the worktree {w.worktree} does not exist")
            return
        git = self.git_for(w.worktree)
        st = testreq._status(git)
        if st is None:
            blocked("the worktree status cannot be read")
            return
        head, changed = st
        rc, br = git(["rev-parse", "--abbrev-ref", "HEAD"])
        if rc != 0 or br.decode("utf-8", "replace").strip() != w.branch:
            blocked(f"the worktree is not on the registered branch {w.branch}", head=head)
            return
        texts = task.get("test_requirements")
        rhash = testreq.reqs_hash(texts) if isinstance(texts, list) else ""
        rc, bsha = git(["rev-parse", self.base])
        base_sha = bsha.decode("utf-8", "replace").strip() if rc == 0 else ""
        if entry and entry.get("head") == head and entry.get("state") in State.FINAL and entry.get("reqs_sha256") == rhash \
                and entry.get("base_sha") == base_sha:
            return                                                       # decided for this head, base and requirements
        if changed:
            blocked("the worktree has uncommitted changes; commit or stash them (the evidence must describe the commit)", head=head)
            return
        rc, n = git(["rev-list", "--count", f"{self.base}..HEAD"])
        if rc != 0 or not n.strip().isdigit() or int(n.strip()) < 1:
            blocked(f"the branch has no commit ahead of {self.base}", head=head)
            return
        diff = integrate.diff_hash(git, self.base, "HEAD") or ""
        if not diff:
            blocked(f"the diff against {self.base} cannot be computed", head=head)
            return
        try:
            reqs = testreq.classify_all(texts)
        except testreq.TestReqError as e:
            blocked(str(e), head=head, diff_sha256=diff)
            return
        manual = [r for r in reqs if r.kind == "manual"]
        if manual:
            blocked(f"{manual[0].label()} is not machine-executable ({manual[0].reason}); rewrite it as an exact command",
                    head=head, diff_sha256=diff)
            return
        rec, why = self.evstore.load(w.task_id, head)
        status, detail = testreq.check_evidence(reqs, texts, rec, why, task_id=w.task_id, head=head, diff_sha=diff,
                                                pid_alive=self.pid_alive)
        bound = self._bound(rec, w.task_id, head, diff, texts)
        known = dict(head=head, diff_sha256=diff, reqs_sha256=rhash, base_sha=base_sha)
        if status == testreq.PASS:
            self._set(doc, changes, w, State.PASSED, detail, runner_pid=None, **known)
            return
        if bound and rec.get("state") == "complete":
            self._set(doc, changes, w, State.FAILED, detail, runner_pid=None, **known)
            return
        if bound and rec.get("state") == "running" and self.pid_alive(rec.get("pid")):
            self._set(doc, changes, w, State.RUNNING, "a test run for this head is in progress (adopted)",
                      runner_pid=rec.get("pid"), event="worker_tests_started", **known)
            return
        key = f"{w.task_id}:{head}"
        attempts = int(doc["attempts"].get(key) or 0)                 # per task and head, not per worker
        if entry and entry.get("head") == head and entry.get("state") == State.RUNNING:
            self._set(doc, changes, w, State.RUNNING, f"the runner (pid {dead_pid}) ended without a complete "
                      f"record; attempt {attempts} of {MAX_ATTEMPTS}", event="worker_tests_interrupted", **known)
        if attempts >= MAX_ATTEMPTS:
            blocked(f"{attempts} runs were interrupted or left no complete record for this head; run it by hand: "
                    f"python -B -m dev_supervisor.testreq run --task {w.task_id} --resume", attempts=attempts, **known)
            return
        self._launch(w, doc, changes, key, attempts, known)

    @staticmethod
    def _bound(rec: Optional[Dict[str, Any]], task_id: str, head: str, diff: str, texts: Any) -> bool:
        return bool(isinstance(rec, dict) and testreq._sealed(rec) and all(rec.get(k) == v for k, v in (
            ("schema", testreq.SCHEMA), ("task", task_id), ("head", head), ("diff_sha256", diff),
            ("reqs_sha256", testreq.reqs_hash(texts)))))

    def _launch(self, w: Any, doc: Dict[str, Any], changes: List[Dict[str, Any]], key: str, attempts: int,
                known: Dict[str, Any]) -> None:
        mine = doc["entries"].get(w.worker_id) or {}
        failed = parse_iso(mine.get("spawn_failed_at"))
        if failed is not None and mine.get("head") == known["head"] and self.clock() - failed < SPAWN_RETRY_S:
            return                                                       # the last start failed a moment ago: no retry storm
        others = [e for e in doc["entries"].values() if e.get("worker_id") != w.worker_id and self._live(e)]
        if others:
            self._set(doc, changes, w, State.PENDING, f"waiting for the test run of {others[0].get('task_id')}", **known)
            return
        floor = self.floor if self.floor is not None else memory_guard.min_free_mb()
        ok, _ = memory_guard.check(self.free_memory(), floor)
        if not ok:
            self._set(doc, changes, w, State.PENDING, f"waiting for memory: below the {floor} MB floor or unreadable", **known)
            return
        doc["attempts"][key] = attempts + 1
        while len(doc["attempts"]) > MAX_ATTEMPT_KEYS:
            doc["attempts"].pop(next(iter(doc["attempts"])))
        # Written before the process exists: a crash after the spawn and before the pid is saved is recovered by adoption.
        self._set(doc, changes, w, State.RUNNING, f"launching attempt {attempts + 1} of {MAX_ATTEMPTS}", attempts=attempts + 1,
                  runner_pid=None, launched_at=utc_iso(self.clock()), spawn_failed_at=None, emit=False, **known)
        self.store.write(DOC, doc)
        self._snapshot = json.dumps(doc, sort_keys=True)
        try:
            proc = self.spawn(w.task_id, w.worktree, str(self.store.directory), resume=attempts > 0, timeout=self.timeout,
                              base=self.base)
        except Exception as e:                                          # noqa: BLE001 - recorded, never raised into the loop
            doc["attempts"][key] = attempts                              # nothing ran: the attempt is not used
            self._set(doc, changes, w, State.BLOCKED, f"the runner could not be started ({type(e).__name__})", runner_pid=None,
                      attempts=attempts, spawn_failed_at=utc_iso(self.clock()), **known)
            return
        self._handles[proc.pid] = proc
        self._set(doc, changes, w, State.RUNNING, f"runner pid {proc.pid} started, attempt {attempts + 1} of {MAX_ATTEMPTS}",
                  runner_pid=proc.pid, event="worker_tests_started", **known)
