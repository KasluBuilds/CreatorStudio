"""Live unattended multi-cycle autonomy harness in a throwaway LOCAL sandbox (T-DEV-026, issue #44).

What it is: a sandbox git repo (bare origin + integration checkout + worktrees + synthetic backlog) in which a real
headless ``claude`` Main runs the loop  next -> claim -> dispatch -> wait -> verify -> review -> integrate -> next
using only the ``ctl`` commands below, while a separate harness-supervisor process spawns and watches real worker
processes and a chaos driver injects real failures. Nothing here touches this repository, the real supervisor state,
real footage, or any paid service.

What it is NOT (labelled in LIVE_AUTONOMY.md): integration uses a LOCAL gate implementation mirroring the production
gate semantics, not ``dev_supervisor.integrate`` over GitHub (that path is proven separately on real PRs); the
supervisor here is the harness supervisor, not the production ``dev_supervisor start``.

Commands (``python -B -m dev_supervisor.live_autonomy <cmd>``)::

    run --root DIR [--model haiku] [--timeout S] [--yes]   build the sandbox and run it unattended (needs --yes)
    supervise --root DIR                                   the harness supervisor loop (started by ``run``)
    ctl --root DIR <next|claim|dispatch|wait|status|verify|review|integrate|recover|stop-hook> [args]

Standard library only (``psutil`` is used for process identity when importable, with a PowerShell fallback).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from . import authority, session_policy
except ImportError:  # pragma: no cover
    import authority  # type: ignore
    import session_policy  # type: ignore

PY = sys.executable
MAX_ATTEMPTS = 2
STALL_SECONDS = 150
LOCK_STALE_SECONDS = 300          # longer than the longest lock hold (tests run up to 120 s)
SUPERVISOR_MARKER = "dev_supervisor.live_autonomy supervise"
MARKER_PREFIX = "cs-live-"        # fixed by the harness; a ledger record cannot widen it
SECRET_RX = re.compile(r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,})")
FORBIDDEN_RX = re.compile(r"(^|/)(\.env|\.claude/|.*\.pem$|.*\.key$)")

# ------------------------------------------------------------------ synthetic backlog

TASKS: List[Dict[str, Any]] = [
    {"id": "L-001", "title": "slugify(text): lowercase, non-alphanumeric runs become one '-', trimmed of '-'",
     "module": "slugify.py", "func": "slugify", "depends_on": [],
     "test": "from slugify import slugify\n\nclass T(unittest.TestCase):\n    def test_basic(self):\n        self.assertEqual(slugify('Hello, World!'), 'hello-world')\n    def test_runs(self):\n        self.assertEqual(slugify('  a -- b__c  '), 'a-b-c')\n    def test_empty(self):\n        self.assertEqual(slugify('***'), '')\n"},
    {"id": "L-002", "title": "wrap_words(text, width): greedy word wrap returning a list of lines no longer than width (a longer word gets its own line)",
     "module": "wrapwords.py", "func": "wrap_words", "depends_on": [],
     "test": "from wrapwords import wrap_words\n\nclass T(unittest.TestCase):\n    def test_basic(self):\n        self.assertEqual(wrap_words('aa bb cc dd', 5), ['aa bb', 'cc dd'])\n    def test_long_word(self):\n        self.assertEqual(wrap_words('abcdefgh ij', 4), ['abcdefgh', 'ij'])\n    def test_empty(self):\n        self.assertEqual(wrap_words('', 5), [])\n"},
    {"id": "L-003", "title": "clamp(x, lo, hi): limit x to [lo, hi]; raise ValueError when lo > hi",
     "module": "clamp.py", "func": "clamp", "depends_on": [],
     "test": "from clamp import clamp\n\nclass T(unittest.TestCase):\n    def test_range(self):\n        self.assertEqual((clamp(5, 0, 3), clamp(-1, 0, 3), clamp(2, 0, 3)), (3, 0, 2))\n    def test_bad(self):\n        with self.assertRaises(ValueError):\n            clamp(1, 3, 0)\n"},
    {"id": "L-004", "title": "running_mean(values, window): list of means of each full window of consecutive values; ValueError when window < 1",
     "module": "running_mean.py", "func": "running_mean", "depends_on": ["L-003"],
     "test": "from running_mean import running_mean\n\nclass T(unittest.TestCase):\n    def test_basic(self):\n        self.assertEqual(running_mean([1, 2, 3, 4], 2), [1.5, 2.5, 3.5])\n    def test_short(self):\n        self.assertEqual(running_mean([1], 2), [])\n    def test_bad(self):\n        with self.assertRaises(ValueError):\n            running_mean([1], 0)\n"},
    {"id": "L-005", "title": "contradiction(x): must satisfy the two assertions in tests/test_l005.py",
     "module": "contradiction.py", "func": "contradiction", "depends_on": [],
     "test": "from contradiction import contradiction\n\nclass T(unittest.TestCase):\n    def test_impossible(self):\n        v = contradiction(3)\n        self.assertEqual(v, 4)\n        self.assertEqual(v, 5)\n"},
    {"id": "L-006", "title": "after_contradiction(): depends on L-005 which can never be integrated",
     "module": "after.py", "func": "after_contradiction", "depends_on": ["L-005"],
     "test": "from after import after_contradiction\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        self.assertEqual(after_contradiction(), 1)\n"},
]


def task_def(tid: str) -> Dict[str, Any]:
    for t in TASKS:
        if t["id"] == tid:
            return t
    raise KeyError(tid)


def owned_paths(t: Dict[str, Any]) -> List[str]:
    return [t["module"]]


def test_module(tid: str) -> str:
    return "tests.test_" + tid.lower().replace("-", "")


def stub_source(t: Dict[str, Any]) -> str:
    return f'"""{t["title"]}"""\n\n\ndef {t["func"]}(*args, **kwargs):\n    raise NotImplementedError\n'


# ------------------------------------------------------------------ small utilities

def now() -> float:
    return time.time()


def iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts if ts is not None else now())) + "Z"


def posix(p: Any) -> str:
    return str(p).replace("\\", "/")


def git(args: Sequence[str], cwd: Any, check: bool = True, input: Optional[str] = None) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, input=input, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return (r.stdout or "").strip() if check else f"{r.returncode}\n{r.stdout}{r.stderr}"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


class Layout:
    def __init__(self, root: Any) -> None:
        self.root = Path(root)
        self.origin = self.root / "origin.git"
        self.main = self.root / "main"
        self.wt = self.root / "wt"
        self.state = self.root / "state"
        self.requests = self.state / "requests"
        self.logs = self.state / "logs"
        self.ledger_path = self.state / "ledger.json"
        self.events_path = self.state / "events.jsonl"
        self.lock_path = self.state / "ledger.lock"
        self.merge_lock = self.state / "merge.lock"


def event(lay: Layout, kind: str, **data: Any) -> None:
    lay.state.mkdir(parents=True, exist_ok=True)
    with open(lay.events_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": iso(), "t": round(now(), 3), "kind": kind, "data": data}) + "\n")


# ------------------------------------------------------------------ ledger (state shared by ctl, supervisor, driver)

class Ledger:
    """JSON ledger guarded by an exclusive lock file; every mutation is read-modify-write under the lock."""

    def __init__(self, lay: Layout, timeout: float = 20.0) -> None:
        self.lay, self.timeout = lay, timeout
        self.token = f"{os.getpid()}-{uuid.uuid4().hex}"        # identifies THIS holder

    def _take_over(self) -> None:
        """Remove a stale lock by atomically renaming it away first, so two waiters cannot both unlink a fresh lock."""
        gone = self.lay.lock_path.with_name(f"ledger.lock.stale-{uuid.uuid4().hex}")
        try:
            os.replace(str(self.lay.lock_path), str(gone))
            gone.unlink()
        except OSError:
            pass

    def __enter__(self) -> "Ledger":
        deadline = now() + self.timeout
        self.lay.state.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.lay.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, self.token.encode())
                os.close(fd)
                break
            except FileExistsError:
                try:                                  # stale only well beyond the longest hold (tests run up to 120 s)
                    if now() - self.lay.lock_path.stat().st_mtime > LOCK_STALE_SECONDS:
                        self._take_over()
                        continue
                except OSError:
                    pass
                if now() > deadline:
                    raise TimeoutError("ledger lock timeout")
                time.sleep(0.05)
        self.data = self._load()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if exc[0] is None:
                write_atomic(self.lay.ledger_path, json.dumps(self.data, indent=1, sort_keys=True))
        finally:
            try:                                      # release only a lock we still own
                if self.lay.lock_path.read_text(encoding="utf-8") == self.token:
                    self.lay.lock_path.unlink()
            except OSError:
                pass

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.lay.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"tasks": {}, "workers": {}, "reviews": {}, "main": {}, "counters": {}}


def read_ledger(lay: Layout) -> Dict[str, Any]:
    try:
        return json.loads(lay.ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tasks": {}, "workers": {}, "reviews": {}, "main": {}, "counters": {}}


# ------------------------------------------------------------------ sandbox construction

def build_sandbox(root: Any) -> Layout:
    lay = Layout(root)
    if lay.root.exists() and any(lay.root.iterdir()):
        raise RuntimeError("sandbox root must be empty or new")
    lay.root.mkdir(parents=True, exist_ok=True)
    lay.state.mkdir(parents=True)
    lay.requests.mkdir()
    lay.logs.mkdir()
    lay.wt.mkdir()
    git(["init", "-q", "--bare", "-b", "main", str(lay.origin)], lay.root)
    git(["clone", "-q", str(lay.origin), str(lay.main)], lay.root)
    for k, v in (("user.email", "sandbox@example.invalid"), ("user.name", "sandbox"), ("core.autocrlf", "false")):
        git(["config", k, v], lay.main)
    (lay.main / "tests").mkdir()
    (lay.main / "tests" / "__init__.py").write_text("", encoding="utf-8")
    files = ["tests/__init__.py"]
    for t in TASKS:
        body = "import unittest\n" + t["test"] + "\n\nif __name__ == '__main__':\n    unittest.main()\n"
        p = f"tests/test_{t['id'].lower().replace('-', '')}.py"
        (lay.main / p).write_text(body, encoding="utf-8", newline="\n")
        (lay.main / t["module"]).write_text(stub_source(t), encoding="utf-8", newline="\n")
        files += [p, t["module"]]
    (lay.main / "backlog.json").write_text(json.dumps(
        [{"id": t["id"], "title": t["title"], "owned_paths": owned_paths(t), "depends_on": t["depends_on"],
          "test": test_module(t["id"])} for t in TASKS], indent=1), encoding="utf-8", newline="\n")
    files.append("backlog.json")
    git(["add", *files], lay.main)
    git(["commit", "-q", "-m", "sandbox: synthetic backlog and failing tests"], lay.main)
    git(["push", "-q", "-u", "origin", "main"], lay.main)
    with Ledger(lay) as L:
        L.data["tasks"] = {t["id"]: {"status": "PENDING", "attempts": 0} for t in TASKS}
    event(lay, "sandbox_built", tasks=[t["id"] for t in TASKS])
    return lay


# ------------------------------------------------------------------ process identity

def proc_identity(pid: int) -> Optional[Dict[str, Any]]:
    """pid, exe, cmdline, parent_pid, create_time of a live process (None when gone). psutil when importable."""
    try:
        import psutil
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return None
        return {"pid": pid, "exe": p.name(), "cmdline": " ".join(p.cmdline()), "parent_pid": p.ppid(),
                "create_time": round(p.create_time(), 2)}
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - NoSuchProcess / AccessDenied
        return None
    ps = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; if($p){{ "
          "@{pid=$p.ProcessId;exe=$p.Name;cmdline=$p.CommandLine;parent_pid=$p.ParentProcessId;"
          "create_time=[string]$p.CreationDate}|ConvertTo-Json -Compress }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError:
        return None


def same_process(rec: Dict[str, Any], live: Optional[Dict[str, Any]]) -> bool:
    """PID-reuse protection: pid AND creation time AND command line marker must all match the recorded identity."""
    if not live or live["pid"] != rec["pid"] or rec["marker"] not in str(live["cmdline"]):
        return False
    try:
        return abs(float(live["create_time"]) - float(rec["create_time"])) < 1.5
    except (TypeError, ValueError):                     # the PowerShell fallback reports a date string
        return str(live["create_time"]) == str(rec["create_time"])


def kill_with_proof(rec: Dict[str, Any], supervisor_pid: int, protected: Sequence[int] = (),
                    info: Callable[[int], Optional[Dict[str, Any]]] = proc_identity,
                    killer: Optional[Callable[[int], None]] = None) -> Tuple[bool, str]:
    """Terminate a recorded process only with full identity proof, via authority.decide. Returns (done, reason)."""
    if not (str(rec.get("marker", "")).startswith(MARKER_PREFIX) or rec.get("marker") == SUPERVISOR_MARKER):
        return False, "marker is not one the harness issues; refusing (ledger record may have been altered)"
    live = info(rec["pid"])
    if live is None:
        return False, "process already gone"
    if not same_process(rec, live):
        return False, "identity mismatch (pid reuse or different process); refusing"
    act = authority.Action(authority.TERMINATE_PROCESS, attrs={
        "pid": live["pid"], "exe": live["exe"], "cmdline": live["cmdline"], "parent_pid": live["parent_pid"],
        "session_id": rec.get("session_id") or rec.get("worker_id"), "owned_marker": rec["marker"],
        "owned_parent_pids": [supervisor_pid], "protected_pids": list(protected)})
    d = authority.decide(act, authority.PolicyContext())
    if not d.allowed:
        return False, f"authority policy: {d.rule}"
    if killer is None:
        import psutil
        killer = lambda pid: psutil.Process(pid).kill()  # noqa: E731
    killer(live["pid"])
    return True, "killed with identity proof"


# ------------------------------------------------------------------ task logic

def eligible(data: Dict[str, Any]) -> List[str]:
    out = []
    for t in TASKS:
        st = data["tasks"].get(t["id"], {})
        if st.get("status") == "PENDING" and all(data["tasks"].get(d, {}).get("status") == "INTEGRATED" for d in t["depends_on"]):
            out.append(t["id"])
    return out


def live_workers(data: Dict[str, Any]) -> List[str]:
    return [w for w, r in data["workers"].items() if r.get("status") in ("QUEUED", "RUNNING", "STALLED")]


def work_remaining(data: Dict[str, Any]) -> bool:
    if eligible(data) or live_workers(data):
        return True
    return any(v.get("status") in ("CLAIMED", "WORKING", "WORKER_DONE", "VERIFIED", "REVIEWED", "CHANGES_NEEDED", "DEAD")
               for v in data["tasks"].values())


def classify_worker(rec: Dict[str, Any], log_size: int, info: Callable[[int], Optional[Dict[str, Any]]], t: float) -> str:
    """QUEUED | RUNNING | STALLED | EXITED | DEAD  (alive != healthy: a live process with a silent log is STALLED)."""
    if not rec.get("pid"):
        return "QUEUED"
    live = info(rec["pid"])
    if live is not None and same_process(rec, live):
        if log_size > rec.get("last_log_size", -1):
            rec["last_log_size"], rec["last_progress"] = log_size, t
        return "STALLED" if t - rec.get("last_progress", rec.get("started", t)) > STALL_SECONDS else "RUNNING"
    if rec.get("exit_code") == 0:
        return "EXITED"
    return "DEAD" if rec.get("exit_code") not in (0,) else "EXITED"


def branch_name(tid: str) -> str:
    return "task/" + tid.lower()


def diff_hash(lay: Layout, tid: str) -> str:
    wt = lay.wt / tid
    base = git(["merge-base", "main", branch_name(tid)], wt)
    d = subprocess.run(["git", "diff", "--binary", base, branch_name(tid)], cwd=str(wt), capture_output=True, timeout=60).stdout
    return hashlib.sha256(d).hexdigest()[:16]


def changed_files(lay: Layout, tid: str) -> List[str]:
    wt = lay.wt / tid
    base = git(["merge-base", "main", branch_name(tid)], wt)
    out = git(["diff", "--name-only", base, branch_name(tid)], wt)
    return [x for x in out.splitlines() if x]


def worktree_dirty(lay: Layout, tid: str) -> str:
    """Tracked changes (staged or not) in the task worktree; untracked files are ignored. '' when clean."""
    return git(["status", "--porcelain", "--untracked-files=no"], lay.wt / tid)


def run_tests(lay: Layout, tid: str, timeout: int = 120) -> Tuple[bool, str]:
    """Run the task's tests against the COMMITTED tree: a dirty worktree is a failure, never a pass."""
    wt = lay.wt / tid
    dirty = worktree_dirty(lay, tid)
    if dirty:
        return False, "uncommitted changes in the worktree (tests must run on the committed tree): " + dirty[:120].replace("\n", "; ")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    r = subprocess.run([PY, "-B", "-m", "unittest", test_module(tid)], cwd=str(wt), capture_output=True, text=True,
                       timeout=timeout, env=env)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or ""))[-400:]


