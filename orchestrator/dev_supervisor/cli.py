"""Command line of the development supervisor (T-DEV-008, wave w03 lane L8). Run from ``orchestrator/``::

    python -B -m dev_supervisor <command> [options]

The single launch command (in a terminal opened by the launcher of your choice, Claude Pro or OmniRoute)::

    python -B -m dev_supervisor start --yes

Commands (every one is non-destructive unless ``--yes`` is given; exit 0 = ok, 1 = a check failed or the action was
refused, 2 = usage or configuration error):

- ``start``         show the Main launch plan; ``--yes`` launches the interactive Main and runs the supervisor loop
                    (console mode), or waits for the Main terminal host (attached mode)
- ``main-host``     attached mode: run the interactive Main inside THIS terminal tab and own it (``--yes``)
- ``status``        Main session, authority, workers, queued requests, lock holder (read only)
- ``workers``       the worker registry (read only)
- ``assign``        validate a worker assignment; ``--yes`` queues it for the supervisor loop (which spawns it)
- ``handoff``       build a handoff from a facts file; ``--yes`` writes and verifies it; ``--verify ID`` re-verifies
- ``compact-check`` Main context pressure and the compaction decision (exit 3 when pressure is UNAVAILABLE,
                    4 when COMPACT or REPLACE is due)
- ``replace``       verify a handoff and show the replacement plan; ``--yes`` replaces Main (through the loop when it
                    runs, else directly)
- ``report``        pending GitHub progress events; ``--yes`` flushes them to the configured issue
- ``snapshot``      the read-only Orbit snapshot JSON (stdout or ``--out``)
- ``route``         explain a routing decision for a task profile
- ``doctor``        check claude CLI, git, gh auth, OmniRoute, Resolve discovery, state dir, repo clean, worktrees
- ``config``        show the effective configuration, or ``--example`` for the template

Integration only: this module composes lanes L1-L7 (telemetry, state store, lifecycle, handoff, compaction,
replacement, routing, OmniRoute, Resolve discovery, GitHub log, Orbit snapshot) and never re-implements them.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from . import autonomy, compaction, config as config_mod, context_monitor, guard_loop, handoff, main_host, replacement, routing, telemetry
from . import authority, github_log, omniroute, provider_env, resolve_discovery, session_policy
from .config import Config, ConfigError
from .evidence import Evidence, unavailable
from .launch import (MAIN_ROLE, InteractiveMainLauncher, LaunchError, MainProfile, NewConsoleTerminal,
                     OwnedMainRetirer, SessionFileHeartbeat, Terminal, build_main_argv, find_session, idle_evidence,
                     read_main, write_main, write_main_fresh)
from .lifecycle import (ClaudeCliLauncher, GitWorktreeChecker, LifecycleError, ProcessLauncher, Supervisor,
                        SupervisorPolicy, build_claude_argv, pid_alive)
from .state_store import (LOCK_FILE, LockHeldError, StateStore, StoreError, UnsafeStoreLocation, check_location,
                          parse_iso, scrub, utc_iso)
from .workers import WorkerRegistry, WorkerStatus

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
EXIT_PRESSURE_UNAVAILABLE, EXIT_ACTION_DUE = 3, 4
REQUESTS_SUBDIR = "requests"
GITHUB_SUBDIR = "github_log"
HANDOFF_FRESH_FOR_REPLACE_S = 1800.0   # a handoff older than this is not used for an automatic replacement
REFRESH_BEFORE_REPLACE_S = 300.0       # a prepared handoff older than this is rewritten right before a replacement
HANDOFF_VERIFY_TTL_S = 120.0          # the loop re-verifies a handoff for the compaction decision at most this often
FACT_KEYS = ("objective", "decisions", "plan", "task_graph", "branches", "worktrees", "commits", "pull_requests",
             "tests", "evidence", "blockers", "next_actions")
RunFn = Callable[..., Tuple[int, str, str]]


def _snapshot_module():
    # dev_supervisor/__init__ re-exports telemetry.snapshot under the name ``snapshot``; import the submodule by path.
    return importlib.import_module(".snapshot", __package__)


# ------------------------------------------------------------------ injectable services

def run_command(argv: Sequence[str], timeout: float = 30.0, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run a program from an argument list (never a shell). Raises OSError when it cannot start."""
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=cwd, shell=False)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return r.returncode, r.stdout or "", r.stderr or ""


@dataclass
class Services:
    """Everything that touches the machine, injectable so tests use fakes."""
    config: Config
    terminal: Terminal
    worker_launcher_factory: Callable[[], ProcessLauncher]
    worktree_checker: Callable[[str, str], Tuple[bool, str]]
    ref_checkers_factory: Callable[[Config, Path], Any]
    run: RunFn
    which: Callable[[str], Optional[str]]
    omniroute_transport: Any
    resolve_discover: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]]
    gh_runner_factory: Callable[[Config], Any]
    read_sessions: Callable[..., List[Dict[str, Any]]] = telemetry.read_sessions
    pid_alive: Callable[[Optional[int]], bool] = pid_alive
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    session_factory: Callable[[], str] = lambda: str(uuid.uuid4())
    out: TextIO = field(default_factory=lambda: sys.stdout)
    env: Optional[Dict[str, str]] = None
    policy_install: Callable[..., Any] = session_policy.install
    timer: Callable[[], float] = time.monotonic          # tick duration measurement (injectable for tests)
    usage_cache: telemetry.UsageTailCache = field(default_factory=telemetry.UsageTailCache)
    main_spawner: Any = None          # attached mode: starts the interactive Main inside the host's terminal

    def store(self, create: bool = True) -> StateStore:
        root = self.config.repository_root()
        return StateStore(self.config.state_directory(self.env), repo_roots=[root] if root else [], clock=self.clock,
                          pid_alive=self.pid_alive, create=create)


def _real_ref_checkers(cfg: Config, root: Path) -> Any:
    return handoff.GitRefChecker(str(root), pr_checker=handoff.GhPrChecker(cfg.github_repo or None))


