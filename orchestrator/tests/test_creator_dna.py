"""Tests for creator_dna (synthetic fixtures only; no real footage).
Run from orchestrator/:  python -I -m unittest discover -s tests -p "test_creator_dna.py" -v"""
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dna_lenient import LenientStore
from creator_dna import (AudioWindow, Category, DNAError, DNALogError, DNAStore, Evidence, Layer,
                         Method, Modality, Origin, Polarity, RecurringPhraseAnalyzer, ReviewInfo, Scope, Status,
                         TraitCandidate, TranscriptSegment, VisualObservation, evidence_from_visual,
                         evidence_from_window, quote_in_transcript, trait_id_for, validate_metrics)
from creator_dna import confidence as conf
from creator_dna.inputs import AudioAnalyzer, TranscriptAnalyzer, VisualAnalyzer


def verb(src, t, quote, ctx=(), pol=Polarity.SUPPORT, date=None):
    return Evidence(source_id=src, modality=Modality.TRANSCRIPT, method=Method.VERBATIM, polarity=pol,
                    start_s=t, end_s=t + 2, quote=quote, context=ctx, source_date=date,
                    reviewed_by="human:tester")


def ai(src, t, note, strength=0.6, pol=Polarity.SUPPORT):
    return Evidence(source_id=src, modality=Modality.VISUAL, method=Method.AI_ESTIMATE, polarity=pol,
                    start_s=t, end_s=t + 1, note=note, strength=strength)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "dna"
        self.n = 0
        self.store = self.new_store()

    def clock(self):
        self.n += 1
        return f"2026-01-01T00:00:{self.n:02d}+00:00"

    def new_store(self):
        return LenientStore(self.dir, clock=self.clock)   # strict-store rules are tested in tests/test_speaker_attribution.py

    def phrase_trait(self, sources=("a",), layer=Layer.OBSERVED):
        tid = self.store.propose_trait(Category.VOCABULARY, "Says 'fiktivno'", layer, "t")
        for s in sources:
            self.store.add_evidence(tid, verb(s, 1.0, "fiktivno"), "t")
        return tid


class EvidenceModel(unittest.TestCase):
    def test_verbatim_requires_quote(self):
        with self.assertRaises(DNAError):
            Evidence("a", Modality.TRANSCRIPT, Method.VERBATIM, start_s=0, end_s=1)

    def test_quote_only_for_verbatim(self):
        with self.assertRaises(DNAError):
            Evidence("a", Modality.VISUAL, Method.AI_ESTIMATE, quote="paraphrase")

    def test_bad_times_and_strength(self):
        with self.assertRaises(DNAError):
            verb("a", 5, "x").__class__("a", Modality.TRANSCRIPT, Method.VERBATIM, start_s=5, end_s=1, quote="x")
        with self.assertRaises(DNAError):
            Evidence("a", Modality.VISUAL, Method.AI_ESTIMATE, strength=1.5)

    def test_feedback_modality_pairing(self):
        with self.assertRaises(DNAError):
            Evidence("a", Modality.AUDIO, Method.CREATOR_STATEMENT)
        with self.assertRaises(DNAError):
            Evidence("a", Modality.FEEDBACK, Method.MEASURED)

    def test_id_is_stable_and_content_based(self):
        a, b = verb("a", 1, "Fiktivno"), verb("a", 1, "  fiktivno ")
        self.assertEqual(a.evidence_id, b.evidence_id)       # normalisation
        self.assertNotEqual(a.evidence_id, verb("b", 1, "fiktivno").evidence_id)

    def test_diacritics_not_conflated(self):
        self.assertNotEqual(trait_id_for(Category.VOCABULARY, "čaša"), trait_id_for(Category.VOCABULARY, "casa"))

    def test_roundtrip(self):
        e = verb("a", 1, "x", ctx=("b", "a"))
        self.assertEqual(Evidence.from_dict(e.to_dict()), e)
        self.assertEqual(e.context, ("a", "b"))


