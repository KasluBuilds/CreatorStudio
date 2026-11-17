"""Tests for speaker_attribution and its Creator DNA enforcement. Synthetic data only.
No real footage is used (in particular NOT MVI_0879.MOV, a friend's narration that must never be DNA evidence).
Run from orchestrator/:  python -I -m unittest discover -s tests -p "test_speaker_attribution.py" -v"""
import dataclasses
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
from media_analysis import (CapabilityError, DnaEligibility, MediaRef, Provenance, QuietGap, SegmentReview,
                            SourceResolver, Transcript, TranscriptSegment, VisualEvent, ShotBoundary, apply_review,
                            make_review)
from media_analysis import audio_analysis as aa
from media_analysis import pipeline
from speaker_attribution import (AttributionError, AttributionEvidence, AttributionLogError, AttributionMethod,
                                 AttributionRequest, AttributionStore, Candidate, Channel, Decision, Delivery,
                                 FixtureProvider, Identity, PolicyConfig, Status, UnavailableProvider,
                                 build_attributed_handoff, candidates_from_audio, candidates_from_transcript,
                                 candidates_from_visual, dna_stamp, run_provider, summarize, sync_revocations)

MID, SHA = "m_a", "a" * 64
H = "human:ana"
ASR = Provenance("fixture-asr", "1", "synthetic_fixture")
REF = MediaRef(MID, "C:/originals/a.mov", None, None, SHA, 60.0)
SPEECH, VISUAL = Channel.SPEECH, Channel.VISUAL


def transcript():
    return Transcript(MID, [
        TranscriptSegment(MID, 10.0, 13.0, "Ajde de, ovo je fejk.", language="hr", confidence=0.8),
        TranscriptSegment(MID, 20.0, 23.0, "pa ajde de nema šanse", language="hr", confidence=0.7)], ASR, "hr")


def reviewed(t):
    return apply_review(t, make_review(t, "human:ana", [SegmentReview(s.record_id, "confirmed") for s in t.segments]))


def audio():
    gap = QuietGap(MID, 20.0, 21.0, -40.0, "between", False)
    ev = aa.EnergyEvent(MID, 12.0, 12.1, "energy_rise", -10.0, -25.0, 15.0)
    return aa.AudioAnalysis(MID, 60.0, 16000, {}, (), (), (gap,), (ev,), Provenance("energy-activity", "1")), gap, ev


WAVE = VisualEvent(MID, 2.0, 3.0, "gesture:wave", "m", "hand wave", 0.7)
CUT = ShotBoundary(MID, 14.0, "ffmpeg-scene@0.4", score=0.9)


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.n = 0
        self.sa = AttributionStore(self.tmp / "attr", clock=self.clock)
        self.dna = strict_test_store(self.tmp / "dna", clock=self.clock)       # STRICT store: verifier + consent (test doubles)
        self.resolver = SourceResolver({MID: REF})
        self.t = transcript()
        self.seg1, self.seg2 = self.t.segments

    def clock(self):
        self.n += 1
        return f"2026-01-01T00:00:{self.n:02d}+00:00"

    # ---- convenience
    def confirm(self, s, e, identity, **kw):
        return self.sa.human_confirm_span(MID, s, e, identity, H, kw.pop("why", "checked by ear"), **kw)

    def decide_transcript(self, src=None, seg=None):
        t = src or self.t
        ds = self.sa.evaluate(candidates_from_transcript(t, self.resolver.refs))
        rid = (seg or self.seg1).record_id
        return next(d for d in ds if d.candidate.record_id == rid)

    def assertDecision(self, d, decision, code):
        self.assertEqual((d.decision, d.reason_code), (decision, code), d.reason)

    def handoff(self, src=None, policy=DnaEligibility(), aud=None, vis=()):
        return build_attributed_handoff(self.resolver, self.sa, policy, [src or self.t], [aud] if aud else [], vis)

    def learn_all(self, h):
        """Push every item of a handoff into the strict DNA store the way analyzers would; return evidence count."""
        before = sum(len(t.evidence_items) for t in self.dna.traits.values())
        for c in dna.RecurringPhraseAnalyzer(["ajde de"]).analyze_transcript(h.transcript_segments):
            self.dna.ingest([c], "an@1")
        for w in h.audio_windows:
            tid = self.dna.propose_trait(dna.Category.PAUSES, "pauses", dna.Layer.HYPOTHESIS, "an@1")
            e = dna.evidence_from_window(w, "pause_duration_s") or dna.evidence_from_window(w, "energy_rise_db")
            self.dna.add_evidence(tid, e, "an@1")
        for o in h.visual_observations:
            tid = self.dna.propose_trait(dna.Category.GESTURE, "waves", dna.Layer.HYPOTHESIS, "an@1")
            self.dna.add_evidence(tid, dna.evidence_from_visual(o), "an@1")
        return sum(len(t.evidence_items) for t in self.dna.traits.values()) - before


