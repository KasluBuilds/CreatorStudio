"""Tests for the learning-ready Creator DNA core (T-DNA-002): attribution verification, consent and quarantine,
analyzers, session independence, review workflow and profile diff. Synthetic data only; no media is read.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_creator_dna_learning.py

These tests use REAL strict stores (injected verifier + project + consent) and never the lenient fixture.
"""
import builtins
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import creator_dna as dna
from _dna_lenient import LenientStore
from _dna_verifiers import RegistryVerifier, TrustingVerifierTestOnly, strict_test_store
from creator_dna import (AudioWindow, Category, DNAError, DNALogError, DNAStore, Evidence, EvidenceAttribution,
                         Layer, Method, Modality, Origin, Polarity, Purpose, ReviewDecisionError, Scope, Status,
                         TraitCandidate, TranscriptSegment, Verification, VerificationUnavailable)
from creator_dna import confidence as conf
from creator_dna.analyzers import (ENERGY_BUCKETS, PAUSE_BUCKETS, EnergyRangeAnalyzer, PauseProfileAnalyzer,
                                   SentenceRhythmAnalyzer, count_sentences)

PROJECT = "proj_a"
ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.dir = self.root / "dna"
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.verifier = RegistryVerifier()
        self.store = self.new_store()
        self.consent_id = self.store.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")

    def clock(self):
        self.t += timedelta(seconds=1)
        return self.t.isoformat()

    def new_store(self, directory=None, **kw):
        kw.setdefault("verifier", self.verifier)
        kw.setdefault("project_id", PROJECT)
        return DNAStore(directory or self.dir, clock=self.clock, **kw)

    # ---- synthetic evidence ---------------------------------------------------------------------
    def stamp(self, key, media=None, rec=None, channel="speech", register=True, start=0.0, end=1000.0):
        media = media or f"m_{key}"
        st = EvidenceAttribution(f"dec_{key}", rec or f"r_{key}", media, channel, start, end, "human_confirmation",
                                 0.95, "synthetic-policy", attribution_evidence_ids=("ae1",))
        if register:
            self.verifier.register(st)
        return st

    def pev(self, i, src="a", pol=Polarity.SUPPORT, session=None, project=None, register=True, key=None):
        """MEASURED audio evidence with a stamp (registered upstream unless register=False)."""
        key = key or f"{src}_{i}"
        st = self.stamp(key, register=register)
        return Evidence(source_id=src, modality=Modality.AUDIO, method=Method.MEASURED, polarity=pol,
                        start_s=float(i), end_s=float(i) + 1, note=f"pause {src} {i}",
                        origin=Origin(media_id=st.media_id, record_id=st.candidate_record_id, session_id=session,
                                      project_id=project), attribution=st)

    def trait(self, store=None, statement="Pauses are typically short", layer=Layer.OBSERVED):
        return (store or self.store).propose_trait(Category.PAUSES, statement, layer, "t")

    def add(self, tid, *evs, store=None):
        s = store or self.store
        return [s.add_evidence(tid, e, "t") for e in evs]

    def events(self, directory=None):
        p = (directory or self.dir) / "events.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]

    def log_bytes(self):
        return (self.dir / "events.jsonl").read_bytes()

    def append_raw(self, type_, data, v=5, actor="x", directory=None):
        d = directory or self.dir
        p = d / "events.jsonl"
        seq = len(p.read_text(encoding="utf-8").splitlines()) + 1
        ev = {"v": v, "seq": seq, "ts": "2026-01-01T00:00:00+00:00", "type": type_, "actor": actor, "data": data}
        with open(p, "ab") as fh:
            fh.write((json.dumps(ev, sort_keys=True) + "\n").encode("utf-8"))

    @staticmethod
    def norm(export):
        e = dict(export)
        e.pop("generated_at", None)
        return json.dumps(e, sort_keys=True)

    def stable_trait(self, n=3):
        tid = self.trait()
        self.add(tid, *[self.pev(1, src=f"rec{k}") for k in range(n)])
        return tid


# ====================================================================== attribution verification
class AttributionVerification(Base):
    def test_store_without_a_verifier_refuses_footage_evidence(self):
        s = DNAStore(self.root / "noverifier", clock=self.clock, project_id=PROJECT)
        s.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")
        tid = self.trait(s)
        with self.assertRaisesRegex(DNAError, "no attribution verifier"):
            s.add_evidence(tid, self.pev(1), "t")
        self.assertEqual(s.get(tid).evidence_items, [])

    def test_there_is_no_flag_that_disables_verification(self):
        import inspect
        params = set(inspect.signature(DNAStore.__init__).parameters)
        self.assertEqual(params, {"self", "directory", "clock", "verifier", "project_id"})

    def test_forged_but_well_formed_stamp_is_refused(self):
        tid = self.trait()
        forged = self.pev(1, register=False)       # internally valid, never decided upstream
        with self.assertRaisesRegex(DNAError, r"attribution not verified \(unknown\)"):
            self.store.add_evidence(tid, forged, "t")
        self.assertEqual(self.store.get(tid).evidence_items, [])

    def test_stale_decision_is_refused(self):
        tid = self.trait()
        ev = self.pev(1)
        self.verifier.decisions[ev.attribution.decision_id]["state"] = "withdrawn"
        with self.assertRaisesRegex(DNAError, r"\(stale\)"):
            self.store.add_evidence(tid, ev, "t")

    def test_decision_for_another_record_is_invalid(self):
        tid = self.trait()
        ev = self.pev(1)
        self.verifier.decisions[ev.attribution.decision_id]["record_id"] = "some_other_record"
        with self.assertRaisesRegex(DNAError, r"\(invalid\)"):
            self.store.add_evidence(tid, ev, "t")

    def test_decision_covering_a_narrower_range_is_invalid(self):
        tid = self.trait()
        ev = self.pev(5)
        self.verifier.decisions[ev.attribution.decision_id]["end_s"] = 2.0
        with self.assertRaisesRegex(DNAError, r"\(invalid\)"):
            self.store.add_evidence(tid, ev, "t")

    def test_unreachable_verifier_refuses(self):
        tid = self.trait()
        ev = self.pev(1)
        self.verifier.offline = True
        with self.assertRaisesRegex(DNAError, r"\(unavailable\).*offline"):
            self.store.add_evidence(tid, ev, "t")

    def test_crashing_verifier_is_unavailable_and_leaks_nothing(self):
        class Boom:
            def verify(self, request):
                raise RuntimeError("secret internal path C:/x/y")
        s = self.new_store(self.root / "boom", verifier=Boom())
        s.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")
        tid = self.trait(s)
        with self.assertRaises(DNAError) as cm:
            s.add_evidence(tid, self.pev(1), "t")
        self.assertIn("unavailable", str(cm.exception))
        self.assertNotIn("secret", str(cm.exception))

    def test_verifier_answering_with_anything_but_a_verification_is_refused(self):
        for answer in (True, "verified", {"outcome": "verified"}, None, 1):
            class Liar:
                def verify(self, request, answer=answer):
                    return answer
            s = self.new_store(self.root / f"liar{id(answer)}", verifier=Liar())
            s.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")
            tid = self.trait(s)
            with self.assertRaises(DNAError, msg=repr(answer)):
                s.add_evidence(tid, self.pev(1), "t")

    def test_verified_evidence_is_admitted_and_counts(self):
        tid = self.trait()
        self.assertIsNotNone(self.add(tid, self.pev(1))[0])
        self.assertEqual(self.store.get(tid).assessment().support_examples, 1)

    def test_verifier_receives_the_evidence_identity(self):
        seen = []

        class Spy:
            def verify(self, request):
                seen.append(request)
                return Verification.verified()
        s = self.new_store(self.root / "spy", verifier=Spy())
        s.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")
        ev = self.pev(3, src="clip")
        s.add_evidence(self.trait(s), ev, "t")
        r = seen[0]
        self.assertEqual((r.evidence_id, r.source_id, r.media_id, r.record_id, r.modality, r.method, r.start_s, r.end_s),
                         (ev.evidence_id, "clip", ev.origin.media_id, ev.origin.record_id, "audio", "measured", 3.0, 4.0))
        self.assertIs(r.stamp, ev.attribution)

    def test_ingest_reports_forged_candidates_and_creates_nothing(self):
        cand = TraitCandidate(Category.PAUSES, "Forged trait", Layer.OBSERVED, [self.pev(1, register=False)])
        rep = self.store.ingest([cand], "t")
        self.assertEqual((rep.traits_created, rep.evidence_added), ([], 0))
        self.assertIn("attribution not verified", rep.refused[0]["reason"])
        self.assertEqual(self.store.traits, {})

    def test_ingest_admits_verified_candidates(self):
        cand = TraitCandidate(Category.PAUSES, "Real trait", Layer.OBSERVED, [self.pev(1), self.pev(2)])
        rep = self.store.ingest([cand], "t")
        self.assertEqual((len(rep.traits_created), rep.evidence_added, rep.refused), (1, 2, []))

    def test_missing_stamp_is_still_refused_before_any_verifier_call(self):
        tid = self.trait()
        bare = Evidence(source_id="a", modality=Modality.AUDIO, method=Method.MEASURED, start_s=0, end_s=1, note="x")
        with self.assertRaisesRegex(DNAError, "unattributed footage evidence refused"):
            self.store.add_evidence(tid, bare, "t")
        self.assertEqual(self.verifier.calls, [])

    def test_creator_statements_need_neither_verifier_nor_consent(self):
        s = DNAStore(self.root / "bare")          # no verifier, no project, no consent
        tid = s.propose_trait(Category.HUMOR, "I like dry understatement", Layer.SELF_REPORT, "creator")
        self.assertIsNotNone(s.add_evidence(tid, Evidence("creator-feedback", Modality.FEEDBACK, Method.CREATOR_STATEMENT,
                                                          note="that is how I talk"), "creator"))

    def test_lenient_fixture_is_test_only_and_not_part_of_the_package(self):
        self.assertIn("TEST FIXTURE ONLY", sys.modules["_dna_lenient"].__doc__)
        self.assertIn("TEST ONLY", TrustingVerifierTestOnly.__doc__)
        for f in (ROOT / "creator_dna").glob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("LenientStore", text, f.name)
            self.assertNotIn("TrustingVerifierTestOnly", text, f.name)

    def test_verification_helpers_reject_a_non_outcome(self):
        with self.assertRaises(TypeError):
            Verification("verified")