# ------------------------------------------------------------------ local gates (mirror the production gate semantics)

@dataclass
class Gate:
    name: str
    ok: bool
    detail: str


def evaluate_gates(lay: Layout, tid: str, data: Dict[str, Any]) -> List[Gate]:
    t = task_def(tid)
    st = data["tasks"][tid]
    wt = lay.wt / tid
    g: List[Gate] = []
    g.append(Gate("registered", st.get("status") in ("REVIEWED",) and bool(st.get("branch")), f"status {st.get('status')}"))
    head = git(["rev-parse", branch_name(tid)], wt)
    ok, tail = run_tests(lay, tid)
    g.append(Gate("tests_on_head", ok and git(["rev-parse", "HEAD"], wt) == head, f"head {head[:10]}: {'pass' if ok else 'FAIL ' + tail[-120:]}"))
    files = changed_files(lay, tid)
    stray = [f for f in files if f not in owned_paths(t)]
    g.append(Gate("scope", bool(files) and not stray, f"changed {files}" + (f"; OUT OF SCOPE {stray}" if stray else "")))
    dh = diff_hash(lay, tid)
    rv = data["reviews"].get(tid, {})
    workers = {w for w, r in data["workers"].items() if r.get("task") == tid and r.get("role") == "worker"}
    g.append(Gate("review", rv.get("verdict") == "APPROVE" and rv.get("diff_hash") == dh and rv.get("reviewer") not in workers
                  and bool(rv.get("reviewer")), f"review {rv.get('verdict')} for {rv.get('diff_hash')} vs diff {dh} by {rv.get('reviewer')}"))
    added = subprocess.run(["git", "diff", "--binary", git(["merge-base", "main", branch_name(tid)], wt), branch_name(tid)],
                           cwd=str(wt), capture_output=True, text=True, errors="replace", timeout=60).stdout
    hits = [ln for ln in added.splitlines() if ln.startswith("+") and SECRET_RX.search(ln)]
    g.append(Gate("secrets", not hits, f"{len(added.splitlines())} diff lines scanned, {len(hits)} hits"))
    bad = [f for f in files if FORBIDDEN_RX.search(f)]
    g.append(Gate("forbidden_files", not bad, f"{bad or 'none'}"))
    base = git(["merge-base", "main", branch_name(tid)], wt)
    main_head = git(["rev-parse", "main"], lay.main)
    g.append(Gate("up_to_date", base == main_head, f"merge-base {base[:10]} vs main {main_head[:10]}"))
    g.append(Gate("lock_free", not lay.merge_lock.exists(), "no other merge in progress"))
    return g


