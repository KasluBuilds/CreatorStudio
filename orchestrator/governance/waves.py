"""Waves: many Claude Code lanes working in parallel, integrated through ONE pull request to main.

Standard library only. See WAVE_PROTOCOL.md for the procedure; this module is the machinery behind it.

Model
  - A wave is an integration branch `wave/<wave_id>` cut from main. Its manifest `waves/<wave_id>/wave.json`
    assigns every lane its owner, branch, owned paths, test modules and acceptance criteria. The manifest is
    read from the REMOTE wave branch, never from a lane's own copy, so a lane cannot widen its own scope.
  - Each lane works in its own worktree on `lane/<wave_id>/<lane_id>`, edits only its owned paths plus its
    private directory `waves/<wave_id>/lanes/<lane_id>/`, and never touches a shared registry.
  - `submit` merges the wave tip into the lane (conflicts surface to the lane), validates, then pushes to the wave
    branch with a plain fast-forward-only push. The push is the lock: if someone else pushed first, it is
    rejected and the lane retries. No claim, step-start, lease-renewal or closeout pull requests are needed.
  - `close` (integrator only) writes the registry, tracker, changelog and handoff records for the whole wave in
    one commit on the wave branch. The wave then merges to main in one pull request.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import handoff as handoff_mod
from . import tasks as tasks_mod

SCHEMA_VERSION = 1
WAVE_ID = re.compile(r"^w\d{2,3}-[a-z0-9][a-z0-9-]*$")
LANE_ID = re.compile(r"^L\d{1,2}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEST_MODULE = re.compile(r"^test_[a-z0-9_]+\.py$")
GRANTORS = ("lukas", "integrator")
APPROVERS = ("chatgpt", "repository_coordinator", "lukas")
REPORT_STATUSES = ("DONE", "IN_PROGRESS", "BLOCKED")
MAX_LANES = 50
SECRET = re.compile(r"((^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
                    r"|BEGIN [A-Z ]*PRIVATE KEY)")
# Paths no lane may ever own: machine-local data, CI and hook machinery, launchers, the wave records themselves.
LANE_FORBIDDEN = (".github", ".githooks", "waves", "tools", "Incoming", "Projects", "test_media", ".git", ".claude",
                  "pro.ps1", "backup.ps1", ".mcp.json", ".gitignore", ".gitattributes")
DEFAULT_SHARED = ("ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json", "CHANGELOG.md", "SESSION_HANDOFF.md",
                  "PROJECT_STATUS.json", "CLAUDE.md", "PROJECT_OPERATING_PROTOCOL.md", "DEVELOPMENT_STEP_PROTOCOL.md")
CLOSEOUT_FILES = ("ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json", "CHANGELOG.md", "SESSION_HANDOFF.md")


def _nonempty(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _strs(v: Any, allow_empty: bool = True) -> bool:
    return isinstance(v, list) and all(_nonempty(x) for x in v) and (allow_empty or bool(v))


def wave_dir(wave_id: str) -> str:
    return f"waves/{wave_id}/"


def lane_dir(wave_id: str, lane_id: str) -> str:
    return f"waves/{wave_id}/lanes/{lane_id}/"


def manifest_path(wave_id: str) -> str:
    return f"waves/{wave_id}/wave.json"


def lane_branch(wave_id: str, lane_id: str) -> str:
    return f"lane/{wave_id}/{lane_id}"


def wave_branch(wave_id: str) -> str:
    return f"wave/{wave_id}"


def lanes_by_id(m: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {l["id"]: l for l in m.get("lanes", []) if isinstance(l, dict) and "id" in l}


# ------------------------------------------------------------------ manifest validation

def validate_manifest(m: Any, registry: Optional[Dict[str, Any]] = None, allow_auto_base: bool = False) -> List[str]:
    """Problems with a wave manifest. `registry` (ACTIVE_TASKS.json) enables the cross-checks with existing tasks.
    `allow_auto_base` accepts base_commit "AUTO" (a manifest not yet started with `wave init`)."""
    if not isinstance(m, dict):
        return ["manifest must be a JSON object"]
    p: List[str] = []
    if m.get("schema_version") != SCHEMA_VERSION:
        p.append(f"schema_version must be {SCHEMA_VERSION}")
    wid = m.get("wave_id")
    if not (isinstance(wid, str) and WAVE_ID.match(wid)):
        p.append(f"wave_id must match {WAVE_ID.pattern}")
        wid = None
    if not _nonempty(m.get("title")):
        p.append("title is required")
    if wid and m.get("integration_branch") != wave_branch(wid):
        p.append(f"integration_branch must be {wave_branch(wid)!r}")
    if not (isinstance(m.get("base_commit"), str) and (SHA.match(m["base_commit"])
                                                       or (allow_auto_base and m["base_commit"] == "AUTO"))):
        p.append("base_commit must be a full 40-character SHA (the main commit the wave starts from)")
    if not (isinstance(m.get("created"), str) and DATE.match(m["created"])):
        p.append("created must be YYYY-MM-DD")
    ih = m.get("inactivity_hours")
    if not (type(ih) is int and 1 <= ih <= 720):
        p.append("inactivity_hours must be an integer from 1 to 720")
    lanes = m.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        return p + ["lanes must be a non-empty list"]
    if len(lanes) > MAX_LANES:
        p.append(f"at most {MAX_LANES} lanes per wave")
    shared = set(DEFAULT_SHARED)
    reg_tasks: Dict[str, dict] = {}
    packages: Dict[str, Any] = {}
    if isinstance(registry, dict):
        shared |= set((registry.get("shared_files") or {}).keys())
        reg_tasks = {t.get("id"): t for t in registry.get("tasks", []) if isinstance(t, dict)}
        packages = registry.get("package_owners") or {}
    ids: Dict[str, dict] = {}
    task_ids: Dict[str, str] = {}
    for idx, lane in enumerate(lanes):
        label = f"lanes[{idx}]"
        if not isinstance(lane, dict):
            p.append(f"{label}: must be an object")
            continue
        lid = lane.get("id")
        if not (isinstance(lid, str) and LANE_ID.match(lid)):
            p.append(f"{label}: id must match {LANE_ID.pattern}")
            continue
        label = f"lane {lid}"
        if lid in ids:
            p.append(f"{label}: duplicate lane id")
            continue
        ids[lid] = lane
        if not _nonempty(lane.get("title")):
            p.append(f"{label}: title is required")
        tid = lane.get("task_id")
        if not (isinstance(tid, str) and tasks_mod.TASK_ID.match(tid)):
            p.append(f"{label}: task_id must match {tasks_mod.TASK_ID.pattern}")
        else:
            if tid in task_ids:
                p.append(f"{label}: task_id {tid} is also used by lane {task_ids[tid]}")
            task_ids[tid] = lid
            if tid in reg_tasks:
                p.append(f"{label}: task_id {tid} already exists in ACTIVE_TASKS.json")
        if not (isinstance(lane.get("owner"), str) and tasks_mod.OWNER.match(lane["owner"])
                and lane["owner"].startswith("claude:")):
            p.append(f"{label}: owner must be a Claude Code stream like 'claude:asset-stream'")
        if wid and lane.get("branch") != lane_branch(wid, lid):
            p.append(f"{label}: branch must be {lane_branch(wid, lid)!r}")
        for f in ("acceptance_criteria",):
            if not _strs(lane.get(f), allow_empty=False):
                p.append(f"{label}: {f} must be a non-empty list of strings")
        tm = lane.get("test_modules")
        if not (isinstance(tm, list) and tm and all(isinstance(x, str) and TEST_MODULE.match(x) for x in tm)):
            p.append(f"{label}: test_modules must be a non-empty list of names like test_assets.py")
        if not _strs(lane.get("depends_on", [])):
            p.append(f"{label}: depends_on must be a list of lane ids")
        auths = lane.get("authorizations", [])
        if not isinstance(auths, list):
            p.append(f"{label}: authorizations must be a list")
            auths = []
        for a in auths:
            if not (isinstance(a, dict) and a.get("granted_by") in GRANTORS and _nonempty(a.get("path"))
                    and _nonempty(a.get("reference"))):
                p.append(f"{label}: each authorization needs path, reference and granted_by in {list(GRANTORS)}")
        paths = lane.get("owned_paths")
        if not _strs(paths, allow_empty=False):
            p.append(f"{label}: owned_paths must be a non-empty list of paths")
            continue
        for path in paths:
            msg = tasks_mod.path_problem(path)
            if msg:
                p.append(f"{label}: {msg}")
                continue
            for bad in LANE_FORBIDDEN:
                if tasks_mod.overlaps(path, bad):
                    p.append(f"{label}: {path!r} is reserved (lanes may not own {bad!r})")
            for sh in shared:
                if tasks_mod.overlaps(path, sh):
                    p.append(f"{label}: {path!r} covers shared file {sh!r}; shared records are written only by `wave close`")
            granted = [a for a in auths if isinstance(a, dict) and a.get("granted_by") in GRANTORS
                       and isinstance(a.get("path"), str) and tasks_mod.overlaps(a["path"], path)]
            for name, pkg in packages.items():
                if not isinstance(pkg, dict):
                    continue
                for pp in pkg.get("paths", []) if isinstance(pkg.get("paths"), list) else []:
                    if isinstance(pp, str) and tasks_mod.overlaps(path, pp) and pkg.get("owner") != lane.get("owner") \
                            and not granted:
                        p.append(f"{label}: {path!r} is inside package '{name}' owned by {pkg.get('owner')}; "
                                 f"needs an authorization granted by lukas or integrator")
            for t in reg_tasks.values():
                if t.get("status") in tasks_mod.ACTIVE_STATUSES:
                    for tp in t.get("owned_paths", []) if isinstance(t.get("owned_paths"), list) else []:
                        if isinstance(tp, str) and tp not in shared and tasks_mod.overlaps(path, tp) and not granted:
                            p.append(f"{label}: {path!r} overlaps active task {t.get('id')} ({tp!r})")
    # lanes must be pairwise disjoint
    lane_list = [l for l in ids.values() if isinstance(l.get("owned_paths"), list)]
    for i, a in enumerate(lane_list):
        for b in lane_list[i + 1:]:
            for pa in a["owned_paths"]:
                for pb in b["owned_paths"]:
                    if isinstance(pa, str) and isinstance(pb, str) and tasks_mod.overlaps(pa, pb):
                        p.append(f"lanes {a['id']} and {b['id']} both own {tasks_mod._overlap_path(pa, pb)!r}")
    for lid, lane in ids.items():
        for dep in lane.get("depends_on", []) if isinstance(lane.get("depends_on"), list) else []:
            if dep not in ids:
                p.append(f"lane {lid}: depends_on unknown lane {dep}")
            elif dep == lid:
                p.append(f"lane {lid}: depends on itself")
    p.extend(_cycles(ids))
    return sorted(set(p))


def _cycles(ids: Dict[str, dict]) -> List[str]:
    out, state = [], {}

    def visit(n: str, trail: List[str]) -> None:
        state[n] = 1
        deps = ids[n].get("depends_on")
        for d in deps if isinstance(deps, list) else []:
            if d not in ids:
                continue
            if state.get(d) == 1:
                out.append("lane dependency cycle: " + " -> ".join(trail + [n, d]))
            elif d not in state:
                visit(d, trail + [n])
        state[n] = 2

    for n in sorted(ids):
        if n not in state:
            visit(n, [])
    return out


# ------------------------------------------------------------------ scope

def lane_scope_problems(m: Dict[str, Any], lane_id: str, changed: Sequence[str]) -> List[str]:
    """Every file a lane changed must be inside its owned paths or its private directory."""
    lane = lanes_by_id(m).get(lane_id)
    if lane is None:
        return [f"lane {lane_id} is not in the manifest"]
    own = list(lane.get("owned_paths", [])) + [lane_dir(m["wave_id"], lane_id)]
    return [f"{f} is outside lane {lane_id}'s owned paths and private directory"
            for f in sorted(set(changed)) if not any(tasks_mod.covers(o, f) for o in own)]


def wave_scope_problems(m: Dict[str, Any], changed: Sequence[str], allow_closeout: bool = False) -> List[str]:
    """Every file changed on the wave branch since its base must belong to some lane, the wave's own directory,
    or (after `close`) the four closeout records."""
    allowed = [wave_dir(m["wave_id"])]
    for lane in m.get("lanes", []):
        allowed += list(lane.get("owned_paths", []))
    if allow_closeout:
        allowed += list(CLOSEOUT_FILES)
    return [f"{f} changed on the wave branch but belongs to no lane" for f in sorted(set(changed))
            if not any(tasks_mod.covers(o, f) for o in allowed)]


def report_problems(report: Any, lane_id: str, need_done: bool = False) -> List[str]:
    if not isinstance(report, dict):
        return [f"lane {lane_id}: report.json must be a JSON object"]
    p = []
    if report.get("lane") != lane_id:
        p.append(f"lane {lane_id}: report.lane must be {lane_id!r}")
    st = report.get("status")
    if st not in REPORT_STATUSES:
        p.append(f"lane {lane_id}: report.status must be one of {list(REPORT_STATUSES)}")
    if not _nonempty(report.get("summary")):
        p.append(f"lane {lane_id}: report.summary is required")
    if not _strs(report.get("unresolved", [])):
        p.append(f"lane {lane_id}: report.unresolved must be a list of strings")
    cmds = report.get("commands", [])
    if not (isinstance(cmds, list) and all(isinstance(c, dict) and _nonempty(c.get("command"))
                                           and _nonempty(c.get("result")) for c in cmds)):
        p.append(f"lane {lane_id}: report.commands must be a list of {{command, result}}")
    if st == "DONE" or need_done:
        if st != "DONE":
            p.append(f"lane {lane_id}: report.status is {st!r}, not DONE")
        if not cmds:
            p.append(f"lane {lane_id}: a DONE report needs at least one command with its result")
        if not _nonempty(report.get("changelog")) or "\n" in str(report.get("changelog")):
            p.append(f"lane {lane_id}: a DONE report needs a one-line 'changelog'")
    return p


def secret_problems(added_lines: Sequence[str]) -> List[str]:
    return [f"possible credential in added line: {l[:60]!r}" for l in added_lines if SECRET.search(l)]


# ------------------------------------------------------------------ brief

def build_brief(m: Dict[str, Any], lane: Dict[str, Any]) -> str:
    wid, lid = m["wave_id"], lane["id"]
    own = "\n".join(f"- `{p}`" for p in lane["owned_paths"])
    crit = "\n".join(f"- {c}" for c in lane["acceptance_criteria"])
    mods = ", ".join(lane["test_modules"])
    deps = ", ".join(lane.get("depends_on") or []) or "none"
    return f"""# Lane {lid} of wave {wid}: {lane['title']}

