"""Offline tests for the safe Resolve execution layer (T-RSV-002). No Resolve, no network, no media.

Live behaviour is exercised separately by `python -B -m resolve_executor.live_suite` on a disposable project with
synthetic media; its evidence is summarised in AI_Creator_Studio_Documentation/RESOLVE_INTEGRATION.md.
"""

import ast
import hashlib
import hmac
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from resolve_executor import (AddMarker, AddSubtitle, AddTrack, AddTransition, AppendSegment, CreateProject,
                              CreateTimeline, DeleteItems, Executor, FailureClass, FrameRange, ImportMedia,
                              InspectProject, InspectTimeline, ItemRef, MemoryJournal, Outcome, PlaceTitle, Policy,
                              RenderSpec, RenderTimeline, SaveProject, SetClipAudioLevel, SetTransform, TimebaseError,
                              VerifyExport, parse_rate, validate)
from resolve_executor import bridge as bridge_mod
from resolve_executor import capabilities as caps
from resolve_executor import probe as probe_mod
from resolve_executor import render as render_mod
from resolve_executor import timebase as tb
from resolve_executor.errors import AdapterError
from resolve_executor.fake import FakeResolve, fake_probe
from resolve_executor.journal import Journal
from resolve_executor.operations import ALL_OPERATIONS

PKG = Path(__file__).resolve().parents[1] / "resolve_executor"


# ── timebase ────────────────────────────────────────────────────────────────


class TestFrameRate(unittest.TestCase):
    def test_exact_rates(self):
        self.assertEqual(parse_rate(25).value, Fraction(25))
        self.assertEqual(parse_rate("25").value, Fraction(25))
        self.assertEqual(parse_rate("29.97").value, Fraction(30000, 1001))
        self.assertEqual(parse_rate("23.976").value, Fraction(24000, 1001))
        self.assertEqual(parse_rate("59.94").value, Fraction(60000, 1001))
        self.assertEqual(parse_rate("30000/1001").value, Fraction(30000, 1001))
        self.assertEqual(parse_rate("29.970029").value, Fraction(30000, 1001))   # ffprobe-style rounding snaps
        self.assertEqual(parse_rate("12.5").value, Fraction(25, 2))

    def test_floats_bools_and_garbage_refused(self):
        for bad in (25.0, 29.97, True, "", "abc", "25fps", "0", "1/0", "-25", 0, Fraction(0), 2000):
            with self.assertRaises(TimebaseError, msg=repr(bad)):
                parse_rate(bad)

    def test_resolve_text_round_trip(self):
        for text in ("23.976", "24", "25", "29.97", "30", "50", "59.94", "60"):
            self.assertEqual(parse_rate(text).resolve_text(), text)
        self.assertEqual(parse_rate("30000/1001").resolve_text(), "29.97")
        self.assertEqual(parse_rate("29.97").nominal, 30)


class TestFrameRange(unittest.TestCase):
    def test_first_frame(self):
        r = FrameRange(0, 1)
        self.assertEqual((r.length, r.last), (1, 0))
        self.assertTrue(r.contains(0))
        self.assertFalse(r.contains(1))

    def test_last_frame_of_a_100_frame_file(self):
        r = FrameRange(99, 100)
        self.assertTrue(r.within(100))
        self.assertFalse(FrameRange(99, 101).within(100))
        self.assertEqual(r.length, 1)

    def test_one_frame_and_hundred_frame_ranges(self):
        self.assertEqual(FrameRange.from_length(42, 1), FrameRange(42, 43))
        r = FrameRange(0, 100)
        self.assertEqual((r.length, r.last), (100, 99))
        self.assertEqual(FrameRange.from_inclusive(0, 99), r)       # a human's In 0 / Out 99 is 100 frames
        self.assertTrue(r.within(100))

    def test_invalid_ranges(self):
        for args in ((5, 5), (6, 5), (-1, 3), (0, 0)):
            with self.assertRaises(TimebaseError, msg=args):
                FrameRange(*args)
        for args in ((0.0, 1), (0, 1.5), (True, 2), ("0", 1)):
            with self.assertRaises(TimebaseError, msg=args):
                FrameRange(*args)
        with self.assertRaises(TimebaseError):
            FrameRange.from_length(0, 0)
        with self.assertRaises(TimebaseError):
            FrameRange.from_inclusive(5, 4)

    def test_overlap_is_half_open(self):
        self.assertFalse(FrameRange(0, 10).overlaps(FrameRange(10, 20)))   # abutting is not overlapping
        self.assertTrue(FrameRange(0, 10).overlaps(FrameRange(9, 20)))
        self.assertEqual(FrameRange(0, 10).shifted(90000), FrameRange(90000, 90010))


class TestRateConversion(unittest.TestCase):
    def test_same_rate_is_identity(self):
        for rate in ("23.976", "25", "29.97", "60"):
            self.assertEqual(tb.timeline_duration_for_source(100, rate, rate), 100)
            self.assertEqual(tb.timeline_duration_for_source(1, rate, rate), 1)

    def test_mixed_rates_floor_exactly(self):
        cases = [(100, "50", "25", 50), (100, "25", "50", 200), (100, "29.97", "25", 83), (100, "24", "25", 104),
                 (100, "23.976", "29.97", 125), (1, "50", "25", 0), (2, "50", "25", 1), (100, "30", "29.97", 99),
                 (1001, "30", "29.97", 1000)]
        for n, src, tl, want in cases:
            self.assertEqual(tb.timeline_duration_for_source(n, src, tl), want, (n, src, tl))

    def test_source_frames_for_duration_is_minimal_and_sufficient(self):
        rates = ("23.976", "24", "25", "29.97", "30", "50", "59.94", "60")
        for src in rates:
            for tl in rates:
                for t in (1, 2, 24, 25, 83, 95, 100, 1001):
                    n = tb.source_frames_for_duration(t, src, tl)
                    self.assertGreaterEqual(tb.timeline_duration_for_source(n, src, tl), t, (t, src, tl))
                    if n > 1:
                        self.assertLess(tb.timeline_duration_for_source(n - 1, src, tl), t, (t, src, tl))

    def test_the_95_of_100_case_follows_from_the_rule_not_from_a_constant(self):
        # The historical 95/100 observation is NOT encoded as a constant. Under the floor rule exactly one pair of
        # supported rates turns 100 source frames into 95 timeline frames: a 25 fps source in a 23.976 fps
        # timeline (100 * 23.976 / 25 = 95.9). The live suite tests that hypothesis (RESOLVE_INTEGRATION.md).
        rates = ("23.976", "24", "25", "29.97", "30", "50", "59.94", "60")
        pairs = [(s, t) for s in rates for t in rates if tb.timeline_duration_for_source(100, s, t) == 95]
        self.assertEqual(pairs, [("25", "23.976")])

    def test_seconds_and_timecode(self):
        self.assertEqual(tb.frames_to_seconds(25, 25), 1)
        self.assertEqual(tb.frames_to_seconds(1001, "29.97"), Fraction(1001 * 1001, 30000))
        self.assertEqual(tb.seconds_to_frames("4", 25), 100)
        self.assertEqual(tb.seconds_to_frames("1.04", 25), 26)
        with self.assertRaises(TimebaseError):
            tb.seconds_to_frames("0.01", 25)                  # 0.25 frame: refused, not rounded silently
        self.assertEqual(tb.seconds_to_frames("0.01", 25, "nearest"), 0)
        self.assertEqual(tb.seconds_to_frames("0.02", 25, "nearest"), 1)
        self.assertEqual(tb.seconds_to_frames("0.01", 25, "ceil"), 1)
        with self.assertRaises(TimebaseError):
            tb.seconds_to_frames(0.5, 25)
        self.assertEqual(tb.timecode_to_frames("01:00:00:00", 25), 90000)
        self.assertEqual(tb.timecode_to_frames("01:00:00:00", "29.97"), 108000)   # non-drop, nominal 30
        self.assertEqual(tb.frames_to_timecode(90000, 25), "01:00:00:00")
        self.assertEqual(tb.frames_to_timecode(90124, 25), "01:00:04:24")
        for bad in ("01:00:00;00", "1:00:00:00", "01:60:00:00", "01:00:00:25"):
            with self.assertRaises(TimebaseError, msg=bad):
                tb.timecode_to_frames(bad, 25)
        for f in (0, 1, 24, 25, 89999, 90000, 123456):
            self.assertEqual(tb.timecode_to_frames(tb.frames_to_timecode(f, 25), 25), f)


