"""End-to-end autonomy acceptance harness for the development supervisor (T-DEV-018, issue #70, mission part 11).

Runs ONE scripted scenario in an isolated temp state directory and a temp git repository::

    python -B -m dev_supervisor.acceptance --out <report.json> [--real-worker] [--keep]

Steps (each recorded with PASS/FAIL, a mode label and a timing):
 1 isolation_setup        temp dirs, temp git repository + one worktree (real ``git`` processes)
 2 synthetic_records      ACTIVE_TASKS.json / DEVELOPMENT_TRACKER.json: two eligible tasks, one blocked
 3 autonomy_next_first    ``autonomy next`` offers the first eligible task, the other is blocked
 4 worker_run             assign + spawn through ``lifecycle.Supervisor`` (fake launcher, or one bounded real
                          ``claude -p --model haiku`` run with --real-worker), wait for exit
 5 result_reported        the result is recorded REPORTED
 6 result_verified        the harness reads the output file itself; VERIFIED only when the content matches
 7 task_completed_next    the task is COMPLETED in the synthetic registry; ``autonomy next`` offers the second task
 8 pressure_simulated     a synthetic transcript above the compact threshold; compaction defers (no handoff yet)
 9 handoff_verified       handoff built, written, verified with injected checkers
10 compaction_decided     compaction now allows the action
11 replacement            replacement with a fake Main launcher and a simulated heartbeat
12 replacement_view      the replacement Main still sees the second task and not the completed first one
13 stop_hook              the Stop hook blocks the new Main while work remains and allows after it was told

Mode labels: REAL = a real external process did the work (git, claude); MOCKED = a fake stands in for a component
(launcher, heartbeat, retirer, checkers); SIMULATED = synthetic input fed to the real module code. Nothing is labelled
REAL unless a real process ran. The default mode never starts ``claude``.

Isolation (enforced, tested): every directory is passed explicitly; the real supervisor state directory and this
repository's registries are never read or written. The report holds identifiers, counts and timings only: no paths,
transcript text or credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import autonomy, compaction, handoff, replacement, telemetry
from .github_log import scrub
from .lifecycle import ClaudeCliLauncher, GitWorktreeChecker, LaunchHandle, Supervisor, SupervisorPolicy
from .state_store import StateStore, default_directory, find_git_root, is_within, utc_iso
from .workers import ResultStatus, WorkerStatus

REPORT_KIND = "acceptance_report"
SCHEMA_VERSION = 1
REAL, SIMULATED, MOCKED = "REAL", "SIMULATED", "MOCKED"
PASS, FAIL = "PASS", "FAIL"
LEGEND = {REAL: "a real external process (git, claude) did the work",
          MOCKED: "a fake stands in for a component (launcher, heartbeat, retirer, checkers)",
          SIMULATED: "synthetic input fed to the real module code"}

OWNER = autonomy.DEFAULT_OWNER
T1, T2, T3 = "T-ACC-1", "T-ACC-2", "T-ACC-3"
WORK_FILE = "ACCEPTANCE_RESULT.txt"
WORKER_MODEL = "auto/coding"        # a gateway route id; the fake launcher stands in for it
WORKER_PROVIDER = "gemini"
WORKER_TIMEOUT_S = 180.0
MAIN_MODEL = "claude-sonnet-5-5"
OLD_MAIN = "00000000-0000-4000-8000-0000000000a1"
STEP_IDS = ["isolation_setup", "synthetic_records", "autonomy_next_first", "worker_run", "result_reported",
            "result_verified", "task_completed_next", "pressure_simulated", "handoff_verified",
            "compaction_decided", "replacement", "replacement_view", "stop_hook"]
# Failure injections understood by the scenario (used by the tests; each makes exactly one step fail).
INJECTIONS = ("inside_repo", "no_eligible", "launch_error", "worker_exit_nonzero", "worker_no_file",
              "worker_wrong_content", "complete_not_applied", "pressure_low", "handoff_secret", "checker_rejects",
              "premature_compact", "heartbeat_never", "reopen_first_task", "all_done_before_hook",
              "hook_wrong_session")


class StepFailed(Exception):
    """A step's assertion failed; the message goes into the report (scrubbed)."""