Owner: `{lane['owner']}`  |  Task id: `{lane['task_id']}`  |  Branch: `{lane['branch']}`  |  Depends on lanes: {deps}

## You may edit only
{own}
- `{lane_dir(wid, lid)}` (your private directory: `report.json`)

Everything else, including every shared registry (`ACTIVE_TASKS.json`, `DEVELOPMENT_TRACKER.json`, `CHANGELOG.md`,
`SESSION_HANDOFF.md`, `PROJECT_STATUS.json`) and `waves/{wid}/wave.json`, is off limits. If you need a change outside your
paths, write it in `report.json` under `unresolved` and stop that part; do not edit it.

## Acceptance criteria
{crit}

## Tests you must run (from `orchestrator/`)
`python -B -I -m unittest discover -s tests -p <module>` for: {mods}

## Working rules
1. Work autonomously in this worktree. Do not ask for approval between steps, and do not open pull requests.
2. Commit explicit paths only (never `git add .`). Never force-push. Never push to `main`.
3. Write `{lane_dir(wid, lid)}report.json` (see `waves/TEMPLATE/lane_report.json`): status, summary, the exact commands you
   ran with their results, anything unresolved, and a one-line changelog entry. Use status `DONE` only when every
   acceptance criterion is met and the tests pass. Never claim VERIFIED without real-input evidence.
