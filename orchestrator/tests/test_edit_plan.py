"""Offline tests for the edit-plan package (T-EDT-001). Synthetic data only: no Resolve, no network, no real media."""
import copy
import json
import re
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_director import schema as CS
from edit_plan import (SCHEMA_VERSION, Caption, EditPlan, ExportTarget, Marker, PlanMeta, PlanNotExecutable,
                       PlanSchemaError, QCExpectation, Rate, Rationale, Segment, SourceEntry, TimelineSettings, Track,
                       TranscriptRange, Transition, canonical_json, json_schema, plan_from_brief, plan_hash,
                       to_executor_requests, validate_plan, validate_plan_dict)
from resolve_executor import operations as ops

H = "a" * 64
MEDIA = "C:\\Synth\\Media\\a.mp4"
OUT = "C:\\Synth\\Out"
RAT = Rationale("test item", "brief-item-1")


def src(sid="S1", rate=Rate(25), dur=1000, origin="creator_recording", path=MEDIA, **kw):
    return SourceEntry(sid, path, rate, dur, H, origin, **kw)


def seg(sid, rin, a, b, **kw):
    d = dict(segment_id=sid, purpose="dialogue", track_id="v1", record_in=rin, rationale=RAT, source_id="S1",
             source_in=a, source_out=b, range_convention="exclusive_out", media="video_audio", audio_track_id="a1",
             speaker="creator")
    d.update(kw)
    return Segment(**d)


def base(**over) -> EditPlan:
    d = dict(version=SCHEMA_VERSION,
             meta=PlanMeta("proj", "plan-1", "tester", "brief-1", H),
             timeline=TimelineSettings("Synthetic Cut", Rate(25), 1920, 1080),
             sources=(src(),), tracks=(Track("v1", "video", 1), Track("a1", "audio", 1)),
             segments=(seg("s1", 0, 0, 50), seg("s2", 50, 50, 100)))
    d.update(over)
    return EditPlan(**d)


def report(plan, **kw):
    return validate_plan(plan, **kw)


class SchemaTests(unittest.TestCase):
    def test_round_trip_and_json(self):
        plan = base(markers=(Marker("m1", 10, RAT, "n"),), transitions=(Transition("t1", "cut", "s1", "s2", 0, RAT),),
                    qc_expectations=(QCExpectation("q1", "total_duration_frames", "", {"frames": 100}),),
                    exports=(ExportTarget("e1", OUT, "final"),))
        data = json.loads(json.dumps(plan.to_dict()))
        self.assertEqual(EditPlan.from_dict(data), plan)
        self.assertEqual(data["version"], "edit-plan/0.1")

    def test_unknown_field_rejected(self):
        d = base().to_dict()
        d["surprise"] = 1
        with self.assertRaises(PlanSchemaError) as cm:
            EditPlan.from_dict(d)
        self.assertEqual(cm.exception.code, "SCHEMA_UNKNOWN_FIELD")
        d = base().to_dict()
        d["segments"][0]["extra"] = 1
        with self.assertRaises(PlanSchemaError) as cm:
            EditPlan.from_dict(d)
        self.assertIn("/segments[0]", cm.exception.path)

    def test_unknown_or_missing_version_rejected(self):
        for v in ("edit-plan/0.2", None, 1):
            d = base().to_dict()
            d["version"] = v
            with self.assertRaises(PlanSchemaError) as cm:
                EditPlan.from_dict(d)
            self.assertEqual(cm.exception.code, "SCHEMA_UNKNOWN_VERSION")

    def test_missing_field_and_bad_types(self):
        d = base().to_dict()
        del d["timeline"]
        with self.assertRaises(PlanSchemaError) as cm:
            EditPlan.from_dict(d)
        self.assertEqual(cm.exception.code, "SCHEMA_MISSING_FIELD")
        d = base().to_dict()
        d["timeline"]["width"] = 1920.0
        with self.assertRaises(PlanSchemaError):
            EditPlan.from_dict(d)
        d = base().to_dict()
        d["segments"][0]["purpose"] = "voiceover"
        with self.assertRaises(PlanSchemaError):
            EditPlan.from_dict(d)
        with self.assertRaises(PlanSchemaError):
            EditPlan.from_dict([])

    def test_validate_dict_never_raises(self):
        for bad in (None, 5, [], {"version": "x"}, {"version": SCHEMA_VERSION}):
            rep = validate_plan_dict(bad)
            self.assertFalse(rep.ok)
            self.assertTrue(rep.errors[0].code.startswith("SCHEMA_"))
        self.assertTrue(validate_plan_dict(base().to_dict()).ok)

    def test_json_schema_export(self):
        s = json_schema()
        self.assertEqual(s["properties"]["version"], {"const": SCHEMA_VERSION})
        self.assertFalse(s["additionalProperties"])
        for name in ("Segment", "Caption", "Transition", "Marker", "Track", "SourceEntry", "QCExpectation", "Rate"):
            self.assertIn(name, s["$defs"])
        self.assertEqual(s["$defs"]["Segment"]["properties"]["purpose"]["enum"],
                         ["dialogue", "b_roll", "music", "sfx", "title"])
        json.dumps(s)


