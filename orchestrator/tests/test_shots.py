"""Shot-detection core tests (metrics parsing + classification). Synthetic data only.

Fixtures marked REAL were captured from FFmpeg 9.0.1 on tiny generated clips (64x48, 25 fps,
10 red + 10 black + 10 blue frames; a 0.8 s test-pattern-to-colour dissolve; a test-pattern fade-out).
No real footage is used. The one process-running test builds a 30-frame, 64x48 clip and skips if
ffmpeg is not on PATH.

Run from orchestrator/:  python -I -m unittest tests.test_shots
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contextlib
import io
import os
import re
import time
from unittest import mock

from creator_state import ingest, media, shots
from creator_state.cli import main as cli_main
from creator_state import shot_classify as sc
from creator_state import shot_metrics as sm
from creator_state.project import Project

# ------------------------------------------------------------------ REAL fixtures
# `scdet=threshold=10,metadata=mode=print:file=...` on red(0-9) / black(10-19) / blue(20-29).
_PTS_TIME = ["0", "0.04", "0.08", "0.12", "0.16", "0.2", "0.24", "0.28", "0.32", "0.36", "0.4", "0.44",
             "0.48", "0.52", "0.56", "0.6", "0.64", "0.68", "0.72", "0.76", "0.8", "0.84", "0.88", "0.92",
             "0.96", "1", "1.04", "1.08", "1.12", "1.16"]


def _real_scdet_text() -> str:
    out = []
    for n, t in enumerate(_PTS_TIME):
        out.append(f"frame:{n:<4d}pts:{n * 40:<7d}pts_time:{t}")
        if n == 10:
            out += ["lavfi.scd.mafd=25.391", "lavfi.scd.score=25.391", "lavfi.scd.time=0.4"]
        elif n == 20:
            out += ["lavfi.scd.mafd=9.766", "lavfi.scd.score=9.766"]
        else:
            out += ["lavfi.scd.mafd=0.000", "lavfi.scd.score=0.000"]
    return "\n".join(out) + "\n"


REAL_SCDET = _real_scdet_text()
# Literal excerpt exactly as FFmpeg wrote it (frames 9-11):
REAL_SCDET_EXCERPT = (
    "frame:9    pts:360     pts_time:0.36\n"
    "lavfi.scd.mafd=0.000\n"
    "lavfi.scd.score=0.000\n"
    "frame:10   pts:400     pts_time:0.4\n"
    "lavfi.scd.mafd=25.391\n"
    "lavfi.scd.score=25.391\n"
    "lavfi.scd.time=0.4\n"
    "frame:11   pts:440     pts_time:0.44\n"
    "lavfi.scd.mafd=0.000\n"
    "lavfi.scd.score=0.000\n"
)
REAL_BLACK_LOG = (
    "[Parsed_blackdetect_0 @ 000001f282480bc0] black_start:0.4 black_end:0.8 black_duration:0.4\n"
    "[out#0/null @ 000001f2824a0c00] video:12KiB audio:0KiB subtitle:0KiB other streams:0KiB "
    "global headers:0KiB muxing overhead: unknown\n")
REAL_BLACK_LOG_TRAILING = (   # black runs to the end of the stream: log end = last frame time
    "[Parsed_blackdetect_0 @ 0000025c6623cd80] black_start:0.4 black_end:0.76 black_duration:0.36\n"
    "[out#0/null @ 0000025c6437e800] video:8KiB audio:0KiB subtitle:0KiB other streams:0KiB "
    "global headers:0KiB muxing overhead: unknown\n")
REAL_BLACK_LOG_NONE = (       # a clean run with no black prints no blackdetect line at all
    "[out#0/null @ 000001e3557df380] video:12KiB audio:0KiB subtitle:0KiB other streams:0KiB "
    "global headers:0KiB muxing overhead: unknown\n")
REAL_BLACK_META = ("frame:10   pts:400     pts_time:0.4\nlavfi.black_start=0.4\n"
                   "frame:20   pts:800     pts_time:0.8\nlavfi.black_end=0.8\n")
REAL_BLACK_META_TRAILING = "frame:10   pts:400     pts_time:0.4\nlavfi.black_start=0.4\n"
REAL_SIGNALSTATS_10 = (
    "frame:10   pts:400     pts_time:0.4\n"
    "lavfi.signalstats.YMIN=16\nlavfi.signalstats.YLOW=16\nlavfi.signalstats.YAVG=16\n"
    "lavfi.signalstats.YHIGH=16\nlavfi.signalstats.YMAX=16\nlavfi.signalstats.UMIN=128\n"
    "lavfi.signalstats.ULOW=128\nlavfi.signalstats.UAVG=128\nlavfi.signalstats.UHIGH=128\n"
    "lavfi.signalstats.UMAX=128\nlavfi.signalstats.VMIN=128\nlavfi.signalstats.VLOW=128\n"
    "lavfi.signalstats.VAVG=128\nlavfi.signalstats.VHIGH=128\nlavfi.signalstats.VMAX=128\n"
    "lavfi.signalstats.SATMIN=0\nlavfi.signalstats.SATLOW=0\nlavfi.signalstats.SATAVG=0\n"
    "lavfi.signalstats.SATHIGH=0\nlavfi.signalstats.SATMAX=0\nlavfi.signalstats.HUEMED=180\n"
    "lavfi.signalstats.HUEAVG=180\nlavfi.signalstats.YDIF=65\nlavfi.signalstats.UDIF=38\n"
    "lavfi.signalstats.VDIF=112\nlavfi.signalstats.YBITDEPTH=1\nlavfi.signalstats.UBITDEPTH=1\n"
    "lavfi.signalstats.VBITDEPTH=1\n")
# Per-frame YAVG / scdet score from real tiny gradual transitions (30 and 25 frames).
REAL_DISSOLVE_LUMA = [71.763, 71.763, 71.763, 71.8132, 71.8132, 71.8132, 71.8184, 71.8242, 71.8242,
                      71.8294, 71.8294, 75.821, 80.4583, 84.7646, 89.4033, 93.9564, 98.4733, 103.022,
                      107.541, 111.971, 117, 117, 117, 117, 117, 117, 117, 117, 117, 117]
REAL_DISSOLVE_SCORES = [0, 0, 0, 0.030, 0, 0, 0.006, 0, 0, 0.006, 0, 2.343, 0.184, 0.126, 0.187, 0.119,
                        0.011, 0.073, 0.073, 0.035, 0.138] + [0] * 9
REAL_FADEOUT_LUMA = [71.763, 71.763, 71.763, 71.8132, 71.8132, 71.8132, 71.8184, 71.8242, 71.8242,
                     71.8294, 71.8294, 66.1201, 60.6702, 54.9723, 49.5225, 43.9564, 38.2572, 32.8145,
                     27.1182, 21.6761, 16, 16, 16, 16, 16]
REAL_FADEOUT_SCORES = [0, 0, 0, 0.030, 0, 0, 0.006, 0, 0, 0.006, 0, 2.230, 0.101, 0.105, 0.105, 0.045,
                       0.058, 0.106, 0.099, 0.099, 0.091] + [0] * 4


def sf(frame, score, mafd=None):
    return sm.ScdetFrame(frame, None, str(frame), None, score if mafd is None else mafd, score, None)


class TimingTests(unittest.TestCase):
    def test_parse_fps_forms(self):
        self.assertEqual(sm.parse_fps("25"), Fraction(25))
        self.assertEqual(sm.parse_fps("25/1"), Fraction(25))
        self.assertEqual(sm.parse_fps("30000/1001"), Fraction(30000, 1001))
        self.assertEqual(sm.parse_fps(25), Fraction(25))
        self.assertEqual(sm.parse_fps(Fraction(24000, 1001)), Fraction(24000, 1001))

    def test_float_ntsc_is_snapped_to_the_exact_rational(self):
        self.assertEqual(sm.parse_fps(29.97), Fraction(30000, 1001))
        self.assertEqual(sm.parse_fps(23.976), Fraction(24000, 1001))
        self.assertEqual(sm.parse_fps(59.94), Fraction(60000, 1001))
        self.assertEqual(sm.parse_fps(25.0), Fraction(25))

    def test_bad_fps_rejected(self):
        for bad in ("0/0", "25/0", "0", "-25", "abc", "", None, True, float("nan"), float("inf"), "1/2/3", []):
            with self.assertRaises(sm.MetricsError, msg=repr(bad)):
                sm.parse_fps(bad)

    def test_frame_to_seconds_exact_at_25fps(self):
        self.assertEqual(sm.frame_to_seconds(0, "25/1"), 0)
        self.assertEqual(sm.frame_to_seconds(10, "25/1"), Fraction(2, 5))
        self.assertEqual(sm.frame_to_seconds(2565, "25"), Fraction(2565, 25))   # 102.6 s
        self.assertEqual(float(sm.frame_to_seconds(2565, "25")), 102.6)

    def test_ntsc_has_no_drift(self):
        # 30000 frames at 30000/1001 is exactly 1001 s; float arithmetic would drift
        self.assertEqual(sm.frame_to_seconds(30000, "30000/1001"), 1001)
        self.assertEqual(sm.frame_to_seconds(3 * 30000, "30000/1001"), 3003)
        self.assertEqual(sm.seconds_to_frame(1001, "30000/1001"), 30000)
        self.assertEqual(sm.seconds_to_frame("3600.0", "24000/1001"), 86314)  # 3600 * 24000/1001 = 86313.69

    def test_round_trip_every_frame(self):
        for fps in ("25/1", "30000/1001", "24000/1001", "60/1"):
            for n in list(range(0, 3000, 37)) + [10 ** 6]:
                self.assertEqual(sm.seconds_to_frame(sm.frame_to_seconds(n, fps), fps), n, (fps, n))

    def test_seconds_to_frame_modes_and_text_input(self):
        self.assertEqual(sm.seconds_to_frame("0.4", "25"), 10)
        self.assertEqual(sm.seconds_to_frame(0.4, "25"), 10)             # float goes through its decimal text
        self.assertEqual(sm.seconds_to_frame("0.41", "25", mode="floor"), 10)
        self.assertEqual(sm.seconds_to_frame("0.41", "25", mode="ceil"), 11)
        self.assertEqual(sm.seconds_to_frame("0.42", "25"), 11)
        self.assertEqual(sm.seconds_to_frame("0.02", "25"), 1)           # tie rounds up
        self.assertEqual(sm.seconds_to_frame("12345.678", "25"), 308642)  # large offset, no drift
        with self.assertRaises(sm.MetricsError):
            sm.seconds_to_frame("-1", "25")
        with self.assertRaises(sm.MetricsError):
            sm.seconds_to_frame("1", "25", mode="bogus")

    def test_snap_reports_pts_time_rounding_for_ntsc(self):
        # REAL: a 1 ms time base printed frame 12 of a 30000/1001 clip as pts_time 0.4 (true 0.4004)
        frame, residual = sm.snap_seconds_to_frame("0.4", "30000/1001")
        self.assertEqual(frame, 12)
        self.assertEqual(residual, Fraction(2, 5) - Fraction(12 * 1001, 30000))
        self.assertLess(abs(residual), Fraction(1, 1000))

    def test_expected_frame_count_and_timestamp_format(self):
        self.assertEqual(sm.expected_frame_count("102.6", "25"), 2565)
        self.assertEqual(sm.format_timestamp(Fraction(2565, 25)), "00:01:42.600")
        self.assertEqual(sm.format_timestamp(0), "00:00:00.000")
        self.assertEqual(sm.format_timestamp(Fraction(3661, 1) + Fraction(1, 2000)), "01:01:01.001")  # half-up
        self.assertEqual(sm.fraction_to_str(Fraction(2, 5)), "2/5")
        self.assertEqual(sm.fraction_to_str(Fraction(4, 1)), "4")

    def test_bad_frame_numbers(self):
        for bad in (-1, 1.5, "3", True, None):
            with self.assertRaises(sm.MetricsError, msg=repr(bad)):
                sm.frame_to_seconds(bad, "25")


class ArgvTests(unittest.TestCase):
    def test_analysis_argv_is_a_safe_list(self):
        a = sm.build_analysis_argv("ffmpeg", "in.mov", "C:\\tmp\\out dir\\meta.txt", threshold=10, threads=2)
        self.assertIsInstance(a, list)
        self.assertTrue(all(isinstance(x, str) for x in a))
        self.assertEqual(a[0], "ffmpeg")
        self.assertIn("-nostdin", a)
        self.assertIn("-an", a)
        self.assertNotIn("-y", a)                                 # never overwrites
        self.assertEqual(a[-3:], ["-f", "null", "-"])             # no media is written
        self.assertEqual(a[a.index("-i") + 1], "in.mov")
        vf = a[a.index("-vf") + 1]
        self.assertIn("scdet=threshold=10", vf)
        self.assertIn("signalstats", vf)
        self.assertIn("scale=-2:360", vf)
        bs = chr(92)
        self.assertIn("file=C" + bs * 2 + ":/tmp/out dir/meta.txt", vf)   # drive colon gets two backslashes (verified)
        self.assertNotIn(bs + bs + bs, vf)                                 # no stray extra escaping
        self.assertEqual(sm.escape_filter_path("D:\\a b\\c.txt"), "D" + bs * 2 + ":/a b/c.txt")
        self.assertEqual(sm.escape_filter_path("/plain/path.txt"), "/plain/path.txt")

    def test_options_can_be_turned_off(self):
        a = sm.build_analysis_argv("ffmpeg", "in.mov", "/t/m.txt", scale_height=None, signalstats=False)
        vf = a[a.index("-vf") + 1]
        self.assertNotIn("scale", vf)
        self.assertNotIn("signalstats", vf)

    def test_refuses_dangerous_or_invalid_inputs(self):
        bad_paths = ["a'b.txt", "a;b.txt", "a,b.txt", "a[1].txt", "a=b.txt", "a\nb.txt", ""]
        for p in bad_paths:
            with self.assertRaises(sm.MetricsError, msg=repr(p)):
                sm.build_analysis_argv("ffmpeg", "in.mov", p)
        with self.assertRaises(sm.MetricsError):
            sm.build_analysis_argv("ffmpeg", "-i", "/t/m.txt")          # source looks like an option
        with self.assertRaises(sm.MetricsError):
            sm.build_analysis_argv("", "in.mov", "/t/m.txt")
        for kw in ({"threshold": -1}, {"threshold": 101}, {"threshold": float("nan")}, {"threads": 0},
                   {"threads": True}, {"scale_height": 3}):
            with self.assertRaises(sm.MetricsError, msg=str(kw)):
                sm.build_analysis_argv("ffmpeg", "in.mov", "/t/m.txt", **kw)

    def test_blackdetect_argv(self):
        a = sm.build_blackdetect_argv("ffmpeg", "in.mov", min_duration=0.1)
        self.assertIn("info", a)
        self.assertIn("blackdetect=d=0.1:pic_th=0.98:pix_th=0.1", a[a.index("-vf") + 1])
        self.assertEqual(a[-3:], ["-f", "null", "-"])
        with self.assertRaises(sm.MetricsError):
            sm.build_blackdetect_argv("ffmpeg", "in.mov", pixel_black=2)


class ScdetParserTests(unittest.TestCase):
    def test_real_fixture_parses_completely(self):
        p = sm.parse_scdet(REAL_SCDET)
        self.assertEqual(p.status, sm.OK)
        self.assertTrue(p.ok)
        self.assertEqual(p.problems, ())
        self.assertEqual(len(p.frames), 30)
        self.assertEqual(p.frames_seen, 30)
        self.assertEqual([f.frame for f in p.frames], list(range(30)))

    def test_cut_frame_values_are_preserved_raw(self):
        p = sm.parse_scdet(REAL_SCDET)
        f10, f20, f11 = p.frames[10], p.frames[20], p.frames[11]
        self.assertEqual((f10.mafd, f10.score, f10.flagged_time, f10.pts, f10.pts_time), (25.391, 25.391, "0.4", 400, "0.4"))
        self.assertEqual(f10.pts_time_s, Fraction(2, 5))
        # black -> blue scored 9.766: below scdet's default threshold of 10, so no time flag
        self.assertEqual((f20.score, f20.flagged_time), (9.766, None))
        self.assertEqual((f11.score, f11.flagged_time), (0.0, None))
        self.assertEqual(p.frames[0].score, 0.0)

    def test_excerpt_as_written_by_ffmpeg(self):
        p = sm.parse_scdet(REAL_SCDET_EXCERPT)
        self.assertEqual(p.status, sm.OK)
        self.assertEqual([f.frame for f in p.frames], [9, 10, 11])
        self.assertEqual(p.frames[1].flagged_time, "0.4")

    def test_valid_empty_result_is_not_a_failed_parse(self):
        quiet = "".join(f"frame:{n:<4d}pts:{n * 40:<7d}pts_time:{n * 0.04:g}\nlavfi.scd.mafd=0.000\nlavfi.scd.score=0.000\n"
                        for n in range(5))
        ok = sm.parse_scdet(quiet)
        self.assertEqual(ok.status, sm.OK)
        self.assertEqual(sm.group_candidates(ok.frames, 5.0), ())      # valid: no events
        for text, status in (("", sm.EMPTY_INPUT), ("   \n", sm.EMPTY_INPUT), ("hello world\n", sm.NO_FRAMES)):
            bad = sm.parse_scdet(text)
            self.assertEqual(bad.status, status)
            self.assertFalse(bad.ok)
            self.assertEqual(bad.frames, ())
        self.assertNotEqual(ok.status, sm.parse_scdet("").status)

    def test_frames_without_scdet_keys(self):
        p = sm.parse_scdet(REAL_SIGNALSTATS_10)
        self.assertEqual(p.status, sm.NO_METADATA)
        self.assertFalse(p.ok)

    def test_incomplete_and_malformed_output(self):
        cases = {
            "missing score": "frame:0   pts:0  pts_time:0\nlavfi.scd.mafd=0.000\n",
            "missing mafd": "frame:0   pts:0  pts_time:0\nlavfi.scd.score=1.0\n",
            "non numeric": "frame:0   pts:0  pts_time:0\nlavfi.scd.mafd=abc\nlavfi.scd.score=1.0\n",
            "nan": "frame:0   pts:0  pts_time:0\nlavfi.scd.mafd=nan\nlavfi.scd.score=nan\n",
            "negative": "frame:0   pts:0  pts_time:0\nlavfi.scd.mafd=-1\nlavfi.scd.score=-1\n",
            "truncated mid-frame": REAL_SCDET[:REAL_SCDET.index("lavfi.scd.score=25.391")],
        }
        for name, text in cases.items():
            p = sm.parse_scdet(text)
            self.assertEqual(p.status, sm.INCOMPLETE, name)
            self.assertTrue(p.problems, name)
            self.assertFalse(p.ok, name)

    def test_gaps_and_ordering_are_reported(self):
        gap = ("frame:0   pts:0  pts_time:0\nlavfi.scd.mafd=0\nlavfi.scd.score=0\n"
               "frame:2   pts:80  pts_time:0.08\nlavfi.scd.mafd=0\nlavfi.scd.score=0\n")
        p = sm.parse_scdet(gap)
        self.assertEqual(p.status, sm.INCOMPLETE)
        self.assertTrue(any("frame gap" in x for x in p.problems))
        back = ("frame:1   pts:40  pts_time:0.04\nlavfi.scd.mafd=0\nlavfi.scd.score=0\n"
                "frame:1   pts:40  pts_time:0.04\nlavfi.scd.mafd=0\nlavfi.scd.score=0\n")
        self.assertEqual(sm.parse_scdet(back).status, sm.INCOMPLETE)

    def test_unrecognised_lines_and_orphan_values(self):
        p = sm.parse_frame_metadata("lavfi.scd.mafd=1\nframe:0 pts:0 pts_time:0\nlavfi.scd.mafd=1\nnoise here\n")
        self.assertEqual(p.status, sm.INCOMPLETE)
        self.assertEqual(len(p.problems), 2)
        self.assertEqual(len(p.frames), 1)

    def test_non_integer_pts_and_time_are_kept_and_flagged(self):
        p = sm.parse_frame_metadata("frame:0   pts:N/A  pts_time:N/A\nlavfi.scd.mafd=0\nlavfi.scd.score=0\n")
        self.assertEqual(p.status, sm.INCOMPLETE)
        self.assertIsNone(p.frames[0].pts)
        self.assertIsNone(p.frames[0].pts_time_s)
        self.assertEqual(p.frames[0].pts_time, "N/A")

    def test_non_string_input_is_a_programming_error(self):
        for fn in (sm.parse_scdet, sm.parse_frame_metadata, sm.parse_signalstats, sm.parse_blackdetect_log,
                   sm.parse_blackdetect_metadata):
            with self.assertRaises(sm.MetricsError):
                fn(None)


class SignalstatsAndBlackTests(unittest.TestCase):
    def test_signalstats_real_frame(self):
        p = sm.parse_signalstats(REAL_SIGNALSTATS_10)
        self.assertEqual(p.status, sm.OK)
        v = p.frames[0].values
        self.assertEqual((v["YAVG"], v["YDIF"], v["SATAVG"], v["HUEMED"], v["YMIN"], v["YMAX"]), (16, 65, 0, 180, 16, 16))
        self.assertEqual(p.frames[0].frame, 10)

    def test_signalstats_failures(self):
        self.assertEqual(sm.parse_signalstats("").status, sm.EMPTY_INPUT)
        self.assertEqual(sm.parse_signalstats(REAL_SCDET).status, sm.NO_METADATA)
        no_yavg = "frame:0 pts:0 pts_time:0\nlavfi.signalstats.YDIF=3\n"
        self.assertEqual(sm.parse_signalstats(no_yavg).status, sm.INCOMPLETE)
        bad = "frame:0 pts:0 pts_time:0\nlavfi.signalstats.YAVG=x\n"
        self.assertEqual(sm.parse_signalstats(bad).status, sm.INCOMPLETE)

    def test_blackdetect_log(self):
        p = sm.parse_blackdetect_log(REAL_BLACK_LOG)
        self.assertEqual(p.status, sm.OK)
        self.assertEqual(p.intervals, (sm.BlackInterval(Fraction(2, 5), Fraction(4, 5), Fraction(2, 5)),))
        t = sm.parse_blackdetect_log(REAL_BLACK_LOG_TRAILING)
        self.assertEqual((t.intervals[0].start, t.intervals[0].end), (Fraction(2, 5), Fraction(19, 25)))

    def test_blackdetect_zero_intervals_vs_no_data(self):
        none = sm.parse_blackdetect_log(REAL_BLACK_LOG_NONE)
        self.assertEqual((none.status, none.intervals), (sm.OK, ()))                  # completed run, no black
        self.assertEqual(sm.parse_blackdetect_log("").status, sm.EMPTY_INPUT)
        cut_short = sm.parse_blackdetect_log("Input #0, matroska,webm, from 'x.mkv':\n")
        self.assertEqual(cut_short.status, sm.INCOMPLETE)                             # no completion line
        self.assertEqual(sm.parse_blackdetect_log("junk", require_completion_marker=False).status, sm.OK)

    def test_blackdetect_inconsistent_interval(self):
        bad = "[x] black_start:0.4 black_end:0.8 black_duration:9\nvideo:1KiB\n"
        p = sm.parse_blackdetect_log(bad)
        self.assertEqual(p.status, sm.INCOMPLETE)
        self.assertEqual(p.intervals, ())

    def test_blackdetect_metadata_forms(self):
        p = sm.parse_blackdetect_metadata(REAL_BLACK_META)
        self.assertEqual(p.status, sm.OK)
        self.assertEqual((p.intervals[0].start, p.intervals[0].end, p.intervals[0].duration),
                         (Fraction(2, 5), Fraction(4, 5), Fraction(2, 5)))
        t = sm.parse_blackdetect_metadata(REAL_BLACK_META_TRAILING)    # black to end of stream: no end key
        self.assertEqual(t.status, sm.OK)
        self.assertTrue(t.intervals[0].open_ended)
        self.assertIsNone(t.intervals[0].end)
        orphan = sm.parse_blackdetect_metadata("frame:5 pts:0 pts_time:0.2\nlavfi.black_end=0.2\n")
        self.assertEqual(orphan.status, sm.INCOMPLETE)
        self.assertEqual(sm.parse_blackdetect_metadata("").status, sm.EMPTY_INPUT)


class GroupingTests(unittest.TestCase):
    def test_adjacent_candidates_cluster(self):
        frames = [sf(i, s) for i, s in enumerate([0, 0, 6, 7, 9, 0, 0, 8, 0])]
        g = sm.group_candidates(frames, 5.0)
        self.assertEqual([(x.first_frame, x.last_frame, x.frames) for x in g], [(2, 4, (2, 3, 4)), (7, 7, (7,))])
        self.assertEqual((g[0].peak_frame, g[0].peak_score, g[0].span), (4, 9, 3))

    def test_short_events_are_never_discarded(self):
        frames = [sf(i, 0) for i in range(30)]
        for i in (3, 5, 20):                           # isolated single-frame events, 2 frames apart
            frames[i] = sf(i, 6.0)
        g = sm.group_candidates(frames, 5.0)
        self.assertEqual([x.frames for x in g], [(3,), (5,), (20,)])

    def test_every_frame_at_or_above_floor_lands_in_exactly_one_group(self):
        scores = [0, 5, 5, 0.1, 12, 30, 4.99, 5.0, 0, 7, 0, 0, 9, 9, 9]
        frames = [sf(i, s) for i, s in enumerate(scores)]
        for gap in (0, 1, 2):
            groups = sm.group_candidates(frames, 5.0, max_gap=gap)
            members = [f for g in groups for f in g.frames]
            self.assertEqual(sorted(members), [i for i, s in enumerate(scores) if s >= 5.0])
            self.assertEqual(len(members), len(set(members)))

    def test_floor_is_inclusive_and_max_gap_bridges_without_swallowing(self):
        frames = [sf(i, s) for i, s in enumerate([6, 0, 6, 0, 0, 6])]
        self.assertEqual(len(sm.group_candidates(frames, 6.0)), 3)
        bridged = sm.group_candidates(frames, 6.0, max_gap=1)
        self.assertEqual([x.frames for x in bridged], [(0, 2), (5,)])
        self.assertEqual(sm.group_candidates(frames, 6.0, max_gap=2)[0].frames, (0, 2, 5))

    def test_peak_tie_prefers_earliest_and_input_order_is_irrelevant(self):
        a = [sf(4, 9.0), sf(3, 9.0), sf(5, 8.0)]
        g = sm.group_candidates(a, 5.0)
        self.assertEqual((g[0].first_frame, g[0].peak_frame), (3, 3))

    def test_bad_inputs(self):
        with self.assertRaises(sm.MetricsError):
            sm.group_candidates([sf(1, 6), sf(1, 7)], 5.0)
        with self.assertRaises(sm.MetricsError):
            sm.group_candidates([], -1)
        with self.assertRaises(sm.MetricsError):
            sm.group_candidates([], 5, max_gap=-1)
        self.assertEqual(sm.group_candidates([], 5.0), ())

    def test_real_fixture_groups(self):
        p = sm.parse_scdet(REAL_SCDET)
        g = sm.group_candidates(p.frames, 5.0)
        self.assertEqual([(x.first_frame, x.peak_score) for x in g], [(10, 25.391), (20, 9.766)])

    def test_context_window_clamps(self):
        vals = list(range(10))
        self.assertEqual(sm.context_window(vals, 4, 5, 2, 2), (2, (2, 3, 4, 5, 6, 7)))
        self.assertEqual(sm.context_window(vals, 0, 0, 3, 1), (0, (0, 1)))
        self.assertEqual(sm.context_window(vals, 9, 9, 1, 5), (8, (8, 9)))
        self.assertEqual(sm.context_window(vals, 4, 4, 1, 1, base_frame=100)[1], ())


class LumaRampTests(unittest.TestCase):
    def test_real_dissolve_is_one_rising_ramp(self):
        r = sm.find_luma_ramps(REAL_DISSOLVE_LUMA)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0].first_frame, r[0].last_frame, r[0].direction, r[0].steps), (11, 20, 1, 10))
        self.assertAlmostEqual(r[0].total_change, 117 - 71.8294, places=3)
        self.assertEqual(r[0].monotonic_fraction, 1.0)

    def test_real_fadeout_is_one_falling_ramp(self):
        r = sm.find_luma_ramps(REAL_FADEOUT_LUMA)
        self.assertEqual([(x.first_frame, x.last_frame, x.direction) for x in r], [(11, 20, -1)])

    def test_static_and_slow_drift_find_nothing(self):
        self.assertEqual(sm.find_luma_ramps([80.0] * 40), ())
        self.assertEqual(sm.find_luma_ramps([80 + 0.05 * i for i in range(60)]), ())   # under min_step
        self.assertEqual(sm.find_luma_ramps(REAL_DISSOLVE_LUMA[:11]), ())              # real pre-transition drift

    def test_short_or_small_ramps_are_not_reported(self):
        self.assertEqual(sm.find_luma_ramps([50, 60, 70, 80, 80, 80]), ())             # 3 steps < 4
        self.assertEqual(sm.find_luma_ramps([50, 51, 52, 53, 54, 55, 56]), ())        # moves only 6 < min_total
        self.assertEqual(len(sm.find_luma_ramps([50, 60, 70, 80, 90, 90], min_frames=4)), 1)

    def test_one_flat_frame_is_tolerated_two_are_not(self):
        flat1 = [50, 60, 70, 70.1, 80, 90, 100]
        r = sm.find_luma_ramps(flat1)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].steps, 6)
        flat2 = [50, 60, 70, 70.1, 70.2, 80, 90, 100]
        self.assertTrue(all(x.steps < 6 for x in sm.find_luma_ramps(flat2)))

    def test_up_then_down_gives_two_ramps_and_base_frame_offsets(self):
        series = [10, 20, 30, 40, 50, 50, 50, 40, 30, 20, 10]
        r = sm.find_luma_ramps(series, base_frame=100)
        self.assertEqual([(x.direction, x.first_frame, x.last_frame) for x in r], [(1, 101, 104), (-1, 107, 110)])

    def test_bad_arguments(self):
        with self.assertRaises(sm.MetricsError):
            sm.find_luma_ramps([1, 2], min_frames=1)
        with self.assertRaises(sm.MetricsError):
            sm.find_luma_ramps([1, 2], max_flat=-1)


# ------------------------------------------------------------------ classification helpers
def window(total=30, spikes=None, luma_fn=None):
    scores = [0.0] * total
    for f, s in (spikes or {}).items():
        scores[f] = s
    luma = tuple(luma_fn(i) for i in range(total)) if luma_fn else ()
    return tuple(scores), luma


def ev(cid="c1", first=10, last=10, peak=25.0, spikes=None, luma_fn=None, **kw):
    scores, luma = window(30, spikes if spikes is not None else {first: peak}, luma_fn)
    return sc.Evidence(cid, first, last, peak, scores=scores, luma=luma, window_start=0, **kw)


class ClassificationTests(unittest.TestCase):
    def assertCat(self, c, category, rule=None):
        self.assertEqual(c.category, category, f"{c.rule}: {c.reasons}")
        if rule:
            self.assertEqual(c.rule, rule)
        self.assertTrue(c.reasons)
        self.assertGreaterEqual(c.confidence, 0.0)
        self.assertLessEqual(c.confidence, 0.95)
        self.assertEqual(c.boundary_kind, sc.BOUNDARY_KIND[category])

    # --- hard cuts / spikes
    def test_single_frame_spike_with_different_content_is_a_hard_cut(self):
        c = sc.classify(ev(luma_fn=lambda i: 80.0 if i < 10 else 120.0, sim_across=0.15, sim_return=0.2))
        self.assertCat(c, "hard_cut", "R4_hard_cut")
        self.assertEqual((c.boundary_kind, c.rejected, c.needs_review), ("cut", False, False))
        self.assertGreaterEqual(c.confidence, 0.8)
        self.assertTrue(any("similarity" in r for r in c.reasons))

    def test_cut_without_similarity_evidence_is_capped_and_says_so(self):
        c = sc.classify(ev(luma_fn=lambda i: 80.0 if i < 10 else 120.0))
        self.assertCat(c, "hard_cut")
        self.assertLessEqual(c.confidence, 0.7)
        self.assertTrue(any("no similarity evidence" in r for r in c.reasons))

    def test_weak_spike_needs_similarity_confirmation(self):
        luma = lambda i: 80.0 if i < 10 else 120.0
        confirmed = sc.classify(ev(peak=7.0, luma_fn=luma, sim_across=0.1, sim_return=0.1))
        self.assertCat(confirmed, "hard_cut", "R4_hard_cut_weak")
        self.assertLess(confirmed.confidence, 0.5)
        unconfirmed = sc.classify(ev(peak=7.0, luma_fn=luma))
        self.assertCat(unconfirmed, "uncertain")
        self.assertTrue(unconfirmed.needs_review)

    def test_two_frame_cut_is_still_a_cut_three_frames_is_not(self):
        luma = lambda i: 80.0 if i < 10 else 120.0
        two = sc.classify(ev(first=10, last=11, spikes={10: 25.0, 11: 4.0}, luma_fn=luma, sim_across=0.1, sim_return=0.1))
        self.assertCat(two, "hard_cut")
        three = sc.classify(ev(first=10, last=12, spikes={10: 25.0, 11: 3.0, 12: 3.0}, luma_fn=luma, sim_across=0.1, sim_return=0.1))
        self.assertCat(three, "uncertain")
        self.assertTrue(any("longer than a cut" in r for r in three.reasons))

    def test_cut_to_or_from_black_without_ramp_stays_a_cut(self):
        c = sc.classify(ev(peak=40.0, luma_fn=lambda i: 100.0 if i < 10 else 16.0, into_black=True))
        self.assertCat(c, "hard_cut")
        self.assertTrue(any("cut to/from black" in r for r in c.reasons))

    def test_spike_but_same_picture_is_uncertain_not_a_cut(self):
        c = sc.classify(ev(luma_fn=lambda i: 80.0, sim_across=0.95, sim_return=0.95))
        self.assertCat(c, "uncertain")
        self.assertTrue(any("looks the same" in r for r in c.reasons))

    def test_similarity_between_the_limits_is_uncertain(self):
        c = sc.classify(ev(luma_fn=lambda i: 80.0 if i < 10 else 120.0, sim_across=0.68, sim_return=0.1))
        self.assertCat(c, "uncertain")
        self.assertTrue(any("between" in r for r in c.reasons))

    # --- flash
    def test_flash_returns_to_the_previous_look(self):
        luma = lambda i: 80.0 if i < 10 else (150.0 if i in (10, 11) else 81.0)
        c = sc.classify(ev(first=10, last=11, spikes={10: 30.0, 11: 28.0}, luma_fn=luma, sim_across=0.9, sim_return=0.95))
        self.assertCat(c, "flash", "R2_flash")
        self.assertTrue(c.rejected)
        self.assertIsNone(c.boundary_kind)
        self.assertTrue(any("returns" in r for r in c.reasons))

    def test_flash_from_luma_alone_is_less_confident_than_with_similarity(self):
        luma = lambda i: 80.0 if i < 10 else (150.0 if i in (10, 11) else 81.0)
        kw = dict(first=10, last=11, spikes={10: 30.0, 11: 28.0}, luma_fn=luma)
        with_sim = sc.classify(ev(sim_return=0.95, **kw))
        luma_only = sc.classify(ev(**kw))
        self.assertCat(luma_only, "flash")
        self.assertLess(luma_only.confidence, with_sim.confidence)

    def test_return_without_brightness_spike_is_a_possible_very_short_shot_not_a_flash(self):
        luma = lambda i: 90.0 if i in (10, 11) else 80.0                        # inserted 2-frame shot, only +10
        c = sc.classify(ev(first=10, last=10, luma_fn=luma, sim_across=0.2, sim_return=0.9))
        self.assertCat(c, "hard_cut")
        self.assertLessEqual(c.confidence, 0.6)
        self.assertTrue(any("very short shot" in r for r in c.reasons))

    def test_return_seen_but_no_luma_data_stays_uncertain(self):
        c = sc.classify(sc.Evidence("c1", 10, 10, 25.0, sim_across=0.2, sim_return=0.9))
        self.assertCat(c, "uncertain", "R2_uncertain_return_without_luma")
        self.assertTrue(c.needs_review)

    def test_bare_spike_with_no_evidence_at_all_is_uncertain(self):
        c = sc.classify(sc.Evidence("c1", 10, 10, 25.0))
        self.assertCat(c, "uncertain", "R2_uncertain_no_return_evidence")

    # --- lighting
    def test_same_content_with_lasting_brightness_step_is_lighting_change(self):
        c = sc.classify(ev(peak=12.0, luma_fn=lambda i: 80.0 if i < 10 else 120.0, sim_across=0.92, sim_return=0.92))
        self.assertCat(c, "lighting_change")
        self.assertTrue(c.rejected)

    def test_gradual_exposure_drift_on_same_content_is_lighting_not_dissolve(self):
        e = sc.Evidence("c1", 11, 20, 2.3, scores=tuple(REAL_DISSOLVE_SCORES), luma=tuple(REAL_DISSOLVE_LUMA),
                        window_start=0, sim_across=0.9)
        self.assertCat(sc.classify(e), "lighting_change")

    # --- sustained events
    def test_plateau_with_similar_content_is_camera_motion_not_a_cut(self):
        spikes = {i: 12.0 for i in range(10, 18)}
        c = sc.classify(ev(first=10, last=17, peak=12.0, spikes=spikes, luma_fn=lambda i: 80.0, sim_across=0.9))
        self.assertCat(c, "camera_motion", "R6_motion")
        self.assertTrue(c.rejected)
        self.assertNotEqual(c.category, "hard_cut")

    def test_plateau_without_similarity_is_uncertain(self):
        spikes = {i: 12.0 for i in range(10, 18)}
        c = sc.classify(ev(first=10, last=17, peak=12.0, spikes=spikes, luma_fn=lambda i: 80.0))
        self.assertCat(c, "uncertain")
        self.assertTrue(any("similarity" in r for r in c.reasons))

    # --- fades
    def test_fade_out_into_black(self):
        luma = lambda i: 100.0 if i < 10 else max(10.0, 100.0 - 9.0 * (i - 9))
        c = sc.classify(ev(first=10, last=19, peak=1.0, spikes={}, luma_fn=luma, into_black=True))
        self.assertCat(c, "fade_out", "R1_fade_out")
        self.assertEqual(c.boundary_kind, "transition")
        self.assertFalse(c.rejected)

    def test_fade_in_from_black(self):
        luma = lambda i: 10.0 if i < 10 else min(100.0, 10.0 + 9.0 * (i - 9))
        c = sc.classify(ev(first=10, last=19, peak=1.0, spikes={}, luma_fn=luma, out_of_black=True))
        self.assertCat(c, "fade_in", "R1_fade_in")

    def test_real_fadeout_fixture(self):
        e = sc.Evidence("fo", 11, 20, 2.23, scores=tuple(REAL_FADEOUT_SCORES), luma=tuple(REAL_FADEOUT_LUMA),
                        window_start=0, into_black=True)
        self.assertCat(sc.classify(e), "fade_out")

    def test_black_next_to_event_with_wrong_direction_or_no_ramp_is_not_a_fade(self):
        rising = lambda i: 10.0 if i < 10 else min(100.0, 10.0 + 9.0 * (i - 9))
        wrong = sc.classify(ev(first=10, last=19, peak=1.0, spikes={}, luma_fn=rising, into_black=True))
        self.assertNotIn(wrong.category, ("fade_in", "fade_out"))
        flat = sc.classify(ev(first=10, last=17, peak=1.0, spikes={}, luma_fn=lambda i: 50.0, into_black=True))
        self.assertCat(flat, "uncertain")
        self.assertTrue(any("black interval nearby" in r for r in flat.reasons))

    # --- dissolve
    def test_real_dissolve_fixture(self):
        e = sc.Evidence("d1", 11, 20, 2.343, scores=tuple(REAL_DISSOLVE_SCORES), luma=tuple(REAL_DISSOLVE_LUMA),
                        window_start=0, sim_across=0.2)
        c = sc.classify(e)
        self.assertCat(c, "dissolve", "R5_dissolve")
        self.assertEqual(c.boundary_kind, "transition")

    def test_dissolve_is_never_a_hard_cut(self):
        for sim in (0.0, 0.2, 0.5, 0.6, 0.7, 0.9, None):
            e = sc.Evidence("d1", 11, 20, 2.343, scores=tuple(REAL_DISSOLVE_SCORES), luma=tuple(REAL_DISSOLVE_LUMA),
                            window_start=0, sim_across=sim)
            self.assertNotEqual(sc.classify(e).category, "hard_cut", sim)
        # even with a strong first-frame score spike inside a long event
        spikes = {11: 40.0, 12: 3.0, 13: 3.0, 14: 3.0, 15: 3.0}
        c = sc.classify(ev(first=11, last=15, peak=40.0, spikes=spikes, luma_fn=lambda i: REAL_DISSOLVE_LUMA[i], sim_across=0.2))
        self.assertNotEqual(c.category, "hard_cut")

    def test_steady_plateau_without_luma_ramp_is_a_weaker_dissolve(self):
        spikes = dict(zip(range(10, 18), [6, 7, 5.5, 6.5, 6, 7, 5, 6]))
        kw = dict(first=10, last=17, peak=7.0, spikes=spikes)
        steady = sc.classify(ev(luma_fn=lambda i: 80.0, sim_across=0.3, **kw))
        self.assertCat(steady, "dissolve")
        real = sc.classify(sc.Evidence("d1", 11, 20, 2.343, scores=tuple(REAL_DISSOLVE_SCORES),
                                       luma=tuple(REAL_DISSOLVE_LUMA), window_start=0, sim_across=0.3))
        self.assertLess(steady.confidence, real.confidence)

    def test_spiky_plateau_without_a_ramp_is_not_called_a_dissolve(self):
        spikes = dict(zip(range(10, 18), [6, 60, 5.5, 6.5, 6, 5, 5, 6]))
        c = sc.classify(ev(first=10, last=17, peak=60.0, spikes=spikes, luma_fn=lambda i: 80.0, sim_across=0.3))
        self.assertNotEqual(c.category, "dissolve")
        self.assertNotEqual(c.category, "hard_cut")
        self.assertTrue(c.needs_review)

    # --- bookkeeping guarantees
    def test_classify_all_keeps_count_and_order_including_uncertain(self):
        items = [
            ev("a", luma_fn=lambda i: 80.0 if i < 10 else 120.0, sim_across=0.1, sim_return=0.1),
            sc.Evidence("b", 5, 5, 25.0),
            ev("c", first=10, last=17, peak=12.0, spikes={i: 12.0 for i in range(10, 18)}, luma_fn=lambda i: 80.0, sim_across=0.9),
            sc.Evidence("d", 11, 20, 2.343, scores=tuple(REAL_DISSOLVE_SCORES), luma=tuple(REAL_DISSOLVE_LUMA), window_start=0, sim_across=0.2),
        ]
        out = sc.classify_all(items)
        self.assertEqual([c.candidate_id for c in out], ["a", "b", "c", "d"])
        self.assertEqual([c.category for c in out], ["hard_cut", "uncertain", "camera_motion", "dissolve"])
        parts = sc.partition(out)
        self.assertEqual({k: len(v) for k, v in parts.items()}, {"cuts": 1, "transitions": 1, "rejected": 1, "uncertain": 1})
        self.assertEqual(sum(len(v) for v in parts.values()), len(items))
        self.assertTrue(all(c.reasons for c in out))

    def test_duplicate_candidate_ids_are_refused(self):
        with self.assertRaises(sc.ClassifyError):
            sc.classify_all([sc.Evidence("x", 1, 1, 1.0), sc.Evidence("x", 2, 2, 1.0)])

    def test_results_are_deterministic(self):
        items = [ev("a", luma_fn=lambda i: 80.0 if i < 10 else 120.0, sim_across=0.1, sim_return=0.1),
                 sc.Evidence("d", 11, 20, 2.343, scores=tuple(REAL_DISSOLVE_SCORES), luma=tuple(REAL_DISSOLVE_LUMA), window_start=0, sim_across=0.2)]
        runs = [sc.classify_all(items) for _ in range(5)]
        self.assertTrue(all(r == runs[0] for r in runs))
        self.assertEqual(json.dumps([c.to_dict() for c in runs[0]], sort_keys=True),
                         json.dumps([c.to_dict() for c in runs[1]], sort_keys=True))

    def test_confidence_is_documented_as_heuristic_and_bounded(self):
        self.assertIn("not a calibrated probability", sc.CONFIDENCE_NOTE)
        self.assertIn("NOT calibrated", sc.__doc__)
        out = sc.classify_all([ev("a", luma_fn=lambda i: 80.0 if i < 10 else 120.0, sim_across=0.0, sim_return=0.0, peak=100.0),
                               sc.Evidence("b", 5, 5, 0.5)])
        for c in out:
            self.assertTrue(0.0 <= c.confidence <= 0.95)
            self.assertTrue(c.confidence_basis)
            d = c.to_dict()
            self.assertEqual(d["confidence_note"], sc.CONFIDENCE_NOTE)
            json.dumps(d)

    def test_thresholds_are_tunable_without_touching_rules(self):
        base = ev(peak=7.0, luma_fn=lambda i: 80.0 if i < 10 else 120.0)
        self.assertEqual(sc.classify(base).category, "uncertain")
        loose = sc.Thresholds(cut_score=6.0)
        self.assertEqual(sc.classify(base, loose).category, "hard_cut")

    def test_evidence_validation(self):
        for kw in ({"first_frame": -1, "last_frame": 3}, {"first_frame": 5, "last_frame": 4},
                   {"first_frame": 1, "last_frame": 1, "sim_across": 1.2},
                   {"first_frame": 1, "last_frame": 1, "sim_return": -0.1},
                   {"first_frame": 1, "last_frame": 1, "scores": (1.0, 2.0), "luma": (1.0,)}):
            with self.assertRaises(sc.ClassifyError, msg=str(kw)):
                sc.Evidence("x", peak_score=1.0, **kw)

    def test_every_category_is_reachable_and_declared(self):
        self.assertEqual(set(sc.CATEGORIES), set(sc.BOUNDARY_KIND))
        self.assertEqual({k for k, v in sc.BOUNDARY_KIND.items() if v == "cut"}, {"hard_cut"})
        self.assertEqual({k for k, v in sc.BOUNDARY_KIND.items() if v == "transition"}, {"fade_in", "fade_out", "dissolve"})


class ShotBuildingTests(unittest.TestCase):
    def covered(self, total, shots, trans):
        return sum(s.length for s in shots) + sum(t.end_frame - t.start_frame + 1 for t in trans)

    def test_no_boundaries_is_one_shot(self):
        shots, trans = sc.build_shots(100, [])
        self.assertEqual([(s.start_frame, s.end_frame) for s in shots], [(0, 99)])
        self.assertEqual(trans, ())

    def test_cuts_split_shots_and_new_shot_starts_on_the_cut_frame(self):
        shots, _ = sc.build_shots(100, [40, 70])
        self.assertEqual([(s.start_frame, s.end_frame) for s in shots], [(0, 39), (40, 69), (70, 99)])

    def test_very_short_shots_are_kept(self):
        shots, _ = sc.build_shots(100, [10, 12])
        self.assertEqual([s.length for s in shots], [10, 2, 88])
        one, _ = sc.build_shots(100, [10, 11])
        self.assertEqual([s.length for s in one], [10, 1, 89])

    def test_dissolve_sits_between_two_shots_and_belongs_to_neither(self):
        shots, trans = sc.build_shots(100, [], [("dissolve", 30, 39, "d1")])
        self.assertEqual([(s.start_frame, s.end_frame) for s in shots], [(0, 29), (40, 99)])
        self.assertEqual((trans[0].kind, trans[0].shot_before, trans[0].shot_after), ("dissolve", 0, 1))
        self.assertEqual(self.covered(100, shots, trans), 100)

    def test_fade_in_at_start_and_fade_out_at_end(self):
        shots, trans = sc.build_shots(100, [], [("fade_in", 0, 9, "f1"), ("fade_out", 90, 99, "f2")])
        self.assertEqual([(s.start_frame, s.end_frame) for s in shots], [(10, 89)])
        self.assertEqual((trans[0].shot_before, trans[0].shot_after), (None, 0))
        self.assertEqual((trans[1].shot_before, trans[1].shot_after), (0, None))
        self.assertEqual(self.covered(100, shots, trans), 100)

    def test_mixed_cuts_and_transitions_cover_every_frame_once(self):
        shots, trans = sc.build_shots(200, [50, 120], [("dissolve", 80, 95, "d"), ("fade_out", 190, 199, "f")])
        self.assertEqual(self.covered(200, shots, trans), 200)
        self.assertEqual([(s.start_frame, s.end_frame) for s in shots], [(0, 49), (50, 79), (96, 119), (120, 189)])

    def test_invalid_boundaries_are_errors_not_silent_merges(self):
        bad = [
            dict(cuts=[0]), dict(cuts=[100]), dict(cuts=[10, 10]), dict(cuts=[True]),
            dict(cuts=[35], transitions=[("dissolve", 30, 39, "d")]),          # cut inside a transition
            dict(cuts=[40], transitions=[("dissolve", 30, 39, "d")]),          # cut leaves no shot after it
            dict(cuts=[], transitions=[("dissolve", 30, 39, "a"), ("fade_out", 38, 45, "b")]),
            dict(cuts=[], transitions=[("wipe", 1, 2, "w")]),
            dict(cuts=[], transitions=[("dissolve", 5, 3, "w")]),
            dict(cuts=[], transitions=[("dissolve", 90, 100, "w")]),
        ]
        for kw in bad:
            with self.assertRaises(sc.ClassifyError, msg=str(kw)):
                sc.build_shots(100, kw["cuts"], kw.get("transitions", ()))
        with self.assertRaises(sc.ClassifyError):
            sc.build_shots(0, [])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
class TinySyntheticFfmpegTest(unittest.TestCase):
    """Runs the argument lists built by shot_metrics against a 30-frame 64x48 generated clip and parses the result."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.clip = cls.dir / "tiny.mkv"
        subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=red:s=64x48:r=25:d=0.4",
                        "-f", "lavfi", "-i", "color=c=black:s=64x48:r=25:d=0.4",
                        "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=25:d=0.4",
                        "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p",
                        "-c:v", "ffv1", "-threads", "1", str(cls.clip)], check=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_analysis_pass_round_trips_through_the_parsers(self):
        meta = self.dir / "meta.txt"
        argv = sm.build_analysis_argv("ffmpeg", str(self.clip), str(meta), threshold=10, scale_height=None, threads=1)
        before = self.clip.read_bytes()
        cp = subprocess.run(argv, capture_output=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.clip.read_bytes(), before)                       # input untouched
        text = meta.read_text(encoding="ascii")
        scd, sig = sm.parse_scdet(text), sm.parse_signalstats(text)
        self.assertEqual((scd.status, sig.status), (sm.OK, sm.OK))
        self.assertEqual(len(scd.frames), 30)
        groups = sm.group_candidates(scd.frames, 5.0)
        self.assertEqual([g.first_frame for g in groups], [10, 20])            # red->black strong; black->blue weaker
        self.assertAlmostEqual(groups[0].peak_score, 25.391, places=2)
        self.assertEqual(sig.frames[9].values["YAVG"], 81)
        self.assertEqual(sig.frames[10].values["YAVG"], 16)
        # the numeric score for the weaker change is below scdet's default threshold of 10
        self.assertLess(groups[1].peak_score, 10.0)

    def test_blackdetect_pass_round_trips(self):
        argv = sm.build_blackdetect_argv("ffmpeg", str(self.clip), min_duration=0.1, scale_height=None, threads=1)
        cp = subprocess.run(argv, capture_output=True, timeout=60)
        self.assertEqual(cp.returncode, 0)
        p = sm.parse_blackdetect_log(cp.stderr.decode("utf-8", "replace"))
        self.assertEqual(p.status, sm.OK)
        self.assertEqual([(i.start, i.end) for i in p.intervals], [(Fraction(2, 5), Fraction(4, 5))])


