"""Run a task's ``test_requirements`` and keep the evidence bound to the exact head (T-DEV-065).

``python -B -m dev_supervisor.testreq plan|run|show|check [--task T-XXX-000] [--repo <worktree>] [--state-dir <dir>]``

``test_requirements`` in ACTIVE_TASKS.json is a list of free-text strings. This module does not add a field. Each string is
classified:

* ``command``  matches a fixed grammar (unittest discover/modules, governance validate/handoff/scope, tracker_check, node
               --test). It is run WITHOUT a shell: the string is parsed, validated and turned into an argv.
* ``gate``     prose that an integration gate already decides (independent review, preflight, CI validation).
* ``manual``   anything else. It cannot be run safely, so it is reported as such and blocks the tests check. It is never
               reported as passed.

``run`` executes the command requirements in the task worktree and writes ONE evidence record per task head into the
supervisor state directory (outside the repository). The record holds, per requirement, the exact argv, directory, times,
duration, exit code, an output tail (credentials scrubbed) and one of ``passed failed skipped blocked not_run``. It is
bound to the task id, the head commit, the diff hash and a hash of the requirement list, and carries its own checksum.
``check`` (and integrate's ninth check, ``tests``) accept a record only when all of that matches the head being merged.

The run refuses to start on a dirty worktree or a head other than the one it records, runs with a minimal environment
(no tokens) and a throw-away supervisor state directory, and fails a requirement whose command modifies the worktree,
runs zero tests, times out, or cannot start. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from dev_supervisor.state_store import StateStore, StoreError, decode_envelope, default_directory, scrub, utc_iso

__all__ = ["Req", "classify", "classify_all", "reqs_hash", "EvidenceStore", "run_requirements", "check_evidence",
           "gate_verdict", "evidence_name", "main"]

SCHEMA = 1
DEFAULT_TIMEOUT = 1800.0
TAIL_CHARS = 4000
SCAN_BYTES = 262144
STATUSES = ("passed", "failed", "skipped", "blocked", "not_run")
PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"

# prose requirements that an integration gate decides on the pull request itself
GATE_COVERED = {
    "independent review of the exact diff": "review",
    "integrate --preflight passes on the branch": "scope, secrets, forbidden_files, reserved and checks",
    "full per-file validation on the exact head": "checks",
}
_CD = re.compile(r"^cd (orchestrator|dashboard/desktop) && (?P<rest>.+)$")
_FROM = re.compile(r"^(?P<rest>.+?) from (?:(?P<o>orchestrator/)|the repository root)$")
_PATTERN = re.compile(r"^[A-Za-z0-9_*.]+\.py$")
_MODULE = re.compile(r"^tests\.test_[a-z0-9_]+$")
_GOV = {("validate",), ("validate", "--against", "origin/main"), ("handoff", "--check"), ("scope", "--base", "origin/main")}
_ENV_KEEP = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
             "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
             "COMMONPROGRAMFILES", "ALLUSERSPROFILE", "PUBLIC", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "OS", "LANG",
             "LC_ALL", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL",
             "PROCESSOR_REVISION", "SHELL", "TERM"}


class TestReqError(Exception):
    """A usage or environment problem that stops a run before any requirement is executed."""
    __test__ = False


# ------------------------------------------------------------------ classification

@dataclass(frozen=True)
class Req:
    index: int
    text: str
    kind: str                       # command | gate | manual
    argv: Tuple[str, ...] = ()      # command only; argv[0] is "python" or "node" (python is replaced by sys.executable)
    cwd: str = "."                  # command only; relative to the worktree root
    counts: bool = False            # the output must show at least one executed test
    gate: str = ""                  # gate kind only
    reason: str = ""                # manual kind only

    def label(self) -> str:
        return f"requirement {self.index + 1}"


def _grammar(argv: List[str]) -> Tuple[bool, str]:
    """(counts_tests, "") for an argv the module may run, else (False, reason)."""
    if not argv:
        return False, "empty command"
    exe, rest = argv[0], list(argv[1:])
    if exe == "node":
        if len(rest) == 2 and rest[0] == "--test" and rest[1] == "tests/*.test.js":
            return True, ""
        return False, "node is only run as: node --test tests/*.test.js"
    if exe != "python":
        return False, f"{exe!r} is not an allowed program (python or node)"
    flags: List[str] = []
    while rest and rest[0] in ("-B", "-I"):
        flags.append(rest.pop(0))
    if "-B" not in flags:
        return False, "python must be run with -B"
    if rest[:2] == ["-m", "unittest"]:
        r = rest[2:]
        if r[:1] == ["discover"]:
            r = r[1:]
            if r[:2] not in (["-s", "tests"], ["-s", "orchestrator/tests"]):
                return False, "unittest discover needs -s tests or -s orchestrator/tests"
            r = r[2:]
            if not r or (len(r) == 2 and r[0] == "-p" and _PATTERN.match(r[1])):
                return True, ""
            return False, "unittest discover accepts only -p <file pattern>"
        if r and all(_MODULE.match(m) for m in r):
            return True, ""
        return False, "unittest accepts discover or tests.test_<name> modules"
    if rest[:2] == ["-m", "governance"]:
        if tuple(rest[2:]) in _GOV:
            return False, ""
        return False, "governance is only run as validate [--against origin/main], handoff --check or scope --base origin/main"
    if rest == ["dashboard/tracker_check.py", "--against", "origin/main"]:
        return False, ""
    return False, "python is only run for unittest, governance or dashboard/tracker_check.py"


def classify(text: Any, index: int = 0) -> Req:
    if not isinstance(text, str) or not text.strip():
        return Req(index, str(text), "manual", reason="the requirement is empty or not a string")
    t = " ".join(text.split())
    if t in GATE_COVERED:
        return Req(index, text, "gate", gate=GATE_COVERED[t])
    work, cwd = t, "."
    if work.startswith("focused: "):
        work = work[len("focused: "):]
    m = _CD.match(work)
    if m:
        cwd, work = m.group(1), m.group("rest")
    m = _FROM.match(work)
    if m:
        if cwd != ".":
            return Req(index, text, "manual", reason="both 'cd' and 'from' name a directory")
        work = m.group("rest")
        cwd = "orchestrator" if m.group("o") else "."
    if any(c in work for c in "&|;<>`$()\n") or "&&" in work:
        return Req(index, text, "manual", reason="not a single plain command (shell syntax or prose)")
    try:
        argv = shlex.split(work)
    except ValueError:
        return Req(index, text, "manual", reason="unbalanced quotes")
    counts, why = _grammar(argv)
    if why:
        return Req(index, text, "manual", reason=why)
    return Req(index, text, "command", argv=tuple(argv), cwd=cwd, counts=counts)


def classify_all(texts: Any) -> List[Req]:
    if not isinstance(texts, list) or not texts:
        raise TestReqError("test_requirements is missing or empty")
    return [classify(t, i) for i, t in enumerate(texts)]


def reqs_hash(texts: Sequence[Any]) -> str:
    return hashlib.sha256(json.dumps(list(texts), ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ evidence store

def evidence_name(task_id: str, head: str) -> str:
    return "tev_" + re.sub(r"[^a-z0-9]", "_", task_id.lower()) + "_" + re.sub(r"[^0-9a-f]", "_", head[:12].lower())


def _seal(record: Dict[str, Any]) -> Dict[str, Any]:
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    record["record_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                                        separators=(",", ":")).encode("utf-8")).hexdigest()
    return record


def _sealed(record: Dict[str, Any]) -> bool:
    return isinstance(record, dict) and record.get("record_sha256") == _seal(dict(record))["record_sha256"]


class EvidenceStore:
    """Evidence records in the ``test_evidence`` subdirectory of the supervisor state directory (never inside the repository).
    A subdirectory of its own, so that the Supervisor's files and their temporary files are never touched. Reading is
    pure (no directory is created, no temp file removed, no damaged file quarantined or rewritten); only ``save`` opens
    the state store."""

    def __init__(self, directory: Optional[os.PathLike] = None, *, clock: Callable[[], float] = time.time):
        self.directory = Path(os.path.abspath(str(directory if directory is not None else default_directory()))) / "test_evidence"
        self._clock = clock
        self._store: Optional[StateStore] = None

    @property
    def store(self) -> StateStore:
        if self._store is None:
            self._store = StateStore(self.directory, create=True, clock=self._clock)
        return self._store

    def load(self, task_id: str, head: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """(record, "") or (None, reason). The record file is tried first, then its backup; nothing is written."""
        name = evidence_name(task_id, head)
        problems: List[str] = []
        for path in (self.directory / f"{name}.json", self.directory / f"{name}.json.bak"):
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                continue
            except OSError as e:
                problems.append(type(e).__name__)
                continue
            try:
                data = decode_envelope(raw, name)
            except (ValueError, StoreError):
                problems.append("damaged")
                continue
            return (data, "") if isinstance(data, dict) else (None, "not a record")
        return None, (f"unreadable ({', '.join(problems)})" if problems else "missing")

    def save(self, record: Dict[str, Any]) -> Dict[str, Any]:
        clean = _seal(scrub(record))
        self.store.write(evidence_name(clean["task"], clean["head"]), clean)
        return clean


# ------------------------------------------------------------------ verdict

def _pid_alive(pid: Any) -> bool:
    from dev_supervisor import integrate
    return integrate._default_pid_alive(pid)


def check_evidence(reqs: Sequence[Req], texts: Sequence[str], record: Optional[Dict[str, Any]], why_missing: str, *,
                   task_id: str, head: str, diff_sha: str, pid_alive: Callable[[int], bool] = _pid_alive) -> Tuple[str, str]:
    """(PASS|FAIL, detail): does this record satisfy the task's requirements for exactly this head and diff?"""
    manual = [r for r in reqs if r.kind == "manual"]
    if manual:
        r = manual[0]
        return FAIL, (f"{r.label()} is not machine-executable ({r.reason}) and no gate covers it: {r.text[:80]!r}; "
                      "rewrite it as an exact command (see TEST_EVIDENCE.md)")
    cmds = [r for r in reqs if r.kind == "command"]
    covered = len(reqs) - len(cmds)
    if not cmds:
        return PASS, f"no executable requirement; {covered} covered by gates"
    run_cmd = f"python -B -m dev_supervisor.testreq run --task {task_id}"
    if record is None:
        return FAIL, f"no test evidence for head {head[:12]} ({why_missing}); run: {run_cmd}"
    if not _sealed(record):
        return FAIL, f"the evidence record for head {head[:12]} is damaged (checksum mismatch); run: {run_cmd}"
    for key, want in (("schema", SCHEMA), ("task", task_id), ("head", head), ("diff_sha256", diff_sha),
                      ("reqs_sha256", reqs_hash(texts))):
        if record.get(key) != want:
            name = {"head": "head commit", "diff_sha256": "diff", "reqs_sha256": "requirement list", "task": "task",
                    "schema": "format"}[key]
            return FAIL, f"the evidence is for a different {name}; run: {run_cmd}"
    state = record.get("state")
    if state == "running":
        if pid_alive(record.get("pid")):
            return FAIL, "a test run for this head is still in progress"
        return FAIL, f"the last test run was interrupted (its process is gone); run: {run_cmd} --resume"
    if state != "complete":
        return FAIL, f"the last test run did not finish (state {state}); run: {run_cmd} --resume"
    results = record.get("results") if isinstance(record.get("results"), list) else []
    by_index = {r.get("index"): r for r in results if isinstance(r, dict)}
    for r in cmds:
        got = by_index.get(r.index)
        if not got or got.get("requirement") != r.text:
            return FAIL, f"{r.label()} has no result in the evidence; run: {run_cmd}"
        if got.get("status") != "passed":
            extra = f": {got.get('reason')}" if got.get("reason") else ""
            return FAIL, (f"{r.label()} {got.get('status')}{extra} - {r.text[:80]!r}; "
                          f"details: python -B -m dev_supervisor.testreq show --task {task_id}")
    last = max((str(x.get("finished") or "") for x in results if isinstance(x, dict)), default="")
    return PASS, (f"{len(cmds)} command requirement(s) passed for head {head[:12]} diff {diff_sha[:16]} (recorded {last or '?'});"
                  f" {covered} covered by gates")


