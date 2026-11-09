"""Tests for media_analysis. Synthetic fixtures only (generated tones/clips); no network, no AI services.
Tests marked REAL run the actual ffmpeg/ffprobe on tiny generated clips and are skipped if absent.
Run from orchestrator/:  python -I -m unittest discover -s tests -p "test_media_analysis.py" -v"""
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creator_state import media as csm
from media_analysis import (AnalysisCopyRegistry, CapabilityError, EvidenceRef, FfmpegSceneDetector, FixtureDetector,
                            FixtureProvider, FrameRef, Interpretation, LanguageSpan, MediaAnalysisError, MediaRef,
                            Moment, MomentIndex, Provenance, ProviderCapabilities, ShotBoundary, Transcript,
                            TranscriptionRequest, TranscriptSegment, UnavailableDetector, UnavailableProvider,
                            VisualEvent, Word, analyze_wav, build_manifest, find_duplicate_content,
                            get_ref, is_variable_frame_rate, load_audio_analysis, load_manifest, load_transcript,
                            parse_scene_output, run_transcription, save_audio_analysis, save_manifest,
                            save_transcript, verify_item)
from media_analysis import audio_analysis as aa
from media_analysis import proxies, scene_detection
from media_analysis.audio_analysis import AudioWindow

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ------------------------------------------------------------------ fixtures
def make_wav(path, parts, rate=16000, channels=1, width=2):
    """parts: [(seconds, amplitude 0..1)] -> sine bursts (220 Hz) and silences."""
    frames = bytearray()
    for dur, amp in parts:
        for i in range(int(dur * rate)):
            v = amp * math.sin(2 * math.pi * 220 * i / rate)
            if width == 2:
                s = struct.pack("<h", int(v * 32767))
            else:   # 8-bit unsigned
                s = struct.pack("B", int(128 + v * 127))
            frames += s * channels
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return Path(path)


def ffprobe_json(fps_r="30/1", fps_avg="30/1", duration="10.0", audio=True):
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                "r_frame_rate": fps_r, "avg_frame_rate": fps_avg, "nb_frames": "300"}]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
                        "sample_rate": "48000", "channel_layout": "stereo"})
    return json.dumps({"streams": streams, "format": {"format_name": "mov", "duration": duration, "bit_rate": "1000"}})


def fake_prober(out=None, rc=0, err=""):
    out = out or ffprobe_json()
    return csm.Prober(path="ffprobe", version="ffprobe version fake", runner=lambda argv, t: (rc, out, err))


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.src = self.tmp / "originals"
        self.src.mkdir()
        self.out = self.tmp / "analysis"

    def original(self, name="clip.mov", data=b"not really a video " * 100):
        p = self.src / name
        p.write_bytes(data)
        return p

    @staticmethod
    def snapshot(folder):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(folder).iterdir()}


