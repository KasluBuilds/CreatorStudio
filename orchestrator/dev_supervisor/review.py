"""Independent review packets and review-report validation (T-DEV-022, issue #44 full-autonomy policy).

A reviewer worker (never the PR author) inspects a PR and posts exactly one comment whose FIRST LINE is

    <!-- dev-review v1 pr=<n> diff_sha256=<16 hex> verdict=<APPROVE|CHANGES_REQUESTED> reviewer=<id> -->

The diff hash is the first 16 hex of sha256 over the bytes of
`git diff --binary $(git merge-base origin/<base> origin/<head>) origin/<head>` (after a fetch). A review only counts
while that hash still matches the PR: a new push makes it STALE.

Pure functions (`parse_marker`, `diff_hash`, `review_status`) are meant to be imported by integrate.py. All process
access (git, gh) goes through injected runners: `run(argv, stdin=None) -> bytes`. Standard library only.

CLI (from orchestrator/):
    python -B -m dev_supervisor.review packet <pr> --out <dir>
    python -B -m dev_supervisor.review check <pr> [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = ["Marker", "ReviewError", "parse_marker", "marker_problem", "diff_hash", "review_status", "format_marker",
           "SubprocessRunner", "compute_diff", "fetch_comments", "fetch_pr", "find_registered_task", "build_packet",
           "build_prompt", "check_pr", "author_ids", "main", "TRUSTED_ASSOCIATIONS",
           "CURRENT_APPROVED", "STALE", "CHANGES_REQUESTED", "MISSING", "INVALID"]

CURRENT_APPROVED = "CURRENT_APPROVED"
STALE = "STALE"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
MISSING = "MISSING"
INVALID = "INVALID"

_REVIEWER_RE = r"[A-Za-z0-9._-]{1,64}"
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_MARKER_RE = re.compile(
    r"^<!-- dev-review v1 pr=(?P<pr>[1-9][0-9]{0,8}) diff_sha256=(?P<hash>[0-9a-f]{16}) "
    r"verdict=(?P<verdict>APPROVE|CHANGES_REQUESTED) reviewer=(?P<reviewer>" + _REVIEWER_RE + r") -->$")
_LOOSE_PREFIX = re.compile(r"(?i)^\s*<!--\s*dev-review\b")


class ReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class Marker:
    pr: int
    diff_sha256: str
    verdict: str
    reviewer: str


def format_marker(pr: int, digest: str, verdict: str, reviewer: str) -> str:
    return f"<!-- dev-review v1 pr={int(pr)} diff_sha256={digest} verdict={verdict} reviewer={reviewer} -->"


def _first_line(body: Any) -> str:
    if not isinstance(body, str):
        return ""
    return body.split("\n", 1)[0].rstrip("\r")


def parse_marker(body: Any) -> Optional[Marker]:
    """Strict parse of the first line only. None if absent or malformed (a marker later in the body is ignored)."""
    m = _MARKER_RE.match(_first_line(body))
    if not m:
        return None
    return Marker(int(m["pr"]), m["hash"], m["verdict"], m["reviewer"])


def marker_problem(body: Any) -> str:
    """'' if the comment is not a review attempt or is a valid marker; otherwise why a review attempt is malformed."""
    first = _first_line(body)
    if _MARKER_RE.match(first):
        return ""
    if _LOOSE_PREFIX.match(first):
        return "malformed dev-review marker on first line"
    if isinstance(body, str) and re.search(r"<!--\s*dev-review\b", body):
        return "dev-review marker not on the first line"
    return ""


def diff_hash(diff: bytes) -> str:
    return hashlib.sha256(diff).hexdigest()[:16]


def _norm(login: Any) -> str:
    return str(login or "").strip().lower().lstrip("@")


def _comment_key(c: Mapping[str, Any]) -> Tuple[str, int]:
    cid = c.get("id")
    return (str(c.get("created_at") or ""), cid if isinstance(cid, int) else 0)


def review_status(pr: int, comments: Iterable[Mapping[str, Any]], current_hash: str,
                  author_ids: Iterable[str] = ()) -> Dict[str, Any]:
    """Pure. comments: GitHub issue-comment dicts (id, body, user.login, created_at). author_ids: identities of the PR
    author: a review whose `reviewer=` id is one of them is rejected. The GitHub login of the commenter is deliberately
    NOT compared: all workers post through one shared account, so the independent reviewer id is the identity.
    Per reviewer id the latest valid marker for the current hash counts; any current
    CHANGES_REQUESTED blocks; CURRENT_APPROVED needs at least one current APPROVE. Only comments whose
    author_association is OWNER, MEMBER or COLLABORATOR count."""
    authors = {_norm(a) for a in author_ids if _norm(a)}
    valid: List[Tuple[Mapping[str, Any], Marker]] = []
    rejected: List[Dict[str, Any]] = []
    attempts = 0
    for c in sorted((c for c in comments if isinstance(c, Mapping)), key=_comment_key):
        body = c.get("body")
        mk = parse_marker(body)
        prob = marker_problem(body)
        if mk is None and not prob:
            continue
        attempts += 1
        cid = c.get("id")
        assoc = str(c.get("author_association") or "").upper()
        if assoc not in TRUSTED_ASSOCIATIONS:
            rejected.append({"comment_id": cid, "reason": f"author_association {assoc or 'MISSING'} is not "
                                                          "OWNER, MEMBER or COLLABORATOR"})
        elif mk is None:
            rejected.append({"comment_id": cid, "reason": prob})
        elif mk.pr != int(pr):
            rejected.append({"comment_id": cid, "reason": f"marker is for PR {mk.pr}, not {int(pr)}"})
        elif _norm(mk.reviewer) in authors:
            rejected.append({"comment_id": cid, "reason": "reviewer is the PR author"})
        else:
            valid.append((c, mk))
    res: Dict[str, Any] = {"pr": int(pr), "current_hash": current_hash, "review_attempts": attempts,
                           "valid_reviews": len(valid), "rejected": rejected, "reviewer": None,
                           "review_hash": None, "verdict": None, "comment_id": None, "current_reviewers": {}}
    if not valid:
        res["status"] = INVALID if attempts else MISSING
        return res
    # each reviewer id's latest marker for the CURRENT hash counts (valid is already in time order)
    latest: Dict[str, Tuple[Mapping[str, Any], Marker]] = {}
    for c, mk in valid:
        if mk.diff_sha256 == current_hash:
            latest[_norm(mk.reviewer)] = (c, mk)
    if not latest:
        c, mk = valid[-1]
        res.update(reviewer=mk.reviewer, review_hash=mk.diff_sha256, verdict=mk.verdict, comment_id=c.get("id"),
                   status=STALE)
        return res
    res["current_reviewers"] = {mk.reviewer: mk.verdict for _, mk in latest.values()}
    blockers = [(c, mk) for c, mk in latest.values() if mk.verdict == "CHANGES_REQUESTED"]
    pick = max(blockers or list(latest.values()), key=lambda t: _comment_key(t[0]))
    c, mk = pick
    res.update(reviewer=mk.reviewer, review_hash=mk.diff_sha256, verdict=mk.verdict, comment_id=c.get("id"),
               status=CHANGES_REQUESTED if blockers else CURRENT_APPROVED)
    return res


# --------------------------------------------------------------------------------------------------
# runners and git/gh access
# --------------------------------------------------------------------------------------------------
class SubprocessRunner:
    """run(argv, stdin=None) -> stdout bytes. Argument lists only."""

    def __init__(self, cwd: Optional[str] = None, timeout: float = 120.0):
        self.cwd = cwd
        self.timeout = timeout

    def __call__(self, argv: Sequence[str], stdin: Optional[bytes] = None) -> bytes:
        try:
            p = subprocess.run(list(argv), input=stdin, capture_output=True, cwd=self.cwd, timeout=self.timeout,
                               shell=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ReviewError(f"{argv[0]} failed to run: {type(e).__name__}")
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise ReviewError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} exit {p.returncode}: "
                              f"{err[0][:160] if err else ''}")
        return p.stdout


def _check_ref(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", name) or name.startswith("-") \
            or ".." in name:
        raise ReviewError("unsafe ref name")
    return name


def fetch_pr(gh: Callable, pr: int, base: Optional[str] = None, allow_base: bool = False) -> Dict[str, Any]:
    out = gh(["gh", "pr", "view", str(int(pr)), "--json",
              "number,title,headRefName,baseRefName,headRefOid,author,state,url"])
    try:
        d = json.loads(out.decode("utf-8"))
    except ValueError:
        raise ReviewError("unparseable gh pr view output")
    if not isinstance(d, dict) or not d.get("headRefName") or not d.get("baseRefName"):
        raise ReviewError("gh pr view returned no head/base")
    if base:
        d["baseRefName"] = base
    if d["baseRefName"] != "main" and not allow_base:
        raise ReviewError(f"base ref {d['baseRefName']!r} is not main; pass --allow-base to review against it")
    return d


def _json_values(text: str) -> List[Any]:
    dec = json.JSONDecoder()
    i, n, out = 0, len(text), []
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        v, i = dec.raw_decode(text, i)
        out.append(v)
    return out


def fetch_comments(gh: Callable, pr: int) -> List[Dict[str, Any]]:
    """All issue comments of a PR (paginated; --paginate prints one JSON array per page)."""
    out = gh(["gh", "api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{int(pr)}/comments?per_page=100"])
    try:
        pages = _json_values(out.decode("utf-8"))
    except ValueError:
        raise ReviewError("unparseable gh api output")
    comments: List[Dict[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            comments.extend(c for c in page if isinstance(c, dict))
        elif isinstance(page, dict):
            comments.append(page)
    return comments


def compute_diff(git: Callable, base: str, head: str, fetch: bool = True) -> Tuple[bytes, str, str, List[str]]:
    """Returns (diff bytes, hash, merge-base sha, changed files). Uses origin/<base> and origin/<head>."""
    base, head = _check_ref(base), _check_ref(head)
    if fetch:
        git(["git", "fetch", "--quiet", "origin", base, head])
    rb, rh = f"origin/{base}", f"origin/{head}"
    mb = git(["git", "merge-base", rb, rh]).decode("utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", mb):
        raise ReviewError("merge-base returned no commit id")
    diff = git(["git", "diff", "--binary", mb, rh])
    files = git(["git", "diff", "--name-only", mb, rh]).decode("utf-8", "replace").splitlines()
    return diff, diff_hash(diff), mb, [f for f in files if f]


def author_ids(pr_info: Mapping[str, Any]) -> List[str]:
    a = pr_info.get("author")
    return [a["login"]] if isinstance(a, Mapping) and a.get("login") else []


def check_pr(pr: int, gh: Callable, git: Callable, fetch: bool = True, base: Optional[str] = None,
             allow_base: bool = False) -> Dict[str, Any]:
    info = fetch_pr(gh, pr, base, allow_base)
    _, digest, mb, files = compute_diff(git, info["baseRefName"], info["headRefName"], fetch)
    res = review_status(pr, fetch_comments(gh, pr), digest, author_ids(info))
    res.update(base=info["baseRefName"], head=info["headRefName"], merge_base=mb, files=len(files))
    return res


# --------------------------------------------------------------------------------------------------
# registered task lookup and packet
# --------------------------------------------------------------------------------------------------
def find_registered_task(root: Path, branch: str) -> Optional[Dict[str, Any]]:
    """Task or wave lane whose branch matches: {id,title,branch,owned_paths,acceptance_criteria,source}."""
    def load(p: Path) -> Any:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def shape(t: Mapping[str, Any], source: str) -> Dict[str, Any]:
        return {"id": t.get("task_id") or t.get("id"), "title": t.get("title", ""), "branch": branch,
                "owned_paths": list(t.get("owned_paths") or []),
                "acceptance_criteria": list(t.get("acceptance_criteria") or []), "source": source}

    reg = load(root / "ACTIVE_TASKS.json")
    for t in (reg.get("tasks", []) if isinstance(reg, dict) else []):
        if isinstance(t, dict) and t.get("branch") == branch:
            return shape(t, "ACTIVE_TASKS.json")
    waves = root / "waves"
    if waves.is_dir():
        for wf in sorted(waves.glob("*/wave.json")):
            d = load(wf)
            for lane in (d.get("lanes", []) if isinstance(d, dict) else []):
                if isinstance(lane, dict) and lane.get("branch") == branch:
                    return shape(lane, f"waves/{wf.parent.name}/wave.json")
    return None


def build_prompt(pr: int, info: Mapping[str, Any], digest: str, task: Optional[Mapping[str, Any]]) -> str:
    n = int(pr)
    ex = format_marker(n, digest, "APPROVE", "<your reviewer id>")
    author = (author_ids(info) or ["unknown"])[0]
    tid = (task or {}).get("id") or "NONE FOUND (report this as a BLOCKING finding)"
    return f"""# Independent review of PR #{n}