class Confidence(Base):
    def conf_of(self, tid):
        return self.store.get(tid).assessment()

    def test_single_source_is_one_off_and_low(self):
        a = self.conf_of(self.phrase_trait(("a",)))
        self.assertEqual(a.scope, Scope.ONE_OFF)
        self.assertEqual(a.band, "low")
        self.assertAlmostEqual(a.confidence, 1 - 2.718281828 ** -0.5, places=2)

    def test_many_examples_same_source_do_not_make_a_habit(self):
        tid = self.store.propose_trait(Category.HUMOR, "callback joke", Layer.OBSERVED, "t")
        for i in range(10):
            self.store.add_evidence(tid, verb("a", i * 10, f"joke {i}"), "t")
        a = self.conf_of(tid)
        self.assertEqual(a.support_sources, 1)
        self.assertEqual(a.scope, Scope.ONE_OFF)
        self.assertLessEqual(a.support_mass, 1.0)

    def test_confidence_grows_with_independent_sources(self):
        vals = []
        for n in (1, 2, 3, 5):
            self.setUp()
            vals.append(self.conf_of(self.phrase_trait(tuple("abcde"[:n]))).confidence)
        self.assertEqual(vals, sorted(vals))
        self.assertLess(vals[0], 0.4)
        self.assertGreater(vals[-1], 0.85)

    def test_stable_needs_three_sources_and_consistency(self):
        self.assertEqual(self.conf_of(self.phrase_trait(("a", "b"))).scope, Scope.EMERGING)
        self.setUp()
        self.assertEqual(self.conf_of(self.phrase_trait(("a", "b", "c"))).scope, Scope.STABLE)

    def test_single_shared_context_is_contextual(self):
        tid = self.store.propose_trait(Category.DELIVERY, "high energy", Layer.OBSERVED, "t")
        for s in "abc":
            self.store.add_evidence(tid, verb(s, 1, "ajme", ctx=("topic:diy",)), "t")
        self.assertEqual(self.conf_of(tid).scope, Scope.CONTEXTUAL)

    def test_counterexamples_lower_confidence_and_make_contextual(self):
        tid = self.phrase_trait(("a", "b", "c"))
        before = self.conf_of(tid)
        self.store.add_evidence(tid, verb("d", 3, "nije rekao", pol=Polarity.COUNTER), "t")
        self.store.add_evidence(tid, verb("e", 3, "ni ovdje", pol=Polarity.COUNTER), "t")
        after = self.conf_of(tid)
        self.assertLess(after.confidence, before.confidence)
        self.assertEqual(after.scope, Scope.CONTEXTUAL)
        self.assertEqual(after.counter_sources, 2)

    def test_layer_caps(self):
        t_obs = self.phrase_trait(tuple("abcdefgh"))
        self.assertLessEqual(self.conf_of(t_obs).confidence, 0.95)
        hyp = self.store.propose_trait(Category.EXPRESSION, "raises eyebrow when ironic", Layer.HYPOTHESIS, "t")
        for s in "abcdefgh":
            self.store.add_evidence(hyp, ai(s, 1, "eyebrow", strength=1.0), "t")
        self.assertLessEqual(self.conf_of(hyp).confidence, 0.5)

    def test_ai_estimate_weighs_less_than_verbatim(self):
        interp = self.store.propose_trait(Category.EXPRESSION, "smiles at punchline", Layer.INTERPRETATION, "t")
        self.store.add_evidence(interp, ai("a", 1, "smile", strength=0.6), "t")
        self.assertAlmostEqual(self.conf_of(interp).support_mass, 0.3)

    def test_last_observed_date(self):
        tid = self.store.propose_trait(Category.VOCABULARY, "x", Layer.OBSERVED, "t")
        self.store.add_evidence(tid, verb("a", 1, "x", date="2026-03-01"), "t")
        self.store.add_evidence(tid, verb("b", 1, "x", date="2026-05-01"), "t")
        self.assertEqual(self.conf_of(tid).last_observed, "2026-05-01")

    def test_explain_mentions_numbers(self):
        text = self.store.explain(self.phrase_trait(("a", "b")))
        self.assertIn("support_score", text)
        self.assertIn("scope emerging", text)


class Layers(Base):
    def test_observed_fact_cannot_rest_on_ai_estimate(self):
        tid = self.store.propose_trait(Category.EXPRESSION, "looks amused", Layer.OBSERVED, "t")
        with self.assertRaises(DNAError):
            self.store.add_evidence(tid, ai("a", 1, "smile"), "t")
        self.assertEqual(self.store.get(tid).evidence_items, [])

    def test_failed_validation_does_not_reach_log(self):
        tid = self.store.propose_trait(Category.EXPRESSION, "looks amused", Layer.OBSERVED, "t")
        size = self.store.log_path.stat().st_size
        with self.assertRaises(DNAError):
            self.store.add_evidence(tid, ai("a", 1, "smile"), "t")
        self.assertEqual(self.store.log_path.stat().st_size, size)

    def test_based_on_must_be_observed(self):
        obs = self.phrase_trait()
        interp = self.store.propose_trait(Category.HUMOR, "dry humour", Layer.INTERPRETATION, "t", based_on=[obs])
        self.assertEqual(self.store.get(interp).based_on, (obs,))
        with self.assertRaises(DNAError):
            self.store.propose_trait(Category.HUMOR, "other", Layer.INTERPRETATION, "t", based_on=[interp])
        with self.assertRaises(DNAError):
            self.store.propose_trait(Category.HUMOR, "third", Layer.OBSERVED, "t", based_on=[obs])

    def test_reclassify(self):
        hyp = self.store.propose_trait(Category.HUMOR, "maybe irony", Layer.HYPOTHESIS, "t")
        self.store.add_evidence(hyp, ai("a", 1, "x"), "t")
        self.store.reclassify(hyp, Layer.INTERPRETATION, "reviewer", "seen in 3 videos")
        self.assertEqual(self.store.get(hyp).layer, Layer.INTERPRETATION)
        with self.assertRaises(DNAError):
            self.store.reclassify(hyp, Layer.OBSERVED, "reviewer", "no")
        with self.assertRaises(DNAError):
            self.store.reclassify(hyp, Layer.OBSERVED, "reviewer", "")

    def test_export_sections_are_separate(self):
        self.phrase_trait()
        h = self.store.propose_trait(Category.EXPRESSION, "hyp", Layer.HYPOTHESIS, "t")
        self.store.add_evidence(h, ai("a", 1, "x"), "t")
        ex = self.store.export()
        self.assertEqual(len(ex["observed_facts"]), 1)
        self.assertEqual(len(ex["hypotheses"]), 1)
        self.assertEqual(ex["ai_interpretations"], [])
        json.dumps(ex, ensure_ascii=False)


