"""Ingestion tests: temp dirs + generated dummy files only. Never touches real footage.

Run from orchestrator/:  python -I -m unittest discover -s tests -v
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creator_state import ingest, managed, media, proxy, recovery, util
from creator_state.cli import main as cli_main
from creator_state.project import Project, StateError

VIDEO_JSON = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High", "width": 1920,
         "height": 1080, "pix_fmt": "yuv420p", "r_frame_rate": "30000/1001", "avg_frame_rate": "30000/1001",
         "nb_frames": "375", "duration": "12.512"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo",
         "sample_rate": "48000"}],
    "format": {"format_name": "mov,mp4", "duration": "12.512", "bit_rate": "8000000",
               "tags": {"creation_time": "2026-10-02T10:00:00.000000Z"}},
}
AUDIO_JSON = {"streams": [{"index": 0, "codec_type": "audio", "codec_name": "pcm_s16le", "channels": 1,
                           "sample_rate": "44100"}], "format": {"format_name": "wav", "duration": "30.0"}}
IMAGE_JSON = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "mjpeg", "width": 4000,
                           "height": 3000, "pix_fmt": "yuvj420p", "r_frame_rate": "25/1",
                           "avg_frame_rate": "25/1"}], "format": {"format_name": "image2"}}
PROXY_JSON = {
    "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 960, "height": 540,
                 "pix_fmt": "yuv420p", "r_frame_rate": "30000/1001", "avg_frame_rate": "30000/1001"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000"}],
    "format": {"format_name": "mov,mp4", "duration": "12.5"}}


def fake_runner(on_call=None, canned=None):
    """Stand-in for ffprobe. on_call(path, n) may raise or return (rc, out, err)."""
    calls = []

    def run(argv, timeout):
        if "-version" in argv:
            return 0, "ffprobe version fake-1.0\n", ""
        path = argv[-1]
        calls.append(path)
        if on_call:
            r = on_call(path, len(calls))
            if r is not None:
                return r
        if ".part" in os.path.basename(path):
            return 0, json.dumps(PROXY_JSON), ""
        data = (canned or {}).get(Path(path).suffix.lower()) or {
            ".mov": VIDEO_JSON, ".mp4": VIDEO_JSON, ".wav": AUDIO_JSON, ".jpg": IMAGE_JSON}.get(
            Path(path).suffix.lower())
        if data is None:
            return 1, "", "Invalid data found when processing input"
        return 0, json.dumps(data), ""
    run.calls = calls
    return run


def fake_prober(runner=None):
    return media.Prober(path="fake-ffprobe", version="ffprobe version fake-1.0", runner=runner or fake_runner())


def no_prober():
    return media.Prober.detect(which=lambda n: None)


def snapshot(folder: Path) -> dict:
    out = {}
    for dirpath, dirs, files in os.walk(folder):
        for d in dirs:
            out[os.path.join(dirpath, d)] = "DIR"
        for f in files:
            p = Path(dirpath) / f
            st = p.stat()
            out[str(p)] = (util.sha256_file(p), st.st_size, st.st_mtime_ns)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "CreatorStudio"
        self.trip = self.root / "Incoming" / "trip"
        self.files = {}
        for rel, content in {
            "camera/a.mov": b"video-a" * 500, "phone/b.mp4": b"video-b" * 700, "audio/c.wav": b"audio-c" * 300,
            "images/d.jpg": b"image-d" * 100, "notes/n.txt": b"my notes\n", "loose.mov": b"video-l" * 50,
        }.items():
            self.put(rel, content)
        self.put("camera/readme.xyz", b"unsupported")
        self.put("camera/.hidden", b"x")
        self.put("camera/Thumbs.db", b"x")
        self.put("camera/clip.mov.part", b"partial")
        self.put("camera/clip.THM", b"sidecar")

    def put(self, rel, content, base=None):
        p = (base or self.trip) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        self.files[rel] = p
        return p

    def do_ingest(self, prober=None, **kw):
        kw.setdefault("project_slug", "trip")
        return ingest.ingest(self.root, "trip", prober=prober or fake_prober(), **kw)

    def project(self, slug="trip"):
        return Project(self.root / "Projects" / slug)

    def manifest(self, slug="trip"):
        return ingest.load_manifest(self.project(slug))

    def item(self, name, slug="trip"):
        return next(i for i in self.manifest(slug)["items"] if i["name"] == name)


class TestClassification(unittest.TestCase):
    def test_types(self):
        c = media.classify_extension
        self.assertEqual([c(".MOV"), c(".mp4"), c(".wav"), c(".JPG"), c(".txt"), c(".srt"), c(".exe"), c("")],
                         ["video", "video", "audio", "image", "notes", "subtitle", None, None])

    def test_ignore_rules(self):
        self.assertTrue(media.ignore_reason("Thumbs.db"))
        self.assertTrue(media.ignore_reason(".hidden"))
        self.assertTrue(media.ignore_reason("~$notes.txt"))
        self.assertTrue(media.ignore_reason("clip.mov.part"))
        self.assertTrue(media.ignore_reason("C0001.THM"))
        self.assertIsNone(media.ignore_reason("normal.mov"))

    def test_category(self):
        self.assertEqual(media.category_for(("camera", "a.mov")), "camera")
        self.assertEqual(media.category_for(("Screen_Recordings", "x.mp4")), "screen")
        self.assertEqual(media.category_for(("weird", "x.mp4")), "unsorted")
        self.assertEqual(media.category_for(("loose.mov",)), "unsorted")


class TestFfprobeParsing(unittest.TestCase):
    def test_constant_fractional_rate_is_exact(self):
        v = media.parse_ffprobe(VIDEO_JSON, "video")["video"][0]
        fr = v["frame_rate"]
        self.assertEqual((fr["kind"], fr["exact"], fr["fps_rational"]), ("constant", True, "30000/1001"))
        self.assertAlmostEqual(fr["fps"], 29.97003, places=4)
        self.assertEqual((v["width"], v["height"], v["codec"], v["pix_fmt"]), (1920, 1080, "h264", "yuv420p"))

    def test_variable_frame_rate_is_flagged_not_exact(self):
        d = json.loads(json.dumps(VIDEO_JSON))
        d["streams"][0].update(r_frame_rate="60/1", avg_frame_rate="2400/101")
        p = media.parse_ffprobe(d, "video")
        fr = p["video"][0]["frame_rate"]
        self.assertEqual((fr["kind"], fr["exact"]), ("variable_suspected", False))
        self.assertIn("VARIABLE_FRAME_RATE", [c for c, _ in p["warnings"]])

    def test_single_or_missing_rate_is_estimate_or_unknown(self):
        self.assertEqual(media.fps_info("25/1", "0/0")["exact"], False)
        self.assertEqual(media.fps_info("0/0", "0/0")["fps"], None)
        self.assertEqual(media.fps_info(None, None)["kind"], "unknown")

    def test_audio_streams_and_container(self):
        p = media.parse_ffprobe(VIDEO_JSON, "video")
        self.assertEqual(p["audio"][0], {"index": 1, "codec": "aac", "channels": 2, "channel_layout": "stereo",
                                         "sample_rate": 48000, "bit_rate": None, "duration_seconds": None})
        self.assertEqual(p["container"]["duration_seconds"], 12.512)
        self.assertEqual(p["container"]["creation_time"], "2026-10-02T10:00:00.000000Z")

    def test_video_extension_but_audio_only(self):
        p = media.parse_ffprobe(AUDIO_JSON, "video")
        self.assertEqual(p["derived_type"], "audio")
        self.assertIn("EXTENSION_MISMATCH", [c for c, _ in p["warnings"]])

    def test_video_without_audio_and_no_streams(self):
        d = json.loads(json.dumps(VIDEO_JSON)); d["streams"].pop()
        self.assertIn("NO_AUDIO_STREAM", [c for c, _ in media.parse_ffprobe(d, "video")["warnings"]])
        empty = {"streams": [], "format": {"format_name": "x"}}
        self.assertIn("NO_MEDIA_STREAMS", [c for c, _ in media.parse_ffprobe(empty, "video")["warnings"]])
        self.assertIn("ZERO_DURATION", [c for c, _ in media.parse_ffprobe(empty, "video")["warnings"]])

    def test_image_has_dimensions_and_no_frame_rate(self):
        p = media.parse_ffprobe(IMAGE_JSON, "image")
        self.assertEqual((p["image"]["width"], p["image"]["height"]), (4000, 3000))
        self.assertEqual(p["video"], [])

    def test_rotation_swaps_display_size(self):
        d = json.loads(json.dumps(VIDEO_JSON))
        d["streams"][0]["side_data_list"] = [{"rotation": -90}]
        v = media.parse_ffprobe(d, "video")["video"][0]
        self.assertEqual((v["rotation"], v["display_width"], v["display_height"]), (270, 1080, 1920))

    def test_cover_art_is_not_a_video_stream(self):
        d = {"streams": [{"index": 0, "codec_type": "audio", "codec_name": "mp3", "channels": 2, "sample_rate": "44100"},
                         {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}],
             "format": {"duration": "10"}}
        self.assertEqual(media.parse_ffprobe(d, "audio")["video"], [])

    def test_probe_failure_modes(self):
        cases = {"nonzero": lambda a, t: (1, "", "moov atom not found"),
                 "badjson": lambda a, t: (0, "<<not json>>", ""),
                 "empty": lambda a, t: (0, "{}", ""),
                 "oserror": lambda a, t: (_ for _ in ()).throw(OSError("denied"))}
        for name, runner in cases.items():
            pr = media.Prober(path="x", version="v", runner=runner)
            res = pr.probe("f.mov")
            self.assertEqual(res["status"], "failed", name)
            self.assertTrue(res["error"], name)

        def timeout(a, t):
            raise media.ProbeTimeout("timed out after 1s")
        self.assertEqual(media.Prober(path="x", runner=timeout).probe("f")["status"], "failed")

    def test_detection(self):
        self.assertFalse(media.Prober.detect(which=lambda n: None).available)
        self.assertIn("not found", media.Prober.detect(which=lambda n: None).reason)
        ok = media.Prober.detect(which=lambda n: "C:/ff/ffprobe.exe", runner=fake_runner())
        self.assertTrue(ok.available)
        self.assertIn("fake-1.0", ok.version)
        bad = media.Prober.detect(which=lambda n: "C:/ff/ffprobe.exe", runner=lambda a, t: (0, "something else", ""))
        self.assertFalse(bad.available)

        def broken(a, t):
            raise OSError("not executable")
        self.assertFalse(media.Prober.detect(which=lambda n: "x", runner=broken).available)
        un = no_prober().probe("f.mov")
        self.assertEqual(un["status"], "unavailable")


class TestScan(Base):
    def test_buckets(self):
        scan = ingest.scan_folder(self.trip)
        rels = {e.rel.replace("\\", "/"): e for e in scan.files}
        self.assertEqual(set(rels), {"camera/a.mov", "phone/b.mp4", "audio/c.wav", "images/d.jpg",
                                     "notes/n.txt", "loose.mov"})
        self.assertEqual(rels["camera/a.mov"].category, "camera")
        self.assertEqual(rels["loose.mov"].category, "unsorted")
        self.assertEqual([s["rel"].replace("\\", "/") for s in scan.skipped], ["camera/readme.xyz"])
        self.assertEqual(len(scan.ignored), 4)
        self.assertEqual(scan.errors, [])

    def test_inaccessible_folder_is_reported_not_fatal(self):
        real_walk = os.walk

        def walk(top, topdown=True, onerror=None, followlinks=False):
            onerror(PermissionError(13, "Permission denied", str(self.trip / "private")))
            return real_walk(top, topdown=topdown, onerror=onerror, followlinks=followlinks)
        with mock.patch("creator_state.ingest.os.walk", walk):
            scan = ingest.scan_folder(self.trip)
        self.assertEqual(len(scan.errors), 1)
        self.assertIn("not accessible", scan.errors[0]["error"])
        self.assertEqual(len(scan.files), 6)

    def test_summary_flags_zero_byte_and_duplicate_names(self):
        self.put("camera/empty.mov", b"")
        self.put("phone/a.mov", b"another a")
        s = ingest.summarize_scan(ingest.scan_folder(self.trip))
        self.assertIn(str(Path("camera/empty.mov")), s["zero_byte"])
        self.assertIn("a.mov", s["duplicate_names"])


class TestIngestEndToEnd(Base):
    def test_originals_untouched_and_nothing_written_beside_them(self):
        before = snapshot(self.trip)
        res = self.do_ingest()
        self.assertEqual(snapshot(self.trip), before, "originals and their folder must be byte-for-byte unchanged")
        self.assertEqual(res["items"], 6)
        self.assertTrue(res["created_project"])

    def test_manifest_content(self):
        self.do_ingest()
        m = self.manifest()
        self.assertEqual(m["manifest_version"], 1)
        a = self.item("a.mov")
        self.assertEqual(a["path"], os.path.abspath(self.files["camera/a.mov"]))
        self.assertTrue(os.path.isabs(a["path"]))
        self.assertEqual((a["media_type"], a["source_category"], a["extension"], a["media_type_source"]),
                         ("video", "camera", ".mov", "ffprobe"))
        self.assertEqual(a["file"]["size"], len(b"video-a" * 500))
        self.assertEqual(a["file"]["sha256"], util.sha256_file(self.files["camera/a.mov"]))
        self.assertEqual(a["video"][0]["frame_rate"]["fps_rational"], "30000/1001")
        self.assertEqual(a["audio"][0]["codec"], "aac")
        self.assertEqual(a["container"]["duration_seconds"], 12.512)
        self.assertIsNotNone(a["file"]["mtime"])
        self.assertEqual(self.item("c.wav")["media_type"], "audio")
        self.assertEqual(self.item("d.jpg")["image"]["width"], 4000)
        notes = self.item("n.txt")
        self.assertEqual((notes["media_type"], notes["source_category"], notes["probe"]["status"]),
                         ("notes", "notes", "not_applicable"))
        self.assertEqual(len(m["skipped"]), 1)
        self.assertEqual(len(m["ignored"]), 4)
        self.assertEqual(m["summary"]["by_type"], {"video": 3, "audio": 1, "image": 1, "notes": 1})

    def test_state_stage_checkpoint_sources_and_decision(self):
        self.do_ingest()
        p = self.project()
        s = p.load_state()
        self.assertEqual(s["stages"]["ingest"]["status"], "completed")
        self.assertEqual([o["path"] for o in s["stages"]["ingest"]["outputs"]],
                         ["source_manifest.json", "analysis/ingest_report.md"])
        self.assertIn("completed ingest", [c["reason"] for c in p.list_checkpoints()])
        self.assertEqual(len(s["source_media"]), 6)
        self.assertTrue(all(os.path.isabs(m["path"]) for m in s["source_media"]))
        self.assertTrue(any("Hybrid media ingestion" in d["text"] for d in s["decisions"]))
        self.assertEqual(recovery.analyze(p)["resume"]["stage"], "understand")
        self.assertFalse(recovery.has_critical(recovery.analyze(p)))
        self.assertFalse([k for k, o in s["stages"]["ingest"]["operations"].items() if o["status"] == "started"])

    def test_report_log_and_handoff(self):
        self.do_ingest()
        p = self.project()
        report = (p.dir / "analysis" / "ingest_report.md").read_text(encoding="utf-8")
        for needle in ("a.mov", "1920x1080", "29.97", "referenced in place", "unsupported format '.xyz'",
                       "Thumbs.db", "none were copied"):
            self.assertIn(needle, report)
        log = (p.dir / "logs" / "activity.log").read_text(encoding="utf-8")
        for needle in ("ingest.started", "ingest.completed", "checkpoint.saved", "source.registered_batch"):
            self.assertIn(needle, log)
        handoff = (p.dir / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn("## Media inventory", handoff)
        self.assertIn("6 files catalogued", handoff)

    def test_source_outside_studio_root(self):
        with tempfile.TemporaryDirectory() as other:
            ext = Path(other) / "camera_card"
            ext.mkdir()
            f = ext / "x.mov"
            f.write_bytes(b"external")
            before = snapshot(ext)
            res = ingest.ingest(self.root, str(ext), prober=fake_prober())
            self.assertEqual(res["project"], "camera-card")
            it = ingest.load_manifest(self.project("camera-card"))["items"][0]
            self.assertEqual(it["path"], os.path.abspath(f))
            self.assertEqual(snapshot(ext), before)

    def test_zero_supported_files_creates_nothing(self):
        empty = self.root / "Incoming" / "empty"
        empty.mkdir(parents=True)
        (empty / "only.xyz").write_bytes(b"x")
        with self.assertRaises(ingest.IngestError) as cm:
            ingest.ingest(self.root, "empty", prober=fake_prober())
        self.assertIn("nothing to ingest", str(cm.exception))
        self.assertFalse((self.root / "Projects" / "empty").exists())

    def test_unknown_source_rejected(self):
        with self.assertRaises(ingest.IngestError):
            ingest.ingest(self.root, "does-not-exist", prober=fake_prober())

    def test_project_inside_source_refused(self):
        d = self.root / "Projects" / "clash"
        d.mkdir(parents=True)
        (d / "a.mov").write_bytes(b"x")
        with self.assertRaises(ingest.IngestError):
            ingest.ingest(self.root, str(d), project_slug="clash", prober=fake_prober())

    def test_bad_hash_policy(self):
        with self.assertRaises(ingest.IngestError):
            self.do_ingest(hash_policy="sometimes")


class TestDuplicatesAndReingest(Base):
    def test_rerun_requires_reopen_and_does_not_duplicate(self):
        self.do_ingest()
        with self.assertRaises(ingest.IngestError) as cm:
            self.do_ingest()
        self.assertIn("--reopen", str(cm.exception))
        res = self.do_ingest(reopen=True)
        self.assertEqual(res["items"], 6)
        self.assertEqual(self.manifest()["ingest_runs"], 2)
        self.assertEqual(len(self.project().load_state()["source_media"]), 6, "no duplicate registration")
        self.assertEqual(res["cache_hits"], 6, "unchanged files reuse their probe cache")

    def test_new_file_is_added_old_ones_kept(self):
        self.do_ingest()
        self.put("camera/new.mov", b"brand new")
        res = self.do_ingest(reopen=True)
        self.assertEqual(res["items"], 7)
        self.assertEqual(res["cache_hits"], 6)
        self.assertEqual(len(self.project().load_state()["source_media"]), 7)

    def test_identical_content_flagged_but_kept(self):
        self.put("phone/copy_of_a.mov", b"video-a" * 500)
        self.do_ingest()
        dup = self.item("copy_of_a.mov")
        a = self.item("a.mov")
        self.assertEqual(dup["duplicate_of"], a["id"])
        self.assertIn("DUPLICATE_CONTENT", [w["code"] for w in dup["warnings"]])
        self.assertIsNone(a["duplicate_of"])
        self.assertEqual(len(self.manifest()["items"]), 7)

    def test_second_folder_into_same_project(self):
        self.do_ingest()
        more = self.root / "Incoming" / "more"
        self.put("m1.mov", b"more footage", base=more)
        res = ingest.ingest(self.root, "more", project_slug="trip", prober=fake_prober(), reopen=True)
        self.assertEqual(res["items"], 7)
        self.assertEqual(len(self.manifest()["source_roots"]), 2)
        self.assertEqual(self.manifest()["ingest_runs"], 2)

    def test_missing_original_detected_and_kept_in_inventory(self):
        self.do_ingest()
        self.files["camera/a.mov"].unlink()
        v = ingest.verify_sources(self.project())
        self.assertEqual(v["counts"].get("missing"), 1)
        self.assertEqual(v["problems"], 1)
        self.assertIn("SOURCE_MEDIA_MISSING", [f["code"] for f in recovery.analyze(self.project())["findings"]])
        self.do_ingest(reopen=True)
        a = self.item("a.mov")
        self.assertEqual(a["status"], "missing")
        self.assertIn("SOURCE_MISSING", [w["code"] for w in a["warnings"]])
        self.assertEqual(len(self.manifest()["items"]), 6, "missing files are flagged, never silently dropped")

    def test_changed_original_detected(self):
        self.do_ingest()
        self.files["phone/b.mp4"].write_bytes(b"someone edited this original")
        v = ingest.verify_sources(self.project())
        self.assertEqual(v["counts"].get("changed"), 1)
        self.do_ingest(reopen=True)
        self.assertIn("SOURCE_CHANGED_SINCE_LAST_INGEST", [w["code"] for w in self.item("b.mp4")["warnings"]])
        self.assertIn("SOURCE_MEDIA_CHANGED", [f["code"] for f in recovery.analyze(self.project())["findings"]])

    def test_deep_verify_catches_same_size_edit(self):
        self.do_ingest()
        f = self.files["notes/n.txt"]
        original = f.read_bytes()
        st = f.stat()
        f.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size AND same mtime
        quick = ingest.verify_sources(self.project())
        self.assertEqual(quick["problems"], 0)
        deep = ingest.verify_sources(self.project(), deep=True)
        self.assertEqual(deep["counts"].get("changed"), 1)

    def test_verify_before_ingest_errors(self):
        p = Project.create(self.root / "Projects", "blank")
        with self.assertRaises(ingest.IngestError):
            ingest.verify_sources(p)


class TestInaccessibleAndBadFiles(Base):
    def test_unreadable_file_is_recorded_and_ingest_still_completes(self):
        target = os.path.abspath(self.files["audio/c.wav"])
        real = media.open_probe_read

        def deny(path):
            if os.path.abspath(path) == target:
                raise PermissionError(13, "Permission denied")
            return real(path)
        with mock.patch("creator_state.media.open_probe_read", deny):
            self.do_ingest()
        c = self.item("c.wav")
        self.assertEqual(c["status"], "inaccessible")
        self.assertIn("FILE_INACCESSIBLE", [w["code"] for w in c["warnings"]])
        self.assertIsNone(c["file"])
        tasks = [t["text"] for t in self.project().load_state()["tasks"]]
        self.assertTrue(any("not accessible" in t for t in tasks))
        self.assertEqual(len(self.project().load_state()["source_media"]), 5, "unreadable file is not registered")

    def test_corrupt_media_gets_probe_failed_warning(self):
        bad = lambda path, n: (1, "", "moov atom not found") if path.endswith("b.mp4") else None
        self.do_ingest(prober=fake_prober(fake_runner(on_call=bad)))
        b = self.item("b.mp4")
        self.assertEqual(b["probe"]["status"], "failed")
        self.assertIn("PROBE_FAILED", [w["code"] for w in b["warnings"]])
        self.assertEqual(b["media_type"], "video", "type falls back to the extension, flagged as such")
        self.assertEqual(b["media_type_source"], "extension")
        self.assertIsNone(b["video"])
        self.assertTrue(any("could not read" in t["text"] for t in self.project().load_state()["tasks"]))

    def test_hash_policies(self):
        self.do_ingest(hash_policy="never")
        a = self.item("a.mov")
        self.assertEqual((a["file"]["sha256"], a["file"]["hash_status"]), (None, "disabled"))
        self.assertIsNone(self.project().load_state()["source_media"][0]["sha256"])
        with mock.patch.object(media, "HASH_AUTO_MAX_BYTES", 1000):
            self.do_ingest(hash_policy="auto", reopen=True)
        a = self.item("a.mov")
        self.assertEqual(a["file"]["hash_status"], "skipped_large")
        self.assertIn("HASH_SKIPPED_LARGE", [w["code"] for w in a["warnings"]])
        self.assertEqual(self.item("n.txt")["file"]["hash_status"], "computed")
        self.do_ingest(hash_policy="always", reopen=True)
        self.assertEqual(self.item("a.mov")["file"]["sha256"], util.sha256_file(self.files["camera/a.mov"]))


class TestFfprobeUnavailable(Base):
    def test_graceful_fallback_invents_nothing(self):
        before = snapshot(self.trip)
        res = self.do_ingest(prober=no_prober())
        self.assertFalse(res["ffprobe"]["available"])
        self.assertEqual(snapshot(self.trip), before)
        a = self.item("a.mov")
        self.assertEqual(a["probe"]["status"], "unavailable")
        for k in ("video", "audio", "container", "image"):
            self.assertIsNone(a[k], f"{k} must stay empty, not guessed")
        self.assertEqual((a["media_type"], a["media_type_source"]), ("video", "extension"))
        self.assertIn("FFPROBE_UNAVAILABLE", [w["code"] for w in a["warnings"]])
        self.assertEqual(a["file"]["size"], len(b"video-a" * 500), "filesystem facts are still recorded")
        self.assertIsNotNone(a["file"]["sha256"])
        p = self.project()
        self.assertEqual(p.load_state()["stages"]["ingest"]["status"], "completed")
        self.assertTrue(any("Install FFmpeg" in t["text"] for t in p.load_state()["tasks"]))
        self.assertIn("ingest.ffprobe_missing", (p.dir / "logs" / "activity.log").read_text(encoding="utf-8"))
        self.assertIn("NOT AVAILABLE", (p.dir / "analysis" / "ingest_report.md").read_text(encoding="utf-8"))
        self.assertIn("NOT available", (p.dir / "HANDOFF.md").read_text(encoding="utf-8"))
        # proxies cannot be planned for unprobed media
        rows = proxy.plan(p, proxy.load_settings(p))
        self.assertTrue(rows and all(r["action"] == "blocked" for r in rows))

    def test_reingest_after_installing_ffprobe_upgrades_records(self):
        self.do_ingest(prober=no_prober())
        res = self.do_ingest(prober=fake_prober(), reopen=True)
        self.assertEqual(res["cache_hits"], 1, "only the notes file (no probe needed) is reused; "
                                               "the 5 media records are refreshed now that ffprobe exists")
        a = self.item("a.mov")
        self.assertEqual(a["probe"]["status"], "ok")
        self.assertEqual(a["video"][0]["width"], 1920)
        self.assertNotIn("FFPROBE_UNAVAILABLE", [w["code"] for w in a["warnings"]])


class TestResumability(Base):
    def test_interrupted_ingest_resumes_from_cache(self):
        def crash_on_third(path, n):
            if n == 3:
                raise RuntimeError("simulated crash / session killed")
        with self.assertRaises(RuntimeError):
            self.do_ingest(prober=fake_prober(fake_runner(on_call=crash_on_third)))
        p = self.project()
        s = p.load_state()
        self.assertEqual(s["stages"]["ingest"]["status"], "in_progress")
        self.assertFalse((p.dir / "source_manifest.json").exists(), "no half-finished manifest")
        self.assertEqual(len(list((p.dir / "analysis" / "probe_cache").glob("*.json"))), 2,
                         "the two files finished before the crash were cached")
        rep = recovery.analyze(p)
        self.assertEqual(rep["resume"]["action"], "resume_interrupted")
        self.assertIn("UNFINISHED_OPERATION", [f["code"] for f in rep["findings"]])
        self.assertEqual(s["source_media"], [], "nothing registered before the manifest exists")

        runner = fake_runner()
        res = self.do_ingest(prober=fake_prober(runner))
        self.assertTrue(res["resumed"])
        self.assertEqual(res["cache_hits"], 2)
        self.assertEqual(len(runner.calls), 3, "only the 3 media files without a cache entry were probed again")
        s = p.load_state()
        self.assertEqual(s["stages"]["ingest"]["status"], "completed")
        self.assertEqual(s["stages"]["ingest"]["attempts"], 2)
        self.assertFalse([k for k, o in s["stages"]["ingest"]["operations"].items() if o["status"] == "started"])
        self.assertTrue(any(o["status"] == "failed" for o in s["stages"]["ingest"]["operations"].values()),
                        "the interrupted operation is recorded as failed, not silently forgotten")
        self.assertEqual(len(s["source_media"]), 6)
        self.assertEqual(len(self.manifest()["items"]), 6)

    def test_interrupted_before_registration_does_not_double_register(self):
        orig = Project.register_sources
        calls = {"n": 0}

        def flaky(self_, recs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("crash while registering")
            return orig(self_, recs)
        with mock.patch.object(Project, "register_sources", flaky):
            with self.assertRaises(RuntimeError):
                self.do_ingest()
            res = self.do_ingest()
        self.assertTrue(res["resumed"])
        self.assertEqual(len(self.project().load_state()["source_media"]), 6)

    def test_corrupt_cache_entry_is_ignored(self):
        with self.assertRaises(RuntimeError):
            self.do_ingest(prober=fake_prober(fake_runner(on_call=lambda p, n: (_ for _ in ()).throw(RuntimeError("x")) if n == 2 else None)))
        cache = self.project().dir / "analysis" / "probe_cache"
        victim = sorted(cache.glob("*.json"))[0]
        victim.write_text("{ truncated", encoding="utf-8")
        res = self.do_ingest()
        self.assertEqual(res["items"], 6)


class TestCLI(Base):
    def run_cli(self, *argv, prober=None):
        buf, err = io.StringIO(), io.StringIO()
        ctx = mock.patch.object(media.Prober, "detect", return_value=prober or fake_prober())
        with ctx, mock.patch("sys.stderr", err):
            code = cli_main(["--root", str(self.root), *argv], out=buf)
        return code, buf.getvalue(), err.getvalue()

    def test_incoming_list_and_inspect(self):
        code, out, _ = self.run_cli("incoming-list")
        self.assertEqual(code, 0)
        self.assertIn("trip", out)
        self.assertIn("video 3", out)
        self.assertIn("no project yet", out)
        before = snapshot(self.root)
        code, out, _ = self.run_cli("incoming-inspect", "trip")
        self.assertEqual(code, 0)
        for needle in ("Supported files  : 6", "Would use project: trip (new)", "unsupported format '.xyz'",
                       "Nothing was changed", "ffprobe          : ffprobe version fake-1.0"):
            self.assertIn(needle, out)
        self.assertEqual(snapshot(self.root), before, "inspect must not change anything")
        code, out, _ = self.run_cli("incoming-inspect", "trip", "--json")
        self.assertEqual(json.loads(out)["summary"]["files"], 6)

    def test_incoming_list_empty_explains_where_to_put_files(self):
        shutil.rmtree(self.root / "Incoming")
        code, out, _ = self.run_cli("incoming-list")
        self.assertIn("Incoming", out)
        self.assertIn("put footage", out)

    def test_full_cli_flow(self):
        code, out, err = self.run_cli("ingest", "trip", "--title", "My Trip")
        self.assertEqual(code, 0, err)
        self.assertIn("Created project 'trip'", out)
        self.assertIn("Catalogued 6 files", out)
        code, out, _ = self.run_cli("incoming-list")
        self.assertIn("ingest: completed", out)
        code, out, _ = self.run_cli("inventory", "trip")
        self.assertEqual(code, 0)
        for needle in ("a.mov", "1920x1080", "29.97", "aac 2ch", "c.wav", "d.jpg", "6 item(s)"):
            self.assertIn(needle, out)
        code, out, _ = self.run_cli("inventory", "trip", "--type", "audio")
        self.assertIn("1 item(s)", out)
        code, out, _ = self.run_cli("inventory", "trip", "--json")
        self.assertEqual(len(json.loads(out)), 6)
        code, out, _ = self.run_cli("verify-sources", "trip", "--deep")
        self.assertEqual(code, 0)
        self.assertIn("ok 6", out)
        code, out, _ = self.run_cli("ingest-status", "trip")
        for needle in ("completed", "6 files", "Originals now: ok 6", "ffprobe      : ffprobe version fake-1.0"):
            self.assertIn(needle, out)
        self.files["audio/c.wav"].unlink()
        code, out, _ = self.run_cli("verify-sources", "trip")
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)
        code, out, _ = self.run_cli("status", "trip")  # existing commands still work
        self.assertEqual(code, 0)
        self.assertIn("SOURCE_MEDIA_MISSING", out)

    def test_cli_reingest_and_errors(self):
        self.run_cli("ingest", "trip")
        code, out, err = self.run_cli("ingest", "trip")
        self.assertEqual(code, 2)
        self.assertIn("--reopen", err)
        self.assertEqual(self.run_cli("ingest", "trip", "--reopen")[0], 0)
        self.assertEqual(self.run_cli("ingest", "nope")[0], 2)
        self.assertEqual(self.run_cli("inventory", "ghost-project")[0], 2)

    def test_cli_without_ffprobe_says_so(self):
        code, out, _ = self.run_cli("ingest", "trip", prober=no_prober())
        self.assertEqual(code, 0)
        self.assertIn("NOT AVAILABLE", out)
        self.assertIn("FFPROBE_UNAVAILABLE", out)
        code, out, _ = self.run_cli("inventory", "trip")
        self.assertIn("video", out)
        self.assertIn("?", out)


class TestManagedCopy(Base):
    def setUp(self):
        super().setUp()
        self.do_ingest()
        self.proj = self.project()
        self.mid = self.item("a.mov")["id"]

    def test_copy_is_verified_and_original_untouched(self):
        before = snapshot(self.trip)
        rec = managed.managed_copy(self.proj, self.mid)
        dest = self.proj.dir / rec["copy_path"]
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), self.files["camera/a.mov"].read_bytes())
        self.assertEqual(rec["sha256"], util.sha256_file(dest))
        self.assertTrue(rec["verified"])
        self.assertEqual(snapshot(self.trip), before)
        self.assertIn(self.mid, (self.proj.dir / "analysis" / "managed_copies.json").read_text(encoding="utf-8"))
        self.assertFalse(list((self.proj.dir / "assets" / "managed").glob(".*.part")))

    def test_never_overwrites(self):
        managed.managed_copy(self.proj, self.mid)
        dest = next((self.proj.dir / "assets" / "managed").glob("*a.mov"))
        dest.write_bytes(b"precious existing file")
        with self.assertRaises(managed.ManagedCopyError):
            managed.managed_copy(self.proj, self.mid)
        self.assertEqual(dest.read_bytes(), b"precious existing file")

    def test_refuses_if_original_changed_since_ingest(self):
        self.files["camera/a.mov"].write_bytes(b"changed after ingest")
        with self.assertRaises(managed.ManagedCopyError):
            managed.managed_copy(self.proj, self.mid)
        self.assertFalse(list((self.proj.dir / "assets" / "managed").glob("*")))
        self.assertEqual(self.files["camera/a.mov"].read_bytes(), b"changed after ingest")

    def test_unknown_id_missing_original_and_cli_needs_yes(self):
        with self.assertRaises(managed.ManagedCopyError):
            managed.managed_copy(self.proj, "m_unknown")
        buf = io.StringIO()
        self.assertEqual(cli_main(["--root", str(self.root), "managed-copy", "trip", self.mid], out=buf), 0)
        self.assertIn("Re-run with --yes", buf.getvalue())
        self.assertFalse((self.proj.dir / "assets" / "managed").exists() and
                         list((self.proj.dir / "assets" / "managed").glob("*")))
        self.files["camera/a.mov"].unlink()
        self.assertEqual(self.item("a.mov")["status"], "ok")
        with self.assertRaises(managed.ManagedCopyError):
            managed.managed_copy(self.proj, self.mid)


class TestProxyDesign(Base):
    def setUp(self):
        super().setUp()
        self.do_ingest()
        self.proj = self.project()

    def fake_ffmpeg_ok(self, calls=None):
        def run(argv):
            if calls is not None:
                calls.append(argv)
            Path(argv[-1]).write_bytes(b"fake proxy bytes")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return run

    def test_ingest_never_creates_proxies(self):
        self.assertFalse((self.proj.dir / "proxies").exists())
        self.assertFalse((self.proj.dir / "analysis" / "proxy_manifest.json").exists())

    def test_settings_defaults_overrides_and_validation(self):
        s = proxy.load_settings(self.proj)
        self.assertEqual(s, proxy.DEFAULT_SETTINGS)
        self.assertEqual(proxy.load_settings(self.proj, {"max_height": 720})["max_height"], 720)
        (self.proj.dir / "proxy_settings.json").write_text(json.dumps({"crf": 28}), encoding="utf-8")
        self.assertEqual(proxy.load_settings(self.proj)["crf"], 28)
        for bad in ({"crf": 99}, {"container": "exe"}, {"nonsense": 1}, {"max_height": "tall"}):
            with self.assertRaises(proxy.ProxyError):
                proxy.load_settings(self.proj, bad)

    def test_deterministic_names_depend_on_settings(self):
        s1, s2 = proxy.load_settings(self.proj), proxy.load_settings(self.proj, {"max_height": 720})
        mid = self.item("a.mov")["id"]
        self.assertEqual(proxy.proxy_path(self.proj, mid, s1), proxy.proxy_path(self.proj, mid, s1))
        self.assertNotEqual(proxy.proxy_path(self.proj, mid, s1), proxy.proxy_path(self.proj, mid, s2))
        self.assertEqual(proxy.proxy_path(self.proj, mid, s1).parent, self.proj.dir / "proxies")

    def test_plan_lists_only_videos_and_touches_nothing(self):
        before = snapshot(self.root)
        rows = proxy.plan(self.proj, proxy.load_settings(self.proj))
        self.assertEqual(sorted(r["name"] for r in rows), ["a.mov", "b.mp4", "loose.mov"])
        self.assertTrue(all(r["action"] == "create" for r in rows))
        self.assertEqual(snapshot(self.root), before)

    def test_make_requires_tools(self):
        s = proxy.load_settings(self.proj)
        with self.assertRaises(proxy.ProxyError):
            proxy.make(self.proj, s, fake_prober(), None)
        with self.assertRaises(proxy.ProxyError):
            proxy.make(self.proj, s, no_prober(), "ffmpeg")

    def test_make_verifies_records_and_never_overwrites(self):
        s = proxy.load_settings(self.proj)
        before = snapshot(self.trip)
        calls = []
        res = proxy.make(self.proj, s, fake_prober(), "ffmpeg", only_ids={self.item("a.mov")["id"]},
                         runner=self.fake_ffmpeg_ok(calls))
        self.assertEqual([r["status"] for r in res], ["created"])
        argv = calls[0]
        self.assertIn("-n", argv, "ffmpeg must be told never to overwrite")
        self.assertEqual(argv[argv.index("-i") + 1], os.path.abspath(self.files["camera/a.mov"]))
        out_path = Path(argv[-1])
        self.assertEqual(out_path.parent, self.proj.dir / "proxies")
        final = self.proj.dir / res[0]["proxy"]
        self.assertTrue(final.is_file())
        self.assertFalse(list((self.proj.dir / "proxies").glob(".*part*")))
        rec = json.loads((self.proj.dir / "analysis" / "proxy_manifest.json").read_text(encoding="utf-8"))["proxies"][0]
        self.assertEqual((rec["source_path"], rec["verified"]), (os.path.abspath(self.files["camera/a.mov"]), True))
        self.assertEqual(rec["proxy_sha256"], util.sha256_file(final))
        self.assertEqual(snapshot(self.trip), before, "source folder untouched")
        res2 = proxy.make(self.proj, s, fake_prober(), "ffmpeg", only_ids={self.item("a.mov")["id"]},
                          runner=self.fake_ffmpeg_ok(calls))
        self.assertEqual(res2[0]["status"], "skipped")
        self.assertEqual(len(calls), 1, "no second encode")
        self.assertEqual(len(json.loads((self.proj.dir / "analysis" / "proxy_manifest.json").read_text(encoding="utf-8"))["proxies"]), 1)

    def test_unverifiable_proxy_is_not_kept(self):
        s = proxy.load_settings(self.proj)
        wrong_len = fake_runner(on_call=lambda path, n: (0, json.dumps({**PROXY_JSON, "format": {"duration": "99"}}), "")
                                if ".part" in os.path.basename(path) else None)
        res = proxy.make(self.proj, s, fake_prober(wrong_len), "ffmpeg", only_ids={self.item("a.mov")["id"]},
                         runner=self.fake_ffmpeg_ok())
        self.assertEqual(res[0]["status"], "failed")
        self.assertIn("duration", res[0]["reason"])
        self.assertFalse(list((self.proj.dir / "proxies").glob("*")), "failed proxy must leave no file behind")
        self.assertFalse((self.proj.dir / "analysis" / "proxy_manifest.json").exists())

    def test_ffmpeg_failure_and_changed_source(self):
        s = proxy.load_settings(self.proj)
        fail = lambda argv: subprocess.CompletedProcess(argv, 1, b"", b"Error opening input")
        res = proxy.make(self.proj, s, fake_prober(), "ffmpeg", only_ids={self.item("a.mov")["id"]}, runner=fail)
        self.assertEqual(res[0]["status"], "failed")
        self.assertIn("Error opening input", res[0]["reason"])
        self.files["phone/b.mp4"].write_bytes(b"modified")
        res = proxy.make(self.proj, s, fake_prober(), "ffmpeg", only_ids={self.item("b.mp4")["id"]},
                         runner=self.fake_ffmpeg_ok())
        self.assertEqual(res[0]["status"], "blocked")

    def test_cli_plan_and_yes_guard(self):
        buf = io.StringIO()
        self.assertEqual(cli_main(["--root", str(self.root), "proxy-plan", "trip"], out=buf), 0)
        self.assertIn("Nothing was generated", buf.getvalue())
        buf = io.StringIO()
        self.assertEqual(cli_main(["--root", str(self.root), "proxy-make", "trip"], out=buf), 0)
        self.assertIn("Re-run with --yes", buf.getvalue())
        self.assertFalse((self.proj.dir / "proxies").exists())


HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class TestRealFfmpegOnSyntheticMedia(unittest.TestCase):
    """Real ffprobe/ffmpeg, but only on tiny clips generated here (never real footage)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "CreatorStudio"
        cls.src = cls.root / "Incoming" / "synth" / "camera"
        cls.src.mkdir(parents=True)
        cls.clip = cls.src / "synthetic.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt",
                        "yuv420p", "-c:a", "aac", "-shortest", str(cls.clip)], check=True)
        (cls.src / "corrupt.mov").write_bytes(os.urandom(4096))
        cls.before = snapshot(cls.root / "Incoming" / "synth")
        cls.result = ingest.ingest(cls.root, "synth")
        cls.proj = Project(cls.root / "Projects" / "synth")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_real_probe_values(self):
        m = ingest.load_manifest(self.proj)
        it = next(i for i in m["items"] if i["name"] == "synthetic.mp4")
        v = it["video"][0]
        self.assertEqual((v["width"], v["height"], v["codec"]), (640, 360, "h264"))
        self.assertEqual((v["frame_rate"]["kind"], v["frame_rate"]["exact"], v["frame_rate"]["fps"]), ("constant", True, 25.0))
        self.assertAlmostEqual(it["container"]["duration_seconds"], 2.0, delta=0.2)
        self.assertEqual(it["audio"][0]["codec"], "aac")
        self.assertEqual(it["probe"]["status"], "ok")
        self.assertIn("ffprobe version", m["tools"]["ffprobe"]["version"])

    def test_real_garbage_file_becomes_a_warning(self):
        m = ingest.load_manifest(self.proj)
        bad = next(i for i in m["items"] if i["name"] == "corrupt.mov")
        self.assertEqual(bad["probe"]["status"], "failed")
        self.assertIn("PROBE_FAILED", [w["code"] for w in bad["warnings"]])
        self.assertEqual(self.proj.load_state()["stages"]["ingest"]["status"], "completed")

    def test_real_proxy_roundtrip(self):
        s = proxy.load_settings(self.proj, {"max_height": 180})
        res = proxy.make(self.proj, s, media.Prober.detect(), proxy.find_ffmpeg())
        by = {r["name"]: r for r in res}
        self.assertEqual(by["synthetic.mp4"]["status"], "created", by)
        self.assertEqual(by["corrupt.mov"]["status"], "skipped")
        out = self.proj.dir / by["synthetic.mp4"]["proxy"]
        probe = media.Prober.detect().probe(out)
        parsed = media.parse_ffprobe(probe["data"], "video")
        self.assertEqual(parsed["video"][0]["height"], 180)
        self.assertTrue(parsed["audio"])
        self.assertEqual(proxy.make(self.proj, s, media.Prober.detect(), proxy.find_ffmpeg())[0]["status"], "skipped")
        self.assertEqual(snapshot(self.root / "Incoming" / "synth"), self.before, "originals byte-identical")


if __name__ == "__main__":
    unittest.main()