# ============================================================================== attribution audit
class AttributionAudit(Base):
    def setUp(self):
        super().setUp()
        self.tid = self.trait()
        self.evs = [self.pev(k, src=f"r{k}") for k in range(3)]
        self.add(self.tid, *self.evs)

    def decision(self, k):
        return self.evs[k].attribution.decision_id

    def test_clean_audit_changes_nothing(self):
        before = self.log_bytes()
        rep = self.store.audit_attributions()
        self.assertEqual((rep.checked, rep.verified, rep.revoked), (3, 3, []))
        self.assertEqual(self.log_bytes(), before)

    def test_stale_decision_is_revoked_and_stops_counting(self):
        self.verifier.decisions[self.decision(0)]["state"] = "corrected"
        rep = self.store.audit_attributions()
        self.assertEqual([(t, e, o) for t, e, o in rep.revoked], [(self.tid, self.evs[0].evidence_id, "stale")])
        a = self.store.get(self.tid).assessment()
        self.assertEqual((a.support_examples, a.support_sources), (2, 2))
        self.assertTrue(self.store.get(self.tid).evidence_items[0].attribution_revoked)

    def test_invalid_and_unknown_are_revoked_too(self):
        self.verifier.decisions[self.decision(0)]["record_id"] = "elsewhere"      # invalid
        del self.verifier.decisions[self.decision(1)]                              # unknown
        rep = self.store.audit_attributions()
        self.assertEqual(sorted(o for _t, _e, o in rep.revoked), ["invalid", "unknown"])
        self.assertEqual(self.store.get(self.tid).assessment().support_examples, 1)

    def test_unreachable_verifier_revokes_nothing_and_says_so(self):
        self.verifier.offline = True
        before = self.log_bytes()
        rep = self.store.audit_attributions()
        self.assertEqual((rep.revoked, len(rep.unavailable)), ([], 3))
        self.assertEqual(self.log_bytes(), before)

    def test_audit_is_idempotent(self):
        self.verifier.decisions[self.decision(0)]["state"] = "withdrawn"
        self.assertEqual(len(self.store.audit_attributions().revoked), 1)
        size = len(self.log_bytes())
        again = self.store.audit_attributions()
        self.assertEqual((again.revoked, again.checked), ([], 2))
        self.assertEqual(len(self.log_bytes()), size)

    def test_revocation_replays_identically(self):
        self.verifier.decisions[self.decision(1)]["state"] = "withdrawn"
        self.store.audit_attributions()
        reloaded = self.new_store()
        self.assertEqual(self.norm(reloaded.export()), self.norm(self.store.export()))
        self.assertEqual([i.revoked_decision_id for i in reloaded.get(self.tid).evidence_items],
                         [None, self.decision(1), None])

    def test_revoked_evidence_cannot_be_restored(self):
        self.verifier.decisions[self.decision(0)]["state"] = "withdrawn"
        self.store.audit_attributions()
        with self.assertRaises(DNAError):
            self.store.restore_evidence(self.tid, self.evs[0].evidence_id, "undo")

    def test_audit_revokes_even_creator_rejected_evidence(self):
        self.store.reject_evidence(self.tid, self.evs[0].evidence_id, "not me")
        self.verifier.decisions[self.decision(0)]["state"] = "withdrawn"
        self.assertEqual(len(self.store.audit_attributions().revoked), 1)
        it = self.store.get(self.tid).evidence_items[0]
        self.assertTrue(it.creator_rejected and it.attribution_revoked)

    def test_revocation_lowers_scope_from_stable(self):
        self.assertEqual(self.store.get(self.tid).assessment().scope, Scope.STABLE)
        self.verifier.decisions[self.decision(2)]["state"] = "withdrawn"
        self.store.audit_attributions()
        self.assertEqual(self.store.get(self.tid).assessment().scope, Scope.EMERGING)

    def test_audit_needs_a_verifier(self):
        s = DNAStore(self.root / "nov", clock=self.clock)
        with self.assertRaises(DNAError):
            s.audit_attributions()

    def test_audit_can_use_an_explicit_verifier(self):
        s = DNAStore(self.dir, clock=self.clock, project_id=PROJECT)       # reload without a verifier
        class Nothing:
            def verify(self, request):
                return Verification.unknown("gone")
        self.assertEqual(len(s.audit_attributions(Nothing()).revoked), 3)

    def test_audit_lists_legacy_unattributed_footage_without_touching_it(self):
        d = self.root / "legacy"
        lenient = LenientStore(d, clock=self.clock)
        lt = lenient.propose_trait(Category.PAUSES, "legacy pause", Layer.OBSERVED, "t")
        lenient.add_evidence(lt, Evidence("a", Modality.AUDIO, Method.MEASURED, start_s=1, end_s=2, note="old"), "t")
        p = d / "events.jsonl"
        lines = []
        for line in p.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e["type"] == "evidence_added":
                e["data"]["evidence"]["attribution"] = None
            lines.append(json.dumps(e, sort_keys=True))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        s = DNAStore(d, clock=self.clock, verifier=self.verifier, project_id=PROJECT)
        before = p.read_bytes()
        rep = s.audit_attributions()
        self.assertEqual((rep.checked, len(rep.unattributed)), (0, 1))
        self.assertEqual(p.read_bytes(), before)