# ----------------------------------------------------------------- inventory
class Inventory(TmpCase):
    def test_manifest_fields_and_stable_ids(self):
        p = self.original()
        m1 = build_manifest(self.src, fake_prober())
        m2 = build_manifest(self.src, fake_prober())
        it = m1["items"][0]
        self.assertEqual(it["id"], csm.item_id(str(p)))            # same ids as creator_state
        self.assertEqual(it["id"], m2["items"][0]["id"])
        self.assertEqual(it["path"], os.path.abspath(p))
        self.assertEqual(it["media_type"], "video")
        self.assertEqual(it["video"][0]["width"], 1920)
        self.assertEqual(it["audio"][0]["channels"], 2)
        self.assertEqual(it["container"]["duration_seconds"], 10.0)
        self.assertEqual(it["file"]["sha256"], hashlib.sha256(p.read_bytes()).hexdigest())
        self.assertEqual(it["file"]["size"], p.stat().st_size)

    def test_content_change_keeps_id_but_changes_checksum(self):
        p = self.original()
        a = build_manifest(self.src, fake_prober())["items"][0]
        p.write_bytes(b"different")
        b = build_manifest(self.src, fake_prober())["items"][0]
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["file"]["sha256"], b["file"]["sha256"])

    def test_originals_untouched(self):
        self.original("a.mov")
        self.original("b.wav", b"RIFF....")
        before = self.snapshot(self.src)
        build_manifest(self.src, fake_prober())
        self.assertEqual(self.snapshot(self.src), before)
        self.assertEqual(sorted(os.listdir(self.src)), ["a.mov", "b.wav"])

    def test_discover_skips_junk_and_unsupported(self):
        self.original("a.mov")
        for n in ("Thumbs.db", "x.part", "notes.txt", "clip.thm", ".hidden.mp4", "data.xyz"):
            (self.src / n).write_bytes(b"x")
        names = [i["name"] for i in build_manifest(self.src, fake_prober())["items"]]
        self.assertEqual(names, ["a.mov"])

    def test_missing_file_and_unsupported_type(self):
        m = build_manifest([self.src / "ghost.mov", self.original("t.xyz")], fake_prober())
        by = {i["name"]: i for i in m["items"]}
        self.assertEqual(by["ghost.mov"]["status"], "missing")
        self.assertEqual(by["ghost.mov"]["warnings"][0]["code"], "SOURCE_MISSING")
        self.assertEqual(by["t.xyz"]["status"], "unsupported")

    def test_probe_failure_marks_invalid_not_crash(self):
        self.original()
        it = build_manifest(self.src, fake_prober(out="", rc=1, err="moov atom not found"))["items"][0]
        self.assertEqual(it["status"], "invalid")
        self.assertEqual(it["warnings"][-1]["code"], "PROBE_FAILED")
        self.assertIsNone(it["container"])

    def test_ffprobe_unavailable_leaves_metadata_null(self):
        self.original()
        it = build_manifest(self.src, csm.Prober(reason="not installed"))["items"][0]
        self.assertEqual(it["probe"]["status"], "unavailable")
        self.assertIsNone(it["container"])
        self.assertIsNone(it["video"])
        self.assertIn("FFPROBE_UNAVAILABLE", [w["code"] for w in it["warnings"]])
        self.assertIsNotNone(it["file"]["sha256"])                 # filesystem facts still recorded

    def test_checksum_policy_and_duplicates(self):
        self.original("a.mov", b"same bytes")
        self.original("b.mov", b"same bytes")
        self.original("c.mov", b"other")
        m = build_manifest(self.src, fake_prober())
        ids = {i["name"]: i["id"] for i in m["items"]}
        self.assertEqual(m["duplicates"], [sorted([ids["a.mov"], ids["b.mov"]])])
        never = build_manifest(self.src, fake_prober(), hash_policy="never")
        self.assertTrue(all(i["file"]["sha256"] is None for i in never["items"]))
        self.assertEqual(find_duplicate_content(never["items"]), [])

    def test_variable_frame_rate_not_trusted(self):
        self.original()
        vfr = build_manifest(self.src, fake_prober(ffprobe_json("30/1", "24/1")))["items"][0]
        self.assertTrue(is_variable_frame_rate(vfr))
        self.assertEqual(vfr["video"][0]["frame_rate"]["kind"], "variable_suspected")
        self.assertIn("VARIABLE_FRAME_RATE", [w["code"] for w in vfr["warnings"]])
        cfr = build_manifest(self.src, fake_prober(ffprobe_json("30000/1001", "30000/1001")))["items"][0]
        self.assertFalse(is_variable_frame_rate(cfr))
        self.assertEqual(cfr["video"][0]["frame_rate"]["fps_rational"], "30000/1001")
        unknown = build_manifest(self.src, fake_prober(ffprobe_json("0/0", "0/0")))["items"][0]
        self.assertTrue(is_variable_frame_rate(unknown))           # unknown is never assumed constant

    def test_verify_item(self):
        p = self.original()
        it = build_manifest(self.src, fake_prober())["items"][0]
        self.assertEqual(verify_item(it, deep=True)["status"], "ok")
        p.write_bytes(p.read_bytes() + b"x")
        self.assertEqual(verify_item(it)["status"], "changed")
        p.unlink()
        self.assertEqual(verify_item(it)["status"], "missing")

    def test_deep_verify_detects_same_size_change(self):
        p = self.original(data=b"A" * 50)
        it = build_manifest(self.src, fake_prober())["items"][0]
        st = p.stat()
        p.write_bytes(b"B" * 50)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(verify_item(it)["status"], "ok")          # cheap check cannot see it
        self.assertEqual(verify_item(it, deep=True)["status"], "changed")

    def test_save_load_and_ref(self):
        self.original()
        m = build_manifest(self.src, fake_prober())
        save_manifest(m, self.out / "manifest.json")
        loaded = load_manifest(self.out / "manifest.json")
        self.assertEqual(loaded["items"], m["items"])
        ref = get_ref(loaded, m["items"][0]["id"])
        self.assertEqual((ref.sha256, ref.duration_s), (m["items"][0]["file"]["sha256"], 10.0))
        with self.assertRaises(MediaAnalysisError):
            get_ref(loaded, "m_nope")

    def test_load_rejects_bad_manifests(self):
        bad = self.tmp / "bad.json"
        for content in ("not json", "{}", json.dumps({"items": [{"id": "a"}, {"id": "a"}]})):
            bad.write_text(content)
            with self.assertRaises(MediaAnalysisError):
                load_manifest(bad)

    def test_reads_creator_state_manifest_shape(self):
        # minimal item in the shape creator_state writes (source_manifest.json)
        cs = {"items": [{"id": "m_abc", "path": "C:/x/clip.mov", "file": {"size": 5, "mtime_ns": 1, "sha256": "ff"},
                         "container": {"duration_seconds": 3.5}}]}
        f = self.tmp / "source_manifest.json"
        f.write_text(json.dumps(cs))
        self.assertEqual(get_ref(load_manifest(f), "m_abc").duration_s, 3.5)


# ------------------------------------------------------------- analysis copies
def fake_ffmpeg_runner(calls, seconds=1.0, rate_override=None):
    def run(argv):
        calls.append(argv)
        part = Path(argv[-1])
        rate = rate_override or int(argv[argv.index("-ar") + 1])
        ch = int(argv[argv.index("-ac") + 1])
        make_wav(part, [(seconds, 0.2)], rate=rate, channels=ch)
        return 0, "", ""
    return run