# ── fixtures ────────────────────────────────────────────────────────────────


class Rig:
    """A temp media root and output root with a FakeResolve and an Executor over it."""

    def __init__(self, testcase, media=None, **ex_kw):
        self.tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.media_dir = os.path.join(self.root, "media")
        self.out = os.path.join(self.root, "out")
        os.makedirs(self.media_dir)
        os.makedirs(self.out)
        media = media or {"a.mov": {"frames": 250, "fps": "25"}}
        self.paths = {}
        files = {}
        for name, spec in media.items():
            p = os.path.join(self.media_dir, name)
            open(p, "w").close()
            self.paths[name] = p
            files[p] = spec
        self.fake = FakeResolve(media_files=files)
        self.clock = [0.0]
        self.journal = MemoryJournal()
        kw = dict(prober=fake_probe, sleep=self._sleep, clock=lambda: self.clock[0], journal=self.journal)
        kw.update(ex_kw)
        self.ex = Executor(self.fake, Policy((self.media_dir,), (self.out,), forbidden_media_names=("MVI_0879.MOV",)), **kw)

    def _sleep(self, s):
        self.clock[0] += s

    def setup_timeline(self, rate="25", import_all=True, name="Main"):
        assert self.ex.execute(CreateProject("p", name="T-RSV-002 test")).ok
        if import_all:
            assert self.ex.execute(ImportMedia("i", paths=tuple(self.paths.values()))).ok
        r = self.ex.execute(CreateTimeline("t", name=name, frame_rate=rate, width=1920, height=1080))
        assert r.ok, r.to_dict()
        return r

    def items(self, kind="video", idx=1, tl="Main"):
        return self.fake.track_items(tl, kind, idx)