class Incremental(Base):
    def cand(self, src, t=1.0, statement="Says 'fiktivno'"):
        return TraitCandidate(Category.VOCABULARY, statement, Layer.OBSERVED, [verb(src, t, "fiktivno")])

    def test_ingest_merges_and_never_overwrites(self):
        r1 = self.store.ingest([self.cand("a")], "an@1")
        r2 = self.store.ingest([self.cand("b")], "an@1")
        self.assertEqual(len(r1.traits_created), 1)
        self.assertEqual(r2.traits_created, [])
        tr = self.store.find(Category.VOCABULARY, "Says 'fiktivno'")
        self.assertEqual([i.evidence.source_id for i in tr.evidence_items], ["a", "b"])

    def test_reingest_is_idempotent(self):
        self.store.ingest([self.cand("a")], "an@1")
        events = self.store._seq
        r = self.store.ingest([self.cand("a")], "an@1")
        self.assertEqual(r.duplicates_skipped, 1)
        self.assertEqual(r.evidence_added, 0)
        self.assertEqual(self.store._seq, events)

    def test_counter_only_candidate_cannot_create_trait(self):
        c = TraitCandidate(Category.VOCABULARY, "ghost", Layer.OBSERVED,
                           [verb("a", 1, "x", pol=Polarity.COUNTER)])
        r = self.store.ingest([c], "an@1")
        self.assertEqual(len(r.refused), 1)
        self.assertEqual(self.store.traits, {})

    def test_counterexample_added_to_existing(self):
        self.store.ingest([self.cand("a")], "an@1")
        c = TraitCandidate(Category.VOCABULARY, "Says 'fiktivno'", Layer.OBSERVED,
                           [verb("b", 5, "umjesto toga", pol=Polarity.COUNTER)])
        self.store.ingest([c], "an@1")
        tr = self.store.find(Category.VOCABULARY, "Says 'fiktivno'")
        self.assertEqual(tr.assessment().counter_examples, 1)

    def test_transcript_verification_refuses_invented_quotes(self):
        segs = [TranscriptSegment("a", 0, 3, "Ovo je fiktivno, kužiš", verification="reviewed", reviewer="human:tester")]
        good = TraitCandidate(Category.VOCABULARY, "real", Layer.OBSERVED, [verb("a", 1, "fiktivno")])
        fake = TraitCandidate(Category.VOCABULARY, "fake", Layer.OBSERVED, [verb("a", 1, "nikad rečeno")])
        r = self.store.ingest([good, fake], "an@1", transcript=segs)
        self.assertEqual(len(r.traits_created), 1)
        self.assertEqual(len(r.refused), 2)    # quote refused + trait without evidence refused
        self.assertIsNone(self.store.find(Category.VOCABULARY, "fake"))

    def test_quote_wrong_timecode_not_verified(self):
        segs = [TranscriptSegment("a", 0, 3, "fiktivno", verification="reviewed", reviewer="human:tester")]
        self.assertTrue(quote_in_transcript(verb("a", 1, "FIKTIVNO"), segs))
        self.assertFalse(quote_in_transcript(verb("a", 50, "fiktivno"), segs))
        self.assertFalse(quote_in_transcript(verb("zzz", 1, "fiktivno"), segs))

    def test_persistence_roundtrip(self):
        self.phrase_trait(("a", "b"))
        before = self.store.export()
        again = self.new_store()
        self.assertEqual(again.export()["observed_facts"], before["observed_facts"])
        tid = trait_id_for(Category.VOCABULARY, "Says 'fiktivno'")
        again.add_evidence(tid, verb("c", 1, "fiktivno"), "t")    # keeps appending
        self.assertEqual(len(self.new_store().get(tid).evidence_items), 3)

    def test_log_is_append_only(self):
        tid = self.phrase_trait(("a",))
        snapshot = self.store.log_path.read_bytes()
        self.store.add_evidence(tid, verb("b", 1, "fiktivno"), "t")
        self.store.reject(tid, "no")
        self.assertTrue(self.store.log_path.read_bytes().startswith(snapshot))

    def test_corrupt_and_torn_logs_are_detected(self):
        self.phrase_trait()
        good = self.store.log_path.read_bytes()
        self.store.log_path.write_bytes(good[:-5])
        with self.assertRaises(DNALogError):
            self.new_store()
        self.store.log_path.write_bytes(good.replace(b'"seq": 2', b'"seq": 9'))
        with self.assertRaises(DNALogError):
            self.new_store()
        self.store.log_path.write_bytes(good)
        self.new_store()

    def test_second_writer_detected(self):
        tid = self.phrase_trait()
        other = self.new_store()
        other.add_evidence(tid, verb("z", 1, "fiktivno"), "t")
        with self.assertRaises(DNALogError):
            self.store.add_evidence(tid, verb("y", 1, "fiktivno"), "t")