def integrate(lay: Layout, tid: str, apply: bool) -> Dict[str, Any]:
    with Ledger(lay) as L:
        tested_head = git(["rev-parse", branch_name(tid)], lay.wt / tid)
        gates = evaluate_gates(lay, tid, L.data)
        ok = all(x.ok for x in gates)
        table = [{"gate": x.name, "ok": x.ok, "detail": x.detail} for x in gates]
        if not apply or not ok:
            return {"task": tid, "merged": False, "gates_all_pass": ok, "gates": table, "dry_run": not apply}
        try:
            fd = os.open(str(lay.merge_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            return {"task": tid, "merged": False, "gates_all_pass": False, "gates": table, "error": "merge lock held"}
        try:
            head = tested_head                      # merge exactly the sha the gates evaluated, never a re-read branch tip
            if git(["rev-parse", branch_name(tid)], lay.wt / tid) != head or worktree_dirty(lay, tid):
                return {"task": tid, "merged": False, "gates_all_pass": False, "gates": table,
                        "error": "branch moved or worktree became dirty after the gates ran; re-verify"}
            git(["fetch", "-q", str(lay.wt / tid), branch_name(tid)], lay.main)
            git(["merge", "--no-ff", "-q", "-m", f"Merge {branch_name(tid)} ({tid})", head], lay.main)
            git(["push", "-q", "origin", "main"], lay.main)
            read_back = subprocess.run(["git", "merge-base", "--is-ancestor", head, "origin/main"], cwd=str(lay.main)).returncode == 0
            if not read_back:
                return {"task": tid, "merged": False, "error": "read-back against origin/main failed", "gates": table}
            L.data["tasks"][tid]["status"] = "INTEGRATED"
            L.data["tasks"][tid]["integrated_at"] = iso()
            L.data["tasks"][tid]["merge_commit"] = git(["rev-parse", "main"], lay.main)[:12]
        finally:
            try:
                lay.merge_lock.unlink()
            except OSError:
                pass
    event(lay, "integrated", task=tid, merge=read_ledger(lay)["tasks"][tid]["merge_commit"], gates=len(table))
    return {"task": tid, "merged": True, "gates_all_pass": True, "gates": table, "merge_commit": read_ledger(lay)["tasks"][tid]["merge_commit"]}


# ------------------------------------------------------------------ worker / reviewer command lines

WORKER_RULES = ("You are a worker in a throwaway sandbox. Edit ONLY the file named in the task (your owned path). Do not edit "
                "tests. Use `git add <that file>` (never `git add .`), then `git commit -m <msg>` on the current branch. Do not "
                "push. Run the given test command and make sure it passes before committing. Be brief.")


def worker_prompt(tid: str, repair_note: str = "") -> str:
    t = task_def(tid)
    base = (f"Task {tid}: implement `{t['func']}` in `{t['module']}` as described: {t['title']}. Test command: "
            f"`python -B -m unittest {test_module(tid)}`. Tests are in tests/ and must not be edited.")
    return base + ((" REPAIR: " + repair_note) if repair_note else "")


def reviewer_prompt(tid: str, diff_text: str) -> str:
    t = task_def(tid)
    return (f"You are an independent code reviewer. Review this diff for task {tid} ({t['title']}). It must implement the "
            f"described behaviour, touch only `{t['module']}`, and not hard-code test answers or special-case test inputs.\n\n"
            f"{diff_text[:6000]}\n\nReply with a short justification and END with exactly one line: "
            f"`VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`.")


def parse_verdict(text: str) -> Optional[str]:
    m = re.findall(r"VERDICT:\s*(APPROVE|CHANGES_REQUESTED)", text or "")
    return m[-1] if m else None


def final_text(stream: str) -> str:
    out = ""
    for ln in stream.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("type") == "result" and isinstance(d.get("result"), str):
            out = d["result"]
    return out


def claude_argv(claude: str, model: str, settings_path: str, session_id: str, prompt: str, tools: str,
                resume: bool = False, system: Optional[str] = None) -> List[str]:
    a = [claude, "-p", "--model", model, "--allowedTools", tools, "--output-format", "stream-json", "--verbose"]
    a += (["--resume", session_id] if resume else ["--session-id", session_id])
    if system:
        a += ["--append-system-prompt", system]
    a += [prompt]
    return a


def policy_for(lay: Layout, session_id: str, role: str, repo_root: Path, owned: Optional[List[str]]) -> session_policy.PolicyInstall:
    return session_policy.install(lay.state, session_id, role, posix(repo_root), roots=[posix(repo_root)], owned_paths=owned)


def spawn(argv: List[str], cwd: Path, env: Dict[str, str], log: Path) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "ab")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=flags)