# =====================================================================================================
# Orchestration (shots.py). Synthetic media only: tiny 64x48 / 25 fps clips generated with lavfi.
# =====================================================================================================
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

_SRC = {"A": "testsrc2=s=64x48:r=25:d=%s", "B": "smptebars=s=64x48:r=25:d=%s", "C": "testsrc=s=64x48:r=25:d=%s",
        "K": "color=c=0x30a050:s=64x48:r=25:d=%s"}
# name -> (list of (source, seconds), filter_complex). Frame numbers asserted below follow from these.
_SCENARIOS = {
    "hardcuts": ([("A", "1.2"), ("B", "1.2"), ("C", "1.2")], "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p"),       # cuts 30, 60
    "shortshot": ([("A", "0.8"), ("B", "0.12"), ("C", "0.8")], "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p"),    # 3-frame shot
    "twoframe": ([("A", "0.8"), ("B", "0.08"), ("C", "0.8")], "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p"),     # 2-frame shot
    "dissolve": ([("A", "1.2"), ("K", "1.2")], "[0][1]xfade=transition=fade:duration=0.4:offset=0.8,format=yuv420p"),
    "fades": ([("A", "1.6")], "fade=t=in:st=0.12:d=0.4,fade=t=out:st=1.0:d=0.4,format=yuv420p"),
    "static": ([("A", "2.0")], "format=yuv420p"),
    "mixed": ([("A", "1.2"), ("B", "1.2"), ("K", "1.0")],
              "[0][1]concat=n=2:v=1:a=0,fps=25,settb=1/25[ab];[ab][2]xfade=transition=fade:duration=0.4:offset=2.0,format=yuv420p"),
}
_FLASH = ("testsrc2=s=64x48:r=25:d=2.4,eq=brightness=1:enable='between(n,25,26)'", "format=yuv420p")   # frames 25-26 blown out
_CLIPS: dict = {}
_CLIP_TMP = None


