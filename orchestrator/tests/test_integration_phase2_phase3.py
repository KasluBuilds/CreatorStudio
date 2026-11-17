"""Integration tests: media_analysis -> creator_dna handoff and the creator_state project pipeline.
Synthetic fixtures only; no network, no AI services, no real footage.
Run from orchestrator/:  python -I -m unittest discover -s tests -p "test_integration_phase2_phase3.py" -v"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import creator_dna as dna
from _dna_lenient import LenientStore
from _dna_verifiers import strict_test_store
from creator_state import ingest
from creator_state.project import Project, StateError
from media_analysis import (AnalysisCopyRegistry, CapabilityError, DnaEligibility, FixtureProvider, FrameRef,
                            MediaAnalysisError, MediaRef, MomentIndex, Provenance, SegmentReview, ShotBoundary,
                            SourceResolver, Transcript, TranscriptSegment, UnavailableProvider, VisualEvent,
                            analyze_wav, apply_review, build_handoff, extend_review, latest_review, make_review,
                            save_review, save_transcript, to_dna_audio, to_dna_transcript, to_dna_visual)
from media_analysis import pipeline
from speaker_attribution import Delivery, Identity
from media_analysis.review import TranscriptReview, load_review
from test_media_analysis import fake_prober, ffprobe_json, make_wav

SHA = hashlib.sha256(b"original bytes").hexdigest()
ASR = Provenance("fixture-asr", "1", "synthetic_fixture")


def asr_transcript(media_id="m_a", copy_id="m_a_audio_wav_x"):
    return Transcript(media_id, [
        TranscriptSegment(media_id, 0.0, 3.0, "Ajde de, ovo je fejk, ček.", language="hr", variety="kajkavian", confidence=0.8),
        TranscriptSegment(media_id, 20.0, 23.0, "pa ajde dee nema šanse", language="hr", confidence=0.55),
        TranscriptSegment(media_id, 40.0, 42.0, "So basically ajde de then", language="en", confidence=0.2),
    ], ASR, "hr", audio_copy_id=copy_id)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.orig = self.tmp / "originals"
        self.orig.mkdir()
        (self.orig / "a.mov").write_bytes(b"original bytes")
        self.ref = MediaRef("m_a", str(self.orig / "a.mov"), 14, None, SHA, 60.0)
        self.resolver = SourceResolver({"m_a": self.ref})
        self.store = LenientStore(self.tmp / "dna")      # attribution rules are tested in test_speaker_attribution.py


# ----------------------------------------------------------- A. transcript review
class UnverifiedVersusReviewed(Tmp):
    def test_unreviewed_asr_is_not_handed_over_by_default(self):
        segs, rep = to_dna_transcript(asr_transcript(), self.resolver)
        self.assertEqual(segs, [])
        self.assertEqual(len(rep.skipped), 3)
        self.assertIn("unreviewed ASR", rep.skipped[0]["reason"])

    def test_unreviewed_asr_stays_unverified_and_cannot_become_verbatim(self):
        segs, rep = to_dna_transcript(asr_transcript(), self.resolver, DnaEligibility(include_unverified_transcripts=True))
        self.assertEqual({s.verification for s in segs}, {"unverified"})
        self.assertEqual({s.reviewer for s in segs}, {None})
        self.assertIn("transcript:unverified", segs[0].context)
        self.assertEqual(rep.included[0]["status"], "unverified")
        # DNA analyzers turn it into ASR_UNVERIFIED hypotheses, never VERBATIM / OBSERVED
        cand = dna.RecurringPhraseAnalyzer(["ajde de"]).analyze_transcript(segs)[0]
        self.assertEqual((cand.layer, {e.method for e in cand.evidence}), (dna.Layer.HYPOTHESIS, {dna.Method.ASR_UNVERIFIED}))
        # forcing the OBSERVED layer is refused by the store
        cand.layer = dna.Layer.OBSERVED
        r = self.store.ingest([cand], "t")
        self.assertEqual(r.traits_created, [])
        self.assertIn("not allowed on a observed trait", r.refused[0]["reason"])
        # and verbatim evidence cannot be built without a named reviewer
        with self.assertRaises(dna.DNAError):
            dna.Evidence("m_a", dna.Modality.TRANSCRIPT, dna.Method.VERBATIM, start_s=0, end_s=3, quote="Ajde de")

    def test_passing_through_the_adapter_does_not_promote(self):
        t = asr_transcript()
        a, _ = to_dna_transcript(t, self.resolver, DnaEligibility(include_unverified_transcripts=True))
        b, _ = to_dna_transcript(apply_review(t, None), self.resolver, DnaEligibility(include_unverified_transcripts=True))
        self.assertEqual(a, b)
        self.assertEqual(t.verification, "unverified")

    def make_review(self, t=None):
        t = t or asr_transcript()
        s = t.segments
        return t, make_review(t, "human:ana", [
            SegmentReview(s[0].record_id, "confirmed", note="checked against audio"),
            SegmentReview(s[1].record_id, "corrected", "pa ajde de nema šanse", "engine heard dee")])

    def test_reviewed_transcript_carries_reviewer_and_review_provenance(self):
        t, review = self.make_review()
        rt = apply_review(t, review)
        self.assertEqual([x.status for x in rt.segments], ["confirmed", "corrected", "unreviewed"])
        segs, rep = to_dna_transcript(rt, self.resolver)              # default policy: reviewed only
        self.assertEqual(len(segs), 2)
        self.assertEqual({s.verification for s in segs}, {"reviewed"})
        self.assertEqual({s.reviewer for s in segs}, {"human:ana"})
        confirmed, corrected = segs
        self.assertEqual(confirmed.text, t.segments[0].text)
        self.assertEqual(corrected.text, "pa ajde de nema šanse")           # reviewed wording, verbatim
        self.assertEqual(corrected.origin.review.original_text, "pa ajde dee nema šanse")
        self.assertEqual(corrected.origin.review.original_record_id, t.segments[1].record_id)
        self.assertEqual((corrected.origin.review.status, confirmed.origin.review.status), ("corrected", "confirmed"))
        self.assertIsNone(confirmed.origin.review.original_text)
        self.assertEqual(corrected.origin.review.review_id, review.review_id)
        self.assertEqual(rep.skipped[0]["reason"], "unreviewed ASR (policy excludes it)")
        # reviewed text becomes VERBATIM evidence with the reviewer, OBSERVED layer
        cand = dna.RecurringPhraseAnalyzer(["ajde de"]).analyze_transcript(segs)[0]
        self.assertEqual(cand.layer, dna.Layer.OBSERVED)
        self.assertEqual({(e.method, e.reviewed_by) for e in cand.evidence}, {(dna.Method.VERBATIM, "human:ana")})
        r = self.store.ingest([cand], "t", transcript=segs)
        self.assertEqual((r.evidence_added, r.refused), (2, []))

    def test_mixed_reviewed_and_unverified_stay_distinguishable(self):
        t, review = self.make_review()
        segs, _ = to_dna_transcript(apply_review(t, review), self.resolver, DnaEligibility(include_unverified_transcripts=True))
        self.assertEqual([s.verification for s in segs], ["reviewed", "reviewed", "unverified"])
        cand = dna.RecurringPhraseAnalyzer(["ajde de"]).analyze_transcript(segs)[0]
        self.assertEqual(cand.layer, dna.Layer.HYPOTHESIS)               # one unverified occurrence caps the claim
        self.assertEqual([e.method for e in cand.evidence],
                         [dna.Method.VERBATIM, dna.Method.VERBATIM, dna.Method.ASR_UNVERIFIED])

    def test_original_asr_output_is_unchanged_by_review_and_correction(self):
        t, review = self.make_review()
        before = json.dumps(t.to_dict(), ensure_ascii=False, sort_keys=True)
        path = save_transcript(self.tmp / "tr", t)
        file_before = path.read_bytes()
        rt = apply_review(t, review)
        to_dna_transcript(rt, self.resolver, DnaEligibility(include_unverified_transcripts=True))
        save_review(self.tmp / "tr", review)
        self.assertEqual(json.dumps(t.to_dict(), ensure_ascii=False, sort_keys=True), before)
        self.assertEqual(path.read_bytes(), file_before)
        self.assertEqual(rt.segments[1].original.text, "pa ajde dee nema šanse")      # original still available
        self.assertEqual(rt.segments[1].text, "pa ajde de nema šanse")
        self.assertIs(rt.original, t)
        self.assertEqual(t.verification, "unverified")

    def test_review_validation(self):
        t = asr_transcript()
        s = t.segments
        bad = [
            lambda: make_review(t, "ana", [SegmentReview(s[0].record_id, "confirmed")]),         # not human:<name>
            lambda: make_review(t, "human:", [SegmentReview(s[0].record_id, "confirmed")]),
            lambda: make_review(t, "human:ana", [SegmentReview("seg_nope", "confirmed")]),
            lambda: make_review(t, "human:ana", [SegmentReview(s[0].record_id, "corrected", s[0].text)]),
            lambda: make_review(t, "human:ana", []),
            lambda: make_review(t, "human:ana", [SegmentReview(s[0].record_id, "confirmed"),
                                                 SegmentReview(s[0].record_id, "confirmed")]),
            lambda: SegmentReview(s[0].record_id, "corrected"),
            lambda: SegmentReview(s[0].record_id, "confirmed", "text"),
            lambda: SegmentReview(s[0].record_id, "maybe"),
        ]
        for i, f in enumerate(bad):
            with self.subTest(i=i), self.assertRaises(MediaAnalysisError):
                f()

    def test_review_is_bound_to_the_exact_transcript_content(self):
        t, review = self.make_review()
        other = Transcript("m_a", t.segments[:2], ASR, "hr", audio_copy_id=t.audio_copy_id)
        self.assertEqual(other.transcript_id, t.transcript_id)           # same engine+settings, different content
        with self.assertRaises(MediaAnalysisError):
            apply_review(other, review)
        wrong_engine = Transcript("m_a", t.segments, Provenance("other-asr", "1", "synthetic_fixture"), "hr")
        with self.assertRaises(MediaAnalysisError):
            apply_review(wrong_engine, review)

    def test_review_chain_extend_latest_and_tamper_detection(self):
        t, r1 = self.make_review()
        d = self.tmp / "rv"
        save_review(d, r1)
        r2 = extend_review(t, r1, "human:bo", [SegmentReview(t.segments[2].record_id, "confirmed"),
                                              SegmentReview(t.segments[1].record_id, "confirmed")])
        self.assertEqual(r2.previous_review_id, r1.review_id)
        self.assertEqual({i.segment_id: i.status for i in r2.items},
                         {t.segments[0].record_id: "confirmed", t.segments[1].record_id: "confirmed",
                          t.segments[2].record_id: "confirmed"})
        self.assertEqual(save_review(d, r1), save_review(d, r1))         # idempotent, write-once
        save_review(d, r2)
        self.assertEqual(latest_review(d, t.transcript_id).review_id, r2.review_id)
        self.assertTrue(r1.review_id != r2.review_id and len(list(d.glob("*.json"))) == 2)   # nothing overwritten
        fork = make_review(t, "human:cy", [SegmentReview(t.segments[0].record_id, "confirmed")], r1)
        save_review(d, fork)
        with self.assertRaises(MediaAnalysisError):
            latest_review(d, t.transcript_id)
        p = next(d.glob("*.json"))
        data = json.loads(p.read_text(encoding="utf-8"))
        data["reviewer"] = "human:mallory"
        p.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(MediaAnalysisError):
            load_review(p)
        self.assertIsNone(latest_review(self.tmp / "empty", "x"))


# ---------------------------------------------------------- B. visual confidence
class VisualAdapter(Tmp):
    def test_missing_confidence_is_none_and_supplied_is_preserved(self):
        none_b = ShotBoundary("m_a", 14.0, "ffmpeg-scene@0.4", score=0.7)
        sure_b = ShotBoundary("m_a", 15.0, "other@1", confidence=0.9)
        ev = VisualEvent("m_a", 2.0, 3.0, "motion_burst", "m", "fast motion", 0.25)
        obs, rep = to_dna_visual([none_b, sure_b, ev], self.resolver)
        self.assertIsNone(obs[0].estimate_confidence)
        self.assertNotIn("default", obs[0].description)                 # no invented value, no hint of one
        self.assertEqual((obs[1].estimate_confidence, obs[2].estimate_confidence), (0.9, 0.25))
        self.assertEqual([o.label for o in obs], ["shot_boundary", "shot_boundary", "motion_burst"])
        self.assertEqual(len(rep.included), 3)

    def test_unrated_detection_reaches_dna_without_manufactured_confidence(self):
        obs, _ = to_dna_visual([ShotBoundary("m_a", 14.0, "ffmpeg-scene@0.4", score=0.7)], self.resolver)
        tid = self.store.propose_trait(dna.Category.CAMERA_PRESENCE, "frequent cuts", dna.Layer.HYPOTHESIS, "t")
        self.store.add_evidence(tid, dna.evidence_from_visual(obs[0]), "t")
        a = self.store.get(tid).assessment()
        self.assertEqual((a.unrated_examples, a.support_mass, a.confidence), (1, 0.0, 0.0))

    def test_policy_can_exclude_visual(self):
        obs, rep = to_dna_visual([ShotBoundary("m_a", 14.0, "x")], self.resolver, DnaEligibility(include_visual=False))
        self.assertEqual((obs, len(rep.skipped)), ([], 1))


# ----------------------------------------------------------- D. audio metrics
class AudioAdapter(Tmp):
    def analysis(self, **kw):
        wav = make_wav(self.tmp / "x.wav", [(3.0, 0.05), (1.0, 0.5), (0.8, 0.0), (1.0, 0.3)])
        return analyze_wav(wav, "m_a", {"threshold_dbfs": -50, **kw}, analysis_copy_id="m_a_audio_wav_x")

    def test_all_phase2_metrics_validate_against_the_dna_contract(self):
        a = self.analysis()
        wins, rep = to_dna_audio(a, self.resolver, DnaEligibility(include_level_windows=True))
        names = {k for w in wins for k in w.metrics}
        self.assertEqual(names, {"pause_duration_s", "energy_rise_db", "level_dbfs", "baseline_dbfs",
                                 "rms_dbfs", "peak_dbfs"})
        self.assertTrue(a.gaps and a.energy_events)
        self.assertEqual({r["kind"] for r in rep.included}, {"audio_gap", "energy_event", "audio_window"})
        gap = next(w for w in wins if "pause_duration_s" in w.metrics)
        self.assertEqual(gap.metric("pause_duration_s"), a.gaps[0].duration_s)
        self.assertIsNone(gap.metric("energy_rise_db"))                      # absent stays absent

    def test_origin_carries_the_analysis_settings_that_give_numbers_meaning(self):
        wins, _ = to_dna_audio(self.analysis(), self.resolver)
        o = wins[0].origin
        self.assertEqual(o.record_kind, "measurement")
        self.assertEqual(o.settings["resolved_threshold_dbfs"], -50.0)
        self.assertEqual({"window_s", "min_gap_s", "long_gap_s"} <= set(o.settings), True)
        self.assertEqual(o.analysis_copy_id, "m_a_audio_wav_x")
        self.assertEqual(o.media_id, "m_a")

    def test_policy_selects_what_is_handed_over(self):
        a = self.analysis()
        gaps_only, _ = to_dna_audio(a, self.resolver, DnaEligibility(include_energy_rises=False))
        self.assertEqual({k for w in gaps_only for k in w.metrics}, {"pause_duration_s"})
        none, _ = to_dna_audio(a, self.resolver, DnaEligibility(include_audio_gaps=False, include_energy_rises=False))
        self.assertEqual(none, [])

    def test_missing_optional_metrics_do_not_crash_a_dna_consumer(self):
        wins, _ = to_dna_audio(self.analysis(), self.resolver, DnaEligibility(include_level_windows=True))
        ev = [dna.evidence_from_window(w, "pause_duration_s") for w in wins]
        self.assertTrue(any(e is None for e in ev) and any(e is not None for e in ev))

    def test_measurement_does_not_become_a_trait_by_itself(self):
        wins, _ = to_dna_audio(self.analysis(), self.resolver)
        self.assertEqual(self.store.traits, {})
        tid = self.store.propose_trait(dna.Category.PAUSES, "pauses before punchlines", dna.Layer.OBSERVED, "t")
        for w in wins:
            e = dna.evidence_from_window(w, "pause_duration_s")
            if e:
                self.store.add_evidence(tid, e, "t")
        a = self.store.get(tid).assessment()
        self.assertEqual(a.scope, dna.Scope.ONE_OFF)                         # one recording is never a rule
        self.assertLess(a.confidence, 0.45)


# --------------------------------------------------- E. source identity and trace
class SourceIdentity(Tmp):
    def test_media_id_checksum_path_and_original_timeline_survive(self):
        segs, _ = to_dna_transcript(asr_transcript(), self.resolver, DnaEligibility(include_unverified_transcripts=True))
        s = segs[1]
        self.assertEqual((s.start_s, s.end_s), (20.0, 23.0))                  # original timeline, untouched
        o = s.origin
        self.assertEqual((o.media_id, o.source_sha256, o.source_path), ("m_a", SHA, str(self.orig / "a.mov")))
        self.assertEqual(o.record_id, asr_transcript().segments[1].record_id)
        self.assertEqual((o.record_kind, o.engine, o.analysis_copy_id), ("transcription", "fixture-asr@1", "m_a_audio_wav_x"))

    def test_source_ids_can_be_namespaced_but_origin_keeps_the_media_id(self):
        r = SourceResolver({"m_a": self.ref}, source_id_fn=lambda m: "trip/" + m)
        segs, _ = to_dna_transcript(asr_transcript(), r, DnaEligibility(include_unverified_transcripts=True))
        self.assertEqual((segs[0].source_id, segs[0].origin.media_id), ("trip/m_a", "m_a"))

    def test_paths_can_be_omitted(self):
        r = SourceResolver({"m_a": self.ref}, include_paths=False)
        segs, _ = to_dna_transcript(asr_transcript(), r, DnaEligibility(include_unverified_transcripts=True))
        self.assertIsNone(segs[0].origin.source_path)
        self.assertEqual(segs[0].origin.source_sha256, SHA)

    def test_unknown_media_cannot_be_traced_so_it_is_refused(self):
        with self.assertRaises(MediaAnalysisError):
            to_dna_transcript(asr_transcript("m_zzz"), self.resolver, DnaEligibility(include_unverified_transcripts=True))

    def test_missing_checksum_stays_missing(self):
        r = SourceResolver({"m_a": MediaRef("m_a", self.ref.path)})
        segs, _ = to_dna_transcript(asr_transcript(), r, DnaEligibility(include_unverified_transcripts=True))
        self.assertIsNone(segs[0].origin.source_sha256)

    def test_analysis_copy_cannot_be_mistaken_for_the_original(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            make_wav(Path(argv[-1]), [(1.0, 0.2)])
            return 0, "", ""
        orig_ref = MediaRef("m_a", self.ref.path, self.ref.size, self.orig.joinpath("a.mov").stat().st_mtime_ns, SHA, 1.0)
        reg = AnalysisCopyRegistry(self.tmp / "analysis", "fake-ffmpeg", runner)
        rec, _ = reg.ensure(orig_ref)
        copy_path = str(reg.directory / rec["output"]["path"])
        with self.assertRaises(MediaAnalysisError):                            # a ref pointing at the copy
            SourceResolver({"m_a": MediaRef("m_a", copy_path, None, None, rec["output"]["sha256"], 1.0)}, reg)
        with self.assertRaises(MediaAnalysisError):                            # the copy id used as a media id
            SourceResolver({rec["copy_id"]: MediaRef(rec["copy_id"], self.ref.path)}, reg)
        ok = SourceResolver({"m_a": orig_ref}, reg)                            # the genuine original is accepted
        self.assertEqual(ok.origin("m_a").source_sha256, SHA)
        # the copy only appears as analysis_copy_id, never as the media id / checksum / path
        wav = make_wav(self.tmp / "w.wav", [(1.0, 0.3), (1.0, 0.0), (1.0, 0.3)])
        a = analyze_wav(wav, "m_a", {"threshold_dbfs": -40}, analysis_copy_id=rec["copy_id"])
        wins, _ = to_dna_audio(a, ok)
        o = wins[0].origin
        self.assertEqual((o.media_id, o.source_sha256, o.analysis_copy_id), ("m_a", SHA, rec["copy_id"]))
        self.assertNotEqual(o.source_sha256, rec["output"]["sha256"])
        self.assertNotIn("audio_wav", o.media_id)

    def test_dna_evidence_traces_back_to_the_phase2_record_and_original(self):
        a = analyze_wav(make_wav(self.tmp / "w.wav", [(1.0, 0.3), (1.0, 0.0), (1.0, 0.3)]), "m_a",
                        {"threshold_dbfs": -40}, analysis_copy_id="m_a_audio_wav_x")
        wins, _ = to_dna_audio(a, self.resolver)
        tid = self.store.propose_trait(dna.Category.PAUSES, "pauses", dna.Layer.OBSERVED, "t")
        for w in wins:
            self.store.add_evidence(tid, dna.evidence_from_window(w, "pause_duration_s"), "t")
        reloaded = dna.DNAStore(self.tmp / "dna")                               # survive persistence
        ev = reloaded.get(tid).evidence_items[0].evidence
        gap = next(g for g in a.gaps if g.record_id == ev.origin.record_id)     # -> Phase 2 record
        self.assertEqual((ev.start_s, ev.end_s), (gap.start_s, gap.end_s))
        self.assertEqual(ev.origin.media_id, a.media_id)                        # -> original media item
        self.assertEqual(ev.source_hash, self.ref.sha256)                        # -> exact original bytes
        self.assertEqual(hashlib.sha256((self.orig / "a.mov").read_bytes()).hexdigest(), ev.source_hash)

    def test_reviewed_text_traces_to_the_asr_segment_and_reviewer(self):
        t = asr_transcript()
        review = make_review(t, "human:ana", [SegmentReview(t.segments[0].record_id, "confirmed")])
        segs, _ = to_dna_transcript(apply_review(t, review), self.resolver)
        cand = dna.RecurringPhraseAnalyzer(["ajde de"]).analyze_transcript(segs)[0]
        self.store.ingest([cand], "t")
        ev = self.store.find(dna.Category.VOCABULARY, cand.statement).evidence_items[0].evidence
        self.assertEqual((ev.origin.record_id, ev.origin.review.review_id, ev.reviewed_by),
                         (t.segments[0].record_id, review.review_id, "human:ana"))
        self.assertEqual(ev.source_hash, SHA)


# ----------------------------------------------- idempotence and one-off handling
class Idempotence(Tmp):
    def handoff(self):
        t = asr_transcript()
        review = make_review(t, "human:ana", [SegmentReview(s.record_id, "confirmed") for s in t.segments])
        return build_handoff(self.resolver, DnaEligibility(), [apply_review(t, review)])

    def test_repeated_integration_does_not_duplicate_dna_evidence(self):
        h = self.handoff()
        an = dna.RecurringPhraseAnalyzer(["ajde de"])
        first = pipeline.ingest_handoff_into_dna(self.store, h, [an])[0]
        n = len(self.store.get(next(iter(self.store.traits))).evidence_items)
        events = self.store._seq
        again = pipeline.ingest_handoff_into_dna(self.store, self.handoff(), [an])[0]
        self.assertEqual((first.evidence_added, again.evidence_added, again.duplicates_skipped), (n, 0, n))
        self.assertEqual(self.store._seq, events)                                # not even a log entry
        self.assertEqual(len(dna.DNAStore(self.tmp / "dna").get(next(iter(self.store.traits))).evidence_items), n)

    def test_one_off_evidence_does_not_become_a_stable_rule(self):
        pipeline.ingest_handoff_into_dna(self.store, self.handoff(), [dna.RecurringPhraseAnalyzer(["ajde de"])])
        trait = next(iter(self.store.traits.values()))
        a = trait.assessment()
        self.assertEqual((a.scope, a.support_sources), (dna.Scope.ONE_OFF, 1))
        self.assertLess(a.confidence, 0.45)

    def test_creator_confirmation_stays_distinguishable_from_observation(self):
        pipeline.ingest_handoff_into_dna(self.store, self.handoff(), [dna.RecurringPhraseAnalyzer(["ajde de"])])
        tid = next(iter(self.store.traits))
        footage = self.store.get(tid).assessment().footage_confidence
        self.store.confirm(tid, "yes, I say that")
        self.assertEqual(self.store.get(tid).assessment().confidence_basis, "creator_confirmation_over_weak_footage")
        corrected = self.store.correct(tid, "Says ajde de to close a thought", "I use it to end a thought")
        ex = self.store.export()
        self.assertEqual(ex["observed_facts"], [])                              # observation was corrected away
        sr = ex["creator_self_report"][0]
        self.assertEqual((sr["trait_id"], sr["layer"], sr["supersedes"]), (corrected, "self_report", tid))
        self.assertEqual(self.store.get(tid).assessment().footage_confidence, footage)
        footage_methods = {e.evidence.method for e in self.store.get(tid).evidence_items}
        self.assertNotIn(dna.Method.CREATOR_STATEMENT, footage_methods)


class Policy(Tmp):
    def test_min_asr_confidence_filters_only_unverified_segments_that_report_one(self):
        t = asr_transcript()
        segs, rep = to_dna_transcript(t, self.resolver, DnaEligibility(include_unverified_transcripts=True, min_asr_confidence=0.5))
        self.assertEqual([s.confidence for s in segs], [0.8, 0.55])
        self.assertIn("below 0.5", rep.skipped[0]["reason"])
        review = make_review(t, "human:ana", [SegmentReview(t.segments[2].record_id, "confirmed")])
        segs, _ = to_dna_transcript(apply_review(t, review), self.resolver,
                                    DnaEligibility(include_unverified_transcripts=True, min_asr_confidence=0.5))
        self.assertEqual([s.verification for s in segs], ["unverified", "unverified", "reviewed"])   # reviewed text is never filtered

    def test_segment_without_engine_confidence_is_kept_with_none(self):
        t = Transcript("m_a", [TranscriptSegment("m_a", 0, 1, "ajde de")], ASR)
        segs, _ = to_dna_transcript(t, self.resolver, DnaEligibility(include_unverified_transcripts=True, min_asr_confidence=0.9))
        self.assertEqual((len(segs), segs[0].confidence), (1, None))

    def test_scene_boundary_is_not_a_topic_change(self):
        obs, _ = to_dna_visual([ShotBoundary("m_a", 14.0, "ffmpeg-scene@0.4", score=0.9)], self.resolver)
        self.assertEqual(obs[0].label, "shot_boundary")
        self.assertNotIn("topic", obs[0].description.lower())


# ------------------------------------------------ central pipeline (creator_state)
def gap_runner(calls):
    def run(argv):
        calls.append(argv)
        make_wav(Path(argv[-1]), [(0.4, 0.3), (0.6, 0.0), (0.4, 0.3)], rate=int(argv[argv.index("-ar") + 1]))
        return 0, "", ""
    return run


class PipelineBase(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name) / "CreatorStudio"
        clip = self.root / "Incoming" / "trip" / "camera" / "a.mov"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"synthetic original " * 50)
        ingest.ingest(self.root, "trip", project_slug="trip", prober=fake_prober(ffprobe_json(duration="1.4")))
        self.project = Project(self.root / "Projects" / "trip")
        self.item = next(i for i in ingest.load_manifest(self.project)["items"] if i["name"] == "a.mov")
        self.mid = self.item["id"]
        self.clip = clip
        self.calls = []

    def ops(self):
        return self.project.load_state()["stages"]["understand"]["operations"]

    def stage_status(self):
        return self.project.load_state()["stages"]["understand"]["status"]


class PipelineTests(PipelineBase):
    def test_stage_must_be_running(self):
        self.assertEqual(self.stage_status(), "pending")
        with self.assertRaises(StateError):
            pipeline.analyze_audio(self.project, self.mid, runner=gap_runner(self.calls))
        self.assertEqual(self.calls, [])

    def test_audio_analysis_is_journaled_and_stored_in_the_project(self):
        pipeline.begin(self.project)
        h0 = hashlib.sha256(self.clip.read_bytes()).hexdigest()
        res = pipeline.analyze_audio(self.project, self.mid, {"threshold_dbfs": -40}, ffmpeg="fake", runner=gap_runner(self.calls))
        a = res["analysis"]
        self.assertEqual([g.position for g in a.gaps], ["between"])
        self.assertAlmostEqual(a.gaps[0].duration_s, 0.6, delta=0.15)
        self.assertEqual(a.analysis_copy_id, res["copy"]["record"]["copy_id"])
        self.assertTrue(str(res["path"].resolve()).startswith(str(self.project.dir)))
        ops = self.ops()
        self.assertEqual(sorted(k.split(":")[0] for k in ops), ["audio", "copy"])
        self.assertTrue(all(o["status"] == "completed" for o in ops.values()))
        self.assertIn("gaps", next(o for k, o in ops.items() if k.startswith("audio"))["result"])
        self.assertEqual(hashlib.sha256(self.clip.read_bytes()).hexdigest(), h0)          # original untouched
        self.assertEqual(self.stage_status(), "in_progress")                              # never completed automatically

    def test_rerun_reuses_copy_and_output(self):
        pipeline.begin(self.project)
        r1 = pipeline.analyze_audio(self.project, self.mid, {"threshold_dbfs": -40}, ffmpeg="fake", runner=gap_runner(self.calls))
        b1 = r1["path"].read_bytes()
        r2 = pipeline.analyze_audio(self.project, self.mid, {"threshold_dbfs": -40}, ffmpeg="fake", runner=gap_runner(self.calls))
        self.assertEqual(len(self.calls), 1)                                              # ffmpeg ran once
        self.assertTrue(r2["copy"]["reused"])
        self.assertEqual((r2["path"], r2["path"].read_bytes()), (r1["path"], b1))
        self.assertEqual(len(self.ops()), 2)

    def test_changed_original_blocks_analysis(self):
        pipeline.begin(self.project)
        self.clip.write_bytes(self.clip.read_bytes() + b"tamper")
        with self.assertRaises(MediaAnalysisError) as cm:
            pipeline.analyze_audio(self.project, self.mid, ffmpeg="fake", runner=gap_runner(self.calls))
        self.assertIn("failed verification", str(cm.exception))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ops(), {})

    def test_unavailable_transcription_fails_visibly(self):
        pipeline.begin(self.project)
        with self.assertRaises(CapabilityError):
            pipeline.transcribe(self.project, self.mid, UnavailableProvider(), "hr", ffmpeg="fake", runner=gap_runner(self.calls))
        failed = [k for k, o in self.ops().items() if o["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].startswith("transcribe:"))
        self.assertFalse(list((pipeline.workspace(self.project) / "transcripts").glob("*")) if
                         (pipeline.workspace(self.project) / "transcripts").exists() else [])

    def fixture_segments(self):
        m = self.mid
        return [TranscriptSegment(m, 0.0, 0.4, "Ajde de, ovo je fejk", language="hr", confidence=0.8),
                TranscriptSegment(m, 0.5, 0.9, "ajde dee to je bilo fejk", language="hr", confidence=0.5),
                TranscriptSegment(m, 1.0, 1.4, "ček ajde de", language="hr", confidence=0.3)]

    def test_full_flow_transcript_review_handoff_and_dna(self):
        pipeline.begin(self.project)
        res = pipeline.transcribe(self.project, self.mid, FixtureProvider(self.fixture_segments()), "hr",
                                  ffmpeg="fake", runner=gap_runner(self.calls))
        t, tpath = res["transcript"], res["path"]
        copy_id = res["transcript"].audio_copy_id
        self.assertEqual(t.verification, "unverified")
        self.assertTrue(copy_id.startswith(self.mid))
        raw = tpath.read_bytes()
        s = t.segments
        review = make_review(t, "human:ana", [SegmentReview(s[0].record_id, "confirmed"),
                                              SegmentReview(s[1].record_id, "corrected", "ajde de to je bilo fejk")])
        pipeline.store_review(self.project, review)
        self.assertEqual(tpath.read_bytes(), raw)                                         # ASR file untouched

        # 1. nothing is attributed yet: reviewed text exists, but nobody has said whose voice it is
        h0 = pipeline.prepare_dna_handoff(self.project)
        self.assertEqual((h0.transcript_segments, h0.audio_windows, h0.visual_observations), ([], [], []))
        reasons = {x["reason"] for x in h0.report.skipped}
        self.assertIn("attribution:ineligible:speaker_unknown", reasons)
        decisions = pipeline.evaluate_eligibility(self.project)
        self.assertTrue(decisions and all(d.decision.value == "ineligible" for d in decisions))
        report = json.loads((pipeline.workspace(self.project) / "eligibility_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["by_decision"], {"ineligible": len(decisions)})

        # 2. a human confirms who speaks (no special recording: the footage itself, once)
        att = pipeline.attribution_store(self.project)
        att.human_confirm_span(self.mid, 0.0, 46.48, Identity.CREATOR, "human:ana", "my own voice, on camera",
                               delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        h = pipeline.prepare_dna_handoff(self.project, attribution=att)                                    # default policy
        self.assertEqual([x.verification for x in h.transcript_segments], ["reviewed", "reviewed"])
        self.assertTrue(all(x.attribution and x.attribution.identity_basis == "human_confirmation" for x in h.transcript_segments))
        manifest = json.loads((pipeline.workspace(self.project) / "dna_handoff.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"]["transcript_segments"], 2)
        self.assertIn("unreviewed ASR (policy excludes it)", {x["reason"] for x in manifest["skipped"]})
        self.assertIn("eligibility_summary", manifest)
        sha = self.item["file"]["sha256"]
        o = h.transcript_segments[1].origin
        self.assertEqual((o.media_id, o.source_sha256, o.source_path, o.analysis_copy_id),
                         (self.mid, sha, self.item["path"], copy_id))
        self.assertEqual(o.review.original_text, "ajde dee to je bilo fejk")

        store = strict_test_store(Path(self.project.dir) / ".." / ".." / "dna_store")  # STRICT store
        an = dna.RecurringPhraseAnalyzer(["ajde de"])
        r1 = pipeline.ingest_handoff_into_dna(store, h, [an])[0]
        self.assertEqual((r1.evidence_added, r1.refused), (2, []))
        tr = next(iter(store.traits.values()))
        self.assertEqual(tr.layer, dna.Layer.OBSERVED)
        self.assertEqual({e.evidence.source_hash for e in tr.evidence_items}, {sha})
        self.assertTrue(all(e.evidence.attribution.decision == "eligible" for e in tr.evidence_items))
        self.assertEqual(tr.assessment().scope, dna.Scope.ONE_OFF)
        h2 = pipeline.prepare_dna_handoff(self.project, attribution=att)
        r2 = pipeline.ingest_handoff_into_dna(store, h2, [an])[0]
        self.assertEqual((r2.evidence_added, r2.duplicates_skipped), (0, 2))              # repeated integration is idempotent

        h3 = pipeline.prepare_dna_handoff(self.project, DnaEligibility(include_unverified_transcripts=True), attribution=att)
        self.assertEqual([x.verification for x in h3.transcript_segments], ["reviewed", "reviewed", "unverified"])

        # 3. the human later corrects part of it: the DNA evidence stamped with the old decision stops counting
        att.human_correct_span(self.mid, 0.0, 0.45, Identity.OTHER_IDENTIFIED, "human:ana", "that was my friend speaking",
                               person_id="person_01", delivery=Delivery.CONVERSATION)
        pipeline.prepare_dna_handoff(self.project, attribution=att)
        from speaker_attribution import sync_revocations
        self.assertEqual(len(sync_revocations(store, att)), 1)
        self.assertEqual(sum(1 for i in tr.evidence_items if i.rejected), 1)

    def test_moment_index_and_stage_completion_use_the_existing_mechanism(self):
        pipeline.begin(self.project)
        pipeline.analyze_audio(self.project, self.mid, {"threshold_dbfs": -40}, ffmpeg="fake", runner=gap_runner(self.calls))
        pipeline.transcribe(self.project, self.mid, FixtureProvider(self.fixture_segments()), "hr",
                            ffmpeg="fake", runner=gap_runner(self.calls))
        path = pipeline.build_moment_index(self.project)
        idx = MomentIndex.load(path)
        kinds = {r.kind for r in idx.evidence.values()}
        self.assertEqual(kinds, {"transcript_segment", "audio_gap"})
        self.assertTrue(all(p["verification"] == "unverified" for p in idx.payloads.values() if "text" in p))
        outs = pipeline.outputs(self.project)
        self.assertIn("analysis/media/moments.json", outs)
        self.assertTrue(all(o.startswith("analysis/media/") for o in outs))
        self.project.complete_stage("understand", outs, "synthetic test completion")
        st = self.project.load_state()["stages"]["understand"]
        self.assertEqual(st["status"], "completed")
        self.assertEqual(len(st["outputs"]), len(outs))                                    # each output hashed by creator_state
        self.assertTrue(all(o.get("sha256") for o in st["outputs"] if o["path"].endswith(".json")))


if __name__ == "__main__":
    unittest.main()
