"""Command line interface:  python -m creator_state <command> ...

Run from the orchestrator/ folder (or set PYTHONPATH to it). The studio root is
found automatically (parent of orchestrator/) or via --root / CREATOR_STUDIO_ROOT.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import handoff, ingest as ingestion, managed, media, proxy, recovery, schema
from . import shots as shotdet
from .project import Project, StateError, slugify


def _root(args) -> Path:
    if args.root:
        return Path(args.root)
    if os.environ.get("CREATOR_STUDIO_ROOT"):
        return Path(os.environ["CREATOR_STUDIO_ROOT"])
    return Path(__file__).resolve().parents[2]


def _project(args) -> Project:
    p = Path(args.project)
    pdir = p if (p / Project.STATE_FILE).exists() or p.is_absolute() else _root(args) / "Projects" / slugify(args.project)
    return Project(pdir)


def _print_report(report: dict, out=sys.stdout):
    s = report.get("summary")
    if s:
        print(f"Project : {s['title']}  ({s['slug']})", file=out)
        print(f"Status  : {s['status']}   revision {s['revision']}   updated {s['updated_at']}", file=out)
        print("Stages  :", file=out)
        icon = {"completed": "[x]", "skipped": "[-]", "in_progress": "[~]", "failed": "[!]", "pending": "[ ]"}
        for sid, st in s["stages"].items():
            print(f"  {icon[st]} {sid:<20} {st}", file=out)
        if s["pending_tasks"]:
            print("Open tasks:", file=out)
            for t in s["pending_tasks"]:
                print(f"  {t['id']}: {t['text']}", file=out)
        if s["unresolved_errors"]:
            print("Unresolved errors:", file=out)
            for e in s["unresolved_errors"]:
                print(f"  {e['id']}: {e['message']}", file=out)
    ck = report.get("latest_checkpoint")
    print(f"Latest checkpoint: {ck['seq']:04d} {ck['reason']} ({ck['created_at']})" if ck
          else "Latest checkpoint: none valid", file=out)
    findings = [f for f in report["findings"] if f["severity"] != "info" or True]
    if findings:
        print("Findings:", file=out)
        for f in findings:
            print(f"  {f['severity'].upper():<8} {f['code']}" + (f" [{f['stage']}]" if f["stage"] else "")
                  + f": {f['message']}", file=out)
    else:
        print("Findings: none", file=out)
    r = report.get("resume")
    if r:
        print(f"Resume  : stage={r['stage']} action={r['action']}" + ("  ** BLOCKED **" if r["blocking"] else ""), file=out)
        print(f"          {r['reason']}", file=out)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="creator_state", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="studio root (default: parent of orchestrator/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def proj(name, help_, **kw):
        sp = sub.add_parser(name, help=help_, **kw)
        sp.add_argument("project", help="project slug/title, or path to a project folder")
        return sp

    c = sub.add_parser("create", help="create a new project")
    c.add_argument("title")
    c.add_argument("--slug")
    c.add_argument("--source", action="append", default=[], help="original media file (by reference; repeatable)")
    c.add_argument("--hash", action="store_true", help="also record sha256 of source files (slow for big video)")

    sub.add_parser("list", help="list projects")
    s = proj("status", "show project status and where to resume")
    s.add_argument("--json", action="store_true")
    s.add_argument("--deep", action="store_true", help="re-hash outputs (slower, stricter)")
    k = proj("check", "audit state vs. disk; exit code 1 if anything critical")
    k.add_argument("--quick", action="store_true", help="size check only, no hashing")
    proj("resume", "print the stage to resume and what to do first")
    proj("handoff", "regenerate HANDOFF.md (deep verification) and print its path")
    proj("checkpoints", "list checkpoints with integrity verdicts")
    r = proj("recover", "restore a damaged state file from a checkpoint (damaged file is kept)")
    r.add_argument("--seq", type=int)
    r.add_argument("--yes", action="store_true", help="required to actually restore")

    x = proj("start-stage", "begin or resume a stage"); x.add_argument("stage", choices=schema.STAGE_IDS)
    x = proj("complete-stage", "verify outputs and mark a stage complete")
    x.add_argument("stage", choices=schema.STAGE_IDS)
    x.add_argument("--output", action="append", default=[], required=True, help="output file/folder (repeatable)")
    x.add_argument("--summary", default="")
    x = proj("fail-stage", "record a stage failure"); x.add_argument("stage", choices=schema.STAGE_IDS)
    x.add_argument("message"); x.add_argument("--no-retry", action="store_true")
    x = proj("skip-stage", "skip a stage with a reason"); x.add_argument("stage", choices=schema.STAGE_IDS)
    x.add_argument("reason")
    x = proj("reopen-stage", "invalidate a stage (and later ones) with a reason")
    x.add_argument("stage", choices=schema.STAGE_IDS); x.add_argument("reason")

    x = proj("begin-op", "journal an operation before doing it")
    x.add_argument("stage", choices=schema.STAGE_IDS); x.add_argument("op_id"); x.add_argument("description")
    x.add_argument("--destructive", action="store_true")
    x = proj("finish-op", "mark an operation completed or failed")
    x.add_argument("stage", choices=schema.STAGE_IDS); x.add_argument("op_id")
    x.add_argument("--failed", action="store_true"); x.add_argument("--result", default="")
    x = proj("resolve-op", "record the verified outcome of an interrupted operation")
    x.add_argument("stage", choices=schema.STAGE_IDS); x.add_argument("op_id")
    x.add_argument("--applied", choices=["yes", "no"], required=True); x.add_argument("--note", required=True)

    x = proj("decide", "record a decision"); x.add_argument("text"); x.add_argument("--why", default="")
    x.add_argument("--category", default="creative"); x.add_argument("--stage"); x.add_argument("--by", default="user")
    x = proj("constraint", "record a constraint"); x.add_argument("text")
    x = proj("task", "add a pending task"); x.add_argument("text"); x.add_argument("--stage")
    x = proj("task-done", "complete a task"); x.add_argument("task_id")
    x = proj("error", "record an error"); x.add_argument("message"); x.add_argument("--stage")
    x = proj("error-resolved", "resolve an error"); x.add_argument("error_id"); x.add_argument("resolution")
    x = proj("note", "add a note"); x.add_argument("text")
    x = proj("add-source", "register original media by reference")
    x.add_argument("path"); x.add_argument("--hash", action="store_true")
    x = proj("switch-provider", "RECORD that you switched AI provider (does not switch anything)")
    x.add_argument("from_provider"); x.add_argument("to_provider"); x.add_argument("--reason", default="")
    x = proj("log", "show recent activity"); x.add_argument("-n", type=int, default=20)

    # ---- media ingestion (Incoming/ convention; originals are referenced in place) ----
    sub.add_parser("incoming-list", help="list project folders under Incoming/ and their ingest status")
    x = sub.add_parser("incoming-inspect", help="dry run: show what would be ingested (changes nothing)")
    x.add_argument("source", help="folder name under Incoming/, or any folder path")
    x.add_argument("--json", action="store_true"); x.add_argument("--ffprobe", help="path to ffprobe")
    x = sub.add_parser("ingest", help="catalogue a folder of originals into a new or existing project")
    x.add_argument("source", help="folder name under Incoming/, or any folder path (may be outside CreatorStudio)")
    x.add_argument("--project", help="project slug (default: derived from the folder name)")
    x.add_argument("--title", help="title if a new project is created")
    x.add_argument("--hash", dest="hash_policy", choices=["auto", "always", "never"], default="auto",
                   help="sha256 of originals: auto = up to 4 GiB (default)")
    x.add_argument("--ffprobe", help="path to ffprobe")
    x.add_argument("--reopen", action="store_true",
                   help="allow adding media to a project whose ingest is already complete")
    x = proj("inventory", "show the media inventory")
    x.add_argument("--type", choices=["video", "audio", "image", "notes", "subtitle"])
    x.add_argument("--json", action="store_true")
    x = proj("verify-sources", "check that catalogued originals still exist and are unchanged (read-only)")
    x.add_argument("--deep", action="store_true", help="also compare sha256 checksums")
    proj("ingest-status", "show ingestion status for a project")
    for name, helptext in (("proxy-plan", "preview proxy generation (changes nothing)"),
                           ("proxy-make", "generate and verify proxies (explicit; needs --yes)")):
        x = proj(name, helptext)
        x.add_argument("--max-height", type=int)
        x.add_argument("--crf", type=int)
        x.add_argument("--preset")
        if name == "proxy-make":
            x.add_argument("--yes", action="store_true", help="required to actually generate files")
            x.add_argument("--id", action="append", default=[], help="only this media id (repeatable)")
            x.add_argument("--ffmpeg")
            x.add_argument("--ffprobe")
    x = proj("managed-copy", "make ONE verified working copy of an original inside the project (explicit)")
    x.add_argument("media_id")
    x.add_argument("--yes", action="store_true", help="required to actually copy")

    # ---- shot detection (explicit; originals are only read; see SHOT_DETECTION.md) ----
    for name, helptext in (("shots-plan", "preview shot detection per video (dry: runs and writes nothing)"),
                           ("shots-detect", "detect shots, transitions and uncertain candidates (explicit; needs --yes)")):
        x = proj(name, helptext)
        x.add_argument("--id", action="append", default=[], help="only this media id (repeatable)")
        x.add_argument("--ffmpeg", help="path to ffmpeg")
        x.add_argument("--ffprobe", help="path to ffprobe")
        x.add_argument("--floor", type=float, help="candidate score floor, 0-100 (default 5; lower = more candidates)")
        x.add_argument("--max-gap", type=int, help="below-floor frames allowed inside one candidate group (default 1)")
        x.add_argument("--threads", type=int, help="FFmpeg decoder threads, 1-8 (default 2)")
        x.add_argument("--evidence", type=int, help="how many events get before/after JPEG frames (default 40, 0 = none)")
        x.add_argument("--timeout", type=int, help="seconds allowed per FFmpeg pass (default 3600)")
        g = x.add_mutually_exclusive_group()
        g.add_argument("--scale-height", type=int, help="analysis height in pixels (default 360)")
        g.add_argument("--full-size", action="store_true", help="analyse at source size (slower; changes scores)")
        if name == "shots-plan":
            x.add_argument("--json", action="store_true")
        else:
            x.add_argument("--yes", action="store_true", help="required to actually run detection")
    for name, helptext in (("shots-status", "list shot-detection runs: complete, incomplete or failed (read-only)"),
                           ("shots-check", "re-verify completed shot-detection runs and their sources; exit 1 if critical")):
        x = proj(name, helptext)
        x.add_argument("--id", help="only this media id")
        x.add_argument("--json", action="store_true")
    return ap


SHOTS_NOTE = ("Confidence values are heuristic, not calibrated probabilities. Shot detection has been verified on "
              "synthetic clips only, not on real footage.")


def _shots_overrides(args) -> dict:
    o = {}
    for flag, key in (("floor", "candidate_floor"), ("max_gap", "group_max_gap"), ("scale_height", "scale_height"),
                      ("threads", "threads"), ("evidence", "max_evidence_events"), ("timeout", "pass_timeout_s")):
        v = getattr(args, flag, None)
        if v is not None:
            o[key] = v
    if getattr(args, "full_size", False):
        o["scale_height"] = None
    return o


def _located_tools(args):
    """Find ffmpeg/ffprobe WITHOUT running them: a dry run must not start any process."""
    ffprobe = args.ffprobe or os.environ.get("CREATOR_FFPROBE") or shutil.which("ffprobe")
    prober = media.Prober(path=ffprobe) if ffprobe else media.Prober(
        reason="ffprobe not found on PATH (install FFmpeg, or pass --ffprobe PATH)")
    return prober, shotdet.find_ffmpeg(args.ffmpeg)


def _print_shots_plan(rows, out):
    for r in rows:
        head = f"  {r['action'].upper():<8} {r['name']}  [{r['id']}]"
        if r["action"] == "blocked":
            print(f"{head}\n           reason: {r['reason']}", file=out)
            continue
        print(f"{head}  run {r['run_id']}  ({r['expected_frames']} frames @ {r['fps']} fps)"
              + (f"\n           {r['reason']}" if r["reason"] else ""), file=out)
        print(f"           output: {r['run_dir']}/", file=out)
    for r in rows:
        for w in r["warnings"]:
            print(f"  WARNING  {r['name']}: {w}", file=out)


def _shots(cmd, args, p, out) -> int:
    if not (p.dir / Project.STATE_FILE).exists():
        raise StateError(f"project '{args.project}' not found (looked in {p.dir}); see `python -m creator_state list`")
    if cmd in ("shots-plan", "shots-detect"):
        ids = list(args.id) or None
        settings = shotdet.load_settings(p, _shots_overrides(args))
        confirmed = cmd == "shots-detect" and args.yes
        if not confirmed:
            prober, ffmpeg = _located_tools(args)
            rows = shotdet.plan(p, settings, prober, ffmpeg, media_ids=ids)
            blocked = any(r["action"] == "blocked" for r in rows)
            if cmd == "shots-plan" and args.json:
                print(json.dumps({"settings": settings, "settings_hash": shotdet.settings_hash(settings), "rows": rows},
                                 indent=2, default=str), file=out)
                return 1 if blocked else 0
            print(f"Shot-detection settings ({shotdet.settings_hash(settings)}): {json.dumps(settings)}", file=out)
            if not rows:
                print("No video files in the inventory; nothing to analyse.", file=sys.stderr)
                return 2
            _print_shots_plan(rows, out)
            print("Nothing was run or written (ffmpeg/ffprobe were located, not started)."
                  + ("" if cmd == "shots-plan" else " Re-run with --yes to detect shots; the originals are only read."), file=out)
            if blocked:
                print("Some videos are BLOCKED; fix the reasons above (nothing will be analysed for them).", file=out)
            return 1 if blocked else 0
        located, located_ffmpeg = _located_tools(args)
        rows = shotdet.plan(p, settings, located, located_ffmpeg, media_ids=ids)   # validates --id before any tool is started
        if rows and all(r["action"] == "blocked" for r in rows):                  # nothing can run: start nothing
            for r in rows:
                print(f"  BLOCKED    {r['name']}: {r['reason']}", file=out)
            print("Nothing was run; every selected video is blocked.", file=out)
            return 1
        prober = media.Prober.detect(explicit=args.ffprobe)
        try:
            results = shotdet.detect(p, settings, prober, shotdet.find_ffmpeg(args.ffmpeg), media_ids=ids,
                                     progress=lambda m: print(m, file=sys.stderr))
        except KeyboardInterrupt:
            print("Interrupted. Nothing was published; any partial work is kept only as diagnostics.", file=sys.stderr)
            return 130
        if not results:
            print("No video files in the inventory; nothing to analyse.", file=sys.stderr)
            return 2
        bad = False
        for r in results:
            status, reason = r["status"], r["reason"]
            if status == "completed":
                problems = shotdet.verify_run_dir(p.dir / r["run_dir"])    # never report success on unverified output
                if problems:
                    status, reason = "failed", "output verification failed: " + "; ".join(problems[:3])
            if status == "completed":
                s = r["summary"]
                print(f"  COMPLETED  {r['name']}  -> {r['run_dir']}/", file=out)
                print(f"             {s['shots']} shot(s), {s['cuts']} hard cut(s), {s['transitions']} transition(s); "
                      f"{s['rejected']} rejected event(s) kept for audit, {s['uncertain']} uncertain candidate(s) need review", file=out)
            elif status == "skipped":
                print(f"  SKIPPED    {r['name']}: {reason}  [{r['run_dir']}/]", file=out)
            else:
                bad = True
                print(f"  {status.upper():<10} {r['name']}: {reason}", file=out)
                if r.get("diagnostics"):
                    print(f"             diagnostics kept at {r['diagnostics']}/ (not a result)", file=out)
        try:
            if p.load_state()["stages"][shotdet.STAGE]["status"] != "in_progress":
                print(f"Note: stage '{shotdet.STAGE}' is not in progress, so these runs were not journaled "
                      f"(`start-stage <slug> {shotdet.STAGE}` first to journal them).", file=out)
        except StateError:
            pass
        print(SHOTS_NOTE, file=out)
        return 1 if bad else 0

    # status / check: read-only
    names = {}
    m = ingestion.load_manifest(p)
    if m:
        names = {i["id"]: i["name"] for i in m["items"]}
    if args.id and args.id not in names and not (p.dir / shotdet.SHOTS_DIR / args.id).is_dir():
        raise StateError(f"media id '{args.id}' is not in this project's inventory and has no shot-detection folder")
    if cmd == "shots-status":
        rows = shotdet.list_runs(p, args.id)
        if args.json:
            print(json.dumps(rows, indent=2, default=str), file=out)
            return 0
        if not rows:
            print("No shot-detection runs yet. Preview with: python -m creator_state shots-plan " + p.dir.name, file=out)
            return 0
        label = {"complete": "COMPLETE", "incomplete": "INCOMPLETE", "failed": "FAILED", "invalid": "INVALID"}
        last = None
        for r in rows:
            if r["media_id"] != last:
                last = r["media_id"]
                print(f"{names.get(last, '(not in inventory)')}  [{last}]", file=out)
            line = f"  {label[r['state']]:<10} {r['run_id']}"
            if r["state"] == "complete":
                s = r.get("summary") or {}
                line += (f"{'  (latest)' if r.get('latest') else ''}  {s.get('shots', '?')} shot(s), {s.get('cuts', '?')} cut(s), "
                         f"{s.get('transitions', '?')} transition(s), {s.get('uncertain', '?')} uncertain")
            else:
                line += f"  {r.get('note', 'no valid run.json')}"
            print(line + f"  [{r['path']}]", file=out)
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        print("Runs: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
              + ". Only COMPLETE runs are results.", file=out)
        return 0
    findings = shotdet.check_runs(p, args.id)           # shots-check
    complete = sum(1 for r in shotdet.list_runs(p, args.id) if r["state"] == "complete")
    critical = [f for f in findings if f["severity"] == "critical"]
    if args.json:
        print(json.dumps({"complete_runs": complete, "findings": findings}, indent=2, default=str), file=out)
        return 1 if critical else 0
    print(f"Checked {complete} complete run(s): structure, file hashes, and each source against the inventory.", file=out)
    for f in findings:
        print(f"  {f['severity'].upper():<8} {f['code']}: {f['message']}  [{f['path']}]", file=out)
    if not findings:
        print("Findings: none", file=out)
    return 1 if critical else 0


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        return _run(args, out)
    except StateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


def _run(args, out) -> int:
    cmd = args.cmd
    if cmd == "create":
        p = Project.create(_root(args) / "Projects", args.title, slug=args.slug,
                           source_media=args.source, hash_sources=args.hash)
        print(f"Created project at {p.dir}\nNext: python -m creator_state status {p.dir.name}", file=out)
        return 0
    if cmd == "list":
        base = _root(args) / "Projects"
        found = sorted(d for d in base.glob("*") if (d / Project.STATE_FILE).exists()) if base.exists() else []
        for d in found:
            try:
                s = Project(d).load_state()
                print(f"{d.name:<30} {s['status']:<12} next/current: {s['current_stage']}", file=out)
            except StateError as e:
                print(f"{d.name:<30} UNREADABLE ({e})", file=out)
        if not found:
            print("No projects yet.", file=out)
        return 0

    if cmd == "incoming-list":
        rows = ingestion.list_incoming(_root(args))
        if not rows:
            print(f"No project folders under {ingestion.incoming_root(_root(args))}. "
                  "Create Incoming\\<project-name>\\ and put footage in it (see Incoming\\README.md).", file=out)
        for r in rows:
            st = r["ingest_status"] or "no project yet"
            types = ", ".join(f"{k} {v}" for k, v in sorted(r["by_type"].items())) or "no supported files"
            print(f"{r['name']:<28} {types}; {ingestion.human_bytes(r['total_bytes'])}; "
                  f"{r['skipped']} unsupported, {r['ignored']} ignored; ingest: {st}", file=out)
        return 0
    if cmd == "incoming-inspect":
        info = ingestion.inspect_source(_root(args), args.source, media.Prober.detect(explicit=args.ffprobe))
        if args.json:
            print(json.dumps(info, indent=2), file=out)
            return 0
        s = info["summary"]
        print(f"Source           : {info['source']}"
              + ("" if info["inside_studio"] else "  (outside CreatorStudio - fine, referenced in place)"), file=out)
        print(f"Would use project: {info['default_project_slug']} ({'exists' if info['project_exists'] else 'new'})", file=out)
        print(f"Supported files  : {s['files']} ({ingestion.human_bytes(s['total_bytes'])}); "
              + (", ".join(f"{k} {v}" for k, v in sorted(s['by_type'].items())) or "none"), file=out)
        print("By category      : " + (", ".join(f"{k} {v}" for k, v in sorted(s['by_category'].items())) or "none"), file=out)
        ff = info["ffprobe"]
        print(f"ffprobe          : {ff['version'] or 'NOT AVAILABLE - ' + str(ff['reason'])}", file=out)
        for label, lst in (("Unsupported (listed in report, never touched)", info["skipped"]),
                           ("Ignored", info["ignored"]), ("Scan error", info["errors"])):
            for x in lst[:20]:
                print(f"  {label}: {x.get('rel') or x['path']} - {x.get('reason') or x.get('error')}", file=out)
            if len(lst) > 20:
                print(f"  ...and {len(lst) - 20} more", file=out)
        if s["zero_byte"]:
            print(f"  WARNING zero-byte files: {', '.join(s['zero_byte'][:10])}", file=out)
        if s["duplicate_names"]:
            print(f"  NOTE same filename in several folders: {', '.join(s['duplicate_names'][:10])}", file=out)
        print("Nothing was changed. To ingest: python -m creator_state ingest " + args.source, file=out)
        return 0
    if cmd == "ingest":
        prober = media.Prober.detect(explicit=args.ffprobe)
        try:
            res = ingestion.ingest(_root(args), args.source, project_slug=args.project, title=args.title,
                                   hash_policy=args.hash_policy, prober=prober, reopen=args.reopen,
                                   progress=lambda m: print(m, file=sys.stderr))
        except ingestion.IngestError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        s = res["summary"]
        print(f"{'Created' if res['created_project'] else 'Used existing'} project '{res['project']}'"
              + (" (resumed an interrupted ingest)" if res["resumed"] else ""), file=out)
        print(f"Catalogued {res['items']} files ({ingestion.human_bytes(s['total_bytes'])}); "
              f"{res['cache_hits']} reused from earlier probe cache", file=out)
        ff = res["ffprobe"]
        print("ffprobe: " + (ff["version"] or f"NOT AVAILABLE ({ff['reason']}) - technical metadata not recorded"), file=out)
        if res["warnings"]:
            print("Warnings: " + ", ".join(f"{k} x{v}" for k, v in sorted(res["warnings"].items())), file=out)
        print(f"Report   : {res['project_dir']}\\analysis\\ingest_report.md", file=out)
        print(f"Next     : python -m creator_state inventory {res['project']}", file=out)
        return 0

    p = _project(args)
    if cmd == "inventory":
        m = ingestion.load_manifest(p)
        if m is None:
            print("No inventory yet. Run: python -m creator_state ingest <folder>", file=sys.stderr)
            return 2
        items = [i for i in m["items"] if not args.type or i["media_type"] == args.type]
        if args.json:
            print(json.dumps(items, indent=2), file=out)
            return 0
        print(f"{'ID':<13}{'FILE':<34}{'TYPE':<9}{'DUR':>8}  {'VIDEO':<16}{'FPS':>8}  {'AUDIO':<16}{'SIZE':>10}  STATUS", file=out)
        for i in items:
            v = (i.get("video") or [None])[0] or i.get("image")
            res = f"{v['display_width']}x{v['display_height']}" if v else "?"
            a = i.get("audio") or []
            at = f"{a[0]['codec']} {a[0]['channels']}ch" if a else (
                "none" if i["probe"]["status"] == "ok" and i["media_type"] == "video" else "-")
            print(f"{i['id']:<13}{i['relative_to_source'][-32:]:<34}{i['media_type']:<9}"
                  f"{ingestion.human_duration((i.get('container') or {}).get('duration_seconds')):>8}  {res:<16}"
                  f"{ingestion.fps_text(i):>8}  {at:<16}{ingestion.human_bytes((i.get('file') or {}).get('size')):>10}  "
                  f"{i['status']}" + (" !" if i["warnings"] else ""), file=out)
        print(f"{len(items)} item(s). '!' = has warnings (see analysis\\ingest_report.md). "
              "fps: ~ estimated, * variable suspected.", file=out)
        return 0
    if cmd == "verify-sources":
        res = ingestion.verify_sources(p, deep=args.deep)
        print(f"Checked {res['checked']} originals ({'deep: checksums compared' if args.deep else 'quick: size/time'}): "
              + ", ".join(f"{k} {v}" for k, v in sorted(res["counts"].items())), file=out)
        for r in res["rows"]:
            if r["status"] != "ok" or r["detail"]:
                print(f"  {r['status'].upper():<22} {r['name']}: {r['detail']}  [{r['path']}]", file=out)
        return 1 if res["problems"] else 0
    if cmd == "ingest-status":
        st = ingestion.ingest_status(p)
        print(f"Ingest stage : {st['stage_status']} (attempts {st['attempts']}); completed {st['completed_at']}", file=out)
        if st["open_operations"]:
            print(f"Open ops     : {', '.join(st['open_operations'])}  (interrupted? re-run `ingest` to resume)", file=out)
        if not st["has_manifest"]:
            print("Inventory    : none yet", file=out)
            return 0
        s = st["summary"]
        print(f"Inventory    : {s['items']} files, {ingestion.human_bytes(s['total_bytes'])}, "
              + ", ".join(f"{k} {v}" for k, v in sorted(s["by_type"].items()))
              + f"  (run {st['ingest_runs']}, {st['generated_at']})", file=out)
        print("Probe status : " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_probe_status"].items())), file=out)
        print("ffprobe      : " + (st["ffprobe"]["version"] or f"NOT AVAILABLE ({st['ffprobe']['reason']})"), file=out)
        print("Warnings     : " + (", ".join(f"{k} x{v}" for k, v in sorted(s["warning_counts"].items())) or "none"), file=out)
        print("Originals now: " + ", ".join(f"{k} {v}" for k, v in sorted(st["source_check"].items())), file=out)
        return 0
    if cmd in ("proxy-plan", "proxy-make"):
        try:
            settings = proxy.load_settings(p, {"max_height": args.max_height, "crf": args.crf, "preset": args.preset})
            rows = proxy.plan(p, settings)
            print(f"Proxy settings ({proxy.settings_hash(settings)}): {json.dumps(settings)}", file=out)
            if cmd == "proxy-plan" or not args.yes:
                for r in rows:
                    print(f"  {r['action'].upper():<8} {r['name']} -> {r['proxy']}"
                          + (f"  ({r['reason']})" if r["reason"] else ""), file=out)
                print("Nothing was generated." + ("" if cmd == "proxy-plan" else " Re-run with --yes to generate."), file=out)
                return 0
            res = proxy.make(p, settings, media.Prober.detect(explicit=args.ffprobe), proxy.find_ffmpeg(args.ffmpeg),
                             only_ids=set(args.id) or None, progress=lambda m: print(m, file=sys.stderr))
        except proxy.ProxyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        for r in res:
            print(f"  {r['status'].upper():<8} {r['name']} -> {r['proxy']}"
                  + (f"  ({r['reason']})" if r["reason"] else ""), file=out)
        return 1 if any(r["status"] == "failed" for r in res) else 0
    if cmd.startswith("shots-"):
        return _shots(cmd, args, p, out)
    if cmd == "managed-copy":
        if not args.yes:
            print(f"Would copy original {args.media_id} into assets/managed/ and verify it by checksum. "
                  "The original is only read. Re-run with --yes.", file=out)
            return 0
        rec = managed.managed_copy(p, args.media_id)
        print(f"Verified copy: {rec['copy_path']} ({ingestion.human_bytes(rec['size'])}); original untouched.", file=out)
        return 0
    if cmd == "status":
        rep = recovery.analyze(p, deep=args.deep)
        if args.json:
            print(json.dumps(rep, indent=2), file=out)
        else:
            _print_report(rep, out)
        return 0
    if cmd == "check":
        rep = recovery.analyze(p, deep=not args.quick)
        _print_report(rep, out)
        return 1 if recovery.has_critical(rep) else 0
    if cmd == "resume":
        rep = recovery.analyze(p, deep=True)
        r = rep["resume"]
        print(f"Resume stage : {r['stage']}\nAction       : {r['action']}\nBlocked      : {r['blocking']}\n"
              f"Reason       : {r['reason']}", file=out)
        crit = [f for f in rep["findings"] if f["severity"] == "critical"]
        for f in crit:
            print(f"  CRITICAL {f['code']}: {f['message']}", file=out)
        return 1 if r["blocking"] else 0
    if cmd == "handoff":
        path = handoff.write(p, deep=True)
        p.log.event("handoff.generated", f"HANDOFF.md regenerated (deep verification) at {path}.")
        print(path, file=out)
        return 0
    if cmd == "checkpoints":
        for c in p.list_checkpoints():
            print(f"{c.get('seq', '?'):>4}  {'ok ' if c['valid'] else 'BAD'}  {c.get('created_at', '')}  "
                  f"rev {c.get('state_revision', '?')}  {c.get('reason', c.get('problem', ''))}", file=out)
        return 0
    if cmd == "recover":
        info, _ = p.load_checkpoint_state(args.seq)
        if not info:
            print("No valid checkpoint to restore from.", file=sys.stderr)
            return 2
        if not args.yes:
            print(f"Would restore checkpoint {info['seq']:04d} ({info['reason']}, {info['created_at']}). "
                  "Re-run with --yes. The current state file is preserved under backups/.", file=out)
            return 0
        info = p.restore_from_checkpoint(args.seq)
        print(f"Restored checkpoint {info['seq']:04d}. Run `check` and re-verify later work.", file=out)
        return 0

    if cmd == "start-stage":
        print(p.start_stage(args.stage), file=out)
    elif cmd == "complete-stage":
        res = p.complete_stage(args.stage, args.output, summary=args.summary)
        print(f"Stage '{args.stage}' completed; {len(res['outputs'])} output(s) verified. Checkpoint saved.", file=out)
    elif cmd == "fail-stage":
        p.fail_stage(args.stage, args.message, retryable=not args.no_retry)
    elif cmd == "skip-stage":
        p.skip_stage(args.stage, args.reason)
    elif cmd == "reopen-stage":
        print("Reopened:", ", ".join(p.reopen_stage(args.stage, args.reason)), file=out)
    elif cmd == "begin-op":
        print(p.begin_operation(args.stage, args.op_id, args.description, args.destructive), file=out)
    elif cmd == "finish-op":
        (p.fail_operation if args.failed else p.complete_operation)(args.stage, args.op_id, args.result)
    elif cmd == "resolve-op":
        p.resolve_operation(args.stage, args.op_id, args.applied == "yes", args.note)
    elif cmd == "decide":
        print(p.add_decision(args.text, args.why, args.category, args.stage, args.by), file=out)
    elif cmd == "constraint":
        print(p.add_constraint(args.text), file=out)
    elif cmd == "task":
        print(p.add_task(args.text, args.stage), file=out)
    elif cmd == "task-done":
        p.complete_task(args.task_id)
    elif cmd == "error":
        print(p.add_error(args.message, args.stage), file=out)
    elif cmd == "error-resolved":
        p.resolve_error(args.error_id, args.resolution)
    elif cmd == "note":
        p.add_note(args.text)
    elif cmd == "add-source":
        rec = p.add_source_media(args.path, hash_file=args.hash)
        print(f"Registered (read-only reference): {rec['path']}", file=out)
    elif cmd == "switch-provider":
        p.record_provider_switch(args.from_provider, args.to_provider, args.reason)
        print("Recorded. HANDOFF.md is up to date; start the new session and have it read HANDOFF.md.", file=out)
    elif cmd == "log":
        print("\n".join(p.log.tail(args.n)), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