def _gen_clip(path, inputs, graph):
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y"]
    for i in inputs:
        argv += ["-f", "lavfi", "-i", i]
    argv += ["-filter_complex", graph, "-c:v", "ffv1", "-threads", "1", str(path)]
    subprocess.run(argv, check=True, timeout=120)


_REAL_FSYNC = os.fsync


def setUpModule():
    global _CLIP_TMP
    os.fsync = lambda fd: None         # durability is irrelevant for throwaway test projects and fsync dominates the runtime
    if not HAVE_FFMPEG:
        return
    _CLIP_TMP = tempfile.TemporaryDirectory()
    root = Path(_CLIP_TMP.name)
    for name, (parts, graph) in _SCENARIOS.items():
        _gen_clip(root / f"{name}.mkv", [_SRC[k] % d for k, d in parts], graph)
        _CLIPS[name] = root / f"{name}.mkv"
    _gen_clip(root / "flash.mkv", [_FLASH[0]], _FLASH[1])
    _CLIPS["flash"] = root / "flash.mkv"


def tearDownModule():
    os.fsync = _REAL_FSYNC
    if _CLIP_TMP is not None:
        _CLIP_TMP.cleanup()


def kind_of(argv):
    """Which pass an argument list belongs to."""
    s = " ".join(argv)
    if "-version" in argv:
        return "version"
    if "scdet" in s:
        return "A"
    if "blackdetect" in s:
        return "B"
    if "rawvideo" in s:
        return "C"
    if "-frames:v" in argv:
        return "E"
    return "?"


