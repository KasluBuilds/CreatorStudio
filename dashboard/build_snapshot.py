"""Build a development snapshot from the canonical JSON files (T-GOV-002 step 02).

Standard library only. Read-only with respect to the project: it reads PROJECT_STATUS.json, ACTIVE_TASKS.json and
DEVELOPMENT_TRACKER.json (and runs read-only git queries) and writes exactly one new file, the path given by --out.

Usage from the repository root:
    python -B dashboard/build_snapshot.py --out PATH [--root DIR]

What the snapshot is, and is not:
- It is a point-in-time SNAPSHOT of those files. It is never live GitHub data: it contacts no network and uses no
  credentials. It carries the source commit, branch and generation time so its age is visible.
- Progress figures are copied from PROJECT_STATUS.json ("computed"); they are never recalculated here.
- Text from the sources is scrubbed: private local paths, private project/footage paths, media file names and
  credential-looking strings are replaced by placeholders, and the number of replacements is recorded.

Exit codes: 0 written, 1 sources failed validation (nothing written), 2 unusable input, output path or git (nothing written).
"""
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracker_check  # noqa: E402

SNAPSHOT_SCHEMA_VERSION = 1
SOURCES = ("PROJECT_STATUS.json", "ACTIVE_TASKS.json", "DEVELOPMENT_TRACKER.json")
NOTICE = ("SNAPSHOT: a point-in-time copy of the repository's status files taken at 'generated_at' from the commit "
          "in 'source.commit'. It is not live GitHub data and may be out of date; it was not fetched from GitHub.")

# Scrubbing rules, applied to every string that is copied out of a source file.
_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = (
    ("local_path", re.compile(r"((?<![A-Za-z0-9])[A-Za-z]:[\\/](?!/)[^\s,;)\"'`]*|\\\\[^\s,;)\"'`]+|/Users/[^\s,;)\"'`]*|/home/[^\s,;)\"'`]*)"),
     "[local-path]"),
    ("private_path", re.compile(r"(?<![A-Za-z0-9_.-])(?:\./)?(?:Incoming|Projects|test_media|tools)/[^\s,;)\"'`]*"),
     "[private-path]"),
    ("media_file", re.compile(r"[^\s,;)\"'`/\\]+\.(?:mp4|mov|mkv|avi|webm|m4a|wav|mp3|flac|aac|braw|mxf|r3d|jpg|jpeg|png|heic|raw)\b",
                              re.IGNORECASE), "[media-file]"),
    ("credential", re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
                              r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"), "[credential]"),
)


class SnapshotError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def scrub(value: Any, counts: Dict[str, int]) -> Any:
    """Return a copy of `value` with every string scrubbed; `counts` accumulates replacements by rule."""
    if isinstance(value, str):
        for name, pattern, placeholder in _PATTERNS:
            value, n = pattern.subn(placeholder, value)
            if n:
                counts[name] = counts.get(name, 0) + n
        return value
    if isinstance(value, list):
        return [scrub(v, counts) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v, counts) for k, v in value.items()}
    return value


