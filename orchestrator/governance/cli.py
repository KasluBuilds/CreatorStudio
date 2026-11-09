"""Command line: python -B -m governance <validate|score|tasks|leases|handoff> (run from orchestrator/)."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import handoff as handoff_mod
from . import status as status_mod
from . import tasks as tasks_mod

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m governance", description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="validate PROJECT_STATUS.json and ACTIVE_TASKS.json")
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
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.root
    sfile, tfile, hfile = root / STATUS_FILE, root / TASKS_FILE, root / HANDOFF_FILE
    try:
        if args.cmd == "validate":
            sdoc, tdoc = _load(sfile), _load(tfile)
            problems = [f"{STATUS_FILE}: {x}" for x in status_mod.validate(sdoc)]
            problems += [f"{TASKS_FILE}: {x}" for x in tasks_mod.validate(tdoc)]
            if not problems:
                problems += [f"{STATUS_FILE}: {x}" for x in status_mod.check_computed(sdoc)]
            return _report(problems, "OK: status and task registry are valid and consistent")
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
                print(f"({result['milestone_count']} milestones, total weight {result['total_weight']})")
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
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 2