# ------------------------------------------------------------------ ctl: the tool surface a real Main uses

def ctl(lay: Layout, cmd: str, args: Sequence[str], out: Callable[[Any], None], claude: str = "claude",
        model: str = "haiku") -> int:
    def emit(**kw: Any) -> int:
        out(json.dumps(kw, sort_keys=True))
        return 0
    if cmd == "next":
        d = read_ledger(lay)
        el = eligible(d)
        blocked = {k: v.get("note", v["status"]) for k, v in d["tasks"].items() if v["status"] == "BLOCKED"}
        waiting = [t["id"] for t in TASKS if d["tasks"][t["id"]]["status"] == "PENDING" and t["id"] not in el]
        need = [k for k, v in d["tasks"].items() if v["status"] in ("WORKER_DONE", "VERIFIED", "REVIEWED", "CHANGES_NEEDED", "DEAD")]
        return emit(eligible=el, needs_action={k: d["tasks"][k]["status"] for k in need}, waiting_on_dependencies=waiting,
                    blocked=blocked, live_workers=live_workers(d), work_remaining=work_remaining(d))
    if cmd == "claim":
        tid = args[0]
        with Ledger(lay) as L:
            st = L.data["tasks"].get(tid)
            if not st or st["status"] != "PENDING" or tid not in eligible(L.data):
                return emit(ok=False, error="task is not eligible (unknown, not PENDING, or dependency not integrated)")
            wt = lay.wt / tid
            git(["worktree", "add", "-q", "-b", branch_name(tid), str(wt), "main"], lay.main)
            for k, v in (("user.email", "worker@example.invalid"), ("user.name", f"worker-{tid}")):
                git(["config", "--worktree", k, v], wt, check=False)
            st.update(status="CLAIMED", branch=branch_name(tid), worktree=posix(wt), claimed_at=iso())
        event(lay, "claimed", task=tid)
        return emit(ok=True, task=tid, branch=branch_name(tid))
    if cmd in ("dispatch", "recover"):
        tid = args[0]
        repair = " ".join(args[1:]) if cmd == "dispatch" else ""
        with Ledger(lay) as L:
            st = L.data["tasks"].get(tid)
            if not st or st["status"] not in ("CLAIMED", "CHANGES_NEEDED", "DEAD", "WORKING"):
                return emit(ok=False, error=f"cannot dispatch from status {st and st['status']}")
            if any(L.data["workers"][w].get("task") == tid and L.data["workers"][w]["status"] in ("QUEUED", "RUNNING", "STALLED")
                   and L.data["workers"][w].get("role") == "worker" for w in L.data["workers"]):
                if cmd == "recover":
                    stalled = [w for w, r in L.data["workers"].items() if r.get("task") == tid and r["status"] == "STALLED"]
                    if not stalled:
                        return emit(ok=False, error="a worker for this task is alive and not stalled; wait")
                    for w in stalled:
                        L.data["workers"][w]["kill_requested"] = True
                    return emit(ok=True, action="stalled worker will be killed with identity proof; recover again afterwards")
                return emit(ok=False, error="a worker is already live for this task")
            if st["attempts"] >= MAX_ATTEMPTS and cmd == "recover":
                st.update(status="BLOCKED", note=f"{st['attempts']} worker attempts exhausted")
                event(lay, "blocked", task=tid, attempts=st["attempts"])
                return emit(ok=True, task=tid, status="BLOCKED", reason="attempts exhausted")
            st["attempts"] += 1
            wid = f"w-{tid.lower()}-{st['attempts']}"
            L.data["workers"][wid] = {"worker_id": wid, "task": tid, "role": "worker", "status": "QUEUED", "queued": now(),
                                      "repair": repair}
            st["status"] = "WORKING"
        write_atomic(lay.requests / f"{wid}.json", json.dumps({"kind": "worker", "worker_id": wid, "task": tid, "repair": repair}))
        event(lay, "dispatch_requested", task=tid, worker=wid, attempt=st["attempts"], repair=bool(repair))
        return emit(ok=True, worker=wid, attempt=st["attempts"])
    if cmd == "wait":
        timeout = float(args[0]) if args else 60
        before = json.dumps({k: v.get("status") for k, v in read_ledger(lay)["workers"].items()}, sort_keys=True)
        deadline = now() + min(timeout, 100)
        while now() < deadline:
            d = read_ledger(lay)
            cur = json.dumps({k: v.get("status") for k, v in d["workers"].items()}, sort_keys=True)
            if cur != before or not live_workers(d):
                break
            time.sleep(2)
        d = read_ledger(lay)
        return emit(workers={k: v["status"] for k, v in d["workers"].items()}, live=live_workers(d))
    if cmd == "status":
        d = read_ledger(lay)
        return emit(tasks={k: {"status": v["status"], "attempts": v["attempts"], "note": v.get("note", "")} for k, v in d["tasks"].items()},
                    workers={k: {"task": v["task"], "role": v["role"], "status": v["status"], "exit": v.get("exit_code")}
                             for k, v in d["workers"].items()})
    if cmd == "verify":
        tid = args[0]
        with Ledger(lay) as L:
            st = L.data["tasks"].get(tid)
            if not st or st["status"] not in ("WORKER_DONE", "VERIFIED", "REVIEWED", "CHANGES_NEEDED"):
                return emit(ok=False, error=f"cannot verify from status {st and st['status']}")
            files = changed_files(lay, tid)
            ok, tail = run_tests(lay, tid)
            stray = [f for f in files if f not in owned_paths(task_def(tid))]
            if ok and files and not stray:
                st["status"] = "VERIFIED"
                st["verified_head"] = git(["rev-parse", branch_name(tid)], lay.wt / tid)[:12]
            else:
                st["status"] = "CHANGES_NEEDED"
                st["note"] = ("tests failed" if not ok else "") + (f" out-of-scope files {stray}" if stray else "") + ("" if files else " no changes committed")
        event(lay, "verified", task=tid, tests_pass=ok, stray=stray, files=files)
        return emit(task=tid, tests_pass=ok, files=files, out_of_scope=stray, status=read_ledger(lay)["tasks"][tid]["status"],
                    test_tail=tail[-200:] if not ok else "",
                    hint=("dispatch a repair worker: ctl dispatch %s \"<what to fix>\"" % tid) if (stray or not ok) else "next: ctl review " + tid)
    if cmd == "review":
        tid = args[0]
        d = read_ledger(lay)
        if d["tasks"].get(tid, {}).get("status") != "VERIFIED":
            return emit(ok=False, error="task must be VERIFIED before review")
        rid = f"r-{tid.lower()}-{uuid.uuid4().hex[:6]}"
        write_atomic(lay.requests / f"{rid}.json", json.dumps({"kind": "review", "worker_id": rid, "task": tid}))
        with Ledger(lay) as L:
            L.data["workers"][rid] = {"worker_id": rid, "task": tid, "role": "reviewer", "status": "QUEUED", "queued": now()}
        deadline = now() + 100
        while now() < deadline:
            d = read_ledger(lay)
            if d["workers"][rid]["status"] in ("EXITED", "DEAD"):
                break
            time.sleep(2)
        d = read_ledger(lay)
        rv = d["reviews"].get(tid, {})
        if d["workers"][rid]["status"] not in ("EXITED", "DEAD"):
            return emit(ok=False, error="reviewer still running; call ctl review-result " + tid + " later", reviewer=rid)
        if rv.get("reviewer") != rid:
            return emit(ok=False, error="reviewer produced no valid verdict", reviewer=rid)
        with Ledger(lay) as L:
            L.data["tasks"][tid]["status"] = "REVIEWED" if rv["verdict"] == "APPROVE" else "CHANGES_NEEDED"
            if rv["verdict"] != "APPROVE":
                L.data["tasks"][tid]["note"] = "review requested changes"
        return emit(task=tid, verdict=rv["verdict"], reviewer=rid, diff_hash=rv["diff_hash"])
    if cmd == "integrate":
        tid = args[0]
        res = integrate(lay, tid, apply=True)
        if not res["merged"] and not res.get("gates_all_pass"):
            failed = [x["gate"] for x in res["gates"] if not x["ok"]]
            event(lay, "integration_refused", task=tid, failed_gates=failed)
            with Ledger(lay) as L:
                if L.data["tasks"][tid]["status"] == "REVIEWED" and failed:
                    L.data["tasks"][tid]["status"] = "CHANGES_NEEDED"
                    L.data["tasks"][tid]["note"] = "gates failed: " + ",".join(failed)
        return emit(**res)
    if cmd == "stop-hook":
        return stop_hook(lay, out)
    return emit(ok=False, error=f"unknown command {cmd}")