class CreatorFeedback(Base):
    def test_confirm_sets_floor_and_keeps_counterexamples(self):
        tid = self.phrase_trait(("a",))
        self.store.add_evidence(tid, verb("b", 1, "ne", pol=Polarity.COUNTER), "t")
        self.store.confirm(tid, "yes that's me", scope=Scope.STABLE)
        a = self.store.get(tid).assessment()
        self.assertGreaterEqual(a.confidence, 0.95)
        self.assertEqual((a.scope, a.scope_source), (Scope.STABLE, "creator_declared"))
        self.assertEqual(a.counter_examples, 1)
        self.assertEqual(self.store.get(tid).status, Status.CONFIRMED)

    def test_reject_excludes_and_is_not_resurrected_by_learning(self):
        tid = self.phrase_trait(("a", "b"))
        self.store.reject(tid, "I don't talk like that")
        self.assertEqual(self.store.get(tid).assessment().confidence, 0.0)
        self.assertNotIn(tid, [t.trait_id for t in self.store.active_traits()])
        r = self.store.ingest([TraitCandidate(Category.VOCABULARY, "Says 'fiktivno'", Layer.OBSERVED,
                                              [verb("c", 1, "fiktivno"), verb("d", 1, "fiktivno")])], "an@1")
        self.assertTrue(any("rejected" in n for n in r.notes))
        self.assertEqual(self.store.get(tid).status, Status.REJECTED)
        self.assertNotIn(tid, [t.trait_id for t in self.store.active_traits()])
        self.assertEqual(len(self.store.get(tid).evidence_items), 4)     # evidence kept
        self.assertIn(tid, [x["trait_id"] for x in self.store.export()["creator_rejected_or_corrected"]])

    def test_reopen(self):
        tid = self.phrase_trait()
        self.store.reject(tid, "x")
        self.store.reopen(tid, "changed my mind")
        self.assertEqual(self.store.get(tid).status, Status.ACTIVE)
        with self.assertRaises(DNAError):
            self.store.reopen(tid)

    def test_correct_creates_creator_authored_trait_and_keeps_old(self):
        tid = self.phrase_trait(("a",))
        new = self.store.correct(tid, "Says 'fiktivno' only when joking", "only when I'm joking")
        old, nt = self.store.get(tid), self.store.get(new)
        self.assertEqual((old.status, old.superseded_by), (Status.CORRECTED, new))
        self.assertTrue(nt.creator_authored)
        self.assertEqual(nt.supersedes, tid)
        self.assertGreaterEqual(nt.assessment().confidence, 0.95)
        self.assertEqual(len(old.evidence_items), 1)
        active = [t.trait_id for t in self.store.active_traits()]
        self.assertIn(new, active)
        self.assertNotIn(tid, active)
        with self.assertRaises(DNAError):
            self.store.correct(tid, "again", "note")
        with self.assertRaises(DNAError):
            self.store.correct(new, "blank", "")

    def test_reject_single_evidence_item(self):
        tid = self.phrase_trait(("a", "b", "c"))
        item = self.store.get(tid).evidence_items[0]
        before = self.store.get(tid).assessment()
        self.store.reject_evidence(tid, item.evidence_id, "that was a quote, not me")
        after = self.store.get(tid).assessment()
        self.assertEqual(after.support_sources, 2)
        self.assertLess(after.confidence, before.confidence)
        self.assertEqual(len(self.store.get(tid).evidence_items), 3)       # still stored
        self.store.restore_evidence(tid, item.evidence_id)
        self.assertEqual(self.store.get(tid).assessment().support_sources, 3)
        with self.assertRaises(DNAError):
            self.store.reject_evidence(tid, "ev_nope", "x")

    def test_creator_statement_on_footage_trait_is_tracked_apart_from_footage(self):
        tid = self.store.propose_trait(Category.HUMOR, "uses absurd humour", Layer.OBSERVED, "t")
        self.store.add_evidence(tid, verb("a", 1, "absurdno"), "t")
        before = self.store.get(tid).assessment()
        self.store.add_creator_statement(tid, "I like absurd jokes")
        a = self.store.get(tid).assessment()
        self.assertEqual((a.support_mass, a.support_sources), (before.support_mass, before.support_sources))
        self.assertEqual(a.confidence, before.confidence)               # self-report adds no footage confidence
        self.assertEqual(a.self_report["statements_support"], 1)
        self.assertFalse(a.self_report["counted_in_footage_confidence"])
        origins = {e["evidence_origin"] for e in self.store.export()["observed_facts"][0]["support"]}
        self.assertEqual(origins, {"footage", "creator_self_report"})

    def test_self_report_layer_is_scored_from_statements_and_unassessed(self):
        tid = self.store.propose_trait(Category.HUMOR, "likes absurd humour", Layer.SELF_REPORT, "t")
        self.store.add_creator_statement(tid, "I like absurd jokes")
        a = self.store.get(tid).assessment()
        self.assertAlmostEqual(a.support_mass, 0.7)
        self.assertEqual(a.scope, Scope.UNASSESSED)
        with self.assertRaises(DNAError):                                 # footage cannot back a self-report
            self.store.add_evidence(tid, verb("a", 1, "x"), "t")

    def test_feedback_survives_reload(self):
        tid = self.phrase_trait()
        self.store.reject(tid, "no")
        self.assertEqual(self.new_store().get(tid).status, Status.REJECTED)

    def test_unknown_trait(self):
        with self.assertRaises(DNAError):
            self.store.confirm("tr_missing")