class AnalysisCopies(TmpCase):
    def ref(self, **kw):
        p = self.original("src.mov")
        st = p.stat()
        base = dict(media_id="m_test", path=str(p), size=st.st_size, mtime_ns=st.st_mtime_ns,
                    sha256=hashlib.sha256(p.read_bytes()).hexdigest(), duration_s=1.0)
        base.update(kw)
        return MediaRef(**base)

    def registry(self, calls, **kw):
        return AnalysisCopyRegistry(self.out, ffmpeg="fake-ffmpeg", runner=fake_ffmpeg_runner(calls, **kw))

    def test_create_records_provenance(self):
        calls, ref = [], self.ref()
        reg = self.registry(calls)
        rec, reused = reg.ensure(ref)
        self.assertFalse(reused)
        out = self.out / rec["output"]["path"]
        self.assertTrue(out.is_file())
        self.assertEqual(rec["output"]["sha256"], hashlib.sha256(out.read_bytes()).hexdigest())
        self.assertEqual(rec["source"]["sha256"], ref.sha256)
        self.assertEqual(rec["settings"], {"sample_rate": 16000, "channels": 1})
        self.assertEqual(rec["tool"]["argv_template"][-1], "<output>")
        self.assertIn("-n", calls[0])                              # ffmpeg never overwrites
        self.assertTrue(calls[0][-1].endswith(".part"))            # written to .part first
        self.assertEqual(list(out.parent.glob("*.part")), [])
        self.assertNotEqual(out.parent, Path(ref.path).parent)

    def test_reuse_valid_copy_without_running_ffmpeg(self):
        calls, ref = [], self.ref()
        reg = self.registry(calls)
        first, _ = reg.ensure(ref)
        second, reused = AnalysisCopyRegistry(self.out, "fake-ffmpeg", fake_ffmpeg_runner(calls)).ensure(ref)  # reloaded
        self.assertTrue(reused)
        self.assertEqual(first["copy_id"], second["copy_id"])
        self.assertEqual(len(calls), 1)

    def test_different_settings_different_copy(self):
        calls, ref = [], self.ref()
        reg = self.registry(calls)
        a, _ = reg.ensure(ref)
        b, _ = reg.ensure(ref, settings={"sample_rate": 8000})
        self.assertNotEqual(a["copy_id"], b["copy_id"])

    def test_stale_when_source_changes_and_new_copy_keeps_old(self):
        calls, ref = [], self.ref()
        reg = self.registry(calls)
        old, _ = reg.ensure(ref)
        Path(ref.path).write_bytes(b"edited original!!")           # simulate a changed source
        self.assertEqual(reg.check(old)["state"], "stale_source")
        self.assertEqual(reg.audit()[0]["state"], "stale_source")
        new_ref = MediaRef("m_test", ref.path, Path(ref.path).stat().st_size, Path(ref.path).stat().st_mtime_ns,
                           None, 1.0)
        new, reused = reg.ensure(new_ref)
        self.assertFalse(reused)
        self.assertNotEqual(old["copy_id"], new["copy_id"])
        self.assertTrue((self.out / old["output"]["path"]).exists())   # stale file not deleted
        self.assertEqual(len(reg.records), 2)

    def test_stale_ref_refused_when_manifest_disagrees_with_disk(self):
        ref = self.ref()
        Path(ref.path).write_bytes(b"changed after the manifest")
        with self.assertRaises(MediaAnalysisError):
            self.registry([]).ensure(ref)

    def test_source_missing_and_output_checks(self):
        calls, ref = [], self.ref()
        reg = self.registry(calls)
        rec, _ = reg.ensure(ref)
        out = self.out / rec["output"]["path"]
        out.write_bytes(out.read_bytes()[:-2] + b"zz")             # same size, other content
        self.assertEqual(reg.check(rec)["state"], "valid")
        self.assertEqual(reg.check(rec, deep=True)["state"], "output_changed")
        out.unlink()
        self.assertEqual(reg.check(rec)["state"], "output_missing")
        Path(ref.path).unlink()
        self.assertEqual(reg.check(rec)["state"], "source_missing")

    def test_missing_ffmpeg_is_a_capability_error(self):
        ref = self.ref()
        with mock.patch.object(proxies, "find_ffmpeg", return_value=None):
            reg = AnalysisCopyRegistry(self.out)
            with self.assertRaises(CapabilityError):
                reg.ensure(ref)
        self.assertFalse((self.out / "copies").exists())
        self.assertEqual(reg.records, [])

    def test_refuses_to_write_beside_original_or_over_existing(self):
        ref = self.ref()
        reg = self.registry([])
        # an original that lives in the registry's own output folder: the copy would land beside it
        inside = reg.copies_dir / "orig.mov"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(b"x" * 10)
        st = inside.stat()
        bad_ref = MediaRef("m_in", str(inside), st.st_size, st.st_mtime_ns, None, 1.0)
        plan_in = reg.plan(bad_ref, "audio_wav")
        with mock.patch.object(reg, "plan", return_value={**plan_in, "output": str(reg.copies_dir / "o.wav")}):
            with self.assertRaises(MediaAnalysisError):
                reg.ensure(bad_ref)
        self.assertEqual(sorted(p.name for p in reg.copies_dir.iterdir()), ["orig.mov"])
        plan = reg.plan(ref, "audio_wav")
        Path(plan["output"]).parent.mkdir(parents=True, exist_ok=True)
        Path(plan["output"]).write_bytes(b"precious")
        with self.assertRaises(MediaAnalysisError):
            reg.ensure(ref)
        self.assertEqual(Path(plan["output"]).read_bytes(), b"precious")

    def test_failures_leave_no_files_and_no_record(self):
        ref = self.ref()
        bad_rc = AnalysisCopyRegistry(self.out, "fake", lambda argv: (1, "", "boom"))
        with self.assertRaises(MediaAnalysisError):
            bad_rc.ensure(ref)
        wrong_len = AnalysisCopyRegistry(self.out, "fake", fake_ffmpeg_runner([], seconds=5.0))
        with self.assertRaises(MediaAnalysisError):
            wrong_len.ensure(ref)                                  # duration 5 s vs source 1 s
        garbage = AnalysisCopyRegistry(self.out, "fake", lambda argv: (Path(argv[-1]).write_bytes(b"junk"), "", "")[1:] and (0, "", ""))
        with self.assertRaises(MediaAnalysisError):
            garbage.ensure(ref)
        self.assertEqual(list((self.out / "copies").iterdir()), [])
        for r in (bad_rc, wrong_len, garbage):
            self.assertEqual(r.records, [])

    def test_original_changing_during_copy_is_detected(self):
        ref = self.ref()

        def run(argv):
            make_wav(Path(argv[-1]), [(1.0, 0.2)])
            Path(ref.path).write_bytes(b"modified while copying")
            return 0, "", ""
        with self.assertRaises(MediaAnalysisError):
            AnalysisCopyRegistry(self.out, "fake", run).ensure(ref)
        self.assertEqual(list((self.out / "copies").iterdir()), [])

    def test_settings_validation(self):
        reg, ref = self.registry([]), self.ref()
        for bad in ({"sample_rate": 12}, {"channels": 5}, {"bogus": 1}):
            with self.assertRaises(MediaAnalysisError):
                reg.plan(ref, "audio_wav", bad)
        with self.assertRaises(MediaAnalysisError):
            reg.plan(ref, "video_8k")

    def test_register_existing_external_copy(self):
        ref = self.ref()
        prox = self.tmp / "proxy.mp4"
        prox.write_bytes(b"proxy bytes")
        reg = self.registry([])
        rec = reg.register_existing(ref, prox, "video_proxy", {"max_height": 540}, {"name": "creator_state.proxy"})
        self.assertTrue(rec["external"])
        self.assertEqual(rec["output"]["sha256"], hashlib.sha256(b"proxy bytes").hexdigest())
        self.assertEqual(reg.check(rec)["state"], "valid")
        with self.assertRaises(MediaAnalysisError):
            reg.register_existing(ref, prox, "video_proxy", {"max_height": 540}, {})      # duplicate
        with self.assertRaises(MediaAnalysisError):
            reg.register_existing(ref, ref.path, "video_proxy", {"x": 1}, {})             # the original itself

    def test_no_analysis_copy_without_ensure(self):
        self.registry([])
        self.assertFalse((self.out / "copies").exists())

    def test_corrupt_registry_rejected(self):
        self.out.mkdir()
        (self.out / proxies.REGISTRY_FILE).write_text('{"schema": "x"}')
        with self.assertRaises(MediaAnalysisError):
            AnalysisCopyRegistry(self.out)