def stop_hook(lay: Layout, out: Callable[[Any], None], max_blocks: int = 400) -> int:
    d = read_ledger(lay)
    with Ledger(lay) as L:
        n = L.data["counters"].get("stop_blocks", 0)
        if work_remaining(L.data) and n < max_blocks:
            L.data["counters"]["stop_blocks"] = n + 1
            out(json.dumps({"decision": "block", "reason": "Autonomy loop: work remains (run `ctl next`). Continue the loop: next -> claim -> "
                            "dispatch -> wait -> verify -> review -> integrate; recover failures; stop only when `ctl next` says "
                            "work_remaining is false."}))
    return 0


# ------------------------------------------------------------------ harness supervisor

def supervise(lay: Layout, claude: str, model: str, stop: Callable[[], bool] = lambda: False, tick: float = 2.0) -> int:
    """Spawn queued workers/reviewers, watch liveness, honour kill requests, resume Main when it dies with work left.
    State lives in the ledger, so a restarted supervisor adopts live processes by identity (pid + create time + marker)."""
    sup_pid = os.getpid()
    procs: Dict[str, subprocess.Popen] = {}
    event(lay, "supervisor_started", pid=sup_pid)
    with Ledger(lay) as L:
        if not L.data.get("main", {}).get("pid"):
            start_main(lay, L.data, claude, model, procs, sup_pid, resume=False)
    while not stop():
        for rp in sorted(lay.requests.glob("*.json")):
            try:
                req = json.loads(rp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rp.unlink(missing_ok=True)
            try:
                launch_request(lay, req, claude, model, procs, sup_pid)
            except Exception as exc:  # noqa: BLE001 - one bad request must not take the supervisor down
                event(lay, "request_rejected", request=rp.name, error=str(exc)[:200])
        with Ledger(lay) as L:
            t = now()
            for wid, rec in L.data["workers"].items():
                if rec["status"] in ("EXITED", "DEAD"):
                    continue
                if wid in procs and procs[wid].poll() is not None:
                    rec["exit_code"] = procs[wid].returncode
                logp = lay.logs / f"{wid}.log"
                size = logp.stat().st_size if logp.exists() else 0
                if rec.get("kill_requested") and rec.get("pid"):
                    done, why = kill_with_proof(rec, sup_pid)
                    rec["kill_requested"] = False
                    event(lay, "worker_kill", worker=wid, done=done, why=why)
                new = classify_worker(rec, size, proc_identity, t) if rec.get("pid") else "QUEUED"
                if new in ("EXITED", "DEAD") and rec["status"] not in ("EXITED", "DEAD"):
                    finish_worker(lay, L.data, wid, rec, new)
                rec["status"] = new
            main = L.data.get("main", {})
            if main.get("pid") and main.get("status") == "RUNNING":
                live = proc_identity(main["pid"])
                if not same_process(main, live):
                    code = procs["main"].returncode if "main" in procs and procs["main"].poll() is not None else None
                    main["status"] = "EXITED_CLEAN" if code == 0 else "DIED"
                    main["ended"], main["exit_code"] = iso(), code
                    event(lay, "main_exited", clean=(code == 0), exit_code=code, session=main["session_id"])
            if main.get("status") == "DIED" and work_remaining(L.data) and main.get("resumes", 0) < 3:
                main["resumes"] = main.get("resumes", 0) + 1
                start_main(lay, L.data, claude, model, procs, sup_pid, resume=True)
            elif main.get("status") == "EXITED_CLEAN" and work_remaining(L.data) and main.get("restarts_after_clean", 0) < 4:
                main["restarts_after_clean"] = main.get("restarts_after_clean", 0) + 1
                start_main(lay, L.data, claude, model, procs, sup_pid, resume=True)
        time.sleep(tick)
    return 0


def finish_worker(lay: Layout, data: Dict[str, Any], wid: str, rec: Dict[str, Any], state: str) -> None:
    tid = rec["task"]
    event(lay, "worker_finished", worker=wid, role=rec["role"], state=state, exit=rec.get("exit_code"))
    if rec["role"] == "reviewer":
        text = final_text((lay.logs / f"{wid}.log").read_text(encoding="utf-8", errors="replace")) if (lay.logs / f"{wid}.log").exists() else ""
        v = parse_verdict(text)
        if v:
            data["reviews"][tid] = {"verdict": v, "reviewer": wid, "diff_hash": rec.get("diff_hash"), "at": iso()}
        return
    st = data["tasks"][tid]
    if st["status"] != "WORKING":
        return
    ahead = git(["rev-list", "--count", f"main..{branch_name(tid)}"], lay.wt / tid, check=False)
    committed = ahead.splitlines()[-1].strip() not in ("0", "") if ahead.startswith("0\n") else False
    if committed and os.environ.get("CS_CHAOS_STRAY") == tid and not data["counters"].get("stray_injected"):
        wt = lay.wt / tid                      # harness-injected failure: an out-of-scope file lands on the branch
        (wt / "stray.txt").write_text("not in scope\n", encoding="utf-8")
        git(["add", "stray.txt"], wt)
        git(["commit", "-q", "-m", "chaos: stray file"], wt)
        data["counters"]["stray_injected"] = tid
        event(lay, "chaos_stray_file_committed", task=tid)
    st["status"] = "WORKER_DONE" if committed else "DEAD"
    if not committed:
        st["note"] = f"worker ended (exit {rec.get('exit_code')}) without a commit"


def launch_request(lay: Layout, req: Dict[str, Any], claude: str, model: str, procs: Dict[str, subprocess.Popen], sup_pid: int) -> None:
    wid, tid = req["worker_id"], req["task"]
    env = dict(os.environ)
    sid = str(uuid.uuid4())
    marker = f"cs-live-{wid}"
    if req["kind"] == "worker":
        wt = lay.wt / tid
        inst = policy_for(lay, sid, "worker", wt, owned_paths(task_def(tid)))
        env.update(inst.env)
        argv = session_policy.argv_with_policy(claude_argv(claude, model, "", sid, worker_prompt(tid, req.get("repair", "")),
                                                          "Bash Edit Write Read", system=WORKER_RULES + f" [{marker}]"), inst)
        cwd, extra = wt, {}
    else:
        wt = lay.wt / tid
        dtext = subprocess.run(["git", "diff", git(["merge-base", "main", branch_name(tid)], wt), branch_name(tid)], cwd=str(wt),
                               capture_output=True, text=True, errors="replace", timeout=60).stdout
        inst = policy_for(lay, sid, "reviewer", wt, [])
        env.update(inst.env)
        argv = session_policy.argv_with_policy(claude_argv(claude, model, "", sid, reviewer_prompt(tid, dtext), "Read",
                                                          system=f"[{marker}] You review only; you cannot edit files."), inst)
        cwd, extra = wt, {"diff_hash": diff_hash(lay, tid)}
    p = spawn(argv, cwd, env, lay.logs / f"{wid}.log")
    procs[wid] = p
    time.sleep(0.5)
    ident = proc_identity(p.pid) or {"exe": "claude", "cmdline": marker, "parent_pid": sup_pid, "create_time": now()}
    with Ledger(lay) as L:
        rec = L.data["workers"].setdefault(wid, {"worker_id": wid, "task": tid, "role": "worker" if req["kind"] == "worker" else "reviewer"})
        rec.update(pid=p.pid, create_time=ident["create_time"], exe=ident["exe"], marker=marker, parent_pid=sup_pid,
                   session_id=sid, started=now(), last_progress=now(), status="RUNNING", **extra)
    event(lay, "process_started", worker=wid, role=req["kind"], pid=p.pid, task=tid, session=sid)


MAIN_PROTOCOL = """You are Main, the autonomous development engineer of a sandbox project. You have NO human to ask; never ask
questions or wait for approval. Work ONLY through the ctl tool (a command is given in $CS_CTL; run it with Bash, e.g.
`$CS_CTL next`). Loop until `next` reports work_remaining false:
1 `next`: pick one eligible task (lowest id). 2 `claim <id>`. 3 `dispatch <id>` (starts a worker). 4 `wait 90`, repeat
until the worker is no longer live (EXITED/DEAD). 5 `verify <id>`: if tests fail or files are out of scope,
`dispatch <id> "<what to fix>"` then wait/verify again; if the worker is DEAD or the task is DEAD, `recover <id>`
(after enough failed attempts the task becomes BLOCKED: leave it, move on). 6 `review <id>` (independent reviewer).
7 `integrate <id>`: if gates fail read which, fix with a repair dispatch or recover, and retry. 8 repeat from 1.
Rules: tasks whose dependencies are not integrated are not eligible; never touch files yourself; never run git
yourself; one task at a time. Keep every message short."""


def start_main(lay: Layout, data: Dict[str, Any], claude: str, model: str, procs: Dict[str, subprocess.Popen], sup_pid: int,
               resume: bool) -> None:
    main = data.setdefault("main", {})
    sid = main.get("session_id") or str(uuid.uuid4())
    inst = policy_for(lay, sid, "main", lay.main, [])
    env = dict(os.environ)
    env.update(inst.env)
    wrapper = lay.root / "ctl"                     # a plain script path: no quoting for the model to get wrong
    write_atomic(wrapper, '#!/bin/sh\nexec "%s" -B -m dev_supervisor.live_autonomy ctl --root "%s" "$@"\n' % (posix(PY), posix(lay.root)))
    env["CS_CTL"] = posix(wrapper)
    env["PYTHONPATH"] = posix(Path(__file__).resolve().parents[1])
    marker = "cs-live-main"
    prompt = "Begin. Run the autonomy loop." if not resume else "You were interrupted. Run `$CS_CTL next` and continue the loop."
    argv = claude_argv(claude, model, "", sid, prompt, "Bash", resume=resume, system=MAIN_PROTOCOL + f" [{marker}]")
    argv = session_policy.argv_with_policy(argv, inst)
    p = spawn(argv, lay.main, env, lay.logs / "main.log")
    procs["main"] = p
    time.sleep(0.5)
    ident = proc_identity(p.pid) or {"create_time": now(), "exe": "claude", "cmdline": marker, "parent_pid": sup_pid}
    main.update(pid=p.pid, create_time=ident["create_time"], marker=marker, session_id=sid, status="RUNNING", parent_pid=sup_pid,
                started=iso(), model=model)
    event(lay, "main_started", pid=p.pid, session=sid, resume=resume)


# ------------------------------------------------------------------ chaos driver for the live run

def _wait_until(pred: Callable[[], bool], timeout: float, step: float = 2.0) -> bool:
    end = now() + timeout
    while now() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def run_live(lay: Layout, a: argparse.Namespace) -> int:  # pragma: no cover - needs real claude processes
    build_sandbox(lay.root)
    write_atomic(lay.main / ".claude" / "settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
        "command": f'"{posix(PY)}" -B -m dev_supervisor.live_autonomy ctl --root "{posix(lay.root)}" stop-hook'}]}]}}, indent=1))
    with open(lay.main / ".git" / "info" / "exclude", "a", encoding="utf-8") as fh:
        fh.write(".claude/\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = posix(Path(__file__).resolve().parents[1])
    env["CS_CHAOS_STRAY"] = "L-003"            # inherited by every supervisor this driver starts

    def start_supervisor() -> subprocess.Popen:
        p = spawn([PY, "-B", "-m", "dev_supervisor.live_autonomy", "supervise", "--root", str(lay.root), "--claude", a.claude,
                   "--model", a.model], lay.root, env, lay.logs / "supervisor.log")
        event(lay, "supervisor_spawned_by_driver", pid=p.pid)
        return p

    started = now()
    sup = start_supervisor()
    sup_rec = {"pid": sup.pid, "marker": SUPERVISOR_MARKER, "parent_pid": os.getpid(), "session_id": "supervisor",
               "create_time": (proc_identity(sup.pid) or {"create_time": now()})["create_time"]}
    done = {"worker_kill": False, "pid_only_refused": False, "stray": False, "main_kill": False, "supervisor_restart": False}
    protected = [p for p in (os.getppid(),)]
    while now() - started < a.timeout:
        d = read_ledger(lay)
        integ = [k for k, v in d["tasks"].items() if v["status"] == "INTEGRATED"]
        # A: kill the first worker that is still young (before it can commit), WITH identity proof; a PID-only kill is
        # tried first and must be refused. The flag is set only when a process was really killed; otherwise retry later.
        for w in d["workers"].values():
            if done["worker_kill"] or w.get("role") != "worker" or w.get("status") != "RUNNING" or not w.get("pid"):
                continue
            if now() - w.get("started", now()) < 4:
                continue
            pid_only = authority.decide(authority.Action(authority.TERMINATE_PROCESS, attrs={"pid": w["pid"]}), authority.PolicyContext())
            event(lay, "chaos_pid_only_kill_refused", worker=w["worker_id"], verdict=pid_only.verdict, rule=pid_only.rule)
            done["pid_only_refused"] = done["pid_only_refused"] or not pid_only.allowed
            ok, why = kill_with_proof(w, sup.pid, protected)
            event(lay, "chaos_worker_kill", worker=w["worker_id"], killed=ok, why=why, pid=w["pid"], task=w.get("task"))
            done["worker_kill"] = ok
        # B (stray out-of-scope commit) is injected deterministically inside finish_worker via CS_CHAOS_STRAY
        done["stray"] = bool(d.get("counters", {}).get("stray_injected"))
        # C: kill Main once two tasks are integrated; the supervisor must resume the same session
        m = d.get("main", {})
        if len(integ) >= 2 and not done["main_kill"] and m.get("status") == "RUNNING":
            ok, why = kill_with_proof(m, sup.pid, protected)
            event(lay, "chaos_main_kill", killed=ok, why=why, session=m.get("session_id"), pid=m.get("pid"))
            done["main_kill"] = True
        # D: restart the supervisor once three tasks are integrated; state must be recovered from the ledger
        if len(integ) >= 3 and not done["supervisor_restart"]:
            ok, why = kill_with_proof(sup_rec, os.getpid(), protected)
            event(lay, "chaos_supervisor_kill", killed=ok, why=why)
            time.sleep(3)
            sup = start_supervisor()
            time.sleep(1)
            sup_rec["pid"] = sup.pid
            sup_rec["create_time"] = (proc_identity(sup.pid) or {"create_time": sup_rec["create_time"]})["create_time"]
            done["supervisor_restart"] = True
        if not work_remaining(d) and not live_workers(d):
            break
        time.sleep(3)
    d = read_ledger(lay)
    report = {"started": iso(started), "ended": iso(), "elapsed_s": round(now() - started), "chaos": done,
              "tasks": {k: {x: v.get(x) for x in ("status", "attempts", "note", "merge_commit", "integrated_at")} for k, v in d["tasks"].items()},
              "main": {x: d.get("main", {}).get(x) for x in ("session_id", "status", "resumes", "restarts_after_clean", "model")},
              "reviews": d["reviews"], "events": [json.loads(l) for l in lay.events_path.read_text(encoding="utf-8").splitlines()]}
    write_atomic(lay.state / "report.json", json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("elapsed_s", "chaos", "tasks", "main")}, indent=1))
    for rec in list(d["workers"].values()) + [d.get("main", {})]:
        if rec.get("pid") and rec.get("status") in ("RUNNING", "STALLED"):
            kill_with_proof(rec, sup.pid, protected)
    kill_with_proof(sup_rec, os.getpid(), protected)
    return 0


# ------------------------------------------------------------------ CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dev_supervisor.live_autonomy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "supervise", "ctl"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        p.add_argument("--claude", default="claude")
        p.add_argument("--model", default="haiku")
        if name == "run":
            p.add_argument("--yes", action="store_true")
            p.add_argument("--timeout", type=float, default=2400)
        if name == "ctl":
            p.add_argument("ctl_cmd")
            p.add_argument("ctl_args", nargs="*")
    a = ap.parse_args(argv)
    lay = Layout(a.root)
    if a.cmd == "ctl":
        return ctl(lay, a.ctl_cmd, a.ctl_args, print, a.claude, a.model)
    if a.cmd == "supervise":
        return supervise(lay, a.claude, a.model)
    if not a.yes:
        print("dry run: would build a throwaway sandbox at the given root and run real claude processes; pass --yes")
        return 0
    return run_live(lay, a)  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