def gate_verdict(tc: Optional[Dict[str, Any]], head: str, diff_sha: str, evstore: EvidenceStore, *,
                 pid_alive: Callable[[int], bool] = _pid_alive) -> Tuple[str, str]:
    """The decision of integrate's ninth check from the pull request facts ``tc`` (branch, reg_only, wave, registry)."""
    if not tc or not head or not diff_sha:
        return UNKNOWN, "the pull request facts could not be read"
    if tc.get("wave"):
        return FAIL, ("wave pull request: test evidence for lane tasks is not supported yet, so the merge is refused here; "
                      "a wave merge needs an explicit decision (see TEST_EVIDENCE.md)")
    if tc.get("reg_only"):
        return PASS, "registry-only record: no code to test"
    registry = tc.get("registry")
    if not isinstance(registry, dict):
        return UNKNOWN, "ACTIVE_TASKS.json on the base branch is not readable"
    from governance import tasks as gtasks
    found = [t for t in registry.get("tasks", []) if isinstance(t, dict) and t.get("branch") == tc.get("branch")
             and t.get("status") in gtasks.ACTIVE_STATUSES]
    if len(found) != 1:
        return FAIL, f"{len(found)} active task(s) claim branch {tc.get('branch')}; the test requirements cannot be determined"
    task = found[0]
    texts = task.get("test_requirements")
    try:
        reqs = classify_all(texts)
    except TestReqError as e:
        return FAIL, f"{task['id']}: {e}"
    record, why = evstore.load(task["id"], head)
    return check_evidence(reqs, texts, record, why, task_id=task["id"], head=head, diff_sha=diff_sha, pid_alive=pid_alive)