4. To deliver: `cd orchestrator && python -B -m governance wave submit` (it merges the wave branch into yours, checks
   scope and tests, and pushes to `{wave_branch(wid)}`). If it reports a conflict, resolve it in your own paths and rerun.
5. Stop after a successful submit and report the result. You never merge pull requests, run `wave close`, or edit the
   manifest.
"""


# ------------------------------------------------------------------ git helpers

class WaveError(Exception):
    pass


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise WaveError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _out(root: Path, *args: str) -> str:
    return git(root, *args).stdout.strip()


def load_manifest_at(root: Path, ref: str, wave_id: str) -> Dict[str, Any]:
    r = git(root, "show", f"{ref}:{manifest_path(wave_id)}", check=False)
    if r.returncode != 0:
        raise WaveError(f"no manifest {manifest_path(wave_id)} at {ref}")
    try:
        return json.loads(r.stdout)
    except ValueError as exc:
        raise WaveError(f"manifest at {ref} is not valid JSON: {exc}")


def load_registry_at(root: Path, ref: str) -> Dict[str, Any]:
    return json.loads(_out(root, "show", f"{ref}:ACTIVE_TASKS.json"))


def parse_lane_branch(branch: str) -> Tuple[str, str]:
    mt = re.match(r"^lane/(w\d{2,3}-[a-z0-9][a-z0-9-]*)/(L\d{1,2})$", branch)
    if not mt:
        raise WaveError(f"branch {branch!r} is not a lane branch (lane/<wave_id>/<lane_id>)")
    return mt.group(1), mt.group(2)


def changed_files(root: Path, base: str, head: str = "HEAD") -> List[str]:
    names = set(_out(root, "diff", "--name-only", f"{base}...{head}").split("\n"))
    return sorted(n for n in names if n)


def worktree_changes(root: Path) -> List[str]:
    out = _out(root, "status", "--porcelain=v1", "--untracked-files=all")
    return [l for l in out.split("\n") if l.strip()]


def added_lines(root: Path, base: str, head: str = "HEAD") -> List[str]:
    diff = git(root, "diff", "-U0", f"{base}...{head}").stdout
    return [l[1:] for l in diff.split("\n") if l.startswith("+") and not l.startswith("+++")]


def conflict_marker_files(root: Path, files: Sequence[str]) -> List[str]:
    bad = []
    for f in files:
        p = root / f
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"^(<<<<<<< |>>>>>>> )", text, re.M):
                bad.append(f)
    return bad


# ------------------------------------------------------------------ submit (the merge train)

def default_test_runner(root: Path, modules: Sequence[str]) -> Tuple[bool, str]:
    orch = root / "orchestrator"
    for mod in modules:
        r = subprocess.run([sys.executable, "-B", "-I", "-m", "unittest", "discover", "-s", "tests", "-p", mod],
                           cwd=str(orch), capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            return False, f"{mod} failed:\n{(r.stdout + r.stderr)[-1500:]}"
    return True, ""


def submit(root: Path, remote: str = "origin", max_attempts: int = 5,
           test_runner: Optional[Callable[[Path, Sequence[str]], Tuple[bool, str]]] = None,
           log: Callable[[str], None] = print) -> int:
    """Deliver the current lane into its wave branch. Returns 0 on success, 1 if the lane must act, 2 on error."""
    runner = test_runner or default_test_runner
    try:
        branch = _out(root, "rev-parse", "--abbrev-ref", "HEAD")
        wid, lid = parse_lane_branch(branch)
        dirty = worktree_changes(root)
        if dirty:
            log("NOT SUBMITTED: the working tree has uncommitted changes; commit explicit paths first:")
            for l in dirty[:20]:
                log(f"  {l}")
            return 1
        wbranch = wave_branch(wid)
        for attempt in range(1, max_attempts + 1):
            git(root, "fetch", remote, wbranch)
            tip = f"{remote}/{wbranch}"
            m = load_manifest_at(root, tip, wid)
            mp = validate_manifest(m, load_registry_at(root, tip))
            if mp:
                log("NOT SUBMITTED: the wave manifest is invalid (report to the integrator):")
                for x in mp:
                    log(f"  - {x}")
                return 1
            lane = lanes_by_id(m).get(lid)
            if lane is None:
                log(f"NOT SUBMITTED: lane {lid} is not in the manifest")
                return 1
            merge = git(root, "merge", "--no-edit", tip, check=False)
            if merge.returncode != 0:
                git(root, "merge", "--abort", check=False)
                log(f"NOT SUBMITTED: merging {tip} into {branch} conflicts. Resolve it inside your own paths:\n"
                    f"  git merge {tip}   (fix, git add <explicit paths>, git commit), then rerun submit.")
                log((merge.stdout + merge.stderr).strip()[-800:])
                return 1
            problems = lane_scope_problems(m, lid, changed_files(root, tip))
            rp = lane_dir(wid, lid) + "report.json"
            if not (root / rp).is_file():
                problems.append(f"{rp} is missing; write your lane report")
            else:
                try:
                    problems += report_problems(json.loads((root / rp).read_text(encoding="utf-8")), lid)
                except ValueError as exc:
                    problems.append(f"{rp} is not valid JSON: {exc}")
            problems += conflict_marker_files(root, changed_files(root, tip))
            problems += secret_problems(added_lines(root, tip))
            if problems:
                log("NOT SUBMITTED:")
                for x in problems:
                    log(f"  - {x}")
                return 1
            ok, msg = runner(root, lane["test_modules"])
            if not ok:
                log(f"NOT SUBMITTED: tests failed\n{msg}")
                return 1
            git(root, "push", remote, f"HEAD:refs/heads/{branch}")
            push = git(root, "push", remote, f"HEAD:refs/heads/{wbranch}", check=False)
            if push.returncode == 0:
                log(f"SUBMITTED lane {lid} to {wbranch} at {_out(root, 'rev-parse', 'HEAD')} (attempt {attempt})")
                return 0
            log(f"attempt {attempt}: the wave branch moved; merging again")
        log(f"NOT SUBMITTED: the wave branch kept moving after {max_attempts} attempts; rerun submit")
        return 1
    except WaveError as exc:
        log(f"ERROR: {exc}")
        return 2


# ------------------------------------------------------------------ status / verify

def lane_state(root: Path, remote: str, m: Dict[str, Any], lane: Dict[str, Any], now: _dt.datetime) -> Dict[str, Any]:
    wid = m["wave_id"]
    tip = f"{remote}/{wave_branch(wid)}"
    ref = f"{remote}/{lane['branch']}"
    st: Dict[str, Any] = {"lane": lane["id"], "owner": lane["owner"], "branch": lane["branch"], "started": False,
                          "submitted": False, "stale": False, "last_commit": None}
    if git(root, "rev-parse", "--verify", "--quiet", ref, check=False).returncode != 0:
        return st
    st["started"] = True
    when = _out(root, "log", "-1", "--format=%cI", ref)
    st["last_commit"] = when
    st["submitted"] = git(root, "merge-base", "--is-ancestor", ref, tip, check=False).returncode == 0
    age = now - _dt.datetime.fromisoformat(when)
    st["stale"] = (not st["submitted"]) and age > _dt.timedelta(hours=m.get("inactivity_hours", 48))
    rp = lane_dir(wid, lane["id"]) + "report.json"
    r = git(root, "show", f"{tip}:{rp}", check=False)
    st["report"] = None
    if r.returncode == 0:
        try:
            st["report"] = json.loads(r.stdout).get("status")
        except ValueError:
            st["report"] = "INVALID"
    return st


def status(root: Path, wave_id: Optional[str], remote: str = "origin",
           now: Optional[_dt.datetime] = None) -> List[Dict[str, Any]]:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    refs = _out(root, "for-each-ref", "--format=%(refname:short)", f"refs/remotes/{remote}/wave/").split("\n")
    out = []
    for ref in [r for r in refs if r]:
        wid = ref.split("/wave/", 1)[1]
        if wave_id and wid != wave_id:
            continue
        try:
            m = load_manifest_at(root, ref, wid)
        except WaveError:
            out.append({"wave": wid, "error": "manifest missing or invalid"})
            continue
        out.append({"wave": wid, "title": m.get("title"),
                    "lanes": [lane_state(root, remote, m, l, now) for l in m.get("lanes", [])]})
    return out


def verify(root: Path, wave_id: str, remote: str = "origin", require_complete: bool = False,
           allow_closeout: bool = False, head: str = "HEAD") -> List[str]:
    """Whole-wave gate, run in CI on the wave branch and by the integrator before `close` and before merging."""
    m = load_manifest_at(root, head, wave_id)
    registry = json.loads(git(root, "show", f"{m.get('base_commit', 'HEAD')}:ACTIVE_TASKS.json").stdout)
    problems = validate_manifest(m, registry)
    base = m.get("base_commit", "")
    changed = changed_files(root, base, head)
    problems += wave_scope_problems(m, changed, allow_closeout=allow_closeout)
    problems += conflict_marker_files(root, changed)
    problems += secret_problems(added_lines(root, base, head))
    for lane in m.get("lanes", []):
        lid = lane["id"]
        rp = lane_dir(wave_id, lid) + "report.json"
        r = git(root, "show", f"{head}:{rp}", check=False)
        if r.returncode != 0:
            if require_complete:
                problems.append(f"lane {lid}: no report.json on the wave branch (lane not submitted)")
            continue
        try:
            problems += report_problems(json.loads(r.stdout), lid, need_done=require_complete)
        except ValueError as exc:
            problems.append(f"lane {lid}: report.json is not valid JSON: {exc}")
        if require_complete:
            ref = f"{remote}/{lane['branch']}"
            if git(root, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0 and \
                    git(root, "merge-base", "--is-ancestor", ref, head, check=False).returncode != 0:
                problems.append(f"lane {lid}: its branch has commits that are not on the wave branch")
    return sorted(set(problems))


# ------------------------------------------------------------------ init / worktrees

def worktree_parent(root: Path) -> Path:
    return root.parent / "CreatorStudio-worktrees"


def init_wave(root: Path, manifest_file: Path, remote: str = "origin", push: bool = True,
              log: Callable[[str], None] = print) -> int:
    """Create `wave/<id>` from origin/main with the manifest and one BRIEF.md per lane. The integrator runs this."""
    try:
        m = json.loads(manifest_file.read_text(encoding="utf-8"))
        git(root, "fetch", remote, "main")
        base = _out(root, "rev-parse", f"{remote}/main")
        if m.get("base_commit") in (None, "", "AUTO"):
            m["base_commit"] = base
        elif m["base_commit"] != base:
            log(f"NOT CREATED: base_commit {m['base_commit']} is not the current {remote}/main {base}")
            return 1
        problems = validate_manifest(m, load_registry_at(root, f"{remote}/main"))
        if problems:
            log("NOT CREATED: the manifest is invalid:")
            for x in problems:
                log(f"  - {x}")
            return 1
        wid = m["wave_id"]
        if git(root, "ls-remote", "--heads", remote, wave_branch(wid)).stdout.strip():
            log(f"NOT CREATED: {wave_branch(wid)} already exists on {remote}")
            return 1
        wt = worktree_parent(root) / f"{wid}-coordinator"
        git(root, "worktree", "add", "-b", wave_branch(wid), str(wt), f"{remote}/main")
        git(wt, "branch", "--unset-upstream", check=False)
        (wt / "waves" / wid).mkdir(parents=True, exist_ok=True)
        (wt / manifest_path(wid)).write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                                             newline="\n")
        paths = [manifest_path(wid)]
        for lane in m["lanes"]:
            d = wt / lane_dir(wid, lane["id"])
            d.mkdir(parents=True, exist_ok=True)
            (d / "BRIEF.md").write_text(build_brief(m, lane), encoding="utf-8", newline="\n")
            paths.append(lane_dir(wid, lane["id"]) + "BRIEF.md")
        git(wt, "add", "--", *paths)
        git(wt, "commit", "-m", f"Start wave {wid}: {m['title']}\n\nManifest and lane briefs only.")
        if push:
            git(wt, "push", "-u", remote, f"{wave_branch(wid)}")
        log(f"CREATED {wave_branch(wid)} at {_out(wt, 'rev-parse', 'HEAD')} ({len(m['lanes'])} lanes); "
            f"coordinator worktree: {wt}")
        return 0
    except (WaveError, OSError, ValueError, KeyError) as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        return 2


def make_lane_worktrees(root: Path, wave_id: str, remote: str = "origin",
                        log: Callable[[str], None] = print) -> int:
    try:
        git(root, "fetch", remote, wave_branch(wave_id))
        tip = f"{remote}/{wave_branch(wave_id)}"
        m = load_manifest_at(root, tip, wave_id)
        for lane in m["lanes"]:
            wt = worktree_parent(root) / f"{wave_id}-{lane['id']}"
            if wt.exists():
                log(f"skip {lane['id']}: {wt} already exists")
                continue
            git(root, "worktree", "add", "-b", lane["branch"], str(wt), tip)
            git(wt, "branch", "--unset-upstream", check=False)
            log(f"lane {lane['id']} ({lane['owner']}): {wt}\n    brief: {wt / lane_dir(wave_id, lane['id']) / 'BRIEF.md'}")
        return 0
    except (WaveError, OSError, KeyError) as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        return 2


# ------------------------------------------------------------------ close (integrator only)

def closeout_records(m: Dict[str, Any], registry: Dict[str, Any], tracker: Dict[str, Any],
                     reports: Dict[str, Dict[str, Any]], heads: Dict[str, str], *, date: str, source: str,
                     wave_head: str, approver: str, lukas_standing: bool) -> Tuple[dict, dict, str]:
    """Return updated registry, tracker and the changelog entry. Pure: no I/O. Every lane becomes one COMPLETED
    task with one COMPLETED step, exactly as if it had gone through claim, step and closeout pull requests."""
    wid = m["wave_id"]
    registry = json.loads(json.dumps(registry))
    tracker = json.loads(json.dumps(tracker))
    lines = []
    for lane in m["lanes"]:
        lid, tid = lane["id"], lane["task_id"]
        rep = reports[lid]
        head = heads[lid]
        reqs = [f"cd orchestrator && python -B -I -m unittest discover -s tests -p {x}" for x in lane["test_modules"]]
        reqs.append("cd orchestrator && python -B -I -m unittest discover -s tests")
        registry["tasks"].append({
            "id": tid, "title": lane["title"], "description": f"Wave {wid}, lane {lid}: {lane['title']}.",
            "owner": lane["owner"], "branch": lane["branch"], "worktree": None, "status": "COMPLETED",
            "depends_on": [], "lease": None, "exclusive_resources": [], "integration_agreements": [],
            "owned_paths": [], "acceptance_criteria": list(lane["acceptance_criteria"]), "test_requirements": reqs,
            "integration_notes": f"Completed {date} as lane {lid} of wave {wid}; delivered through the wave branch "
                                 f"and merged to main in one wave pull request (reviewed wave head {wave_head}, lane "
                                 f"head {head}). No claim, step-start, lease or closeout pull request was used."})
        sid = "S-" + tid[2:] + "-00"
        evidence = [{"type": "commit", "summary": f"Lane {lid} head on the wave branch (last commit to its report)",
                     "commit": head},
                    {"type": "commit", "summary": f"Reviewed wave head of {wid}", "commit": wave_head}]
        for c in rep.get("commands", []):
            evidence.append({"type": "test_run", "summary": f"Run by the lane agent and recorded in its report",
                             "command": c["command"], "result": c["result"]})
        evidence.append({"type": "observation", "summary": rep["summary"]})
        tracker["tasks"].append({"task_id": tid, "title": lane["title"], "steps": [{
            "id": sid, "title": f"Lane {lid} of wave {wid}", "status": "COMPLETED",
            "acceptance_criteria": list(lane["acceptance_criteria"]), "evidence": evidence,
            "completion": {"reviewed_by_chatgpt": approver == "chatgpt", "approved_by": approver,
                           "lukas_confirmed": bool(lukas_standing), "date": date, "source": source}}]})
        lines.append(f"- `{tid}` lane {lid} ({lane['owner']}): {rep['changelog'].strip()}")
    entry = (f"## {date} — wave `{wid}`: {m['title']} ({len(m['lanes'])} lanes, one pull request)\n\n"
             + "\n".join(lines) + f"\n- Reviewed wave head `{wave_head}`. Records generated by `governance wave close`; "
             "approval recorded as REPORTED (it does not claim Lukas reviewed the code).\n\n")
    return registry, tracker, entry


def close(root: Path, wave_id: str, *, source: str, date: str, approver: str, lukas_standing: bool,
          remote: str = "origin", log: Callable[[str], None] = print) -> int:
    """Integrator only. Run on the wave branch, clean, all lanes submitted. Commits the closeout; never pushes."""
    from importlib import import_module
    try:
        branch = _out(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != wave_branch(wave_id):
            log(f"NOT CLOSED: check out {wave_branch(wave_id)} first (on {branch})")
            return 1
        if worktree_changes(root):
            log("NOT CLOSED: the working tree is not clean")
            return 1
        if approver not in APPROVERS:
            log(f"NOT CLOSED: --approver must be one of {list(APPROVERS)}")
            return 1
        if not lukas_standing:
            log("NOT CLOSED: pass --standing-instruction only if Lukas's standing delegation or direct word covers "
                "this approval; the records must not claim an approval that was not given")
            return 1
        if not (_nonempty(source) and re.match(r"(DIRECT|REPORTED)\b", source)):
            log("NOT CLOSED: --source must begin with DIRECT or REPORTED and say where the approval came from")
            return 1
        git(root, "fetch", remote)
        problems = verify(root, wave_id, remote, require_complete=True)
        if problems:
            log("NOT CLOSED: the wave is not complete and valid:")
            for x in problems:
                log(f"  - {x}")
            return 1
        m = load_manifest_at(root, "HEAD", wave_id)
        reports, heads = {}, {}
        for lane in m["lanes"]:
            rp = lane_dir(wave_id, lane["id"]) + "report.json"
            reports[lane["id"]] = json.loads((root / rp).read_text(encoding="utf-8"))
            heads[lane["id"]] = _out(root, "log", "-1", "--format=%H", "--", rp)
        wave_head = _out(root, "rev-parse", "HEAD")
        registry = json.loads((root / "ACTIVE_TASKS.json").read_text(encoding="utf-8"))
        tracker = json.loads((root / "DEVELOPMENT_TRACKER.json").read_text(encoding="utf-8"))
        registry, tracker, entry = closeout_records(m, registry, tracker, reports, heads, date=date, source=source,
                                                    wave_head=wave_head, approver=approver,
                                                    lukas_standing=lukas_standing)
        bad = tasks_mod.validate(registry)
        sys.path.insert(0, str(root / "dashboard"))
        try:
            tc = import_module("tracker_check")
            bad += tc.validate(tracker, registry)
        finally:
            sys.path.pop(0)
        if bad:
            log("NOT CLOSED: the generated records are invalid (nothing was written):")
            for x in bad:
                log(f"  - {x}")
            return 1
        (root / "ACTIVE_TASKS.json").write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
                                                encoding="utf-8", newline="\n")
        (root / "DEVELOPMENT_TRACKER.json").write_text(json.dumps(tracker, indent=2, ensure_ascii=False) + "\n",
                                                       encoding="utf-8", newline="\n")
        cl = root / "CHANGELOG.md"
        text = cl.read_text(encoding="utf-8", newline="")
        nl = "\r\n" if "\r\n" in text else "\n"
        first = text.index("\n## ") + 1 if "\n## " in text else len(text)
        cl.write_text(text[:first] + entry.replace("\n", nl) + text[first:], encoding="utf-8", newline="")
        status_doc = json.loads((root / "PROJECT_STATUS.json").read_text(encoding="utf-8"))
        (root / "SESSION_HANDOFF.md").write_text(handoff_mod.render(status_doc, registry), encoding="utf-8",
                                                 newline="\n")
        git(root, "add", "--", *CLOSEOUT_FILES)
        git(root, "commit", "-m", f"Close out wave {wave_id}: record {len(m['lanes'])} lanes as COMPLETED\n\n"
                                  f"Generated by governance wave close. Approval recorded as REPORTED.")
        log(f"CLOSED {wave_id}: closeout committed at {_out(root, 'rev-parse', 'HEAD')} (not pushed). "
            f"Re-run `wave verify --allow-closeout`, push the wave branch, and open the single wave pull request.")
        return 0
    except (WaveError, OSError, ValueError, KeyError) as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        return 2