# ======================================================================================== consent
class Consent(Base):
    def fresh(self, name="s2", **kw):
        return self.new_store(self.root / name, **kw)

    def test_no_consent_no_learning(self):
        s = self.fresh()
        tid = self.trait(s)
        with self.assertRaisesRegex(DNAError, r"consent refused \(no_consent\)"):
            s.add_evidence(tid, self.pev(1), "t")

    def test_consent_for_style_learning_admits_evidence(self):
        self.assertTrue(self.store.consent_decision(Purpose.STYLE_LEARNING, PROJECT).allowed)
        tid = self.trait()
        self.assertIsNotNone(self.add(tid, self.pev(1))[0])

    def test_a_consent_for_other_purposes_does_not_cover_learning(self):
        s = self.fresh()
        s.record_consent("creator", PROJECT, [Purpose.EDITING_GUIDANCE, Purpose.PROFILE_REVIEW], "human:lukas")
        tid = self.trait(s)
        with self.assertRaisesRegex(DNAError, r"purpose_not_covered"):
            s.add_evidence(tid, self.pev(1), "t")
        self.assertTrue(s.consent_decision(Purpose.EDITING_GUIDANCE, PROJECT).allowed)

    def test_purpose_vocabulary_is_closed(self):
        for bad in (["surveillance"], [], "style_learning", ["style_learning", "sell_data"], None):
            with self.assertRaises(DNAError, msg=repr(bad)):
                self.store.record_consent("creator", PROJECT, bad, "human:lukas")
        with self.assertRaises(DNAError):
            self.store.consent_decision("anything", PROJECT)

    def test_authorizing_actor_must_be_a_named_human(self):
        for bad in ("lukas", "claude", "human:", "", None):
            with self.assertRaises(DNAError, msg=repr(bad)):
                self.store.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], bad)

    def test_identity_and_project_are_required(self):
        for creator, project in (("", PROJECT), ("creator", ""), (None, PROJECT), ("creator", None)):
            with self.assertRaises(DNAError):
                self.store.record_consent(creator, project, [Purpose.STYLE_LEARNING], "human:lukas")

    def test_expired_consent_refuses_learning(self):
        s = self.fresh()
        s.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas",
                         expires_at=(self.t + timedelta(hours=1)).isoformat())
        tid = self.trait(s)
        self.assertIsNotNone(s.add_evidence(tid, self.pev(1), "t"))
        self.t += timedelta(hours=2)
        with self.assertRaisesRegex(DNAError, r"\(expired\)"):
            s.add_evidence(tid, self.pev(2), "t")
        self.assertEqual(s.current_consents()[0]["status"], "expired")

    def test_expiry_must_follow_the_grant_and_carry_a_zone(self):
        for bad in ((self.t - timedelta(days=1)).isoformat(), "2099-01-01T00:00:00", "tomorrow"):
            with self.assertRaises(DNAError, msg=bad):
                self.store.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas", expires_at=bad)

    def test_withdrawal_stops_learning_immediately(self):
        tid = self.trait()
        self.add(tid, self.pev(1))
        self.store.withdraw_consent("changed my mind", "human:lukas")
        with self.assertRaisesRegex(DNAError, r"\(withdrawn\)"):
            self.store.add_evidence(tid, self.pev(2), "t")

    def test_withdrawal_quarantines_existing_footage_evidence(self):
        tid = self.trait()
        self.add(tid, self.pev(1, src="a"), self.pev(2, src="b"), self.pev(3, src="c"))
        self.assertEqual(self.store.get(tid).assessment().scope, Scope.STABLE)
        res = self.store.withdraw_consent("changed my mind", "human:lukas")
        self.assertEqual((len(res["withdrawn"]), len(res["quarantined"])), (1, 3))
        tr = self.store.get(tid)
        self.assertTrue(all(i.quarantined and i.excluded for i in tr.evidence_items))
        a = tr.assessment()
        self.assertEqual((a.confidence, a.support_examples, a.scope), (0.0, 0, Scope.UNASSESSED))
        self.assertEqual(self.store.active_traits(min_confidence=0.01), [])

    def test_quarantined_evidence_is_kept_and_flagged_in_the_export(self):
        tid = self.trait()
        self.add(tid, self.pev(1))
        self.store.withdraw_consent("stop", "human:lukas")
        item = self.store.export()["observed_facts"][0]["support"][0]
        self.assertTrue(item["quarantined"])
        self.assertIn("consent withdrawn", item["quarantine_reason"])
        self.assertFalse(item["rejected_by_creator"])
        self.assertEqual(len(self.store.export()["quarantined_evidence"]), 1)

    def test_creator_statements_are_not_quarantined(self):
        tid = self.trait()
        self.add(tid, self.pev(1))
        sid = self.store.add_creator_statement(tid, "yes that is how I talk")
        self.store.withdraw_consent("stop", "human:lukas")
        item = [i for i in self.store.get(tid).evidence_items if i.evidence_id == sid][0]
        self.assertFalse(item.quarantined)

    def test_repeated_withdrawal_and_quarantine_are_idempotent(self):
        tid = self.trait()
        self.add(tid, self.pev(1))
        self.store.withdraw_consent("stop", "human:lukas")
        size = len(self.log_bytes())
        again = self.store.withdraw_consent("stop again", "human:lukas")
        self.assertEqual(again, {"withdrawn": [], "quarantined": []})
        self.assertEqual(self.store.quarantine_all("again", "human:lukas"), [])
        self.assertEqual(len(self.log_bytes()), size)

    def test_withdrawing_one_of_two_consents_keeps_learning_and_evidence(self):
        second = self.store.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING, Purpose.PROFILE_REVIEW], "human:ana")
        self.assertNotEqual(second, self.consent_id)
        tid = self.trait()
        self.add(tid, self.pev(1))
        res = self.store.withdraw_consent("one person withdrew", "human:lukas", consent_id=self.consent_id)
        self.assertEqual(res["quarantined"], [])
        self.assertIsNotNone(self.add(tid, self.pev(2))[0])
        res2 = self.store.withdraw_consent("both gone", "human:ana")
        self.assertEqual((len(res2["withdrawn"]), len(res2["quarantined"])), (1, 2))

    def test_withdrawal_without_quarantine_only_stops_learning(self):
        tid = self.trait()
        self.add(tid, self.pev(1))
        self.store.withdraw_consent("pause learning", "human:lukas", quarantine=False)
        self.assertFalse(self.store.get(tid).evidence_items[0].quarantined)

    def test_withdrawal_needs_a_reason_and_a_known_consent(self):
        with self.assertRaises(DNAError):
            self.store.withdraw_consent("  ", "human:lukas")
        with self.assertRaises(DNAError):
            self.store.withdraw_consent("x", "human:lukas", consent_id="cs_nope")

    def test_projects_are_isolated(self):
        with self.assertRaises(DNAError):                                # a store belongs to one project
            self.store.record_consent("creator", "proj_b", [Purpose.STYLE_LEARNING], "human:lukas")
        tid = self.trait()
        with self.assertRaisesRegex(DNAError, "belongs to project"):
            self.store.add_evidence(tid, self.pev(1, project="proj_b"), "t")
        self.assertEqual(self.store.consent_decision(Purpose.STYLE_LEARNING, "proj_b").code, "no_consent")

    def test_store_without_any_project_identity_refuses_footage(self):
        s = self.new_store(self.root / "noproj", project_id=None)
        s.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")
        tid = self.trait(s)
        with self.assertRaisesRegex(DNAError, r"\(no_project\)"):
            s.add_evidence(tid, self.pev(1), "t")
        self.assertIsNotNone(s.add_evidence(tid, self.pev(2, project=PROJECT), "t"))    # origin names the project

    def test_identical_current_consent_is_not_recorded_twice(self):
        n = len(self.events())
        self.assertEqual(self.store.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas"), self.consent_id)
        self.assertEqual(len(self.events()), n)

    def test_regranting_after_withdrawal_creates_a_new_consent(self):
        self.store.withdraw_consent("stop", "human:lukas")
        new = self.store.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")
        self.assertNotEqual(new, self.consent_id)
        self.assertTrue(self.store.consent_decision(Purpose.STYLE_LEARNING, PROJECT).allowed)

    def test_release_needs_a_current_consent(self):
        tid = self.trait()
        self.add(tid, self.pev(1))
        self.store.withdraw_consent("stop", "human:lukas")
        self.assertEqual(self.store.release_quarantine("not yet", "human:lukas"), [])
        self.assertTrue(self.store.get(tid).evidence_items[0].quarantined)
        self.store.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")
        self.assertEqual(len(self.store.release_quarantine("consent renewed", "human:lukas")), 1)
        self.assertEqual(self.store.get(tid).assessment().support_examples, 1)

    def test_release_never_undoes_rejection_or_revocation(self):
        tid = self.trait()
        e1, e2, e3 = self.pev(1, src="a"), self.pev(2, src="b"), self.pev(3, src="c")
        self.add(tid, e1, e2, e3)
        self.store.reject_evidence(tid, e1.evidence_id, "not a pause")
        self.verifier.decisions[e2.attribution.decision_id]["state"] = "withdrawn"
        self.store.audit_attributions()
        self.store.withdraw_consent("stop", "human:lukas")
        self.store.record_consent("creator", PROJECT, [Purpose.STYLE_LEARNING], "human:lukas")
        self.store.release_quarantine("renewed", "human:lukas")
        a = self.store.get(tid).assessment()
        self.assertEqual(a.support_examples, 1)             # only e3 counts again
        items = {i.evidence_id: i for i in self.store.get(tid).evidence_items}
        self.assertTrue(items[e1.evidence_id].creator_rejected and items[e2.evidence_id].attribution_revoked)

    def test_quarantine_all_can_be_limited_to_a_project_and_is_deterministic(self):
        tid = self.trait()
        self.add(tid, self.pev(1, src="a"), self.pev(2, src="b"))
        self.assertEqual(self.store.quarantine_all("other project", "human:lukas", project_id="proj_b"), [])
        got = self.store.quarantine_all("review", "human:lukas", project_id=PROJECT)
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(got), 2)

    def test_quarantine_and_consent_need_reasons(self):
        for call in (lambda: self.store.quarantine_all("", "x"), lambda: self.store.release_quarantine(" ", "x")):
            with self.assertRaises(DNAError):
                call()

    def test_consent_and_quarantine_replay_identically(self):
        tid = self.trait()
        self.add(tid, self.pev(1), self.pev(2, src="b"))
        self.store.record_consent("creator", PROJECT, [Purpose.EDITING_GUIDANCE], "human:ana",
                                  provenance={"channel": "in person", "form": "v1"})
        self.store.withdraw_consent("stop", "human:lukas", consent_id=self.consent_id)
        reloaded = self.new_store()
        self.assertEqual(self.norm(reloaded.export()), self.norm(self.store.export()))
        self.assertEqual(reloaded.current_consents(), self.store.current_consents())
        self.assertEqual([i.quarantined for i in reloaded.get(tid).evidence_items], [True, True])

    def test_export_lists_consent_history(self):
        self.store.withdraw_consent("stop", "human:lukas")
        c = self.store.export()["consent"][0]
        self.assertEqual((c["status"], c["withdrawn_reason"], c["purposes"], c["authorized_by"]),
                         ("withdrawn", "stop", ["style_learning"], "human:lukas"))

    def test_provenance_is_limited_to_short_scalars(self):
        for bad in ({"k": ["list"]}, {"k": {"nested": 1}}, {"k": "x" * 301}, {1: "x"}, "text"):
            with self.assertRaises(DNAError, msg=repr(bad)):
                self.store.record_consent("creator", PROJECT, [Purpose.PROFILE_REVIEW], "human:z", provenance=bad)