# ------------------------------------------------------------------ execution

def clean_env(base: Dict[str, str], state_dir: str) -> Dict[str, str]:
    """A minimal environment: no tokens, no inherited supervisor directory, no bytecode files."""
    env = {k: v for k, v in base.items() if k.upper() in _ENV_KEEP}
    env["CREATORSTUDIO_DEV_SUPERVISOR_DIR"] = state_dir
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)       # the child leads its own session (see _execute)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def _execute(argv: Sequence[str], cwd: str, env: Dict[str, str], timeout: float, out_path: str) -> Tuple[Optional[int], bool, str]:
    """(exit code, timed out, start error). Output goes to ``out_path``. Only the child process tree is ever killed."""
    with open(out_path, "wb") as fh:
        try:
            proc = subprocess.Popen(list(argv), cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **({} if os.name == "nt" else {"start_new_session": True}))
        except OSError as e:
            return None, False, f"cannot start: {type(e).__name__}"
        try:
            return proc.wait(timeout=timeout), False, ""
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return None, True, ""
        except BaseException:
            _kill_tree(proc)
            raise


def spawn_run(task_id: str, worktree: str, state_dir: str, *, resume: bool = False, timeout: float = DEFAULT_TIMEOUT,
              popen: Optional[Callable[..., Any]] = None) -> "subprocess.Popen[bytes]":
    """Start ``testreq run`` for one task as a child process and return at once (T-DEV-073).

    The argument list is fixed text plus the task id, the worktree, the state directory and the timeout: nothing from a
    task record is ever passed on a command line, and the child itself runs only what the requirement grammar allows. The
    environment is the same minimal one the runner gives its own children (no tokens). The child has its own process group
    (a Ctrl+C in the Supervisor's window does not stop a run) and no console window; it writes the evidence record, not a log."""
    if not isinstance(task_id, str) or not re.fullmatch(r"T-[A-Z0-9]+-\d{3}", task_id):
        raise TestReqError(f"not a task id: {str(task_id)[:40]!r}")
    argv = [sys.executable, "-B", "-m", "dev_supervisor.testreq", "run", "--task", task_id, "--repo", str(worktree),
            "--state-dir", str(state_dir), "--timeout", f"{float(timeout):g}"] + (["--resume"] if resume else [])
    if os.name == "nt":
        flags = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x08000000}   # + CREATE_NO_WINDOW
    else:
        flags = {"start_new_session": True}
    return (popen or subprocess.Popen)(argv, cwd=str(Path(__file__).resolve().parents[1]), env=clean_env(dict(os.environ), str(state_dir)),
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)