class Interfaces(Base):
    SEGS = [
        TranscriptSegment("vid1", 0, 4, "Ajde de, ovo je fiktivno.", language="hr", context=("topic:diy",), verification="reviewed", reviewer="human:tester"),
        TranscriptSegment("vid1", 10, 14, "Pa ajde  DE, nema šanse.", context=("topic:diy",), verification="reviewed", reviewer="human:tester"),
        TranscriptSegment("vid2", 0, 3, "Ajdemo dalje.", verification="reviewed", reviewer="human:tester"),
    ]

    def test_phrase_analyzer_finds_verbatim_only(self):
        an = RecurringPhraseAnalyzer(["ajde de", "nepostoji"])
        self.assertIsInstance(an, TranscriptAnalyzer)
        cands = an.analyze_transcript(self.SEGS)
        self.assertEqual(len(cands), 1)
        quotes = [e.quote for e in cands[0].evidence]
        self.assertEqual(quotes, ["Ajde de", "ajde  DE"])           # exactly as spoken
        self.assertEqual(cands[0].layer, Layer.OBSERVED)
        r = self.store.ingest(cands, "recurring-phrase@1", transcript=self.SEGS)
        self.assertEqual((r.evidence_added, r.refused), (2, []))

    def test_whole_word_matching(self):
        an = RecurringPhraseAnalyzer(["ajde"])
        quotes = [e.quote for c in an.analyze_transcript(self.SEGS) for e in c.evidence]
        self.assertEqual(quotes, ["Ajde", "ajde"])               # not "Ajdemo"

    def test_future_input_shapes_and_protocols(self):
        class FakeVisual:
            name, version = "fake-visual", "0"
            def analyze_visual(self, obs):
                return [TraitCandidate(Category.GESTURE, "Waves hand while explaining", Layer.INTERPRETATION,
                                       [Evidence(o.source_id, Modality.VISUAL, Method.AI_ESTIMATE,
                                                 start_s=o.start_s, end_s=o.end_s, note=o.description,
                                                 strength=o.estimate_confidence) for o in obs])]

        class FakeAudio:
            name, version = "fake-audio", "0"
            def analyze_audio(self, windows):
                return [TraitCandidate(Category.PAUSES, "Pauses >1s before punchlines", Layer.OBSERVED,
                                       [Evidence(w.source_id, Modality.AUDIO, Method.MEASURED,
                                                 start_s=w.start_s, end_s=w.end_s,
                                                 note=f"pause {w.metrics['pause_duration_s']}s")
                                        for w in windows if w.metrics["pause_duration_s"] > 1])]

        self.assertIsInstance(FakeVisual(), VisualAnalyzer)
        self.assertIsInstance(FakeAudio(), AudioAnalyzer)
        v = FakeVisual().analyze_visual([VisualObservation("vid1", 2, 3, "gesture:wave", "hand wave", 0.4)])
        a = FakeAudio().analyze_audio([AudioWindow("vid1", 5, 7, {"pause_duration_s": 1.4}),
                                       AudioWindow("vid1", 8, 9, {"pause_duration_s": 0.2})])
        r = self.store.ingest(v + a, "fakes@0")
        self.assertEqual((len(r.traits_created), r.evidence_added, r.refused), (2, 2, []))
        # a visual analyzer cannot sneak AI estimates in as observed facts
        bad = FakeVisual().analyze_visual([VisualObservation("vid1", 2, 3, "x", "y", 0.4)])
        bad[0].layer = Layer.OBSERVED
        bad[0].statement = "different"
        r = self.store.ingest(bad, "fakes@0")
        self.assertEqual(len(r.refused), 1)

    def test_write_export(self):
        self.phrase_trait()
        p = self.store.write_export()
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["observed_facts"][0]["support"][0]["quote"], "fiktivno")


# ------------------------------------------------- Phase 2 <-> 3 contract hardening
SHA = "a" * 64
RV = dict(verification="reviewed", reviewer="human:tester")