# ================================================================ schema v5, malformed logs, compatibility
class SchemaAndLogs(Base):
    def test_schema_version_is_5_and_only_new_events_are_v5(self):
        tid = self.trait()
        self.add(tid, self.pev(1))
        self.store.reject_evidence(tid, self.pev(1).evidence_id, "no")
        by_type = {e["type"]: e["v"] for e in self.events()}
        self.assertEqual(dna.SCHEMA_VERSION, 5)
        self.assertEqual((by_type["consent_recorded"], by_type["trait_proposed"], by_type["evidence_added"],
                          by_type["evidence_review"]), (5, 4, 4, 4))

    def test_evidence_with_session_or_project_ids_is_written_as_v5(self):
        tid = self.trait()
        self.add(tid, self.pev(1, session="s1"), self.pev(2))
        v = [e["v"] for e in self.events() if e["type"] == "evidence_added"]
        self.assertEqual(v, [5, 4])

    def test_v4_log_replays_into_a_v5_store_with_identical_assessments(self):
        d = self.root / "v4"
        lenient = LenientStore(d, clock=self.clock)
        tid = lenient.propose_trait(Category.PAUSES, "legacy trait", Layer.OBSERVED, "t")
        for k in range(3):
            lenient.add_evidence(tid, Evidence(f"s{k}", Modality.AUDIO, Method.MEASURED, start_s=k, end_s=k + 1, note=f"n{k}"), "t")
        self.assertTrue(all(e["v"] == 4 for e in self.events(d)))
        s = DNAStore(d, clock=self.clock)
        self.assertEqual(s.get(tid).assessment().to_dict(), lenient.get(tid).assessment().to_dict())
        self.assertEqual(s.consents, {})

    def test_v3_and_earlier_events_still_replay_with_original_semantics(self):
        d = self.root / "v3"
        lenient = LenientStore(d, clock=self.clock)
        tid = lenient.propose_trait(Category.PAUSES, "old trait", Layer.OBSERVED, "t")
        ev = Evidence("s", Modality.AUDIO, Method.MEASURED, start_s=1, end_s=2, note="n")
        lenient.add_evidence(tid, ev, "t")
        lenient.reject_evidence(tid, lenient.get(tid).evidence_items[0].evidence_id, "attribution revoked (dec_x): gone")
        p = d / "events.jsonl"
        p.write_text("\n".join(json.dumps({**json.loads(x), "v": 3}, sort_keys=True)
                               for x in p.read_text(encoding="utf-8").splitlines()) + "\n", encoding="utf-8")
        s = DNAStore(d, clock=self.clock)
        it = s.get(tid).evidence_items[0]
        self.assertTrue(it.creator_rejected and it.legacy_possible_revocation)
        self.assertEqual(len(s.legacy_warnings()), 1)

    def test_newer_schema_events_are_refused(self):
        self.append_raw("consent_recorded", {}, v=6)
        with self.assertRaisesRegex(DNALogError, "newer schema"):
            self.new_store()

    def test_unknown_event_types_are_refused_not_ignored(self):
        self.append_raw("consent_recorded_v6", {"x": 1})
        with self.assertRaises(DNALogError):
            self.new_store()

    def test_malformed_consent_events_are_refused(self):
        good = {"consent_id": "cs_x", "project_id": PROJECT, "creator_id": "creator", "purposes": ["style_learning"],
                "authorized_by": "human:lukas"}
        for name, change in (("bad purpose", {"purposes": ["spy"]}), ("no creator", {"creator_id": ""}),
                             ("not human", {"authorized_by": "claude"}), ("wrong project", {"project_id": "proj_z"}),
                             ("missing id", {"consent_id": None}), ("expiry before grant", {"expires_at": "2000-01-01T00:00:00+00:00"})):
            self.setUp()
            self.append_raw("consent_recorded", {**good, **change})
            with self.assertRaises(DNALogError, msg=name):
                self.new_store()

    def test_duplicate_consent_id_is_refused(self):
        self.append_raw("consent_recorded", {"consent_id": self.consent_id, "project_id": PROJECT, "creator_id": "creator",
                                             "purposes": ["style_learning"], "authorized_by": "human:lukas"})
        with self.assertRaises(DNALogError):
            self.new_store()

    def test_malformed_withdrawals_are_refused(self):
        for data in ({"consent_id": "cs_none", "reason": "x"}, {"consent_id": self.consent_id, "reason": " "}):
            self.setUp()
            self.append_raw("consent_withdrawn", data)
            with self.assertRaises(DNALogError):
                self.new_store()
        self.setUp()
        self.append_raw("consent_withdrawn", {"consent_id": self.consent_id, "reason": "a"})
        self.append_raw("consent_withdrawn", {"consent_id": self.consent_id, "reason": "b"})
        with self.assertRaises(DNALogError):
            self.new_store()

    def test_malformed_quarantine_events_are_refused(self):
        tid = self.trait()
        e = self.pev(1)
        self.add(tid, e)
        bad = [{"reason": "", "items": [[tid, e.evidence_id]]}, {"reason": "r", "items": []},
               {"reason": "r", "items": [[tid]]}, {"reason": "r", "items": [[tid, "ev_missing"]]},
               {"reason": "r", "items": [[tid, e.evidence_id], [tid, e.evidence_id]]}, {"reason": "r"}]
        for data in bad:
            import shutil
            keep = self.log_bytes()
            self.append_raw("evidence_quarantined", data)
            with self.assertRaises(DNALogError, msg=str(data)):
                self.new_store()
            (self.dir / "events.jsonl").write_bytes(keep)
            shutil.rmtree(self.root / "x", ignore_errors=True)

    def test_double_quarantine_and_release_of_unquarantined_are_refused(self):
        tid = self.trait()
        e = self.pev(1)
        self.add(tid, e)
        keep = self.log_bytes()
        self.append_raw("quarantine_released", {"reason": "r", "items": [[tid, e.evidence_id]]})
        with self.assertRaises(DNALogError):
            self.new_store()
        (self.dir / "events.jsonl").write_bytes(keep)
        for _ in range(2):
            self.append_raw("evidence_quarantined", {"reason": "r", "items": [[tid, e.evidence_id]]})
        with self.assertRaises(DNALogError):
            self.new_store()

    def test_creator_statements_cannot_be_quarantined_by_a_forged_event(self):
        tid = self.trait()
        sid = self.store.add_creator_statement(tid, "my own words")
        self.append_raw("evidence_quarantined", {"reason": "r", "items": [[tid, sid]]})
        with self.assertRaises(DNALogError):
            self.new_store()

    def test_torn_or_reordered_logs_still_refuse(self):
        p = self.dir / "events.jsonl"
        p.write_bytes(p.read_bytes()[:-5])
        with self.assertRaises(DNALogError):
            self.new_store()

    def test_project_mismatch_between_store_and_logged_consent_is_refused(self):
        with self.assertRaises(DNALogError):
            DNAStore(self.dir, clock=self.clock, project_id="proj_other")


# ======================================================================================== analyzers
def _mk(cls_origin_media="m1"):
    return Origin(media_id=cls_origin_media)


class AnalyzerBase(Base):
    def seg(self, i, text, src="a", reviewed=True, origin=True, attrib=True, start=None):
        st = self.stamp(f"{src}_seg{i}") if attrib else None
        kw = dict(verification="reviewed", reviewer="human:ana") if reviewed else {}
        return TranscriptSegment(src, float(i) if start is None else start, float(i) + 1, text,
                                 origin=Origin(media_id=st.media_id, record_id=st.candidate_record_id) if (origin and st) else
                                 (Origin(media_id="m_x") if origin else None), attribution=st, **kw)

    def win(self, i, metrics, src="a", origin=True, attrib=True):
        st = self.stamp(f"{src}_win{i}") if attrib else None
        return AudioWindow(src, float(i), float(i) + 1, metrics,
                           origin=Origin(media_id=st.media_id, record_id=st.candidate_record_id) if (origin and st) else
                           (Origin(media_id="m_x") if origin else None), attribution=st)