You are an INDEPENDENT reviewer worker. You did not write this change and must not be the PR author
(`{author}`). Do not edit the branch, merge, close or push. Never print or store secrets.

PR: {info.get('title', '')}
Head: `{info.get('headRefName')}` -> base `{info.get('baseRefName')}`
Diff hash (current): `{digest}` (first 16 hex of sha256 of `git diff --binary <merge-base> <head>`)
Registered task: {tid}

Files in this packet: `diff.patch`, `files.txt`, `task.md` (owned paths and acceptance criteria).

## What to inspect
1. Correctness: does the change do what the acceptance criteria in `task.md` require, including edge cases?
2. Tests: are they meaningful, offline and synthetic? RUN them (from `orchestrator/`:
   `python -B -I -m unittest discover -s tests -p <module>` for the touched modules, then the full suite) and record
   the exact result lines.
3. Regressions: behaviour of existing modules, public names, file formats.
4. Security: secrets, tokens, absolute user paths in tracked files, shell injection, path traversal, network in tests.
5. Architecture and conventions: owned paths respected (any file outside them is BLOCKING), repo rules in CLAUDE.md,
   no claim of VERIFIED without evidence.

## Findings
Classify every finding as BLOCKING (must change before merge) or NON-BLOCKING (suggestion). Verdict is
`CHANGES_REQUESTED` if there is any BLOCKING finding, else `APPROVE`.