class VerbatimVersusAsr(Base):
    def test_verbatim_requires_a_named_reviewer(self):
        base = dict(source_id="a", modality=Modality.TRANSCRIPT, method=Method.VERBATIM, quote="x")
        for bad in ({}, {"reviewed_by": "tester"}, {"reviewed_by": "human:"}, {"reviewed_by": ""}):
            with self.subTest(bad=bad), self.assertRaises(DNAError):
                Evidence(**{**base, **bad})
        Evidence(**base, reviewed_by="human:ana")

    def test_asr_unverified_is_quoted_but_never_reviewed(self):
        base = dict(source_id="a", modality=Modality.TRANSCRIPT, method=Method.ASR_UNVERIFIED)
        with self.assertRaises(DNAError):
            Evidence(**base)                                        # needs the recognised text
        with self.assertRaises(DNAError):
            Evidence(**base, quote="x", reviewed_by="human:ana")    # cannot claim a reviewer
        with self.assertRaises(DNAError):
            Evidence("a", Modality.AUDIO, Method.ASR_UNVERIFIED, quote="x")
        self.assertIsNone(Evidence(**base, quote="x").reviewed_by)

    def test_unverified_asr_cannot_back_an_observed_fact(self):
        obs = self.store.propose_trait(Category.VOCABULARY, "Says fiktivno", Layer.OBSERVED, "t")
        asr = Evidence("a", Modality.TRANSCRIPT, Method.ASR_UNVERIFIED, start_s=1, end_s=2, quote="fiktivno", strength=0.9)
        with self.assertRaises(DNAError) as cm:
            self.store.add_evidence(obs, asr, "t")
        self.assertIn("asr_unverified", str(cm.exception))
        hyp = self.store.propose_trait(Category.VOCABULARY, "maybe fiktivno", Layer.HYPOTHESIS, "t")
        self.assertIsNotNone(self.store.add_evidence(hyp, asr, "t"))     # allowed as a hypothesis
        self.assertLessEqual(self.store.get(hyp).assessment().confidence, 0.5)

    def test_segment_verification_is_explicit_and_never_implied(self):
        self.assertEqual(TranscriptSegment("a", 0, 1, "x").verification, "unverified")
        self.assertIsNone(TranscriptSegment("a", 0, 1, "x").reviewer)
        with self.assertRaises(DNAError):
            TranscriptSegment("a", 0, 1, "x", verification="reviewed")             # reviewer missing
        with self.assertRaises(DNAError):
            TranscriptSegment("a", 0, 1, "x", verification="reviewed", reviewer="ana")
        with self.assertRaises(DNAError):
            TranscriptSegment("a", 0, 1, "x", reviewer="human:ana")                # unverified + reviewer
        with self.assertRaises(DNAError):
            TranscriptSegment("a", 0, 1, "x", verification="verified")

    def test_analyzer_uses_asr_method_and_hypothesis_for_unverified_text(self):
        segs = [TranscriptSegment("a", 0, 3, "Ajde de, ovo", confidence=0.8),
                TranscriptSegment("b", 0, 3, "ajde de opet", **RV)]
        c = RecurringPhraseAnalyzer(["ajde de"]).analyze_transcript(segs)[0]
        self.assertEqual(c.layer, Layer.HYPOTHESIS)                   # one unverified occurrence is enough
        self.assertEqual([e.method for e in c.evidence], [Method.ASR_UNVERIFIED, Method.VERBATIM])
        self.assertEqual(c.evidence[0].strength, 0.8)
        self.assertEqual(c.evidence[1].reviewed_by, "human:tester")
        only_rev = RecurringPhraseAnalyzer(["ajde de"]).analyze_transcript(segs[1:])[0]
        self.assertEqual(only_rev.layer, Layer.OBSERVED)

    def test_verbatim_is_not_confirmed_by_unverified_text(self):
        unverified = [TranscriptSegment("a", 0, 3, "ovo je fiktivno")]
        claim = Evidence("a", Modality.TRANSCRIPT, Method.VERBATIM, start_s=1, end_s=2, quote="fiktivno",
                         reviewed_by="human:tester")
        self.assertFalse(quote_in_transcript(claim, unverified))
        other_reviewer = [TranscriptSegment("a", 0, 3, "ovo je fiktivno", verification="reviewed", reviewer="human:other")]
        self.assertFalse(quote_in_transcript(claim, other_reviewer))
        reviewed = [TranscriptSegment("a", 0, 3, "ovo je fiktivno", **RV)]
        self.assertTrue(quote_in_transcript(claim, reviewed))
        rep = self.store.ingest([TraitCandidate(Category.VOCABULARY, "claimed", Layer.OBSERVED, [claim])],
                                "t", transcript=unverified)
        self.assertEqual(rep.traits_created, [])
        self.assertIn("reviewed by the same reviewer", rep.refused[0]["reason"])
        asr = Evidence("a", Modality.TRANSCRIPT, Method.ASR_UNVERIFIED, start_s=1, end_s=2, quote="fiktivno")
        self.assertTrue(quote_in_transcript(asr, unverified))          # an ASR quote may be found in ASR text

    def test_reviewed_by_and_origin_survive_the_event_log(self):
        origin = Origin("m_a", SHA, "C:/x/a.mov", "seg_1", "transcription", "eng@1", None, "m_a_audio_wav_x",
                        ReviewInfo("human:tester", "rev_1", "corrected", "seg_1", "orig text"))
        ev = Evidence("a", Modality.TRANSCRIPT, Method.VERBATIM, start_s=1, end_s=2, quote="fiktivno",
                      reviewed_by="human:tester", source_hash=SHA, origin=origin)
        tid = self.store.propose_trait(Category.VOCABULARY, "Says fiktivno", Layer.OBSERVED, "t")
        self.store.add_evidence(tid, ev, "t")
        back = self.new_store().get(tid).evidence_items[0].evidence
        self.assertEqual(dataclasses.replace(back, attribution=None), ev)       # the fixture added only the stamp
        self.assertEqual(back.origin.review.original_text, "orig text")
        self.assertEqual(back.origin.source_sha256, SHA)

    def test_schema_v1_verbatim_without_reviewer_is_refused_on_load(self):
        self.phrase_trait(("a",))
        raw = self.store.log_path.read_text(encoding="utf-8").replace('"reviewed_by": "human:tester"', '"reviewed_by": null')
        self.store.log_path.write_text(raw, encoding="utf-8")
        with self.assertRaises(DNALogError):
            self.new_store()

    def test_origin_validation(self):
        for bad in (dict(media_id=""), dict(media_id="m", source_sha256="xyz"), dict(media_id="m", record_kind="opinion")):
            with self.subTest(bad=bad), self.assertRaises(DNAError):
                Origin(**bad)
        with self.assertRaises(DNAError):
            ReviewInfo("ana", "r", "confirmed")
        with self.assertRaises(DNAError):
            ReviewInfo("human:ana", "r", "maybe")