class SentenceRhythm(AnalyzerBase):
    SHORT = "Ajde de. To je bilo fejk. Ne mogu vjerovati."

    def test_counts_words_with_croatian_letters(self):
        self.assertEqual(count_sentences("Čuj, đavle! Što je šaš? Ništa..."), [2, 3, 1])
        self.assertEqual(count_sentences("don't stop-me now"), [3])
        self.assertEqual(count_sentences("   "), [])

    def test_short_sentences_become_one_observed_measured_trait(self):
        a = SentenceRhythmAnalyzer()
        out = a.analyze_transcript([self.seg(i, self.SHORT) for i in range(3)])
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual((c.category, c.layer, c.statement), (Category.SPEECH_RHYTHM, Layer.OBSERVED,
                                                              "Sentences are typically short (8 words or fewer)"))
        self.assertTrue(all(e.method is Method.MEASURED and e.quote is None and e.polarity is Polarity.SUPPORT
                            for e in c.evidence))

    def test_bucket_boundaries(self):
        def bucket(words):
            segs = [self.seg(i, " ".join(["w"] * words) + ".", src=f"s{i}") for i in range(3)]
            return SentenceRhythmAnalyzer().analyze_transcript(segs)[0].statement.split()[3]
        self.assertEqual([bucket(n) for n in (8, 9, 16, 17)], ["short", "medium", "medium", "long"])

    def test_only_reviewed_segments_are_used(self):
        a = SentenceRhythmAnalyzer()
        segs = [self.seg(i, self.SHORT) for i in range(3)] + [self.seg(9, "x " * 40 + ".", reviewed=False)]
        out = a.analyze_transcript(segs)
        self.assertEqual(len(out[0].evidence), 3)
        self.assertEqual([s.reason for s in a.skipped], ["transcript segment is not human-reviewed"])
        self.assertEqual(a.analyze_transcript([self.seg(i, self.SHORT, reviewed=False) for i in range(5)]), [])

    def test_inputs_without_provenance_or_attribution_are_skipped_with_a_reason(self):
        a = SentenceRhythmAnalyzer()
        segs = [self.seg(1, self.SHORT), self.seg(2, self.SHORT, origin=False, attrib=False),
                self.seg(3, self.SHORT, attrib=False), self.seg(4, "..."), self.seg(5, self.SHORT)]
        out = a.analyze_transcript(segs)
        self.assertEqual(len(out[0].evidence), 2)
        self.assertEqual(sorted(s.reason for s in a.skipped), sorted([
            "segment has no origin (provenance is required)", "segment has no eligible-creator attribution",
            "segment holds no words"]))

    def test_too_few_sentences_give_no_trait(self):
        self.assertEqual(SentenceRhythmAnalyzer().analyze_transcript([self.seg(1, "Jedna rečenica.")]), [])
        self.assertEqual(SentenceRhythmAnalyzer(min_samples=1).analyze_transcript([self.seg(1, "Jedna.")])[0].layer, Layer.OBSERVED)

    def test_min_samples_is_validated(self):
        for bad in (0, -1, 1.5, True, "3"):
            with self.assertRaises(DNAError, msg=repr(bad)):
                SentenceRhythmAnalyzer(min_samples=bad)

    def test_different_bucket_segments_are_kept_as_counter_evidence(self):
        long_text = " ".join(["riječ"] * 25) + "."
        segs = [self.seg(i, self.SHORT, src=f"s{i}") for i in range(3)] + [self.seg(9, long_text, src="s9")]
        c = SentenceRhythmAnalyzer().analyze_transcript(segs)[0]
        pol = sorted(e.polarity.value for e in c.evidence)
        self.assertEqual(pol, ["counter", "support", "support", "support"])

    def test_output_is_deterministic_and_order_independent(self):
        segs = [self.seg(i, self.SHORT if i % 2 else "Kratko. Da.", src=f"s{i % 3}") for i in range(6)]
        a, b = SentenceRhythmAnalyzer(), SentenceRhythmAnalyzer()
        one = a.analyze_transcript(segs)
        two = b.analyze_transcript(list(reversed(segs)))
        self.assertEqual([e.to_dict() for e in one[0].evidence], [e.to_dict() for e in two[0].evidence])
        self.assertEqual(one[0].statement, two[0].statement)

    def test_trait_is_admitted_by_a_strict_store_and_traced_to_its_source(self):
        segs = [self.seg(i, self.SHORT, src=f"s{i}") for i in range(3)]
        rep = self.store.ingest(SentenceRhythmAnalyzer().analyze_transcript(segs), "sentence-rhythm@1")
        self.assertEqual((len(rep.traits_created), rep.evidence_added, rep.refused), (1, 3, []))
        tr = next(iter(self.store.traits.values()))
        self.assertEqual(tr.assessment().scope, Scope.STABLE)
        self.assertTrue(all(i.evidence.origin.media_id for i in tr.evidence_items))

    def test_forged_attribution_inside_an_analyzer_input_is_still_refused(self):
        segs = [self.seg(i, self.SHORT, src=f"s{i}") for i in range(3)]
        self.verifier.decisions.pop("dec_s1_seg1")
        rep = self.store.ingest(SentenceRhythmAnalyzer().analyze_transcript(segs), "sentence-rhythm@1")
        self.assertEqual(rep.evidence_added, 0)
        self.assertIn("attribution not verified", rep.refused[0]["reason"])


class WindowAnalyzers(AnalyzerBase):
    def test_pause_buckets_and_boundaries(self):
        def stmt(v):
            ws = [self.win(i, {"pause_duration_s": v}, src=f"w{i}") for i in range(3)]
            return PauseProfileAnalyzer().analyze_audio(ws)[0].statement.split()[3]
        self.assertEqual([stmt(v) for v in (0.0, 0.79, 0.8, 1.49, 1.5, 9.0)],
                         ["short", "short", "medium", "medium", "long", "long"])

    def test_pause_analyzer_uses_only_supplied_measurements(self):
        a = PauseProfileAnalyzer()
        ws = [self.win(1, {"pause_duration_s": 0.4}), self.win(2, {"level_dbfs": -20.0}), self.win(3, {"pause_duration_s": 0.5}),
              self.win(4, {"pause_duration_s": 0.3}), self.win(5, {"pause_duration_s": 0.6}, attrib=False)]
        out = a.analyze_audio(ws)
        self.assertEqual(len(out[0].evidence), 3)
        self.assertEqual(sorted(s.reason for s in a.skipped), ["window has no eligible-creator attribution",
                                                                "window has no pause_duration_s measurement"])
        self.assertTrue(all(e.method is Method.MEASURED and e.modality is Modality.AUDIO for e in out[0].evidence))

    def test_pause_counter_evidence_and_contextual_scope(self):
        vals = [0.3, 0.4, 0.5, 0.35, 2.4, 0.45]
        ws = [self.win(i, {"pause_duration_s": v}, src=f"w{i}") for i, v in enumerate(vals)]
        c = PauseProfileAnalyzer().analyze_audio(ws)[0]
        self.assertEqual(sorted(e.polarity.value for e in c.evidence).count("counter"), 1)
        self.store.ingest([c], "pause-profile@1")
        a = next(iter(self.store.traits.values())).assessment()
        self.assertEqual((a.counter_examples, a.support_examples), (1, 5))
        self.assertLess(a.consistency, 1.0)

    def test_too_few_windows_give_nothing(self):
        self.assertEqual(PauseProfileAnalyzer().analyze_audio([self.win(1, {"pause_duration_s": 1.0})]), [])

    def test_energy_rise_buckets_and_level_is_quoted_not_invented(self):
        def trait(v):
            ws = [self.win(i, {"energy_rise_db": v, "level_dbfs": -20.0, "baseline_dbfs": -20.0 - v}, src=f"e{i}")
                  for i in range(3)]
            return EnergyRangeAnalyzer().analyze_audio(ws)[0]
        self.assertEqual([trait(v).statement.split()[4] for v in (5.9, 6.0, 11.9, 12.0)],
                         ["small", "moderate", "moderate", "large"])
        self.assertIn("level -20 dBFS", trait(6.0).evidence[0].note)
        ws = [self.win(i, {"energy_rise_db": 7.0}, src=f"e{i}") for i in range(3)]
        c = EnergyRangeAnalyzer().analyze_audio(ws)[0]
        self.assertNotIn("dBFS", c.evidence[0].note.replace("energy_rise_db=7 dB", ""))
        self.assertEqual(c.category, Category.DELIVERY)

    def test_windows_without_energy_rise_are_skipped(self):
        a = EnergyRangeAnalyzer()
        self.assertEqual(a.analyze_audio([self.win(i, {"level_dbfs": -30.0}) for i in range(4)]), [])
        self.assertEqual(len(a.skipped), 4)

    def test_energy_contradiction_is_kept(self):
        vals = [3.0, 14.0, 4.0, 15.0, 3.5]
        ws = [self.win(i, {"energy_rise_db": v}, src=f"e{i}") for i, v in enumerate(vals)]
        c = EnergyRangeAnalyzer().analyze_audio(ws)[0]
        pols = [e.polarity for e in c.evidence]
        self.assertEqual((pols.count(Polarity.SUPPORT), pols.count(Polarity.COUNTER)), (3, 2))

    def test_analyzer_output_is_stable_across_calls_and_input_order(self):
        ws = [self.win(i, {"pause_duration_s": 0.3 + i / 10}, src=f"w{i % 2}") for i in range(6)]
        a = PauseProfileAnalyzer()
        one = [e.to_dict() for e in a.analyze_audio(ws)[0].evidence]
        two = [e.to_dict() for e in a.analyze_audio(list(reversed(ws)))[0].evidence]
        self.assertEqual(one, two)
        self.assertEqual(a.analyze_audio(ws)[0].statement, a.analyze_audio(ws)[0].statement)

    def test_analyzers_never_claim_humor_emotion_or_ai_estimates(self):
        banned = re.compile(r"humou?r|funny|joke|emotion|angry|happy|excit|awkward|dead air|emphasis|sarcas|ironi", re.I)
        outs = []
        outs += SentenceRhythmAnalyzer().analyze_transcript([self.seg_(i) for i in range(3)])
        outs += PauseProfileAnalyzer().analyze_audio([self.win(i, {"pause_duration_s": 1.0}, src=f"p{i}") for i in range(3)])
        outs += EnergyRangeAnalyzer().analyze_audio([self.win(i, {"energy_rise_db": 9.0}, src=f"q{i}") for i in range(3)])
        self.assertEqual(len(outs), 3)
        for c in outs:
            self.assertFalse(banned.search(c.statement), c.statement)
            self.assertIn(c.category, (Category.SPEECH_RHYTHM, Category.PAUSES, Category.DELIVERY))
            self.assertIs(c.layer, Layer.OBSERVED)
            for e in c.evidence:
                self.assertIs(e.method, Method.MEASURED)
                self.assertFalse(banned.search(e.note), e.note)

    def seg_(self, i):
        st = self.stamp(f"sr{i}")
        return TranscriptSegment(f"s{i}", float(i), i + 1.0, "Da. Ne. Možda.", verification="reviewed", reviewer="human:ana",
                                 origin=Origin(media_id=st.media_id, record_id=st.candidate_record_id), attribution=st)

    def test_window_inputs_must_obey_the_metric_contract_before_analysis(self):
        for bad in ({"pause_duration_s": -1.0}, {"pause_duration_s": float("nan")}, {"energy_rise_db": 500}, {"mood": 1}):
            with self.assertRaises(DNAError, msg=str(bad)):
                AudioWindow("a", 0.0, 1.0, bad)

    def test_window_traits_through_a_strict_store_reach_stable_scope(self):
        ws = [self.win(i, {"pause_duration_s": 0.4}, src=f"w{i}") for i in range(3)]
        self.store.ingest(PauseProfileAnalyzer().analyze_audio(ws), "pause-profile@1")
        self.assertEqual(next(iter(self.store.traits.values())).assessment().scope, Scope.STABLE)

    def test_bucket_tables_are_the_documented_ones(self):
        self.assertEqual([b[0] for b in PAUSE_BUCKETS], ["short", "medium", "long"])
        self.assertEqual([b[1] for b in ENERGY_BUCKETS], [6.0, 12.0, None])