# -------------------------------------------------------------- transcription
KAJ = "Ča si ti rekel? Ne bum ti verjel, to je totally fejk, ajde de"
SEGS = [
    TranscriptSegment("m_a", 0.0, 4.2, KAJ, language="hr", variety="kajkavian", confidence=0.61,
                      language_spans=(LanguageSpan(0, 40, "hr"), LanguageSpan(40, 54, "en"))),
    TranscriptSegment("m_a", 4.5, 7.0, "ja ja ja, ono... ne, ček, ono — ne znam", language="hr"),
    TranscriptSegment("m_a", 7.2, 9.0, "So basically it's a mushroom.", language="en", confidence=0.93,
                      words=(Word("So", 7.2, 7.4), Word("basically", 7.4, 7.9))),
]


class Transcription(TmpCase):
    def prov(self, **kw):
        return Provenance("fixture", "1", "synthetic_fixture", kw)

    def test_segment_validation(self):
        base = dict(media_id="m", start_s=1.0, end_s=2.0, text="x")
        bad = [dict(end_s=0.5), dict(start_s=-1), dict(text="   "), dict(language="not a tag!"), dict(confidence=1.5),
               dict(start_s=float("nan")), dict(words=()), dict(words=(Word("a", 0.0, 0.5),)),
               dict(words=(Word("a", 1.5, 1.9), Word("b", 1.0, 1.4))),
               dict(language_spans=(LanguageSpan(0, 99, "hr"),)),
               dict(language_spans=(LanguageSpan(0, 1, "hr"), LanguageSpan(0, 1, "en")))]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(MediaAnalysisError):
                TranscriptSegment(**{**base, **kw})
        TranscriptSegment(**base)

    def test_text_is_never_modified(self):
        raw = "Ča  si ti  rekel?? ne ne ne—ček"
        decomposed = "c\u030ca \u0161ta"                          # combining caron: must not be NFC-normalised
        for text in (raw, decomposed, KAJ, "  leading and trailing  "):
            self.assertEqual(TranscriptSegment("m", 0, 1, text).text, text)
            self.assertEqual(TranscriptSegment.from_dict(TranscriptSegment("m", 0, 1, text).to_dict()).text, text)

    def test_mixed_language_and_diacritics_roundtrip_on_disk(self):
        t = Transcript("m_a", SEGS, self.prov(), "hr", word_timestamps="engine")
        path = save_transcript(self.out, t)
        raw = path.read_text(encoding="utf-8")
        self.assertIn("Ča si ti rekel", raw)                       # written as real characters
        self.assertIn("ja ja ja, ono... ne, ček, ono — ne znam", raw)
        back = load_transcript(path)
        self.assertEqual(back, t)
        self.assertEqual([s.text for s in back.segments], [s.text for s in SEGS])
        self.assertEqual(back.segments[0].variety, "kajkavian")
        self.assertEqual({s.language for s in back.segments}, {"hr", "en"})
        sp = back.segments[0].language_spans
        self.assertEqual(back.segments[0].text[sp[1].start_char:sp[1].end_char], "totally fejk, ")

    def test_defaults_are_unverified_and_no_fabricated_words(self):
        t = Transcript("m_a", SEGS[:2], self.prov(), "hr")
        self.assertEqual(t.verification, "unverified")
        self.assertIsNone(t.segments[0].words)                     # phrase timing only: nothing derived
        self.assertEqual(t.word_timestamps, "none")
        self.assertIsNone(t.segments[0].speaker_id)
        with self.assertRaises(MediaAnalysisError):                # words present but not declared as engine-supplied
            Transcript("m_a", SEGS, self.prov())

    def test_transcript_level_validation(self):
        with self.assertRaises(MediaAnalysisError):
            Transcript("other", SEGS[:1], self.prov())
        with self.assertRaises(MediaAnalysisError):
            Transcript("m_a", [SEGS[1], SEGS[0]], self.prov())     # unordered: refuse, don't reorder silently
        with self.assertRaises(MediaAnalysisError):
            Transcript("m_a", SEGS[:2], self.prov(), duration_s=3.0)
        with self.assertRaises(MediaAnalysisError):                # a Transcript can never be marked reviewed
            Transcript("m_a", SEGS[:1], self.prov(), verification="reviewed")

    def audio_input(self):
        return str(make_wav(self.tmp / "a.wav", [(0.2, 0.1)]))

    def request(self, lang="hr"):
        return TranscriptionRequest(MediaRef("m_a", "C:/x/a.mov", duration_s=10.0), self.audio_input(), lang)

    def test_unavailable_provider_raises_capability_error(self):
        with self.assertRaises(CapabilityError):
            run_transcription(UnavailableProvider(), self.request())
        self.assertFalse(UnavailableProvider().capabilities().available)

    def test_fixture_provider_end_to_end_marked_synthetic(self):
        t = run_transcription(FixtureProvider(SEGS), self.request())
        self.assertEqual(t.provenance.kind, "synthetic_fixture")
        self.assertIn("synthetic", t.provenance.notes)
        self.assertEqual(t.verification, "unverified")
        self.assertEqual(t.word_timestamps, "engine")
        self.assertEqual(t.duration_s, 10.0)
        self.assertEqual(len(t.segments), 3)

    def test_provider_capability_checks(self):
        with self.assertRaises(CapabilityError):
            run_transcription(FixtureProvider(SEGS, languages=("en",)), self.request("hr"))
        with self.assertRaises(MediaAnalysisError):                # words returned without declaring support
            run_transcription(FixtureProvider(SEGS, word_timestamps=False), self.request())
        with self.assertRaises(MediaAnalysisError):                # variety not declared
            run_transcription(FixtureProvider(SEGS, varieties=()), self.request())
        with self.assertRaises(MediaAnalysisError):                # speaker not declared
            seg = TranscriptSegment("m_a", 0, 1, "hi", speaker_id="S1")
            run_transcription(FixtureProvider([seg]), self.request())
        req = self.request()
        missing = TranscriptionRequest(req.media, str(self.tmp / "nope.wav"), "hr")
        with self.assertRaises(MediaAnalysisError):
            run_transcription(FixtureProvider(SEGS), missing)

    def test_save_is_write_once(self):
        t = Transcript("m_a", SEGS[:2], self.prov(), "hr")
        p1 = save_transcript(self.out, t)
        self.assertEqual(save_transcript(self.out, t), p1)         # identical: fine
        other = Transcript("m_a", SEGS[:1], self.prov(), "hr")     # same id, different content
        with self.assertRaises(MediaAnalysisError):
            save_transcript(self.out, other)
        self.assertEqual(load_transcript(p1), t)

    def test_segment_ids_stable(self):
        a = TranscriptSegment("m", 1, 2, "ista rečenica")
        self.assertEqual(a.record_id, TranscriptSegment("m", 1, 2, "ista rečenica", confidence=0.3).record_id)
        self.assertNotEqual(a.record_id, TranscriptSegment("m", 1, 2, "Ista rečenica").record_id)   # no case folding