# ============================================================ the 20 required scenarios
class Scenarios(Base):
    def test_01_creator_speaking_directly_to_camera(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        self.confirm(0, 30, Identity.CREATOR, channel=VISUAL)
        d = self.decide_transcript(reviewed(self.t))
        self.assertDecision(d, Decision.ELIGIBLE, "creator_speech_confirmed")
        self.assertEqual((d.max_evidence_grade, d.transcript_review, d.identity_basis), ("verbatim", "reviewed", "human_confirmation"))
        self.assertGreaterEqual(d.confidence, 0.9)

    def test_02_creator_narrating_a_voiceover(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.VOICEOVER)         # creator not visible at all
        self.assertDecision(self.decide_transcript(), Decision.ELIGIBLE, "creator_voiceover_confirmed")

    def test_03_friend_narrating_footage(self):
        self.confirm(0, 30, Identity.OTHER_IDENTIFIED, person_id="person_01", delivery=Delivery.NARRATION)
        d = self.decide_transcript()
        self.assertDecision(d, Decision.INELIGIBLE, "other_speaker")
        self.assertIn("person_01", d.reason)
        self.assertEqual(self.handoff(reviewed(self.t)).transcript_segments, [])

    def test_04_friend_speaking_while_creator_is_visible(self):
        self.confirm(0, 30, Identity.OTHER_IDENTIFIED, person_id="person_01", delivery=Delivery.CONVERSATION)
        self.confirm(0, 30, Identity.CREATOR, channel=VISUAL)                       # creator on screen, listening
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "other_speaker")
        d = self.sa.evaluate(candidates_from_visual([WAVE]))[0]
        self.assertDecision(d, Decision.ELIGIBLE, "creator_visual_confirmed")        # visual channel is independent

    def test_05_creator_speaking_while_not_visible(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.OFF_CAMERA_SPEECH)
        self.assertDecision(self.decide_transcript(), Decision.ELIGIBLE, "creator_speech_confirmed")
        self.assertDecision(self.sa.evaluate(candidates_from_visual([WAVE]))[0], Decision.INELIGIBLE, "visual_subject_unknown")

    def test_06_unknown_narrator(self):
        self.confirm(0, 30, Identity.UNKNOWN, delivery=Delivery.NARRATION)
        d = self.decide_transcript()
        self.assertDecision(d, Decision.INELIGIBLE, "speaker_unknown")
        self.assertIn("unknown narrator", d.reason)
        self.assertEqual(self.sa.resolve(MID, SPEECH, 10, 13).identity, Identity.UNKNOWN)

    def test_07_background_television_or_playback_audio(self):
        self.confirm(0, 30, Identity.UNKNOWN, delivery=Delivery.PLAYBACK_OR_BACKGROUND_AUDIO)
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "playback_or_background_audio")

    def test_07b_even_the_creators_own_voice_in_playback_is_not_live_delivery(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.PLAYBACK_OR_BACKGROUND_AUDIO)
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "playback_or_background_audio")

    def test_08_multiple_overlapping_speakers(self):
        self.confirm(0, 30, Identity.MULTIPLE_OR_OVERLAPPING, delivery=Delivery.CONVERSATION)
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "overlapping_speakers")

    def test_08b_creator_contribution_isolated_inside_overlap(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.CONVERSATION)
        self.confirm(11, 12, Identity.OTHER_IDENTIFIED, person_id="person_01", delivery=Delivery.CONVERSATION)
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "mixed_speakers_in_candidate")
        self.confirm(10, 13, Identity.CREATOR, delivery=Delivery.CONVERSATION, isolated=True,
                     why="the friend's interjection is cut out of this segment's words")
        self.assertDecision(self.decide_transcript(), Decision.ELIGIBLE, "creator_speech_confirmed")

    def test_09_creator_visible_but_not_speaking(self):
        self.confirm(0, 30, Identity.CREATOR, channel=VISUAL)
        self.assertDecision(self.sa.evaluate(candidates_from_visual([WAVE]))[0], Decision.ELIGIBLE, "creator_visual_confirmed")
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "speaker_unknown")   # presence is not speech
        a, gap, _ = audio()
        gap_d = next(d for d in self.sa.evaluate(candidates_from_audio(a)) if d.candidate.kind == "audio_gap")
        self.assertDecision(gap_d, Decision.INELIGIBLE, "speaker_unknown")

    def test_10_unknown_person_visibly_speaking(self):
        self.confirm(0, 30, Identity.UNKNOWN, channel=VISUAL)
        self.confirm(0, 30, Identity.UNKNOWN, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        self.assertDecision(self.sa.evaluate(candidates_from_visual([WAVE]))[0], Decision.INELIGIBLE, "visual_subject_unknown")
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "speaker_unknown")

    def test_11_speaker_clustering_without_identity_attribution(self):
        ev = [AttributionEvidence(MID, SPEECH, AttributionMethod.DIARIZATION_CLUSTER, "provider:fixture@1", "run1",
                                  "voices grouped by a fixture", start_s=0, end_s=30, cluster_id="spk_0",
                                  provider="fixture@1", confidence=0.99)]
        out = run_provider(self.sa, FixtureProvider(ev), AttributionRequest(REF))
        self.assertEqual(out["added"], 1)
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertEqual((r.identity, r.status, r.cluster_without_identity), (Identity.UNKNOWN, Status.UNATTRIBUTED, True))
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "cluster_without_identity")
        with self.assertRaises(AttributionError):        # a cluster can never be an identity
            AttributionEvidence(MID, SPEECH, AttributionMethod.DIARIZATION_CLUSTER, "provider:x", "r", "why",
                                start_s=0, end_s=1, cluster_id="c", provider="p", identity=Identity.CREATOR)

    def test_12_human_confirmation_of_an_unknown_speaker(self):
        self.confirm(0, 30, Identity.UNKNOWN, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "speaker_unknown")
        self.sa.human_correct_span(MID, 0, 30, Identity.CREATOR, H, "that is me, I recognise my own voice",
                                   delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        d = self.decide_transcript()
        self.assertDecision(d, Decision.ELIGIBLE, "creator_speech_confirmed")
        mine = [x for x in self.sa.decisions(history=True) if x.candidate.record_id == self.seg1.record_id]
        self.assertEqual([x.reason_code for x in mine], ["speaker_unknown", "creator_speech_confirmed"])   # both on record
        self.assertEqual(d.supersedes, mine[0].decision_id)

    def test_12b_human_maps_a_cluster_to_the_creator(self):
        run_provider(self.sa, FixtureProvider([AttributionEvidence(
            MID, SPEECH, AttributionMethod.DIARIZATION_CLUSTER, "provider:fixture@1", "run1", "grouped", start_s=0,
            end_s=30, cluster_id="spk_0", provider="fixture@1", confidence=0.95)]), AttributionRequest(REF))
        self.sa.map_cluster(MID, "fixture@1", "spk_0", Identity.CREATOR, H, "spk_0 is me in this video",
                            delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        d = self.decide_transcript()
        self.assertDecision(d, Decision.ELIGIBLE, "creator_speech_confirmed")
        self.assertIn("cluster_mapping", d.identity_basis)
        self.assertAlmostEqual(d.confidence, 0.95 * 0.95, places=3)               # human confidence x membership confidence

    def test_12c_cluster_without_membership_confidence_cannot_be_eligible(self):
        run_provider(self.sa, FixtureProvider([AttributionEvidence(
            MID, SPEECH, AttributionMethod.DIARIZATION_CLUSTER, "provider:fixture@1", "run1", "grouped", start_s=0,
            end_s=30, cluster_id="spk_0", provider="fixture@1")]), AttributionRequest(REF))
        self.sa.map_cluster(MID, "fixture@1", "spk_0", Identity.CREATOR, H, "me", delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertEqual((r.status, r.confidence), (Status.CONFIRMED_VIA_CLUSTER, None))     # not invented
        self.assertDecision(self.decide_transcript(), Decision.REQUIRES_REVIEW, "attribution_confidence_unquantified")

    def test_12d_cluster_mapped_to_a_friend_is_ineligible(self):
        run_provider(self.sa, FixtureProvider([AttributionEvidence(
            MID, SPEECH, AttributionMethod.DIARIZATION_CLUSTER, "provider:fixture@1", "run1", "grouped", start_s=0,
            end_s=30, cluster_id="spk_1", provider="fixture@1", confidence=0.9)]), AttributionRequest(REF))
        self.sa.map_cluster(MID, "fixture@1", "spk_1", Identity.OTHER_IDENTIFIED, H, "that is my friend",
                            person_id="person_01")
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "other_speaker")

    def test_13_human_correction_of_a_previously_incorrect_attribution_revokes_dna_evidence(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        rt = reviewed(self.t)
        self.assertEqual(self.learn_all(self.handoff(rt)), 2)
        trait = next(iter(self.dna.traits.values()))
        self.assertGreater(trait.assessment().confidence, 0.3)
        # the human realises 8-15 s was the friend, not the creator
        self.sa.human_correct_span(MID, 8, 15, Identity.OTHER_IDENTIFIED, H, "that part is my friend",
                                   person_id="person_01", delivery=Delivery.CONVERSATION)
        self.assertDecision(self.decide_transcript(rt), Decision.INELIGIBLE, "other_speaker")
        self.assertDecision(self.decide_transcript(rt, self.seg2), Decision.ELIGIBLE, "creator_speech_confirmed")  # outside the correction
        revoked = sync_revocations(self.dna, self.sa)
        self.assertEqual(len(revoked), 1)
        items = {i.evidence.start_s: i for i in trait.evidence_items}
        self.assertTrue(items[10.0].rejected)
        self.assertIn("attribution revoked", items[10.0].review_note)
        self.assertFalse(items[20.0].rejected)                                         # still creator speech
        self.assertEqual(len(trait.evidence_items), 2)                                 # nothing deleted
        self.assertEqual(sync_revocations(self.dna, self.sa), [])                      # idempotent
        self.assertEqual(self.learn_all(self.handoff(rt)), 0)                          # the corrected segment is not re-learned

    def test_14_creator_attributed_speech_with_unreviewed_transcript(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        d = self.decide_transcript()
        self.assertDecision(d, Decision.ELIGIBLE, "creator_speech_confirmed")
        self.assertEqual((d.transcript_review, d.max_evidence_grade), ("unverified", "asr_unverified_only"))
        self.assertEqual(self.handoff().transcript_segments, [])                       # default policy: no unreviewed ASR
        h = self.handoff(policy=DnaEligibility(include_unverified_transcripts=True))
        self.assertEqual({s.verification for s in h.transcript_segments}, {"unverified"})
        self.assertTrue(all(s.attribution is not None for s in h.transcript_segments))
        self.learn_all(h)
        tr = next(iter(self.dna.traits.values()))
        self.assertEqual((tr.layer, {i.evidence.method for i in tr.evidence_items}),
                         (dna.Layer.HYPOTHESIS, {dna.Method.ASR_UNVERIFIED}))          # attribution did not verify the wording

    def test_15_creator_attributed_speech_with_reviewed_transcript(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        h = self.handoff(reviewed(self.t))
        self.assertEqual({s.verification for s in h.transcript_segments}, {"reviewed"})
        self.learn_all(h)
        tr = next(iter(self.dna.traits.values()))
        self.assertEqual((tr.layer, {(i.evidence.method, i.evidence.reviewed_by) for i in tr.evidence_items}),
                         (dna.Layer.OBSERVED, {(dna.Method.VERBATIM, "human:ana")}))
        ev = tr.evidence_items[0].evidence
        self.assertEqual((ev.attribution.channel, ev.attribution.identity_basis, ev.source_hash), ("speech", "human_confirmation", SHA))
        self.assertEqual(ev.origin.record_id, ev.attribution.candidate_record_id)

    def test_16_visual_creator_behaviour_is_eligible_independently_of_speech(self):
        self.confirm(0, 30, Identity.UNKNOWN, delivery=Delivery.PLAYBACK_OR_BACKGROUND_AUDIO)   # speech unusable
        self.confirm(0, 30, Identity.CREATOR, channel=VISUAL)
        h = self.handoff(reviewed(self.t), vis=[WAVE])
        self.assertEqual(h.transcript_segments, [])
        self.assertEqual(len(h.visual_observations), 1)
        self.assertEqual(h.visual_observations[0].attribution.channel, "visual")
        self.assertEqual(self.learn_all(h), 1)
        self.assertEqual(self.dna.get(next(iter(self.dna.traits))).assessment().unrated_examples, 0)   # confidence 0.7 supplied

    def test_17_unknown_visual_subject(self):
        self.assertDecision(self.sa.evaluate(candidates_from_visual([WAVE]))[0], Decision.INELIGIBLE, "visual_subject_unknown")
        self.confirm(0, 30, Identity.OTHER_IDENTIFIED, person_id="p2", channel=VISUAL)
        self.assertDecision(self.sa.evaluate(candidates_from_visual([WAVE]))[0], Decision.INELIGIBLE, "other_person_visible")
        self.assertEqual(self.handoff(vis=[WAVE]).visual_observations, [])

    def test_17b_a_scene_boundary_is_never_person_behaviour(self):
        self.confirm(0, 30, Identity.CREATOR, channel=VISUAL)
        d = self.sa.evaluate(candidates_from_visual([CUT]))[0]
        self.assertDecision(d, Decision.INELIGIBLE, "not_person_behavior")
        self.assertEqual(self.handoff(vis=[CUT]).visual_observations, [])

    def test_18_repeated_attribution_evidence_and_idempotency(self):
        first = self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        self.assertIsNone(self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH))
        self.assertTrue(first.startswith("ae_"))
        rt = reviewed(self.t)
        d1 = self.sa.evaluate(candidates_from_transcript(rt, self.resolver.refs))
        seq = self.sa._seq
        d2 = self.sa.evaluate(candidates_from_transcript(rt, self.resolver.refs))
        self.assertEqual(([x.decision_id for x in d1]), [x.decision_id for x in d2])
        self.assertEqual(self.sa._seq, seq)                                           # no new events at all
        self.assertEqual(self.learn_all(self.handoff(rt)), 2)
        self.assertEqual(self.learn_all(self.handoff(rt)), 0)                         # DNA evidence not duplicated
        again = AttributionStore(self.tmp / "attr")                                   # replay gives the same state
        self.assertEqual([d.decision_id for d in again.decisions()], [d.decision_id for d in self.sa.decisions()])

    def test_19_contradictory_attribution_evidence(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        self.sa.human_confirm_span(MID, 0, 30, Identity.OTHER_IDENTIFIED, "human:bo", "I hear the friend",
                                   person_id="person_01", delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertEqual((r.status, r.identity), (Status.CONFLICTED, Identity.UNKNOWN))
        self.assertDecision(self.decide_transcript(), Decision.REQUIRES_REVIEW, "conflicting_attribution_evidence")
        self.assertEqual(self.handoff(reviewed(self.t)).transcript_segments, [])
        self.sa.human_correct_span(MID, 0, 30, Identity.CREATOR, H, "listened again: it is me",
                                   delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        self.assertDecision(self.decide_transcript(), Decision.ELIGIBLE, "creator_speech_confirmed")

    def test_19b_supporting_evidence_alone_never_makes_a_creator_eligible(self):
        for method, ident in ((AttributionMethod.AV_SYNC, Identity.CREATOR), (AttributionMethod.VISUAL_CONTEXT, Identity.CREATOR)):
            self.sa.add_evidence(AttributionEvidence(MID, SPEECH, method, "provider:fixture@1", "r", "lips match",
                                                     start_s=0, end_s=30, identity=ident, confidence=0.99))
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertEqual((r.status, r.identity), (Status.PROPOSED, Identity.CREATOR))
        self.assertLessEqual(r.confidence, 0.70)                                       # capped: cannot approach "confirmed"
        self.assertDecision(self.decide_transcript(), Decision.REQUIRES_REVIEW, "creator_proposed_unconfirmed")
        self.assertEqual(self.handoff(reviewed(self.t)).transcript_segments, [])

    def test_19c_conflicting_supporting_evidence(self):
        self.sa.add_evidence(AttributionEvidence(MID, SPEECH, AttributionMethod.AV_SYNC, "provider:x@1", "r", "w", start_s=0,
                                                 end_s=30, identity=Identity.CREATOR, confidence=0.9))
        self.sa.add_evidence(AttributionEvidence(MID, SPEECH, AttributionMethod.CONTINUITY, "provider:y@1", "r", "w", start_s=0,
                                                 end_s=30, identity=Identity.OTHER_IDENTIFIED, person_id="p9", confidence=0.9))
        self.assertEqual(self.sa.resolve(MID, SPEECH, 10, 13).status, Status.CONFLICTED)
        self.assertDecision(self.decide_transcript(), Decision.REQUIRES_REVIEW, "conflicting_attribution_evidence")

    def test_20_missing_or_incomplete_provenance(self):
        base = dict(media_id=MID, channel=SPEECH, method=AttributionMethod.HUMAN_CONFIRMATION, actor=H,
                    source_ref="note", rationale="why", start_s=0, end_s=1, identity=Identity.CREATOR)
        bad = [dict(rationale=" "), dict(source_ref=""), dict(actor="ana"), dict(actor="provider:x"), dict(media_id=""),
               dict(start_s=None), dict(end_s=-1), dict(identity=None), dict(identity=Identity.OTHER_IDENTIFIED),
               dict(person_id="p1"), dict(confidence=1.5), dict(confidence=True),
               dict(method=AttributionMethod.HUMAN_CORRECTION),             # a correction must name what it overrides
               dict(isolated=True, identity=Identity.UNKNOWN), dict(channel=VISUAL, delivery=Delivery.VOICEOVER),
               dict(channel=VISUAL, identity=Identity.NON_SPEECH),
               dict(method=AttributionMethod.AV_SYNC)]                        # a human cannot hide behind a supporting method
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(AttributionError):
                AttributionEvidence(**{**base, **kw})
        with self.assertRaises(AttributionError):                            # overriding unknown evidence
            self.sa.add_evidence(AttributionEvidence(**{**base, "method": AttributionMethod.HUMAN_CORRECTION,
                                                        "supersedes": ("ae_nope",)}))
        with self.assertRaises(AttributionError):
            self.sa.human_correct_span(MID, 0, 1, Identity.CREATOR, H, "x")      # nothing to correct
        with self.assertRaises(AttributionError):
            Candidate("transcript_segment", "seg_x", MID, 0, 1)                  # transcript state is mandatory
        with self.assertRaises(AttributionError):
            Candidate("energy_event", "e", MID, 0, 1, transcript_state="reviewed")


# ================================================== no leakage: uncertain material never reaches DNA
class NoLeak(Base):
    SETUPS = {
        "no_evidence": lambda s: None,
        "unknown": lambda s: s.confirm(0, 30, Identity.UNKNOWN, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH),
        "other": lambda s: s.confirm(0, 30, Identity.OTHER_IDENTIFIED, person_id="p1", delivery=Delivery.NARRATION),
        "playback": lambda s: s.confirm(0, 30, Identity.CREATOR, delivery=Delivery.PLAYBACK_OR_BACKGROUND_AUDIO),
        "overlap": lambda s: s.confirm(0, 30, Identity.MULTIPLE_OR_OVERLAPPING),
        "creator_delivery_unknown": lambda s: s.confirm(0, 30, Identity.CREATOR),
        "supporting_only": lambda s: s.sa.add_evidence(AttributionEvidence(
            MID, SPEECH, AttributionMethod.AV_SYNC, "provider:x@1", "r", "w", start_s=0, end_s=30,
            identity=Identity.CREATOR, confidence=0.99)),
        "low_confidence": lambda s: s.confirm(0, 30, Identity.CREATOR, delivery=Delivery.VOICEOVER, confidence=0.5),
    }

    def test_nothing_uncertain_reaches_the_dna_store_through_the_handoff(self):
        a, gap, ev = audio()
        for name, setup in self.SETUPS.items():
            with self.subTest(setup=name):
                self.setUp()
                setup(self)
                h = self.handoff(reviewed(self.t), DnaEligibility(include_unverified_transcripts=True), a)
                self.assertEqual((h.transcript_segments, h.audio_windows), ([], []), name)
                self.assertEqual(self.learn_all(h), 0)
                self.assertEqual(self.dna.traits, {})
                kept = [s for s in h.report.skipped if s["reason"].startswith("attribution:")]
                self.assertTrue(kept, "every exclusion carries a machine-readable reason")

    def test_the_dna_store_itself_refuses_unattributed_footage_evidence(self):
        tid = self.dna.propose_trait(dna.Category.VOCABULARY, "Says ajde de", dna.Layer.HYPOTHESIS, "t")
        asr = dna.Evidence(MID, dna.Modality.TRANSCRIPT, dna.Method.ASR_UNVERIFIED, start_s=1, end_s=2, quote="ajde de", strength=0.9)
        with self.assertRaises(dna.DNAError) as cm:
            self.dna.add_evidence(tid, asr, "t")
        self.assertIn("unattributed footage evidence refused", str(cm.exception))
        c = dna.TraitCandidate(dna.Category.VOCABULARY, "x", dna.Layer.HYPOTHESIS, [asr])
        rep = self.dna.ingest([c], "t")
        self.assertEqual((rep.traits_created, rep.evidence_added), ([], 0))
        self.assertIn("unattributed", rep.refused[0]["reason"])
        for raw in (dna.AudioWindow(MID, 1, 2, {"pause_duration_s": 1.0}), dna.VisualObservation(MID, 1, 2, "wave", "w", 0.5),
                    dna.TranscriptSegment(MID, 1, 2, "ajde de")):
            self.assertIsNone(raw.attribution)                                             # never implied
        self.assertEqual(self.dna.traits[tid].evidence_items, [])

    def test_an_attribution_stamp_cannot_describe_anyone_but_an_eligible_creator(self):
        ok = dict(decision_id="dec_1", candidate_record_id="seg_1", media_id=MID, channel="speech", start_s=1, end_s=2,
                  identity_basis="human_confirmation", confidence=0.9, policy_version="1", attribution_evidence_ids=("ae_1",))
        dna.EvidenceAttribution(**ok)
        for kw in (dict(subject="other"), dict(subject="unknown"), dict(decision="ineligible"), dict(decision="requires_review"),
                   dict(confidence=None), dict(confidence=0), dict(confidence=True), dict(channel="audio"),
                   dict(attribution_evidence_ids=()), dict(decision_id=""), dict(end_s=0.5)):
            with self.subTest(kw=kw), self.assertRaises(dna.DNAError):
                dna.EvidenceAttribution(**{**ok, **kw})

    def test_stamp_must_match_the_evidence_it_is_attached_to(self):
        stamp = dna.EvidenceAttribution("dec_1", "seg_1", MID, "speech", 1.0, 2.0, "human_confirmation", 0.9, "1", attribution_evidence_ids=("ae_1",))
        base = dict(source_id=MID, modality=dna.Modality.TRANSCRIPT, method=dna.Method.ASR_UNVERIFIED, start_s=1.0, end_s=2.0,
                    quote="x", attribution=stamp)
        dna.Evidence(**base)
        for kw in (dict(modality=dna.Modality.VISUAL, method=dna.Method.AI_ESTIMATE, quote=None, note="n"),   # speech stamp, visual evidence
                   dict(start_s=5.0, end_s=6.0),                                                              # outside the decided range
                   dict(start_s=None, end_s=None),                                                            # untimed
                   dict(origin=dna.Origin("other_media", record_id="seg_1")),
                   dict(origin=dna.Origin(MID, record_id="seg_other"))):
            with self.subTest(kw=kw), self.assertRaises(dna.DNAError):
                dna.Evidence(**{**base, **kw})
        with self.assertRaises(dna.DNAError):                                                                 # creator statements carry none
            dna.Evidence(MID, dna.Modality.FEEDBACK, dna.Method.CREATOR_STATEMENT, attribution=stamp)

    def test_only_eligible_decisions_can_be_stamped(self):
        d = self.decide_transcript()
        self.assertEqual(d.decision, Decision.INELIGIBLE)
        with self.assertRaises(AttributionError):
            dna_stamp(d)

    def test_unattributed_legacy_evidence_loaded_from_a_log_is_visible_but_never_counted(self):
        lenient = LenientStore(self.tmp / "legacy")
        tid = lenient.propose_trait(dna.Category.VOCABULARY, "Says ajde de", dna.Layer.HYPOTHESIS, "t")
        lenient.add_evidence(tid, dna.Evidence(MID, dna.Modality.TRANSCRIPT, dna.Method.ASR_UNVERIFIED, start_s=1, end_s=2,
                                               quote="ajde de", strength=0.9), "t")
        self.assertGreater(lenient.get(tid).assessment().confidence, 0)
        p = lenient.log_path
        p.write_text(p.read_text(encoding="utf-8").replace('"attribution": {', '"attribution_removed": {'), encoding="utf-8")
        # simulate a v2 log: evidence without a stamp
        lines = []
        for line in p.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev["type"] == "evidence_added":
                ev["data"]["evidence"]["attribution"] = None
                ev["data"]["evidence"].pop("attribution_removed", None)
            lines.append(json.dumps(ev, ensure_ascii=False, sort_keys=True))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        strict = dna.DNAStore(self.tmp / "legacy")
        a = strict.get(tid).assessment()
        self.assertEqual((a.unattributed_examples, a.support_examples, a.confidence), (1, 0, 0.0))
        self.assertTrue(any("no creator attribution" in w for w in a.explanation))
        self.assertEqual(len(strict.export()["hypotheses"][0]["support"]), 1)             # still listed

    def test_revoke_attribution_requires_a_reason(self):
        with self.assertRaises(dna.DNAError):
            self.dna.revoke_attribution("dec_x", " ", "t")


# ====================================================================== resolution rules
class Resolution(Base):
    def test_union_coverage_of_adjacent_same_identity_evidence(self):
        self.confirm(0, 11, Identity.CREATOR, delivery=Delivery.VOICEOVER)
        self.assertEqual(self.sa.resolve(MID, SPEECH, 10, 13).status, Status.UNATTRIBUTED)       # not covered yet
        self.confirm(11, 30, Identity.CREATOR, delivery=Delivery.VOICEOVER)
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertEqual((r.identity, r.status), (Identity.CREATOR, Status.CONFIRMED))

    def test_partial_evidence_alone_is_not_an_attribution(self):
        self.confirm(10, 11, Identity.CREATOR, delivery=Delivery.VOICEOVER)
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertEqual((r.identity, r.status, r.mixed), (Identity.UNKNOWN, Status.UNATTRIBUTED, False))
        self.assertTrue(r.partial_ids)
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "speaker_unknown")

    def test_correction_overrides_only_inside_its_range(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.CONVERSATION)
        self.sa.human_correct_span(MID, 9, 14, Identity.OTHER_IDENTIFIED, H, "friend", person_id="p1", delivery=Delivery.CONVERSATION)
        inside, outside = self.sa.resolve(MID, SPEECH, 10, 13), self.sa.resolve(MID, SPEECH, 20, 23)
        straddle = self.sa.resolve(MID, SPEECH, 12, 16)
        self.assertEqual((inside.identity, outside.identity), (Identity.OTHER_IDENTIFIED, Identity.CREATOR))
        self.assertTrue(inside.overridden_ids)
        self.assertEqual((straddle.identity, straddle.mixed), (Identity.CREATOR, True))          # cannot be isolated

    def test_human_outranks_project_metadata_and_metadata_alone_can_be_eligible(self):
        self.sa.project_metadata(MID, 0, 30, Identity.CREATOR, "project:notes", "notes/clip_a.txt", "notes say: creator voiceover",
                                 delivery=Delivery.VOICEOVER)
        d = self.decide_transcript()
        self.assertDecision(d, Decision.ELIGIBLE, "creator_voiceover_confirmed")
        self.assertEqual(d.identity_basis, "project_metadata")
        self.assertAlmostEqual(d.confidence, 0.85)
        self.confirm(0, 30, Identity.OTHER_IDENTIFIED, person_id="p1", delivery=Delivery.VOICEOVER)
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertEqual((r.identity, r.status), (Identity.OTHER_IDENTIFIED, Status.CONFIRMED))   # human beats metadata
        self.assertIn("overruled", " ".join(r.explanation))

    def test_delivery_conflict_and_unknown_delivery_need_review(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.VOICEOVER)
        self.sa.human_confirm_span(MID, 0, 30, Identity.CREATOR, "human:bo", "I think it is on camera", delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertTrue(r.delivery_conflict)
        self.assertDecision(self.decide_transcript(), Decision.REQUIRES_REVIEW, "delivery_conflict")

    def test_supporting_delivery_claims_are_not_trusted(self):
        self.confirm(0, 30, Identity.CREATOR)
        self.sa.add_evidence(AttributionEvidence(MID, SPEECH, AttributionMethod.AV_SYNC, "provider:x@1", "r", "lips match", start_s=0,
                                                 end_s=30, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH))
        r = self.sa.resolve(MID, SPEECH, 10, 13)
        self.assertEqual((r.delivery, r.proposed_delivery), (Delivery.UNKNOWN, ("on_camera_direct_speech",)))
        self.assertDecision(self.decide_transcript(), Decision.REQUIRES_REVIEW, "delivery_unknown")

    def test_low_confidence_human_statement_needs_review(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.VOICEOVER, confidence=0.6)
        self.assertDecision(self.decide_transcript(), Decision.REQUIRES_REVIEW, "low_attribution_confidence")

    def test_evidence_for_another_recording_or_channel_never_applies(self):
        self.sa.human_confirm_span("m_other", 0, 30, Identity.CREATOR, H, "other video", delivery=Delivery.VOICEOVER)
        self.confirm(0, 30, Identity.CREATOR, channel=VISUAL)
        self.assertEqual(self.sa.resolve(MID, SPEECH, 10, 13).status, Status.UNATTRIBUTED)
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "speaker_unknown")

    def test_cluster_ids_are_provider_and_recording_local(self):
        run_provider(self.sa, FixtureProvider([AttributionEvidence(
            MID, SPEECH, AttributionMethod.DIARIZATION_CLUSTER, "provider:p1@1", "r", "g", start_s=0, end_s=30, cluster_id="spk_0",
            provider="p1@1", confidence=0.9)], name="p1"), AttributionRequest(REF))
        self.sa.map_cluster(MID, "p2@1", "spk_0", Identity.CREATOR, H, "other provider's spk_0", delivery=Delivery.VOICEOVER)
        self.sa.map_cluster("m_other", "p1@1", "spk_0", Identity.CREATOR, H, "other video's spk_0", delivery=Delivery.VOICEOVER)
        self.assertEqual(self.sa.resolve(MID, SPEECH, 10, 13).identity, Identity.UNKNOWN)

    def test_same_dialect_is_not_same_person(self):
        # two voices that sound alike are two clusters; mapping one to the creator says nothing about the other
        for cid, conf in (("spk_0", 0.95), ("spk_1", 0.95)):
            self.sa.add_evidence(AttributionEvidence(MID, SPEECH, AttributionMethod.DIARIZATION_CLUSTER, "provider:p@1", "r",
                                                     "same accent", start_s=0 if cid == "spk_0" else 15, end_s=15 if cid == "spk_0" else 30,
                                                     cluster_id=cid, provider="p@1", confidence=conf))
        self.sa.map_cluster(MID, "p@1", "spk_0", Identity.CREATOR, H, "me", delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        self.assertEqual(self.sa.resolve(MID, SPEECH, 10, 13).identity, Identity.CREATOR)
        self.assertEqual(self.sa.resolve(MID, SPEECH, 20, 23).identity, Identity.UNKNOWN)

    def test_pause_belongs_to_the_creator_only_between_creator_speech(self):
        a, gap, ev = audio()
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        gd = next(d for d in self.sa.evaluate(candidates_from_audio(a)) if d.candidate.kind == "audio_gap")
        self.assertDecision(gd, Decision.ELIGIBLE, "creator_pause_between_creator_speech")
        self.sa.human_correct_span(MID, 20.9, 30, Identity.OTHER_IDENTIFIED, H, "friend answers after the pause", person_id="p1",
                                   delivery=Delivery.CONVERSATION)
        gd = next(d for d in self.sa.evaluate(candidates_from_audio(a)) if d.candidate.kind == "audio_gap")
        self.assertDecision(gd, Decision.INELIGIBLE, "gap_not_between_creator_speech")
        edge = Candidate("audio_gap", "gap_edge", MID, 0, 1, position="leading")
        self.assertDecision(self.sa.evaluate([edge])[0], Decision.INELIGIBLE, "gap_without_two_sided_speech")

    def test_energy_event_follows_the_speech_attribution_at_its_time(self):
        a, gap, ev = audio()
        self.confirm(0, 30, Identity.OTHER_IDENTIFIED, person_id="p1", delivery=Delivery.CONVERSATION)
        e = next(d for d in self.sa.evaluate(candidates_from_audio(a)) if d.candidate.kind == "energy_event")
        self.assertDecision(e, Decision.INELIGIBLE, "other_speaker")

    def decide_transcript(self, src=None, seg=None):
        return Base.decide_transcript(self, src, seg)

    def assertDecision(self, d, decision, code):
        Base.assertDecision(self, d, decision, code)


# =================================================================== providers (honesty)
class Providers(Base):
    def test_unavailable_provider_is_recorded_and_attributes_nobody(self):
        with self.assertRaises(CapabilityError):
            run_provider(self.sa, UnavailableProvider(), AttributionRequest(REF))
        self.assertEqual([r["status"] for r in self.sa.provider_runs], ["unavailable"])
        self.assertEqual(self.sa.all_evidence(), [])
        self.assertDecision(self.decide_transcript(), Decision.INELIGIBLE, "speaker_unknown")
        self.assertEqual(AttributionStore(self.tmp / "attr").provider_runs[0]["provider"], "none")      # survives reload

    def test_providers_cannot_produce_authoritative_or_undeclared_or_foreign_evidence(self):
        human = AttributionEvidence(MID, SPEECH, AttributionMethod.HUMAN_CONFIRMATION, H, "r", "w", start_s=0, end_s=1, identity=Identity.CREATOR)
        sync = AttributionEvidence(MID, SPEECH, AttributionMethod.AV_SYNC, "provider:x@1", "r", "w", start_s=0, end_s=1, identity=Identity.CREATOR)
        foreign = dataclasses.replace(sync, media_id="m_other")

        class Careless(FixtureProvider):                  # a provider that does not filter by recording
            def attribute(self, request):
                return list(self._evidence)
        wrong_sha = dataclasses.replace(sync, media_sha256="b" * 64)
        for ev in (human, foreign, wrong_sha):
            with self.subTest(ev=ev.method.value, media=ev.media_id), self.assertRaises(AttributionError):
                run_provider(self.sa, Careless([ev]), AttributionRequest(REF))
        undeclared = FixtureProvider([sync])
        undeclared._caps = dataclasses.replace(undeclared._caps, methods=())
        with self.assertRaises(AttributionError):
            run_provider(self.sa, undeclared, AttributionRequest(REF))
        self.assertEqual(self.sa.all_evidence(), [])
        self.assertTrue(all(r["status"] == "failed" for r in self.sa.provider_runs))

    def test_repeated_provider_runs_are_idempotent(self):
        sync = AttributionEvidence(MID, SPEECH, AttributionMethod.AV_SYNC, "provider:x@1", "r", "w", start_s=0, end_s=1, identity=Identity.CREATOR)
        self.assertEqual(run_provider(self.sa, FixtureProvider([sync]), AttributionRequest(REF))["added"], 1)
        self.assertEqual(run_provider(self.sa, FixtureProvider([sync]), AttributionRequest(REF))["duplicates"], 1)
        self.assertEqual(len(self.sa.all_evidence()), 1)

    def test_a_provider_run_never_changes_who_the_creator_is(self):
        sync = AttributionEvidence(MID, SPEECH, AttributionMethod.AV_SYNC, "provider:x@1", "r", "w", start_s=0, end_s=30,
                                   identity=Identity.CREATOR, confidence=0.99)
        run_provider(self.sa, FixtureProvider([sync]), AttributionRequest(REF))
        self.assertEqual(self.sa.resolve(MID, SPEECH, 10, 13).status, Status.PROPOSED)

    def decide_transcript(self, src=None, seg=None):
        return Base.decide_transcript(self, src, seg)

    def assertDecision(self, d, decision, code):
        Base.assertDecision(self, d, decision, code)


# ================================================================== store and persistence
class StoreTests(Base):
    def test_log_is_append_only_strict_and_replayable(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.VOICEOVER)
        self.decide_transcript(reviewed(self.t))
        snap = self.sa.log_path.read_bytes()
        self.confirm(0, 5, Identity.UNKNOWN)
        self.assertTrue(self.sa.log_path.read_bytes().startswith(snap))
        good = self.sa.log_path.read_bytes()
        self.sa.log_path.write_bytes(good[:-4])
        with self.assertRaises(AttributionLogError):
            AttributionStore(self.tmp / "attr")
        self.sa.log_path.write_bytes(good.replace(b'"seq": 2', b'"seq": 7'))
        with self.assertRaises(AttributionLogError):
            AttributionStore(self.tmp / "attr")
        self.sa.log_path.write_bytes(good)
        again = AttributionStore(self.tmp / "attr")
        self.assertEqual(again.resolve(MID, SPEECH, 10, 13).identity, Identity.CREATOR)

    def test_tampered_decision_is_detected(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.VOICEOVER)
        self.decide_transcript()
        raw = self.sa.log_path.read_text(encoding="utf-8").replace('"decision": "eligible"', '"decision": "ineligible"')
        raw = raw.replace('"decision_id"', '"decision_id"')
        self.sa.log_path.write_text(raw, encoding="utf-8")
        with self.assertRaises(AttributionLogError):
            AttributionStore(self.tmp / "attr")

    def test_second_writer_detected(self):
        other = AttributionStore(self.tmp / "attr")
        other.confirm = None
        other.human_confirm_span(MID, 0, 1, Identity.UNKNOWN, H, "x")
        with self.assertRaises(AttributionLogError):
            self.confirm(0, 2, Identity.UNKNOWN)

    def test_every_decision_is_kept_with_its_provenance_and_summarised(self):
        self.confirm(0, 30, Identity.CREATOR, delivery=Delivery.ON_CAMERA_DIRECT_SPEECH)
        a, gap, ev = audio()
        ds = self.sa.evaluate(candidates_from_transcript(self.t, self.resolver.refs) + candidates_from_audio(a, refs=self.resolver.refs)
                              + candidates_from_visual([WAVE, CUT], self.resolver.refs))
        s = summarize(ds)
        self.assertEqual(s["total"], 6)
        self.assertEqual(s["by_decision"]["ineligible"], 2)                       # the visual event and the shot boundary
        for d in ds:
            self.assertTrue(d.reason_code and d.reason and d.policy_version == "1")
            self.assertEqual(d.candidate.media_sha256, SHA)
            if d.decision is Decision.ELIGIBLE:
                self.assertTrue(d.attribution_evidence_ids)
        rows = json.loads((self.sa.log_path.read_text(encoding="utf-8").splitlines()[-1]))
        self.assertEqual(rows["type"], "decision_recorded")

    def test_policy_thresholds_are_configurable_and_recorded(self):
        strict = AttributionStore(self.tmp / "strict", policy=PolicyConfig(min_eligible_confidence=0.99))
        strict.human_confirm_span(MID, 0, 30, Identity.CREATOR, H, "me", delivery=Delivery.VOICEOVER)
        d = strict.evaluate(candidates_from_transcript(self.t, self.resolver.refs))[0]
        self.assertDecision(d, Decision.REQUIRES_REVIEW, "low_attribution_confidence")


if __name__ == "__main__":
    unittest.main()