# ============================================================================== session independence
class SessionIndependence(Base):
    def assess(self, evs):
        tid = self.trait()
        self.add(tid, *evs)
        return self.store.get(tid).assessment()

    def test_recordings_of_one_session_are_one_witness(self):
        a = self.assess([self.pev(1, src=f"r{k}", session="shoot1") for k in range(3)])
        self.assertEqual((a.support_sources, a.scope), (1, Scope.ONE_OFF))
        self.assertTrue(any("one session are one witness" in x for x in a.explanation))

    def test_three_separate_sessions_can_be_stable(self):
        a = self.assess([self.pev(1, src=f"r{k}", session=f"shoot{k}") for k in range(3)])
        self.assertEqual((a.support_sources, a.scope), (3, Scope.STABLE))

    def test_two_sessions_are_only_emerging(self):
        a = self.assess([self.pev(1, src="a", session="s1"), self.pev(1, src="b", session="s1"), self.pev(1, src="c", session="s2")])
        self.assertEqual((a.support_sources, a.scope), (2, Scope.EMERGING))

    def test_without_session_ids_behaviour_is_the_legacy_one(self):
        a = self.assess([self.pev(1, src=f"r{k}") for k in range(3)])
        self.assertEqual((a.support_sources, a.scope), (3, Scope.STABLE))

    def test_legacy_and_session_tagged_evidence_mix(self):
        a = self.assess([self.pev(1, src="a", session="s1"), self.pev(1, src="b", session="s1"),
                         self.pev(1, src="c"), self.pev(1, src="d")])
        self.assertEqual((a.support_sources, a.scope), (3, Scope.STABLE))        # s1 + c + d

    def test_historical_results_do_not_change_when_no_session_is_known(self):
        evs = [self.pev(1, src=f"r{k}") for k in range(4)]
        on = self.assess(evs)
        tr = self.store.get(next(iter(self.store.traits)))
        off = tr.assessment(conf.ScoringConfig(group_sessions=False))
        self.assertEqual(on.to_dict(), off.to_dict())

    def test_session_grouping_can_be_switched_off_explicitly(self):
        tid = self.trait()
        self.add(tid, *[self.pev(1, src=f"r{k}", session="same") for k in range(3)])
        tr = self.store.get(tid)
        self.assertEqual(tr.assessment(conf.ScoringConfig(group_sessions=False)).support_sources, 3)
        self.assertEqual(tr.assessment().support_sources, 1)

    def test_witness_mass_combines_recordings_like_examples_of_one_source(self):
        a = self.assess([self.pev(1, src=f"r{k}", session="s") for k in range(2)])
        self.assertAlmostEqual(a.support_mass, 1.0)                      # min(1, 1 + 0.3 * 1)

    def test_counterexamples_group_by_session_too(self):
        tid = self.trait()
        self.add(tid, self.pev(1, src="a"), self.pev(1, src="b"), self.pev(1, src="c"))
        self.add(tid, *[self.pev(2, src=f"x{k}", session="bad", pol=Polarity.COUNTER) for k in range(3)])
        a = self.store.get(tid).assessment()
        self.assertEqual((a.counter_sources, a.counter_examples), (1, 3))

    def test_session_ids_survive_replay(self):
        tid = self.trait()
        self.add(tid, *[self.pev(1, src=f"r{k}", session="same") for k in range(3)])
        reloaded = self.new_store()
        self.assertEqual(reloaded.get(tid).assessment().to_dict(), self.store.get(tid).assessment().to_dict())
        self.assertEqual(reloaded.get(tid).evidence_items[0].evidence.origin.session_id, "same")

    def test_origin_validates_and_omits_empty_ids(self):
        for bad in ("", "  ", 5):
            with self.assertRaises(DNAError):
                Origin(media_id="m", session_id=bad)
            with self.assertRaises(DNAError):
                Origin(media_id="m", project_id=bad)
        d = Origin(media_id="m").to_dict()
        self.assertNotIn("session_id", d)
        self.assertNotIn("project_id", d)
        self.assertEqual(Origin.from_dict(Origin(media_id="m", session_id="s", project_id="p").to_dict()).session_id, "s")


# =============================================================================== confidence properties
class Monotonicity(Base):
    def test_counter_evidence_never_raises_confidence_or_scope_rank(self):
        tid = self.trait()
        self.add(tid, *[self.pev(1, src=f"s{k}") for k in range(4)])
        last = self.store.get(tid).assessment().confidence
        for k in range(6):
            self.add(tid, self.pev(2 + k, src=f"c{k}", pol=Polarity.COUNTER))
            now = self.store.get(tid).assessment().confidence
            self.assertLessEqual(now, last + 1e-12)
            last = now
        self.assertLess(last, self.store.get(tid).assessment().cap)

    def test_excluding_support_never_raises_confidence(self):
        tid = self.trait()
        evs = [self.pev(1, src=f"s{k}") for k in range(4)]
        self.add(tid, *evs)
        last = self.store.get(tid).assessment().confidence
        for e in evs[:3]:
            self.store.reject_evidence(tid, e.evidence_id, "no")
            now = self.store.get(tid).assessment().confidence
            self.assertLessEqual(now, last + 1e-12)
            last = now

    def test_support_after_quarantine_does_not_count(self):
        tid = self.trait()
        self.add(tid, self.pev(1))
        self.store.quarantine_all("review", "human:lukas")
        self.assertEqual(self.store.get(tid).assessment().confidence, 0.0)


# ================================================================================ review workflow
def decisions(*items):
    return json.dumps({"schema": dna.review.DECISIONS_SCHEMA, "decisions": list(items)})