class FrameTests(unittest.TestCase):
    def test_valid_base_and_duration(self):
        r = report(base())
        self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(r.total_duration_frames, 100)
        self.assertEqual(r.gaps, ())
        self.assertEqual(r.warnings, ())

    def test_first_last_and_single_frame(self):
        one = report(base(segments=(seg("s1", 0, 0, 0, range_convention="inclusive_out"),)))
        self.assertTrue(one.ok, one.to_dict())
        self.assertEqual(one.total_duration_frames, 1)
        first = report(base(segments=(seg("s1", 0, 0, 49, range_convention="inclusive_out"),)))
        last = report(base(segments=(seg("s1", 0, 999, 999, range_convention="inclusive_out"),)))
        last_excl = report(base(segments=(seg("s1", 0, 999, 1000),)))
        for r in (first, last, last_excl):
            self.assertTrue(r.ok, r.to_dict())
        self.assertEqual(first.total_duration_frames, 50)
        past = report(base(segments=(seg("s1", 0, 999, 1000, range_convention="inclusive_out"),)))
        self.assertIn("RANGE_OUT_OF_SOURCE", past.codes("error"))

    def test_inclusive_and_exclusive_agree(self):
        a = report(base(segments=(seg("s1", 0, 10, 59, range_convention="inclusive_out"),)))
        b = report(base(segments=(seg("s1", 0, 10, 60),)))
        self.assertEqual(a.total_duration_frames, b.total_duration_frames)

    def test_range_errors(self):
        cases = {"RANGE_NEGATIVE": seg("s1", 0, -1, 10), "RANGE_INVERTED": seg("s1", 0, 20, 10),
                 "RANGE_EMPTY": seg("s1", 0, 20, 20), "RANGE_OUT_OF_SOURCE": seg("s1", 0, 990, 1001),
                 "RANGE_CONVENTION_MISSING": seg("s1", 0, 0, 10, range_convention=None),
                 "RECORD_NEGATIVE": seg("s1", -5, 0, 10),
                 "ZERO_DURATION": seg("s1", 0, 0, 1)}
        for code, s in cases.items():
            with self.subTest(code):
                p = base(segments=(s,))
                if code == "ZERO_DURATION":
                    p = replace(p, sources=(src(rate=Rate(60)),))
                self.assertIn(code, report(p).codes("error"))
        inc = seg("s1", 0, 20, 19, range_convention="inclusive_out")
        self.assertIn("RANGE_INVERTED", report(base(segments=(inc,))).codes("error"))

    def test_non_zero_start_timecode_is_relative(self):
        plan = base(timeline=TimelineSettings("Synthetic Cut", Rate(25), 1920, 1080, "01:00:00:00"))
        append = [o for o in to_executor_requests(plan) if isinstance(o, ops.AppendSegment)]
        self.assertEqual([a.record_frame for a in append], [90000, 90050])
        plan0 = base(timeline=TimelineSettings("Synthetic Cut", Rate(25), 1920, 1080, "00:00:00:00"))
        self.assertEqual(report(plan0).total_duration_frames, 100)
        append0 = [o for o in to_executor_requests(plan0) if isinstance(o, ops.AppendSegment)]
        self.assertEqual([a.record_frame for a in append0], [0, 50])

    def test_mixed_rates(self):
        exact = report(base(sources=(src(rate=Rate(50)),), segments=(seg("s1", 0, 0, 50),)))
        self.assertTrue(exact.ok, exact.to_dict())
        self.assertEqual(exact.total_duration_frames, 25)
        self.assertIn("RATE_MIXED", exact.codes("warning"))
        self.assertNotIn("RATE_CONVERSION_INEXACT", exact.codes())
        inexact = report(base(sources=(src(rate=Rate(30)),), segments=(seg("s1", 0, 0, 50),)))
        self.assertTrue(inexact.ok)
        self.assertEqual(inexact.total_duration_frames, 41)            # floor(50 * 25 / 30)
        self.assertIn("RATE_CONVERSION_INEXACT", inexact.codes("warning"))
        ntsc = report(base(sources=(src(rate=Rate(30000, 1001)),), segments=(seg("s1", 0, 0, 30),),
                           timeline=TimelineSettings("Synthetic Cut", Rate(30000, 1001), 1920, 1080)))
        self.assertTrue(ntsc.ok, ntsc.to_dict())
        self.assertEqual(ntsc.warnings, ())

    def test_overlap_and_gap(self):
        r = report(base(segments=(seg("s1", 0, 0, 50), seg("s2", 40, 50, 100))))
        self.assertIn("TRACK_OVERLAP", r.codes("error"))
        touching = report(base(segments=(seg("s1", 0, 0, 50), seg("s2", 50, 50, 100))))
        self.assertNotIn("TRACK_OVERLAP", touching.codes())
        gap = report(base(segments=(seg("s1", 0, 0, 50), seg("s2", 60, 50, 100))))
        self.assertTrue(gap.ok)
        self.assertIn("GAP", gap.codes("warning"))
        self.assertIn({"track_id": "v1", "start": 50, "end": 60}, gap.gaps)
        self.assertEqual(gap.total_duration_frames, 110)
        lead = report(base(segments=(seg("s1", 5, 0, 50),)))
        self.assertIn({"track_id": "v1", "start": 0, "end": 5}, lead.gaps)

    def test_audio_track_overlap_detected(self):
        a2 = seg("s2", 40, 50, 100, track_id="v2", audio_track_id="a1")
        plan = base(tracks=(Track("v1", "video", 1), Track("a1", "audio", 1), Track("v2", "video", 2)),
                    segments=(seg("s1", 0, 0, 50), a2))
        self.assertIn("TRACK_OVERLAP", report(plan).codes("error"))