_RAN = re.compile(r"(?m)^Ran (\d+) tests? in ")
_SKIPPED = re.compile(r"skipped=(\d+)")
_NODE = re.compile(r"(?m)^(?:#|\u2139) (tests|skip|skipped) (\d+)\s*$")     # TAP ('# tests 2') and the spec reporter
_FAILLINE = re.compile(r"(?m)^(?:FAIL|ERROR): .+$")


def parse_counts(output: str) -> Tuple[Optional[int], Optional[int]]:
    """(tests run, tests skipped) from unittest or node:test output; None when the summary line is absent."""
    ran = _RAN.findall(output)
    if ran:
        sk = _SKIPPED.findall(output.rsplit("Ran ", 1)[-1])
        return int(ran[-1]), int(sk[-1]) if sk else 0
    node = {k: int(v) for k, v in _NODE.findall(output)}
    if "tests" in node:
        return node["tests"], node.get("skip", node.get("skipped", 0))
    return None, None


def _tail(path: str) -> Tuple[str, str]:
    """(output tail for the record, the last SCAN_BYTES decoded for parsing)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - SCAN_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return "", ""
    text = scrub(text)                      # scrub the whole window first: a cut must not leave half a credential
    return text[-TAIL_CHARS:], text


_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def _status(git: Callable) -> Optional[Tuple[str, str]]:
    """(head commit, text of the changed/untracked entries) from ONE ``git status`` call; None when it cannot be read.
    Ignored files are not listed, so ``-B`` bytecode and similar never count as a change."""
    rc, out = git(["--no-optional-locks", "status", "--porcelain=v2", "--branch"])
    if rc != 0:
        return None
    head, changes = "", []
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("# branch.oid "):
            head = line[len("# branch.oid "):].strip()
        elif line and not line.startswith("#"):
            changes.append(line)
    return (head, "\n".join(changes)) if _OID.match(head) else None


def _head(git: Callable) -> str:
    rc, out = git(["rev-parse", "HEAD"])
    return out.decode("utf-8", "replace").strip() if rc == 0 else ""


def _iso(clock: Callable[[], float]) -> str:
    return utc_iso(clock())


def _blank(r: Req, status: str, reason: str = "") -> Dict[str, Any]:
    d: Dict[str, Any] = {"index": r.index, "requirement": r.text, "kind": r.kind, "status": status, "reason": reason}
    if r.kind == "gate":
        d["gate"] = r.gate
    return d


def run_requirements(root: Path, task_id: str, branch: str, texts: Sequence[str], git: Callable, evstore: EvidenceStore, *,
                     resume: bool = False, timeout: float = DEFAULT_TIMEOUT, base: str = "origin/main",
                     clock: Callable[[], float] = time.time, executor: Callable = _execute,
                     which: Callable[[str], Optional[str]] = shutil.which,
                     pid_alive: Callable[[int], bool] = _pid_alive, pid: Optional[int] = None,
                     force: bool = False) -> Dict[str, Any]:
    """Run the command requirements in ``root`` and store the record. Raises TestReqError when nothing can be run safely."""
    from dev_supervisor import integrate
    reqs = classify_all(list(texts))
    start = _status(git)
    diff = integrate.diff_hash(git, base, "HEAD") or ""
    if start is None or not diff:
        raise TestReqError(f"cannot read the head commit or the diff against {base}")
    head = start[0]
    prev, _ = evstore.load(task_id, head)
    me = os.getpid() if pid is None else pid
    if (prev and prev.get("state") == "running" and prev.get("pid") != me and pid_alive(prev.get("pid")) and not force):
        raise TestReqError("another test run for this head is in progress (a reused process id looks the same; "
                           "if you know it is dead, run again with --ignore-running)")
    keep: Dict[int, Dict[str, Any]] = {}
    if resume and prev and _sealed(prev) and all(prev.get(k) == v for k, v in (
            ("schema", SCHEMA), ("task", task_id), ("head", head), ("diff_sha256", diff), ("reqs_sha256", reqs_hash(texts)))):
        keep = {x["index"]: x for x in prev.get("results", []) if isinstance(x, dict) and x.get("status") == "passed"
                and x.get("kind") == "command"}
    results = [dict(keep[r.index], reused=True) if r.index in keep and keep[r.index].get("requirement") == r.text
               else _blank(r, "blocked" if r.kind == "manual" else ("not_run" if r.kind == "command" else "skipped"),
                           r.reason if r.kind == "manual" else ("" if r.kind == "command" else f"not run here; decided by gate: {r.gate}"))
               for r in reqs]
    record: Dict[str, Any] = {
        "schema": SCHEMA, "task": task_id, "branch": branch, "head": head, "diff_sha256": diff, "reqs_sha256": reqs_hash(texts),
        "state": "running", "pid": me, "attempt": int((prev or {}).get("attempt") or 0) + 1, "started": _iso(clock),
        "finished": None, "results": results, "verdict": "not_run"}

    def save() -> Dict[str, Any]:
        return evstore.save(record)

    def finish(state: str) -> Dict[str, Any]:
        record["state"], record["finished"] = state, _iso(clock)
        ok = state == "complete" and all(x["status"] == "passed" for x in results if x["kind"] != "gate")
        record["verdict"] = "passed" if ok else "failed"
        return save()      # the sealed, scrubbed copy that was stored

    save()
    if start[1]:
        why = "the worktree has uncommitted changes; commit or stash them so the evidence describes the commit"
        for r, x in zip(reqs, results):
            if r.kind == "command" and not x.get("reused"):
                x.update(status="blocked", reason=why)
        return finish("complete")
    state_tmp = tempfile.mkdtemp(prefix="testreq_state_")
    try:
        env = clean_env(dict(os.environ), state_tmp)
        stop = ""
        for r, x in zip(reqs, results):
            if r.kind != "command" or x.get("reused"):
                continue
            if stop:
                x.update(status="not_run", reason=stop)
                continue
            exe = sys.executable if r.argv[0] == "python" else which(r.argv[0])
            if not exe:
                x.update(status="blocked", reason=f"runner not found: {r.argv[0]}")
                save()
                continue
            argv = [exe, *r.argv[1:]]
            cwd = str(root / r.cwd) if r.cwd != "." else str(root)
            if not os.path.isdir(cwd):
                x.update(status="blocked", reason=f"directory not found: {r.cwd}")
                save()
                continue
            out_path = os.path.join(state_tmp, f"out{r.index}.txt")
            x.update(argv=list(r.argv), cwd=r.cwd, started=_iso(clock), status="not_run", reason="interrupted")
            save()
            t0 = clock()
            try:
                code, timed_out, start_err = executor(argv, cwd, env, timeout, out_path)
            except KeyboardInterrupt:
                x.update(finished=_iso(clock), reason="interrupted by the operator")
                for y in results:
                    if y["status"] == "not_run" and not y.get("reason"):
                        y["reason"] = "interrupted before it ran"
                finish("interrupted")
                raise
            tail, scan = _tail(out_path)
            ran, skipped = parse_counts(scan) if r.counts else (None, None)
            x.update(finished=_iso(clock), duration_s=round(max(0.0, clock() - t0), 3), exit_code=code,
                     output_tail=tail, tests_run=ran, tests_skipped=skipped, reason="")
            if start_err:
                x.update(status="blocked", reason=start_err)
            elif timed_out:
                x.update(status="failed", reason=f"timed out after {timeout:g} s")
            elif code != 0:
                bad = _FAILLINE.findall(scan)[:10]
                x.update(status="failed", reason=f"exit code {code}" + ("; ran no tests" if r.counts and not ran else "") +
                         (f"; {len(bad)} failing: " + " | ".join(b[:80] for b in bad) if bad else ""))
            elif r.counts and not ran:
                x.update(status="failed", reason="the command exited 0 but ran no tests (or printed no summary)")
            elif r.counts and skipped is not None and skipped >= ran:
                x.update(status="failed", reason=f"all {ran} test(s) were skipped")
            else:
                x.update(status="passed", reason="")
            after = _status(git)
            if after is None or after[0] != head:
                x.update(status="failed", reason="the head commit changed during the run (or the worktree status is unreadable)")
                stop = "an earlier requirement invalidated the run"
            elif after[1]:
                x.update(status="failed", reason="the command modified the worktree: " +
                         " | ".join(l[:60] for l in after[1].splitlines()[:3]))
                stop = "an earlier requirement modified the worktree"
            save()
        return finish("complete")
    finally:
        shutil.rmtree(state_tmp, ignore_errors=True)


# ------------------------------------------------------------------ CLI

def _toplevel(path: Path) -> Path:
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise TestReqError(f"cannot run git: {type(e).__name__}") from None
    if r.returncode != 0 or not r.stdout.strip():
        raise TestReqError(f"{path} is not inside a git worktree")
    return Path(r.stdout.strip()).resolve()


def _git_runner(root: Path) -> Callable:
    from dev_supervisor import integrate
    return integrate.real_git_runner(root)


def _resolve(root: Path, git: Callable, task_id: str, base: str) -> Tuple[str, str, List[str]]:
    """(task id, branch, requirement texts) from the registry on <base>, the same source integrate uses."""
    rc, out = git(["show", f"{base}:ACTIVE_TASKS.json"])
    if rc != 0:
        raise TestReqError(f"cannot read ACTIVE_TASKS.json on {base}")
    try:
        doc = json.loads(out.decode("utf-8"))
    except ValueError:
        raise TestReqError("ACTIVE_TASKS.json is not valid JSON") from None
    rc, br = git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = br.decode("utf-8", "replace").strip() if rc == 0 else ""
    found = [t for t in doc.get("tasks", []) if isinstance(t, dict) and (t.get("id") == task_id if task_id else t.get("branch") == branch)]
    if len(found) != 1:
        raise TestReqError(f"{len(found)} task(s) match " + (task_id or f"branch {branch!r}") + f" on {base}; pass --task")
    t = found[0]
    return str(t["id"]), str(t.get("branch") or branch), list(t.get("test_requirements") or [])


def _print_plan(reqs: Sequence[Req], out: Any) -> None:
    for r in reqs:
        if r.kind == "command":
            print(f"  {r.index + 1}. COMMAND in {r.cwd}: {' '.join(r.argv)}" + ("  [must run >=1 test]" if r.counts else ""), file=out)
        elif r.kind == "gate":
            print(f"  {r.index + 1}. GATE ({r.gate}): {r.text}", file=out)
        else:
            print(f"  {r.index + 1}. MANUAL, not run and not passable: {r.text[:100]!r} ({r.reason})", file=out)


def _print_record(rec: Dict[str, Any], out: Any) -> None:
    print(f"task {rec.get('task')} head {str(rec.get('head'))[:12]} diff {rec.get('diff_sha256')} state {rec.get('state')} "
          f"attempt {rec.get('attempt')} verdict {rec.get('verdict')}", file=out)
    for x in rec.get("results", []):
        dur = f" {x['duration_s']}s" if x.get("duration_s") is not None else ""
        n = f" ({x['tests_run']} tests)" if x.get("tests_run") is not None else ""
        print(f"  {x['index'] + 1}. {str(x['status']).upper()}{dur}{n}" + (" [reused]" if x.get("reused") else "") +
              (f" - {x['reason']}" if x.get("reason") else "") + f" :: {x['requirement'][:90]}", file=out)


def main(argv: Optional[Sequence[str]] = None, *, git: Optional[Callable] = None, out: Any = None,
         executor: Callable = _execute) -> int:
    ap = argparse.ArgumentParser(prog="dev_supervisor.testreq", description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=("plan", "run", "show", "check"))
    ap.add_argument("--task", default="", help="task id (default: the task whose branch is checked out)")
    ap.add_argument("--repo", default=".", help="the task worktree (default: current directory)")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--resume", action="store_true", help="keep results that already passed for this exact head")
    ap.add_argument("--ignore-running", action="store_true",
                    help="start even though the record says a run is in progress (use only when that process is known to be dead)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per requirement (default 1800)")
    a = ap.parse_args(argv)
    out = out or sys.stdout
    root = Path(a.repo).resolve()
    try:
        if git is None:
            root = _toplevel(root)      # requirement directories are relative to the worktree root, not to where it was called
            git = _git_runner(root)
        tid, branch, texts = _resolve(root, git, a.task, a.base)
        reqs = classify_all(texts)
        print(f"{tid} on {branch}: {len(reqs)} requirement(s)", file=out)
        if a.action == "plan":
            _print_plan(reqs, out)
            return 1 if any(r.kind == "manual" for r in reqs) else 0
        ev = EvidenceStore(a.state_dir)
        if a.action == "run":
            _print_plan(reqs, out)
            rec = run_requirements(root, tid, branch, texts, git, ev, resume=a.resume, timeout=a.timeout, base=a.base,
                                   executor=executor, force=a.ignore_running)
            _print_record(rec, out)
            return 0 if rec["verdict"] == "passed" else 1
        from dev_supervisor import integrate
        head, diff = _head(git), integrate.diff_hash(git, a.base, "HEAD") or ""
        rec, why = ev.load(tid, head) if head else (None, "no head")
        if a.action == "show":
            if rec is None:
                print(f"no evidence for head {head[:12]}: {why}", file=out)
                return 1
            _print_record(rec, out)
            for x in rec.get("results", []):
                if x.get("status") == "failed" and x.get("output_tail"):
                    print(f"--- output tail of requirement {x['index'] + 1} ---\n{x['output_tail']}", file=out)
            return 0
        status, detail = check_evidence(reqs, texts, rec, why, task_id=tid, head=head, diff_sha=diff)
        print(f"tests check: {status} - {detail}", file=out)
        return 0 if status == PASS else 1
    except (TestReqError, StoreError, OSError) as e:
        print(f"refused: {e if isinstance(e, TestReqError) else type(e).__name__ + ' while using the evidence store'}", file=out)
        return 2
    except KeyboardInterrupt:
        print("interrupted: the evidence record says so; re-run with --resume", file=out)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