class ReviewQueue(Base):
    def setUp(self):
        super().setUp()
        # stable, clean trait: not queued
        self.clean = self.stable_trait()
        # hypothesis with unrated ASR only
        self.hyp = self.store.propose_trait(Category.VOCABULARY, "Says ajde de", Layer.HYPOTHESIS, "t")
        st = self.stamp("asr")
        self.store.add_evidence(self.hyp, Evidence("a", Modality.TRANSCRIPT, Method.ASR_UNVERIFIED, start_s=1, end_s=2, quote="ajde de",
                                                   origin=Origin(media_id=st.media_id, record_id=st.candidate_record_id),
                                                   attribution=st), "t")
        # contested trait
        self.cont = self.store.propose_trait(Category.PAUSES, "Pauses contested", Layer.OBSERVED, "t")
        self.add(self.cont, self.pev(1, src="c1"), self.pev(1, src="c2"), self.pev(1, src="c3", pol=Polarity.COUNTER),
                 self.pev(2, src="c4", pol=Polarity.COUNTER))

    def codes(self, q):
        return [(i["statement"], i["priority"], [r["code"] for r in i["reasons"]]) for i in q["items"]]

    def test_golden_queue(self):
        q = dna.review_queue(self.store)
        self.assertEqual(q["schema"], "creator_dna.review_queue/1")
        self.assertEqual(self.codes(q), [
            ("Pauses contested", 2, ["contested"]),
            ("Says ajde de", 4, ["unrated_only", "hypothesis"]),
        ])
        self.assertNotIn(self.clean, [i["trait_id"] for i in q["items"]])

    def test_queue_is_deterministic(self):
        self.assertEqual(json.dumps(dna.review_queue(self.store), sort_keys=True),
                         json.dumps(dna.review_queue(self.new_store()), sort_keys=True))

    def test_confirmed_over_weak_footage_and_revoked_evidence_are_flagged(self):
        self.store.confirm(self.hyp, "yes I say that")
        revoked = self.pev(9, src="z9")
        self.add(self.clean, revoked)
        self.verifier.decisions[revoked.attribution.decision_id]["state"] = "withdrawn"
        self.store.audit_attributions()
        q = {i["trait_id"]: [r["code"] for r in i["reasons"]] for i in dna.review_queue(self.store)["items"]}
        self.assertIn("confirmed_over_weak_footage", q[self.hyp])
        self.assertEqual(q[self.clean], ["revoked_evidence"])

    def test_quarantine_and_legacy_warnings_surface(self):
        self.store.quarantine_all("review", "human:lukas")
        codes = {c for i in dna.review_queue(self.store)["items"] for c in (r["code"] for r in i["reasons"])}
        self.assertIn("quarantined_evidence", codes)

    def test_rejected_and_corrected_traits_leave_the_queue(self):
        self.store.reject(self.cont, "wrong")
        self.store.correct(self.hyp, "Says 'ajde de' a lot", "that is how I talk")
        ids = [i["trait_id"] for i in dna.review_queue(self.store)["items"]]
        self.assertNotIn(self.cont, ids)
        self.assertNotIn(self.hyp, ids)

    def test_empty_store_has_an_empty_queue(self):
        s = DNAStore(self.root / "empty")
        self.assertEqual(dna.review_queue(s), {"schema": "creator_dna.review_queue/1", "count": 0, "items": []})


class ApplyDecisions(Base):
    def setUp(self):
        super().setUp()
        self.tid = self.trait()
        self.evs = [self.pev(k, src=f"r{k}") for k in range(3)]
        self.add(self.tid, *self.evs)

    def test_confirm_with_scope_and_note(self):
        r = dna.apply_decisions(self.store, decisions({"action": "confirm", "trait_id": self.tid, "scope": "contextual", "note": "ok"}))
        self.assertEqual((r["applied"], r["results"][0]["status"]), (1, "applied"))
        tr = self.store.get(self.tid)
        self.assertEqual((tr.status, tr.assessment().scope, tr.assessment().scope_source),
                         (Status.CONFIRMED, Scope.CONTEXTUAL, "creator_declared"))

    def test_correction_keeps_the_original_trait_and_all_its_evidence(self):
        r = dna.apply_decisions(self.store, decisions({"action": "correct", "trait_id": self.tid,
                                                       "new_statement": "I hold pauses on purpose", "note": "deliberate"}))
        new = r["results"][0]["new_trait_id"]
        old = self.store.get(self.tid)
        self.assertEqual((old.status, old.superseded_by, len(old.evidence_items)), (Status.CORRECTED, new, 3))
        self.assertEqual((self.store.get(new).layer, self.store.get(new).creator_authored), (Layer.SELF_REPORT, True))

    def test_dry_run_changes_nothing(self):
        before = self.log_bytes()
        r = dna.apply_decisions(self.store, decisions({"action": "reject", "trait_id": self.tid}), dry_run=True)
        self.assertEqual((r["dry_run"], r["applied"], r["results"][0]["status"]), (True, 0, "would_apply"))
        self.assertEqual(self.log_bytes(), before)
        self.assertEqual(self.store.get(self.tid).status, Status.ACTIVE)

    def test_one_invalid_decision_applies_nothing(self):
        before = self.log_bytes()
        with self.assertRaises(ReviewDecisionError) as cm:
            dna.apply_decisions(self.store, decisions(
                {"action": "confirm", "trait_id": self.tid},
                {"action": "restore_evidence", "trait_id": self.tid, "evidence_id": "ev_does_not_exist"}))
        self.assertIn("decision 1", str(cm.exception))
        self.assertEqual(self.log_bytes(), before)
        self.assertEqual(self.store.get(self.tid).status, Status.ACTIVE)
        self.assertEqual(self.new_store().get(self.tid).status, Status.ACTIVE)

    def test_restoring_revoked_evidence_fails_the_whole_batch(self):
        self.verifier.decisions[self.evs[0].attribution.decision_id]["state"] = "withdrawn"
        self.store.audit_attributions()
        before = self.log_bytes()
        with self.assertRaises(ReviewDecisionError):
            dna.apply_decisions(self.store, decisions(
                {"action": "confirm", "trait_id": self.tid},
                {"action": "restore_evidence", "trait_id": self.tid, "evidence_id": self.evs[0].evidence_id}))
        self.assertEqual(self.log_bytes(), before)

    def test_decisions_may_depend_on_earlier_ones_in_the_same_batch(self):
        dna.apply_decisions(self.store, decisions(
            {"action": "reject_evidence", "trait_id": self.tid, "evidence_id": self.evs[0].evidence_id, "note": "wrong"},
            {"action": "restore_evidence", "trait_id": self.tid, "evidence_id": self.evs[0].evidence_id},
            {"action": "reject", "trait_id": self.tid, "note": "no"},
            {"action": "reopen", "trait_id": self.tid}))
        tr = self.store.get(self.tid)
        self.assertEqual((tr.status, tr.evidence_items[0].creator_rejected), (Status.ACTIVE, False))

    def test_correcting_twice_in_one_batch_is_refused_as_a_whole(self):
        before = self.log_bytes()
        with self.assertRaises(ReviewDecisionError):
            dna.apply_decisions(self.store, decisions(
                {"action": "correct", "trait_id": self.tid, "new_statement": "A", "note": "n"},
                {"action": "correct", "trait_id": self.tid, "new_statement": "B", "note": "n"}))
        self.assertEqual(self.log_bytes(), before)

    def test_strict_schema(self):
        t = self.tid
        bad = {
            "not json": "{nope",
            "wrong schema": json.dumps({"schema": "x", "decisions": [{"action": "confirm", "trait_id": t}]}),
            "no decisions": json.dumps({"schema": dna.review.DECISIONS_SCHEMA, "decisions": []}),
            "extra top key": json.dumps({"schema": dna.review.DECISIONS_SCHEMA, "decisions": [{"action": "confirm", "trait_id": t}], "x": 1}),
            "unknown action": decisions({"action": "delete", "trait_id": t}),
            "missing key": decisions({"action": "correct", "trait_id": t, "new_statement": "x"}),
            "extra key": decisions({"action": "confirm", "trait_id": t, "confidence": 1.0}),
            "bad scope": decisions({"action": "confirm", "trait_id": t, "scope": "forever"}),
            "unassessed scope": decisions({"action": "confirm", "trait_id": t, "scope": "unassessed"}),
            "bad category": decisions({"action": "correct", "trait_id": t, "new_statement": "x", "note": "n", "category": "politics"}),
            "non-string": decisions({"action": "confirm", "trait_id": 5}),
            "empty note on correct": decisions({"action": "correct", "trait_id": t, "new_statement": "x", "note": " "}),
            "not an object": decisions(["confirm"]),
        }
        before = self.log_bytes()
        for name, doc in bad.items():
            with self.assertRaises(ReviewDecisionError, msg=name):
                dna.apply_decisions(self.store, doc)
        self.assertEqual(self.log_bytes(), before)

    def test_all_problems_are_listed(self):
        with self.assertRaises(ReviewDecisionError) as cm:
            dna.apply_decisions(self.store, decisions({"action": "nope"}, {"action": "confirm"}))
        self.assertEqual(len(cm.exception.problems), 2)

    def test_accepts_dict_str_and_bytes(self):
        doc = {"schema": dna.review.DECISIONS_SCHEMA, "decisions": [{"action": "confirm", "trait_id": self.tid}]}
        for form in (doc, json.dumps(doc), json.dumps(doc).encode("utf-8")):
            self.assertEqual(dna.apply_decisions(self.store, form, dry_run=True)["results"][0]["action"], "confirm")

    def test_failure_while_writing_rolls_the_whole_batch_back(self):
        before = self.log_bytes()
        orig, calls = self.store._emit, [0]

        def flaky(*a, **k):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("disk full")
            return orig(*a, **k)
        self.store._emit = flaky
        with self.assertRaisesRegex(ReviewDecisionError, "batch undone"):
            dna.apply_decisions(self.store, decisions({"action": "confirm", "trait_id": self.tid},
                                                      {"action": "reject_evidence", "trait_id": self.tid,
                                                       "evidence_id": self.evs[0].evidence_id, "note": "x"}))
        self.assertEqual(self.log_bytes(), before)
        self.assertEqual(self.store.get(self.tid).status, Status.ACTIVE)
        self.assertFalse(self.store.get(self.tid).evidence_items[0].creator_rejected)

    def test_actor_is_recorded_and_required(self):
        dna.apply_decisions(self.store, decisions({"action": "confirm", "trait_id": self.tid}), actor="human:lukas")
        self.assertEqual(self.events()[-1]["actor"], "human:lukas")
        with self.assertRaises(ReviewDecisionError):
            dna.apply_decisions(self.store, decisions({"action": "confirm", "trait_id": self.tid}), actor=" ")

    def test_batch_does_not_touch_the_callers_clock_or_directory(self):
        n = self.t
        dna.apply_decisions(self.store, decisions({"action": "confirm", "trait_id": self.tid}), dry_run=True)
        self.assertEqual(self.t, n)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["events.jsonl"])