class StructureTests(unittest.TestCase):
    def check(self, code, plan, severity="error", **kw):
        self.assertIn(code, report(plan, **kw).codes(severity), report(plan, **kw).to_dict())

    def test_structure_codes(self):
        t = Track
        cases = {
            "ID_INVALID": base(segments=(seg("bad id!", 0, 0, 10),)),
            "DUPLICATE_ID": base(segments=(seg("s1", 0, 0, 10), seg("s1", 10, 10, 20))),
            "META_INVALID": base(meta=PlanMeta("proj", "plan-1", "")),
            "TIMELINE_RATE_INVALID": base(timeline=TimelineSettings("T", Rate(1, 0), 1920, 1080)),
            "TIMELINE_SIZE_INVALID": base(timeline=TimelineSettings("T", Rate(25), 1921, 1080)),
            "START_TIMECODE_INVALID": base(timeline=TimelineSettings("T", Rate(25), 1920, 1080, "01:00:00;00")),
            "SOURCE_RATE_INVALID": base(sources=(src(rate=Rate(0, 1)),)),
            "SOURCE_DURATION_INVALID": base(sources=(src(dur=0),)),
            "SOURCE_HASH_INVALID": base(sources=(replace(src(), content_hash="xyz"),)),
            "SOURCE_PATH_INVALID": base(sources=(src(path="media\\a.mp4"),)),
            "SOURCE_NO_MEDIA": base(sources=(src(has_video=False, has_audio=False),)),
            "TRACK_INDEX_INVALID": base(tracks=(t("v1", "video", 0), t("a1", "audio", 1))),
            "TRACK_INDEX_DUPLICATE": base(tracks=(t("v1", "video", 1), t("a1", "audio", 1), t("v9", "video", 1))),
            "TRACK_KIND_INVALID": base(tracks=(t("v1", "video", 1), t("a1", "midi", 1))),
            "EMPTY_PLAN": base(segments=()),
            "UNKNOWN_TRACK": base(segments=(seg("s1", 0, 0, 10, track_id="nope"),)),
            "TRACK_KIND_MISMATCH": base(segments=(seg("s1", 0, 0, 10, media="video", track_id="a1"),)),
            "AUDIO_TRACK_MISSING": base(segments=(seg("s1", 0, 0, 10, audio_track_id=None),)),
            "AUDIO_TRACK_UNEXPECTED": base(segments=(seg("s1", 0, 0, 10, media="video"),)),
            "UNKNOWN_SOURCE": base(segments=(seg("s1", 0, 0, 10, source_id="ZZ"),)),
            "SOURCE_LACKS_MEDIA": base(sources=(src(has_audio=False),)),
            "PURPOSE_MEDIA_MISMATCH": base(segments=(seg("s1", 0, 0, 10, purpose="music", speaker="none"),)),
            "RATIONALE_MISSING": base(segments=(seg("s1", 0, 0, 10, rationale=Rationale(" ")),)),
            "TITLE_HAS_SOURCE": base(segments=(seg("s1", 0, 0, 10, purpose="title", speaker="none", title_text="T",
                                                   title_duration_frames=10, media="video"),)),
            "TITLE_FIELDS_INVALID": base(segments=(Segment("s1", "title", "v1", 0, RAT, title_text=" ",
                                                           title_duration_frames=0),)),
        }
        for code, plan in cases.items():
            with self.subTest(code):
                self.check(code, plan)

    def test_negative_controls(self):
        self.assertEqual(report(base()).codes(), [])

    def test_rationale_not_linked_warning(self):
        s = seg("s1", 0, 0, 10, rationale=Rationale("why"))
        self.check("RATIONALE_NOT_LINKED", base(segments=(s,)), "warning")
        self.assertNotIn("RATIONALE_NOT_LINKED", report(base(meta=PlanMeta("p", "plan-1", "t"), segments=(s,))).codes())

    def test_title_segment_ok(self):
        t = Segment("t1", "title", "v2", 0, RAT, title_text="Chapter one", title_duration_frames=25)
        plan = base(tracks=(Track("v1", "video", 1), Track("a1", "audio", 1), Track("v2", "video", 2)),
                    segments=(seg("s1", 0, 0, 50), t))
        r = report(plan)
        self.assertTrue(r.ok, r.to_dict())
        kinds = [o.KIND for o in to_executor_requests(plan)]
        self.assertEqual(kinds, ["create_timeline", "add_track", "import_media", "append_segment", "place_title"])

    def test_transitions(self):
        def tr(kind="cut", dur=0, a="s1", b="s2"):
            return base(transitions=(Transition("t1", kind, a, b, dur, RAT),))
        self.assertTrue(report(tr()).ok)
        self.check("TRANSITION_UNKNOWN_SEGMENT", tr(a="zz"))
        self.check("TRANSITION_NOT_ADJACENT", tr(a="s2", b="s1"))
        self.check("TRANSITION_DURATION_INVALID", tr(dur=5))
        self.check("TRANSITION_DURATION_INVALID", tr("dissolve", 0))
        self.check("TRANSITION_DURATION_INVALID", tr("dissolve", 51))

    def test_markers(self):
        def mk(*ms):
            return base(markers=tuple(ms))
        self.assertTrue(report(mk(Marker("m1", 0, RAT), Marker("m2", 99, RAT))).ok)
        self.check("MARKER_OUT_OF_RANGE", mk(Marker("m1", 100, RAT)))
        self.check("MARKER_OUT_OF_RANGE", mk(Marker("m1", -1, RAT)))
        self.check("MARKER_DUPLICATE_FRAME", mk(Marker("m1", 5, RAT), Marker("m2", 5, RAT)))
        self.check("MARKER_COLOR_INVALID", mk(Marker("m1", 5, RAT, color="Teal")))
        self.check("MARKER_DURATION_INVALID", mk(Marker("m1", 5, RAT, duration_frames=0)))

    def test_qc_expectations(self):
        def q(kind, exp):
            return base(qc_expectations=(QCExpectation("q1", kind, "", exp),))
        self.assertTrue(report(q("total_duration_frames", {"frames": 100})).ok)
        self.assertTrue(report(q("frame_rate", {"num": 50, "den": 2})).ok)
        self.assertTrue(report(q("resolution", {"width": 1920, "height": 1080})).ok)
        self.check("QC_EXPECTATION_UNMET", q("total_duration_frames", {"frames": 99}))
        self.check("QC_EXPECTATION_UNMET", q("frame_rate", {"num": 24, "den": 1}))
        self.check("QC_EXPECTATION_UNMET", q("resolution", {"width": 1280, "height": 720}))
        self.check("QC_EXPECTATION_INVALID", q("total_duration_frames", {}))
        gap = base(segments=(seg("s1", 0, 0, 50), seg("s2", 60, 50, 100)),
                   qc_expectations=(QCExpectation("q1", "no_gaps_on_base_tracks"),))
        self.check("QC_EXPECTATION_UNMET", gap)
        dup = base(qc_expectations=(QCExpectation("q1", "custom"), QCExpectation("q1", "custom")))
        self.check("DUPLICATE_ID", dup)

    def test_validation_never_raises_on_garbage(self):
        bad = base(timeline=TimelineSettings("T", Rate(True, 0), "x", None, 5),
                   segments=(seg("s1", "a", "b", None),))
        rep = report(bad)
        self.assertFalse(rep.ok)