# ── validation ──────────────────────────────────────────────────────────────


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(self)
        self.pol = self.rig.ex.policy
        self.med = self.rig.paths["a.mov"]

    def bad(self, op, fragment):
        problems = validate(op, self.pol)
        self.assertTrue(any(fragment in p for p in problems), (fragment, problems))

    def test_valid_requests_pass(self):
        ops = [InspectProject("x"), CreateProject("x", name="Disposable 1"), ImportMedia("x", paths=(self.med,)),
               CreateTimeline("x", name="Main", frame_rate="29.97", width=1080, height=1920),
               AddTrack("x", timeline="Main", track_kind="audio", audio_format="mono"),
               AppendSegment("x", timeline="Main", media_path=self.med, source=FrameRange(0, 1)),
               SetTransform("x", timeline="Main", item=ItemRef("video", 1, 90000), values={"ZoomX": 1.5, "FlipX": True}),
               AddMarker("x", timeline="Main", frame=0), PlaceTitle("x", timeline="Main", text="Hi", record_frame=90000,
                                                                    duration=25, center=(0.5, 0.2)),
               DeleteItems("x", timeline="Main", items=(ItemRef("video", 1, 90000),)), SaveProject("x"),
               RenderTimeline("x", timeline="Main", spec=RenderSpec(self.rig.out, "out_1", width=1080, height=1920))]
        for op in ops:
            self.assertEqual(validate(op, self.pol), [], op)

    def test_identifiers(self):
        self.bad(InspectProject(""), "op_id")
        self.bad(InspectProject("a b"), "op_id")
        self.bad(SaveProject("x", idempotency_key="bad key"), "idempotency_key")
        self.bad(CreateProject("x", name="../evil"), "project name")
        self.bad(AddMarker("x", timeline=""), "timeline")

    def test_media_paths_are_allowlisted_and_must_exist(self):
        self.bad(ImportMedia("x", paths=("relative.mov",)), "absolute")
        outside = os.path.join(os.path.dirname(self.rig.media_dir), "media2", "a.mov")
        self.bad(ImportMedia("x", paths=(outside,)), "outside the allowed media roots")
        dotdot = os.path.join(self.rig.media_dir, "..", "out", "x.mov")
        self.bad(ImportMedia("x", paths=(dotdot,)), "outside the allowed media roots")
        self.bad(ImportMedia("x", paths=(os.path.join(self.rig.media_dir, "nope.mov"),)), "does not exist")
        self.bad(ImportMedia("x", paths=(self.med, self.med)), "duplicates")
        self.bad(ImportMedia("x", paths=()), "non-empty")
        forbidden = os.path.join(self.rig.media_dir, "mvi_0879.mov")
        open(forbidden, "w").close()
        self.bad(ImportMedia("x", paths=(forbidden,)), "forbidden")
        self.bad(ImportMedia("x", paths=(self.med,)) if False else AppendSegment(
            "x", timeline="Main", media_path=forbidden, source=FrameRange(0, 1)), "forbidden")

    def test_no_roots_configured_fails_closed(self):
        pol = Policy()
        self.assertTrue(any("no allowed media roots" in p for p in validate(ImportMedia("x", paths=(self.med,)), pol)))
        spec = RenderSpec(self.rig.out, "o")
        self.assertTrue(any("no allowed output roots" in p for p in validate(RenderTimeline("x", timeline="M", spec=spec), pol)))

    def test_timeline_and_tracks(self):
        self.bad(CreateTimeline("x", name="M", frame_rate="25.5", width=1920, height=1080), "frame_rate")
        self.bad(CreateTimeline("x", name="M", frame_rate="25", width=1921, height=1080), "even")
        self.bad(CreateTimeline("x", name="M", frame_rate="25", width=1920, height=1080, start_timecode="01:00:00;00"),
                 "drop-frame")
        self.bad(AddTrack("x", timeline="M", track_kind="subtitle"), "track_kind")
        self.bad(AddTrack("x", timeline="M", track_kind="audio", audio_format="7.1"), "audio_format")

    def test_append_segment(self):
        self.bad(AppendSegment("x", timeline="M", media_path=self.med, source=None), "FrameRange")
        self.bad(AppendSegment("x", timeline="M", media_path=self.med, source=FrameRange(0, 1), track_index=0), "track_index")
        self.bad(AppendSegment("x", timeline="M", media_path=self.med, source=FrameRange(0, 1), record_frame=-1), "record_frame")
        self.bad(AppendSegment("x", timeline="M", media_path=self.med, source=FrameRange(0, 1), media="both"), "media")
        self.bad(AppendSegment("x", timeline="M", media_path=self.med, source=FrameRange(0, 1), media="video_audio"),
                 "audio_track_index")
        self.bad(AppendSegment("x", timeline="M", media_path=self.med, source=FrameRange(0, 1), audio_track_index=2),
                 "only used")

    def test_transforms(self):
        ref = ItemRef("video", 1, 90000)
        self.bad(SetTransform("x", timeline="M", item=ref, values={}), "empty")
        self.bad(SetTransform("x", timeline="M", item=ref, values={"Volume": 1}), "not a supported transform key")
        self.bad(SetTransform("x", timeline="M", item=ref, values={"ZoomX": 101}), "outside")
        self.bad(SetTransform("x", timeline="M", item=ref, values={"ZoomX": float("nan")}), "finite")
        self.bad(SetTransform("x", timeline="M", item=ref, values={"ZoomX": True}), "finite")
        self.bad(SetTransform("x", timeline="M", item=ref, values={"FlipX": 1}), "bool")
        self.bad(SetTransform("x", timeline="M", item=ItemRef("audio", 1, 90000), values={"ZoomX": 1}), "video items")

    def test_markers_titles(self):
        self.bad(AddMarker("x", timeline="M", color="Orange"), "color")
        self.bad(AddMarker("x", timeline="M", duration=0), "duration")
        self.bad(AddMarker("x", timeline="M", note="n" * 501), "note")
        self.bad(PlaceTitle("x", timeline="M", text=" ", record_frame=0, duration=1), "text")
        self.bad(PlaceTitle("x", timeline="M", text="a", record_frame=0, duration=0), "duration")
        self.bad(PlaceTitle("x", timeline="M", text="a", record_frame=0, duration=1, center=(2, 0)), "center")
        self.bad(PlaceTitle("x", timeline="M", text="a", record_frame=0, duration=1, size=0), "size")

    def test_render_spec(self):
        def r(**kw):
            base = dict(target_dir=self.rig.out, file_name="o")
            base.update(kw)
            return RenderTimeline("x", timeline="M", spec=RenderSpec(**base))
        self.bad(r(format="avi"), "format")
        self.bad(r(codec="H.264"), "codec id")
        self.bad(r(width=1081), "even")
        self.bad(r(file_name="a/b"), "file_name")
        self.bad(r(file_name="..x"), "file_name")
        self.bad(r(target_dir=self.rig.media_dir), "outside the allowed output roots")
        self.bad(r(target_dir=os.path.join(self.rig.out, "missing")), "does not exist")
        self.bad(r(export_video=False, export_audio=False), "video, audio or both")
        self.bad(r(mark_in=10), "together")
        self.bad(r(mark_in=10, mark_out=10), "greater")
        self.bad(r(timeout_seconds=1), "timeout_seconds")

    def test_validation_layer_never_imports_an_adapter(self):
        tree = ast.parse((PKG / "validation.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        for forbidden in ("adapter", "live", "bridge", "executor", "fake", "render", "socket", "subprocess"):
            self.assertNotIn(forbidden, imported)


# ── executor: placement and frame accuracy ──────────────────────────────────


class TestAppendSegment(unittest.TestCase):
    def test_hundred_frames_land_as_hundred_frames_at_the_timeline_start(self):
        rig = Rig(self)
        rig.setup_timeline()
        r = rig.ex.execute(AppendSegment("a", timeline="Main", media_path=rig.paths["a.mov"], source=FrameRange(0, 100)))
        self.assertEqual(r.outcome, Outcome.SUCCEEDED, r.to_dict())
        (it,) = rig.items()
        self.assertEqual((it["start"], it["end"], it["duration"], it["source_start"], it["source_end"]),
                         (90000, 90100, 100, 0, 100))

    def test_one_frame_first_and_last_frames(self):
        rig = Rig(self)
        rig.setup_timeline()
        p = rig.paths["a.mov"]
        self.assertTrue(rig.ex.execute(AppendSegment("f", timeline="Main", media_path=p, source=FrameRange(0, 1))).ok)
        self.assertTrue(rig.ex.execute(AppendSegment("l", timeline="Main", media_path=p, source=FrameRange(249, 250))).ok)
        r = rig.ex.execute(AppendSegment("x", timeline="Main", media_path=p, source=FrameRange(249, 251)))
        self.assertEqual((r.outcome, r.failure), (Outcome.REFUSED, FailureClass.PRECONDITION))
        self.assertIn("last 249", r.message)
        self.assertEqual([(i["start"], i["end"]) for i in rig.items()], [(90000, 90001), (90001, 90002)])

    def test_consecutive_appends_are_gap_free(self):
        rig = Rig(self)
        rig.setup_timeline()
        p = rig.paths["a.mov"]
        for n, (s, e) in enumerate([(0, 10), (10, 35), (100, 101), (200, 250)]):
            self.assertTrue(rig.ex.execute(AppendSegment(f"s{n}", timeline="Main", media_path=p, source=FrameRange(s, e))).ok)
        spans = [(i["start"], i["end"]) for i in rig.items()]
        self.assertEqual(spans, [(90000, 90010), (90010, 90035), (90035, 90036), (90036, 90086)])

    def test_explicit_record_frame_and_non_zero_start(self):
        rig = Rig(self)
        assert rig.ex.execute(CreateProject("p", name="P")).ok
        assert rig.ex.execute(ImportMedia("i", paths=(rig.paths["a.mov"],))).ok
        assert rig.ex.execute(CreateTimeline("t", name="Main", frame_rate="25", width=1920, height=1080,
                                             start_timecode="00:00:10:00")).ok
        self.assertEqual(rig.fake.timeline_info("Main")["start_frame"], 250)
        r = rig.ex.execute(AppendSegment("a", timeline="Main", media_path=rig.paths["a.mov"], source=FrameRange(5, 30),
                                         record_frame=300))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual((rig.items()[0]["start"], rig.items()[0]["end"]), (300, 325))
        r = rig.ex.execute(AppendSegment("b", timeline="Main", media_path=rig.paths["a.mov"], source=FrameRange(0, 5),
                                         record_frame=249))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)
        self.assertIn("before the timeline start", r.message)

    def test_mixed_rates_use_the_floor_rule(self):
        rig = Rig(self, media={"fifty.mov": {"frames": 500, "fps": "50"}, "ntsc.mov": {"frames": 300, "fps": "29.97"}})
        rig.setup_timeline()
        r = rig.ex.execute(AppendSegment("a", timeline="Main", media_path=rig.paths["fifty.mov"], source=FrameRange(0, 100)))
        self.assertTrue(r.ok)
        self.assertEqual(r.evidence["expect"]["duration"], 50)
        r = rig.ex.execute(AppendSegment("b", timeline="Main", media_path=rig.paths["ntsc.mov"], source=FrameRange(0, 100)))
        self.assertTrue(r.ok)
        self.assertEqual([i["duration"] for i in rig.items()], [50, 83])
        r = rig.ex.execute(AppendSegment("c", timeline="Main", media_path=rig.paths["fifty.mov"], source=FrameRange(0, 1)))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)          # 1 frame at 50 fps is < 1 frame at 25
        self.assertIn("shorter than one timeline frame", r.message)

    def test_overlap_is_refused_before_anything_is_sent(self):
        rig = Rig(self)
        rig.setup_timeline()
        p = rig.paths["a.mov"]
        assert rig.ex.execute(AppendSegment("a", timeline="Main", media_path=p, source=FrameRange(0, 50))).ok
        rig.fake.calls.clear()
        r = rig.ex.execute(AppendSegment("b", timeline="Main", media_path=p, source=FrameRange(0, 10), record_frame=90049))
        self.assertEqual((r.outcome, r.failure), (Outcome.REFUSED, FailureClass.PRECONDITION))
        self.assertNotIn("append", rig.fake.calls)
        r = rig.ex.execute(AppendSegment("c", timeline="Main", media_path=p, source=FrameRange(0, 10), record_frame=90050))
        self.assertTrue(r.ok)                                           # abutting is fine

    def test_video_audio_and_multitrack(self):
        rig = Rig(self)
        rig.setup_timeline()
        p = rig.paths["a.mov"]
        for i, kind in enumerate(("video", "audio", "audio")):
            assert rig.ex.execute(AddTrack(f"tr{i}", timeline="Main", track_kind=kind)).ok
        self.assertEqual(rig.fake.timeline_info("Main")["track_counts"], {"video": 2, "audio": 3, "subtitle": 0})
        r = rig.ex.execute(AppendSegment("av", timeline="Main", media_path=p, source=FrameRange(0, 50),
                                         media="video_audio", audio_track_index=2))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(sorted(r.evidence["placed"]), ["audio2", "video1"])
        self.assertEqual(rig.items("audio", 2)[0]["start"], rig.items("video", 1)[0]["start"])   # in sync
        r = rig.ex.execute(AppendSegment("v2", timeline="Main", media_path=p, source=FrameRange(10, 20), track_index=2,
                                         record_frame=90020))
        self.assertTrue(r.ok)
        r = rig.ex.execute(AppendSegment("music", timeline="Main", media_path=p, source=FrameRange(0, 100), media="audio",
                                         track_index=3, record_frame=90010))
        self.assertTrue(r.ok)
        self.assertEqual(rig.items("audio", 3)[0]["start"], 90010)
        r = rig.ex.execute(AppendSegment("v9", timeline="Main", media_path=p, source=FrameRange(0, 1), track_index=9))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)
        self.assertIn("does not exist", r.message)

    def test_media_parts_must_exist(self):
        rig = Rig(self, media={"silent.mov": {"frames": 50, "fps": "25", "audio": False},
                               "tone.wav": {"frames": 240, "fps": "24", "video": False}})
        rig.setup_timeline()
        rig.fake.calls.clear()
        r = rig.ex.execute(AppendSegment("a", timeline="Main", media_path=rig.paths["silent.mov"], source=FrameRange(0, 10),
                                         media="video_audio", audio_track_index=1))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)
        self.assertIn("no audio part", r.message)
        r = rig.ex.execute(AppendSegment("b", timeline="Main", media_path=rig.paths["tone.wav"], source=FrameRange(0, 10)))
        self.assertIn("no video part", r.message)
        self.assertNotIn("append", rig.fake.calls)
        r = rig.ex.execute(AppendSegment("c", timeline="Main", media_path=rig.paths["tone.wav"], source=FrameRange(0, 192),
                                         media="audio"))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(rig.items("audio", 1)[0]["duration"], 200)    # 192 frames at 24 fps in a 25 fps timeline

    def test_media_must_be_imported_and_unambiguous(self):
        rig = Rig(self)
        rig.setup_timeline(import_all=False)
        r = rig.ex.execute(AppendSegment("a", timeline="Main", media_path=rig.paths["a.mov"], source=FrameRange(0, 1)))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)
        self.assertIn("import it first", r.message)

    def test_wrong_project_is_never_touched(self):
        rig = Rig(self)
        rig.setup_timeline()
        rig.fake.current = "Untitled Project"
        rig.fake.calls.clear()
        r = rig.ex.execute(AddTrack("x", timeline="Main"))
        self.assertEqual((r.outcome, r.failure), (Outcome.REFUSED, FailureClass.IDENTITY_MISMATCH))
        self.assertNotIn("add_track", rig.fake.calls)
        ex2 = Executor(rig.fake, rig.ex.policy, prober=fake_probe)        # no expected project at all
        r = ex2.execute(AddTrack("x", timeline="Main"))
        self.assertEqual(r.failure, FailureClass.IDENTITY_MISMATCH)

    def test_existing_items_changing_is_a_mismatch(self):
        rig = Rig(self)
        rig.setup_timeline()
        p = rig.paths["a.mov"]
        assert rig.ex.execute(AppendSegment("a", timeline="Main", media_path=p, source=FrameRange(0, 10))).ok
        real_append = rig.fake.append

        def append_and_clobber(name, infos):
            rig.fake._tl(name).items["video1"][0]["end"] = 90005     # something else moved
            return real_append(name, infos)
        rig.fake.append = append_and_clobber
        r = rig.ex.execute(AppendSegment("b", timeline="Main", media_path=p, source=FrameRange(0, 10)))
        self.assertEqual(r.failure, FailureClass.READBACK_MISMATCH)