class DiffExports(Base):
    def setUp(self):
        super().setUp()
        self.tid = self.trait()
        self.evs = [self.pev(k, src=f"r{k}") for k in range(2)]
        self.add(self.tid, *self.evs)
        self.a = self.store.export()

    def test_identical_profiles_have_an_empty_diff(self):
        d = dna.diff_exports(self.a, self.store.export())
        self.assertEqual((d["traits_added"], d["traits_removed"], d["trait_changes"], d["contradictions"]), ([], [], [], []))

    def test_new_and_removed_traits(self):
        other = self.trait(statement="A new pause habit")
        self.add(other, self.pev(1, src="n1"))
        d = dna.diff_exports(self.a, self.store.export())
        self.assertEqual([t["statement"] for t in d["traits_added"]], ["A new pause habit"])
        back = dna.diff_exports(self.store.export(), self.a)
        self.assertEqual([t["statement"] for t in back["traits_removed"]], ["A new pause habit"])

    def test_new_support_changes_confidence_and_scope(self):
        new = self.pev(5, src="r5")
        self.add(self.tid, new)
        d = dna.diff_exports(self.a, self.store.export())
        ch = d["trait_changes"][0]["changes"]
        self.assertEqual(ch["new_support"], [new.evidence_id])
        self.assertEqual(ch["scope"], {"from": "emerging", "to": "stable"})
        self.assertGreater(ch["confidence"]["delta"], 0)

    def test_new_counter_evidence_is_a_contradiction(self):
        counter = self.pev(7, src="x7", pol=Polarity.COUNTER)
        self.add(self.tid, counter)
        d = dna.diff_exports(self.a, self.store.export())
        self.assertEqual(d["trait_changes"][0]["changes"]["new_counter"], [counter.evidence_id])
        self.assertEqual(d["contradictions"][0]["reason"], "counter-evidence added to a supported trait")
        self.assertLess(d["trait_changes"][0]["changes"]["confidence"]["delta"], 0)

    def test_counter_evidence_against_a_confirmed_trait_is_called_out(self):
        self.store.confirm(self.tid, "ok")
        before = self.store.export()
        self.add(self.tid, self.pev(7, src="x7", pol=Polarity.COUNTER))
        d = dna.diff_exports(before, self.store.export())
        self.assertIn("confirmed", d["contradictions"][0]["reason"])

    def test_revocation_rejection_and_quarantine_effects(self):
        self.store.reject_evidence(self.tid, self.evs[0].evidence_id, "no")
        self.verifier.decisions[self.evs[1].attribution.decision_id]["state"] = "withdrawn"
        self.store.audit_attributions()
        d = dna.diff_exports(self.a, self.store.export())
        ex = d["trait_changes"][0]["changes"]["newly_excluded"]
        self.assertEqual((ex["rejected"], ex["revoked"]), ([self.evs[0].evidence_id], [self.evs[1].evidence_id]))
        mid = self.store.export()
        self.store.withdraw_consent("stop", "human:lukas")
        q = dna.diff_exports(mid, self.store.export())
        self.assertEqual(len(q["trait_changes"][0]["changes"]["newly_excluded"]["quarantined"]), 2)

    def test_status_changes_for_rejection_and_correction(self):
        self.store.reject(self.tid, "wrong")
        d = dna.diff_exports(self.a, self.store.export())
        self.assertEqual(d["trait_changes"][0]["changes"]["status"], {"from": "active", "to": "rejected"})
        self.assertIsNone(d["trait_changes"][0]["changes"]["confidence"]["to"])

    def test_correction_adds_a_creator_trait_and_marks_the_old_one(self):
        new = self.store.correct(self.tid, "I pause on purpose", "deliberate")
        d = dna.diff_exports(self.a, self.store.export())
        self.assertEqual([t["trait_id"] for t in d["traits_added"]], [new])
        self.assertEqual(d["trait_changes"][0]["changes"]["status"]["to"], "corrected")
        self.assertEqual(d["trait_changes"][0]["changes"]["superseded_by"]["to"], new)

    def test_diff_is_deterministic_and_ignores_timestamps(self):
        self.add(self.tid, self.pev(5, src="r5"))
        b = self.store.export()
        one = json.dumps(dna.diff_exports(self.a, b), sort_keys=True)
        b2 = dict(b, generated_at="1999-01-01T00:00:00+00:00", events=0)
        self.assertEqual(one, json.dumps(dna.diff_exports(self.a, b2), sort_keys=True))

    def test_golden_diff(self):
        new = self.pev(5, src="r5")
        self.add(self.tid, new)
        d = dna.diff_exports(self.a, self.store.export())
        ch = d["trait_changes"][0]
        self.assertEqual(d["summary"], {"added": 0, "removed": 0, "changed": 1, "contradictions": 0})
        self.assertEqual(sorted(ch["changes"]), ["band", "confidence", "new_support", "scope"])
        self.assertEqual(ch["trait_id"], self.tid)

    def test_session_tagged_support_shows_up_as_new_evidence(self):
        self.add(self.tid, self.pev(5, src="r5", session="s1"), self.pev(6, src="r6", session="s1"))
        d = dna.diff_exports(self.a, self.store.export())
        self.assertEqual(len(d["trait_changes"][0]["changes"]["new_support"]), 2)


# ===================================================================== safety rails: no media, no MVI
class NoRealInput(unittest.TestCase):
    MEDIA = (".mov", ".mp4", ".mkv", ".wav", ".mp3", ".m4a", ".avi", ".flac")

    def test_package_has_no_media_process_network_or_upstream_imports(self):
        imports = re.compile(r"(?m)^\s*(?:from|import)\s+(subprocess|socket|urllib|http|cv2|librosa|whisper|wave|"
                             r"media_analysis|speaker_attribution|creator_state)")
        for f in (ROOT / "creator_dna").glob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertIsNone(imports.search(text), f"{f.name} imports a forbidden module")
            for word in ("ffprobe", "ffmpeg"):
                self.assertNotIn(word, text, f"{f.name} mentions {word}")

    def test_no_media_file_is_opened_by_the_new_code_paths(self):
        opened = []
        real_open = builtins.open

        def spy(file, *a, **k):
            opened.append(str(file))
            return real_open(file, *a, **k)
        with tempfile.TemporaryDirectory() as td, mock.patch("builtins.open", spy):
            s = strict_test_store(Path(td) / "d", project_id="p")
            tid = s.propose_trait(Category.PAUSES, "x", Layer.OBSERVED, "t")
            s.export()
            dna.review_queue(s)
            dna.apply_decisions(s, decisions({"action": "confirm", "trait_id": tid}), dry_run=True)
            s.withdraw_consent("done", "human:t")
        self.assertTrue(opened)
        self.assertFalse([p for p in opened if p.lower().endswith(self.MEDIA)], opened)

    def test_the_prohibited_recording_is_not_referenced_by_the_package_or_fixtures(self):
        needle = "MVI_" + "0879"
        for f in list((ROOT / "creator_dna").glob("*.py")) + [Path(__file__).parent / "_dna_verifiers.py"]:
            self.assertNotIn(needle, f.read_text(encoding="utf-8"), f.name)

    def test_synthetic_source_ids_do_not_look_like_real_footage(self):
        for f in (Path(__file__),):
            self.assertNotRegex(f.read_text(encoding="utf-8"), r"(?i)\bMVI_\d{4}\b.*\.(mov|mp4)")


if __name__ == "__main__":
    unittest.main()