class OptionalVisualConfidence(Base):
    def test_missing_confidence_stays_none(self):
        o = VisualObservation("a", 1.0, 1.0, "shot_boundary", "cut")
        self.assertIsNone(o.estimate_confidence)
        ev = evidence_from_visual(o)
        self.assertIsNone(ev.strength)
        self.assertEqual(Evidence.from_dict(ev.to_dict()).strength, None)

    def test_supplied_confidence_is_preserved(self):
        o = VisualObservation("a", 1.0, 2.0, "gesture:wave", "wave", 0.35)
        self.assertEqual(o.estimate_confidence, 0.35)
        self.assertEqual(evidence_from_visual(o).strength, 0.35)
        for bad in (1.5, -0.1):
            with self.assertRaises(DNAError):
                VisualObservation("a", 1.0, 2.0, "x", estimate_confidence=bad)

    def test_unrated_evidence_is_listed_but_adds_no_confidence(self):
        tid = self.store.propose_trait(Category.GESTURE, "waves while explaining", Layer.HYPOTHESIS, "t")
        self.store.add_evidence(tid, evidence_from_visual(VisualObservation("a", 1, 2, "wave", "wave")), "t")
        a = self.store.get(tid).assessment()
        self.assertEqual((a.unrated_examples, a.support_examples, a.support_mass, a.confidence), (1, 0, 0.0, 0.0))
        self.assertEqual(a.scope, Scope.UNASSESSED)
        self.assertTrue(any("no supplied confidence" in line for line in a.explanation))
        self.assertEqual(len(self.store.export()["hypotheses"][0]["support"]), 1)       # still visible
        self.store.add_evidence(tid, evidence_from_visual(VisualObservation("b", 1, 2, "wave", "wave", 0.6)), "t")
        rated = self.store.get(tid).assessment()
        self.assertAlmostEqual(rated.support_mass, 0.3)
        self.assertEqual((rated.support_examples, rated.unrated_examples), (1, 1))

    def test_exact_methods_do_not_need_a_supplied_strength(self):
        tid = self.phrase_trait(("a",))
        self.assertEqual(self.store.get(tid).assessment().unrated_examples, 0)
        self.assertAlmostEqual(self.store.get(tid).assessment().support_mass, 1.0)