# ------------------------------------------------------------------- audio
class AudioAnalysisTests(TmpCase):
    def wav(self, parts, **kw):
        return make_wav(self.tmp / "x.wav", parts, **kw)

    def test_window_validation(self):
        AudioWindow("m", 0, 0.1, -20.0, -10.0)
        for args in ((0.2, 0.1, -20, -10), (0, 0.1, -200, -10), (0, 0.1, 5, 6), (0, 0.1, -10, -20)):
            with self.subTest(args=args), self.assertRaises(MediaAnalysisError):
                AudioWindow("m", *args)

    def test_speech_like_pattern_gaps_and_regions(self):
        # tone 1.0 | quiet 1.5 | tone 1.0 | quiet 0.1 (blip-gap) | tone 0.8 | quiet 3.0
        w = self.wav([(1.0, 0.3), (1.5, 0.0), (1.0, 0.3), (0.1, 0.0), (0.8, 0.3), (3.0, 0.0)])
        a = analyze_wav(w, "m_a")
        self.assertAlmostEqual(a.duration_s, 7.4, places=2)
        between = [g for g in a.gaps if g.position == "between"]
        trailing = [g for g in a.gaps if g.position == "trailing"]
        self.assertEqual(len(between), 1)                          # the 0.1 s dip is not a pause
        self.assertAlmostEqual(between[0].start_s, 1.0, delta=0.15)
        self.assertAlmostEqual(between[0].duration_s, 1.5, delta=0.2)
        self.assertFalse(between[0].long)
        self.assertEqual(len(trailing), 1)
        self.assertTrue(trailing[0].long)
        self.assertAlmostEqual(trailing[0].duration_s, 3.0, delta=0.2)
        states = [r.state for r in a.regions]
        self.assertEqual(states, ["above_threshold", "below_threshold", "above_threshold", "below_threshold"][:len(states)])
        self.assertTrue(all(r.state in ("above_threshold", "below_threshold") for r in a.regions))
        self.assertEqual(a.speech_regions, ())                     # energy is not speech detection
        self.assertEqual(a.provenance.kind, "deterministic")
        self.assertEqual(a.params["threshold_dbfs"], "auto")
        self.assertLess(a.resolved_threshold_dbfs, -10)

    def test_measured_levels(self):
        a = analyze_wav(self.wav([(0.5, 0.5)]), "m", {"threshold_dbfs": -60})
        expected = 20 * math.log10(0.5 / math.sqrt(2))             # RMS of a sine
        self.assertAlmostEqual(a.windows[0].rms_dbfs, expected, delta=0.3)
        self.assertAlmostEqual(a.windows[0].peak_dbfs, 20 * math.log10(0.5), delta=0.3)
        self.assertEqual(a.gaps, ())

    def test_digital_silence_is_one_whole_gap(self):
        a = analyze_wav(self.wav([(2.0, 0.0)]), "m")
        self.assertEqual([(g.position, g.long) for g in a.gaps], [("whole", True)])
        self.assertEqual(a.windows[0].rms_dbfs, aa.FLOOR_DB)

    def test_uniform_tone_has_no_invented_pauses(self):
        a = analyze_wav(self.wav([(3.0, 0.3)]), "m")
        self.assertEqual(a.gaps, ())

    def test_energy_rise_event(self):
        a = analyze_wav(self.wav([(4.0, 0.05), (1.0, 0.5), (1.0, 0.05)]), "m", {"threshold_dbfs": -60})
        self.assertEqual(len(a.energy_events), 1)
        e = a.energy_events[0]
        self.assertAlmostEqual(e.start_s, 4.0, delta=0.15)
        self.assertGreaterEqual(e.delta_db, 6.0)
        self.assertEqual(e.kind, "energy_rise")                    # a level change, not "emphasis"

    def test_stereo_and_8bit(self):
        st = analyze_wav(self.wav([(1.0, 0.3), (1.0, 0.0)], channels=2), "m")
        self.assertEqual(len(st.gaps), 1)
        e8 = analyze_wav(make_wav(self.tmp / "e8.wav", [(1.0, 0.3), (1.0, 0.0)], width=1), "m")
        self.assertEqual(len(e8.gaps), 1)

    def test_bad_input(self):
        junk = self.tmp / "j.wav"
        junk.write_bytes(b"not a wav")
        with self.assertRaises(MediaAnalysisError):
            analyze_wav(junk, "m")
        with self.assertRaises(MediaAnalysisError):
            analyze_wav(self.wav([(1, 0.1)]), "m", {"bogus": 1})
        empty = self.tmp / "e.wav"
        with wave.open(str(empty), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        with self.assertRaises(MediaAnalysisError):
            analyze_wav(empty, "m")

    def test_deterministic_and_persisted(self):
        w = self.wav([(1.0, 0.3), (1.0, 0.0), (1.0, 0.3)])
        a1, a2 = analyze_wav(w, "m"), analyze_wav(w, "m")
        self.assertEqual(a1.to_dict()["windows"], a2.to_dict()["windows"])
        self.assertEqual(a1.gaps, a2.gaps)
        p = save_audio_analysis(self.out, a1)
        self.assertEqual(save_audio_analysis(self.out, a2), p)     # same content, different created_at: fine
        back = load_audio_analysis(p)
        self.assertEqual((back.gaps, back.regions, back.windows), (a1.gaps, a1.regions, a1.windows))
        changed = analyze_wav(self.wav([(1.0, 0.3), (2.0, 0.0), (1.0, 0.3)]), "m")
        with self.assertRaises(MediaAnalysisError):
            save_audio_analysis(self.out, changed)

    def test_speech_provider_unavailable(self):
        with self.assertRaises(CapabilityError):
            aa.UnavailableSpeechActivity().detect_speech("m", "x.wav")

    def test_speech_provider_results_kept_separate(self):
        class Vad:
            name, version = "fake-vad", "0"
            def detect_speech(self, media_id, audio_path):
                return [aa.SpeechRegion(media_id, 0.0, 1.0, 0.9)]
        a = analyze_wav(self.wav([(1.0, 0.3), (1.0, 0.0)]), "m", speech_provider=Vad())
        self.assertEqual(len(a.speech_regions), 1)
        self.assertEqual(a.provenance.settings["speech_provider"], "fake-vad@0")


# --------------------------------------------------------------- scene detection
SCENE_OUT = """frame:0    pts:20      pts_time:2
lavfi.scene_score=0.912345
frame:1    pts:41      pts_time:4.1
lavfi.scene_score=0.455000
"""


class SceneDetection(unittest.TestCase):
    def test_boundary_validation(self):
        ShotBoundary("m", 1.0, "x")
        for kw in (dict(time_s=-1), dict(time_s=float("inf")), dict(confidence=1.5), dict(method=""),
                   dict(frame_refs=(FrameRef("other", 1.0),))):
            with self.subTest(kw=kw), self.assertRaises(MediaAnalysisError):
                ShotBoundary(**{**dict(media_id="m", time_s=1.0, method="x"), **kw})
        with self.assertRaises(MediaAnalysisError):
            FrameRef("m", -1.0)
        with self.assertRaises(MediaAnalysisError):
            VisualEvent("m", 2.0, 1.0, "motion", "x")

    def test_parse_output(self):
        b = parse_scene_output(SCENE_OUT, "m", "ffmpeg-scene@0.4", duration_s=10)
        self.assertEqual([x.time_s for x in b], [2.0, 4.1])
        self.assertEqual(b[0].score, 0.912345)
        self.assertIsNone(b[0].confidence)                         # raw score is not a confidence
        self.assertEqual(b[0].frame_refs[0].time_s, 2.0)
        self.assertIsNone(b[0].frame_refs[0].frame_index)
        self.assertEqual(len(parse_scene_output(SCENE_OUT, "m", "x", duration_s=3.0)), 1)   # beyond duration dropped
        self.assertEqual(parse_scene_output("noise\n", "m", "x"), [])

    def test_boundary_roundtrip_and_ids(self):
        b = ShotBoundary("m", 2.0, "x", 0.5, None, (FrameRef("m", 2.0, image_path="f.png"),))
        self.assertEqual(ShotBoundary.from_dict(b.to_dict()), b)
        self.assertEqual(b.record_id, ShotBoundary("m", 2.0, "x", 0.9).record_id)

    def test_missing_ffmpeg_is_a_capability_error(self):
        det = FfmpegSceneDetector(which=lambda n: None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CREATOR_FFMPEG", None)
            with self.assertRaises(CapabilityError):
                det.detect(MediaRef("m", "C:/x.mov"))
        with self.assertRaises(CapabilityError):
            UnavailableDetector().detect(MediaRef("m", "C:/x.mov"))

    def test_runner_used_and_failure_reported(self):
        calls = []
        det = FfmpegSceneDetector(0.3, ffmpeg="ff", runner=lambda a: (calls.append(a) or (0, SCENE_OUT, "")))
        out = det.detect(MediaRef("m", "C:/x.mov", duration_s=10))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].method, "ffmpeg-scene@0.3")
        self.assertIn("gt(scene,0.3)", " ".join(calls[0]))
        self.assertIn("-an", calls[0])
        failing = FfmpegSceneDetector(ffmpeg="ff", runner=lambda a: (1, "", "bad"))
        with self.assertRaises(MediaAnalysisError):
            failing.detect(MediaRef("m", "C:/x.mov"))
        with self.assertRaises(MediaAnalysisError):
            FfmpegSceneDetector(threshold=2)

    def test_fixture_detector_filters_by_media(self):
        d = FixtureDetector([ShotBoundary("a", 1, "f"), ShotBoundary("b", 2, "f")])
        self.assertEqual([x.time_s for x in d.detect(MediaRef("a", "p"))], [1.0])