class CapabilityTests(unittest.TestCase):
    def test_request_mapping_and_order(self):
        plan = base(markers=(Marker("m1", 10, RAT, "n"),), exports=(ExportTarget("e1", OUT, "final"),))
        reqs = to_executor_requests(plan, export_allowlist=(OUT,))
        self.assertEqual([r.KIND for r in reqs],
                         ["create_timeline", "import_media", "append_segment", "append_segment", "add_marker",
                          "render_timeline"])
        ct = reqs[0]
        self.assertEqual((ct.frame_rate, ct.width, ct.height, ct.start_timecode), ("25", 1920, 1080, "01:00:00:00"))
        a = reqs[2]
        self.assertEqual((a.source.start, a.source.end, a.media, a.track_index, a.audio_track_index),
                         (0, 50, "video_audio", 1, 1))
        self.assertEqual(reqs[4].frame, 10)
        self.assertEqual(reqs[5].spec.target_dir, OUT)
        for r in reqs:
            self.assertTrue(r.op_id.startswith("plan-1:"))

    def test_extra_tracks_get_add_track(self):
        plan = base(tracks=(Track("v1", "video", 1), Track("a1", "audio", 1), Track("a3", "audio", 3)))
        adds = [o for o in to_executor_requests(plan) if isinstance(o, ops.AddTrack)]
        self.assertEqual([(a.track_kind) for a in adds], ["audio", "audio"])

    def _refused(self, plan, path_part):
        rep = report(plan)
        issues = [i for i in rep.errors if i.code == "CAP_UNSUPPORTED"]
        self.assertTrue(issues, rep.to_dict())
        self.assertTrue(any(path_part in i.path for i in issues), [i.path for i in issues])
        with self.assertRaises(PlanNotExecutable) as cm:
            to_executor_requests(plan)
        self.assertIn("CAP_UNSUPPORTED", cm.exception.report.codes("error"))

    def test_unsupported_items_are_hard_errors(self):
        self._refused(base(segments=(seg("s1", 0, 0, 50, audio_gain_db=-6),)), "audio_gain_db")
        self._refused(base(transitions=(Transition("t1", "dissolve", "s1", "s2", 10, RAT),)), "/transitions/0")
        self._refused(base(transitions=(Transition("t1", "dip_to_black", "s1", "s2", 10, RAT),)), "/transitions/0")
        ref = TranscriptRange("S1", 0, 50)
        sub = Caption("c1", "x", "st1", 0, 10, RAT, ref, "subtitle_track")
        plan = base(tracks=(Track("v1", "video", 1), Track("a1", "audio", 1), Track("st1", "subtitle", 1)),
                    captions=(sub,))
        self._refused(plan, "/captions/0")
        self._refused(plan, "/tracks/2")

    def test_cut_needs_no_operation_and_burn_in_caption_maps(self):
        ref = TranscriptRange("S1", 0, 50)
        cap = Caption("c1", "hello", "v2", 0, 25, RAT, ref)
        plan = base(tracks=(Track("v1", "video", 1), Track("a1", "audio", 1), Track("v2", "video", 2)),
                    transitions=(Transition("t1", "cut", "s1", "s2", 0, RAT),), captions=(cap,))
        r = report(plan, transcript={"S1": [{"start_frame": 0, "end_frame": 50, "text": "Hello there"}]})
        self.assertTrue(r.ok, r.to_dict())
        reqs = to_executor_requests(plan, export_allowlist=())
        self.assertIn("place_title", [o.KIND for o in reqs])
        self.assertNotIn("add_transition", [o.KIND for o in reqs])

    def test_executor_validation_applies(self):
        plan = base(timeline=TimelineSettings("bad/name", Rate(25), 1920, 1080))
        self.assertIn("CAP_REQUEST_INVALID", report(plan).codes("error"))
        odd = base(sources=(src(rate=Rate(15)),), timeline=TimelineSettings("T", Rate(15), 1920, 1080))
        self.assertFalse(report(odd).ok)

    def test_not_executable_when_structurally_broken(self):
        with self.assertRaises(PlanNotExecutable):
            to_executor_requests(base(segments=(seg("s1", 0, 0, 5000),)))