## Output: post EXACTLY ONE comment on the PR
The FIRST LINE must be exactly this marker (nothing before it, hash and PR number unchanged):

{ex}

Use `verdict=CHANGES_REQUESTED` when appropriate and replace `<your reviewer id>` with your own id (letters,
digits, `_ . -`, 1 to 64 characters). Then the body:

    ## BLOCKING
    - <finding or "none">
    ## NON-BLOCKING
    - <finding or "none">
    ## Tests run
    - <exact command> -> <exact result line, e.g. "Ran 120 tests ... OK">

If the PR head moves after you start, the hash is stale: re-fetch, recompute and review again.
"""


def build_packet(pr: int, out: Path, gh: Callable, git: Callable, root: Path, fetch: bool = True,
                 base: Optional[str] = None, allow_base: bool = False) -> Dict[str, Any]:
    info = fetch_pr(gh, pr, base, allow_base)
    diff, digest, mb, files = compute_diff(git, info["baseRefName"], info["headRefName"], fetch)
    task = find_registered_task(root, info["headRefName"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "diff.patch").write_bytes(diff)
    (out / "files.txt").write_text("\n".join(files) + ("\n" if files else ""), encoding="utf-8", newline="\n")
    if task:
        lines = [f"# {task['id']}: {task['title']}", "", f"Source: {task['source']}", "", "## Owned paths"]
        lines += [f"- {p}" for p in task["owned_paths"]] or ["- (none declared)"]
        lines += ["", "## Acceptance criteria"] + [f"- {c}" for c in task["acceptance_criteria"]]
    else:
        lines = ["# No registered task or lane found for this branch", "",
                 f"Branch `{info['headRefName']}` is in neither ACTIVE_TASKS.json nor any wave manifest."]
    (out / "task.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    (out / "reviewer-prompt.md").write_text(build_prompt(pr, info, digest, task), encoding="utf-8", newline="\n")
    return {"pr": int(pr), "diff_sha256": digest, "merge_base": mb, "files": len(files),
            "task": task["id"] if task else None, "diff_bytes": len(diff)}


# --------------------------------------------------------------------------------------------------
def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "ACTIVE_TASKS.json").exists():
            return p
    return here.parents[2]


def main(argv: Optional[Sequence[str]] = None, gh: Optional[Callable] = None, git: Optional[Callable] = None,
         root: Optional[Path] = None) -> int:
    ap = argparse.ArgumentParser(prog="dev_supervisor.review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("packet")
    p1.add_argument("pr", type=int)
    p1.add_argument("--out", required=True)
    p1.add_argument("--base", default=None, help="override the PR base ref (only main unless --allow-base)")
    p1.add_argument("--allow-base", action="store_true")
    p2 = sub.add_parser("check")
    p2.add_argument("pr", type=int)
    p2.add_argument("--json", action="store_true")
    p2.add_argument("--base", default=None, help="override the PR base ref (only main unless --allow-base)")
    p2.add_argument("--allow-base", action="store_true")
    a = ap.parse_args(argv)
    root = root or _repo_root()
    gh = gh or SubprocessRunner(str(root))
    git = git or SubprocessRunner(str(root))
    try:
        if a.cmd == "packet":
            print(json.dumps(build_packet(a.pr, Path(a.out), gh, git, root, base=a.base, allow_base=a.allow_base), sort_keys=True))
            return 0
        r = check_pr(a.pr, gh, git, base=a.base, allow_base=a.allow_base)
    except ReviewError as e:
        print(f"review: error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(r, sort_keys=True, default=str))
    else:
        print(f"{r['status']} pr={r['pr']} current={r['current_hash']} review={r['review_hash']} "
              f"verdict={r['verdict']} reviewer={r['reviewer']} valid={r['valid_reviews']}/{r['review_attempts']} "
              f"files={r['files']}")
        for x in r["rejected"]:
            print(f"  rejected comment {x['comment_id']}: {x['reason']}")
    return 0 if r["status"] == CURRENT_APPROVED else 1


if __name__ == "__main__":
    sys.exit(main())