def _real_discover(identity: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return resolve_discovery.discover(config=identity)


def build_services(cfg: Config) -> Services:
    return Services(config=cfg, terminal=NewConsoleTerminal(),
                    worker_launcher_factory=lambda: ClaudeCliLauncher(cfg.claude_executable),
                    worktree_checker=GitWorktreeChecker(), ref_checkers_factory=_real_ref_checkers,
                    run=run_command, which=shutil.which, omniroute_transport=omniroute.urllib_transport,
                    resolve_discover=_real_discover,
                    gh_runner_factory=lambda c: github_log.GhRunner(repo=c.github_repo),
                    main_spawner=main_host.ForegroundSpawner())


# ------------------------------------------------------------------ worker supervisor with configured permissions

class ConfiguredSupervisor(Supervisor):
    """The L2 ``Supervisor`` with the worker permission mode from the machine-local config (L2 extension point:
    ``build_spec`` is overridden; the argv is still built by L2 ``build_claude_argv``)."""

    def __init__(self, *a: Any, permission_mode: Optional[str] = None, repo_root: Optional[Path] = None,
                 policy_install: Callable[..., Any] = session_policy.install, **kw: Any) -> None:
        self.permission_mode = permission_mode
        self.repo_root, self.policy_install = repo_root, policy_install
        super().__init__(*a, **kw)

    def build_spec(self, w):  # type: ignore[override]
        """Raises ``PolicyInstallError`` if the authority hook cannot be installed; ``_launch`` then fails the worker."""
        spec = super().build_spec(w)
        if self.permission_mode is not None:
            spec.argv = build_claude_argv(w.model, w.session_id or "", executable=self.executable,
                                          permission_mode=self.permission_mode)
        if self.repo_root is None:
            raise session_policy.PolicyInstallError("repository root unknown; refusing an unhooked worker launch")
        # T-DEV-028: a worker is confined to its own worktree. Its repo root is the worktree, not the main checkout
        # (OBSERVED: with repo_root = main repo a worker wrote into the main checkout).
        inst = self.policy_install(self.store.directory, w.session_id, "worker", str(w.worktree),
                                   roots=[w.worktree])
        spec.argv = session_policy.argv_with_policy(spec.argv, inst)
        spec.env = dict(inst.env)
        return spec


# ------------------------------------------------------------------ request queue (CLI -> running loop)

def _requests_dir(store: StateStore) -> Path:
    return store.directory / REQUESTS_SUBDIR


def enqueue(store: StateStore, op: str, payload: Dict[str, Any]) -> str:
    """Queue a request for the supervisor loop. The loop is the only writer of the worker registry while it runs."""
    rid = f"{int(store.clock() * 1000):015d}-{uuid.uuid4().hex[:8]}"
    d = _requests_dir(store)
    os.makedirs(str(d), exist_ok=True)
    body = scrub({"request_id": rid, "op": op, "payload": payload, "queued_at": utc_iso(store.clock())})
    store.atomic_write_bytes(d / f"{rid}.json", (json.dumps(body, sort_keys=True) + "\n").encode("utf-8"))
    store.append_event("request_queued", request_id=rid, op=op)
    return rid


def pending_requests(store: StateStore) -> List[Tuple[Path, Dict[str, Any]]]:
    d = _requests_dir(store)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            out.append((p, {"request_id": p.stem, "op": "unreadable", "payload": {}}))
    return out


def _finish_request(store: StateStore, path: Path, outcome: Dict[str, Any]) -> None:
    done = _requests_dir(store) / "done"
    os.makedirs(str(done), exist_ok=True)
    try:
        os.replace(str(path), str(done / path.name))
    except OSError:
        pass
    store.append_event("request_done", request_id=path.stem, **outcome)


def lock_holder(store: StateStore, alive: Callable[[Optional[int]], bool]) -> Optional[Dict[str, Any]]:
    try:
        held = json.loads((store.directory / LOCK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(held, dict):
        return None
    pid = held.get("pid")
    return {"pid": pid, "owner": held.get("owner"), "alive": bool(isinstance(pid, int) and alive(pid))}


# ------------------------------------------------------------------ helpers

def _pressure_config(cfg: Config) -> telemetry.PressureConfig:
    return telemetry.PressureConfig.from_config(cfg)


def _observe(svc: Services, session_id: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[Path]]:
    """(session file entry or None, context pressure dict, transcript path or None). Read only. The statusLine record
    Claude Code wrote for this session (OBSERVED) is preferred when fresh, else the transcript estimate (INFERRED)."""
    cfg = svc.config
    if not session_id:
        na = unavailable("no Main session recorded", "supervisor")
        return None, {k: na for k in ("tokens", "model", "window", "ratio", "level")}, None
    sess = find_session(svc.read_sessions(cfg.sessions_directory(), pid_alive=svc.pid_alive), session_id)
    cwd = sess["cwd"].value if sess is not None and sess["cwd"].available else None
    path = telemetry.find_transcript(cfg.projects_directory(), session_id, cwd)
    now = datetime.fromtimestamp(svc.clock(), tz=timezone.utc)
    pressure = telemetry.context_pressure(path, _pressure_config(cfg), cache=svc.usage_cache,
                                          context_dir=cfg.state_directory(svc.env) / telemetry.CONTEXT_SUBDIR,
                                          session_id=session_id, now=now)
    return sess, pressure, path


def main_observation(svc: Services, session_id: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """(session file entry or None, context pressure dict) for the Main session. Read only."""
    sess, pressure, _path = _observe(svc, session_id)
    return sess, pressure


def _ser(x: Any) -> Any:
    if isinstance(x, Evidence):
        return x.to_dict()
    if isinstance(x, dict):
        return {k: _ser(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_ser(v) for v in x]
    return x


def _ev_text(ev: Any) -> str:
    if not isinstance(ev, Evidence):
        return "UNAVAILABLE"
    return f"{ev.value} [{ev.label.value}]" if ev.available else f"UNAVAILABLE ({ev.reason})"


def _print(svc: Services, data: Any, as_json: bool, text: Optional[str] = None) -> None:
    if as_json or text is None:
        svc.out.write(json.dumps(_ser(data), indent=2, sort_keys=True, default=str) + "\n")
    else:
        svc.out.write(text.rstrip("\n") + "\n")


def _short(session_id: Optional[str]) -> str:
    """First 8 characters of a session id: full UUIDs look like opaque tokens to the public-log scrubber."""
    return str(session_id)[:8] if session_id else "none"


def _logger(svc: Services, store: StateStore) -> Optional[github_log.GitHubLogger]:
    if not svc.config.github_issue:
        return None
    return github_log.GitHubLogger(svc.gh_runner_factory(svc.config), store.directory / GITHUB_SUBDIR,
                                   default_issue=svc.config.github_issue, sleep=svc.sleep)


def _gh_event(svc: Services, store: StateStore, kind: github_log.EventKind, task_id: str, title: str,
              detail: str = "", evidence: Sequence[str] = ()) -> str:
    """Spool a progress event for the GitHub log (posted by ``report --yes``). Never raises."""
    lg = _logger(svc, store)
    if lg is None:
        return "no_issue_configured"
    try:
        return lg.log(github_log.Event(kind=kind, task_id=task_id, title=title, detail=detail,
                                       evidence=tuple(evidence)), flush=False)
    except Exception as e:  # noqa: BLE001 - the GitHub log must never break supervision
        store.append_event("github_log_error", error=e.__class__.__name__)
        return "error"


def _repo_root(cfg: Config) -> Path:
    root = cfg.repository_root()
    if root is None:
        raise ConfigError("repository root unknown: set repo_root in the machine-local config")
    return root


HEARTBEAT_FILE = "supervisor_heartbeat.json"


def _ms(seconds: float) -> float:
    return round(max(0.0, float(seconds)) * 1000.0, 1)


def heartbeat_view(store: StateStore, now: float) -> Dict[str, Any]:
    """Persisted supervisor heartbeat with its age: MISSING (absent/unreadable), STALE (> 3 x poll), else FRESH."""
    try:
        hb = json.loads((store.directory / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        ts = parse_iso(hb["utc"])
        interval = float(hb["poll_interval_s"])
        if ts is None:
            raise ValueError("bad timestamp")
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "MISSING"}
    age = max(0.0, now - ts)
    try:                                   # a slow but alive loop (one tick takes longer than the poll) is not STALE
        last = max(0.0, float(hb.get("duration_ms") or 0.0)) / 1000.0
    except (TypeError, ValueError):
        last = 0.0
    limit = max(3 * interval, 3 * last)
    return {**hb, "state": "STALE" if age > limit else "FRESH", "age_s": round(age, 1), "stale_after_s": round(limit, 1)}


def _heartbeat_text(hb: Dict[str, Any]) -> str:
    if hb["state"] == "MISSING":
        return "MISSING (no supervisor loop has written one)"
    dur = hb.get("duration_ms")
    return (f"{hb['state']} age {hb['age_s']}s tick {hb.get('tick')} pid {hb.get('pid')} "
            f"main_alive={hb.get('main_alive')} workers={hb.get('worker_count')} "
            f"last_tick_duration_ms={dur if dur is not None else 'unknown'}")


def _authoritative_main(store: StateStore) -> Optional[str]:
    if read_main(store).get("status") in ("EXITED", "AWAITING_HOST"):
        # A Main whose process ended is never authoritative, and neither is the state a restarted supervisor writes for
        # a dead Main in attached mode (AWAITING_HOST): the session record stays ACTIVE until a host adopts the next Main.
        return None
    doc = replacement.load_sessions(store)
    for sid, rec in doc["sessions"].items():
        if rec.get("role") == MAIN_ROLE and rec.get("authoritative") and rec.get("status") in (
                replacement.SessionStatus.ACTIVE, replacement.SessionStatus.RETIRING):
            return sid
    return None


def latest_handoff_for(store: StateStore, session_id: str) -> Optional[Tuple[str, float]]:
    """Newest handoff written for ``session_id`` according to the event log: (handoff_id, written_ts)."""
    events, _bad = store.read_events()
    best = None
    for ev in events:
        if ev.get("kind") != "handoff_written":
            continue
        data = ev.get("data") or {}
        if data.get("session_id") != session_id or not data.get("handoff_id"):
            continue
        ts = parse_iso(ev.get("ts")) or 0.0
        if best is None or ts >= best[1]:
            best = (data["handoff_id"], ts)
    return best


# ------------------------------------------------------------------ the supervisor loop

class _PromotingRetirer:
    """Wraps the Main retirer (T-DEV-030). Right before the old Main is retired, and never earlier, main.json is
    pointed at the NEW session as RUNNING, after re-reading the sessions record: the new session must be the
    authoritative ACTIVE one. Otherwise nothing is retired and the old Main stays."""

    def __init__(self, loop: "SupervisorLoop", inner: Any, old: str, handoff_id: str) -> None:
        self.loop, self.inner, self.old, self.handoff_id = loop, inner, old, handoff_id

    def retire(self, session_id: str) -> Tuple[bool, str]:
        store = self.loop.store
        recs = replacement.load_sessions(store)["sessions"]
        new = (recs.get(self.old) or {}).get("replaced_by")
        rec = recs.get(new) or {}
        if not new or rec.get("authoritative") is not True or rec.get("status") != replacement.SessionStatus.ACTIVE:
            return False, "new Main is not recorded authoritative and ACTIVE; old Main not retired"
        pid = next((a.get("pid") for a in self.loop.main_launcher.launched if a.get("session_id") == new), None)
        write_main(store, session_id=new, status="RUNNING", pid=pid, replaced_session_id=self.old,
                   handoff_id=self.handoff_id, launched_at=utc_iso(self.loop.svc.clock()),
                   autocompact_pct=self.loop._launched_pct(new))
        store.append_event("main_promoted", session_id=new, replaced_session_id=self.old)
        return self.inner.retire(session_id)


class SupervisorLoop:
    """Owns the lock, the worker supervisor and the interactive Main for one ``start`` run."""

    def __init__(self, svc: Services, store: StateStore) -> None:
        self.svc, self.store, self.cfg = svc, store, svc.config
        self.root = _repo_root(self.cfg)
        policy = SupervisorPolicy(max_runtime_s=float(self.cfg.worker_max_runtime_s))
        self.sup = ConfiguredSupervisor(store, svc.worker_launcher_factory(), worktree_checker=svc.worktree_checker,
                                        clock=svc.clock, sleep=svc.sleep, policy=policy, pid_probe=svc.pid_alive,
                                        session_factory=svc.session_factory, executable=self.cfg.claude_executable,
                                        permission_mode=self.cfg.worker_permission_mode, repo_root=self.root,
                                        policy_install=svc.policy_install)
        self.main_launcher = InteractiveMainLauncher(store, svc.terminal, MainProfile.from_config(self.cfg),
                                                     str(self.root), policy_install=svc.policy_install)
        self.attached: Optional[main_host.AttachedMain] = None
        if self.cfg.main_launch_mode == "attached":
            self.attached = main_host.AttachedMain(store, self.cfg, read_sessions=svc.read_sessions,
                                                   pid_alive=svc.pid_alive, clock=svc.clock, sleep=svc.sleep)
        self.last_action: Optional[str] = None
        self.tracker = compaction.ContextTracker(store)
        self._boundary_cache: Dict[str, Tuple[Tuple[int, int], Optional[float]]] = {}
        self._verify_cache: Dict[str, Tuple[float, Optional[Any]]] = {}
        self.ticks = 0
        self.watch_main = True       # False with start --no-main: the loop supervises workers only

    def close(self) -> None:
        self.sup.close()

    # -------------------------------------------------- Main

    def _launched_pct(self, sid: Optional[str]) -> Optional[int]:
        """The CLAUDE_AUTOCOMPACT_PCT_OVERRIDE value Main ``sid`` was actually launched with (None: none/unknown)."""
        for a in reversed(self.main_launcher.launched):
            if a.get("session_id") == sid:
                return a.get("autocompact_pct")
        return None

    def main_alive(self, main: Dict[str, Any]) -> bool:
        pid = main.get("pid")
        if not isinstance(pid, int):
            return False
        if self.main_launcher.owns(pid):
            return self.main_launcher.poll(pid) is None
        return bool(self.svc.pid_alive(pid))

    def ensure_main(self) -> Dict[str, Any]:
        main = read_main(self.store)
        if main.get("session_id") and main.get("status") == "RUNNING" and self.main_alive(main):
            self.store.append_event("main_observed_existing", session_id=main["session_id"], pid=main.get("pid"))
            return main
        if self.attached is not None:
            # Attached mode: the supervisor NEVER launches Main. It adopts what the host runs, or waits for the host.
            self.attached.safe_reconcile()
            main = read_main(self.store)
            if main.get("status") == "RUNNING" and self.main_alive(main):
                return main
            if main.get("status") == "IDENTITY_MISMATCH":
                return main
            if self.main_alive(main) and main.get("status") != "AWAITING_HOST":
                # A recorded Main process is alive although its record is not RUNNING: never erase that pid.
                self.store.append_event("main_awaiting_host_kept_live_pid", status=main.get("status"), pid=main.get("pid"))
                return main
            main = write_main(self.store, status="AWAITING_HOST", pid=None)
            self.store.append_event("main_awaiting_host")
            return main
        host_child = (main_host.read_host(self.store).get("child") or {}).get("pid")
        if host_child and self.svc.pid_alive(host_child):
            raise LaunchError(f"a Main is running under a terminal host (pid {host_child}); refusing to launch a "
                              "second one from console mode")
        old_sid = main.get("session_id")
        if old_sid:                      # a recorded Main that is no longer running must lose authority (R3)
            _mark_exited_unless_retired(self.store, old_sid)
            self.store.append_event("main_exited", session_id=old_sid)
        sid = str(uuid.UUID(str(self.svc.session_factory())))
        write_main_fresh(self.store, session_id=sid, status="STARTING", pid=None, model=self.cfg.model(MAIN_ROLE))
        self.store.append_event("main_launch_begin", session_id=sid)
        try:
            pid, _argv, prompt = self.main_launcher.open_main(sid)
        except Exception as e:  # noqa: BLE001
            write_main(self.store, status="FAILED", failure=e.__class__.__name__)
            self.store.append_event("main_launch_failed", session_id=sid, error=e.__class__.__name__)
            raise
        main = write_main(self.store, status="RUNNING", pid=pid, launched_at=utc_iso(self.svc.clock()),
                          prompt_file=prompt.name, autocompact_pct=self._launched_pct(sid))
        replacement.register_session(self.store, sid, MAIN_ROLE)
        _gh_event(self.svc, self.store, github_log.EventKind.TASK_STARTED, "dev-supervisor", "Main session started",
                  evidence=(f"session {_short(sid)}", f"model {self.cfg.model(MAIN_ROLE)}"))
        return main

    # -------------------------------------------------- requests

    def process_requests(self) -> List[Dict[str, Any]]:
        results = []
        for path, req in pending_requests(self.store):
            op, p = req.get("op"), req.get("payload") or {}
            try:
                if op == "assign":
                    w = self.sup.assign(task_id=p["task_id"], role=p["role"], branch=p["branch"],
                                        worktree=p["worktree"], owner_stream=p["owner_stream"], model=p["model"],
                                        prompt_file=p["prompt_file"], provider=p.get("provider", "anthropic"))
                    w = self.sup.spawn(w.worker_id)
                    outcome = {"ok": w.status == WorkerStatus.RUNNING, "worker_id": w.worker_id, "status": w.status}
                    _gh_event(self.svc, self.store, github_log.EventKind.STEP_DONE, p["task_id"],
                              "worker spawned" if outcome["ok"] else "worker launch failed",
                              evidence=(f"worker {w.worker_id}", f"status {w.status}", f"model {w.model}"))
                elif op == "replace":
                    res = self.replace_main(p["handoff_id"])
                    outcome = {"ok": res.get("status") in (replacement.ReplacementStatus.REPLACED,
                                                           replacement.ReplacementStatus.REPLACED_OLD_NOT_CLEARED),
                               "status": res.get("status")}
                else:
                    outcome = {"ok": False, "error": f"unknown op {op!r}"}
            except (KeyError, TypeError) as e:
                outcome = {"ok": False, "error": f"malformed request ({e.__class__.__name__})"}
            except (StoreError, LaunchError, ConfigError, OSError) as e:
                outcome = {"ok": False, "error": f"{e.__class__.__name__}: {str(e)[:160]}"}
            _finish_request(self.store, path, {"op": op, **outcome})
            results.append({"request_id": path.stem, "op": op, **outcome})
        return results

    # -------------------------------------------------- replacement

    def replace_main(self, handoff_id: str) -> Dict[str, Any]:
        main = read_main(self.store)
        old = main.get("session_id")
        if not old:
            return {"status": "REFUSED", "reasons": ["no Main session recorded"]}
        if self.attached is not None:
            return self._replace_attached(main, old, handoff_id)
        checkers = self.svc.ref_checkers_factory(self.cfg, self.root)
        rep = handoff.verify_handoff(self.store, handoff_id, checkers)
        if not rep.safe:
            return {"status": replacement.ReplacementStatus.REFUSED_HANDOFF_NOT_SAFE, "reasons": rep.reasons}
        retirer = _PromotingRetirer(self, OwnedMainRetirer(self.main_launcher,
                                                           lambda sid: main.get("pid") if sid == old else None),
                                    old, handoff_id)
        heartbeat = SessionFileHeartbeat(self.cfg.sessions_directory(), self.svc.read_sessions, self.svc.pid_alive,
                                         self.svc.clock)
        res = replacement.replace_session(self.store, self.main_launcher, old_session_id=old, role=MAIN_ROLE,
                                          model=self.cfg.model(MAIN_ROLE), cwd=str(self.root), verified=rep.verified,
                                          checkers=checkers, heartbeat=heartbeat, retirer=retirer,
                                          clock=self.svc.clock, sleep=self.svc.sleep,
                                          session_factory=self.svc.session_factory,
                                          executable=self.cfg.claude_executable)
        if res.new_session_id:
            pid = next((a.get("pid") for a in res.attempts if a.get("session_id") == res.new_session_id), None)
            write_main(self.store, session_id=res.new_session_id, status="RUNNING", pid=pid,
                       replaced_session_id=old, handoff_id=handoff_id, launched_at=utc_iso(self.svc.clock()),
                       autocompact_pct=self._launched_pct(res.new_session_id))
        _gh_event(self.svc, self.store, github_log.EventKind.HANDOFF, "dev-supervisor",
                  f"Main replacement: {res.status}",
                  detail=replacement.USAGE_NOTE,
                  evidence=(f"handoff {handoff_id}", f"old session {_short(old)}",
                            f"new session {_short(res.new_session_id)}"))
        return res.to_dict()

    def _verified(self, hid: str) -> Optional[bool]:
        """Handoff verification for the compaction decision, reused for HANDOFF_VERIFY_TTL_S. Verification runs git
        and gh ref checkers; while Main sits at compact/replace pressure that used to run on EVERY tick. The actual
        replacement still verifies afresh (``replace_main``), so a cached answer never authorises anything."""
        now = self.svc.clock()
        hit = self._verify_cache.get(hid)
        if hit is not None and 0 <= now - hit[0] < HANDOFF_VERIFY_TTL_S:
            return hit[1]
        rep = handoff.verify_handoff(self.store, hid, self.svc.ref_checkers_factory(self.cfg, self.root))
        result = rep.verified if rep.safe else None
        self._verify_cache = {hid: (now, result)}          # only the newest handoff matters
        return result

    # -------------------------------------------------- context management (T-DEV-030)

    def _open_prs(self) -> List[Dict[str, Any]]:
        argv = [self.svc.which("gh") or "gh", "pr", "list", "--state", "open", "--json", "number,state,headRefName",
                "--limit", "50"] + (["--repo", self.cfg.github_repo] if self.cfg.github_repo else [])
        code, out, _err = self.svc.run(argv, timeout=30.0, cwd=str(self.root))
        data = json.loads(out) if code == 0 else None
        if not isinstance(data, list):
            raise RuntimeError("gh pr list returned no list")
        return [p for p in data if isinstance(p, dict)]

    def _head_of(self, branch: str) -> Optional[str]:
        if not handoff._BRANCH.match(branch) or ".." in branch:
            return None
        code, out, _err = self.svc.run(["git", "-C", str(self.root), "rev-parse", "--verify", "--quiet",
                                        f"refs/heads/{branch}"], timeout=20.0, cwd=str(self.root))
        sha = (out or "").strip()
        return sha if code == 0 and handoff._SHA.match(sha) else None

    def make_handoff(self, sid: str, pressure: Dict[str, Any], max_age_s: float) -> Tuple[Optional[str], bool]:
        """(handoff_id, adopted). A handoff for this session younger than ``max_age_s`` that verifies is adopted
        (this also makes a restart after a crash reuse the earlier one); otherwise a MECHANICAL handoff is built from
        recorded state only, written and verified by read-back. (None, False) when that fails (event recorded)."""
        now = self.svc.clock()
        latest = latest_handoff_for(self.store, sid)
        if latest is not None and 0 <= now - latest[1] <= max_age_s and self._verified(latest[0]) is not None:
            return latest[0], True
        events, _bad = self.store.read_events()
        approvals = authority.list_approvals(str(session_policy.approvals_dir(self.store.directory)))
        h, last = None, ""
        for with_titles in (True, False):
            try:
                h = handoff.build_mechanical_handoff(
                    session_id=sid, pressure=pressure, tasks=handoff.read_active_tasks(self.root / "ACTIVE_TASKS.json"),
                    workers=self.sup.registry.list(), events=events[-80:], approvals=approvals,
                    pr_lister=self._open_prs if self.svc.which("gh") else None, head_of=self._head_of,
                    usage=_usage_evidence(_account_usage(self.store)), with_titles=with_titles,
                    created_at=utc_iso(now))
                break
            except handoff.HandoffError as e:
                last = str(e)[:160]
        if h is None:
            self.store.append_event("handoff_prepare_failed", session_id=sid, stage="build", error=last)
            return None, False
        rec = handoff.write_handoff(self.store, h)
        rep = handoff.verify_handoff(self.store, rec.handoff_id, self.svc.ref_checkers_factory(self.cfg, self.root),
                                     expected_hash=rec.content_hash)
        if not rep.safe:
            self.store.append_event("handoff_prepare_failed", session_id=sid, stage="verify",
                                    handoff_id=rec.handoff_id, reasons=rep.reasons[:5])
            return None, False
        self._verify_cache = {rec.handoff_id: (now, rep.verified)}
        return rec.handoff_id, False

    def _boundary(self, path: Optional[Path]) -> Optional[float]:
        """Newest compact_boundary time of the Main transcript; re-read only when the file changed."""
        if path is None:
            return None
        try:
            st = os.stat(str(path))
        except OSError:
            return None
        hit = self._boundary_cache.get(str(path))
        if hit is not None and hit[0] == (st.st_size, st.st_mtime_ns):
            return hit[1]
        res = telemetry.last_compact_boundary(path)
        self._boundary_cache[str(path)] = ((st.st_size, st.st_mtime_ns), res)
        return res

    def context_step(self, sid: str, pressure: Dict[str, Any], path: Optional[Path]) -> Dict[str, Any]:
        """Prepare the durable handoff at >= prepare_handoff (once per crossing, refreshed after 30 min) and follow a
        native compaction at >= compact (COMPACTED / COMPACT_FAILED). State is persisted; a restart repeats nothing."""
        now = self.svc.clock()
        st = self.tracker.load(sid)
        before = json.dumps(st, sort_keys=True)
        lv, ra = pressure.get("level"), pressure.get("ratio")
        level = lv.value if isinstance(lv, Evidence) and lv.available else "ok"
        ratio = ra.value if isinstance(ra, Evidence) and ra.available else None
        label = ra.label.value if isinstance(ra, Evidence) else "UNAVAILABLE"
        out = {"level": level, "handoff_id": st["prepare"]["handoff_id"], "compact_failed": False, "events": []}
        if ratio is None:                       # UNAVAILABLE pressure: no figure, no action
            return out
        pct = round(float(ratio) * 100, 1)
        act = self.tracker.prepare_action(st, level, now)
        p = st["prepare"]
        if act in ("prepare", "refresh") and now - float(p.get("failed_at") or -1e9) >= 120.0:
            hid, adopted = self.make_handoff(sid, pressure, -1.0 if act == "refresh" else compaction.PREPARE_REFRESH_S)
            if hid:
                self._note_prepared(st, sid, hid, adopted, pct, now, "refresh" if act == "refresh" else "crossing")
            else:
                p["failed_at"] = now
        elif act == "clear":
            p["active"] = False
        rank = compaction.LEVEL_RANK.get(level, 0)
        c = st["compact"]
        if c["state"] != "none" or rank >= compaction.LEVEL_RANK["compact"]:
            tracked = c["state"] != "none"
            tok = pressure.get("tokens")
            key = ra.timestamp if label == "OBSERVED" else f"tokens:{tok.value if isinstance(tok, Evidence) else None}"
            events = self.tracker.step_compaction(
                st, level=level, ratio=float(ratio), label=label, obs_key=key,
                boundary_ts=self._boundary(path) if tracked else None,
                started_ts=context_monitor.read_started(self.store.directory, sid) if tracked else None,
                now=now, k=int(self.cfg.compact_failed_observations), launched_ratio=self._main_launched_ratio(sid))
            for kind, data in events:
                self.store.append_event(kind, **data)
                out["events"].append(kind)
                if kind in ("COMPACTED", "COMPACT_FAILED"):
                    _gh_event(self.svc, self.store, github_log.EventKind.COMPACTION, "dev-supervisor",
                              f"Main {kind}", evidence=(f"ratio {ratio}", f"label {label}"))
        out["compact_failed"] = st["compact"]["state"] == "failed"
        replace_due = rank >= compaction.LEVEL_RANK["replace"] or (rank >= compaction.LEVEL_RANK["compact"]
                                                                   and out["compact_failed"])
        if replace_due and (not p["handoff_id"] or now - float(p["prepared_at"] or 0) > REFRESH_BEFORE_REPLACE_S):
            hid, adopted = self.make_handoff(sid, pressure, REFRESH_BEFORE_REPLACE_S)   # refresh before replacing
            if hid:
                self._note_prepared(st, sid, hid, adopted, pct, now, "pre_replace")
        out["handoff_id"] = p["handoff_id"] if p["handoff_id"] else None
        if json.dumps(st, sort_keys=True) != before:
            self.tracker.save(st)
        return out

    def _handoff_fresh(self, sid: str) -> bool:
        """A replacement may only use a handoff prepared within the refresh-before-replace window (never older than
        HANDOFF_FRESH_FOR_REPLACE_S); a stale one is not handed to ``decide``, which then blocks."""
        p = self.tracker.load(sid)["prepare"]
        if not p.get("handoff_id") or p.get("prepared_at") is None:
            return False
        age = self.svc.clock() - float(p["prepared_at"])
        return 0 <= age <= min(REFRESH_BEFORE_REPLACE_S, HANDOFF_FRESH_FOR_REPLACE_S)

    def _main_launched_ratio(self, sid: str) -> Optional[float]:
        main = read_main(self.store)
        pct = main.get("autocompact_pct") if main.get("session_id") == sid else None
        if pct is None:
            pct = self._launched_pct(sid)
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            return float(pct) / 100.0
        # No recorded CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (e.g. a Main launched before T-DEV-030): Claude Code was never asked
        # to compact early, so native compaction is only expected near the full window. Never count COMPACT_FAILED for
        # it below that; replacement still happens at the replace threshold.
        return 1.0

    def _note_prepared(self, st: Dict[str, Any], sid: str, hid: str, adopted: bool, pct: float, now: float,
                       reason: str) -> None:
        p = st["prepare"]
        p.update(active=True, handoff_id=hid, prepared_at=now, count=int(p.get("count") or 0) + 1, failed_at=None)
        self.store.append_event("handoff_prepared", session_id=sid, handoff_id=hid, percent=pct, adopted=adopted,
                                reason=reason, verified=True)
        try:
            context_monitor.write_notice(self.store.directory, sid, hid, pct, refreshed=reason != "crossing")
        except OSError as e:
            self.store.append_event("context_notice_failed", error=e.__class__.__name__)
        self.tracker.save(st)               # persist before anything that can fail afterwards (crash recovery)

    def _replace_attached(self, main: Dict[str, Any], old: str, handoff_id: str) -> Dict[str, Any]:
        """Replacement through the Main terminal host. The old Main is only ended by the host AFTER its preflight; if the
        new Main is not confirmed the host resumes the old session (``recover_after_failed_replace``)."""
        att = self.attached
        checkers = self.svc.ref_checkers_factory(self.cfg, self.root)
        rep = handoff.verify_handoff(self.store, handoff_id, checkers)
        if not rep.safe:
            return {"status": replacement.ReplacementStatus.REFUSED_HANDOFF_NOT_SAFE, "reasons": rep.reasons}
        att.launcher.begin(old, handoff_id)
        res = replacement.replace_session(self.store, att.launcher, old_session_id=old, role=MAIN_ROLE,
                                          model=self.cfg.model(MAIN_ROLE), cwd=str(self.root), verified=rep.verified,
                                          checkers=checkers, heartbeat=att.heartbeat_probe(), retirer=att.retirer,
                                          clock=self.svc.clock, sleep=self.svc.sleep,
                                          session_factory=self.svc.session_factory,
                                          executable=self.cfg.claude_executable)
        out = res.to_dict()
        if res.new_session_id:
            pid = next((a.get("pid") for a in res.attempts if a.get("session_id") == res.new_session_id), None)
            write_main(self.store, session_id=res.new_session_id, status="RUNNING", pid=pid,
                       replaced_session_id=old, handoff_id=handoff_id, launched_at=utc_iso(self.svc.clock()),
                       adopted_from_host=False, host_note=None)
            out["settle"] = "DONE" if att.settle(res.new_session_id) else "UNCONFIRMED"
            out["authoritative_session_id"] = res.new_session_id
        elif res.status == replacement.ReplacementStatus.ROLLED_BACK:
            rec = att.recover_after_failed_replace(old)
            out["recovery"] = rec
            auth = read_main(self.store).get("session_id")
            out["authoritative_session_id"] = auth
            if rec.get("outcome") == "HOST_HAS_OTHER_MAIN" and auth and auth != old:
                # The host finished a handover the supervisor had given up on. The reported result must agree with the
                # authoritative Main, so it is not "rolled back".
                out["status"] = "REPLACED_LATE_HANDOVER"
                out["new_session_id"] = auth
                out["reasons"] = list(out.get("reasons") or []) + [
                    "the host completed the handover after the supervisor stopped waiting; the host's Main was adopted "
                    "from its session file (not confirmed by the replacement heartbeat)"]
        _gh_event(self.svc, self.store, github_log.EventKind.HANDOFF, "dev-supervisor",
                  f"Main replacement (terminal host): {out.get('status')}", detail=replacement.USAGE_NOTE,
                  evidence=(f"handoff {handoff_id}", f"old session {_short(old)}",
                            f"new session {_short(res.new_session_id)}",
                            f"recovery {(out.get('recovery') or {}).get('outcome', 'none')}"))
        return out

    def check_pressure(self) -> Dict[str, Any]:
        main = read_main(self.store)
        sid = main.get("session_id")
        sess, pressure, tpath = _observe(self.svc, sid)
        idle = idle_evidence(sess)
        verified, hid = None, None
        level = pressure.get("level")
        ctx = {"compact_failed": False, "handoff_id": None}
        if sid:
            try:
                ctx = self.context_step(sid, pressure, tpath)
            except Exception as e:  # noqa: BLE001 - context management must never stop the supervisor loop
                self.store.append_event("context_management_error", error=e.__class__.__name__, detail=str(e)[:160])
        due = isinstance(level, Evidence) and level.available and level.value in ("compact", "replace")
        if due and sid and ctx.get("handoff_id") and self._handoff_fresh(sid):
            hid = ctx["handoff_id"]
            verified = self._verified(hid)
        dec = compaction.decide(pressure, idle=idle, verified=verified, config=_pressure_config(self.cfg),
                                now=self.svc.clock(), session_id=sid or "")
        if dec.action != self.last_action:
            compaction.record_decision(self.store, dec)
            if dec.action != compaction.NONE:
                _gh_event(self.svc, self.store, github_log.EventKind.COMPACTION, "dev-supervisor",
                          f"Main context pressure: {dec.action}",
                          evidence=(f"ratio {dec.ratio}", f"allowed {dec.allowed}",
                                    f"blocked_by {','.join(dec.blocked_by) or 'none'}"))
            self.last_action = dec.action
        out = {"decision": dec.to_dict(), "replacement": None, "context": ctx}
        # COMPACT is handled natively by Claude Code (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE at launch); Main is replaced only at
        # REPLACE, or at COMPACT after COMPACT_FAILED. Both need an idle Main and a fresh verified handoff (``allowed``).
        replace_due = dec.action == compaction.REPLACE or (dec.action == compaction.COMPACT
                                                           and ctx.get("compact_failed") is True)
        if dec.allowed and replace_due and self.cfg.auto_replace_main and hid:
            out["replacement"] = self.replace_main(hid)
            self.last_action = None
        return out

    # -------------------------------------------------- loop

    def _guard_tick(self, main: Dict[str, Any], alive: bool, pressure: Any) -> None:
        """Feature-flagged progress guard (T-DEV-037). Off unless CREATORSTUDIO_SUPERVISOR_GUARD is "1".
        Never raises: a guard failure must not stop supervision."""
        if not guard_loop.enabled():
            return
        try:
            if getattr(self, "_guard", None) is None:
                self._guard = guard_loop.GuardAdapter(
                    self.store.directory, clock=self.svc.clock, request_handover=self._guard_request_handover,
                    event=self.store.append_event)
            self._guard.tick(self._guard_signals(main, alive))
        except Exception as e:  # noqa: BLE001 - the guard is advisory and must never stop the loop
            self.store.append_event("guard_error", error=e.__class__.__name__, detail=str(e)[:160])

    def _guard_signals(self, main: Dict[str, Any], alive: bool) -> "guard_loop.Signals":
        sid = main.get("session_id") if isinstance(main, dict) else None
        self._guard_sid = sid
        sess = None
        if sid:
            sess = find_session(self.svc.read_sessions(self.cfg.sessions_directory(), pid_alive=self.svc.pid_alive), sid)
        ev = idle_evidence(sess)
        status = ("idle" if ev.value else "busy") if (ev.available and isinstance(ev.value, bool)) else None
        picture = autonomy.compute_items(self.root, self.store)
        return guard_loop.Signals(
            session_id=sid if isinstance(sid, str) else None,
            main_alive=alive if isinstance(alive, bool) else None, session_status=status,
            eligible_work=len(picture["items"]), progress_marker=picture["fingerprint"],
            pressure_pct=None,   # not wired yet: context pressure is held by check_pressure, not returned here
            handoff_verified=bool(sid and self._handoff_fresh(sid)))

    def _guard_request_handover(self, signals: "guard_loop.Signals") -> Dict[str, Any]:
        """Replacement only through the host's supported handover command; the host checks ownership itself."""
        sid = getattr(self, "_guard_sid", None)
        if not (signals.handoff_verified and sid):
            return {"sent": False, "reason": "no verified handoff for the current Main"}
        new_sid = self.svc.session_factory()
        cid = main_host.send_command(self.store, "handover", {"old_session_id": sid, "new_session_id": new_sid},
                                     float(self.cfg.host_command_timeout_s), supervisor_pid=os.getpid())
        return {"sent": True, "cid": cid}

    def tick(self) -> Dict[str, Any]:
        self.ticks += 1
        timer = self.svc.timer
        t0 = timer()
        if self.attached is not None:
            self.attached.safe_reconcile()
        requests = self.process_requests()
        t1 = timer()
        workers = self.sup.tick()
        t2 = timer()
        main = read_main(self.store)
        alive = self.main_alive(main)
        pressure = None
        if alive:
            try:
                pressure = self.check_pressure()
            except Exception as e:  # noqa: BLE001 - one bad observation must not stop supervision (T-DEV-030)
                self.store.append_event("context_management_error", error=e.__class__.__name__, detail=str(e)[:160])
        t3 = timer()
        if not alive and main.get("status") == "RUNNING":
            write_main(self.store, status="EXITED", ended_at=utc_iso(self.svc.clock()))
            self.store.append_event("main_exited", session_id=main.get("session_id"))
            if main.get("session_id") and self.attached is None:
                # Attached mode: the old session's retirement belongs to the host's adoption of the next Main, which
                # needs the session still RETIRING/ACTIVE to record it as RETIRED (see main_host.MainHost._adopt).
                _mark_exited_unless_retired(self.store, main["session_id"])
        if guard_loop.enabled():
            self._guard_tick(main, alive, pressure)
        phases = {"requests": _ms(t1 - t0), "workers": _ms(t2 - t1), "main_and_pressure": _ms(t3 - t2)}
        self.write_heartbeat(alive, main.get("session_id"), len(self.sup.registry.list()),
                             duration_ms=_ms(timer() - t0), phase_ms=phases)
        return {"tick": self.ticks, "requests": requests, "workers": workers, "main_alive": alive,
                "pressure": pressure}

    def write_heartbeat(self, main_alive: bool, main_session_id: Optional[str], worker_count: int,
                        duration_ms: Optional[float] = None, phase_ms: Optional[Dict[str, float]] = None) -> None:
        body = {"pid": os.getpid(), "tick": self.ticks, "utc": utc_iso(self.svc.clock()),
                "main_alive": bool(main_alive), "main_session_id": main_session_id, "worker_count": worker_count,
                "poll_interval_s": float(self.cfg.poll_interval_s)}
        if duration_ms is not None:        # how long this tick took (excluding the poll sleep and this write)
            body["duration_ms"] = duration_ms
        if phase_ms:
            body["phase_ms"] = phase_ms
        try:
            self.store.atomic_write_bytes(self.store.directory / HEARTBEAT_FILE,
                                          (json.dumps(body, sort_keys=True) + "\n").encode("utf-8"))
        except (OSError, StoreError) as e:    # a diagnostic file must never stop the loop it reports on
            try:
                self.store.append_event("heartbeat_write_failed", tick=self.ticks, error=e.__class__.__name__)
            except (OSError, StoreError):
                pass

    def main_gone(self, t: Dict[str, Any]) -> bool:
        if self.attached is None:
            return not t["main_alive"]
        # Attached mode: waiting for the host to start Main is not "gone"; only a Main that ran and ended, with no
        # live host to bring it back, ends the loop.
        main = read_main(self.store)
        return not t["main_alive"] and main.get("status") == "EXITED" and not self.attached.channel.fresh()

    def run(self, max_ticks: Optional[int] = None) -> int:
        while True:
            t = self.tick()
            self.svc.out.write(f"tick {t['tick']}: main_alive={t['main_alive']} requests={len(t['requests'])} "
                               f"worker_changes={len(t['workers']['changes'])}\n")
            if self.watch_main and self.cfg.exit_with_main and self.main_gone(t):
                self.svc.out.write("Main is no longer running; supervisor loop ends (workers keep running).\n")
                return EXIT_OK
            if max_ticks is not None and self.ticks >= max_ticks:
                return EXIT_OK
            self.svc.sleep(float(self.cfg.poll_interval_s))


# ------------------------------------------------------------------ commands

def cmd_config(svc: Services, a: argparse.Namespace) -> int:
    if a.example:
        _print(svc, config_mod.example(), True)
        return EXIT_OK
    d = svc.config.to_dict()
    d["_source"] = svc.config.source
    d["_state_directory"] = str(svc.config.state_directory(svc.env))
    _print(svc, d, True)
    return EXIT_OK


def cmd_start(svc: Services, a: argparse.Namespace) -> int:
    cfg = svc.config
    root = _repo_root(cfg)
    profile = MainProfile.from_config(cfg)
    if not a.yes and cfg.main_launch_mode == "attached":
        _print(svc, {"action": "run the supervisor loop; Main is NOT launched here: start it in the other terminal tab "
                               "with main-host (attached mode)",
                     "main_launch_mode": "attached", "cwd": str(root), "state_directory": str(cfg.state_directory(svc.env)),
                     "auto_replace_main": cfg.auto_replace_main, "run": "python -B -m dev_supervisor start --yes",
                     "main_tab": "python -B -m dev_supervisor main-host --yes"}, a.json, None)
        return EXIT_OK
    if not a.yes:
        argv = build_main_argv(profile, "00000000-0000-4000-8000-000000000000", "<state dir>/prompts/main-<uuid>.prompt.txt")
        plan = {"action": "launch interactive Main in a new console window, then run the supervisor loop",
                "argv": argv, "cwd": str(root), "state_directory": str(cfg.state_directory(svc.env)),
                "provider": "inherited from the terminal you opened (Claude Pro or OmniRoute launcher); not changed",
                "dangerously_skip_permissions": profile.dangerously_skip_permissions,
                "auto_replace_main": cfg.auto_replace_main, "run": "python -B -m dev_supervisor start --yes"}
        _print(svc, plan, a.json, None)
        return EXIT_OK
    store = svc.store()
    try:
        loop = SupervisorLoop(svc, store)
    except LockHeldError as e:
        svc.out.write(f"refused: {e} (a supervisor loop is already running)\n")
        return EXIT_FAIL
    try:
        plan = loop.sup.recover()
        svc.out.write(f"recovery: {len(plan.items)} item(s), {len(plan.blockers)} blocker(s); nothing relaunched\n")
        loop.watch_main = not a.no_main
        if not a.no_main:
            main = loop.ensure_main()
            svc.out.write(f"Main session {main.get('session_id')} pid {main.get('pid')} ({main.get('status')})\n")
            if main.get("status") == "AWAITING_HOST":
                svc.out.write("Waiting for the Main terminal host. In the Main tab run: "
                              "python -B -m dev_supervisor main-host --yes\n")
        return loop.run(a.max_ticks)
    except KeyboardInterrupt:
        svc.out.write("interrupted; supervisor loop stopped (Main and workers keep running)\n")
        return EXIT_OK
    finally:
        loop.close()


def cmd_main_host(svc: Services, a: argparse.Namespace) -> int:
    cfg = svc.config
    root = _repo_root(cfg)
    store = svc.store()
    spawner = svc.main_spawner or main_host.ForegroundSpawner()
    host = main_host.MainHost(store, cfg, spawner, root=str(root), clock=svc.clock, sleep=svc.sleep,
                              pid_alive=svc.pid_alive, session_factory=svc.session_factory, out=svc.out,
                              read_sessions=svc.read_sessions)
    if not a.yes:
        try:
            host.guard(a.ignore_recorded_main)
            guard = "OK: no live Main is recorded"
        except LaunchError as e:
            guard = f"WOULD REFUSE: {e}"
        profile = MainProfile.from_config(cfg)
        argv = build_main_argv(profile, "00000000-0000-4000-8000-000000000000",
                               "<state dir>/prompts/main-<uuid>.prompt.txt")
        _print(svc, {"action": "run the interactive Main in this terminal and own it", "argv": argv, "cwd": str(root),
                     "main_launch_mode": cfg.main_launch_mode, "duplicate_check": guard,
                     "isolation": {"repo_root": str(root), "state_directory": str(cfg.state_directory(svc.env)),
                                   "model": profile.model, "github_issue": cfg.github_issue or None,
                                   "initial_prompt": profile.initial_prompt},
                     "run": "python -B -m dev_supervisor main-host --yes"}, a.json, None)
        return EXIT_OK
    code = host.run(resume_last=a.resume_last, ignore_recorded=a.ignore_recorded_main, max_ticks=a.max_ticks)
    return EXIT_OK if code == 0 else EXIT_FAIL


def _registry_view(store: StateStore) -> Dict[str, Any]:
    reg = WorkerRegistry(store)
    return {"workers": [{k: w.to_dict()[k] for k in ("worker_id", "role", "task_id", "status", "branch", "model",
                                                       "provider", "pid", "restart_count", "exit_code")}
                        for w in reg.list()],
            "results": [r.to_dict() for r in reg.results.values()],
            "blockers": [b for b in reg.blockers if not b.get("resolved")],
            "contradictions": [c for c in reg.contradictions if not c.get("resolved")]}


def _account_usage(store: StateStore) -> Dict[str, Any]:
    """Account usage from the newest rate_limit_event in worker stream-json logs (OBSERVED), else UNAVAILABLE."""
    return telemetry.account_usage(telemetry.RateLimitEventProvider(store.directory / "logs"))


def _usage_evidence(au: Dict[str, Any]) -> Any:
    """One Evidence-shaped dict for a handoff: the observed usage fields as one value, or the UNAVAILABLE evidence."""
    u = au.get("usage")
    if isinstance(u, dict) and u:
        first = next(iter(u.values()))
        return {"label": first.label.value, "value": {k: v.value for k, v in u.items()},
                "source": first.source, "timestamp": first.timestamp}
    return u


def _usage_text(au: Dict[str, Any]) -> str:
    u = au.get("usage")
    if not isinstance(u, dict) or not u:
        return _ev_text(u)
    parts = []
    for name, label in (("five_hour", "5h"), ("weekly", "7d")):
        pct = u.get(name + "_used_percentage")
        if pct is not None:
            rs = u.get(name + "_resets_at")
            parts.append(f"{label} {pct.value}%" + (f" (resets {rs.value})" if rs is not None else ""))
    first = next(iter(u.values()))
    return (", ".join(parts) or "no windows") + f" [{first.label.value}, observed {first.timestamp}]"


def _mark_exited_unless_retired(store: StateStore, session_id: str) -> None:
    """Mark a session EXITED only while it is still live. A session the replacement already retired (RETIRED) or could
    not clear (RETIRE_FAILED) keeps that status: its process ending is expected, not a new failure (attached mode)."""
    rec = replacement.load_sessions(store)["sessions"].get(session_id) or {}
    if rec.get("status") in (replacement.SessionStatus.RETIRED, replacement.SessionStatus.RETIRE_FAILED):
        return
    replacement.mark_exited(store, session_id)


def _context_view(store: StateStore, sid: Optional[str]) -> Optional[Dict[str, Any]]:
    """Persisted context-management state of the current Main (read only), None without a Main or on any read error."""
    if not sid:
        return None
    try:
        st = compaction.ContextTracker(store).load(sid)
    except StoreError:
        return None
    return {"handoff_prepared": st["prepare"]["active"], "handoff_id": st["prepare"]["handoff_id"],
            "compaction": st["compact"]["state"], "last_result": st["compact"]["result"]}

def _host_view(svc: Services, store: StateStore) -> Optional[Dict[str, Any]]:
    if svc.config.main_launch_mode != "attached":
        return None
    h = main_host.read_host(store)
    return {"state": h.get("state"), "pid": h.get("pid"), "child": h.get("child"),
            "heartbeat_fresh": main_host.host_fresh(h, svc.clock(), float(svc.config.host_heartbeat_stale_s),
                                                    svc.pid_alive),
            "recover_session_id": h.get("recover_session_id")}


def cmd_status(svc: Services, a: argparse.Namespace) -> int:
    store = svc.store(create=False)
    main = read_main(store)
    sid = main.get("session_id")
    sess, pressure = main_observation(svc, sid)
    reg = _registry_view(store)
    lg = _logger(svc, store)
    data = {"main": {"session_id": sid, "status": main.get("status"), "pid": main.get("pid"),
                     "model": main.get("model"), "replaced_session_id": main.get("replaced_session_id"),
                     "session_file_found": sess is not None, "idle": idle_evidence(sess),
                     "context_ratio": pressure.get("ratio"), "context_level": pressure.get("level")},
            "authoritative_main": _authoritative_main(store),
            "context_management": _context_view(store, sid),
            "heartbeat": heartbeat_view(store, svc.clock()),
            "main_launch_mode": svc.config.main_launch_mode,
            "main_host": _host_view(svc, store),
            "lock": lock_holder(store, svc.pid_alive),
            "workers": {s: sum(1 for w in reg["workers"] if w["status"] == s) for s in WorkerStatus.ALL
                        if any(w["status"] == s for w in reg["workers"])},
            "open_blockers": len(reg["blockers"]), "open_contradictions": len(reg["contradictions"]),
            "queued_requests": len(pending_requests(store)),
            "github_pending": len(lg.pending()) if lg else None,
            "usage_note": replacement.USAGE_NOTE}
    au = _account_usage(store)
    data["account_usage"] = au["usage"]
    text = (f"Main: {sid or 'none'} [{main.get('status') or 'not started'}] pid={main.get('pid')} "
            f"authoritative={data['authoritative_main'] or 'none'}\n"
            f"context ratio: {_ev_text(pressure.get('ratio'))}\n"
            f"account usage: {_usage_text(au)}\n"
            f"workers: {data['workers'] or 'none'}; blockers={data['open_blockers']} "
            f"queued_requests={data['queued_requests']}\nlock: {data['lock']}\n"
            f"supervisor heartbeat: {_heartbeat_text(data['heartbeat'])}")
    _print(svc, data, a.json, text)
    return EXIT_OK


def cmd_workers(svc: Services, a: argparse.Namespace) -> int:
    reg = _registry_view(svc.store(create=False))
    lines = [f"{w['worker_id']} {w['status']:<8} task={w['task_id']} role={w['role']} model={w['model']} "
             f"branch={w['branch']}" for w in reg["workers"]] or ["no workers"]
    _print(svc, reg, a.json, "\n".join(lines))
    return EXIT_OK


def cmd_assign(svc: Services, a: argparse.Namespace) -> int:
    cfg = svc.config
    model = a.model or cfg.model("worker")
    payload = {"task_id": a.task, "role": a.role, "branch": a.branch, "worktree": os.path.abspath(a.worktree),
               "owner_stream": a.owner_stream, "model": model, "prompt_file": os.path.abspath(a.prompt_file),
               "provider": "anthropic"}
    problems = []
    try:
        build_claude_argv(model, str(uuid.uuid4()), executable=cfg.claude_executable,
                          permission_mode=cfg.worker_permission_mode)
    except LifecycleError as e:
        problems.append(str(e))
    if not os.path.isfile(payload["prompt_file"]):
        problems.append("prompt file does not exist")
    ok, why = svc.worktree_checker(payload["worktree"], a.branch)
    if not ok:
        problems.append(f"worktree check: {why}")
    if problems:
        _print(svc, {"ok": False, "problems": problems}, a.json, "refused: " + "; ".join(problems))
        return EXIT_FAIL
    if not a.yes:
        _print(svc, {"ok": True, "dry_run": True, "would_queue": payload}, a.json,
               f"valid assignment for task {a.task} (model {model}); rerun with --yes to queue it")
        return EXIT_OK
    store = svc.store()
    rid = enqueue(store, "assign", payload)
    holder = lock_holder(store, svc.pid_alive)
    running = bool(holder and holder["alive"])
    _print(svc, {"ok": True, "queued": rid, "loop_running": running}, a.json,
           f"queued {rid}; " + ("the running supervisor loop will spawn it" if running
                                else "it will be spawned when the supervisor loop runs (start --yes)"))
    return EXIT_OK


def _load_facts(path: str) -> Dict[str, Any]:
    try:
        facts = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"facts file unreadable ({e.__class__.__name__})") from None
    if not isinstance(facts, dict):
        raise ConfigError("facts file must be a JSON object")
    unknown = sorted(set(facts) - set(FACT_KEYS))
    if unknown:
        raise ConfigError(f"unknown facts keys: {', '.join(unknown)} (allowed: {', '.join(FACT_KEYS)})")
    return facts


def cmd_handoff(svc: Services, a: argparse.Namespace) -> int:
    store = svc.store()
    root = _repo_root(svc.config)
    checkers = svc.ref_checkers_factory(svc.config, root)
    if a.verify:
        rep = handoff.verify_handoff(store, a.verify, checkers)
        _print(svc, rep.to_dict(), a.json, f"{rep.status} {rep.handoff_id}" +
               ("".join(f"\n- {r}" for r in rep.reasons)))
        return EXIT_OK if rep.safe else EXIT_FAIL
    if not a.input:
        svc.out.write("handoff needs --input <facts.json> or --verify <handoff id>\n")
        return EXIT_USAGE
    facts = _load_facts(a.input)
    main = read_main(store)
    sid = a.session_id or main.get("session_id")
    if not sid:
        svc.out.write("refused: no Main session recorded; pass --session-id\n")
        return EXIT_FAIL
    _sess, pressure = main_observation(svc, sid)
    try:
        h = handoff.build_handoff(session_id=sid, role=MAIN_ROLE, workers=handoff.worker_entries(
            WorkerRegistry(store).list()), context=pressure,
            **({"usage": _usage_evidence(_account_usage(store))} if "usage" not in facts else {}), **facts)
    except handoff.HandoffError as e:
        _print(svc, {"ok": False, "error": str(e)}, a.json, f"refused: {e}")
        return EXIT_FAIL
    if not a.yes:
        _print(svc, {"ok": True, "dry_run": True, "handoff_id": h["handoff_id"], "workers": len(h["workers"]),
                     "branches": len(h["branches"]), "commits": len(h["commits"])}, a.json,
               f"handoff would be valid ({len(h['workers'])} workers); rerun with --yes to write and verify it")
        return EXIT_OK
    rec = handoff.write_handoff(store, h)
    rep = handoff.verify_handoff(store, rec.handoff_id, checkers, expected_hash=rec.content_hash)
    _gh_event(svc, store, github_log.EventKind.HANDOFF, "dev-supervisor", f"handoff {rep.status}",
              evidence=(f"handoff {rec.handoff_id}", f"hash {rec.content_hash[:19]}", f"session {_short(sid)}"))
    _print(svc, {"handoff_id": rec.handoff_id, "content_hash": rec.content_hash, **rep.to_dict()}, a.json,
           f"{rep.status} {rec.handoff_id} {rec.content_hash}" + "".join(f"\n- {r}" for r in rep.reasons))
    return EXIT_OK if rep.safe else EXIT_FAIL


def cmd_compact_check(svc: Services, a: argparse.Namespace) -> int:
    store = svc.store()
    sid = a.session_id or read_main(store).get("session_id")
    sess, pressure = main_observation(svc, sid)
    verified = None
    if a.handoff_id:
        rep = handoff.verify_handoff(store, a.handoff_id, svc.ref_checkers_factory(svc.config, _repo_root(svc.config)))
        verified = rep.verified if rep.safe else None
    dec = compaction.decide(pressure, idle=idle_evidence(sess), verified=verified,
                            config=_pressure_config(svc.config), now=svc.clock(), session_id=sid or "")
    compaction.record_decision(store, dec)
    _print(svc, dec.to_dict(), a.json, f"{dec.action} (allowed={dec.allowed}): {dec.reason}")
    if "pressure_unavailable" in dec.blocked_by:
        return EXIT_PRESSURE_UNAVAILABLE
    return EXIT_ACTION_DUE if dec.action in (compaction.COMPACT, compaction.REPLACE) else EXIT_OK


def cmd_replace(svc: Services, a: argparse.Namespace) -> int:
    store = svc.store()
    main = read_main(store)
    rep = handoff.verify_handoff(store, a.handoff_id, svc.ref_checkers_factory(svc.config, _repo_root(svc.config)))
    if not rep.safe:
        _print(svc, {"ok": False, **rep.to_dict()}, a.json,
               f"refused: handoff {a.handoff_id} is NOT_SAFE" + "".join(f"\n- {r}" for r in rep.reasons))
        return EXIT_FAIL
    if not a.yes:
        _print(svc, {"ok": True, "dry_run": True, "old_session_id": main.get("session_id"),
                     "handoff_id": a.handoff_id, "usage_note": replacement.USAGE_NOTE}, a.json,
               f"handoff verified; rerun with --yes to replace Main {main.get('session_id')}\n"
               f"{replacement.USAGE_NOTE}")
        return EXIT_OK
    holder = lock_holder(store, svc.pid_alive)
    if holder and holder["alive"]:
        rid = enqueue(store, "replace", {"handoff_id": a.handoff_id})
        _print(svc, {"ok": True, "queued": rid}, a.json, f"queued {rid}; the running supervisor loop will replace Main")
        return EXIT_OK
    try:
        loop = SupervisorLoop(svc, store)
    except LockHeldError as e:
        svc.out.write(f"refused: {e}\n")
        return EXIT_FAIL
    try:
        res = loop.replace_main(a.handoff_id)
    finally:
        loop.close()
    _print(svc, res, a.json, f"{res.get('status')}: new Main {res.get('new_session_id')}" +
           "".join(f"\n- {r}" for r in res.get("reasons", [])))
    return EXIT_OK if res.get("status") in (replacement.ReplacementStatus.REPLACED,
                                            replacement.ReplacementStatus.REPLACED_OLD_NOT_CLEARED) else EXIT_FAIL


def cmd_report(svc: Services, a: argparse.Namespace) -> int:
    store = svc.store()
    lg = _logger(svc, store)
    if lg is None:
        svc.out.write("no github_issue configured; nothing to report\n")
        return EXIT_FAIL
    pending = lg.pending()
    if not a.yes:
        _print(svc, {"pending": len(pending), "kinds": sorted({e.kind.value for e in pending}),
                     "refusals": len(lg.refusals())}, a.json,
               f"{len(pending)} event(s) pending for issue #{svc.config.github_issue}; rerun with --yes to post")
        return EXIT_OK
    res = lg.flush()
    _print(svc, {"posted": len(res.posted), "remaining": res.remaining, "errors": res.errors,
                 "already_posted": len(res.already_posted)}, a.json,
           f"posted {len(res.posted)}, remaining {res.remaining}" + "".join(f"\n- {e}" for e in res.errors))
    return EXIT_OK if res.remaining == 0 else EXIT_FAIL


def cmd_snapshot(svc: Services, a: argparse.Namespace) -> int:
    snap_mod = _snapshot_module()
    cfg = svc.config
    store = svc.store(create=False)
    tele = telemetry.snapshot(cfg.sessions_directory(), cfg.projects_directory(), config=_pressure_config(cfg),
                              usage_provider=telemetry.RateLimitEventProvider(store.directory / "logs"),
                              pid_alive=svc.pid_alive)
    reg, events = None, []
    if store.directory.is_dir():
        reg = WorkerRegistry(store)
        events, _bad = store.read_events()
    snap = snap_mod.build_snapshot(tele, reg, events, main_session_id=read_main(store).get("session_id")
                                   if store.directory.is_dir() else None)
    if a.out:
        snap_mod.write_snapshot(snap, a.out)
        svc.out.write(f"snapshot written; overall freshness: {snap['overall_freshness']}\n")
    else:
        _print(svc, snap, True)
    return EXIT_OK


def _num_or_unavailable(v: Optional[float]) -> Any:
    return routing.UNAVAILABLE if v is None else v


def cmd_route(svc: Services, a: argparse.Namespace) -> int:
    prof = routing.TaskProfile(criticality=a.criticality or routing.UNAVAILABLE,
                               complexity=a.complexity or routing.UNAVAILABLE,
                               est_context_tokens=_num_or_unavailable(a.context_tokens),
                               deadline_minutes=_num_or_unavailable(a.deadline_minutes),
                               concurrency=_num_or_unavailable(a.concurrency),
                               context_pressure=_num_or_unavailable(a.pressure),
                               validation_failed=bool(a.validation_failed), failures=int(a.failures or 0))
    dec = routing.decide(prof, routing.RoutingConfig(allow_paid_api=svc.config.allow_paid_api))
    models = {routing.CLAUDE_STRONG: svc.config.model("strongest"), routing.CLAUDE_STANDARD: svc.config.model("standard")}
    data = {"best": dec.best, "model": (models.get(dec.best) if dec.best in models else
                                        next((r.model for r in dec.ranking if r.route == dec.best), None)),
            "ranking": [{"route": r.route, "score": r.score, "reasons": list(r.reasons)} for r in dec.ranking],
            "excluded": [list(e) for e in dec.excluded], "notes": list(dec.notes)}
    _print(svc, data, a.json, dec.explain() + f"\nbest: {dec.best} (model {data['model']})")
    return EXIT_OK if dec.best else EXIT_FAIL


# ------------------------------------------------------------------ doctor

_GH_ACCOUNT = re.compile(r"Logged in to (\S+) (?:account|as) ([A-Za-z0-9-]+)")


def _check(name: str, status: str, detail: str) -> Dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _run_safe(svc: Services, argv: Sequence[str], cwd: Optional[str] = None, timeout: float = 30.0
              ) -> Tuple[Optional[int], str, str]:
    try:
        return svc.run(list(argv), timeout=timeout, cwd=cwd)
    except OSError as e:
        return None, "", e.__class__.__name__


def doctor_checks(svc: Services) -> List[Dict[str, str]]:
    cfg = svc.config
    checks: List[Dict[str, str]] = []
    root = cfg.repository_root()

    exe = svc.which(cfg.claude_executable)
    if not exe:
        checks.append(_check("claude", "FAIL", f"{cfg.claude_executable} not found on PATH"))
    else:
        code, out, _ = _run_safe(svc, [exe, "--version"])
        ver = (out.strip().splitlines() or [""])[0][:60]
        checks.append(_check("claude", "OK" if code == 0 and ver else "FAIL",
                             f"version {ver}" if code == 0 else f"claude --version failed ({code})"))

    git = svc.which("git")
    code, out, _ = _run_safe(svc, [git or "git", "--version"]) if git else (None, "", "")
    checks.append(_check("git", "OK" if code == 0 else "FAIL",
                         out.strip()[:60] if code == 0 else "git not available"))

    gh = svc.which("gh")
    if not gh:
        checks.append(_check("gh_auth", "FAIL", "gh not found on PATH"))
    else:
        code, out, err = _run_safe(svc, [gh, "auth", "status"])
        accounts = sorted({f"{h} {u}" for h, u in _GH_ACCOUNT.findall(out + "\n" + err)})
        checks.append(_check("gh_auth", "OK" if code == 0 and accounts else "FAIL",
                             ("logged in: " + ", ".join(accounts)) if accounts else "not logged in"))

    try:
        client = omniroute.OmniRouteClient(svc.omniroute_transport, base_url=cfg.omniroute_base_url, timeout_s=5.0)
        n = len(client.list_models())
        checks.append(_check("omniroute", "OK", f"reachable on localhost, {n} model id(s) listed"))
    except omniroute.OmniRouteError as e:
        checks.append(_check("omniroute", "FAIL", f"not usable: {e.error_class}"))

    try:
        identity = resolve_discovery.load_config(cfg.resolve_identity_config)
        rep = svc.resolve_discover(identity)
        verdict = rep.get("verdict")
        status = {"MATCH": "OK", "MISMATCH": "FAIL"}.get(verdict, "WARN")
        checks.append(_check("resolve", status, f"discovery verdict {verdict}" +
                             (" (acceptable: no identity configured)" if verdict == "UNCONFIGURED" else "")))
    except (ValueError, OSError) as e:
        checks.append(_check("resolve", "FAIL", f"identity config unusable ({e.__class__.__name__})"))

    markers = provider_env.parent_session_markers(svc.env if svc.env is not None else os.environ)
    checks.append(_check("parent_session", "WARN" if markers else "OK",
                         ("supervisor runs inside a Claude Code session (" + ", ".join(markers) +
                          "); children get them removed, but start the supervisor from a plain terminal")
                         if markers else "not inside a Claude Code session"))

    sd = cfg.state_directory(svc.env)
    try:
        check_location(sd, [root] if root else [])
        checks.append(_check("state_dir", "OK", "outside any repository" + ("" if sd.exists() else " (not created yet)")))
    except UnsafeStoreLocation:
        checks.append(_check("state_dir", "FAIL", "inside a repository working tree"))

    if root is None:
        checks.append(_check("repo_clean", "FAIL", "repository root unknown"))
        checks.append(_check("worktrees", "FAIL", "repository root unknown"))
        return checks
    code, out, _ = _run_safe(svc, ["git", "-C", str(root), "status", "--porcelain"])
    changed = [ln for ln in out.splitlines() if ln.strip()]
    checks.append(_check("repo_clean", "OK" if code == 0 and not changed else "FAIL",
                         "clean" if code == 0 and not changed else
                         (f"{len(changed)} changed path(s)" if code == 0 else "git status failed")))

    code, out, _ = _run_safe(svc, ["git", "-C", str(root), "worktree", "list", "--porcelain"])
    if code != 0:
        checks.append(_check("worktrees", "FAIL", "git worktree list failed"))
        return checks
    branches = {ln.strip()[len("branch refs/heads/"):] for ln in out.splitlines() if ln.startswith("branch ")}
    count = sum(1 for ln in out.splitlines() if ln.startswith("worktree "))
    if not cfg.wave_id:
        checks.append(_check("worktrees", "OK", f"{count} worktree(s) registered; no wave_id configured"))
        return checks
    try:
        wave = json.loads((root / "waves" / cfg.wave_id / "wave.json").read_text(encoding="utf-8"))
        lanes = [(ln.get("id"), ln.get("branch")) for ln in wave.get("lanes", [])]
    except (OSError, ValueError, AttributeError):
        checks.append(_check("worktrees", "FAIL", f"wave {cfg.wave_id} manifest unreadable"))
        return checks
    missing = [lid for lid, br in lanes if br not in branches]
    checks.append(_check("worktrees", "OK" if not missing else "FAIL",
                         f"{len(lanes) - len(missing)}/{len(lanes)} lane worktrees present" +
                         (f"; missing: {', '.join(str(m) for m in missing)}" if missing else "")))
    return checks


def cmd_doctor(svc: Services, a: argparse.Namespace) -> int:
    checks = doctor_checks(svc)
    failed = [c for c in checks if c["status"] == "FAIL"]
    _print(svc, {"ok": not failed, "checks": checks}, a.json,
           "\n".join(f"[{c['status']:<4}] {c['name']}: {c['detail']}" for c in checks) +
           f"\n{'all required checks passed' if not failed else str(len(failed)) + ' check(s) failed'}")
    return EXIT_OK if not failed else EXIT_FAIL


# ------------------------------------------------------------------ parser and entry point

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -B -m dev_supervisor",
                                 description="Development supervisor (issue #44). Non-destructive unless --yes.")
    ap.add_argument("--config", help="machine-local config JSON (default: env "
                                     f"{config_mod.ENV_CONFIG} or <state dir>/config.json)")
    sub = ap.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    p = add("start", "launch Main and run the supervisor loop (--yes)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--max-ticks", type=int, default=None, help="stop the loop after N ticks")
    p.add_argument("--no-main", action="store_true", help="supervise workers only; do not launch Main")
    p = add("main-host", "attached mode: run the interactive Main in this terminal tab (--yes)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--resume-last", action="store_true",
                   help="resume the session recorded in main.json instead of starting a fresh one")
    p.add_argument("--ignore-recorded-main", action="store_true",
                   help="start although main.json records a live Main (only after you verified its pid is stale)")
    p.add_argument("--max-ticks", type=int, default=None, help=argparse.SUPPRESS)
    add("status", "Main, authority, workers, requests, lock")
    add("workers", "worker registry")
    p = add("assign", "validate (and with --yes queue) a worker assignment")
    p.add_argument("--task", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--owner-stream", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--model", default=None)
    p.add_argument("--yes", action="store_true")
    p = add("handoff", "build/write/verify a handoff")
    p.add_argument("--input", help="facts JSON (keys: " + ", ".join(FACT_KEYS) + ")")
    p.add_argument("--verify", metavar="HANDOFF_ID")
    p.add_argument("--session-id", default=None)
    p.add_argument("--yes", action="store_true")
    p = add("compact-check", "Main context pressure and compaction decision")
    p.add_argument("--session-id", default=None)
    p.add_argument("--handoff-id", default=None)
    p = add("replace", "replace Main after a verified handoff (--yes)")
    p.add_argument("--handoff-id", required=True)
    p.add_argument("--yes", action="store_true")
    p = add("report", "flush the GitHub progress log (--yes)")
    p.add_argument("--yes", action="store_true")
    p = add("snapshot", "read-only Orbit snapshot")
    p.add_argument("--out", default=None)
    p = add("route", "explain a routing decision")
    p.add_argument("--criticality", choices=routing.CRITICALITIES)
    p.add_argument("--complexity", choices=routing.COMPLEXITIES)
    p.add_argument("--context-tokens", type=int)
    p.add_argument("--deadline-minutes", type=float)
    p.add_argument("--concurrency", type=int)
    p.add_argument("--pressure", type=float)
    p.add_argument("--failures", type=int, default=0)
    p.add_argument("--validation-failed", action="store_true")
    add("doctor", "environment checks")
    p = add("config", "effective configuration")
    p.add_argument("--example", action="store_true", help="print the template with placeholders")
    return ap


COMMANDS: Dict[str, Callable[[Services, argparse.Namespace], int]] = {
    "start": cmd_start, "main-host": cmd_main_host, "status": cmd_status, "workers": cmd_workers, "assign": cmd_assign, "handoff": cmd_handoff,
    "compact-check": cmd_compact_check, "replace": cmd_replace, "report": cmd_report, "snapshot": cmd_snapshot,
    "route": cmd_route, "doctor": cmd_doctor, "config": cmd_config,
}


def main(argv: Optional[Sequence[str]] = None, services: Optional[Services] = None) -> int:
    args = build_parser().parse_args(argv)
    out = services.out if services is not None else sys.stdout
    try:
        if services is None:
            if args.command == "config" and args.example:
                services = build_services(Config())
            else:
                services = build_services(config_mod.load(args.config))
        return COMMANDS[args.command](services, args)
    except ConfigError as e:
        out.write(f"configuration error: {e}\n")
        return EXIT_USAGE
    except (StoreError, LaunchError) as e:
        out.write(f"refused: {e.__class__.__name__}: {e}\n")
        return EXIT_FAIL