class FidelityTests(unittest.TestCase):
    def check(self, code, plan, **kw):
        self.assertIn(code, report(plan, **kw).codes("error"), report(plan, **kw).to_dict())

    def test_dialogue_rules(self):
        self.check("FIDELITY_GENERATED_SPEECH", base(sources=(src(origin="generated_speech"),)))
        self.check("FIDELITY_DIALOGUE_NOT_CREATOR", base(sources=(src(origin="other_recording"),)))
        self.check("FIDELITY_DIALOGUE_SPEAKER", base(segments=(seg("s1", 0, 0, 10, speaker="unknown"),)))
        self.check("FIDELITY_DIALOGUE_NO_AUDIO", base(segments=(seg("s1", 0, 0, 10, media="video",
                                                                    audio_track_id=None),)))
        self.check("FIDELITY_SPEAKER_ORIGIN_MISMATCH",
                   base(sources=(src(origin="stock_footage"),),
                        segments=(seg("s1", 0, 0, 10, purpose="b_roll", speaker="creator"),)))
        broll = base(sources=(src(origin="stock_footage"),),
                     segments=(seg("s1", 0, 0, 10, purpose="b_roll", speaker="none", media="video",
                                   audio_track_id=None),))
        self.assertTrue(report(broll).ok)

    def test_caption_rules(self):
        tracks = (Track("v1", "video", 1), Track("a1", "audio", 1), Track("v2", "video", 2))
        tr = {"S1": [{"start_frame": 0, "end_frame": 25, "text": "Hello   there"},
                     {"start_frame": 25, "end_frame": 50, "text": "my friend"}]}

        def cap(text="hello there", ref=TranscriptRange("S1", 0, 25), cid="c1"):
            return base(tracks=tracks, captions=(Caption(cid, text, "v2", 0, 10, RAT, ref),))
        self.assertTrue(report(cap(), transcript=tr).ok)
        self.assertTrue(report(cap("there my"), transcript=tr).ok is False)         # text outside its own range
        self.assertTrue(report(cap("Hello there my friend", TranscriptRange("S1", 0, 50)), transcript=tr).ok)
        self.check("CAPTION_NOT_QUOTED", cap("something never said"), transcript=tr)
        self.check("CAPTION_NO_TRANSCRIPT_REF", cap(ref=None), transcript=tr)
        self.check("CAPTION_TEXT_EMPTY", cap(" "), transcript=tr)
        self.check("CAPTION_SOURCE_UNKNOWN", cap(ref=TranscriptRange("ZZ", 0, 25)), transcript=tr)
        self.check("CAPTION_REF_RANGE_INVALID", cap(ref=TranscriptRange("S1", 10, 10)), transcript=tr)
        self.check("CAPTION_REF_OUT_OF_SOURCE", cap(ref=TranscriptRange("S1", 0, 5000)), transcript=tr)
        self.check("CAPTION_DURATION_INVALID",
                   base(tracks=tracks, captions=(Caption("c1", "hi", "v2", 0, 0, RAT, TranscriptRange("S1", 0, 5)),)))
        no_tr = report(cap())
        self.assertIn("CAPTION_UNVERIFIED_NO_TRANSCRIPT", no_tr.codes("warning"))
        self.assertTrue(no_tr.ok)
        gen = replace(cap(), sources=(src(origin="generated_speech"),))
        self.check("FIDELITY_GENERATED_SPEECH", gen, transcript=tr)

    def test_export_allowlist(self):
        def ex(directory=OUT, name="final", fmt="mp4"):
            return base(exports=(ExportTarget("e1", directory, name, fmt),))
        self.assertIn("EXPORT_NOT_ALLOWLISTED", report(ex()).codes("error"))                # empty allowlist refuses
        self.assertIn("EXPORT_NOT_ALLOWLISTED", report(ex(), export_allowlist=()).codes("error"))
        self.assertTrue(report(ex(), export_allowlist=(OUT,)).ok)
        self.assertTrue(report(ex(OUT + "\\sub\\deeper"), export_allowlist=(OUT,)).ok)
        self.assertTrue(report(ex("c:/synth/OUT/"), export_allowlist=(OUT,)).ok)           # case and separators
        self.assertIn("EXPORT_NOT_ALLOWLISTED", report(ex("C:\\Synth\\Out2"), export_allowlist=(OUT,)).codes("error"))
        self.assertIn("EXPORT_NOT_ALLOWLISTED", report(ex("C:\\Synth\\Out\\..\\Elsewhere"), export_allowlist=(OUT,)).codes())
        self.assertIn("EXPORT_PATH_INVALID", report(ex("out"), export_allowlist=(OUT,)).codes("error"))
        self.assertIn("EXPORT_NAME_INVALID", report(ex(name="..\\x"), export_allowlist=(OUT,)).codes("error"))

    def test_export_under_projects_refused_even_if_allowlisted(self):
        root = "C:\\Users\\Someone\\CreatorStudio"
        for d in (root + "\\Projects\\demo\\renders", root + "\\projects", "C:/Users/Someone/creatorstudio/PROJECTS/x"):
            with self.subTest(d):
                r = report(base(exports=(ExportTarget("e1", d, "final"),)), export_allowlist=(root,))
                self.assertIn("EXPORT_UNDER_PROJECTS", r.codes("error"))
                self.assertFalse(r.ok)
        ok = report(base(exports=(ExportTarget("e1", root + "\\Exports", "final"),)), export_allowlist=(root,))
        self.assertTrue(ok.ok, ok.to_dict())

    def test_sources_are_read_only(self):
        r = report(base(exports=(ExportTarget("e1", "C:\\Synth\\Media", "other"),)), export_allowlist=("C:\\Synth",))
        self.assertIn("EXPORT_INSIDE_SOURCE_DIR", r.codes("error"))
        r = report(base(exports=(ExportTarget("e1", "c:/synth/media/nested", "other"),)), export_allowlist=("C:\\Synth",))
        self.assertIn("EXPORT_INSIDE_SOURCE_DIR", r.codes("error"))
        r = report(base(exports=(ExportTarget("e1", "C:\\Synth\\Media", "a"),)), export_allowlist=("C:\\Synth",))
        self.assertIn("EXPORT_OVERWRITES_SOURCE", r.codes("error"))
        r = report(base(exports=(ExportTarget("e1", "C:\\Synth\\Media2", "a"),)), export_allowlist=("C:\\Synth",))
        self.assertNotIn("EXPORT_INSIDE_SOURCE_DIR", r.codes())                              # not a string prefix match
        self.assertTrue(r.ok, r.to_dict())

    def test_duplicate_exports(self):
        p = base(exports=(ExportTarget("e1", OUT, "final"), ExportTarget("e2", OUT, "FINAL")))
        self.assertIn("EXPORT_DUPLICATE", report(p, export_allowlist=(OUT,)).codes("error"))