# ------------------------------------------------------------------ moment index
def build_index(tmp):
    prov = Provenance("fixture", "1", "synthetic_fixture")
    t = Transcript("m_a", [TranscriptSegment("m_a", 10.0, 13.0, "Pa znaš kaj, to je bilo fejk dobro.", language="hr"),
                           TranscriptSegment("m_a", 30.0, 32.0, "Pa znaš kaj, to je bilo fejk dobro.", language="hr"),
                           TranscriptSegment("m_a", 50.0, 52.0, "Nešto sasvim drugo.", language="hr")], prov)
    wav = make_wav(tmp / "i.wav", [(1.0, 0.3), (1.5, 0.0), (1.0, 0.3)])
    a = analyze_wav(wav, "m_a")
    # shift analysis to line up with the transcript by building gap/energy records directly
    gap = aa.QuietGap("m_a", 13.2, 14.7, -40.0, "between", False)
    a = aa.AudioAnalysis("m_a", 60.0, 16000, a.params, (), a.regions, (gap,), (), a.provenance, -40.0)
    shot = ShotBoundary("m_a", 14.0, "ffmpeg-scene@0.4", 0.7, None, (FrameRef("m_a", 14.0),))
    idx = MomentIndex()
    idx.register_transcript(t)
    idx.register_audio(a)
    idx.register_boundaries([shot], Provenance("ffmpeg-scene", "1"))
    return idx, t, gap, shot