def _read_json(path: Path) -> Tuple[Any, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SnapshotError(f"cannot read {path.name}: {exc.strerror or exc}")
    try:
        digest = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()  # line endings normalised
        return json.loads(raw.decode("utf-8")), digest
    except (UnicodeDecodeError, ValueError) as exc:
        raise SnapshotError(f"{path.name} is not valid JSON: {exc}")


def _git(root: Path, *args: str) -> Optional[str]:
    """Output of a read-only git query, or None if git or the repository is unavailable."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_metadata(root: Path) -> Dict[str, Any]:
    """Source commit, branch and working-tree cleanliness. Unknown values are null, never guessed."""
    commit = _git(root, "rev-parse", "HEAD")
    if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return {"git_available": False, "commit": None, "branch": None, "working_tree_clean": None,
                "uncommitted_file_count": None,
                "note": "git metadata unavailable: the source commit, branch and cleanliness are unknown"}
    branch = _git(root, "branch", "--show-current") or "(detached HEAD)"
    status = _git(root, "status", "--porcelain")
    dirty = None if status is None else len([ln for ln in status.splitlines() if ln.strip()])
    return {"git_available": True, "commit": commit, "branch": branch,
            "working_tree_clean": None if dirty is None else dirty == 0, "uncommitted_file_count": dirty}


def _check_sources(status: Any, tasks: Any, tracker: Any) -> None:
    if not isinstance(status, dict) or not isinstance(status.get("milestones"), list):
        raise SnapshotError("PROJECT_STATUS.json: 'milestones' list missing", 1)
    computed = status.get("computed")
    if not (isinstance(computed, dict) and all(isinstance(computed.get(k), (int, float)) and not isinstance(computed.get(k), bool)
                                               for k in ("implementation_percent", "verification_percent"))):
        raise SnapshotError("PROJECT_STATUS.json: 'computed' needs numeric implementation_percent and verification_percent "
                            "(they are copied, never recalculated)", 1)
    if not isinstance(tasks, dict) or not isinstance(tasks.get("tasks"), list):
        raise SnapshotError("ACTIVE_TASKS.json: 'tasks' list missing", 1)
    problems = tracker_check.validate(tracker, tasks)
    if problems:
        raise SnapshotError("DEVELOPMENT_TRACKER.json failed validation: " + "; ".join(problems[:5]), 1)


def _milestone(m: Dict[str, Any]) -> Dict[str, Any]:
    impl, ver = m.get("implementation") or {}, m.get("verification") or {}
    return {"id": m.get("id"), "title": m.get("title"), "phase": m.get("phase"), "weight": m.get("weight"),
            "implementation_state": impl.get("state"), "verification_state": ver.get("state"),
            "verification_level": ver.get("level"), "depends_on": m.get("depends_on", []),
            "open_questions": m.get("open_questions", []), "blockers": m.get("blockers", [])}


def _task(t: Dict[str, Any]) -> Dict[str, Any]:
    # worktree and owned_paths are deliberately left out: local layout is not dashboard material.
    return {"id": t.get("id"), "title": t.get("title"), "owner": t.get("owner"), "branch": t.get("branch"),
            "status": t.get("status"), "depends_on": t.get("depends_on", []),
            "lease_expires": (t.get("lease") or {}).get("expires"),
            "acceptance_criteria": t.get("acceptance_criteria", []), "integration_notes": t.get("integration_notes")}


def _gate(g: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": g.get("id"), "title": g.get("title"), "status": g.get("status"),
            "trigger_milestones": g.get("trigger_milestones", []), "message": g.get("message")}


def build(root: Path, now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """Build the snapshot dictionary (nothing is written). `now` is injectable for tests."""
    loaded = {n: _read_json(root / n) for n in SOURCES}
    status, tasks, tracker = (loaded[n][0] for n in SOURCES)
    _check_sources(status, tasks, tracker)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        raise SnapshotError("generation time must include a UTC offset")
    git = git_metadata(root)
    tracker_prs = [{"task_id": t["task_id"], "step_id": s["id"], "summary": e.get("summary"), "ref": e.get("ref"),
                    "merge_or_head_commit": e.get("commit")}
                   for t in tracker["tasks"] for s in t["steps"] for e in s["evidence"] if e["type"] == "pull_request"]
    snap: Dict[str, Any] = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "is_snapshot": True,
        "live_github_data": False,
        "notice": NOTICE,
        "source": {**git,
                   "generated_at": now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "files": {n: {"sha256": loaded[n][1]} for n in SOURCES}},
        "progress": {"source": "PROJECT_STATUS.json computed block, copied unchanged (not recalculated here)",
                     "computed": status["computed"], "weights_status": status.get("weights_status"),
                     "status_updated": status.get("updated"), "baseline": status.get("baseline")},
        "milestones": [_milestone(m) for m in status["milestones"] if isinstance(m, dict)],
        "tasks": [_task(t) for t in tasks["tasks"] if isinstance(t, dict)],
        "development_tracker": {"protocol": tracker.get("protocol"), "step_statuses": tracker.get("step_statuses"),
                                "tasks": tracker["tasks"]},
        "open_decisions": status.get("open_decisions", []),
        "next_actions": status.get("next_actions", []),
        "user_action_gates": [_gate(g) for g in status.get("user_action_gates", []) if isinstance(g, dict)],
        "github": {"live": False, "repository": status.get("repository"), "branch": git["branch"],
                   "commit": git["commit"], "baseline_branch": (status.get("baseline") or {}).get("branch"),
                   "baseline_commit": (status.get("baseline") or {}).get("commit"),
                   "pull_requests_recorded_in_tracker": tracker_prs,
                   "note": "Recorded facts only. Open or merged state of any pull request is not queried; check GitHub."},
    }
    counts: Dict[str, int] = {}
    snap = scrub(snap, counts)
    snap["sanitization"] = {"replacements": dict(sorted(counts.items())), "total": sum(counts.values()),
                            "rules": [n for n, _, _ in _PATTERNS]}
    return snap


def render(snap: Dict[str, Any]) -> str:
    return json.dumps(snap, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_snapshot(root: Path, out: Path, now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    out = out.resolve()
    if out.is_dir():
        raise SnapshotError(f"--out {out.name} is a directory")
    for n in SOURCES:
        if out == (root / n).resolve():
            raise SnapshotError(f"--out must not be a source file ({n})")
    snap = build(root, now)  # cleanliness is measured before this call writes anything
    if not out.parent.is_dir():
        raise SnapshotError("the output directory does not exist")
    fd, tmp = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render(snap))
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return snap


def main(argv: List[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    out = None
    try:
        if "--root" in argv:
            root = Path(argv[argv.index("--root") + 1])
        if "--out" in argv:
            out = Path(argv[argv.index("--out") + 1])
    except IndexError:
        print("ERROR: --root and --out need a value", file=sys.stderr)
        return 2
    if out is None:
        print("ERROR: --out PATH is required (the snapshot is never written to a default place)", file=sys.stderr)
        return 2
    try:
        snap = write_snapshot(root, out)
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    src = snap["source"]
    print(f"OK: snapshot written ({src['generated_at']}, commit {str(src['commit'])[:12]}, "
          f"working tree {'clean' if src['working_tree_clean'] else 'NOT clean' if src['working_tree_clean'] is False else 'unknown'}, "
          f"{snap['sanitization']['total']} scrubbed)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