class HashTests(unittest.TestCase):
    def test_stable_and_canonical(self):
        plan = base()
        self.assertEqual(plan_hash(plan), plan_hash(base()))
        self.assertEqual(len(plan_hash(plan)), 64)
        text = canonical_json(plan)
        self.assertNotIn(" ", text.replace("Synthetic Cut", "").replace("test item", ""))
        data = json.loads(text)
        self.assertEqual(list(data), sorted(data))
        shuffled = json.loads(json.dumps(plan.to_dict(), sort_keys=True))
        shuffled = dict(reversed(list(shuffled.items())))
        self.assertEqual(plan_hash(EditPlan.from_dict(shuffled)), plan_hash(plan))

    def test_equivalent_rates_hash_alike_and_changes_change_it(self):
        a = base(timeline=TimelineSettings("T", Rate(30000, 1001), 1920, 1080))
        b = base(timeline=TimelineSettings("T", Rate(60000, 2002), 1920, 1080))
        self.assertEqual(plan_hash(a), plan_hash(b))
        self.assertNotEqual(plan_hash(base()), plan_hash(base(segments=(seg("s1", 0, 0, 49), seg("s2", 50, 50, 100)))))
        self.assertNotEqual(plan_hash(base()), plan_hash(base(meta=PlanMeta("proj", "plan-2", "tester", "brief-1", H))))

    def test_report_carries_hash(self):
        self.assertEqual(report(base()).plan_hash, plan_hash(base()))


