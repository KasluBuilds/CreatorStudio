"""Command line: python -B -m governance <validate|score|tasks|leases|handoff|scope|wave> (run from orchestrator/)."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import handoff as handoff_mod
from . import status as status_mod
from . import tasks as tasks_mod
from . import waves as waves_mod

ROOT = Path(__file__).resolve().parents[2]
STATUS_FILE = "PROJECT_STATUS.json"
TASKS_FILE = "ACTIVE_TASKS.json"
HANDOFF_FILE = "SESSION_HANDOFF.md"


def _load(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def _report(problems: List[str], ok_message: str) -> int:
    if problems:
        print("INVALID:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(ok_message)
    return 0


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m governance", description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    sub = p.add_subparsers(dest="cmd", required=True)
    va = sub.add_parser("validate", help="validate PROJECT_STATUS.json and ACTIVE_TASKS.json")
    va.add_argument("--against", metavar="REF", help="also compare PROJECT_STATUS.json with its version at this git ref "
                    "(for example origin/main): append-only evidence, no dropped milestones, no unevidenced upgrades")
    sp = sub.add_parser("scope", help="check that this branch's changed files lie inside its task's owned_paths")
    sp.add_argument("--base", default="origin/main", help="git ref the branch started from (default origin/main)")
    sp.add_argument("--branch", help="branch name (default: the current branch)")
    sc = sub.add_parser("score", help="print implementation and verification percentages")
    sc.add_argument("--json", action="store_true", help="machine-readable output")
    sc.add_argument("--write", action="store_true", help="refresh the 'computed' block in PROJECT_STATUS.json")
    sc.add_argument("--verify-git", action="store_true", help="also check that referenced commits exist")
    sub.add_parser("tasks", help="validate the task registry only")
    lv = sub.add_parser("leases", help="list active tasks whose lease has expired")
    lv.add_argument("--now", help="ISO-8601 time with offset (default: current UTC time)")
    ho = sub.add_parser("handoff", help="render SESSION_HANDOFF.md from the two JSON files")
    g = ho.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    wv = sub.add_parser("wave", help="parallel waves of lanes integrated through one pull request (WAVE_PROTOCOL.md)")
    ws = wv.add_subparsers(dest="wcmd", required=True)
    w = ws.add_parser("init", help="integrator: create wave/<id> with its manifest and lane briefs from origin/main")
    w.add_argument("manifest", type=Path)
    w.add_argument("--no-push", action="store_true")
    w = ws.add_parser("worktrees", help="create one worktree and branch per lane of a wave")
    w.add_argument("wave_id")
    w = ws.add_parser("validate", help="validate a manifest file against ACTIVE_TASKS.json")
    w.add_argument("manifest", type=Path)
    w = ws.add_parser("submit", help="lane: merge the wave branch in, validate, push to the wave branch")
    w.add_argument("--attempts", type=int, default=5)
    w = ws.add_parser("status", help="show every wave and lane (started, submitted, stale)")
    w.add_argument("wave_id", nargs="?")
    w.add_argument("--json", action="store_true")
    w = ws.add_parser("verify", help="whole-wave gate used by CI and before close (run on the wave branch)")
    w.add_argument("wave_id")
    w.add_argument("--require-complete", action="store_true", help="every lane report must be DONE and submitted")
    w.add_argument("--allow-closeout", action="store_true", help="accept the four closeout records")
    w = ws.add_parser("close", help="integrator only: write registry/tracker/changelog/handoff for the wave and commit")
    w.add_argument("wave_id")
    w.add_argument("--source", required=True, help="must begin DIRECT or REPORTED; says where the approval came from")
    w.add_argument("--date", required=True, help="YYYY-MM-DD")
    w.add_argument("--approver", required=True, choices=list(waves_mod.APPROVERS),
                   help="who technically approved the wave (recorded in every lane's completion record)")
    w.add_argument("--standing-instruction", action="store_true", help="Lukas's standing instruction covers this approval")
    return p


def _wave(args, root: Path) -> int:
    c = args.wcmd
    if c == "init":
        return waves_mod.init_wave(root, args.manifest, push=not args.no_push)
    if c == "worktrees":
        return waves_mod.make_lane_worktrees(root, args.wave_id)
    if c == "validate":
        reg = _load(root / TASKS_FILE)
        m = _load(args.manifest)
        return _report(waves_mod.validate_manifest(m, reg, allow_auto_base=True), f"OK: wave manifest {m.get('wave_id')} is valid "
                                                           f"({len(m.get('lanes', []))} lanes)")
    if c == "submit":
        return waves_mod.submit(root, max_attempts=args.attempts)
    if c == "status":
        data = waves_mod.status(root, args.wave_id)
        if args.json:
            print(json.dumps(data, indent=2))
            return 0
        if not data:
            print("no waves found on origin (run git fetch origin first)")
        for wv_ in data:
            print(f"wave {wv_['wave']}: {wv_.get('title', wv_.get('error'))}")
            for l in wv_.get("lanes", []):
                flag = ("SUBMITTED" if l["submitted"] else "STALE" if l["stale"] else
                        "working" if l["started"] else "not started")
                print(f"  {l['lane']:<4} {flag:<12} report={l.get('report') or '-':<12} {l['owner']}  {l['branch']}"
                      f"  last={l['last_commit'] or '-'}")
        return 0
    if c == "verify":
        return _report(waves_mod.verify(root, args.wave_id, require_complete=args.require_complete,
                                        allow_closeout=args.allow_closeout), f"OK: wave {args.wave_id} is valid")
    if c == "close":
        return waves_mod.close(root, args.wave_id, source=args.source, date=args.date,
                               approver=args.approver, lukas_standing=args.standing_instruction)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.root
    sfile, tfile, hfile = root / STATUS_FILE, root / TASKS_FILE, root / HANDOFF_FILE
    try:
        if args.cmd == "wave":
            return _wave(args, root)
        if args.cmd == "validate":
            sdoc, tdoc = _load(sfile), _load(tfile)
            problems = [f"{STATUS_FILE}: {x}" for x in status_mod.validate(sdoc)]
            problems += [f"{TASKS_FILE}: {x}" for x in tasks_mod.validate(tdoc)]
            if not problems:
                problems += [f"{STATUS_FILE}: {x}" for x in status_mod.check_computed(sdoc)]
            if not problems and args.against:
                exists = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{args.against}:{STATUS_FILE}"],
                                        capture_output=True).returncode == 0
                if exists:
                    old = json.loads(_git(root, "show", f"{args.against}:{STATUS_FILE}"))
                    problems += [f"{STATUS_FILE} vs {args.against}: {x}" for x in status_mod.transition_problems(old, sdoc)]
                else:
                    print(f"note: {STATUS_FILE} does not exist at {args.against}; nothing to compare with")
            return _report(problems, "OK: status and task registry are valid and consistent")
        if args.cmd == "scope":
            branch = args.branch or _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
            changed = set(_git(root, "diff", "--name-only", f"{args.base}...HEAD").split())
            changed |= set(_git(root, "diff", "--name-only", "HEAD").split())
            changed |= set(_git(root, "ls-files", "--others", "--exclude-standard").split())
            return _report(tasks_mod.scope_problems(_load(tfile), branch, sorted(changed)),
                           f"OK: all {len(changed)} changed files on {branch} are inside its claimed paths")
        if args.cmd == "tasks":
            return _report(tasks_mod.validate(_load(tfile)), "OK: task registry is valid")
        if args.cmd == "score":
            sdoc = _load(sfile)
            result = status_mod.calculate(sdoc)
            if args.verify_git:
                bad = status_mod.verify_commits(sdoc, root)
                if bad:
                    return _report(bad, "")
            if args.write:
                sdoc["computed"] = {k: result[k] for k in ("implementation_percent", "verification_percent",
                                                          "total_weight", "milestone_count")}
                sfile.write_text(json.dumps(sdoc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Implementation: {result['implementation_percent']}%")
                print(f"Verification:   {result['verification_percent']}%")
                print(f"({result['milestone_count']} milestones, total weight {result['total_weight']}; "
                      f"weights {result['weights_status']}"
                      + (" - proposals awaiting Lukas's review" if result["weights_status"] == "PROPOSED" else "") + ")")
                print(f"Milestones verified on reviewed real input: {len(result['real_input_reviewed_milestones'])} "
                      f"of {result['milestone_count']}")
                if result["reported_only_milestones"]:
                    print("Verification credit resting on reported evidence only: "
                          + ", ".join(result["reported_only_milestones"]))
            return 0
        if args.cmd == "leases":
            now = _dt.datetime.fromisoformat(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
            if now.tzinfo is None:
                print("--now must include a UTC offset", file=sys.stderr)
                return 2
            expired = tasks_mod.expired_leases(_load(tfile), now)
            for e in expired:
                print(f"EXPIRED {e['id']} ({e['owner']}) at {e['expired']}")
            if not expired:
                print("no expired leases")
            return 1 if expired else 0
        if args.cmd == "handoff":
            text = handoff_mod.render(_load(sfile), _load(tfile))
            if args.check:
                current = _norm(hfile.read_text(encoding="utf-8")) if hfile.exists() else None
                if current != _norm(text):
                    print(f"{HANDOFF_FILE} is stale or missing; run: python -B -m governance handoff --write")
                    return 1
                print(f"OK: {HANDOFF_FILE} is up to date")
                return 0
            if args.write:
                hfile.write_text(text, encoding="utf-8", newline="\n")
                print(f"wrote {hfile}")
                return 0
            print(text)
            return 0
    except status_mod.StatusError as exc:
        return _report(exc.problems, "")
    except (OSError, json.JSONDecodeError, KeyError, ValueError, waves_mod.WaveError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 2