class TestCreateTimelineAndOthers(unittest.TestCase):
    def test_timeline_settings_verified(self):
        rig = Rig(self)
        r = rig.setup_timeline(rate="29.97")
        self.assertEqual(r.evidence["timeline"]["frame_rate"], "29.97")
        self.assertEqual(r.evidence["timeline"]["start_frame"], 108000)
        r = rig.ex.execute(CreateTimeline("t2", name="Main", frame_rate="25", width=1920, height=1080))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)
        rig.fake.faults["set_timeline_setting"] = ["ok", "false"]      # rate rejected
        r = rig.ex.execute(CreateTimeline("t3", name="Other", frame_rate="50", width=1920, height=1080))
        self.assertEqual(r.failure, FailureClass.READBACK_MISMATCH)
        self.assertIn("rate", r.message)

    def test_vertical_timeline(self):
        rig = Rig(self)
        assert rig.ex.execute(CreateProject("p", name="P")).ok
        r = rig.ex.execute(CreateTimeline("v", name="Vertical", frame_rate="30", width=1080, height=1920))
        self.assertTrue(r.ok)
        self.assertEqual((r.evidence["timeline"]["width"], r.evidence["timeline"]["height"]), (1080, 1920))

    def test_create_project_refuses_existing_names(self):
        rig = Rig(self)
        r = rig.ex.execute(CreateProject("p", name="Untitled Project"))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)
        r = rig.ex.execute(CreateProject("p2", name="Fresh"))
        self.assertTrue(r.ok)
        self.assertEqual(rig.ex.expected_project, "Fresh")

    def test_import_is_idempotent_per_path(self):
        rig = Rig(self, media={"a.mov": {"frames": 10, "fps": "25"}, "b.wav": {"frames": 10, "fps": "25", "video": False}})
        assert rig.ex.execute(CreateProject("p", name="P")).ok
        assert rig.ex.execute(ImportMedia("i1", paths=(rig.paths["a.mov"],))).ok
        rig.fake.calls.clear()
        r = rig.ex.execute(ImportMedia("i2", paths=(rig.paths["a.mov"], rig.paths["b.wav"])))
        self.assertTrue(r.ok)
        self.assertEqual(r.evidence["already_present"], [rig.paths["a.mov"]])
        self.assertEqual(len(rig.fake.media_items()), 2)
        rig.fake.faults["import_media"] = ["ignore"]
        rig2 = Rig(self)
        assert rig2.ex.execute(CreateProject("p", name="P")).ok
        rig2.fake.faults["import_media"] = ["ignore"]
        r = rig2.ex.execute(ImportMedia("i", paths=(rig2.paths["a.mov"],)))
        self.assertEqual((r.outcome, r.failure), (Outcome.FAILED, FailureClass.RESOLVE_REFUSED))

    def test_transform_readback(self):
        rig = Rig(self)
        rig.setup_timeline()
        assert rig.ex.execute(AppendSegment("a", timeline="Main", media_path=rig.paths["a.mov"], source=FrameRange(0, 10))).ok
        ref = ItemRef("video", 1, 90000)
        r = rig.ex.execute(SetTransform("s", timeline="Main", item=ref,
                                        values={"ZoomX": 1.5, "ZoomY": 1.5, "Pan": 120.0, "CropLeft": 40.0, "FlipX": True}))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(r.evidence["after"]["ZoomX"], 1.5)
        rig.fake.sets_ignored = {"Tilt"}                                # Resolve says True but nothing changes
        r = rig.ex.execute(SetTransform("s2", timeline="Main", item=ref, values={"Tilt": 10.0}))
        self.assertEqual((r.outcome, r.failure), (Outcome.FAILED, FailureClass.RESOLVE_REFUSED))
        r = rig.ex.execute(SetTransform("s3", timeline="Main", item=ref, values={"Tilt": 10.0, "ZoomX": 2.0}))
        self.assertEqual(r.failure, FailureClass.READBACK_MISMATCH)
        self.assertIn("Tilt", r.message)
        r = rig.ex.execute(SetTransform("s4", timeline="Main", item=ItemRef("video", 1, 90001), values={"ZoomX": 1.0}))
        self.assertEqual(r.outcome, Outcome.REFUSED)                   # no item there

    def test_markers(self):
        rig = Rig(self)
        rig.setup_timeline()
        assert rig.ex.execute(AppendSegment("a", timeline="Main", media_path=rig.paths["a.mov"], source=FrameRange(0, 100))).ok
        r = rig.ex.execute(AddMarker("m", timeline="Main", frame=99, color="Green", name="end", note="n", duration=1))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(rig.ex.execute(AddMarker("m2", timeline="Main", frame=99)).failure, FailureClass.PRECONDITION)
        self.assertEqual(rig.ex.execute(AddMarker("m3", timeline="Main", frame=100)).failure, FailureClass.PRECONDITION)

    def test_titles(self):
        rig = Rig(self)
        rig.setup_timeline()
        assert rig.ex.execute(AddTrack("v2", timeline="Main", track_kind="video")).ok
        r = rig.ex.execute(PlaceTitle("t1", timeline="Main", text="Lower third", track_index=2, record_frame=90010,
                                      duration=50, size=0.08, center=(0.5, 0.15)))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(r.evidence["text"], "Lower third")
        r = rig.ex.execute(PlaceTitle("t2", timeline="Main", text="Too long", track_index=2, record_frame=90100,
                                      duration=126))
        self.assertEqual(r.failure, FailureClass.READBACK_MISMATCH)        # default Text+ length is 125 at 25 fps
        self.assertIn("only 125 frames", r.message)
        r = rig.ex.execute(PlaceTitle("t3", timeline="Main", text="Overlap", track_index=2, record_frame=90059, duration=5))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)

    def test_delete_items_checkpoints_first_and_spares_others(self):
        rig = Rig(self)
        rig.setup_timeline()
        p = rig.paths["a.mov"]
        for n in range(3):
            assert rig.ex.execute(AppendSegment(f"a{n}", timeline="Main", media_path=p, source=FrameRange(0, 10))).ok
        r = rig.ex.execute(DeleteItems("d", timeline="Main", items=(ItemRef("video", 1, 90010),)))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual([i["start"] for i in rig.items()], [90000, 90020])
        cps = [rec for rec in rig.journal.records() if rec["event"] == "checkpoint"]
        self.assertEqual(len(cps), 1)
        self.assertIn("before_d", cps[0]["label"])
        snap = rig.journal.checkpoints[cps[0]["file"]]
        self.assertEqual(len(snap["tracks"]["video1"]), 3)               # pre-state preserved
        r = rig.ex.execute(DeleteItems("d2", timeline="Main", items=(ItemRef("video", 1, 90010),)))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)

    def test_inspect(self):
        rig = Rig(self)
        rig.setup_timeline()
        r = rig.ex.execute(InspectProject("i"))
        self.assertTrue(r.ok)
        self.assertEqual(r.evidence["project"], "T-RSV-002 test")
        r = rig.ex.execute(InspectTimeline("t", timeline="Main"))
        self.assertTrue(r.ok)
        self.assertEqual(r.evidence["timeline"]["start_frame"], 90000)
        self.assertEqual(rig.ex.execute(InspectTimeline("t", timeline="Nope")).failure, FailureClass.PRECONDITION)