def _brief(status="ok", attribution="creator", source_id="S1", second_start=5.21):
    ref1 = CS.SourceRef(source_id, 1.0, 3.0, ("seg1",), "reviewed")
    ref2 = CS.SourceRef(source_id, second_start, 6.0, ("seg2",), "reviewed")
    moments = tuple(CS.Moment(mid, CS.MomentKind.OPINION, ref, "x", attribution, "i", 0.5, "r", "heuristic")
                    for mid, ref in (("m1", ref1), ("m2", ref2)))
    sections = (CS.Section("sec1", "Opening", CS.Function.HOOK, "open", ("m1",), (CS.RangeUse(ref1),), "starts strong"),
                CS.Section("sec2", "Middle", CS.Function.DEVELOPMENT, "develop", ("m2",), (CS.RangeUse(ref2),),
                           "follows on"))
    arc = CS.Arc("arc1", "Linear", "chronological", "d", "f", sections, "r", (), 0.5, False)
    return CS.StoryBrief(
        schema_version="x", brief_id="brief-42", mode=CS.Mode.LONG_FORM, project_id="proj", status=status, sources=(),
        central_idea=None, audience=CS.Audience(None, None, "unresolved", 0.0), moments=moments, pauses=(),
        callbacks=(), findings=(), beginnings=(), endings=(), arcs=(arc,), recommended_arc_id="arc1",
        recommendation_basis="b", hooks=(), short_concepts=(), style_considerations=(), research_context=(),
        research_suggestions=(), filming_guidance=(), rejected_inputs=(), issues=(), warnings=(), confidence={},
        authenticity={}, material_digest={}, research_meta={}, research_digest={}, dna_notice="")


def _inventory():
    return {"sources": [{"source_id": "S1", "path": MEDIA, "frame_rate": {"num": 25, "den": 1},
                         "duration_frames": 1000, "content_hash": H, "origin": "creator_recording"}]}


TRANSCRIPT = {"S1": [{"start_frame": 25, "end_frame": 75, "text": "This is the first thing."},
                     {"start_frame": 125, "end_frame": 150, "text": "And the second."}]}


