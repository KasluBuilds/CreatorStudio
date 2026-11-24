"""Worker records and the persistent worker registry (T-DEV-002, issue #44 part B).

A ``Worker`` is one supervised Claude Code process working on one task in one worktree. The ``WorkerRegistry`` keeps
workers, their reported results, contradictions and blockers in the machine-local ``StateStore`` document
``workers.json`` and reloads them in a fresh process (IMPLEMENTED, tested offline).

Result status is never promoted by the worker itself: a result is ``REPORTED`` until an injected verifier marks it
``VERIFIED`` or ``REJECTED`` (see ``lifecycle.Supervisor.verify_result``).
"""
from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Iterable, List, Optional

try:  # package import (normal) or flat import (module loaded standalone)
    from .state_store import StateStore, StoreError
except ImportError:  # pragma: no cover
    from state_store import StateStore, StoreError  # type: ignore

REGISTRY_DOC = "workers"


class WorkerStatus:
    ASSIGNED = "ASSIGNED"      # recorded, worktree checked, not launched
    STARTING = "STARTING"      # launch in progress (a crash here leaves an uncertain launch)
    RUNNING = "RUNNING"
    STALE = "STALE"            # running but no heartbeat within the stale window
    STOPPING = "STOPPING"      # graceful stop requested
    STOPPED = "STOPPED"        # stopped by the supervisor
    EXITED = "EXITED"          # process ended with exit code 0
    FAILED = "FAILED"          # process ended badly (non-zero exit, timeout, launch error)
    BLOCKED = "BLOCKED"        # restart budget exhausted or repeated identical failure: needs a human
    LOST = "LOST"              # process vanished while the supervisor was not watching

    ACTIVE = (ASSIGNED, STARTING, RUNNING, STALE, STOPPING)
    LIVE = (STARTING, RUNNING, STALE, STOPPING)
    ALL = (ASSIGNED, STARTING, RUNNING, STALE, STOPPING, STOPPED, EXITED, FAILED, BLOCKED, LOST)


class ResultStatus:
    REPORTED = "REPORTED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    ALL = (REPORTED, VERIFIED, REJECTED)


class DuplicateWorkerError(StoreError):
    """An active worker already holds this task or worktree."""


class UnknownWorkerError(StoreError):
    pass


@dataclass
class Worker:
    worker_id: str
    role: str
    task_id: str
    branch: str
    worktree: str
    owner_stream: str
    provider: str = "anthropic"
    model: str = ""
    status: str = WorkerStatus.ASSIGNED
    pid: Optional[int] = None
    session_id: Optional[str] = None
    prompt_file: Optional[str] = None
    assigned_at: Optional[str] = None
    started_at: Optional[str] = None
    last_heartbeat: Optional[str] = None
    ended_at: Optional[str] = None
    exit_code: Optional[int] = None
    restart_count: int = 0
    failures: List[str] = field(default_factory=list)       # failure signatures, oldest first
    next_restart_not_before: Optional[str] = None
    stop_requested_at: Optional[str] = None
    last_result_ref: Optional[str] = None
    previous_session_ids: List[str] = field(default_factory=list)
    note: str = ""
    allocation: Dict[str, Any] = field(default_factory=dict)   # route chosen by role (T-DEV-038); never a credential

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Worker":
        known = {f.name for f in fields(cls)}
        w = cls(**{k: v for k, v in d.items() if k in known})
        if w.status not in WorkerStatus.ALL:
            raise StoreError(f"worker {w.worker_id}: unknown status {w.status!r}")
        return w

    @property
    def active(self) -> bool:
        return self.status in WorkerStatus.ACTIVE


@dataclass
class ResultRecord:
    result_id: str
    worker_id: str
    task_id: str
    outcome: str                                   # what the worker claims, e.g. DONE / IN_PROGRESS / FAILED
    claims: Dict[str, str] = field(default_factory=dict)   # short factual claims, e.g. {"tests": "pass"}
    ref: Optional[str] = None                      # where the full report lives (a path or commit), never its contents
    status: str = ResultStatus.REPORTED
    reported_at: Optional[str] = None
    decided_at: Optional[str] = None
    verifier_note: str = ""
    contradiction: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ResultRecord":
        known = {f.name for f in fields(cls)}
        r = cls(**{k: v for k, v in d.items() if k in known})
        if r.status not in ResultStatus.ALL:
            raise StoreError(f"result {r.result_id}: unknown status {r.status!r}")
        return r


def norm_worktree(path: str) -> str:
    """Comparable form of a worktree path (case-insensitive and separator-insensitive on Windows)."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path))).rstrip("\\/")


class WorkerRegistry:
    """Workers, results, contradictions and blockers, persisted in one store document after every change."""

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.workers: Dict[str, Worker] = {}
        self.results: Dict[str, ResultRecord] = {}
        self.contradictions: List[Dict[str, Any]] = []
        self.blockers: List[Dict[str, Any]] = []
        self.load()

    # -------------------------------------------------- persistence

    def load(self) -> None:
        doc = self.store.read(REGISTRY_DOC, default=None) or {}
        self.workers = {k: Worker.from_dict(v) for k, v in (doc.get("workers") or {}).items()}
        self.results = {k: ResultRecord.from_dict(v) for k, v in (doc.get("results") or {}).items()}
        self.contradictions = list(doc.get("contradictions") or [])
        self.blockers = list(doc.get("blockers") or [])

    def save(self) -> None:
        self.store.write(REGISTRY_DOC, {
            "workers": {k: w.to_dict() for k, w in sorted(self.workers.items())},
            "results": {k: r.to_dict() for k, r in sorted(self.results.items())},
            "contradictions": self.contradictions,
            "blockers": self.blockers,
        })

    # -------------------------------------------------- workers

    def new_id(self, prefix: str = "w") -> str:
        while True:
            wid = f"{prefix}-{uuid.uuid4().hex[:10]}"
            if wid not in self.workers and wid not in self.results:
                return wid

    def find_conflict(self, task_id: str, worktree: str, ignore: Optional[str] = None) -> Optional[Worker]:
        wt = norm_worktree(worktree)
        for w in self.workers.values():
            if w.worker_id == ignore or not w.active:
                continue
            if w.task_id == task_id or norm_worktree(w.worktree) == wt:
                return w
        return None

    def add(self, worker: Worker) -> Worker:
        if worker.worker_id in self.workers:
            raise DuplicateWorkerError(f"worker id {worker.worker_id} already exists")
        other = self.find_conflict(worker.task_id, worker.worktree)
        if other is not None:
            raise DuplicateWorkerError(
                f"active worker {other.worker_id} ({other.status}) already holds task {other.task_id} "
                f"or worktree {other.worktree}")
        self.workers[worker.worker_id] = worker
        self.save()
        return worker

    def get(self, worker_id: str) -> Worker:
        try:
            return self.workers[worker_id]
        except KeyError:
            raise UnknownWorkerError(f"no worker {worker_id}") from None

    def update(self, worker: Worker) -> None:
        self.workers[worker.worker_id] = worker
        self.save()

    def list(self, statuses: Optional[Iterable[str]] = None) -> List[Worker]:
        st = set(statuses) if statuses is not None else None
        return [w for _, w in sorted(self.workers.items()) if st is None or w.status in st]

    def for_task(self, task_id: str) -> List[Worker]:
        return [w for w in self.list() if w.task_id == task_id]

    # -------------------------------------------------- results

    def results_for_task(self, task_id: str) -> List[ResultRecord]:
        return [r for _, r in sorted(self.results.items()) if r.task_id == task_id]