# ── capability gate ─────────────────────────────────────────────────────────


class TestCapabilities(unittest.TestCase):
    def test_every_request_kind_has_a_capability(self):
        for cls in ALL_OPERATIONS:
            cap = caps.capability_for(cls.KIND)
            self.assertIn(cap.status, (caps.VERIFIED, caps.IMPLEMENTED, caps.NOT_TESTED, caps.UNSUPPORTED))

    def test_statuses_are_honest(self):
        for c in caps.CAPABILITIES:
            self.assertIn(c.status, (caps.VERIFIED, caps.IMPLEMENTED, caps.NOT_TESTED, caps.UNSUPPORTED), c.name)
            self.assertTrue(c.api and c.limitations and c.verification, c.name)
            if c.status == caps.VERIFIED:
                self.assertTrue(c.evidence.startswith("E-RSV-"), c.name)
            else:
                self.assertEqual(c.evidence, "", c.name)

    def test_unsupported_operations_are_refused_without_any_call(self):
        rig = Rig(self)
        rig.setup_timeline()
        rig.fake.calls.clear()
        ref = ItemRef("audio", 1, 90000)
        for op in (SetClipAudioLevel("v", timeline="Main", item=ref, volume_db=-6.0),
                   AddTransition("x", timeline="Main", item=ItemRef("video", 1, 90000), duration=12),
                   AddSubtitle("s", timeline="Main", text="hi", record_frame=90000, duration=10)):
            r = rig.ex.execute(op)
            self.assertEqual((r.outcome, r.failure), (Outcome.REFUSED, FailureClass.UNSUPPORTED), op)
        self.assertEqual(rig.fake.calls, [])

    def test_not_tested_capabilities_are_refused(self):
        rig = Rig(self)
        rig.setup_timeline()
        cap = caps.CAPABILITY_BY_NAME["add_marker"]
        patched = caps.Capability(**{**cap.to_dict(), "status": caps.NOT_TESTED})
        with mock.patch.dict(caps.CAPABILITY_BY_NAME, {"add_marker": patched}):
            r = rig.ex.execute(AddMarker("m", timeline="Main", frame=0))
        self.assertEqual(r.failure, FailureClass.UNSUPPORTED)

    def test_mcp_inventory_is_complete(self):
        self.assertEqual(len(caps.MCP_TOOLS), 37)


# ── recovery, idempotency, interruption ─────────────────────────────────────