class PlannerTests(unittest.TestCase):
    def test_end_to_end(self):
        res = plan_from_brief(_brief(), _inventory())
        self.assertTrue(res.report.ok, res.report.to_dict())
        plan = res.plan
        self.assertEqual(plan.meta.created_by, "editor_planner/offline-0.1")
        self.assertEqual((plan.meta.source_brief_id, plan.meta.project_slug), ("brief-42", "proj"))
        self.assertEqual([(s.source_in, s.source_out, s.record_in) for s in plan.segments], [(25, 75, 0), (130, 150, 50)])
        self.assertEqual([m.frame for m in plan.markers], [0, 50])
        self.assertEqual(res.report.total_duration_frames, 70)
        self.assertTrue(any("widened" in n for n in res.notes))
        self.assertIn("CANDIDATE", res.candidate_notice)
        self.assertEqual(res.report.plan_hash, plan_hash(plan))
        self.assertEqual(EditPlan.from_dict(json.loads(json.dumps(plan.to_dict()))), plan)
        reqs = to_executor_requests(plan)
        self.assertEqual([r.KIND for r in reqs],
                         ["create_timeline", "import_media", "append_segment", "append_segment", "add_marker",
                          "add_marker"])
        self.assertTrue(all(s.speaker == "creator" for s in plan.segments))

    def test_deterministic_and_dict_brief(self):
        a = plan_from_brief(_brief(), _inventory())
        b = plan_from_brief(_brief().to_dict(), copy.deepcopy(_inventory()))
        self.assertIsNotNone(a.plan, a.report.to_dict())
        self.assertIsNotNone(b.plan, b.report.to_dict())
        self.assertEqual(plan_hash(a.plan), plan_hash(plan_from_brief(_brief(), _inventory()).plan))
        self.assertEqual(plan_hash(a.plan), plan_hash(b.plan))

    def test_captions_quote_transcript(self):
        res = plan_from_brief(_brief(), _inventory(), TRANSCRIPT, captions=True)
        self.assertTrue(res.report.ok, res.report.to_dict())
        self.assertEqual([c.text for c in res.plan.captions], ["This is the first thing.", "And the second."])
        self.assertTrue(all(c.transcript_ref is not None for c in res.plan.captions))
        self.assertIn("v2", [t.track_id for t in res.plan.tracks])
        self.assertEqual([r.KIND for r in to_executor_requests(res.plan)].count("place_title"), 2)
        tampered = {"S1": [{"start_frame": 25, "end_frame": 75, "text": "Something else entirely."}]}
        again = validate_plan(res.plan, transcript=tampered)
        self.assertIn("CAPTION_NOT_QUOTED", again.codes("error"))

    def test_refusals_never_raise(self):
        self.assertEqual(plan_from_brief(_brief(status="insufficient_material"), _inventory()).report.errors[0].code,
                         "PLANNER_BRIEF_UNUSABLE")
        r = plan_from_brief(_brief(source_id="ZZ"), _inventory())
        self.assertIsNone(r.plan)
        self.assertEqual(r.report.errors[0].code, "PLANNER_UNKNOWN_SOURCE")
        r = plan_from_brief(_brief(), {"sources": []})
        self.assertEqual(r.report.errors[0].code, "PLANNER_NO_SOURCES")
        r = plan_from_brief({"status": "ok", "arcs": []}, _inventory())
        self.assertEqual(r.report.errors[0].code, "PLANNER_NO_ARC")
        r = plan_from_brief(object(), {})
        self.assertFalse(r.report.ok)
        r = plan_from_brief(_brief(), {"sources": [{"source_id": "S1"}]})
        self.assertEqual(r.report.errors[0].code, "PLANNER_INPUT_INVALID")

    def test_non_creator_attribution_is_reported_not_hidden(self):
        res = plan_from_brief(_brief(attribution="other"), _inventory())
        self.assertIsNotNone(res.plan)
        self.assertIn("FIDELITY_DIALOGUE_SPEAKER", res.report.codes("error"))
        with self.assertRaises(PlanNotExecutable):
            to_executor_requests(res.plan)

    def test_end_clamped_to_source(self):
        inv = _inventory()
        inv["sources"][0]["duration_frames"] = 140
        res = plan_from_brief(_brief(), inv)
        self.assertIsNotNone(res.plan, res.report.to_dict())
        self.assertEqual(res.plan.segments[1].source_out, 140)
        self.assertTrue(any("clamped" in n for n in res.notes))


class PackageHygieneTests(unittest.TestCase):
    def test_no_network_or_resolve_imports(self):
        pkg = Path(__file__).resolve().parents[1] / "edit_plan"
        text = "\n".join(p.read_text(encoding="utf-8") for p in pkg.glob("*.py"))
        for banned in ("import socket", "import urllib", "import requests", "import http", "DaVinciResolveScript",
                       "subprocess", "os.system"):
            self.assertNotIn(banned, text, banned)
        self.assertIsNone(re.search(r"(?<![\w.])open\(", text), "the package must not open files")


if __name__ == "__main__":
    unittest.main()