class MomentIndexTests(TmpCase):
    def test_registration_makes_factual_moments_only(self):
        idx, *_ = build_index(self.tmp)
        types = sorted(m.moment_type for m in idx.moments.values())
        self.assertEqual(types, ["pause", "scene_change", "spoken_statement", "spoken_statement", "spoken_statement"])
        self.assertEqual(idx.interpretations, {})                  # nothing is ever labelled automatically

    def test_subjective_types_rejected_as_moment_type(self):
        idx, *_ = build_index(self.tmp)
        eid = next(iter(idx.evidence))
        for bad in ("joke", "reaction", "interesting", "awkward"):
            with self.assertRaises(MediaAnalysisError):
                Moment("m_a", 0, 1, bad, (eid,))
        with self.assertRaises(MediaAnalysisError):
            Moment("m_a", 0, 1, "pause", ())

    def test_unusual_wording_is_not_a_joke(self):
        idx, *_ = build_index(self.tmp)
        self.assertEqual(idx.query(label="joke"), [])
        self.assertTrue(all("joke" not in m.moment_type for m in idx.moments.values()))

    def test_correlate_builds_multi_evidence_moment_and_interpretation_stays_separate(self):
        idx, t, gap, shot = build_index(self.tmp)
        created = idx.correlate(window_s=1.0)
        self.assertEqual(len(created), 1)                          # only statement 1 has neighbours (gap + shot)
        comp = [m for m in idx.moments.values() if m.moment_type == "composite"][0]
        kinds = sorted(idx.evidence[e].kind for e in comp.evidence_ids)
        self.assertEqual(kinds, ["audio_gap", "shot_boundary", "transcript_segment"])
        self.assertEqual(idx.interpretations, {})
        interp = Interpretation("joke", "ai:fake-model", "pause after a punchline-like phrase, visual cut", 0.4)
        cand = idx.add_candidate("m_a", comp.evidence_ids, interp)
        r = idx.resolve(cand.moment_id)
        self.assertEqual(r["moment_type"], "candidate")
        self.assertEqual({e["record_kind"] for e in r["evidence"]}, {"transcription", "measurement", "detection"})
        self.assertEqual(r["interpretations"][0]["label"], "joke")
        self.assertEqual(r["interpretations"][0]["author"], "ai:fake-model")
        self.assertIn("Pa znaš kaj", next(e for e in r["evidence"] if e["kind"] == "transcript_segment")["payload"]["text"])
        self.assertEqual(len(idx.query(label="joke")), 1)

    def test_interpretation_requires_author_and_rationale(self):
        for kw in (dict(author="somebody"), dict(author="ai:"), dict(rationale=" "), dict(label=""), dict(confidence=2)):
            with self.subTest(kw=kw), self.assertRaises(MediaAnalysisError):
                Interpretation(**{**dict(label="joke", author="ai:x", rationale="r"), **kw})

    def test_duplicates_are_detected(self):
        idx, t, gap, shot = build_index(self.tmp)
        n_ev, n_mom = len(idx.evidence), len(idx.moments)
        idx.register_transcript(t)
        idx.register_boundaries([shot], Provenance("ffmpeg-scene", "1"))
        self.assertEqual((len(idx.evidence), len(idx.moments)), (n_ev, n_mom))
        self.assertGreaterEqual(idx.duplicates_skipped, 4)
        m = next(iter(idx.moments.values()))
        again, created = idx.add_moment(Moment(m.media_id, m.start_s, m.end_s, m.moment_type, m.evidence_ids))
        self.assertFalse(created)
        self.assertEqual(again.moment_id, m.moment_id)
        self.assertTrue(idx.interpret(m.moment_id, Interpretation("reaction", "human:creator", "my face")))
        self.assertFalse(idx.interpret(m.moment_id, Interpretation("reaction", "human:creator", "again")))
        self.assertEqual(len(idx.interpretations[m.moment_id]), 1)

    def test_repeated_statements_found(self):
        idx, *_ = build_index(self.tmp)
        groups = idx.repeated_statements()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_evidence_must_exist_and_match_media(self):
        idx, *_ = build_index(self.tmp)
        with self.assertRaises(MediaAnalysisError):
            idx.add_moment(Moment("m_a", 0, 1, "pause", ("nope",)))
        eid = next(iter(idx.evidence))
        with self.assertRaises(MediaAnalysisError):
            idx.add_moment(Moment("m_other", 0, 1, "pause", (eid,)))
        with self.assertRaises(MediaAnalysisError):
            idx.add_candidate("m_a", ["ghost"], Interpretation("joke", "ai:x", "r"))
        with self.assertRaises(MediaAnalysisError):
            idx.interpret("mom_missing", Interpretation("joke", "ai:x", "r"))

    def test_queries(self):
        idx, *_ = build_index(self.tmp)
        self.assertEqual(len(idx.query(moment_type="pause")), 1)
        self.assertEqual(len(idx.query(media_id="m_a", start_s=29, end_s=33)), 1)
        self.assertEqual({m.moment_type for m in idx.at("m_a", 14.0)}, {"scene_change", "pause"})
        self.assertEqual(idx.query(media_id="other"), [])
        self.assertEqual(len(idx.query(evidence_kind="shot_boundary")), 1)

    def test_persistence_roundtrip_and_tamper_detection(self):
        idx, *_ = build_index(self.tmp)
        idx.correlate()
        comp = [m for m in idx.moments.values() if m.moment_type == "composite"][0]
        idx.add_candidate("m_a", comp.evidence_ids, Interpretation("joke", "ai:x", "because"))
        path = self.out / "moments.json"
        idx.save(path)
        back = MomentIndex.load(path)
        self.assertEqual(back.to_dict(), idx.to_dict())
        self.assertEqual(back.resolve(comp.moment_id)["evidence"], idx.resolve(comp.moment_id)["evidence"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["moments"][0]["start_s"] += 1
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(MediaAnalysisError):
            MomentIndex.load(path)
        path.write_text('{"schema": "wrong"}')
        with self.assertRaises(MediaAnalysisError):
            MomentIndex.load(path)


# Creator DNA adapter tests live in tests/test_integration_phase2_phase3.py


# ---------------------------------------------------------------------- REAL tools
@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class RealTools(TmpCase):
    def make_clip(self):
        p = self.src / "synthetic.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=2",
               "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10:d=2",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=4:sample_rate=44100",
               "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
               "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(p)]
        subprocess.run(cmd, check=True, capture_output=True)
        return p

    def test_real_pipeline_on_synthetic_clip(self):
        clip = self.make_clip()
        digest = hashlib.sha256(clip.read_bytes()).hexdigest()
        # inventory (real ffprobe)
        m = build_manifest(self.src)
        it = m["items"][0]
        self.assertEqual(it["status"], "ok")
        self.assertEqual((it["video"][0]["width"], it["video"][0]["height"]), (160, 120))
        self.assertAlmostEqual(it["container"]["duration_seconds"], 4.0, delta=0.3)
        self.assertEqual(it["audio"][0]["sample_rate"], 44100)
        self.assertFalse(is_variable_frame_rate(it))
        ref = MediaRef.from_item(it)
        # analysis copy (real ffmpeg), then reuse
        reg = AnalysisCopyRegistry(self.out)
        rec, reused = reg.ensure(ref)
        self.assertFalse(reused)
        wav = self.out / rec["output"]["path"]
        with wave.open(str(wav)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels()), (16000, 1))
        self.assertTrue(reg.ensure(ref)[1])
        # audio analysis of a continuous tone: no invented pauses
        a = analyze_wav(wav, ref.media_id)
        self.assertEqual(a.gaps, ())
        self.assertAlmostEqual(a.duration_s, 4.0, delta=0.4)
        # scene detection: red -> blue at 2.0 s
        det = FfmpegSceneDetector(0.4)
        shots = det.detect(ref)
        self.assertEqual(len(shots), 1)
        self.assertAlmostEqual(shots[0].time_s, 2.0, delta=0.25)
        self.assertIsNotNone(shots[0].score)
        # moment index
        idx = MomentIndex()
        idx.register_audio(a)
        idx.register_boundaries(shots, det.provenance())
        self.assertEqual([m.moment_type for m in idx.moments.values()], ["scene_change"])
        # the original is byte-identical
        self.assertEqual(hashlib.sha256(clip.read_bytes()).hexdigest(), digest)
        self.assertEqual(verify_item(it, deep=True)["status"], "ok")

    def test_real_ffprobe_rejects_garbage(self):
        (self.src / "broken.mp4").write_bytes(b"this is not an mp4 at all")
        it = build_manifest(self.src)["items"][0]
        self.assertEqual(it["status"], "invalid")
        self.assertIn("PROBE_FAILED", [w["code"] for w in it["warnings"]])


if __name__ == "__main__":
    unittest.main()
