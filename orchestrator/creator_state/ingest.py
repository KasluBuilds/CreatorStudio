"""Hybrid media ingestion: catalogue originals IN PLACE, record everything, change nothing.

Pipeline (stage `ingest` of creator_state):
  scan folder -> per-file facts + ffprobe (cached, resumable) -> merge into
  `source_manifest.json` -> `analysis/ingest_report.md` -> register originals in
  project state -> verified stage completion (checkpoint).

Originals are only ever opened for reading. Nothing is created, renamed, moved or
deleted inside a source folder, and no large file is copied.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import media, util
from .project import Project, StateError, slugify
from .redact import redact_obj

MANIFEST_FILE = "source_manifest.json"
REPORT_FILE = "analysis/ingest_report.md"
PROBE_CACHE_DIR = "analysis/probe_cache"
MANIFEST_VERSION = 1
STAGE = "ingest"

DERIVED_WARNINGS = {"DUPLICATE_CONTENT", "SOURCE_MISSING"}  # recomputed on every merge


class IngestError(StateError):
    pass


# --------------------------------------------------------------------------- locations
def incoming_root(studio_root) -> Path:
    return Path(studio_root) / "Incoming"


def resolve_source(studio_root, arg: str) -> Path:
    """`arg` is either an Incoming/<name> project folder name or any folder path (also outside CreatorStudio)."""
    inc = incoming_root(studio_root) / arg
    if os.sep not in arg and "/" not in arg and inc.is_dir():
        return inc.resolve()
    p = Path(arg)
    if p.is_dir():
        return p.resolve()
    raise IngestError(f"'{arg}' is neither an existing folder nor a folder under {incoming_root(studio_root)}")


# --------------------------------------------------------------------------- scanning
@dataclass
class ScanEntry:
    path: str
    rel: str
    category: str
    media_class: str
    is_symlink: bool = False


@dataclass
class Scan:
    root: str
    files: list = field(default_factory=list)
    skipped: list = field(default_factory=list)   # unsupported formats (listed, never touched)
    ignored: list = field(default_factory=list)   # junk / partial downloads / sidecars
    errors: list = field(default_factory=list)    # unreadable folders, broken links


def scan_folder(source) -> Scan:
    """Walk a folder (read-only, no symlink-following into directories) and sort files into buckets."""
    root = os.path.abspath(str(source))
    scan = Scan(root=root)

    def onerror(e):
        scan.errors.append({"path": str(getattr(e, "filename", root)),
                            "error": f"folder not accessible: {e.strerror or e}"})

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            parts = Path(rel).parts
            reason = media.ignore_reason(name)
            if reason:
                scan.ignored.append({"path": full, "rel": rel, "reason": reason})
                continue
            islink = os.path.islink(full)
            if islink and not os.path.exists(full):
                scan.errors.append({"path": full, "error": "broken symbolic link"})
                continue
            cls = media.classify_extension(os.path.splitext(name)[1])
            if cls is None:
                scan.skipped.append({"path": full, "rel": rel,
                                     "reason": f"unsupported format '{os.path.splitext(name)[1] or '(none)'}'"})
                continue
            scan.files.append(ScanEntry(full, rel, media.category_for(parts), cls, islink))
    return scan


def summarize_scan(scan: Scan) -> dict:
    sizes = Counter()
    by_type, by_cat = Counter(), Counter()
    total = 0
    zero = []
    for e in scan.files:
        by_type[e.media_class] += 1
        by_cat[e.category] += 1
        try:
            sz = os.stat(e.path).st_size
        except OSError:
            continue
        total += sz
        sizes[e.media_class] += sz
        if sz == 0:
            zero.append(e.rel)
    names = Counter(os.path.basename(e.path).lower() for e in scan.files)
    return {"files": len(scan.files), "total_bytes": total, "by_type": dict(by_type),
            "by_category": dict(by_cat), "skipped": len(scan.skipped), "ignored": len(scan.ignored),
            "errors": len(scan.errors), "zero_byte": zero,
            "duplicate_names": sorted(n for n, c in names.items() if c > 1)}


# --------------------------------------------------------------------------- per-file inventory
def _w(code, message):
    return {"code": code, "message": message}


def build_item(entry: ScanEntry, scan_root: str, prober: media.Prober, hash_policy: str,
               cache_dir: Path, prev: dict | None, now: str, counters: dict) -> dict:
    """Catalogue one file. Never raises for a bad file: problems become status + warnings."""
    iid = media.item_id(entry.path)
    item = {"id": iid, "name": os.path.basename(entry.path), "path": os.path.abspath(entry.path),
            "relative_to_source": entry.rel.replace("\\", "/"), "source_root": scan_root,
            "source_category": entry.category, "extension": os.path.splitext(entry.path)[1].lower(),
            "media_type": entry.media_class, "media_type_source": "extension",
            "status": "ok", "file": None, "probe": {"status": "not_applicable"},
            "container": None, "video": None, "audio": None, "image": None, "warnings": [],
            "first_seen": (prev or {}).get("first_seen", now), "last_checked": now, "duplicate_of": None}
    if entry.is_symlink:
        item["real_path"] = os.path.realpath(entry.path)
        item["warnings"].append(_w("SYMLINK", f"file is a symbolic link to {item['real_path']}"))
    try:
        st = os.stat(entry.path)
        media.open_probe_read(entry.path)
    except FileNotFoundError:
        item["status"] = "missing"
        item["warnings"].append(_w("SOURCE_MISSING", "file disappeared while scanning"))
        return item
    except OSError as e:
        item["status"] = "inaccessible"
        item["warnings"].append(_w("FILE_INACCESSIBLE", f"cannot read file: {e.strerror or e}"))
        return item

    cache_file = cache_dir / f"{iid}.json"
    cached = _read_cache(cache_file, st, prober, hash_policy)
    if cached is not None:
        counters["cache_hits"] += 1
        for k in ("file", "probe", "container", "video", "audio", "image", "media_type",
                  "media_type_source"):
            item[k] = cached[k]
        item["warnings"] += cached["warnings"]
    else:
        counters["computed"] += 1
        item["file"] = media.file_facts(entry.path, st, hash_policy)
        if item["file"]["hash_status"] == "skipped_large":
            item["warnings"].append(_w("HASH_SKIPPED_LARGE",
                                       f"{st.st_size} bytes exceeds the automatic checksum limit; "
                                       "use --hash always to checksum it"))
        elif item["file"]["hash_status"] == "failed":
            item["warnings"].append(_w("HASH_FAILED", "could not read file to compute checksum"))
        if entry.media_class in ("video", "audio", "image"):
            counters["probed"] += 1
            res = prober.probe(entry.path)
            if res["status"] == "ok":
                parsed = media.parse_ffprobe(res["data"], entry.media_class)
                item["probe"] = {"status": "ok", "tool": prober.version}
                for k in ("container", "video", "audio", "image"):
                    item[k] = parsed[k]
                item["media_type"] = parsed["derived_type"]
                item["media_type_source"] = "ffprobe"
                item["warnings"] += [_w(c, m) for c, m in parsed["warnings"]]
            elif res["status"] == "unavailable":
                item["probe"] = {"status": "unavailable", "error": res["error"]}
                item["warnings"].append(_w("FFPROBE_UNAVAILABLE",
                                           "ffprobe not available: technical metadata not recorded"))
            else:
                item["probe"] = {"status": "failed", "error": res["error"], "tool": prober.version}
                item["warnings"].append(_w("PROBE_FAILED", f"ffprobe could not read this file: {res['error']}"))
        _write_cache(cache_file, item)
    if prev and prev.get("file") and (prev["file"].get("size") != item["file"]["size"]
                                      or prev["file"].get("mtime_ns") != item["file"]["mtime_ns"]):
        item["warnings"].append(_w("SOURCE_CHANGED_SINCE_LAST_INGEST",
                                   "size or modified time differs from the previous ingest "
                                   f"(previous sha256: {prev['file'].get('sha256')}); "
                                   "originals should not change - investigate"))
    return item


def _read_cache(path: Path, st, prober, hash_policy):
    """Reuse a previous probe/hash if the file is unchanged and the cache is still adequate."""
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
        f = c["file"]
        if f["size"] != st.st_size or f["mtime_ns"] != st.st_mtime_ns:
            return None
        if c["probe"]["status"] == "unavailable" and prober.available:
            return None   # ffprobe has been installed since: upgrade the record
        if c["probe"]["status"] == "failed" and c["probe"].get("tool") != prober.version:
            return None   # different ffprobe build: try again
        if f.get("hash_status") != media.expected_hash_status(st.st_size, hash_policy):
            return None   # hash policy changed (or an earlier hash attempt failed): recompute
        if f["hash_status"] == "computed" and not f.get("sha256"):
            return None
        return c
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(path: Path, item: dict) -> None:
    keys = ("file", "probe", "container", "video", "audio", "image", "media_type", "media_type_source")
    body = {k: item[k] for k in keys}
    body["warnings"] = [w for w in item["warnings"] if w["code"] not in
                        ("SYMLINK", "SOURCE_CHANGED_SINCE_LAST_INGEST", "SOURCE_MISSING")]
    path.parent.mkdir(parents=True, exist_ok=True)
    util.atomic_write_text(path, util.pretty_json(redact_obj(body)))


# --------------------------------------------------------------------------- manifest
def load_manifest(project: Project) -> dict | None:
    p = project.dir / MANIFEST_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        raise IngestError(f"{MANIFEST_FILE} is corrupt (invalid JSON); re-run ingest to rebuild it "
                          "(originals are unaffected)")


def _apply_derived(items: list, now: str) -> None:
    """Duplicate-content flags, recomputed from scratch each time (idempotent)."""
    for it in items:
        it["warnings"] = [w for w in it["warnings"] if w["code"] != "DUPLICATE_CONTENT"]
        it["duplicate_of"] = None
    groups = {}
    for it in sorted(items, key=lambda i: (i["first_seen"], i["path"])):
        sha = (it.get("file") or {}).get("sha256")
        if sha and it["status"] == "ok":
            groups.setdefault(sha, []).append(it)
    for grp in groups.values():
        for dup in grp[1:]:
            dup["duplicate_of"] = grp[0]["id"]
            dup["warnings"].append(_w("DUPLICATE_CONTENT",
                                      f"identical content to {grp[0]['name']} ({grp[0]['path']}); "
                                      "kept in the inventory, decide later which to use"))


def build_summary(items: list, scan_info: dict) -> dict:
    by_type, by_cat, by_status, by_probe, wcodes = Counter(), Counter(), Counter(), Counter(), Counter()
    total_bytes, total_dur = 0, 0.0
    for it in items:
        by_type[it["media_type"]] += 1
        by_cat[it["source_category"]] += 1
        by_status[it["status"]] += 1
        by_probe[it["probe"]["status"]] += 1
        total_bytes += (it.get("file") or {}).get("size", 0)
        if it["media_type"] in ("video", "audio"):
            total_dur += ((it.get("container") or {}).get("duration_seconds") or 0.0)
        for w in it["warnings"]:
            wcodes[w["code"]] += 1
    return {"items": len(items), "by_type": dict(by_type), "by_category": dict(by_cat),
            "by_status": dict(by_status), "by_probe_status": dict(by_probe),
            "total_bytes": total_bytes, "total_media_duration_seconds": round(total_dur, 3),
            "warning_counts": dict(wcodes), "unsupported_skipped": scan_info["skipped"],
            "ignored": scan_info["ignored"], "scan_errors": scan_info["errors"]}


def human_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_duration(sec) -> str:
    if sec is None:
        return "?"
    sec = float(sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fps_text(it) -> str:
    v = (it.get("video") or [None])[0]
    if not v:
        return "?"
    fr = v["frame_rate"]
    if fr["fps"] is None:
        return "?"
    tag = "" if fr["exact"] else "~"
    return f"{tag}{fr['fps']:g}" + ("*" if fr["kind"] == "variable_suspected" else "")


def render_report(manifest: dict) -> str:
    s = manifest["summary"]
    tool = manifest["tools"]["ffprobe"]
    L = [f"# Ingest report — {manifest['project_title']}", "",
         f"_Generated {manifest['generated_at']} (manifest run {manifest['ingest_runs']})._", "",
         "## Summary",
         f"- Catalogued files: **{s['items']}** ({human_bytes(s['total_bytes'])}); "
         f"media duration {human_duration(s['total_media_duration_seconds'])}",
         f"- By type: {', '.join(f'{k} {v}' for k, v in sorted(s['by_type'].items())) or 'none'}",
         f"- By source category: {', '.join(f'{k} {v}' for k, v in sorted(s['by_category'].items())) or 'none'}",
         f"- File status: {', '.join(f'{k} {v}' for k, v in sorted(s['by_status'].items())) or 'none'}",
         f"- Probe status: {', '.join(f'{k} {v}' for k, v in sorted(s['by_probe_status'].items())) or 'none'}",
         f"- ffprobe: {tool['version'] if tool['available'] else 'NOT AVAILABLE — ' + str(tool['reason'])}",
         f"- Unsupported files skipped: {s['unsupported_skipped']}; ignored (junk/sidecars/partial): "
         f"{s['ignored']}; scan errors: {s['scan_errors']}", "",
         "Originals are **referenced in place** at the absolute paths below; none were copied, moved, "
         "renamed or modified.", ""]
    L += ["## Media", "", "| id | file | type | category | duration | size | video | fps | audio | status |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for it in manifest["items"]:
        v = (it.get("video") or [None])[0] or (it.get("image") and {"display_width": it["image"]["display_width"],
                                                                     "display_height": it["image"]["display_height"],
                                                                     "codec": it["image"]["codec"]})
        res = f"{v['display_width']}x{v['display_height']} {v['codec']}" if v else "?"
        a = it.get("audio") or []
        atext = "; ".join(f"{x['codec']} {x['channels']}ch {x['sample_rate']}Hz" for x in a) if a else \
            ("none" if it["probe"]["status"] == "ok" else "?")
        L.append(f"| {it['id']} | {it['relative_to_source']} | {it['media_type']} | {it['source_category']} | "
                 f"{human_duration((it.get('container') or {}).get('duration_seconds'))} | "
                 f"{human_bytes((it.get('file') or {}).get('size'))} | {res} | {fps_text(it)} | {atext} | "
                 f"{it['status']} |")
    L += ["", "`~` = frame rate is an estimate (not exact); `*` = variable frame rate suspected.", ""]
    warn = [(it, w) for it in manifest["items"] for w in it["warnings"]]
    L.append("## Warnings")
    L += [f"- `{w['code']}` **{it['name']}** — {w['message']}" for it, w in warn] or ["None."]
    L += ["", "## Skipped and ignored (never touched)"]
    for lst, label in ((manifest["skipped"], "Unsupported"), (manifest["ignored"], "Ignored"),
                       (manifest["scan_errors"], "Scan error")):
        L += [f"- {label}: `{x['path']}` — {x.get('reason') or x.get('error')}" for x in lst]
    if not (manifest["skipped"] or manifest["ignored"] or manifest["scan_errors"]):
        L.append("None.")
    L += ["", "## Next", "- `python -m creator_state inventory <project>` to browse, "
          "`verify-sources <project> --deep` to re-check originals, `proxy-plan <project>` to preview proxies.", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- the pipeline
def _ensure_task(proj: Project, text: str, stage=STAGE):
    if not any(t["text"] == text and t["status"] != "done" for t in proj.load_state()["tasks"]):
        proj.add_task(text, stage=stage)


def _ensure_decision(proj: Project, text: str, rationale: str):
    if not any(d["text"] == text for d in proj.load_state()["decisions"]):
        proj.add_decision(text, rationale, category="technical", stage=STAGE, made_by="user")


def ingest(studio_root, source, project_slug=None, title=None, hash_policy="auto",
           prober: media.Prober | None = None, reopen=False, progress=None) -> dict:
    """Ingest a folder into a new or existing project. Safe to re-run and to resume."""
    if hash_policy not in ("auto", "always", "never"):
        raise IngestError("hash policy must be auto, always or never")
    prober = prober or media.Prober.detect()
    progress = progress or (lambda msg: None)
    root = Path(studio_root)
    source = resolve_source(root, str(source))
    scan = scan_folder(source)
    if not scan.files:
        raise IngestError(f"nothing to ingest: no supported media/audio/image/note files found under {source} "
                          f"({len(scan.skipped)} unsupported, {len(scan.ignored)} ignored, "
                          f"{len(scan.errors)} scan errors). No project was created or changed.")

    slug = slugify(project_slug or source.name)
    pdir = root / "Projects" / slug
    rp = pdir.resolve()
    if rp == source or rp in source.parents or source in rp.parents:
        raise IngestError("the source folder and the project folder must not contain one another "
                          "(generated files must stay apart from originals)")
    created = not (pdir / Project.STATE_FILE).exists()
    proj = Project.create(root / "Projects", title or source.name, slug=slug) if created else Project(pdir)
    state = proj.load_state()
    st = state["stages"][STAGE]
    if st["status"] == "completed":
        if not reopen:
            raise IngestError(f"project '{slug}' already finished ingest. To add more media use --reopen "
                              "(this also invalidates any later stages that were completed).")
        proj.reopen_stage(STAGE, f"additional media ingested from {source}")
    elif st["status"] == "skipped":
        raise IngestError("the ingest stage was skipped for this project; refusing to ingest into it")

    _ensure_decision(proj, "Hybrid media ingestion: originals stay in place and are referenced by absolute path; "
                           "no large copies by default; proxies/managed copies are explicit, verified, separate operations.",
                     "approved design for media handling (see INGESTION.md)")
    run = proj.start_stage(STAGE)
    attempt = run["attempts"]
    for op_id, op in list(proj.load_state()["stages"][STAGE]["operations"].items()):
        if op["status"] == "started":   # left open by an interrupted earlier run
            proj.fail_operation(STAGE, op_id, "interrupted before finishing; superseded by a resumed run")
    if run["resumed"]:
        proj.log.event("ingest.resumed", f"Resuming interrupted ingest (attempt {attempt}).", "WARN", stage=STAGE)
    proj.log.event("ingest.started", f"Ingesting {source} into '{slug}' "
                   f"({len(scan.files)} supported files, hash policy {hash_policy}, "
                   f"ffprobe {'available: ' + str(prober.version) if prober.available else 'NOT available'}).",
                   stage=STAGE)
    if not prober.available:
        proj.log.event("ingest.ffprobe_missing", f"{prober.reason}. Continuing with filesystem facts only.",
                       "WARN", stage=STAGE)

    def op(name, desc):
        return f"{name}@{attempt}", desc

    # 1. scan --------------------------------------------------------------
    oid, desc = op("scan", f"scan {source}")
    proj.begin_operation(STAGE, oid, desc)
    sinfo = summarize_scan(scan)
    proj.complete_operation(STAGE, oid, f"{sinfo['files']} files, {len(scan.skipped)} unsupported, "
                                        f"{len(scan.ignored)} ignored, {len(scan.errors)} errors")
    # 2. probe each file (cached) ---------------------------------------------
    prev_manifest = load_manifest(proj)
    prev_items = {i["id"]: i for i in (prev_manifest or {}).get("items", [])}
    cache_dir = proj.dir / PROBE_CACHE_DIR
    counters = Counter()
    oid, desc = op("probe", "read file facts and probe media with ffprobe")
    proj.begin_operation(STAGE, oid, desc)
    now = util.utcnow()
    new_items = []
    for n, entry in enumerate(scan.files, 1):
        progress(f"[{n}/{len(scan.files)}] {entry.rel}")
        new_items.append(build_item(entry, scan.root, prober, hash_policy, cache_dir,
                                    prev_items.get(media.item_id(entry.path)), now, counters))
    proj.complete_operation(STAGE, oid, f"{counters['computed']} computed, {counters['cache_hits']} reused from cache")

    # 3. merge with the previous manifest and write it --------------------------
    seen = {i["id"] for i in new_items}
    carried = []
    for iid, old in prev_items.items():
        if iid in seen:
            continue
        old["warnings"] = [w for w in old["warnings"] if w["code"] not in DERIVED_WARNINGS]
        if not os.path.exists(old["path"]):
            old["status"] = "missing"
            old["warnings"].append(_w("SOURCE_MISSING", "file no longer exists at its recorded path"))
        else:
            old["status"] = "ok" if old["status"] == "missing" else old["status"]
        old["last_checked"] = now
        carried.append(old)
    items = sorted(new_items + carried, key=lambda i: (i["source_root"], i["relative_to_source"].lower()))
    _apply_derived(items, now)

    skipped = scan.skipped + [x for x in (prev_manifest or {}).get("skipped", [])
                              if not x["path"].startswith(scan.root + os.sep)]
    ignored = scan.ignored + [x for x in (prev_manifest or {}).get("ignored", [])
                              if not x["path"].startswith(scan.root + os.sep)]
    errors = scan.errors + [x for x in (prev_manifest or {}).get("scan_errors", [])
                            if not x["path"].startswith(scan.root + os.sep)]
    sources = sorted(set((prev_manifest or {}).get("source_roots", [])) | {scan.root})
    manifest = {
        "manifest_version": MANIFEST_VERSION, "project_id": state["project_id"],
        "project_title": state["title"], "generated_at": now,
        "ingest_runs": (prev_manifest or {}).get("ingest_runs", 0) + 1,
        "tools": {"ffprobe": prober.describe()}, "settings": {"hash_policy": hash_policy},
        "source_roots": sources, "items": items, "skipped": skipped, "ignored": ignored,
        "scan_errors": errors, "summary": build_summary(items, {"skipped": len(skipped),
                                                                   "ignored": len(ignored),
                                                                   "errors": len(errors)}),
    }
    manifest = redact_obj(manifest)
    oid, desc = op("write-manifest", f"write {MANIFEST_FILE} and ingest report")
    proj.begin_operation(STAGE, oid, desc)
    util.atomic_write_text(proj.dir / MANIFEST_FILE, util.pretty_json(manifest))
    (proj.dir / "analysis").mkdir(exist_ok=True)
    util.atomic_write_text(proj.dir / REPORT_FILE, render_report(manifest))
    proj.complete_operation(STAGE, oid, f"{len(items)} inventory items")

    # 4. register originals in project state (by reference) -------------------
    oid, desc = op("register-sources", "register originals in project state")
    proj.begin_operation(STAGE, oid, desc)
    regs = proj.register_sources([{"path": i["path"], "size": i["file"]["size"], "mtime_ns": i["file"]["mtime_ns"],
                                   "sha256": i["file"]["sha256"]} for i in items
                                  if i.get("file") and i["status"] == "ok"])
    proj.complete_operation(STAGE, oid, f"{regs['added']} new, {regs['already_registered']} already registered")

    # 5. warnings, follow-ups, completion -------------------------------------------
    wc = manifest["summary"]["warning_counts"]
    shown = 0
    for it in items:
        for w in it["warnings"]:
            if shown < 50:
                proj.log.event("ingest.warning", f"{it['name']}: {w['code']} - {w['message']}", "WARN", stage=STAGE)
            shown += 1
    if shown > 50:
        proj.log.event("ingest.warning", f"...and {shown - 50} more warnings (see {REPORT_FILE}).", "WARN", stage=STAGE)
    if wc.get("FFPROBE_UNAVAILABLE"):
        _ensure_task(proj, "Install FFmpeg/ffprobe and re-run ingest to fill in technical metadata "
                           "(duration, resolution, frame rate, codecs, audio streams)")
    if wc.get("PROBE_FAILED"):
        _ensure_task(proj, f"Investigate {wc['PROBE_FAILED']} file(s) ffprobe could not read "
                           f"(see {REPORT_FILE}); they may be corrupt")
    if wc.get("FILE_INACCESSIBLE") or manifest["summary"]["scan_errors"]:
        _ensure_task(proj, "Some files/folders were not accessible during ingest; fix permissions or paths and re-run")
    if wc.get("VARIABLE_FRAME_RATE"):
        _ensure_task(proj, "Variable-frame-rate media detected: plan special handling before precise editing")

    proj.complete_stage(STAGE, [MANIFEST_FILE, REPORT_FILE],
                        summary=f"{len(items)} files catalogued from {len(sources)} source folder(s); "
                                f"{sum(wc.values())} warnings; ffprobe "
                                f"{'used' if prober.available else 'unavailable'}",
                        verification_note="manifest and report exist, manifest parses as JSON")
    proj.log.event("ingest.completed", f"Ingest finished: {len(items)} items, {sum(wc.values())} warnings. "
                   f"Report: {REPORT_FILE}", stage=STAGE)
    return {"project": slug, "project_dir": str(proj.dir), "created_project": created,
            "resumed": run["resumed"], "items": len(items), "new_or_updated": len(new_items),
            "cache_hits": counters["cache_hits"], "warnings": wc, "summary": manifest["summary"],
            "ffprobe": prober.describe()}


# --------------------------------------------------------------------------- verification
def verify_sources(project: Project, deep: bool = False) -> dict:
    """Re-check every catalogued original against the manifest. Read-only."""
    m = load_manifest(project)
    if m is None:
        raise IngestError("no source_manifest.json yet; run ingest first")
    rows = []
    for it in m["items"]:
        row = {"id": it["id"], "name": it["name"], "path": it["path"], "status": "ok", "detail": ""}
        try:
            st = os.stat(it["path"])
            media.open_probe_read(it["path"])
        except FileNotFoundError:
            row.update(status="missing", detail="file no longer exists")
            rows.append(row)
            continue
        except OSError as e:
            row.update(status="inaccessible", detail=str(e.strerror or e))
            rows.append(row)
            continue
        f = it.get("file") or {}
        if st.st_size != f.get("size"):
            row.update(status="changed", detail=f"size {f.get('size')} -> {st.st_size}")
        elif deep and f.get("sha256"):
            if util.sha256_file(Path(it["path"])) != f["sha256"]:
                row.update(status="changed", detail="content checksum differs")
        elif st.st_mtime_ns != f.get("mtime_ns"):
            row.update(status="modified_time_changed",
                       detail="modified time differs (size same); run with --deep to compare checksum")
        if deep and not f.get("sha256") and row["status"] == "ok":
            row["detail"] = "no checksum recorded; only size/time compared"
        rows.append(row)
    counts = Counter(r["status"] for r in rows)
    bad = sum(v for k, v in counts.items() if k != "ok")
    project.log.event("sources.verified", f"Checked {len(rows)} originals ({'deep' if deep else 'quick'}): "
                      + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
                      "WARN" if bad else "INFO", stage=STAGE)
    return {"deep": deep, "checked": len(rows), "counts": dict(counts), "problems": bad, "rows": rows}


# --------------------------------------------------------------------------- status & listings
def manifest_summary_lines(project: Project) -> list:
    """Short text for HANDOFF.md; empty if nothing ingested."""
    try:
        m = load_manifest(project)
    except IngestError as e:
        return [f"- {e}"]
    if not m:
        return []
    s = m["summary"]
    tool = m["tools"]["ffprobe"]
    lines = [f"- {s['items']} files catalogued ({human_bytes(s['total_bytes'])}), by type: "
             + ", ".join(f"{k} {v}" for k, v in sorted(s["by_type"].items())),
             f"- Manifest: `{MANIFEST_FILE}` (generated {m['generated_at']}); report: `{REPORT_FILE}`",
             f"- Originals are referenced in place (not copied). Source folders: "
             + "; ".join(f"`{r}`" for r in m["source_roots"]),
             f"- ffprobe: {'used (' + str(tool['version']) + ')' if tool['available'] else 'was NOT available — technical metadata missing'}"]
    if s["warning_counts"]:
        lines.append("- Warnings: " + ", ".join(f"{k} x{v}" for k, v in sorted(s["warning_counts"].items())))
    return lines


def ingest_status(project: Project) -> dict:
    state = project.load_state()
    st = state["stages"][STAGE]
    m = load_manifest(project)
    out = {"stage_status": st["status"], "attempts": st["attempts"], "completed_at": st["completed_at"],
           "has_manifest": m is not None, "summary": m["summary"] if m else None,
           "generated_at": m["generated_at"] if m else None, "ingest_runs": m["ingest_runs"] if m else 0,
           "ffprobe": m["tools"]["ffprobe"] if m else None,
           "open_operations": [k for k, o in st["operations"].items() if o["status"] == "started"],
           "source_check": None}
    if m:
        out["source_check"] = verify_sources(project, deep=False)["counts"]
    return out


def list_incoming(studio_root) -> list:
    inc = incoming_root(studio_root)
    rows = []
    if not inc.is_dir():
        return rows
    for d in sorted(p for p in inc.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        scan = scan_folder(d)
        info = summarize_scan(scan)
        slug = slugify(d.name)
        pstate = None
        sf = Path(studio_root) / "Projects" / slug / Project.STATE_FILE
        if sf.exists():
            try:
                pstate = Project(sf.parent).load_state()["stages"][STAGE]["status"]
            except StateError:
                pstate = "unreadable"
        rows.append({"name": d.name, "path": str(d.resolve()), "project_slug": slug,
                     "project_exists": sf.exists(), "ingest_status": pstate, **{k: info[k] for k in
                     ("files", "total_bytes", "by_type", "skipped", "ignored", "errors")}})
    return rows


def inspect_source(studio_root, arg: str, prober: media.Prober | None = None) -> dict:
    """Dry run: what WOULD be ingested. No hashing, no probing, no project changes."""
    source = resolve_source(studio_root, arg)
    scan = scan_folder(source)
    info = summarize_scan(scan)
    slug = slugify(source.name)
    exists = (Path(studio_root) / "Projects" / slug / Project.STATE_FILE).exists()
    prober = prober or media.Prober.detect()
    return {"source": str(source), "inside_studio": str(source).lower().startswith(str(Path(studio_root).resolve()).lower()),
            "default_project_slug": slug, "project_exists": exists, "summary": info,
            "skipped": scan.skipped, "ignored": scan.ignored, "errors": scan.errors,
            "files": [{"rel": e.rel, "type": e.media_class, "category": e.category} for e in scan.files],
            "ffprobe": prober.describe()}