class NoRunner:
    """Proof that nothing was executed."""
    def __call__(self, *a, **k):
        raise AssertionError("a process was started but none should have been")


class InjectRunner:
    """Real runner, with one pass sabotaged. action: rc | timeout | oserror | kbd | stub:<bytes> | after:<callable>."""
    def __init__(self, on, action, payload=None):
        self.on, self.action, self.payload, self.calls = on, action, payload, []

    def __call__(self, argv, timeout, stdout_path=None):
        k = kind_of(argv)
        self.calls.append(k)
        if k == self.on:
            if self.action == "rc":
                return 1, b"injected failure"
            if self.action == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            if self.action == "oserror":
                raise OSError("cannot start")
            if self.action == "kbd":
                raise KeyboardInterrupt()
            if self.action == "stub":
                return 0, self.payload
        rc, err = shots.default_runner(argv, timeout, stdout_path)
        if k == self.on and self.action == "after":
            self.payload()
        return rc, err


def tree_state(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


class Studio:
    """A throwaway studio: copies of the generated clips are ingested into a project with the real ffprobe."""

    def __init__(self, test, clips, root_name="CreatorStudio", extra=None):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.test = test
        self.root = Path(tmp.name) / root_name
        inc = self.root / "Incoming" / "synth"
        inc.mkdir(parents=True)
        self.files = {}
        for c in clips:
            self.files[c] = inc / f"{c}.mkv"
            shutil.copy2(_CLIPS[c], self.files[c])
        for name, data in (extra or {}).items():
            (inc / name).write_bytes(data)
        self.prober = media.Prober.detect()
        ingest.ingest(self.root, "synth", project_slug="synth", prober=self.prober)
        self.project = Project(self.root / "Projects" / "synth")
        self.manifest = ingest.load_manifest(self.project)
        self.ids = {Path(i["name"]).stem: i["id"] for i in self.manifest["items"]}
        self.ffmpeg = shots.find_ffmpeg()
        self.source_snapshot = self.snap()

    def settings(self, **over):
        base = {"scale_height": None, "threads": 1, "max_evidence_events": 0, "pass_timeout_s": 120}
        base.update(over)
        return shots.load_settings(self.project, base)

    def detect(self, clip, runner=None, ffmpeg="default", prober=None, **over):
        return shots.detect(self.project, self.settings(**over), prober or self.prober,
                            self.ffmpeg if ffmpeg == "default" else ffmpeg, media_ids=[self.ids[clip]], runner=runner)[0]

    def plan(self, clip=None, ffmpeg="default", prober=None, **over):
        return shots.plan(self.project, self.settings(**over), prober or self.prober,
                          self.ffmpeg if ffmpeg == "default" else ffmpeg, media_ids=[self.ids[clip]] if clip else None)

    def snap(self):
        out = {}
        for name, p in self.files.items():
            st = p.stat() if p.exists() else None
            out[name] = (st.st_size, st.st_mtime_ns, util_sha(p)) if st else None
        return out

    def mdir(self, clip):
        return self.project.dir / "analysis" / "shots" / self.ids[clip]

    def part_file(self, clip, rel):
        parts = list(self.mdir(clip).glob(".r_*.part"))
        self.test.assertEqual(len(parts), 1, parts)
        return parts[0] / rel

    def doc(self, res):
        return json.loads((self.project.dir / res["run_dir"] / "shots.json").read_text(encoding="utf-8"))

    def run_hashes(self, res):
        return shots._sha_tree(self.project.dir / res["run_dir"], skip=())


def util_sha(p):
    from creator_state import util
    return util.sha256_file(p)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class OrchestrationCase(unittest.TestCase):
    """Shared assertions."""

    def assert_clean_run(self, st, res):
        self.assertEqual(res["status"], "completed", res)
        run_dir = st.project.dir / res["run_dir"]
        self.assertEqual(shots.verify_run_dir(run_dir), [])
        for rel in ("shots.json", "candidates.csv", "frame_scores.csv", "run.json", "report.md"):
            self.assertGreater((run_dir / rel).stat().st_size, 0, rel)
        self.assertTrue((run_dir / "raw").is_dir() and (run_dir / "frames").is_dir())
        for raw in ("analysis_metadata.txt", "pass_a_stderr.txt", "pass_b_blackdetect_log.txt", "pass_c_stderr.txt",
                    "thumbs_gray.bin", "ffmpeg_version.txt"):
            self.assertTrue((run_dir / "raw" / raw).is_file(), f"raw diagnostic output missing: {raw}")
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["output_sha256"], shots._sha_tree(run_dir))
        self.assertEqual(run["source_before"], run["source_after"])
        self.assertIn("ffmpeg version", run["tools"]["ffmpeg"])
        self.assertEqual({p["name"] for p in run["processes"]} >= {"pass_a_scdet_signalstats", "pass_b_blackdetect", "pass_c_thumbnails"}, True)
        self.assertTrue(all(p["returncode"] in (0, "partial") for p in run["processes"]))
        latest = json.loads((st.mdir_of(res) / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], res["run_id"])
        self.assertEqual(list(st.mdir_of(res).glob(".r_*.part")), [])
        self.assertEqual(st.snap(), st.source_snapshot, "an original was modified")
        doc = json.loads((run_dir / "shots.json").read_text(encoding="utf-8"))
        self.assertIn("not a calibrated probability", doc["confidence_note"])
        for ref in [e for s in doc["shots"] for e in s["evidence"]]:
            self.assertTrue((run_dir / ref).is_file(), ref)
        return doc, run_dir

    def assert_failed_cleanly(self, st, clip, res, expect=None):
        self.assertEqual(res["status"], "failed", res)
        if expect:
            self.assertIn(expect.lower(), res["reason"].lower())
        self.assertIsNone(res["run_dir"])
        md = st.mdir(clip)
        self.assertEqual([p.name for p in md.glob("r_*")], [], "a failed run must never leave a result folder")
        self.assertFalse((md / "latest.json").exists())
        self.assertTrue(res["diagnostics"])
        fail = json.loads((st.project.dir / res["diagnostics"] / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(fail["status"], "failed")
        self.assertIn("NOT A VALID RESULT", fail["note"])
        states = {r["state"] for r in shots.list_runs(st.project)}
        self.assertNotIn("complete", states)
        self.assertEqual(st.snap(), st.source_snapshot, "an original was modified")


def _mdir_of(self, res):
    return self.project.dir / "analysis" / "shots" / res["id"]


Studio.mdir_of = _mdir_of


class TestSettingsAndIdentity(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Project.create(Path(tmp.name), "demo")

    def test_defaults_valid_and_hash_is_stable(self):
        s = shots.load_settings(self.project)
        self.assertEqual(s, shots.validate_settings(json.loads(json.dumps(shots.DEFAULT_SETTINGS))))
        self.assertEqual(shots.settings_hash(s), shots.settings_hash(shots.load_settings(self.project)))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{8}", shots.settings_hash(s)))
        self.assertNotEqual(shots.settings_hash(s), shots.settings_hash(shots.load_settings(self.project, {"candidate_floor": 6})))

    def test_overrides_and_project_file_merge_in_order(self):
        (self.project.dir / shots.SETTINGS_FILE).write_text(json.dumps({"threads": 3, "candidate_floor": 7}), encoding="utf-8")
        s = shots.load_settings(self.project, {"threads": 4})
        self.assertEqual((s["threads"], s["candidate_floor"]), (4, 7.0))
        self.assertIsInstance(s["candidate_floor"], float)

    def test_invalid_settings_are_refused(self):
        bad = [{"nope": 1}, {"threads": 0}, {"threads": 9}, {"threads": True}, {"threads": "2"}, {"candidate_floor": -1},
               {"candidate_floor": 101}, {"candidate_floor": float("nan")}, {"candidate_floor": True}, {"scale_height": 3},
               {"scale_height": "360"}, {"group_max_gap": -1}, {"classifier": {"not_a_threshold": 1}},
               {"classifier": {"cut_score": "x"}}, {"classifier": {"cut_score": True}}, {"classifier": []}]
        for over in bad:
            with self.assertRaises(shots.ShotsError, msg=str(over)):
                shots.load_settings(self.project, over)
        (self.project.dir / shots.SETTINGS_FILE).write_text("{not json", encoding="utf-8")
        with self.assertRaises(shots.ShotsError):
            shots.load_settings(self.project)

    def test_scale_height_may_be_null(self):
        self.assertIsNone(shots.load_settings(self.project, {"scale_height": None})["scale_height"])

    def test_classifier_overrides_reach_the_thresholds(self):
        s = shots.load_settings(self.project, {"classifier": {"cut_score": 12, "cut_max_len": 3}})
        th = shots.thresholds_from(s)
        self.assertEqual((th.cut_score, th.cut_max_len), (12.0, 3))
        self.assertIsInstance(th.cut_max_len, int)

    def test_run_id_is_deterministic_and_tracks_identity_and_settings(self):
        item = {"file": {"size": 100, "mtime_ns": 5, "sha256": "ab" * 32}, "path": "/x"}
        s = shots.load_settings(self.project)
        rid = shots.run_id_for(item, s)
        self.assertRegex(rid, r"^r_[0-9a-f]{8}_[0-9a-f]{8}$")
        self.assertEqual(rid, shots.run_id_for(json.loads(json.dumps(item)), s))
        for change in ({"size": 101}, {"mtime_ns": 6}, {"sha256": None}):
            self.assertNotEqual(rid, shots.run_id_for({"file": {**item["file"], **change}, "path": "/x"}, s))
        self.assertNotEqual(rid, shots.run_id_for(item, shots.load_settings(self.project, {"threads": 4})))


class TestSimilarityAndHelpers(unittest.TestCase):
    @staticmethod
    def texture(seed, n=576):
        x, out = seed, []
        for _ in range(n):
            x = (1103515245 * x + 12345) & 0x7FFFFFFF
            out.append(40 + (x >> 8) % 180)
        return bytes(out)

    def test_similarity_cases(self):
        a, b = self.texture(1), self.texture(2)
        self.assertEqual(shots.frame_similarity(a, a), 1.0)
        shifted = bytes(min(255, v + 30) for v in a)                      # same picture, brighter
        self.assertGreater(shots.frame_similarity(a, shifted), 0.95)
        self.assertLess(shots.frame_similarity(a, b), 0.3)                # unrelated pictures
        self.assertEqual(shots.frame_similarity(bytes([80] * 576), bytes([82] * 576)), 1.0)   # flat, same level
        self.assertEqual(shots.frame_similarity(bytes([20] * 576), bytes([200] * 576)), 0.0)  # flat, different level
        self.assertEqual(shots.frame_similarity(bytes([80] * 576), a), 0.0)                   # flat vs textured
        self.assertEqual(shots.frame_similarity(a, b), shots.frame_similarity(a, b))
        for x, y in ((b"", b""), (a, a[:-1])):
            with self.assertRaises(ValueError):
                shots.frame_similarity(x, y)

    def test_thumbs_random_access(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.bin"
            p.write_bytes(bytes(range(12)))
            t = shots.Thumbs(p, 2, 2)
            self.assertEqual((t.count, t.exact), (3, True))
            self.assertEqual(t.get(1), bytes([4, 5, 6, 7]))
            self.assertIsNone(t.get(3))
            self.assertIsNone(t.get(-1))
            p.write_bytes(bytes(range(13)))
            self.assertFalse(shots.Thumbs(p, 2, 2).exact)

    def test_default_runner_bounds_priority_and_never_overwrites(self):
        rc, err = shots.default_runner([sys.executable, "-c", "import sys; sys.stderr.write('e')"], 30)
        self.assertEqual((rc, err), (0, b"e"))
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "o.bin"
            rc, _ = shots.default_runner([sys.executable, "-c", "print('hi')"], 30, out)
            self.assertEqual((rc, out.read_bytes().strip()), (0, b"hi"))
            with self.assertRaises(FileExistsError):                         # exclusive create: never overwrites
                shots.default_runner([sys.executable, "-c", "print('x')"], 30, out)
            self.assertEqual(out.read_bytes().strip(), b"hi")
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            shots.default_runner([sys.executable, "-c", "import time; time.sleep(60)"], 1)
        self.assertLess(time.monotonic() - t0, 20)                           # the process was killed, not waited for

    def test_processes_are_started_without_a_shell_at_low_priority(self):
        seen = {}

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                return None, b""

        def fake_popen(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            return FakeProc()
        with mock.patch.object(subprocess, "Popen", side_effect=fake_popen):
            self.assertEqual(shots.default_runner(["ffmpeg", "-i", "a b.mkv"], 5), (0, b""))
        self.assertIsInstance(seen["argv"], list)
        self.assertFalse(seen["kw"].get("shell", False))
        self.assertIs(seen["kw"]["stdin"], subprocess.DEVNULL)
        if os.name == "nt":
            self.assertTrue(seen["kw"]["creationflags"] & subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            self.assertTrue(callable(seen["kw"]["preexec_fn"]))

    def test_only_own_part_folders_may_be_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            md = Path(d)
            ok = md / ".r_aaaaaaaa_bbbbbbbb.part"
            ok.mkdir()
            (ok / "x").write_text("1")
            shots._discard_own_part(md, ok)
            self.assertFalse(ok.exists())
            for bad in (md / "r_aaaaaaaa_bbbbbbbb", md / ".other.part", md / "sub" / ".r_x.part"):
                bad.mkdir(parents=True, exist_ok=True)
                with self.assertRaises(shots.ShotsError):
                    shots._discard_own_part(md, bad)
                self.assertTrue(bad.exists())


def cls(cid, category, first, last, conf=0.8):
    return sc.Classification(cid, category, sc.BOUNDARY_KIND[category], rejected=category in ("flash", "camera_motion", "lighting_change"),
                             needs_review=category == "uncertain", confidence=conf, confidence_basis="t", rule="T",
                             reasons=("r",), first_frame=first, last_frame=last, metrics={})


class TestConflictResolution(unittest.TestCase):
    def test_cuts_inside_or_right_after_a_transition_are_demoted_not_dropped(self):
        items = [cls("a", "dissolve", 20, 29), cls("b", "hard_cut", 25, 25), cls("c", "hard_cut", 30, 30),
                 cls("d", "hard_cut", 40, 40), cls("e", "flash", 50, 52)]
        out = shots._resolve_conflicts(items, 100)
        self.assertEqual([c.candidate_id for c in out], ["a", "b", "c", "d", "e"])
        self.assertEqual([c.category for c in out], ["dissolve", "uncertain", "uncertain", "hard_cut", "flash"])
        for c in out[1:3]:
            self.assertEqual((c.rule, c.boundary_kind, c.needs_review), ("ORCH_conflict", None, True))
            self.assertLessEqual(c.confidence, 0.2)
            self.assertIn("originally classified hard_cut", c.reasons[-1])
        shots_, trans = sc.build_shots(100, [c.first_frame for c in out if c.boundary_kind == "cut"],
                                       [(c.category, c.first_frame, c.last_frame, c.candidate_id) for c in out if c.boundary_kind == "transition"])
        self.assertEqual(len(trans), 1)
        self.assertEqual([s.length for s in shots_], [20, 10, 60])       # 0-19 | transition 20-29 | 30-39 | 40-99

    def test_overlapping_transitions_keep_the_earlier_one(self):
        out = shots._resolve_conflicts([cls("a", "fade_out", 10, 20), cls("b", "dissolve", 18, 25), cls("c", "dissolve", 30, 35)], 100)
        self.assertEqual([c.category for c in out], ["fade_out", "uncertain", "dissolve"])
        self.assertIn("overlaps transition a", out[1].reasons[-1])

    def test_cut_outside_the_stream_is_demoted(self):
        out = shots._resolve_conflicts([cls("a", "hard_cut", 0, 0), cls("b", "hard_cut", 100, 100)], 100)
        self.assertEqual([c.category for c in out], ["uncertain", "uncertain"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class TestPlan(OrchestrationCase):
    def test_plan_is_read_only_deterministic_and_runs_nothing(self):
        st = Studio(self, ["hardcuts"])
        before = tree_state(st.root)
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("plan must not start a process")):
            rows = st.plan()
            again = st.plan()
        self.assertEqual(rows, again)
        self.assertEqual(tree_state(st.root), before, "plan changed files")
        self.assertFalse((st.project.dir / "analysis" / "shots").exists())
        r = rows[0]
        self.assertEqual((r["action"], r["expected_frames"], r["fps"], r["warnings"]), ("detect", 90, "25", []))
        self.assertRegex(r["run_id"], r"^r_[0-9a-f]{8}_[0-9a-f]{8}$")
        self.assertEqual(r["source_identity"]["size"], st.files["hardcuts"].stat().st_size)
        self.assertEqual(r["settings_hash"], shots.settings_hash(st.settings()))
        prefix = f"analysis/shots/{r['id']}/{r['run_id']}/"
        self.assertTrue(all(o.startswith(prefix) for o in r["expected_outputs"]))
        self.assertEqual(sorted(o[len(prefix):] for o in r["expected_outputs"]),
                         ["candidates.csv", "frame_scores.csv", "frames/", "raw/", "report.md", "run.json", "shots.json"])
        self.assertNotEqual(shots.plan(st.project, st.settings(candidate_floor=6), st.prober, st.ffmpeg)[0]["run_id"], r["run_id"])

    def test_plan_reports_blocked_inputs_with_reasons(self):
        def blocked(mutate, expect, **kw):
            st = Studio(self, ["hardcuts"])
            mutate(st)
            row = st.plan(**kw)[0]
            self.assertEqual(row["action"], "blocked", row)
            self.assertIn(expect, row["reason"])

        blocked(lambda st: st.files["hardcuts"].write_bytes(st.files["hardcuts"].read_bytes() + b"x"), "differs from the ingestion manifest")
        blocked(lambda st: os.utime(st.files["hardcuts"], ns=(1, 2 * 10 ** 18)), "differs from the ingestion manifest")
        blocked(lambda st: st.files["hardcuts"].unlink(), "missing")
        blocked(lambda st: None, "ffmpeg not found", ffmpeg=None)
        blocked(lambda st: None, "ffprobe is not available", prober=media.Prober(reason="gone"))

        def vfr(st):
            p = st.project.dir / "source_manifest.json"
            m = json.loads(p.read_text(encoding="utf-8"))
            m["items"][0]["warnings"].append({"code": "VARIABLE_FRAME_RATE", "message": "x"})
            p.write_text(json.dumps(m), encoding="utf-8")
        blocked(vfr, "variable frame rate")

        def unknown_rate(st):
            p = st.project.dir / "source_manifest.json"
            m = json.loads(p.read_text(encoding="utf-8"))
            m["items"][0]["video"][0]["frame_rate"]["kind"] = "unknown"
            p.write_text(json.dumps(m), encoding="utf-8")
        blocked(unknown_rate, "not a verified constant rate")

    def test_unprobeable_file_and_non_video_ids_are_refused(self):
        import wave
        buf = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, buf, True)
        wav = buf / "a.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 800)
        st = Studio(self, ["hardcuts"], extra={"bad.mkv": b"not a video at all " * 200, "a.wav": wav.read_bytes()})
        row = {r["name"]: r for r in st.plan()}["bad.mkv"]
        self.assertEqual(row["action"], "blocked")
        self.assertIn("no successful ffprobe record", row["reason"])
        res = st.detect("bad", runner=NoRunner())
        self.assertEqual(res["status"], "blocked")
        self.assertFalse((st.project.dir / "analysis" / "shots").exists())
        with self.assertRaises(shots.ShotsError):
            shots.plan(st.project, st.settings(), st.prober, st.ffmpeg, media_ids=[st.ids["a"]])      # audio, not video
        with self.assertRaises(shots.ShotsError):
            shots.plan(st.project, st.settings(), st.prober, st.ffmpeg, media_ids=["m_doesnotexist"])

    def test_manifest_from_another_project_is_refused(self):
        st = Studio(self, ["static"])
        p = st.project.dir / "source_manifest.json"
        m = json.loads(p.read_text(encoding="utf-8"))
        m["project_id"] = "00000000-0000-0000-0000-000000000000"
        p.write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaisesRegex(shots.ShotsError, "different project"):
            st.plan()
        with self.assertRaisesRegex(shots.ShotsError, "different project"):
            st.detect("static", runner=NoRunner())
        self.assertFalse((st.project.dir / "analysis" / "shots").exists())

    def test_plan_without_manifest_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proj = Project.create(Path(tmp.name), "empty")
        with self.assertRaises(shots.ShotsError):
            shots.plan(proj, shots.load_settings(proj))

    def test_plan_notes_existing_run_and_leftover_part(self):
        st = Studio(self, ["static"])
        res = st.detect("static")
        self.assertEqual(st.plan("static")[0]["action"], "exists")
        shutil.rmtree(st.project.dir / res["run_dir"])
        (st.mdir("static") / f".{res['run_id']}.part").mkdir()
        row = st.plan("static")[0]
        self.assertEqual(row["action"], "detect")
        self.assertTrue(any(".part" in w for w in row["warnings"]))

    def test_project_path_unusable_in_a_filter_is_blocked_before_anything_runs(self):
        st = Studio(self, ["static"], root_name="Creator,Studio")
        row = st.plan("static")[0]
        self.assertEqual(row["action"], "blocked")
        self.assertIn("filter", row["reason"])
        self.assertEqual(st.detect("static", runner=NoRunner())["status"], "blocked")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class TestSyntheticEndToEnd(OrchestrationCase):
    """One real detection run per scenario; frame numbers follow from how each clip is generated."""

    def run_clip(self, clip, **over):
        st = Studio(self, [clip])
        over.setdefault("max_evidence_events", 4)
        res = st.detect(clip, **over)
        doc, run_dir = self.assert_clean_run(st, res)
        return st, res, doc, run_dir

    def test_hard_cuts(self):
        st, res, doc, run_dir = self.run_clip("hardcuts")
        self.assertEqual(doc["total_frames"], 90)
        self.assertEqual([c["first_frame"] for c in doc["cuts"]], [30, 60])
        self.assertEqual([(s["start_frame"], s["end_frame"]) for s in doc["shots"]], [(0, 29), (30, 59), (60, 89)])
        self.assertEqual((doc["shots"][1]["start_time"], doc["shots"][1]["end_time"], doc["shots"][1]["duration"]),
                         ("1.200000", "2.400000", "1.200000"))
        self.assertEqual([s["detection_method"] for s in doc["shots"]][0], "scdet+signalstats+thumbnail_similarity+blackdetect")
        self.assertEqual((doc["transitions"], doc["rejected_candidates"], doc["uncertain_candidates"]), ([], [], []))
        for c in doc["cuts"]:
            self.assertEqual((c["category"], c["boundary_kind"], c["rule"]), ("hard_cut", "cut", "R4_hard_cut"))
            self.assertTrue(0 < c["confidence"] <= 0.95)
            self.assertEqual(c["sim_across"], 0.0)
            self.assertTrue(c["reasons"])
        self.assertEqual([s["start_boundary"] for s in doc["shots"]], ["stream_start", doc["cuts"][0]["candidate_id"], doc["cuts"][1]["candidate_id"]])
        self.assertEqual(len((run_dir / "frame_scores.csv").read_text(encoding="utf-8").splitlines()), 91)
        self.assertEqual(len((run_dir / "candidates.csv").read_text(encoding="utf-8").splitlines()), 3)
        self.assertGreaterEqual(len(list((run_dir / "frames").glob("*.jpg"))), 2)
        self.assertEqual(doc["fps"], "25")
        self.assertEqual(doc["summary"]["candidates_total"], 2)

    def test_very_short_shot_of_three_frames_is_kept(self):
        _, _, doc, _ = self.run_clip("shortshot")
        self.assertEqual([c["first_frame"] for c in doc["cuts"]], [20, 23])
        self.assertEqual([s["frames"] for s in doc["shots"]], [20, 3, 20])
        self.assertEqual(doc["shots"][1]["duration"], "0.120000")

    def test_two_frame_shot_is_retained_as_uncertain_never_lost(self):
        _, _, doc, run_dir = self.run_clip("twoframe")
        self.assertEqual(doc["summary"]["candidates_total"], 1)
        u = doc["uncertain_candidates"]
        self.assertEqual([(c["first_frame"], c["last_frame"], c["category"]) for c in u], [(20, 22, "uncertain")])
        self.assertTrue(u[0]["needs_review"])
        self.assertTrue(any("longer than a cut" in r for r in u[0]["reasons"]))
        self.assertEqual((doc["cuts"], len(doc["shots"])), ([], 1))
        self.assertIn(u[0]["candidate_id"], (run_dir / "candidates.csv").read_text(encoding="utf-8"))

    def test_flash_is_rejected_but_kept(self):
        _, _, doc, _ = self.run_clip("flash")
        self.assertEqual([(c["category"], c["first_frame"], c["last_frame"]) for c in doc["rejected_candidates"]], [("flash", 25, 27)])
        self.assertTrue(doc["rejected_candidates"][0]["rejected"])
        self.assertEqual((doc["cuts"], doc["transitions"], len(doc["shots"])), ([], [], 1))
        self.assertIsNone(doc["shots"][0]["confidence"])

    def test_dissolve_is_a_transition_not_a_cut(self):
        _, _, doc, _ = self.run_clip("dissolve")
        self.assertEqual(doc["cuts"], [])
        self.assertEqual(len(doc["transitions"]), 1)
        t = doc["transitions"][0]
        self.assertEqual(t["kind"], "dissolve")
        self.assertTrue(19 <= t["start_frame"] <= 23 and 8 <= t["end_frame"] - t["start_frame"] + 1 <= 12, t)
        self.assertEqual((t["shot_before"], t["shot_after"]), ("s0001", "s0002"))
        self.assertEqual(len(doc["shots"]), 2)
        self.assertEqual(doc["shots"][0]["end_frame"], t["start_frame"] - 1)
        self.assertEqual(doc["shots"][1]["start_frame"], t["end_frame"] + 1)

    def test_fade_in_and_out(self):
        _, _, doc, _ = self.run_clip("fades")
        self.assertEqual([t["kind"] for t in doc["transitions"]], ["fade_in", "fade_out"])
        fi, fo = doc["transitions"]
        self.assertTrue(fi["end_frame"] < fo["start_frame"])
        self.assertEqual(doc["cuts"], [])
        self.assertTrue(all(t["evidence_frames"] for t in doc["transitions"]))

    def test_static_footage_has_no_candidates_and_is_still_a_valid_run(self):
        _, _, doc, run_dir = self.run_clip("static")
        self.assertEqual(doc["summary"]["candidates_total"], 0)
        self.assertEqual(len(doc["shots"]), 1)
        self.assertEqual((doc["shots"][0]["start_frame"], doc["shots"][0]["end_frame"]), (0, 49))
        self.assertEqual(len((run_dir / "candidates.csv").read_text(encoding="utf-8").splitlines()), 1)   # header only
        self.assertEqual(list((run_dir / "frames").glob("*")), [])

    def test_cut_followed_by_dissolve(self):
        _, _, doc, _ = self.run_clip("mixed")
        self.assertEqual([c["first_frame"] for c in doc["cuts"]], [30])
        self.assertEqual([t["kind"] for t in doc["transitions"]], ["dissolve"])
        self.assertEqual(len(doc["shots"]), 3)
        self.assertEqual(doc["shots"][1]["end_frame"] + 1, doc["transitions"][0]["start_frame"])

    def test_derivation_is_deterministic_and_matches_the_published_result(self):
        st, res, doc, run_dir = self.run_clip("mixed")
        text = (run_dir / "raw" / "analysis_metadata.txt").read_text(encoding="ascii")
        scd, sig = sm.parse_scdet(text), sm.parse_signalstats(text)
        black = sm.parse_blackdetect_log((run_dir / "raw" / "pass_b_blackdetect_log.txt").read_text(encoding="utf-8"))
        th = shots.Thumbs(run_dir / "raw" / "thumbs_gray.bin", 32, 18)
        settings = st.settings()
        a = shots.derive(scd.frames, sig.frames, black.intervals, th, settings, Fraction(25))
        b = shots.derive(scd.frames, sig.frames, black.intervals, th, settings, Fraction(25))
        self.assertEqual(a["classifications"], b["classifications"])
        self.assertEqual((a["shots"], a["transitions"]), (b["shots"], b["transitions"]))
        self.assertEqual([c.first_frame for c in a["classifications"] if c.boundary_kind == "cut"], [c["first_frame"] for c in doc["cuts"]])
        self.assertEqual(len(a["shots"]), len(doc["shots"]))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class TestSafetyAndRecovery(OrchestrationCase):
    def test_blocked_input_starts_no_process_and_creates_no_output(self):
        st = Studio(self, ["static"])
        st.files["static"].write_bytes(st.files["static"].read_bytes() + b"x")         # changed after ingest
        res = st.detect("static", runner=NoRunner())
        self.assertEqual(res["status"], "blocked")
        self.assertIn("differs from the ingestion manifest", res["reason"])
        self.assertFalse((st.project.dir / "analysis" / "shots").exists())
        self.assertEqual(st.detect("static", runner=NoRunner(), ffmpeg=None)["status"], "blocked")

    def test_source_that_changes_during_processing_is_refused(self):
        for label, mutate in (("mtime", lambda p: os.utime(p, ns=(1, 3 * 10 ** 18))),
                              ("size", lambda p: p.write_bytes(p.read_bytes() + b"x"))):
            for after_pass in ("A", "C"):
                with self.subTest(change=label, after=after_pass):
                    st = Studio(self, ["static"])
                    runner = InjectRunner(after_pass, "after", lambda st=st, mutate=mutate: mutate(st.files["static"]))
                    res = st.detect("static", runner=runner)
                    st.source_snapshot = st.snap()               # the test itself changed the file; compare from here
                    self.assertEqual(res["status"], "failed")
                    self.assertIn("changed while being processed", res["reason"])
                    self.assertEqual([p.name for p in st.mdir("static").glob("r_*")], [])
                    self.assertFalse((st.mdir("static") / "latest.json").exists())

    def test_ffmpeg_failures_never_publish_and_a_rerun_recovers(self):
        cases = [("version", "rc", "ffmpeg_version"), ("A", "rc", "pass_a"), ("B", "rc", "pass_b"), ("C", "rc", "pass_c"),
                 ("A", "timeout", "did not finish"), ("C", "timeout", "did not finish"), ("A", "oserror", "could not start")]
        for on, action, expect in cases:
            with self.subTest(on=on, action=action):
                st = Studio(self, ["hardcuts"])
                res = st.detect("hardcuts", runner=InjectRunner(on, action))
                self.assert_failed_cleanly(st, "hardcuts", res, expect)
                ok = st.detect("hardcuts")
                self.assert_clean_run(st, ok)
                states = sorted(r["state"] for r in shots.list_runs(st.project))
                self.assertEqual(states, ["complete", "failed"])
                self.assertEqual([f for f in shots.check_runs(st.project) if f["severity"] == "critical"], [])

    def test_keyboard_interrupt_is_not_swallowed_and_nothing_is_published(self):
        st = Studio(self, ["hardcuts"])
        st.project.start_stage(shots.STAGE)
        with self.assertRaises(KeyboardInterrupt):
            st.detect("hardcuts", runner=InjectRunner("B", "kbd"))
        md = st.mdir("hardcuts")
        self.assertEqual([p.name for p in md.glob("r_*")], [])
        self.assertFalse((md / "latest.json").exists())
        rows = shots.list_runs(st.project)
        self.assertEqual([r["state"] for r in rows], ["failed"])
        op = list(st.project.load_state()["stages"][shots.STAGE]["operations"].values())[0]
        self.assertEqual(op["status"], "failed")
        self.assertIn("KeyboardInterrupt", op["result"])
        self.assertEqual(st.snap(), st.source_snapshot)
        self.assert_clean_run(st, st.detect("hardcuts"))

    def test_a_crash_leaves_only_an_incomplete_part_folder_that_is_never_a_result(self):
        st = Studio(self, ["hardcuts"])
        rid = st.plan("hardcuts")[0]["run_id"]
        part = st.mdir("hardcuts") / f".{rid}.part"
        (part / "raw").mkdir(parents=True)
        (part / "shots.json").write_text("{}", encoding="utf-8")          # looks half-done
        (part / "raw" / "analysis_metadata.txt").write_text("frame:0", encoding="utf-8")
        rows = shots.list_runs(st.project)
        self.assertEqual([r["state"] for r in rows], ["incomplete"])
        self.assertNotIn("complete", [r["state"] for r in rows])
        self.assertEqual([f["code"] for f in shots.check_runs(st.project)], ["RUN_INCOMPLETE"])
        self.assertEqual(st.plan("hardcuts")[0]["action"], "detect")
        res = st.detect("hardcuts")
        doc, run_dir = self.assert_clean_run(st, res)
        self.assertFalse(part.exists())
        self.assertEqual(doc["summary"]["cuts"], 2)                       # the stale partial content was not reused

    def test_nothing_is_visible_as_final_until_the_last_step(self):
        st = Studio(self, ["static"])
        seen = {}

        def at_pass_c():
            md = st.mdir("static")
            seen["final"] = [p.name for p in md.glob("r_*")]
            seen["part"] = [p.name for p in md.glob(".r_*.part")]
            seen["states"] = [r["state"] for r in shots.list_runs(st.project)]
            seen["latest"] = (md / "latest.json").exists()
        res = st.detect("static", runner=InjectRunner("C", "after", at_pass_c))
        self.assertEqual(seen["final"], [])
        self.assertEqual(len(seen["part"]), 1)
        self.assertEqual(seen["states"], ["incomplete"])
        self.assertFalse(seen["latest"])
        self.assert_clean_run(st, res)

    def test_failed_publish_leaves_no_result_and_the_next_attempt_succeeds(self):
        st = Studio(self, ["hardcuts"])
        with mock.patch.object(shots.os, "rename", side_effect=OSError("disk full")):
            res = st.detect("hardcuts")
        self.assertEqual(res["status"], "failed")
        self.assertIn("disk full", res["reason"])
        md = st.mdir("hardcuts")
        self.assertEqual([p.name for p in md.glob("r_*")], [])
        self.assertFalse((md / "latest.json").exists())
        self.assertEqual([r["state"] for r in shots.list_runs(st.project)], ["incomplete"])   # the .part folder remains
        ok = st.detect("hardcuts")
        self.assert_clean_run(st, ok)
        self.assertEqual([r["state"] for r in shots.list_runs(st.project)], ["complete"])

    def test_existing_runs_are_never_touched(self):
        st = Studio(self, ["hardcuts"])
        first = st.detect("hardcuts")
        self.assert_clean_run(st, first)
        before = st.run_hashes(first)
        mtimes = tree_state(st.project.dir / first["run_dir"])
        again = st.detect("hardcuts", runner=NoRunner())
        self.assertEqual((again["status"], again["run_id"]), ("skipped", first["run_id"]))
        self.assertEqual(st.run_hashes(first), before)
        self.assertEqual(tree_state(st.project.dir / first["run_dir"]), mtimes)
        # a later failing run with different settings must not disturb the completed one or the latest pointer
        bad = st.detect("hardcuts", runner=InjectRunner("C", "rc"), candidate_floor=6)
        self.assertEqual(bad["status"], "failed")
        self.assertEqual(st.run_hashes(first), before)
        latest = json.loads((st.mdir("hardcuts") / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], first["run_id"])

    def test_output_name_collisions_are_not_overwritten(self):
        for label, make in (("empty folder", lambda p: p.mkdir()), ("a file", lambda p: p.write_text("mine", encoding="utf-8"))):
            with self.subTest(final_path_is=label):
                st = Studio(self, ["static"])
                rid = st.plan("static")[0]["run_id"]
                final = st.mdir("static") / rid
                final.parent.mkdir(parents=True)
                make(final)
                res = st.detect("static", runner=NoRunner())
                self.assertEqual(res["status"], "skipped")
                self.assertEqual(final.read_text(encoding="utf-8") if final.is_file() else sorted(final.iterdir()),
                                 "mine" if final.is_file() else [])
        st = Studio(self, ["static"])                                      # appears while the job is running
        rid = st.plan("static")[0]["run_id"]
        runner = InjectRunner("C", "after", lambda: (st.mdir("static") / rid).mkdir())
        res = st.detect("static", runner=runner)
        self.assertEqual(res["status"], "failed")
        self.assertIn("appeared", res["reason"])
        self.assertEqual(sorted((st.mdir("static") / rid).iterdir()), [])  # the foreign folder was left alone

    def test_malformed_or_incomplete_ffmpeg_output_fails_the_run(self):
        def truncate_meta_mid_frame(st):
            def f():
                p = st.part_file("hardcuts", "raw/analysis_metadata.txt")
                t = p.read_text(encoding="ascii")
                p.write_text(t[:t.index("frame:40 ")] + "frame:40   pts:1600  pts_time:1.6\nlavfi.scd.mafd=0.0", encoding="ascii")
            return f

        def keep_first_frames(st):
            def f():
                p = st.part_file("hardcuts", "raw/analysis_metadata.txt")
                t = p.read_text(encoding="ascii")
                p.write_text(t[:t.index("frame:40 ")], encoding="ascii")
            return f

        def replace_meta(text):
            return lambda st: (lambda: st.part_file("hardcuts", "raw/analysis_metadata.txt").write_text(text, encoding="ascii"))

        def drop_signalstats(st):
            def f():
                p = st.part_file("hardcuts", "raw/analysis_metadata.txt")
                p.write_text("\n".join(l for l in p.read_text(encoding="ascii").splitlines() if "signalstats" not in l) + "\n", encoding="ascii")
            return f

        def cut_thumbs(n):
            def mk(st):
                def f():
                    with open(st.part_file("hardcuts", "raw/thumbs_gray.bin"), "r+b") as fh:
                        fh.truncate(max(0, fh.seek(0, 2) - n) if n else 0)
                return f
            return mk

        cases = [("A", "metadata cut mid-frame", truncate_meta_mid_frame, "unusable"),
                 ("A", "only the first 40 frames", keep_first_frames, "analysed 40 frames"),
                 ("A", "garbage text", replace_meta("this is not metadata\n"), "unusable"),
                 ("A", "empty file", replace_meta(""), "unusable"),
                 ("A", "signalstats missing", drop_signalstats, "signalstats output unusable"),
                 ("C", "thumbnail data short by 7 bytes", cut_thumbs(7), "thumbnail data"),
                 ("C", "thumbnail data empty", cut_thumbs(0), "thumbnail data")]
        for on, label, make, expect in cases:
            with self.subTest(label):
                st = Studio(self, ["hardcuts"])
                res = st.detect("hardcuts", runner=InjectRunner(on, "after", make(st)))
                self.assert_failed_cleanly(st, "hardcuts", res, expect)
        for payload, label in ((b"junk without a completion line", "blackdetect log cut short"), (b"", "blackdetect log empty")):
            with self.subTest(label):
                st = Studio(self, ["hardcuts"])
                res = st.detect("hardcuts", runner=InjectRunner("B", "stub", payload))
                self.assert_failed_cleanly(st, "hardcuts", res, "blackdetect output unusable")

    def test_missing_evidence_images_are_a_warning_not_a_failure(self):
        st = Studio(self, ["hardcuts"])
        res = st.detect("hardcuts", runner=InjectRunner("E", "rc"), max_evidence_events=4)
        doc, run_dir = self.assert_clean_run(st, res)
        self.assertTrue(any("evidence frame" in w for w in doc["warnings"]))
        self.assertEqual(list((run_dir / "frames").glob("*")), [])
        self.assertEqual(doc["summary"]["cuts"], 2)

    def test_operation_journal_is_used_only_when_the_stage_is_in_progress(self):
        st = Studio(self, ["static"])
        st.detect("static")
        self.assertEqual(st.project.load_state()["stages"][shots.STAGE]["operations"], {})       # no stage: no journal, no error
        st2 = Studio(self, ["static"])
        st2.project.start_stage(shots.STAGE)
        bad = st2.detect("static", runner=InjectRunner("B", "rc"))
        ops = st2.project.load_state()["stages"][shots.STAGE]["operations"]
        self.assertEqual([o["status"] for o in ops.values()], ["failed"])
        ok = st2.detect("static")
        ops = st2.project.load_state()["stages"][shots.STAGE]["operations"]
        (op,) = ops.values()
        self.assertEqual((bad["status"], ok["status"], op["status"], op["attempts"], op["destructive"]), ("failed", "completed", "completed", 2, False))
        self.assertTrue(ok["run_id"] in list(ops)[0])

    def test_check_runs_finds_tampering_and_a_changed_source(self):
        st = Studio(self, ["hardcuts"])
        res = st.detect("hardcuts")
        self.assert_clean_run(st, res)
        self.assertEqual(shots.check_runs(st.project), [])
        run_dir = st.project.dir / res["run_dir"]
        (run_dir / "frame_scores.csv").write_text("edited\n", encoding="utf-8")
        codes = {f["code"] for f in shots.check_runs(st.project)}
        self.assertIn("RUN_OUTPUT_PROBLEM", codes)
        st2 = Studio(self, ["hardcuts"])
        self.assert_clean_run(st2, st2.detect("hardcuts"))
        st2.files["hardcuts"].write_bytes(st2.files["hardcuts"].read_bytes() + b"x")
        self.assertIn("SOURCE_CHANGED", {f["code"] for f in shots.check_runs(st2.project)})

    def test_every_run_records_its_settings_hashes_and_process_outcomes(self):
        st = Studio(self, ["hardcuts"])
        res = st.detect("hardcuts", candidate_floor=6, group_max_gap=1)
        doc, run_dir = self.assert_clean_run(st, res)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["settings"]["candidate_floor"], 6.0)
        self.assertEqual(run["settings_hash"], shots.settings_hash(st.settings(candidate_floor=6, group_max_gap=1)))
        self.assertEqual(run["frames_analysed"], 90)
        self.assertIn("raw/thumbs_gray.bin", run["output_sha256"])
        passes = [p for p in run["processes"] if p["name"].startswith("pass_")]
        self.assertEqual(len(passes), 3)
        for p in passes:
            a = p["argv"]
            self.assertIsInstance(a, list)                      # argument lists, never a shell string
            self.assertNotIn("-y", a)                           # FFmpeg is never told to overwrite
            self.assertIn("-nostdin", a)
            self.assertEqual(a[a.index("-threads") + 1], "1")   # bounded decoder threads
            self.assertEqual(a[a.index("-filter_threads") + 1], "1")
            self.assertEqual(a[a.index("-map") + 1], "0:v:0")   # only the first video stream; no audio/other streams
            self.assertIn("-an", a)
        self.assertIn("not validated on real footage", doc["validation_note"].lower())


# =====================================================================================================
# CLI integration (python -m creator_state shots-plan | shots-detect | shots-status | shots-check)
# =====================================================================================================
def cli(st, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err):
        code = cli_main(["--root", str(st.root), *argv], out=out)
    return code, out.getvalue(), err.getvalue()


def no_process():
    return mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process was started"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class TestShotsCli(OrchestrationCase):
    FAST = ("--threads", "1", "--evidence", "0", "--full-size")

    def test_plan_lists_every_video_and_is_completely_dry(self):
        st = Studio(self, ["hardcuts", "static"])
        before = tree_state(st.root)
        with no_process():
            code, out, err = cli(st, "shots-plan", "synth")
            jcode, jout, _ = cli(st, "shots-plan", "synth", "--json", "--floor", "7", "--threads", "1")
            icode, iout, _ = cli(st, "shots-plan", "synth", "--id", st.ids["static"])
        self.assertEqual((code, jcode, icode), (0, 0, 0), (out, err))
        self.assertEqual(out.count("DETECT"), 2)
        for clip in ("hardcuts", "static"):
            self.assertIn(f"{clip}.mkv", out)
            self.assertIn(st.ids[clip], out)
        self.assertIn("(90 frames @ 25 fps)", out)
        self.assertIn("Nothing was run or written", out)
        self.assertEqual(tree_state(st.root), before, "a dry run changed files")
        self.assertFalse((st.project.dir / "analysis" / "shots").exists())
        j = json.loads(jout)
        self.assertEqual((j["settings"]["candidate_floor"], j["settings"]["threads"]), (7.0, 1))
        self.assertEqual({r["action"] for r in j["rows"]}, {"detect"})
        self.assertEqual(iout.count("DETECT"), 1)
        self.assertNotIn("hardcuts.mkv", iout)

    def test_detection_needs_explicit_confirmation(self):
        st = Studio(self, ["hardcuts"])
        before = tree_state(st.root)
        with no_process():
            code, out, err = cli(st, "shots-detect", "synth", *self.FAST)
        self.assertEqual(code, 0, err)
        self.assertIn("Re-run with --yes", out)
        self.assertNotIn("COMPLETED", out)
        self.assertEqual(tree_state(st.root), before)
        self.assertFalse((st.project.dir / "analysis" / "shots").exists())

    def test_detect_reports_the_verified_result_then_skips_a_repeat(self):
        st = Studio(self, ["hardcuts"])
        mid = st.ids["hardcuts"]
        code, out, err = cli(st, "shots-detect", "synth", "--yes", "--id", mid, *self.FAST)
        self.assertEqual(code, 0, (out, err))
        self.assertIn("COMPLETED", out)
        self.assertIn("3 shot(s), 2 hard cut(s), 0 transition(s); 0 rejected event(s) kept for audit, 0 uncertain candidate(s)", out)
        self.assertIn("not calibrated probabilities", out)
        self.assertIn("not journaled", out)                               # stage 'understand' was not in progress
        rows = shots.list_runs(st.project)
        self.assertEqual([r["state"] for r in rows], ["complete"])
        self.assertEqual(shots.verify_run_dir(st.project.dir / rows[0]["path"]), [])
        self.assertEqual(st.snap(), st.source_snapshot)
        code2, out2, _ = cli(st, "shots-detect", "synth", "--yes", "--id", mid, *self.FAST)
        self.assertEqual(code2, 0)
        self.assertIn("SKIPPED", out2)
        self.assertEqual(len(shots.list_runs(st.project)), 1)

    def test_journal_note_disappears_when_the_stage_is_in_progress(self):
        st = Studio(self, ["static"])
        st.project.start_stage(shots.STAGE)
        code, out, _ = cli(st, "shots-detect", "synth", "--yes", *self.FAST)
        self.assertEqual(code, 0)
        self.assertNotIn("not journaled", out)
        (op,) = st.project.load_state()["stages"][shots.STAGE]["operations"].values()
        self.assertEqual(op["status"], "completed")

    def test_blocked_preflight_is_explained_and_fails_the_command(self):
        st = Studio(self, ["hardcuts"])
        st.files["hardcuts"].write_bytes(st.files["hardcuts"].read_bytes() + b"x")
        pcode, pout, _ = cli(st, "shots-plan", "synth")
        self.assertEqual(pcode, 1)
        self.assertIn("BLOCKED", pout)
        self.assertIn("reason: source size or modified time differs from the ingestion manifest", pout)
        with no_process():                                    # blocked inputs must not start any process
            dcode, dout, derr = cli(st, "shots-detect", "synth", "--yes", *self.FAST)
        self.assertEqual(dcode, 1, (dout, derr))
        self.assertIn("BLOCKED", dout)
        self.assertIn("differs from the ingestion manifest", dout)
        self.assertNotIn("COMPLETED", dout)
        self.assertFalse((st.project.dir / "analysis" / "shots").exists())

    def test_failed_detection_returns_failure_and_keeps_only_diagnostics(self):
        st = Studio(self, ["hardcuts"])
        bogus = str(st.root / "no-such-ffmpeg.exe")
        code, out, err = cli(st, "shots-detect", "synth", "--yes", "--ffmpeg", bogus, *self.FAST)
        self.assertEqual(code, 1, (out, err))
        self.assertIn("FAILED", out)
        self.assertIn("could not start FFmpeg", out)
        self.assertIn("diagnostics kept at", out)
        self.assertNotIn("COMPLETED", out)
        md = st.mdir("hardcuts")
        self.assertEqual([p.name for p in md.glob("r_*")], [])
        self.assertFalse((md / "latest.json").exists())
        scode, sout, _ = cli(st, "shots-status", "synth")
        self.assertEqual(scode, 0)
        self.assertIn("FAILED", sout)
        self.assertNotIn(chr(10) + "  COMPLETE", sout)
        ok, okout, _ = cli(st, "shots-detect", "synth", "--yes", *self.FAST)               # a later good run still works
        self.assertEqual((ok, "COMPLETED" in okout), (0, True))

    def test_status_and_check_tell_complete_incomplete_and_failed_apart(self):
        st = Studio(self, ["hardcuts", "static", "flash"])
        self.assertEqual(cli(st, "shots-detect", "synth", "--yes", "--id", st.ids["static"], *self.FAST)[0], 0)
        self.assertEqual(cli(st, "shots-detect", "synth", "--yes", "--id", st.ids["flash"], "--ffmpeg", str(st.root / "nope.exe"), *self.FAST)[0], 1)
        (st.mdir("hardcuts") / ".r_aaaaaaaa_bbbbbbbb.part" / "raw").mkdir(parents=True)        # a crashed run's leftover
        code, out, _ = cli(st, "shots-status", "synth")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        def state_under(name):
            i = next(n for n, l in enumerate(lines) if l.startswith(name))
            return lines[i + 1].split()[0]
        self.assertEqual((state_under("static.mkv"), state_under("flash.mkv"), state_under("hardcuts.mkv")),
                         ("COMPLETE", "FAILED", "INCOMPLETE"))
        self.assertIn("(latest)", out)
        self.assertIn("Only COMPLETE runs are results", out)
        jcode, jout, _ = cli(st, "shots-status", "synth", "--json")
        self.assertEqual({r["state"] for r in json.loads(jout)}, {"complete", "incomplete", "failed"})
        only = cli(st, "shots-status", "synth", "--id", st.ids["flash"])[1]
        self.assertIn("FAILED", only)
        self.assertNotIn(chr(10) + "  COMPLETE", only)
        ccode, cout, _ = cli(st, "shots-check", "synth")
        self.assertEqual(ccode, 0, cout)                                       # incomplete = warning, failed = info
        self.assertIn("Checked 1 complete run(s)", cout)
        self.assertIn("RUN_INCOMPLETE", cout)
        self.assertIn("RUN_FAILED_DIAGNOSTICS", cout)
        run_dir = st.project.dir / next(r["path"] for r in shots.list_runs(st.project) if r["state"] == "complete")
        (run_dir / "frame_scores.csv").write_text("tampered\n", encoding="utf-8")
        ccode2, cout2, _ = cli(st, "shots-check", "synth")
        self.assertEqual(ccode2, 1)
        self.assertIn("CRITICAL", cout2)
        self.assertIn("RUN_OUTPUT_PROBLEM", cout2)
        self.assertEqual(cli(st, "shots-check", "synth", "--json")[0], 1)

    def test_status_and_check_with_no_runs(self):
        st = Studio(self, ["static"])
        code, out, _ = cli(st, "shots-status", "synth")
        self.assertEqual(code, 0)
        self.assertIn("No shot-detection runs yet", out)
        ccode, cout, _ = cli(st, "shots-check", "synth")
        self.assertEqual((ccode, "Findings: none" in cout), (0, True))

    def test_invalid_project_media_and_settings_fail_safely(self):
        import wave
        buf = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, buf, True)
        with wave.open(str(buf / "a.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 800)
        st = Studio(self, ["static"], extra={"a.wav": (buf / "a.wav").read_bytes()})
        before = tree_state(st.root)
        bad_runs = [
            (("shots-plan", "no-such-project"), "not found"),
            (("shots-detect", "no-such-project", "--yes"), "not found"),
            (("shots-status", "../../escape"), "not found"),
            (("shots-check", "no-such-project"), "not found"),
            (("shots-plan", "synth", "--id", "m_doesnotexist"), "not in the ingestion manifest"),
            (("shots-detect", "synth", "--yes", "--id", "m_doesnotexist"), "not in the ingestion manifest"),
            (("shots-plan", "synth", "--id", st.ids["a"]), "not videos"),
            (("shots-status", "synth", "--id", "m_doesnotexist"), "not in this project"),
            (("shots-check", "synth", "--id", "m_doesnotexist"), "not in this project"),
            (("shots-plan", "synth", "--threads", "0"), "threads"),
            (("shots-plan", "synth", "--floor", "500"), "candidate_floor"),
            (("shots-detect", "synth", "--yes", "--scale-height", "3"), "scale_height"),
        ]
        for argv, expect in bad_runs:
            with self.subTest(argv=argv):
                with no_process():
                    code, out, err = cli(st, *argv)
                self.assertEqual(code, 2, (out, err))
                self.assertIn(expect, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(out, "")
        self.assertEqual(tree_state(st.root), before)
        for argv in (("shots-plan", "synth", "--threads", "abc"), ("shots-plan", "synth", "--scale-height", "100", "--full-size"),
                     ("shots-plan", "synth", "--no-such-flag"), ("shots-detect",)):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    cli_main(["--root", str(st.root), *argv], out=io.StringIO())
                self.assertEqual(cm.exception.code, 2)
        for name in ("shots-plan", "shots-detect", "shots-status", "shots-check"):
            with self.subTest(help=name), contextlib.redirect_stdout(io.StringIO()) as o:
                with self.assertRaises(SystemExit) as cm:
                    cli_main([name, "--help"])
                self.assertEqual(cm.exception.code, 0)
            self.assertIn(name, o.getvalue())

    def test_a_completed_run_is_never_reported_if_its_output_does_not_verify(self):
        st = Studio(self, ["static"])
        real, calls = shots.verify_run_dir, []

        def verify(*a, **k):
            calls.append(1)
            return real(*a, **k) if len(calls) <= 2 else ["file hashes differ from run.json: ['shots.json']"]    # 3rd call = the CLI's own
        with mock.patch.object(shots, "verify_run_dir", side_effect=verify):
            code, out, _ = cli(st, "shots-detect", "synth", "--yes", *self.FAST)
        self.assertEqual(len(calls), 3)
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)
        self.assertIn("output verification failed", out)
        self.assertNotIn("COMPLETED", out)

    def test_ctrl_c_exits_130_without_a_traceback(self):
        st = Studio(self, ["static"])
        with mock.patch.object(shots, "detect", side_effect=KeyboardInterrupt()):
            code, out, err = cli(st, "shots-detect", "synth", "--yes", *self.FAST)
        self.assertEqual(code, 130)
        self.assertIn("Interrupted", err)
        self.assertNotIn("Traceback", err)

    def test_cli_contains_no_detection_logic(self):
        src = (Path(__file__).resolve().parents[1] / "creator_state" / "cli.py").read_text(encoding="utf-8")
        for forbidden in ("shot_classify", "shot_metrics", "subprocess", "scdet", "blackdetect", "signalstats"):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