# ------------------------------------------------------------------ small fakes

class ScenarioClock:
    """Wall clock plus an offset that ``sleep`` advances (no real waiting in the simulated parts)."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def sleep(self, s: float) -> None:
        self.offset += max(0.0, float(s))


class FakeLauncher:
    """Fake ``ProcessLauncher``: a process runs until ``exit`` is called; ``on_launch`` may act as the worker."""

    def __init__(self, fail_with: Optional[BaseException] = None) -> None:
        self.next_pid = 40_000
        self.procs: Dict[int, Optional[int]] = {}
        self.launches: List[Any] = []
        self.fail_with = fail_with
        self.on_launch: Optional[Callable[[Any, int], None]] = None
        self.terminated: List[int] = []

    def launch(self, spec: Any) -> LaunchHandle:
        if self.fail_with is not None:
            raise self.fail_with
        self.next_pid += 1
        self.procs[self.next_pid] = None
        self.launches.append(spec)
        if self.on_launch is not None:
            self.on_launch(spec, self.next_pid)
        return LaunchHandle(pid=self.next_pid)

    def owns(self, pid: int) -> bool:
        return pid in self.procs

    def poll(self, pid: int) -> Optional[int]:
        return self.procs[pid]

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        if self.procs.get(pid) is None:
            self.procs[pid] = 0

    def kill(self, pid: int) -> None:
        if self.procs.get(pid) is None:
            self.procs[pid] = -9

    def exit(self, pid: int, code: int) -> None:
        self.procs[pid] = code


class FakeHeartbeat:
    def __init__(self, clock: Callable[[], float], never: bool = False) -> None:
        self.clock, self.never, self.calls = clock, never, 0

    def first_heartbeat(self, session_id: str, pid: int) -> Optional[float]:
        self.calls += 1
        return None if self.never else float(self.clock())


class FakeRetirer:
    def __init__(self) -> None:
        self.retired: List[str] = []

    def retire(self, session_id: str) -> Tuple[bool, str]:
        self.retired.append(session_id)
        return True, "simulated clear"


class SetCheckers:
    """Injected handoff reference checkers: membership in the sets the harness recorded (no network, no git)."""

    def __init__(self, branches: Sequence[str], worktrees: Sequence[Tuple[str, str]], commits: Sequence[str],
                 reject: bool = False) -> None:
        self.b, self.w, self.c, self.reject = set(branches), set(worktrees), set(commits), reject

    def branch(self, name: str) -> Tuple[bool, str]:
        return (not self.reject and name in self.b), "recorded branch set"

    def worktree(self, path: str, branch: str) -> Tuple[bool, str]:
        return ((path, branch) in self.w), "recorded worktree set"

    def commit(self, sha: str) -> Tuple[bool, str]:
        return (sha in self.c), "recorded commit set"

    def pull_request(self, number: int) -> Tuple[bool, str]:
        return False, "no pull requests in this scenario"


# ------------------------------------------------------------------ helpers

def _rmtree(path: Path) -> None:
    def onerror(func: Callable[..., Any], p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(str(path), onerror=onerror)


def _no_git(_args: Sequence[str]) -> Tuple[int, str]:
    """Fake read-only git for ``autonomy``: no remote refs exist in the temp repository."""
    return 1, ""


def real_state_directory() -> Path:
    return default_directory()


def real_repository_root() -> Optional[Path]:
    return find_git_root(Path(__file__).resolve().parent)


def _digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def protected_fingerprint() -> Dict[str, Any]:
    """What must not change during a run: this repository's registries and the existence of the real state dir."""
    root = real_repository_root()
    reg = {}
    if root is not None:
        for name in ("ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json"):
            reg[name] = _digest(root / name)
    return {"registries": reg, "real_state_dir_exists": real_state_directory().exists()}


# ------------------------------------------------------------------ the scenario

@dataclass
class StepResult:
    id: str
    outcome: str
    mode: str
    seconds: float
    detail: str
    ran: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "outcome": self.outcome, "mode": self.mode, "seconds": round(self.seconds, 3),
                "detail": self.detail, "ran": self.ran}