class AudioMetricContract(Base):
    def test_valid_metrics(self):
        w = AudioWindow("a", 1.0, 2.5, {"pause_duration_s": 1.5})
        self.assertEqual(w.metric("pause_duration_s"), 1.5)
        full = validate_metrics({"level_dbfs": -10.0, "baseline_dbfs": -25.0, "energy_rise_db": 15.0,
                                 "rms_dbfs": -20, "peak_dbfs": -6.0})
        self.assertTrue(all(isinstance(v, float) for v in full.values()))

    def test_every_documented_metric_is_accepted_and_nothing_else(self):
        from creator_dna import METRICS
        self.assertEqual(set(METRICS), {"pause_duration_s", "energy_rise_db", "level_dbfs", "baseline_dbfs",
                                        "rms_dbfs", "peak_dbfs"})
        for name, spec in METRICS.items():
            self.assertTrue(spec.unit and spec.meaning and spec.larger_means and spec.scope in ("duration", "window", "point"))
            validate_metrics({name: spec.minimum})
        with self.assertRaises(DNAError):
            validate_metrics({"pause_before_s": 1.0})

    def test_invalid_values_rejected(self):
        bad = [{}, {"pause_duration_s": -0.1}, {"pause_duration_s": float("nan")}, {"pause_duration_s": float("inf")},
               {"pause_duration_s": True}, {"pause_duration_s": "1.5"}, {"pause_duration_s": None},
               {"level_dbfs": 3.0}, {"rms_dbfs": -200.0}, {"energy_rise_db": -1.0}, {"energy_rise_db": 500.0},
               {"rms_dbfs": -10.0, "peak_dbfs": -30.0},
               {"level_dbfs": -10.0, "baseline_dbfs": -30.0, "energy_rise_db": 3.0}]
        for m in bad:
            with self.subTest(m=m), self.assertRaises(DNAError):
                AudioWindow("a", 0, 1, m)
        with self.assertRaises(DNAError):
            AudioWindow("a", 2, 1, {"rms_dbfs": -10.0})

    def test_missing_optional_metrics_do_not_crash_consumers(self):
        wins = [AudioWindow("a", 5, 6.5, {"pause_duration_s": 1.5}),
                AudioWindow("a", 9, 9.1, {"energy_rise_db": 12.0, "level_dbfs": -10.0}),     # baseline absent
                AudioWindow("a", 0, 0.1, {"rms_dbfs": -20.0})]
        pauses = [evidence_from_window(w, "pause_duration_s") for w in wins]
        self.assertEqual([e is None for e in pauses], [False, True, True])
        self.assertEqual(wins[1].metric("baseline_dbfs"), None)
        self.assertIsNone(evidence_from_window(wins[1], "baseline_dbfs"))
        self.assertEqual(pauses[0].method, Method.MEASURED)
        self.assertIn("pause_duration_s=1.5 s", pauses[0].note)
        with self.assertRaises(DNAError):
            wins[0].metric("not_a_metric")

    def test_measured_evidence_keeps_origin_and_original_timeline(self):
        origin = Origin("m_a", SHA, None, "gap_1", "measurement", "energy-activity@1", {"resolved_threshold_dbfs": -40.0})
        w = AudioWindow("a", 5.0, 6.5, {"pause_duration_s": 1.5}, origin=origin)
        ev = evidence_from_window(w, "pause_duration_s")
        self.assertEqual((ev.start_s, ev.end_s, ev.source_hash), (5.0, 6.5, SHA))
        self.assertEqual(ev.origin.settings["resolved_threshold_dbfs"], -40.0)


class CreatorSelfReportDistinct(Base):
    def test_confirm_keeps_footage_confidence_visible(self):
        tid = self.phrase_trait(("a",))
        footage = self.store.get(tid).assessment().footage_confidence
        self.store.confirm(tid, "yes, that is me")
        a = self.store.get(tid).assessment()
        self.assertEqual(a.footage_confidence, footage)
        self.assertGreaterEqual(a.confidence, 0.95)
        self.assertEqual(a.confidence_basis, "creator_confirmation_over_weak_footage")
        self.assertTrue(a.self_report["confirmed"])
        self.assertEqual(a.support_sources, 1)                          # confirmation adds no footage source

    def test_strong_footage_plus_confirmation_basis(self):
        tid = self.phrase_trait(tuple("abcdefgh"))
        self.store.confirm(tid)
        self.assertEqual(self.store.get(tid).assessment().confidence_basis, "footage_and_creator_confirmation")

    def test_correction_is_self_report_with_link_to_prior_trait(self):
        tid = self.phrase_trait(("a", "b"))
        new = self.store.correct(tid, "Says fiktivno only when joking", "only when I am joking")
        t = self.store.get(new)
        self.assertEqual((t.layer, t.supersedes, t.creator_authored), (Layer.SELF_REPORT, tid, True))
        a = t.assessment()
        self.assertEqual((a.confidence_basis, a.scope), ("creator_self_report", Scope.UNASSESSED))
        old = self.store.get(tid)
        self.assertEqual((old.status, old.superseded_by, len(old.evidence_items)), (Status.CORRECTED, new, 2))
        ex = self.store.export()
        self.assertEqual([x["trait_id"] for x in ex["creator_self_report"]], [new])
        self.assertEqual(ex["observed_facts"], [])                       # the corrected observation left the active profile
        self.assertEqual(ex["creator_self_report"][0]["support"][0]["evidence_origin"], "creator_self_report")
        with self.assertRaises(DNAError):
            self.store.correct(new, "x", "y", layer=Layer.OBSERVED)       # a correction is never footage

    def test_export_never_mixes_footage_and_self_report_evidence(self):
        tid = self.phrase_trait(("a", "b", "c"))
        self.store.add_creator_statement(tid, "I do say that a lot")
        trait = self.store.export()["observed_facts"][0]
        origins = [e["evidence_origin"] for e in trait["support"]]
        self.assertEqual(sorted(origins), ["creator_self_report", "footage", "footage", "footage"])
        a = trait["assessment"]
        self.assertEqual(a["support_examples"], 3)                        # the statement is not a footage example
        self.assertEqual(a["self_report"]["statements_support"], 1)
        self.assertEqual(a["scope"], "stable")

    def test_self_report_cannot_make_a_trait_stable(self):
        tid = self.phrase_trait(("a", "b"))
        for i in range(5):
            self.store.add_creator_statement(tid, f"yes really {i}")
        self.assertEqual(self.store.get(tid).assessment().scope, Scope.EMERGING)

    def test_reclassify_guards_self_report(self):
        tid = self.phrase_trait(("a",))
        new = self.store.correct(tid, "Says it when joking", "joking only")
        with self.assertRaises(DNAError):
            self.store.reclassify(new, Layer.OBSERVED, "x", "promote")


if __name__ == "__main__":
    unittest.main()