class TestRecovery(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(self)
        self.rig.setup_timeline()
        self.p = self.rig.paths["a.mov"]

    def seg(self, op_id="a", key=None, **kw):
        return AppendSegment(op_id, timeline="Main", media_path=self.p, source=FrameRange(0, 10), idempotency_key=key, **kw)

    def test_applied_despite_timeout_is_recognised_by_readback(self):
        self.rig.fake.faults["append"] = ["raise_after"]
        r = self.rig.ex.execute(self.seg(key="k1"))
        self.assertEqual(r.outcome, Outcome.SUCCEEDED)
        self.assertEqual(r.evidence["call_error"]["failure"], "bridge_timeout")
        self.assertEqual(len(self.rig.items()), 1)                      # not appended twice
        self.assertEqual(self.rig.journal.state_of("k1")["state"], "done")

    def test_not_applied_timeout_is_retried_once_then_reported(self):
        self.rig.fake.faults["append"] = ["raise_before"]
        r = self.rig.ex.execute(self.seg(key="k2"))
        self.assertEqual((r.outcome, r.attempts), (Outcome.SUCCEEDED, 2))
        self.assertEqual(len(self.rig.items()), 1)
        self.rig.fake.faults["append"] = ["raise_before", "raise_before"]
        r = self.rig.ex.execute(self.seg("b", key="k3"))
        self.assertEqual((r.outcome, r.failure, r.attempts), (Outcome.FAILED, FailureClass.BRIDGE_TIMEOUT, 2))
        self.assertEqual(len(self.rig.items()), 1)

    def test_unknown_outcome_is_never_repeated(self):
        # preflight reads the track once (ok), the post-mutation readback cannot reach the bridge
        self.rig.fake.faults["append"] = ["raise_after"]
        self.rig.fake.faults["track_items"] = ["ok", "unavailable"]
        r = self.rig.ex.execute(self.seg(key="k4"))
        self.assertEqual((r.outcome, r.failure), (Outcome.UNCERTAIN, FailureClass.INTERRUPTED))
        self.assertEqual(self.rig.journal.state_of("k4")["state"], "uncertain")
        self.assertEqual(self.rig.journal.uncertain_keys(), ["k4"])
        self.rig.fake.calls.clear()
        r = self.rig.ex.execute(self.seg("a-again", key="k4"))
        self.assertEqual((r.outcome, r.failure), (Outcome.REFUSED, FailureClass.UNCERTAIN_PREVIOUS_ATTEMPT))
        self.assertEqual(self.rig.fake.calls, [])
        with self.assertRaises(ValueError):
            self.rig.ex.resolve_uncertain("k4", True, " ")
        self.rig.ex.resolve_uncertain("k4", True, "item found at 90000 on V1 by InspectTimeline")
        r = self.rig.ex.execute(self.seg("a-third", key="k4"))
        self.assertEqual(r.outcome, Outcome.ALREADY_DONE)
        self.assertEqual(len(self.rig.items()), 1)

    def test_resolved_as_not_applied_allows_a_new_attempt(self):
        self.rig.fake.faults["append"] = ["raise_before"]
        self.rig.fake.faults["track_items"] = ["ok", "unavailable"]
        r = self.rig.ex.execute(self.seg(key="k5"))
        self.assertEqual(r.outcome, Outcome.UNCERTAIN)
        self.rig.ex.resolve_uncertain("k5", False, "V1 empty in InspectTimeline")
        r = self.rig.ex.execute(self.seg("again", key="k5"))
        self.assertEqual(r.outcome, Outcome.SUCCEEDED)

    def test_bridge_unavailable_sends_nothing(self):
        self.rig.fake.faults["append"] = ["unavailable"]
        r = self.rig.ex.execute(self.seg(key="k6"))
        self.assertEqual((r.outcome, r.failure), (Outcome.FAILED, FailureClass.BRIDGE_UNAVAILABLE))
        self.assertEqual(self.rig.journal.state_of("k6")["state"], "failed")
        self.rig.fake.faults["current_project"] = ["unavailable", "unavailable", "unavailable"]
        r = self.rig.ex.execute(self.seg("z", key="k7"))
        self.assertEqual((r.outcome, r.failure), (Outcome.REFUSED, FailureClass.BRIDGE_UNAVAILABLE))

    def test_reads_retry_transient_errors(self):
        self.rig.fake.faults["timeline_info"] = ["raise_before", "raise_before"]
        r = self.rig.ex.execute(InspectTimeline("i", timeline="Main"))
        self.assertTrue(r.ok)
        self.rig.fake.faults["timeline_info"] = ["raise_before"] * 3
        r = self.rig.ex.execute(InspectTimeline("i", timeline="Main"))
        self.assertEqual(r.failure, FailureClass.BRIDGE_TIMEOUT)

    def test_false_from_resolve_is_a_known_failure(self):
        self.rig.fake.faults["append"] = ["false"]
        r = self.rig.ex.execute(self.seg(key="k8"))
        self.assertEqual((r.outcome, r.failure), (Outcome.FAILED, FailureClass.RESOLVE_REFUSED))
        self.assertEqual(r.recovery.split(".")[0], "Resolve rejected the call")

    def test_completed_keys_are_not_repeated(self):
        self.assertTrue(self.rig.ex.execute(self.seg(key="k9")).ok)
        self.rig.fake.calls.clear()
        r = self.rig.ex.execute(self.seg("b", key="k9"))
        self.assertEqual(r.outcome, Outcome.ALREADY_DONE)
        self.assertEqual(self.rig.fake.calls, [])

    def test_disk_journal_records_begin_before_mutation_and_reports_torn_lines(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            seen = []
            real = self.rig.fake.append

            def spy(name, infos):
                seen.append([r["event"] for r in j.records()])
                return real(name, infos)
            self.rig.fake.append = spy
            ex = Executor(self.rig.fake, self.rig.ex.policy, j, expected_project="T-RSV-002 test", prober=fake_probe)
            r = ex.execute(self.seg("disk", key="kd", record_frame=90100))
            self.assertTrue(r.ok, r.to_dict())
            self.assertEqual(seen, [["begin"]])
            events = [r["event"] for r in j.records()]
            self.assertEqual(events, ["begin", "finish"])
            with open(j.path, "a", encoding="utf-8") as fh:
                fh.write('{"torn": ')
            self.assertEqual(len(Journal(d).corrupt_lines()), 1)
            ex.checkpoint("cp", "Main")
            self.assertTrue(any(f.endswith("_cp.json") for f in os.listdir(os.path.join(d, "checkpoints"))))

    def test_run_plan_stops_at_the_first_failure(self):
        plan = [self.seg("a"), AddMarker("m", timeline="Main", frame=500), AddMarker("m2", timeline="Main", frame=1)]
        results = self.rig.ex.run_plan(plan)
        self.assertEqual([r.outcome for r in results], [Outcome.SUCCEEDED, Outcome.REFUSED])


# ── render ──────────────────────────────────────────────────────────────────


class TestRender(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(self)
        self.rig.setup_timeline()
        assert self.rig.ex.execute(AppendSegment("a", timeline="Main", media_path=self.rig.paths["a.mov"],
                                                 source=FrameRange(0, 100), media="video_audio", audio_track_index=1)).ok

    def render(self, name="out", **kw):
        spec = RenderSpec(self.rig.out, name, **kw)
        return self.rig.ex.execute(RenderTimeline(f"r-{name}", timeline="Main", spec=spec))

    def test_full_render_is_verified_from_the_file(self):
        r = self.render()
        self.assertEqual(r.outcome, Outcome.SUCCEEDED, r.to_dict())
        v = r.evidence["verification"]
        self.assertTrue(v["ok"])
        self.assertEqual(v["media"]["streams"][0]["frames"], 100)
        self.assertEqual(r.evidence["page_before"], "edit")
        self.assertEqual(self.rig.fake.page, "edit")                    # page restored
        self.assertTrue(r.evidence["job_deleted"])
        sets = [s for s in r.evidence["steps"] if "settings" in s]
        self.assertTrue(all(len(s["settings"]) == 1 for s in sets))     # one key per payload

    def test_mark_range_is_half_open(self):
        r = self.render("range", mark_in=90010, mark_out=90035)
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(r.evidence["expect"]["frames"], 25)
        sent = [s["settings"] for s in r.evidence["steps"] if "settings" in s]
        self.assertIn({"MarkOut": 90034}, sent)                         # Resolve's MarkOut is inclusive
        self.assertNotIn({"SelectAllFrames": False}, sent)              # rejected by Resolve on its own
        r = self.render("past", mark_in=90050, mark_out=90101)
        self.assertEqual(r.failure, FailureClass.PRECONDITION)

    def test_vertical_and_h265(self):
        assert self.rig.ex.execute(CreateTimeline("tv", name="Vertical", frame_rate="25", width=1080, height=1920)).ok
        assert self.rig.ex.execute(AppendSegment("av", timeline="Vertical", media_path=self.rig.paths["a.mov"],
                                                 source=FrameRange(0, 25))).ok
        spec = RenderSpec(self.rig.out, "vertical", codec="H265", width=1080, height=1920)
        r = self.rig.ex.execute(RenderTimeline("rv", timeline="Vertical", spec=spec))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(r.evidence["verification"]["media"]["streams"][0]["codec"], "hevc")

    def test_never_overwrites(self):
        open(os.path.join(self.rig.out, "out.mp4"), "w").close()
        r = self.render()
        self.assertEqual((r.outcome, r.failure), (Outcome.REFUSED, FailureClass.PRECONDITION))
        self.assertIn("overwrite", r.message)

    def test_codec_not_offered(self):
        self.rig.fake.codecs["mp4"] = {"H.264": "H264"}
        r = self.render(codec="H265")
        self.assertEqual(r.failure, FailureClass.UNSUPPORTED)

    def test_render_error_timeout_and_bad_files(self):
        for behaviour, failure in (("error", FailureClass.RENDER_FAILED), ("stall", FailureClass.RENDER_TIMEOUT),
                                   ("stub", FailureClass.EXPORT_INVALID), ("audio_only", FailureClass.EXPORT_INVALID),
                                   ("wrong_size", FailureClass.EXPORT_INVALID), ("no_file", FailureClass.EXPORT_INVALID)):
            self.rig.fake.render_behaviour = behaviour
            r = self.render(f"b_{behaviour}", timeout_seconds=30)
            self.assertEqual((r.outcome, r.failure), (Outcome.FAILED, failure), (behaviour, r.message))
        self.assertFalse(self.rig.fake.is_rendering())                  # the stalled job was stopped

    def test_rejected_setting_and_start(self):
        self.rig.fake.faults["set_render_settings"] = ["ok", "false"]
        r = self.render("rej")
        self.assertEqual(r.failure, FailureClass.RESOLVE_REFUSED)
        self.assertIn("render setting rejected", r.message)
        self.rig.fake.faults["start_render"] = ["false"]
        r = self.render("nostart")
        self.assertEqual(r.failure, FailureClass.RENDER_FAILED)

    def test_bridge_loss_while_rendering_is_uncertain_and_not_repeated(self):
        self.rig.fake.faults["render_status"] = ["unavailable"]
        spec = RenderSpec(self.rig.out, "lost")
        r = self.rig.ex.execute(RenderTimeline("rl", timeline="Main", spec=spec, idempotency_key="render-lost"))
        self.assertEqual((r.outcome, r.failure), (Outcome.UNCERTAIN, FailureClass.INTERRUPTED))
        r = self.rig.ex.execute(RenderTimeline("rl2", timeline="Main", spec=spec, idempotency_key="render-lost"))
        self.assertEqual(r.failure, FailureClass.UNCERTAIN_PREVIOUS_ATTEMPT)

    def test_already_rendering_and_empty_timeline(self):
        self.rig.fake.p["rendering"] = "job-x"
        self.assertEqual(self.render("busy").failure, FailureClass.PRECONDITION)
        self.rig.fake.p["rendering"] = None
        assert self.rig.ex.execute(CreateTimeline("e", name="Empty", frame_rate="25", width=1920, height=1080)).ok
        r = self.rig.ex.execute(RenderTimeline("re", timeline="Empty", spec=RenderSpec(self.rig.out, "empty")))
        self.assertEqual(r.failure, FailureClass.PRECONDITION)

    def test_settings_payload_order(self):
        s = render_mod.render_settings(RenderSpec("C:\\x", "n", mark_in=5, mark_out=10))
        self.assertEqual(s[:2], [{"MarkIn": 5}, {"MarkOut": 9}])
        self.assertNotIn({"CustomName": ""}, s)


# ── probe ───────────────────────────────────────────────────────────────────


FFPROBE_DOC = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                            "r_frame_rate": "25/1", "nb_frames": "100", "duration": "4.000000"},
                           {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}],
               "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "4.000000"}}


class TestProbe(unittest.TestCase):
    def test_parse_and_verify(self):
        info = probe_mod.parse_ffprobe_json("x.mp4", 5000, FFPROBE_DOC)
        good = {"video_codec": "H264", "width": 1920, "height": 1080, "frame_rate": "25", "frames": 100, "audio": True}
        self.assertEqual(probe_mod.verify_media(info, good), [])
        self.assertEqual(probe_mod.duration_frames(info, "25"), 100)
        for change, frag in (({"video_codec": "H265"}, "codec"), ({"width": 1080}, "width"),
                             ({"frame_rate": "29.97"}, "frame rate"), ({"frames": 95}, "95"),
                             ({"audio": False}, "unexpected audio"), ({"audio_channels": 1}, "channels")):
            problems = probe_mod.verify_media(info, {**good, **change})
            self.assertTrue(any(frag in p for p in problems), (change, problems))
        audio_only = probe_mod.parse_ffprobe_json("y.mp4", 5000, {"streams": FFPROBE_DOC["streams"][1:], "format": {}})
        self.assertIn("no video stream", probe_mod.verify_media(audio_only, good))
        empty = probe_mod.parse_ffprobe_json("z.mp4", 0, {"streams": [], "format": {}})
        self.assertTrue(any("too small" in p for p in probe_mod.verify_media(empty, good)))

    def test_ffprobe_missing_fails_closed(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"x")
        self.addCleanup(os.unlink, fh.name)
        with mock.patch.object(probe_mod.shutil, "which", return_value=None):
            with self.assertRaises(probe_mod.ProbeError) as cm:
                probe_mod.ffprobe(fh.name)
        self.assertEqual(cm.exception.code, "ffprobe_unavailable")
        with self.assertRaises(probe_mod.ProbeError) as cm:
            probe_mod.ffprobe(fh.name + "missing", executable="ffprobe")
        self.assertEqual(cm.exception.code, "missing")

    def test_verify_export_operation(self):
        rig = Rig(self)
        path = os.path.join(rig.out, "f.mp4")
        with open(path, "w") as fh:
            json.dump({"codec": "h264", "width": 1920, "height": 1080, "rate": "25", "frames": 10, "video": True,
                       "audio": False}, fh)
        r = rig.ex.execute(VerifyExport("v", path=path, expect={"frames": 10, "audio": False}))
        self.assertTrue(r.ok, r.to_dict())
        r = rig.ex.execute(VerifyExport("v", path=path, expect={"frames": 11}))
        self.assertEqual(r.failure, FailureClass.EXPORT_INVALID)
        r = rig.ex.execute(VerifyExport("v", path=os.path.join(rig.out, "none.mp4")))
        self.assertEqual(r.failure, FailureClass.EXPORT_INVALID)
        r = rig.ex.execute(VerifyExport("v", path=rig.paths["a.mov"]))
        self.assertEqual(r.failure, FailureClass.VALIDATION)           # outside the output roots


# ── bridge client ───────────────────────────────────────────────────────────


class _OneShotServer:
    """A loopback server answering one connection with a function of the request."""

    def __init__(self, testcase, reply_fn):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.reply_fn = reply_fn
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()
        testcase.addCleanup(self.sock.close)

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn, conn.makefile("rb") as r:
            req = json.loads(r.readline())
            self.received.append(req)
            out = self.reply_fn(req)
            if out is not None:
                conn.sendall(out)
            else:
                threading.Event().wait(1.5)                             # hold the connection open, silent


class TestBridgeClient(unittest.TestCase):
    TOKEN = "t" * 32

    def client(self, port, timeout=2.0):
        return bridge_mod.BridgeClient("127.0.0.1", port, self.TOKEN, timeout=timeout)

    def test_signature_is_hmac_over_canonical_request(self):
        payload = {"protocol": "1.0", "id": "x", "timestamp": 1, "nonce": "n" * 20, "operation": "health", "arguments": {}}
        want = hmac.new(self.TOKEN.encode(), json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                        hashlib.sha256).hexdigest()
        self.assertEqual(bridge_mod.sign(self.TOKEN, payload), want)
        self.assertEqual(bridge_mod.sign(self.TOKEN, {**payload, "signature": "zz"}), want)

    def test_token_is_never_shown_and_host_must_be_loopback(self):
        c = self.client(1)
        self.assertNotIn(self.TOKEN, repr(c))
        self.assertNotIn(self.TOKEN, str(vars(c)))
        with self.assertRaises(AdapterError):
            bridge_mod.BridgeClient("192.168.1.5", 49632, self.TOKEN)
        with self.assertRaises(AdapterError):
            bridge_mod.BridgeClient("127.0.0.1", 49632, "short")

    def test_config_loading(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(AdapterError) as cm:
                bridge_mod.BridgeClient.from_config(Path(d) / "none.json")
            self.assertEqual(cm.exception.failure, FailureClass.BRIDGE_UNAVAILABLE)
            p = Path(d) / "bridge.json"
            p.write_text(json.dumps({"host": "127.0.0.1", "port": 5, "token": self.TOKEN,
                                     "allowed_output_roots": ["C:\\out"], "allowed_media_roots": ["C:\\m"]}))
            c = bridge_mod.BridgeClient.from_config(p)
            self.assertEqual((c.allowed_output_roots, c.allowed_media_roots), (("C:\\out",), ("C:\\m",)))

    def test_round_trip_with_handles(self):
        def reply(req):
            self.assertTrue(hmac.compare_digest(req["signature"], bridge_mod.sign(self.TOKEN, req)))
            value = {"__handle__": "h7", "__shape__": "Project"}
            return (json.dumps({"id": req["id"], "ok": True, "result": {"value": [value, 3]}}) + "\n").encode()
        srv = _OneShotServer(self, reply)
        out = self.client(srv.port).call(bridge_mod.Handle("h1"), "GetThing", bridge_mod.Handle("h2"), {"k": 1})
        self.assertEqual(out, [bridge_mod.Handle("h7", "Project"), 3])
        req = srv.received[0]
        self.assertEqual(req["arguments"], {"target": "h1", "method": "GetThing", "args": [{"__handle__": "h2"}, {"k": 1}]})

    def test_failures_are_classified(self):
        srv = _OneShotServer(self, lambda req: (json.dumps({"id": "other", "ok": True}) + "\n").encode())
        with self.assertRaises(AdapterError) as cm:
            self.client(srv.port).health()
        self.assertEqual(cm.exception.failure, FailureClass.BRIDGE_PROTOCOL)
        srv = _OneShotServer(self, lambda req: (json.dumps({"id": req["id"], "ok": False,
                                                            "error": {"code": "call_failed", "message": "boom"}}) + "\n").encode())
        with self.assertRaises(AdapterError) as cm:
            self.client(srv.port).call("resolve", "X")
        self.assertEqual(cm.exception.failure, FailureClass.RESOLVE_REFUSED)
        srv = _OneShotServer(self, lambda req: None)                    # never answers
        with self.assertRaises(AdapterError) as cm:
            self.client(srv.port, timeout=0.3).call("resolve", "GetVersionString")
        self.assertEqual(cm.exception.failure, FailureClass.BRIDGE_TIMEOUT)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                                       # nothing listens there
        with self.assertRaises(AdapterError) as cm:
            self.client(port, timeout=0.5).health()
        self.assertEqual(cm.exception.failure, FailureClass.BRIDGE_UNAVAILABLE)
        srv = _OneShotServer(self, lambda req: (json.dumps({"id": req["id"], "ok": True,
                                                            "result": {"value": [], "truncated": {"dropped": 3}}}) + "\n").encode())
        with self.assertRaises(AdapterError) as cm:
            self.client(srv.port).call("resolve", "GetList")
        self.assertEqual(cm.exception.failure, FailureClass.BRIDGE_PROTOCOL)


# ── layering ────────────────────────────────────────────────────────────────


class TestLayering(unittest.TestCase):
    def imports_of(self, name):
        tree = ast.parse((PKG / name).read_text(encoding="utf-8"))
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                out.add(("." * node.level) + (node.module or ""))
            elif isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
        return out

    def test_package_depends_on_nothing_else_in_the_repository(self):
        siblings = {"creator_state", "media_analysis", "speaker_attribution", "creator_dna", "research", "assets",
                    "creative_director", "governance"}
        for f in PKG.glob("*.py"):
            for imp in self.imports_of(f.name):
                self.assertNotIn(imp.split(".")[0], siblings, f.name)

    def test_executor_and_render_never_import_the_live_transport(self):
        for name in ("executor.py", "render.py", "validation.py", "timebase.py", "operations.py", "capabilities.py"):
            imps = self.imports_of(name)
            self.assertFalse({".live", ".bridge", "socket"} & imps, name)

    def test_no_raw_call_reachable_from_the_high_level_api(self):
        import resolve_executor
        self.assertFalse(hasattr(resolve_executor, "BridgeClient"))
        self.assertFalse(any("call" == n.lower() for n in dir(resolve_executor.Executor)))


class TestMeasuredResolveBehaviour(unittest.TestCase):
    """Behaviours established live on Free 21.0.4.5 (E-RSV-LIVE-0001), reproduced against the fake."""

    def test_ntsc_timeline_uses_the_project_rate_and_a_second_ntsc_rate_is_refused(self):
        rig = Rig(self)
        assert rig.ex.execute(CreateProject("p", name="P")).ok
        rig.fake.p["settings"]["timelineFrameRate"] = "24"
        r = rig.ex.execute(CreateTimeline("t", name="N", frame_rate="23.976", width=1920, height=1080))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(r.evidence["steps"][0], {"project_rate": True})
        self.assertEqual(rig.fake.timeline_info("N")["frame_rate"], "23.976")
        self.assertEqual(rig.fake.timeline_info("N")["start_frame"], 86400)      # 01:00:00:00 at nominal 24
        rig.fake.calls.clear()
        r = rig.ex.execute(CreateTimeline("t2", name="N2", frame_rate="29.97", width=1920, height=1080))
        self.assertEqual((r.outcome, r.failure), (Outcome.REFUSED, FailureClass.PRECONDITION))
        self.assertIn("new project", r.message)
        self.assertNotIn("create_timeline", rig.fake.calls)
        r = rig.ex.execute(CreateTimeline("t3", name="I", frame_rate="50", width=1920, height=1080))
        self.assertTrue(r.ok)                                           # integer rates are set per timeline
        r = rig.ex.execute(CreateTimeline("t4", name="N3", frame_rate="23.976", width=1920, height=1080))
        self.assertTrue(r.ok)                                           # same NTSC rate as the project is fine

    def test_ninety_five_from_a_25_fps_source_in_ntsc_and_24_fps_timelines(self):
        rig = Rig(self)
        assert rig.ex.execute(CreateProject("p", name="P")).ok
        assert rig.ex.execute(ImportMedia("i", paths=(rig.paths["a.mov"],))).ok
        assert rig.ex.execute(CreateTimeline("t", name="N", frame_rate="23.976", width=1920, height=1080)).ok
        assert rig.ex.execute(CreateTimeline("t24", name="T24", frame_rate="24", width=1920, height=1080)).ok
        p = rig.paths["a.mov"]
        self.assertEqual(rig.ex.execute(AppendSegment("a", timeline="N", media_path=p, source=FrameRange(0, 100)))
                         .evidence["expect"]["duration"], 95)
        d = [rig.ex.execute(AppendSegment(f"b{e}", timeline="T24", media_path=p, source=FrameRange(0, e)))
             .evidence["expect"]["duration"] for e in (100, 99)]
        self.assertEqual(d, [96, 95])                                   # the historical 95 was [0, 99) at 24 fps

    def test_title_on_a_nested_timeline_at_another_rate_is_converted_exactly(self):
        rig = Rig(self)
        rig.setup_timeline()
        rig.fake.p["settings"]["timelineFrameRate"] = "24"
        rig.fake.title_rate_ignored = True
        r = rig.ex.execute(PlaceTitle("t", timeline="Main", text="T", track_index=1, record_frame=90000, duration=40))
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual((r.evidence["title_rate"], r.evidence["source_frames"]), ("24", 39))
        self.assertEqual(rig.items()[0]["duration"], 40)
        r = rig.ex.execute(PlaceTitle("t2", timeline="Main", text="T", track_index=1, record_frame=90100, duration=121))
        self.assertTrue(r.ok, r.to_dict())                              # 117 of the 120 title frames
        r = rig.ex.execute(PlaceTitle("t3", timeline="Main", text="T", track_index=1, record_frame=90300, duration=126))
        self.assertEqual(r.failure, FailureClass.READBACK_MISMATCH)     # would need 121 of 120 title frames
        self.assertIn("only 120 frames", r.message)
        r = rig.ex.execute(PlaceTitle("t4", timeline="Main", text="T", track_index=1, record_frame=90500, duration=24))
        self.assertEqual(r.failure, FailureClass.READBACK_MISMATCH)     # floor(n * 25/24) skips 24: no exact length
        self.assertIn("no whole number", r.message)

    def test_queued_job_readback_must_match(self):
        rig = Rig(self)
        rig.setup_timeline()
        assert rig.ex.execute(AppendSegment("a", timeline="Main", media_path=rig.paths["a.mov"], source=FrameRange(0, 10))).ok
        rig.fake.job_overrides = {"IsExportAudio": False}
        r = rig.ex.execute(RenderTimeline("r", timeline="Main", spec=RenderSpec(rig.out, "o")))
        self.assertEqual(r.failure, FailureClass.READBACK_MISMATCH)
        self.assertIn("IsExportAudio", r.message)
        self.assertEqual(rig.fake.render_jobs(), [])                    # the wrong job was removed from the queue
        self.assertFalse(os.path.exists(os.path.join(rig.out, "o.mp4")))

    def test_audio_only_clip_frames_from_duration(self):
        from resolve_executor.live import clip_frames
        self.assertEqual(clip_frames({"Frames": "200", "FPS": 25.0}), (200, "Frames"))
        self.assertEqual(clip_frames({"Frames": "", "FPS": 24.0, "Type": "Audio", "Duration": "00:00:10:00"}),
                         (240, "Duration"))
        self.assertEqual(clip_frames({"Frames": "", "FPS": 24.0, "Type": "Video", "Duration": "00:00:10:00"}),
                         (None, None))
        self.assertEqual(clip_frames({"Frames": "", "FPS": 24.0, "Type": "Audio", "Duration": "bad"}), (None, None))


@unittest.skipUnless(os.environ.get("CREATOR_STUDIO_RESOLVE_LIVE") == "1",
                     "live Resolve tests run only with CREATOR_STUDIO_RESOLVE_LIVE=1 (see resolve_executor/live_suite.py)")
class TestLiveSmoke(unittest.TestCase):
    def test_bridge_health(self):
        from resolve_executor.live import LiveResolveAdapter
        h = LiveResolveAdapter.connect().health()
        self.assertIn("DaVinci Resolve", str(h.get("product")))


if __name__ == "__main__":
    unittest.main()