class Scenario:
    def __init__(self, *, real_worker: bool = False, inject: Sequence[str] = (), keep: bool = False,
                 base_dir: Optional[str] = None, git: str = "git", claude: str = "claude",
                 worker_timeout_s: float = WORKER_TIMEOUT_S) -> None:
        bad = [i for i in inject if i not in INJECTIONS]
        if bad:
            raise ValueError(f"unknown injection(s): {bad}")
        self.real_worker, self.inject, self.keep = real_worker, set(inject), keep
        self.base_dir, self.git, self.claude = base_dir, git, claude
        self.worker_timeout_s = worker_timeout_s
        self.clock = ScenarioClock()
        self.results: List[StepResult] = []
        self.failed_step: Optional[str] = None
        self.root: Optional[Path] = None
        self.kept_dir: Optional[Path] = None
        self.facts: Dict[str, Any] = {}
        self.observation: Dict[str, Any] = {}
        self.token = uuid.uuid4().hex[:12]
        self.expected = f"ACCEPTANCE OK {self.token}"
        self.worker_mode = REAL if real_worker else MOCKED
        self.store: Optional[StateStore] = None
        self.sup: Optional[Supervisor] = None
        self.worker_launcher: Any = None

    # -------------------------------------------------- plumbing

    @property
    def repo(self) -> Path:
        return self.root / "repo"          # type: ignore[operator]

    @property
    def state(self) -> Path:
        return self.root / "state"         # type: ignore[operator]

    @property
    def wt(self) -> Path:
        return self.root / "wt-task1"      # type: ignore[operator]

    def _git(self, *args: str, cwd: Optional[Path] = None) -> str:
        r = subprocess.run([self.git, *args], cwd=str(cwd or self.repo), capture_output=True, text=True,
                           timeout=60, shell=False)
        if r.returncode != 0:
            raise StepFailed(f"git {args[0]} failed (exit {r.returncode})")
        return (r.stdout or "").strip()

    def _write(self, name: str, doc: Any) -> None:
        (self.repo / name).write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def _step(self, sid: str, mode: str, fn: Callable[[], str]) -> None:
        if self.failed_step is not None:
            self.results.append(StepResult(sid, FAIL, mode, 0.0, f"not run: step {self.failed_step} failed", False))
            return
        t0 = time.perf_counter()
        try:
            detail, outcome = fn(), PASS
        except StepFailed as e:
            detail, outcome = str(e), FAIL
        except Exception as e:  # noqa: BLE001 - any error is a step failure, never a crash of the harness
            detail, outcome = f"{e.__class__.__name__}: {e}", FAIL
        if self.root is not None:
            detail = detail.replace(str(self.root), "<tmp>")
        self.results.append(StepResult(sid, outcome, mode, time.perf_counter() - t0, scrub(detail, 400)))
        if outcome == FAIL:
            self.failed_step = sid

    def _autonomy(self, argv: List[str], stdin: str = "") -> Tuple[int, str]:
        out = io.StringIO()
        env = {autonomy.ENV_MAX_BLOCKS: "3",
               "CREATORSTUDIO_DEV_SUPERVISOR_CONFIG": str(self.root / "no-config.json")}   # type: ignore[operator]
        full = list(argv) + ["--repo", str(self.repo), "--state-dir", str(self.state)]
        rc = autonomy.main(full, out=out, stdin=io.StringIO(stdin), env=env, git=_no_git, clock=self.clock,
                           sleep=self.clock.sleep)
        return rc, out.getvalue()

    def _next(self) -> Dict[str, Any]:
        rc, text = self._autonomy(["next", "--json"])
        if rc != 0:
            raise StepFailed(f"autonomy next exited {rc}")
        return json.loads(text)

    @staticmethod
    def _start_ids(res: Dict[str, Any]) -> List[str]:
        return [i["task_id"] for i in res["items"] if i["kind"] == "start_task"]

    # -------------------------------------------------- run

    def run(self) -> Dict[str, Any]:
        before = protected_fingerprint()
        started = time.time()
        t0 = time.perf_counter()
        base = self.base_dir or tempfile.gettempdir()
        self.root = Path(tempfile.mkdtemp(prefix="acc-", dir=base)).resolve()
        try:
            self._steps()
        finally:
            if self.sup is not None:
                try:
                    self.sup.close()
                except Exception:  # noqa: BLE001
                    pass
            if self.keep:
                self.kept_dir = self.root
            else:
                try:
                    _rmtree(self.root)
                except OSError:
                    pass
        after = protected_fingerprint()
        root_taken = self.root
        isolation = {"real_state_dir_untouched": before["real_state_dir_exists"] == after["real_state_dir_exists"],
                     "repo_registries_untouched": before["registries"] == after["registries"],
                     "state_dir_in_temp": True, "repo_in_temp": True}
        real_root = real_repository_root()
        if real_root is not None and is_within(root_taken, real_root):
            isolation["repo_in_temp"] = False
        if is_within(root_taken, real_state_directory()):
            isolation["state_dir_in_temp"] = False
        ok = all(r.outcome == PASS for r in self.results) and all(isolation.values())
        if not all(isolation.values()):
            self.results.append(StepResult("isolation_check", FAIL, SIMULATED, 0.0,
                                           "protected state changed: " + ", ".join(k for k, v in isolation.items()
                                                                                    if not v)))
        steps = [r.to_dict() for r in self.results]
        mode_counts: Dict[str, int] = {}
        for r in self.results:
            mode_counts[r.mode] = mode_counts.get(r.mode, 0) + 1
        return {"schema_version": SCHEMA_VERSION, "kind": REPORT_KIND,
                "mode": "real-worker" if self.real_worker else "mocked",
                "started_at": utc_iso(started), "duration_s": round(time.perf_counter() - t0, 3),
                "overall": PASS if ok else FAIL, "isolation": isolation, "steps": steps,
                "counts": {"steps": len(steps), "passed": sum(1 for r in self.results if r.outcome == PASS),
                           "failed": sum(1 for r in self.results if r.outcome == FAIL), "by_mode": mode_counts},
                "observation": self.observation, "legend": LEGEND,
                "notes": ["identifiers, counts and timings only; no paths, transcript text or credentials",
                          "ACCEPTANCE is a harness result for synthetic data, not a verification of the project"]}

    def _steps(self) -> None:
        self._step("isolation_setup", REAL, self._s_setup)
        self._step("synthetic_records", SIMULATED, self._s_records)
        self._step("autonomy_next_first", SIMULATED, self._s_next_first)
        self._step("worker_run", self.worker_mode, self._s_worker)
        self._step("result_reported", self.worker_mode, self._s_reported)
        self._step("result_verified", self.worker_mode, self._s_verified)
        self._step("task_completed_next", SIMULATED, self._s_completed)
        self._step("pressure_simulated", SIMULATED, self._s_pressure)
        self._step("handoff_verified", MOCKED, self._s_handoff)
        self._step("compaction_decided", SIMULATED, self._s_compaction)
        self._step("replacement", MOCKED, self._s_replacement)
        self._step("replacement_view", SIMULATED, self._s_view)
        self._step("stop_hook", SIMULATED, self._s_hook)

    # -------------------------------------------------- steps

    def _s_setup(self) -> str:
        if "inside_repo" in self.inject:
            state = self.repo / "state-inside"      # deliberately inside the working tree: the store refuses it
        else:
            state = self.state
        self.repo.mkdir()
        self._git("init", "-q", "-b", "acc-main")
        (self.repo / "README.md").write_text("synthetic acceptance repository\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("-c", "user.name=acc", "-c", "user.email=acc@example.invalid", "commit", "-q", "-m", "synthetic init")
        self.facts["commit"] = self._git("rev-parse", "HEAD")
        self.facts["branch"] = f"acc/{T1}"
        self._git("worktree", "add", "-q", "-b", self.facts["branch"], str(self.wt))
        self.store = StateStore(state, clock=self.clock, sleep=self.clock.sleep)
        if not is_within(self.store.directory, self.root):  # type: ignore[arg-type]
            raise StepFailed("state directory is not inside the temp root")
        return "temp git repository with one commit and one worktree; temp state directory outside the repository"

    def _s_records(self) -> str:
        def task(tid: str, status: str, deps: Sequence[str], path: str) -> Dict[str, Any]:
            return {"id": tid, "status": status, "depends_on": list(deps), "owned_paths": [path], "owner": OWNER}
        t3_deps = [T2]
        tasks = [task(T1, "PLANNED", [], "acc/one/"), task(T2, "PLANNED", [], "acc/two/"),
                 task(T3, "PLANNED", t3_deps, "acc/three/")]
        if "no_eligible" in self.inject:
            tasks[0]["depends_on"], tasks[1]["depends_on"] = [T3], [T3]
        self._write("ACTIVE_TASKS.json", {"schema_version": 1, "tasks": tasks,
                                          "shared_files": {"ACTIVE_TASKS.json": "shared",
                                                           "DEVELOPMENT_TRACKER.json": "shared"}})
        self._write("DEVELOPMENT_TRACKER.json", {"tasks": [{"task_id": T1, "steps": [{"id": "S1",
                                                                                         "status": "PLANNED"}]}]})
        return "3 synthetic tasks: 2 eligible, 1 blocked (waits for the second); no waves directory"

    def _s_next_first(self) -> str:
        res = self._next()
        starts = self._start_ids(res)
        if len(starts) != 2:
            raise StepFailed(f"expected 2 eligible tasks, found {len(starts)}")
        if starts[0] != T1:
            raise StepFailed(f"first eligible task is {starts[0]}, expected {T1}")
        if T3 not in [b["task_id"] for b in res["blocked"]]:
            raise StepFailed(f"{T3} was not reported blocked")
        self.facts["first_task"] = starts[0]
        return f"eligible={len(starts)} blocked={len(res['blocked'])}; first={starts[0]}"

    def _set_status(self, tid: str, status: str, step_status: Optional[str] = None) -> None:
        reg = json.loads((self.repo / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
        for t in reg["tasks"]:
            if t["id"] == tid:
                t["status"] = status
        self._write("ACTIVE_TASKS.json", reg)
        if step_status is not None:
            self._write("DEVELOPMENT_TRACKER.json",
                        {"tasks": [{"task_id": tid, "steps": [{"id": "S1", "status": step_status}]}]})

    def _prompt(self) -> str:
        return (f"Create exactly one file named {WORK_FILE} in the current working directory, using the Write tool. "
                f"Its entire content must be the single line: {self.expected}\n"
                "Do nothing else. Do not run commands and do not read other files. Reply with the word done.\n")

    def _s_worker(self) -> str:
        assert self.store is not None
        self._set_status(T1, "ACTIVE", "IN_PROGRESS")
        prompt = self.root / "task1.prompt.md"      # type: ignore[operator]
        prompt.write_text(self._prompt(), encoding="utf-8")
        policy = SupervisorPolicy(max_runtime_s=self.worker_timeout_s, heartbeat_stale_s=self.worker_timeout_s)
        verifier = self._verifier
        if self.real_worker:
            # Never starts claude on the supervisor's own login: a real worker needs a verified free route (T-DEV-032).
            raise StepFailed("real-worker acceptance is refused: workers run only on a verified free route through the "
                             "gateway, and the Claude login is never used")
        else:
            fake = FakeLauncher(RuntimeError("injected launch failure") if "launch_error" in self.inject else None)
            fake.on_launch = self._fake_worker
            launcher, sleep, clock = fake, self.clock.sleep, self.clock
            self.worker_launcher = fake
        self.sup = Supervisor(self.store, launcher, worktree_checker=GitWorktreeChecker(self.git), clock=clock,
                              sleep=sleep, policy=policy, verifier=verifier)
        w = self.sup.assign(task_id=T1, role="worker", branch=self.facts["branch"], worktree=str(self.wt),
                            owner_stream="acceptance", model=WORKER_MODEL, provider=WORKER_PROVIDER,
                            prompt_file=str(prompt))
        w = self.sup.spawn(w.worker_id)
        self.facts["worker_id"] = w.worker_id
        if w.status != WorkerStatus.RUNNING:
            raise StepFailed(f"worker is {w.status} after spawn ({'; '.join(w.failures[-1:]) or 'no detail'})")
        if self.real_worker:
            deadline = time.time() + self.worker_timeout_s + 10
            while w.status in WorkerStatus.LIVE and time.time() < deadline:
                time.sleep(1.0)
                self.sup.monitor()
                w = self.sup.registry.get(w.worker_id)
            self._observe_stream(w)
        else:
            self.sup.monitor()
            w = self.sup.registry.get(w.worker_id)
        if w.status != WorkerStatus.EXITED:
            raise StepFailed(f"worker ended {w.status} (exit {w.exit_code}, failures={len(w.failures)})")
        return f"worker {w.worker_id} spawned and exited with code {w.exit_code} ({self.worker_mode})"

    def _fake_worker(self, spec: Any, pid: int) -> None:
        """What the simulated worker does: write the file (or not, or wrongly) and exit."""
        if "worker_no_file" not in self.inject:
            body = "wrong content\n" if "worker_wrong_content" in self.inject else self.expected + "\n"
            (Path(spec.cwd) / WORK_FILE).write_text(body, encoding="utf-8")
        self.worker_launcher.exit(pid, 3 if "worker_exit_nonzero" in self.inject else 0)

    def _observe_stream(self, w: Any) -> None:
        """Counts only from the real run's stream-json output: no text is kept."""
        assert self.sup is not None
        lines = results = errors = 0
        try:
            with open(self.sup.stdout_path(w), "rb") as fh:
                for raw in fh:
                    lines += 1
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("type") == "result":
                        results += 1
                        errors += 1 if ev.get("is_error") else 0
        except OSError:
            pass
        self.observation["real_worker"] = {"model": WORKER_MODEL, "exit_code": w.exit_code, "stream_lines": lines,
                                           "result_events": results, "result_errors": errors,
                                           "timeout_s": self.worker_timeout_s}

    def _read_work_file(self) -> Optional[str]:
        try:
            return (self.wt / WORK_FILE).read_text(encoding="utf-8")
        except OSError:
            return None

    def _verifier(self, result: Any, worker: Any) -> Tuple[str, str]:
        """Independent verification: the harness reads the output file itself; the worker's claim is not trusted."""
        body = self._read_work_file()
        if body is None:
            return ResultStatus.REJECTED, "output file missing"
        if body.strip() != self.expected:
            return ResultStatus.REJECTED, "output file content differs from the expected line"
        return ResultStatus.VERIFIED, "output file read back by the harness; content matches"

    def _s_reported(self) -> str:
        assert self.sup is not None
        r = self.sup.collect(self.facts["worker_id"], "DONE", {"wrote": WORK_FILE})
        self.facts["result_id"] = r.result_id
        if r.status != ResultStatus.REPORTED:
            raise StepFailed(f"result is {r.status}, expected REPORTED")
        return f"result {r.result_id} recorded as REPORTED (claim only)"

    def _s_verified(self) -> str:
        assert self.sup is not None
        r = self.sup.verify_result(self.facts["result_id"])
        if r.status != ResultStatus.VERIFIED:
            raise StepFailed(f"result is {r.status}: {r.verifier_note}")
        return f"result {r.result_id} VERIFIED by reading the file back ({r.verifier_note})"

    def _s_completed(self) -> str:
        if "complete_not_applied" not in self.inject:
            self._set_status(T1, "COMPLETED", "COMPLETED")
        reg = json.loads((self.repo / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
        status = next(t["status"] for t in reg["tasks"] if t["id"] == T1)
        if status != "COMPLETED":
            raise StepFailed(f"{T1} is {status} in the registry, expected COMPLETED")
        res = self._next()
        starts = self._start_ids(res)
        if T1 in starts:
            raise StepFailed(f"{T1} is still offered after completion")
        if not starts or starts[0] != T2:
            raise StepFailed(f"second task not selected (got {starts[:1]})")
        if any(i["kind"] == "review_worker_result" for i in res["items"]):
            raise StepFailed("a verified result is still offered for review")
        return f"{T1} COMPLETED; next={starts[0]}; no human input"

    def _s_pressure(self) -> str:
        # Sized relative to the model's configured window (5% low, 93% high) so the simulation tracks the table.
        win = telemetry.PressureConfig().window_for(MAIN_MODEL) or 200_000
        frac = (0.005, 0.025, 0.02) if "pressure_low" in self.inject else (0.03, 0.85, 0.05)
        tokens = tuple(int(win * f) for f in frac)
        rec = {"type": "assistant", "message": {"role": "assistant", "model": MAIN_MODEL, "content": [],
                                                "usage": {"input_tokens": tokens[0],
                                                          "cache_read_input_tokens": tokens[1],
                                                          "cache_creation_input_tokens": tokens[2],
                                                          "output_tokens": 10}}}
        path = self.root / "transcript.jsonl"        # type: ignore[operator]
        path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}) + "\n"
                        + json.dumps(rec) + "\n", encoding="utf-8")
        pressure = telemetry.context_pressure(path)
        self.facts["pressure"] = pressure
        dec = compaction.decide(pressure, idle=True, verified=None, now=self.clock(), session_id=OLD_MAIN)
        ratio = dec.ratio
        if dec.action not in (compaction.COMPACT, compaction.REPLACE):
            raise StepFailed(f"pressure {ratio} did not reach the compact threshold (action {dec.action})")
        if dec.allowed or "no_fresh_verified_handoff" not in dec.blocked_by:
            raise StepFailed("compaction was not deferred while no verified handoff existed")
        self.facts["ratio"] = ratio
        return f"synthetic transcript ratio={ratio} action={dec.action}; deferred: no verified handoff yet"

    def _s_handoff(self) -> str:
        assert self.store is not None
        replacement.register_session(self.store, OLD_MAIN, "main")
        wk = list(self.sup.registry.workers.values()) if self.sup else []  # type: ignore[union-attr]
        decisions = ["Acceptance scenario: synthetic data only"]
        if "handoff_secret" in self.inject:
            decisions.append("credential " + "sk-" + "ant-" + "x" * 24)       # built at runtime, never a literal
        h = handoff.build_handoff(
            session_id=OLD_MAIN, role="main", objective="Continue the synthetic acceptance scenario",
            decisions=decisions, plan=[f"offer {T2} next"],
            task_graph=[{"task_id": T1, "title": "first", "status": "COMPLETED", "depends_on": []},
                        {"task_id": T2, "title": "second", "status": "PLANNED", "depends_on": []},
                        {"task_id": T3, "title": "third", "status": "PLANNED", "depends_on": [T2]}],
            workers=handoff.worker_entries(wk), branches=[self.facts["branch"]],
            worktrees=[{"path": str(self.wt), "branch": self.facts["branch"]}],
            commits=[{"sha": self.facts["commit"], "branch": self.facts["branch"], "subject": "synthetic init"}],
            tests=[{"command": "harness file read-back", "result": "content matched", "label": "OBSERVED"}],
            evidence=[{"claim": f"{T1} output verified by the harness", "label": "OBSERVED", "source": "acceptance"}],
            next_actions=[f"start {T2}"], context=self.facts["pressure"])
        rec = handoff.write_handoff(self.store, h)
        checkers = SetCheckers([self.facts["branch"]], [(str(self.wt), self.facts["branch"])],
                               [self.facts["commit"]], reject="checker_rejects" in self.inject)
        self.facts["checkers"] = checkers
        rep = handoff.verify_handoff(self.store, h["handoff_id"], checkers, expected_hash=rec.content_hash)
        if not rep.safe:
            raise StepFailed("handoff is " + rep.status + ": " + "; ".join(rep.reasons[:3]))
        self.facts["verified"] = rep.verified
        self.facts["handoff_id"] = h["handoff_id"]
        return f"handoff {h['handoff_id']} built, written and verified {rep.status} ({sum(rep.checked.values())} refs)"

    def _s_compaction(self) -> str:
        assert self.store is not None
        if "premature_compact" in self.inject:
            self.facts["verified"] = None
        dec = compaction.decide(self.facts["pressure"], idle=True, verified=self.facts["verified"], now=self.clock(),
                                session_id=OLD_MAIN)
        compaction.record_decision(self.store, dec)
        if not dec.allowed or dec.action not in (compaction.COMPACT, compaction.REPLACE):
            raise StepFailed(f"compaction not allowed: action={dec.action} blocked_by={dec.blocked_by}")
        return f"decision {dec.action} allowed with handoff {dec.handoff_id}"

    def _s_replacement(self) -> str:
        assert self.store is not None
        launcher = FakeLauncher()
        self.facts["main_launcher"] = launcher
        beat = FakeHeartbeat(self.clock, never="heartbeat_never" in self.inject)
        retirer = FakeRetirer()
        new_sid = str(uuid.uuid4())
        self.facts["new_session"] = new_sid
        res = replacement.replace_session(
            self.store, launcher, old_session_id=OLD_MAIN, role="main", model=MAIN_MODEL, cwd=str(self.repo),
            verified=self.facts["verified"], checkers=self.facts["checkers"], heartbeat=beat, retirer=retirer,
            clock=self.clock, sleep=self.clock.sleep, session_factory=lambda: new_sid,
            policy=replacement.ReplacementPolicy(heartbeat_timeout_s=30.0, heartbeat_poll_s=5.0))
        if res.status != replacement.ReplacementStatus.REPLACED:
            raise StepFailed(f"replacement ended {res.status}: {'; '.join(res.reasons[:2])}")
        if res.authoritative_session_id != new_sid or retirer.retired != [OLD_MAIN]:
            raise StepFailed("authority or retirement did not move to the new session")
        return (f"{res.status}: {len(res.attempts)} attempt(s), heartbeat probes={beat.calls}, "
                f"old session cleared={res.old_session_cleared}")

    def _s_view(self) -> str:
        if "reopen_first_task" in self.inject:
            self._set_status(T1, "PLANNED", "PLANNED")
        res = self._next()
        starts = self._start_ids(res)
        if T1 in starts:
            raise StepFailed(f"duplicated work: {T1} is offered again to the new Main")
        if not starts or starts[0] != T2:
            raise StepFailed(f"new Main does not see {T2} first (got {starts[:1]})")
        return f"new Main sees {starts[0]} first; {T1} not offered; eligible={len(starts)}"

    def _s_hook(self) -> str:
        if "all_done_before_hook" in self.inject:
            self._set_status(T2, "COMPLETED")
        sid = str(uuid.uuid4()) if "hook_wrong_session" in self.inject else self.facts["new_session"]
        rc, out = self._autonomy(["stop-hook"], json.dumps({"session_id": sid, "stop_hook_active": False}))
        try:
            first = json.loads(out) if out.strip() else None
        except ValueError:
            first = None
        if rc != 0 or not first or first.get("decision") != "block" or T2 not in str(first.get("reason")):
            raise StepFailed("the Stop hook did not block the new Main while work remains")
        rc2, out2 = self._autonomy(["stop-hook"], json.dumps({"session_id": sid, "stop_hook_active": True}))
        if rc2 != 0 or out2.strip():
            raise StepFailed("the Stop hook kept blocking after Main was told")
        return "first stop blocked (reason names the second task); second stop allowed after it was told"


# ------------------------------------------------------------------ CLI

def run_acceptance(*, real_worker: bool = False, inject: Sequence[str] = (), keep: bool = False,
                   base_dir: Optional[str] = None, **kw: Any) -> Tuple[Dict[str, Any], Optional[Path]]:
    sc = Scenario(real_worker=real_worker, inject=inject, keep=keep, base_dir=base_dir, **kw)
    return sc.run(), sc.kept_dir


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dev_supervisor.acceptance",
                                description="End-to-end autonomy acceptance scenario (isolated, synthetic)")
    p.add_argument("--out", required=True, help="where to write the JSON report (outside the repository)")
    p.add_argument("--real-worker", action="store_true",
                   help="REFUSED: a real worker needs a verified free route; the Claude login is never used")
    p.add_argument("--keep", action="store_true", help="keep the temp directory and print its location on stderr")
    return p


def main(argv: Optional[Sequence[str]] = None, *, out: Any = None, err: Any = None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    target = Path(a.out).resolve()
    root = real_repository_root()
    if root is not None and is_within(target, root):
        out.write("refused: --out must be outside the repository working tree\n")
        return 2
    report, kept = run_acceptance(real_worker=a.real_worker, keep=a.keep)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for s in report["steps"]:
        out.write(f"{s['outcome']:<4} {s['mode']:<9} {s['seconds']:>7.2f}s  {s['id']}\n")
    out.write(f"overall {report['overall']} ({report['counts']['passed']}/{report['counts']['steps']} steps, "
              f"{report['duration_s']}s, mode {report['mode']})\n")
    if kept is not None:
        err.write(f"kept temp directory: {kept}\n")
    return 0 if report["overall"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
