"""Asset Agent tests. Synthetic fixtures only; temp dirs; sockets blocked (no network).

Run from orchestrator/:  python -I -m unittest discover -s tests -p test_assets.py -v
"""
import copy
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assets import (PERMITTED, QUARANTINE, REJECT, AssetCandidate, AssetExistsError, AssetLibrary, AssetRecord,
                    AssetRequest, Category, DuplicateAssetError, Evidence, ManifestError, MediaType,
                    ProviderRegistry, RightsBasis, State, TargetSpec, UnsafePathError, UseContext, Verification,
                    Watermark, candidate_to_record, evaluate_rights, parse_license, sanitize_candidate, triage)

CLOCK = lambda: "2026-11-08T00:00:00+00:00"


def _no_net(*a, **k):
    raise AssertionError("network access attempted")


def rec(asset_id="a1", **kw) -> AssetRecord:
    """A documented, user-owned, unverified record with EXPLICIT permissions (override per test)."""
    base = dict(asset_id=asset_id, title="Mushroom closeup", category=Category.BROLL, media_type=MediaType.VIDEO,
                tags=["mushroom", "macro"], source="creator", rights_basis=RightsBasis.USER_OWNED,
                watermark_status=Watermark.CLEAR, commercial_use_allowed=True, modification_allowed=True,
                evidence=[Evidence("owner_declaration", "filmed by the creator", strength="declaration")])
    base.update(kw)
    return AssetRecord(**base)


def cc(name="CC-BY-4.0", **kw) -> AssetRecord:
    base = dict(rights_basis=RightsBasis.CREATIVE_COMMONS, license_name=name, source="example-archive",
                commercial_use_allowed=None, modification_allowed=None,   # CC terms come from the licence
                source_url="https://example.invalid/x", creator="Someone", attribution_text="Photo by Someone, CC BY 4.0",
                evidence=[Evidence("license_page", "captured licence page", strength="document")])
    base.update(kw)
    return rec(**base)


def verified(r: AssetRecord, method="human_review") -> AssetRecord:
    """Attest the record's CURRENT facts (evaluate_rights-level tests only; the library path is
    exercised through lib.attest). Re-call after mutating the record."""
    from assets import Attestation
    from assets.rights import rights_fingerprint
    r.file_path = r.file_path or "C:/synthetic/clip.mp4"
    r.checksum_sha256 = r.checksum_sha256 or "a" * 64
    r.attestations.append(Attestation("Reviewer", method, "checked", "2026-11-08", rights_fingerprint(r)))
    r.verification_status = Verification.VERIFIED
    return r


def owned_verified(asset_id="a1", **kw):
    return verified(rec(asset_id, **kw), "user_declaration")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()   # no links in the chain (sources are inspected)
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(socket.socket, "connect", _no_net)
        p.start()
        self.addCleanup(p.stop)
        self.lib = AssetLibrary(self.tmp / "lib", clock=CLOCK)
        (self.tmp / "src").mkdir()

    def dir_link(self, link: Path, target: Path) -> bool:
        """Create a directory symlink, or a junction on Windows. False if neither is possible here."""
        try:
            link.symlink_to(target, target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            pass
        if sys.platform == "win32":
            import subprocess
            return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                  capture_output=True).returncode == 0
        return False

    def media(self, name="clip.mp4", data=b"synthetic-bytes-1") -> Path:
        p = self.tmp / "src" / name
        p.write_bytes(data)
        return p


class TestRights(unittest.TestCase):
    def test_unknown_rights_never_permitted(self):
        d = evaluate_rights(rec(rights_basis=RightsBasis.UNKNOWN, source="somewhere"))
        self.assertEqual(d.verdict, QUARANTINE)
        self.assertIn("UNKNOWN_RIGHTS", d.codes())
        self.assertEqual(d.confidence, "none")

    def test_unknown_source_rejected(self):
        d = evaluate_rights(rec(rights_basis=RightsBasis.UNKNOWN, source="", source_url=""))
        self.assertEqual(d.verdict, REJECT)
        self.assertIn("SOURCE_UNKNOWN", d.codes())

    def test_missing_license_not_permitted(self):
        d = evaluate_rights(verified(cc(name="")))
        self.assertFalse(d.permitted)
        self.assertIn("MISSING_LICENSE", d.codes())

    def test_unverified_valid_license_still_quarantined(self):
        d = evaluate_rights(cc())
        self.assertEqual(d.verdict, QUARANTINE)
        self.assertIn("UNVERIFIED", d.codes())

    def test_verified_cc_by_permitted(self):
        d = evaluate_rights(verified(cc()))
        self.assertTrue(d.permitted, d.codes())
        self.assertEqual(d.confidence, "high")

    def test_cc_by_needs_attribution_text(self):
        d = evaluate_rights(verified(cc(attribution_text="")))
        self.assertIn("ATTRIBUTION_TEXT_MISSING", d.codes())
        self.assertFalse(d.permitted)

    def test_cc_nc_rejected_for_commercial_but_ok_noncommercial(self):
        r = verified(cc("CC-BY-NC-4.0"))
        self.assertEqual(evaluate_rights(r).verdict, REJECT)
        self.assertIn("NO_COMMERCIAL", evaluate_rights(r).codes())
        self.assertTrue(evaluate_rights(r, UseContext(commercial=False)).permitted)

    def test_cc_nd_rejected_when_editing(self):
        r = verified(cc("CC BY-ND 4.0"))
        self.assertIn("NO_MODIFICATION", evaluate_rights(r).codes())
        self.assertTrue(evaluate_rights(r, UseContext(modification=False)).permitted)

    def test_cc_sa_needs_acceptance(self):
        r = verified(cc("CC-BY-SA-4.0"))
        self.assertIn("SHARE_ALIKE", evaluate_rights(r).codes())
        self.assertTrue(evaluate_rights(r, UseContext(accept_share_alike=True)).permitted)

    def test_not_all_cc_allow_everything(self):
        for name, comm, mod in [("CC-BY-4.0", True, True), ("CC-BY-NC-4.0", False, True), ("CC-BY-ND-4.0", True, False),
                                ("CC-BY-NC-ND-4.0", False, False), ("CC-BY-SA-3.0", True, True), ("CC0-1.0", True, True)]:
            t = parse_license(name)
            self.assertEqual((t.commercial, t.modification), (comm, mod), name)

    def test_unrecognised_cc_name_quarantined(self):
        d = evaluate_rights(verified(cc("CC-BY-XX-9.9")))
        self.assertIn("UNRECOGNISED_LICENSE", d.codes())
        self.assertFalse(d.permitted)

    def test_record_flag_cannot_loosen_license(self):
        d = evaluate_rights(verified(cc("CC-BY-NC-4.0", commercial_use_allowed=True)))
        self.assertEqual(d.verdict, REJECT)
        self.assertIn("LICENSE_FIELD_MISMATCH", d.codes())

    def test_record_flag_can_tighten_license(self):
        d = evaluate_rights(verified(cc(modification_allowed=False)))
        self.assertIn("NO_MODIFICATION", d.codes())

    def test_watermarked_preview_rejected_even_if_verified(self):
        self.assertEqual(evaluate_rights(verified(cc(watermark_status=Watermark.WATERMARKED))).verdict, REJECT)
        d = evaluate_rights(verified(cc(is_preview=True)))
        self.assertEqual(d.verdict, REJECT)
        self.assertIn("WATERMARKED_PREVIEW", d.codes())

    def test_unchecked_or_suspected_watermark_quarantined(self):
        for w in (Watermark.UNCHECKED, Watermark.SUSPECTED):
            self.assertFalse(evaluate_rights(verified(cc(watermark_status=w))).permitted)

    def test_watermark_hint_in_title_quarantined(self):
        d = evaluate_rights(verified(cc(title="Forest (preview)")))
        self.assertIn("WATERMARK_HINT", d.codes())

    def test_no_watermark_is_not_permission(self):
        d = evaluate_rights(rec(rights_basis=RightsBasis.LICENSED_OTHER, source="x", watermark_status=Watermark.CLEAR))
        self.assertFalse(d.permitted)

    def test_licensed_other_unknown_terms_quarantined(self):
        r = verified(rec(rights_basis=RightsBasis.LICENSED_OTHER, license_name="Stock EULA", source="stock-site",
                         commercial_use_allowed=None, modification_allowed=None,
                         evidence=[Evidence("receipt", "purchase receipt", strength="document")]))
        d = evaluate_rights(r)
        self.assertIn("COMMERCIAL_UNKNOWN", d.codes())
        self.assertIn("MODIFICATION_UNKNOWN", d.codes())
        r.commercial_use_allowed, r.modification_allowed, r.attribution_required = True, True, False
        self.assertIn("ATTESTATION_STALE", evaluate_rights(r).codes())   # facts changed after attestation
        self.assertTrue(evaluate_rights(verified(r)).permitted)

    def test_incompatible_restrictions(self):
        base = dict(rights_basis=RightsBasis.LICENSED_OTHER, license_name="EULA", source="s", commercial_use_allowed=True,
                    modification_allowed=True, attribution_required=False,
                    evidence=[Evidence("receipt", "r", strength="document")])
        self.assertEqual(evaluate_rights(verified(rec(restrictions=["editorial_only"], **base))).verdict, REJECT)
        self.assertEqual(evaluate_rights(verified(rec(restrictions=["no_modification"], **base))).verdict, REJECT)
        d = evaluate_rights(verified(rec(restrictions=["only on tuesdays"], **base)))
        self.assertIn("UNRECOGNISED_RESTRICTION", d.codes())
        self.assertEqual(d.verdict, QUARANTINE)

    def test_allowed_uses_mismatch(self):
        r = verified(cc(allowed_uses=["editorial"]))
        d = evaluate_rights(r, UseContext(required_uses=("social_media",)))
        self.assertIn("USE_NOT_ALLOWED", d.codes())

    def test_claim_only_evidence_insufficient(self):
        r = verified(cc(evidence=[Evidence("provider_claim", "site says CC", strength="claim")]))
        self.assertIn("CLAIM_ONLY_EVIDENCE", evaluate_rights(r).codes())
        self.assertFalse(evaluate_rights(r).permitted)

    def test_no_evidence_quarantined(self):
        self.assertIn("NO_EVIDENCE", evaluate_rights(verified(cc(evidence=[]))).codes())

    def test_user_owned_needs_declaration(self):
        d = evaluate_rights(verified(rec(), "human_review"))
        self.assertIn("NO_OWNER_DECLARATION", d.codes())
        self.assertTrue(evaluate_rights(owned_verified()).permitted)

    def test_generated_requires_provider_terms(self):
        r = verified(rec(rights_basis=RightsBasis.GENERATED, source="gen-tool", commercial_use_allowed=True,
                         modification_allowed=True, attribution_required=False, category=Category.GENERATED))
        self.assertIn("NO_PROVIDER_TERMS", evaluate_rights(r).codes())
        r.evidence.append(Evidence("provider_terms", "captured terms", strength="document"))
        self.assertTrue(evaluate_rights(verified(r)).permitted)

    def test_public_domain_cc0_and_free_text(self):
        pd = verified(rec(rights_basis=RightsBasis.PUBLIC_DOMAIN, license_name="CC0-1.0", source="archive",
                          evidence=[Evidence("license_page", "cc0 dedication", strength="document")]))
        self.assertTrue(evaluate_rights(pd).permitted)
        pd.license_name = "CC-BY-4.0"
        self.assertIn("BASIS_LICENSE_MISMATCH", evaluate_rights(verified(pd)).codes())
        pd.license_name = "All rights reserved"
        self.assertIn("BASIS_LICENSE_MISMATCH", evaluate_rights(verified(pd)).codes())
        pd.license_name = "Public domain"
        self.assertTrue(evaluate_rights(verified(pd)).permitted)

    def test_disputed(self):
        r = owned_verified()
        r.verification_status = Verification.DISPUTED
        self.assertIn("DISPUTED", evaluate_rights(r).codes())


class TestSchema(unittest.TestCase):
    def test_roundtrip(self):
        r = verified(cc())
        r2 = AssetRecord.from_dict(json.loads(json.dumps(r.to_dict())))
        self.assertEqual(r.to_dict(), r2.to_dict())

    def test_invalid_manifests(self):
        good = cc().to_dict()
        cases = {
            "unknown key": {**good, "evil": 1},
            "bad enum": {**good, "category": "weapon"},
            "bad checksum": {**good, "checksum_sha256": "xyz"},
            "bad id": {**good, "asset_id": "../x"},
            "bad date": {**good, "acquisition_date": "yesterday"},
            "bad tri-state": {**good, "commercial_use_allowed": "yes"},
            "bad version": {**good, "schema_version": 99},
            "bad scope": {**good, "scope": "project:../x"},
            "verified w/o attestation": {**good, "verification_status": "verified"},
            "bad evidence": {**good, "evidence": [{"kind": "nope", "description": "x"}]},
            "bad quality": {**good, "quality": {"visual_score": 7}},
            "bad technical": {**good, "technical": {"width": -1}},
            "control chars": {**good, "title": "a\x00b"},
            "not an object": [1, 2],
        }
        for name, d in cases.items():
            with self.assertRaises(ManifestError, msg=name):
                AssetRecord.from_dict(d)
        missing = copy.deepcopy(good)
        del missing["title"]
        with self.assertRaises(ManifestError):
            AssetRecord.from_dict(missing)

    def test_default_state_is_quarantined_and_unknown(self):
        r = AssetRecord("x", "t", Category.IMAGE, MediaType.IMAGE)
        self.assertEqual((r.state, r.rights_basis, r.verification_status),
                         (State.QUARANTINED, RightsBasis.UNKNOWN, Verification.UNVERIFIED))
        self.assertIsNone(r.commercial_use_allowed)


class TestLibrary(Base):
    def test_unknown_rights_quarantined_on_add(self):
        r = self.lib.add(rec(rights_basis=RightsBasis.UNKNOWN, source="s"), self.media())
        self.assertEqual(r.state, State.QUARANTINED)
        self.assertEqual(self.lib.search(), [])  # default search hides non-approved

    def test_watermark_rejected_and_not_copied(self):
        r = self.lib.add(rec(watermark_status=Watermark.WATERMARKED), self.media(), mode="copy")
        self.assertEqual(r.state, State.REJECTED)
        self.assertFalse(r.managed_copy)
        self.assertFalse((self.tmp / "lib" / "files").exists())

    def test_full_approval_flow(self):
        r = self.lib.add(rec(), self.media())
        self.assertEqual(r.state, State.QUARANTINED)
        self.lib.attest("a1", "Luka", "user_declaration", "I filmed this")
        self.assertEqual(self.lib.get("a1").state, State.APPROVED)
        self.assertEqual([x.asset_id for x in self.lib.search(tags=["mushroom"])], ["a1"])
        events = [h["event"] for h in self.lib.get("a1").history]
        self.assertIn("attested", events)

    def test_cannot_approve_without_attestation_or_via_flags(self):
        self.lib.add(rec(), self.media())
        self.lib.reevaluate("a1")
        self.assertNotEqual(self.lib.get("a1").state, State.APPROVED)
        with self.assertRaises(ValueError):
            self.lib.attest("a1", "", "user_declaration", "x")
        with self.assertRaises(ValueError):
            self.lib.attest("a1", "Bot", "automated", "x")

    def test_attestation_by_wrong_method_for_user_owned_is_not_enough(self):
        self.lib.add(rec(), self.media())
        self.lib.attest("a1", "Reviewer", "human_review", "looks fine")
        self.assertEqual(self.lib.get("a1").state, State.QUARANTINED)

    def test_update_rights_resets_verification(self):
        self.lib.add(rec(), self.media())
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.lib.update_rights("a1", commercial_use_allowed=False)
        r = self.lib.get("a1")
        self.assertEqual(r.verification_status, Verification.UNVERIFIED)
        self.assertNotEqual(r.state, State.APPROVED)
        self.assertEqual(len(r.attestations), 1)  # history preserved
        with self.assertRaises(ValueError):
            self.lib.update_rights("a1", title="not a rights field")

    def test_evidence_is_preserved(self):
        self.lib.add(rec(), self.media())
        self.lib.add_evidence("a1", Evidence("screenshot", "later screenshot", strength="document"))
        self.assertEqual(len(self.lib.get("a1").evidence), 2)

    def test_duplicate_content_detected(self):
        self.lib.add(rec("a1"), self.media("one.mp4"))
        with self.assertRaises(DuplicateAssetError) as cm:
            self.lib.add(rec("a2"), self.media("two.mp4"))   # same bytes
        self.assertEqual(cm.exception.existing_id, "a1")
        self.assertEqual(len(self.lib.all()), 1)
        self.lib.link_project("a1", "my-video")
        self.assertIn("my-video", self.lib.get("a1").projects)

    def test_no_overwrite_of_id_or_file(self):
        self.lib.add(rec("a1"), self.media("one.mp4"), mode="copy")
        with self.assertRaises(AssetExistsError):
            self.lib.add(rec("a1"), self.media("two.mp4", b"different"))
        # pre-existing destination file is never overwritten
        dest = self.tmp / "lib" / "files" / "broll" / "a3.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"precious")
        with self.assertRaises(AssetExistsError):
            self.lib.add(rec("a3"), self.media("three.mp4", b"third"), mode="copy")
        self.assertEqual(dest.read_bytes(), b"precious")
        self.assertNotIn("a3", {r.asset_id for r in self.lib.all()})

    def test_original_never_modified_or_removed(self):
        src = self.media("orig.mp4", b"original-data")
        before = (src.read_bytes(), src.stat().st_mtime_ns)
        self.lib.add(rec(), src, mode="copy")
        self.assertEqual((src.read_bytes(), src.stat().st_mtime_ns), before)
        self.assertTrue(src.exists())
        self.assertTrue(self.lib.get("a1").managed_copy)
        self.assertEqual((self.tmp / "lib" / self.lib.get("a1").file_path).read_bytes(), b"original-data")
        self.assertFalse(any(n.startswith(("delete", "remove")) for n in dir(self.lib)))

    def test_unsafe_ids_and_paths(self):
        for bad in ("../evil", "a/b", "a\\b", "", "A1", "x" * 80, "con:ads"):
            with self.assertRaises(UnsafePathError, msg=bad):
                self.lib.add(rec(bad), self.media())
        with self.assertRaises(UnsafePathError):
            self.lib.add(rec("b1"), self.tmp / "src")            # directory
        with self.assertRaises(UnsafePathError):
            self.lib.add(rec("b2"), self.tmp / "src" / "nope.mp4")  # missing
        with self.assertRaises(ManifestError):
            self.lib.add(rec("b3", projects=["../x"]), self.media())
        self.assertEqual(self.lib.all(), [])

    def test_symlink_source_refused(self):
        target = self.media("t.mp4")
        link = self.tmp / "src" / "link.mp4"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(UnsafePathError):
            self.lib.add(rec(), link)

    def test_managed_suffix_sanitised(self):
        p = self.media("weird.mp4:stream")  if sys.platform != "win32" else self.media("weird.mp4")
        self.lib.add(rec(), p, mode="copy")
        dest = (self.tmp / "lib" / self.lib.get("a1").file_path).resolve()
        self.assertIn((self.tmp / "lib").resolve(), dest.parents)

    def test_checksum_mismatch_refused(self):
        with self.assertRaises(ManifestError):
            self.lib.add(rec(checksum_sha256="0" * 64), self.media())

    def test_persistence_and_search(self):
        self.lib.add(rec("a1", tags=["Mushroom", "macro"]), self.media("1.mp4", b"1"))
        self.lib.add(rec("a2", title="Theme", category=Category.MUSIC, media_type=MediaType.AUDIO, tags=["calm"],
                         scope="project:vid-1"), self.media("2.mp3", b"2"))
        self.lib.add(rec("a3", title="Other", scope="project:vid-2", tags=["calm"]), self.media("3.mp4", b"3"))
        for i in ("a1", "a2", "a3"):
            self.lib.attest(i, "Luka", "user_declaration", "mine")
        lib2 = AssetLibrary(self.tmp / "lib", clock=CLOCK)
        ids = lambda xs: [x.asset_id for x in xs]
        self.assertEqual(ids(lib2.search(tags=["mushroom"])), ["a1"])
        self.assertEqual(ids(lib2.search(category=Category.MUSIC)), ["a2"])
        self.assertEqual(ids(lib2.search(media_type=MediaType.AUDIO)), ["a2"])
        self.assertEqual(ids(lib2.search(any_tags=["calm", "macro"])), ["a1", "a2", "a3"])
        self.assertEqual(ids(lib2.search(project="vid-1")), ["a1", "a2"])           # project + global
        self.assertEqual(ids(lib2.search(project="vid-1", include_global=False)), ["a2"])
        self.assertEqual(ids(lib2.search(text="theme")), ["a2"])

    def test_corrupt_index_not_overwritten(self):
        self.lib.add(rec(), self.media())
        idx = self.tmp / "lib" / "index.json"
        idx.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ManifestError):
            AssetLibrary(self.tmp / "lib")
        self.assertEqual(idx.read_text(encoding="utf-8"), "{broken")
        d = {"schema_version": 1, "assets": [{"asset_id": "x"}]}
        idx.write_text(json.dumps(d), encoding="utf-8")
        with self.assertRaises(ManifestError):
            AssetLibrary(self.tmp / "lib")

    def test_verify_files_quarantines_changed_and_missing(self):
        src = self.media()
        self.lib.add(rec(), src, mode="copy")
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.assertEqual(self.lib.verify_files(), [])
        (self.tmp / "lib" / self.lib.get("a1").file_path).write_bytes(b"tampered")
        problems = self.lib.verify_files()
        self.assertEqual(problems[0]["problem"], "checksum changed")
        self.assertEqual(self.lib.get("a1").state, State.QUARANTINED)
        # sticky: attesting again does not silently re-approve
        self.lib.attest("a1", "Luka", "user_declaration", "again")
        self.assertEqual(self.lib.get("a1").state, State.QUARANTINED)
        self.assertTrue(src.exists())

    def test_manual_hold_sticky_and_release(self):
        self.lib.add(rec(), self.media())
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.lib.quarantine("a1", "looks suspicious")
        self.lib.reevaluate("a1")
        self.assertEqual(self.lib.get("a1").state, State.QUARANTINED)
        self.lib.release_hold("a1", "Luka", "checked again")
        self.assertEqual(self.lib.get("a1").state, State.APPROVED)

    def test_cleared_for_context(self):
        self.lib.add(cc("CC-BY-NC-4.0"), self.media())
        self.lib.attest("a1", "Luka", "human_review", "licence page checked")
        self.assertEqual(self.lib.get("a1").state, State.REJECTED)  # default ctx is commercial
        self.assertFalse(self.lib.cleared_for("a1", UseContext()))

    def test_manual_reject_state(self):
        self.lib.add(rec(), self.media())
        self.lib.reject("a1", "wrong content")
        self.assertEqual(self.lib.get("a1").state, State.REJECTED)


class TestEvaluation(Base):
    def test_relevance_quality_technical(self):
        self.lib.add(rec(technical={"width": 1920, "height": 1080, "duration_s": 8.0},
                         quality={"visual_score": 0.8}), self.media())
        req = AssetRequest(description="macro shot of a mushroom", keywords=["fungi"], categories=[Category.BROLL])
        ev = self.lib.evaluate("a1", req, TargetSpec(min_width=1920, min_height=1080, allowed_extensions=(".mp4",)))
        self.assertGreater(ev.relevance.score, 0.15)
        self.assertEqual(ev.technical.status, "compatible")
        self.assertEqual(ev.quality_score, 0.8)
        self.assertEqual(ev.verdict, "quarantine")  # rights not yet verified dominates
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.assertEqual(self.lib.evaluate("a1", req).verdict, "usable")

    def test_technical_incompatible_and_unknown(self):
        self.lib.add(rec(technical={"width": 640, "height": 360}), self.media())
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        ev = self.lib.evaluate("a1", AssetRequest(keywords=["mushroom"]), TargetSpec(min_width=1920))
        self.assertEqual((ev.technical.status, ev.verdict), ("incompatible", "unsuitable"))
        self.lib.add(rec("a2"), self.media("2.mp4", b"2"))
        ev2 = self.lib.evaluate("a2", AssetRequest(keywords=["mushroom"]), TargetSpec(min_width=1920))
        self.assertEqual(ev2.technical.status, "unknown")   # missing metadata is not a pass

    def test_irrelevant_and_wrong_category(self):
        self.lib.add(rec(), self.media())
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        ev = self.lib.evaluate("a1", AssetRequest(description="oil painting of ships", categories=[Category.MUSIC]))
        self.assertEqual(ev.verdict, "unsuitable")
        codes = {r.code for r in ev.reasons}
        self.assertTrue({"LOW_RELEVANCE", "CATEGORY_MISMATCH"} <= codes)

    def test_rejection_reasons_explicit(self):
        self.lib.add(rec(watermark_status=Watermark.WATERMARKED), self.media())
        ev = self.lib.evaluate("a1", AssetRequest(keywords=["mushroom"]))
        self.assertEqual(ev.verdict, "reject")
        self.assertIn("WATERMARKED_PREVIEW", {r.code for r in ev.reasons})
        self.assertTrue(all(r.message for r in ev.reasons))

    def test_bad_quality_metadata_rejected_at_add(self):
        with self.assertRaises(ManifestError):
            self.lib.add(rec(quality={"audio_score": 2}), self.media())


class TestProviders(Base):
    def cand(self, **kw):
        base = dict(provider="fake", external_id="e1", title="Forest", category=Category.BROLL,
                    media_type=MediaType.VIDEO, source_url="https://example.invalid/v/1",
                    claimed_license_name="CC-BY-4.0", claimed_commercial_use=True, claimed_modification=True,
                    claimed_attribution_text="by X", claims_watermarked=False)
        base.update(kw)
        return AssetCandidate(**base)

    def test_triage_never_permits(self):
        d = triage(self.cand())
        self.assertEqual(d.verdict, QUARANTINE)
        self.assertIn("UNVERIFIED", d.codes())
        self.assertIn("CLAIM_ONLY_EVIDENCE", d.codes())

    def test_triage_rejects_watermarked_and_nc(self):
        self.assertEqual(triage(self.cand(claims_watermarked=True)).verdict, REJECT)
        self.assertEqual(triage(self.cand(claims_preview=True)).verdict, REJECT)
        self.assertEqual(triage(self.cand(claimed_license_name="CC-BY-NC-4.0")).verdict, REJECT)

    def test_claimed_clear_watermark_not_trusted(self):
        r = candidate_to_record(self.cand(claims_watermarked=False), "c1", "2026-11-08")
        self.assertEqual(r.watermark_status, Watermark.UNCHECKED)
        self.assertEqual(r.verification_status, Verification.UNVERIFIED)
        self.assertIsNone(r.file_path)
        self.assertEqual(r.checksum_sha256, "")

    def test_no_license_claim_is_unknown(self):
        r = candidate_to_record(self.cand(claimed_license_name=""), "c1", "2026-11-08")
        self.assertEqual(r.rights_basis, RightsBasis.UNKNOWN)

    def test_sanitize_url_and_malformed(self):
        c = sanitize_candidate(self.cand(source_url="https://user:pw@example.invalid/p?token=SECRET"))
        self.assertEqual(c.source_url, "")           # credentials in URL -> dropped entirely
        c = sanitize_candidate(self.cand(source_url="https://example.invalid/p?token=SECRET"))
        self.assertNotIn("SECRET", c.source_url)
        self.assertEqual(sanitize_candidate(self.cand(source_url="file:///etc/passwd")).source_url, "")
        for bad in (self.cand(title=""), self.cand(title="a\x07b"), self.cand(claimed_commercial_use="yes")):
            with self.assertRaises(ManifestError):
                sanitize_candidate(bad)

    def test_registry_isolates_failures(self):
        class Good:
            name = "good"
            def search(self, request, max_results=10):
                return [self_.cand(), self_.cand(title="")]
        class Boom:
            name = "boom"
            def search(self, request, max_results=10):
                raise RuntimeError("secret-token-123")
        self_ = self
        reg = ProviderRegistry()
        reg.register_search(Good())
        reg.register_search(Boom())
        cands, problems = reg.search_all(AssetRequest(keywords=["forest"]))
        self.assertEqual(len(cands), 1)
        self.assertEqual(len(problems), 2)
        self.assertFalse(any("secret-token" in p for p in problems))
        with self.assertRaises(Exception):
            reg.register_search(Good())          # duplicate
        with self.assertRaises(Exception):
            reg.register_search(object())        # not a provider

    def test_candidate_cannot_enter_library_without_a_real_file(self):
        r = candidate_to_record(self.cand(), "c1", "2026-11-08")
        with self.assertRaises(UnsafePathError):
            self.lib.add(r, self.tmp / "src" / "never-downloaded.mp4")


# ---------------------------------------------------------------------------------------------
# Security / correctness audit regressions
# ---------------------------------------------------------------------------------------------
class TestUserOwnedUnknownPermissions(Base):
    """Unknown (None) must never become permission, including for the user's own footage."""

    def _check(self, **flags):
        r = owned_verified(**flags)
        d = evaluate_rights(r)
        self.assertFalse(d.permitted, d.codes())
        return d

    def test_unknown_commercial(self):
        d = self._check(commercial_use_allowed=None)
        self.assertEqual(d.verdict, QUARANTINE)
        self.assertIn("COMMERCIAL_UNKNOWN", d.codes())
        self.assertNotIn("MODIFICATION_UNKNOWN", d.codes())

    def test_unknown_modification(self):
        d = self._check(modification_allowed=None)
        self.assertEqual(d.verdict, QUARANTINE)
        self.assertIn("MODIFICATION_UNKNOWN", d.codes())
        self.assertNotIn("COMMERCIAL_UNKNOWN", d.codes())

    def test_both_unknown(self):
        d = self._check(commercial_use_allowed=None, modification_allowed=None)
        self.assertEqual(d.verdict, QUARANTINE)
        self.assertTrue({"COMMERCIAL_UNKNOWN", "MODIFICATION_UNKNOWN"} <= set(d.codes()))

    def test_explicit_false_rejected(self):
        self.assertEqual(evaluate_rights(owned_verified(commercial_use_allowed=False)).verdict, REJECT)
        self.assertEqual(evaluate_rights(owned_verified(modification_allowed=False)).verdict, REJECT)

    def test_explicit_true_permitted_and_attribution_unknown_is_fine_for_own_work(self):
        d = evaluate_rights(owned_verified(attribution_required=None))
        self.assertTrue(d.permitted, d.codes())

    def test_noncommercial_context_only_needs_the_flag_it_uses(self):
        r = owned_verified(commercial_use_allowed=None)
        self.assertTrue(evaluate_rights(r, UseContext(commercial=False)).permitted)

    def test_library_never_approves_unknown_owner_permissions(self):
        for n, flags in enumerate(({"commercial_use_allowed": None}, {"modification_allowed": None},
                                   {"commercial_use_allowed": None, "modification_allowed": None})):
            lib = AssetLibrary(self.tmp / f"lib-{n}", clock=CLOCK)
            lib.add(rec(**flags), self.media(data=f"x{n}".encode()))
            lib.attest("a1", "Luka", "user_declaration", "I filmed it")
            self.assertEqual(lib.get("a1").state, State.QUARANTINED, flags)
            self.assertEqual(lib.search(), [])

    def test_truthy_non_bool_flags_do_not_grant_permission(self):
        r = owned_verified()
        for n, bad in enumerate(("yes", 1, "true", [1])):
            r.commercial_use_allowed = bad          # bypass the constructor entirely
            self.assertFalse(evaluate_rights(r).permitted, bad)
            with self.assertRaises(ManifestError):
                self.lib.add(rec(commercial_use_allowed=bad), self.media(data=f"b{n}".encode()))


class TestEvidenceAndAttestation(Base):
    def test_missing_evidence(self):
        for r in (owned_verified(evidence=[]), verified(cc(evidence=[]))):
            self.assertIn("NO_EVIDENCE", evaluate_rights(r).codes())
            self.assertFalse(evaluate_rights(r).permitted)

    def test_attestation_cannot_replace_document_evidence(self):
        decl = [Evidence("owner_declaration", "I say it is CC", strength="declaration")]
        d = evaluate_rights(verified(cc(evidence=decl)))
        self.assertIn("NO_DOCUMENT_EVIDENCE", d.codes())
        claim = [Evidence("provider_claim", "site claims CC", strength="claim")]
        self.assertIn("CLAIM_ONLY_EVIDENCE", evaluate_rights(verified(cc(evidence=claim))).codes())

    def test_user_owned_needs_owner_declaration_evidence(self):
        wrong = [Evidence("license_page", "a page", strength="document")]
        self.assertIn("NO_OWNER_EVIDENCE", evaluate_rights(owned_verified(evidence=wrong)).codes())
        weak = [Evidence("owner_declaration", "claimed", strength="claim")]
        self.assertFalse(evaluate_rights(owned_verified(evidence=weak)).permitted)

    def test_contradictory_evidence_blocks_even_when_attested(self):
        ev = [Evidence("license_page", "page shows NC", strength="document", license_name="CC-BY-NC-4.0")]
        d = evaluate_rights(verified(cc("CC-BY-4.0", evidence=ev)))
        self.assertIn("EVIDENCE_CONTRADICTS_LICENSE", d.codes())
        self.assertFalse(d.permitted)
        ev_ok = [Evidence("license_page", "page shows BY", strength="document", license_name="cc by 4.0")]
        self.assertTrue(evaluate_rights(verified(cc("CC-BY-4.0", evidence=ev_ok))).permitted)
        # evidence names a licence but the record names none
        self.assertFalse(evaluate_rights(verified(cc("", evidence=ev))).permitted)

    def test_contradictory_license_url(self):
        r = verified(cc("CC-BY-4.0", license_url="https://creativecommons.org/licenses/by-nc/4.0/"))
        self.assertIn("LICENSE_URL_MISMATCH", evaluate_rights(r).codes())
        r = verified(cc("CC-BY-4.0", license_url="https://creativecommons.org/licenses/by/4.0/"))
        self.assertTrue(evaluate_rights(r).permitted)
        # a look-alike host is not treated as a CC page
        r = verified(cc("CC-BY-4.0", license_url="https://creativecommons.org.evil.invalid/licenses/by-nc/4.0/"))
        self.assertNotIn("LICENSE_URL_MISMATCH", evaluate_rights(r).codes())

    def test_attestation_goes_stale_when_any_rights_field_changes_in_place(self):
        base = verified(cc())
        self.assertTrue(evaluate_rights(base).permitted)
        changes = {"license_name": "CC-BY-SA-4.0", "license_url": "https://example.invalid/l", "source": "other",
                   "source_url": "https://example.invalid/other", "creator": "Else", "attribution_text": "changed",
                   "commercial_use_allowed": False, "modification_allowed": False, "attribution_required": True,
                   "restrictions": ["no_redistribution"], "allowed_uses": ["editorial"],
                   "watermark_status": Watermark.UNCHECKED, "checksum_sha256": "b" * 64,
                   "rights_basis": RightsBasis.LICENSED_OTHER}
        for field, value in changes.items():
            r = copy.deepcopy(base)
            setattr(r, field, value)
            self.assertFalse(evaluate_rights(r).permitted, field)
            self.assertIn("ATTESTATION_STALE", evaluate_rights(r).codes(), field)
        r = copy.deepcopy(base)
        r.evidence.append(Evidence("other", "late addition", strength="document"))
        self.assertIn("ATTESTATION_STALE", evaluate_rights(r).codes())

    def test_attestation_method_must_fit_basis(self):
        d = evaluate_rights(verified(cc(), "user_declaration"))
        self.assertIn("ATTESTATION_METHOD_INVALID", d.codes())
        self.assertFalse(d.permitted)

    def test_verified_flag_without_a_matching_attestation_is_not_enough(self):
        r = cc()
        r.verification_status = Verification.VERIFIED          # forged flag, no attestation
        r.file_path, r.checksum_sha256 = "C:/x/y.mp4", "a" * 64
        self.assertFalse(evaluate_rights(r).permitted)
        from assets import Attestation
        r.attestations.append(Attestation("Someone", "human_review", "n", "2026-11-08", "c" * 64))  # wrong fingerprint
        self.assertIn("ATTESTATION_STALE", evaluate_rights(r).codes())

    def test_library_update_and_evidence_invalidate_approval(self):
        for n, change in enumerate(({"license_name": "CC-BY-SA-4.0"}, {"attribution_text": "different"},
                                    {"source_url": "https://example.invalid/new"})):
            lib = AssetLibrary(self.tmp / f"l{n}", clock=CLOCK)
            lib.add(cc(), self.media(data=f"d{n}".encode()))
            lib.add_evidence("a1", Evidence("license_page", "page", strength="document"))
            lib.attest("a1", "Luka", "human_review", "checked")
            self.assertEqual(lib.get("a1").state, State.APPROVED)
            lib.update_rights("a1", **change)
            self.assertEqual(lib.get("a1").verification_status, Verification.UNVERIFIED)
            self.assertNotEqual(lib.get("a1").state, State.APPROVED, change)
            self.assertEqual(len(lib.get("a1").attestations), 1)
        self.lib.add(cc(), self.media())
        self.lib.attest("a1", "Luka", "human_review", "checked")
        self.assertEqual(self.lib.get("a1").state, State.APPROVED)
        self.lib.add_evidence("a1", Evidence("other", "late evidence", strength="document"))
        self.assertNotEqual(self.lib.get("a1").state, State.APPROVED)   # new evidence needs a new review

    def test_evidence_from_caller_is_validated(self):
        self.lib.add(rec(), self.media())
        with self.assertRaises(ManifestError):
            self.lib.add_evidence("a1", Evidence("not-a-kind", "x"))
        with self.assertRaises(ManifestError):
            self.lib.add_evidence("a1", Evidence("other", "x", sha256="zz"))


class TestCreativeCommonsVariants(unittest.TestCase):
    # name -> (commercial, modification, attribution, share_alike)
    TABLE = {
        "CC-BY-4.0": (True, True, True, False),
        "CC-BY-SA-4.0": (True, True, True, True),
        "CC-BY-ND-4.0": (True, False, True, False),
        "CC-BY-NC-4.0": (False, True, True, False),
        "CC-BY-NC-SA-4.0": (False, True, True, True),
        "CC-BY-NC-ND-4.0": (False, False, True, False),
        "CC BY 3.0": (True, True, True, False),
        "cc by-nc-sa 2.5": (False, True, True, True),
        "CC-BY-ND-1.0": (True, False, True, False),
        "CC BY-NC-ND 2.0": (False, False, True, False),
        "Creative Commons BY-SA 4.0": (True, True, True, True),
        "CC-BY": (True, True, True, False),
        "CC0": (True, True, False, False),
        "CC0-1.0": (True, True, False, False),
        "PDM-1.0": (True, True, False, False),
    }

    def test_parse_table(self):
        for name, want in self.TABLE.items():
            t = parse_license(name)
            self.assertIsNotNone(t, name)
            self.assertEqual((t.commercial, t.modification, t.attribution, t.share_alike), want, name)

    def test_rejects_what_cc_never_published_or_that_is_ambiguous(self):
        for name in ("CC-BY-9.9", "CC-BY-4.1", "CC-BY-ND-SA-4.0", "CC-BY-SA-ND-4.0", "CC-BY-NC-NC-4.0", "CC-ND-4.0",
                     "CC-NC-4.0", "CC-SA-4.0", "CC", "CC-BY-4.0 and more", "xCC-BY-4.0", "CC-BY--4.0",
                     "CC BY-NC-ND-SA 4.0", "CC0-2.0", "PDM-2.0", "BY-4.0", "", "  ", "CC-BY-4.0-NC"):
            self.assertIsNone(parse_license(name), repr(name))

    def test_surrounding_whitespace_is_ignored_but_nothing_else(self):
        self.assertIsNotNone(parse_license("  CC-BY-4.0\n"))
        self.assertIsNone(parse_license("CC-BY-4.0\x00"))

    def test_verdict_for_each_variant_by_default_use(self):
        # default use = monetised + edited, no share-alike acceptance
        for name, want in {"CC-BY-4.0": PERMITTED, "CC-BY-SA-4.0": QUARANTINE, "CC-BY-ND-4.0": REJECT,
                           "CC-BY-NC-4.0": REJECT, "CC-BY-NC-SA-4.0": REJECT, "CC-BY-NC-ND-4.0": REJECT,
                           "CC0-1.0": PERMITTED}.items():
            self.assertEqual(evaluate_rights(verified(cc(name))).verdict, want, name)

    def test_noncommercial_unedited_use_matrix(self):
        ctx = UseContext(commercial=False, modification=False, accept_share_alike=True)
        for name in ("CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-ND-4.0", "CC-BY-NC-4.0", "CC-BY-NC-SA-4.0",
                     "CC-BY-NC-ND-4.0", "CC0-1.0"):
            self.assertTrue(evaluate_rights(verified(cc(name)), ctx).permitted, name)

    def test_attribution_licences_need_attribution_text_everywhere(self):
        ctx = UseContext(False, False, True)
        for name in ("CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-ND-4.0", "CC-BY-NC-4.0", "CC-BY-NC-SA-4.0", "CC-BY-NC-ND-4.0"):
            r = verified(cc(name, attribution_text=""))
            self.assertIn("ATTRIBUTION_TEXT_MISSING", evaluate_rights(r, ctx).codes(), name)

    def test_cc_name_on_other_bases_can_only_restrict(self):
        r = owned_verified(license_name="CC-BY-NC-4.0")      # NC name but commercial=True: contradictory
        d = evaluate_rights(r)
        self.assertIn("LICENSE_FIELD_MISMATCH", d.codes())
        self.assertFalse(d.permitted)
        r = owned_verified(license_name="CC-BY-4.0", commercial_use_allowed=None)   # name never supplies permission
        self.assertIn("COMMERCIAL_UNKNOWN", evaluate_rights(r).codes())
        r = verified(rec(rights_basis=RightsBasis.LICENSED_OTHER, license_name="CC-BY-4.0", source="s",
                         evidence=[Evidence("receipt", "r", strength="document")]))
        self.assertIn("BASIS_LICENSE_MISMATCH", evaluate_rights(r).codes())

    def test_public_domain_variants(self):
        ev = [Evidence("license_page", "dedication", strength="document")]
        for name in ("CC0-1.0", "PDM-1.0", "Public domain", ""):
            r = verified(rec(rights_basis=RightsBasis.PUBLIC_DOMAIN, license_name=name, source="archive", evidence=ev))
            self.assertTrue(evaluate_rights(r).permitted, name)
        for name in ("CC-BY-4.0", "Copyright 2020", "proprietary"):
            r = verified(rec(rights_basis=RightsBasis.PUBLIC_DOMAIN, license_name=name, source="archive", evidence=ev))
            self.assertFalse(evaluate_rights(r).permitted, name)
        r = verified(rec(rights_basis=RightsBasis.PUBLIC_DOMAIN, license_name="CC0-1.0", source="archive", evidence=ev,
                         commercial_use_allowed=False))
        self.assertEqual(evaluate_rights(r).verdict, REJECT)


class TestPathSafety(Base):
    def test_hostile_asset_ids(self):
        for bad in ("..\\evil", "../evil", "a\\b", "C:\\x", "c:evil", "a:b", "con", "nul", "aux", "prn", "com1", "lpt9",
                    "a.b", "a b", "a\x00b", ".hidden", "-x", "a/", "x" * 65, "ÄÖ", "con\n", "a1\n"):
            with self.assertRaises(UnsafePathError, msg=repr(bad)):
                self.lib.add(rec(bad), self.media())
        self.assertEqual(self.lib.all(), [])

    def test_hostile_source_strings_rejected_before_filesystem_access(self):
        for bad in ("\\\\server\\share\\x.mp4", "//server/share/x.mp4", "\\\\?\\C:\\x.mp4", "\\\\.\\pipe\\x",
                    "C:\\x.mp4:hidden", "C:x.mp4:stream", "clip.mp4:Zone.Identifier", "a\x00b.mp4", ""):
            with self.assertRaises(UnsafePathError, msg=repr(bad)):
                self.lib.add(rec(), bad)
        self.assertEqual(self.lib.all(), [])

    def test_traversal_in_source_does_not_escape_and_destination_is_inside_library(self):
        outside = self.tmp / "outside.mp4"
        outside.write_bytes(b"zzz")
        sneaky = self.tmp / "src" / ".." / "outside.mp4"
        self.lib.add(rec(), sneaky, mode="copy")                  # resolved to a real file
        r = self.lib.get("a1")
        self.assertEqual(r.file_path, "files/broll/a1.mp4")
        self.assertTrue(outside.exists())

    def test_junction_or_symlink_inside_library_not_written_through(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        (self.tmp / "lib" / "files").mkdir(parents=True)
        link = self.tmp / "lib" / "files" / "broll"
        made = False
        try:
            link.symlink_to(target, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError):
            if sys.platform == "win32":
                import subprocess
                made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                      capture_output=True).returncode == 0
        if not made:
            self.skipTest("cannot create a symlink or junction here")
        with self.assertRaises(UnsafePathError):
            self.lib.add(rec(), self.media(), mode="copy")
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.lib.all(), [])

    def test_manifest_file_paths(self):
        good = rec().to_dict()
        for fp, managed in [("../../x.mp4", True), ("files/../../x.mp4", True), ("/abs/x.mp4", True), ("C:\\x.mp4", True),
                            ("files\\broll\\x.mp4", True), ("other/x.mp4", True), ("files/broll/x.mp4:ads", True),
                            ("files//x.mp4", True), ("relative/x.mp4", False), ("C:x.mp4", False),
                            ("\\\\srv\\share\\x.mp4", False), ("//srv/share/x.mp4", False), ("C:\\a\\..\\b\\x.mp4", False),
                            ("C:\\a\\x.mp4:stream", False), ("", False), ("/a/../b", False)]:
            with self.assertRaises(ManifestError, msg=(fp, managed)):
                AssetRecord.from_dict({**good, "file_path": fp, "managed_copy": managed})
        for fp, managed in [("files/broll/a1.mp4", True), ("C:\\media\\x.mp4", False), ("/media/x.mp4", False), (None, False)]:
            AssetRecord.from_dict({**good, "file_path": fp, "managed_copy": managed})

    def test_tampered_index_with_bad_paths_is_refused(self):
        self.lib.add(rec(), self.media(), mode="copy")
        idx = self.tmp / "lib" / "index.json"
        data = json.loads(idx.read_text(encoding="utf-8"))
        data["assets"][0]["file_path"] = "../../../Windows/System32/x"
        idx.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ManifestError):
            AssetLibrary(self.tmp / "lib")


class TestNoBackDoorToApproval(Base):
    def test_cannot_smuggle_state_or_verification_through_add(self):
        from assets import Attestation
        r = cc(state=State.APPROVED, state_reason="manual: pre-approved", verification_status=Verification.VERIFIED)
        r.attestations.append(Attestation("Fake", "human_review", "n", "2026-11-08", "d" * 64))
        out = self.lib.add(r, self.media())
        self.assertEqual(out.state, State.QUARANTINED)
        self.assertEqual(out.verification_status, Verification.UNVERIFIED)
        self.assertEqual(out.attestations, [])
        self.assertTrue(any(h["event"] == "imported_attestations_discarded" for h in out.history))

    def test_rejected_and_quarantined_never_reach_approved_through_any_method(self):
        lib = self.lib
        lib.add(rec("w1", watermark_status=Watermark.WATERMARKED), self.media("w.mp4", b"w"))
        lib.add(rec("u1", rights_basis=RightsBasis.UNKNOWN, source="s"), self.media("u.mp4", b"u"))
        lib.add(cc("CC-BY-4.0", evidence=[Evidence("provider_claim", "claim", strength="claim")]), self.media("c.mp4", b"c"))
        for aid in ("w1", "u1", "a1"):
            lib.attest(aid, "Luka", "human_review", "looked fine")
            lib.reevaluate(aid)
            lib.release_hold(aid, "Luka", "nothing was wrong")
            lib.link_project(aid, "proj")
            self.assertNotEqual(lib.get(aid).state, State.APPROVED, aid)
        self.assertEqual(lib.search(), [])
        self.assertEqual(lib.search(states=[State.APPROVED], project="proj"), [])
        for aid in ("w1", "u1", "a1"):
            self.assertFalse(lib.cleared_for(aid, UseContext()))

    def test_no_public_approve_or_setter(self):
        names = {n for n in dir(self.lib) if not n.startswith("_")}
        self.assertFalse({"approve", "set_state", "force_approve", "delete", "remove"} & names)
        self.lib.add(rec(), self.media())
        with self.assertRaises(ValueError):
            self.lib.update_rights("a1", state="approved")

    def test_returned_records_are_copies(self):
        self.lib.add(rec(), self.media())
        got = self.lib.get("a1")
        got.state, got.verification_status = State.APPROVED, Verification.VERIFIED
        self.assertEqual(self.lib.get("a1").state, State.QUARANTINED)
        self.lib.all()[0].state = State.APPROVED
        self.assertEqual(self.lib.get("a1").state, State.QUARANTINED)
        self.assertEqual(self.lib.search(states=[State.APPROVED]), [])

    def test_tampered_index_cannot_grant_approval(self):
        self.lib.add(rec(), self.media())
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        idx = self.tmp / "lib" / "index.json"
        pristine = idx.read_text(encoding="utf-8")
        self.assertEqual(AssetLibrary(self.tmp / "lib").get("a1").state, State.APPROVED)
        for edit in ({"license_name": "CC-BY-NC-4.0"}, {"commercial_use_allowed": None}, {"watermark_status": "watermarked"},
                     {"creator": "someone else"}, {"checksum_sha256": "e" * 64}):
            data = json.loads(pristine)
            data["assets"][0].update(edit)
            idx.write_text(json.dumps(data), encoding="utf-8")
            self.assertNotEqual(AssetLibrary(self.tmp / "lib").get("a1").state, State.APPROVED, edit)

    def test_forged_approved_state_on_quarantined_record_is_downgraded(self):
        self.lib.add(rec("b1", rights_basis=RightsBasis.UNKNOWN, source="s"), self.media("b.mp4", b"b"))
        idx = self.tmp / "lib" / "index.json"
        data = json.loads(idx.read_text(encoding="utf-8"))
        data["assets"][0].update(state="approved", verification_status="verified", attestations=[])
        idx.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ManifestError):          # 'verified' with no attestations is itself invalid
            AssetLibrary(self.tmp / "lib")
        data["assets"][0].update(verification_status="unverified")
        idx.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(AssetLibrary(self.tmp / "lib").get("b1").state, State.QUARANTINED)

    def test_overwrite_attempts(self):
        src1, src2 = self.media("one.mp4", b"first"), self.media("two.mp4", b"second")
        self.lib.add(rec("a1", title="Original"), src1, mode="copy")
        managed = self.tmp / "lib" / "files" / "broll" / "a1.mp4"
        before = managed.read_bytes()
        with self.assertRaises(AssetExistsError):
            self.lib.add(rec("a1", title="Replacement"), src2, mode="copy")
        self.assertEqual(self.lib.get("a1").title, "Original")
        self.assertEqual(managed.read_bytes(), before)
        self.assertTrue(src1.exists() and src2.exists())
        self.lib.add(rec("r1", watermark_status=Watermark.WATERMARKED), self.media("r.mp4", b"r"))
        with self.assertRaises(AssetExistsError):          # a rejected asset's id cannot be reused
            self.lib.add(rec("r1"), self.media("r2.mp4", b"r2"))
        for field in ("asset_id", "checksum_sha256", "file_path", "state", "attestations", "history", "managed_copy"):
            with self.assertRaises(ValueError, msg=field):
                self.lib.update_rights("a1", **{field: "x"})

    def test_duplicate_keeps_provenance_and_cannot_launder_a_rejected_file(self):
        self.lib.add(rec("w1", watermark_status=Watermark.WATERMARKED, source="stock-preview"), self.media("w.mp4", b"same"))
        self.assertEqual(self.lib.get("w1").state, State.REJECTED)
        clean = cc("CC-BY-4.0", asset_id="w2", creator="Other", source="another-site", source_url="https://example.invalid/clean")
        with self.assertRaises(DuplicateAssetError) as cm:
            self.lib.add(clean, self.media("again.mp4", b"same"))
        self.assertEqual(cm.exception.existing_id, "w1")
        w1 = self.lib.get("w1")
        self.assertEqual((w1.state, w1.source, w1.verification_status), (State.REJECTED, "stock-preview", Verification.UNVERIFIED))
        self.assertEqual(len(self.lib.all()), 1)
        dup = [h for h in w1.history if h["event"] == "duplicate_submission"]
        self.assertEqual(len(dup), 1)
        self.assertEqual((dup[0]["offered_id"], dup[0]["source"]), ("w2", "another-site"))
        self.assertTrue(any(h["event"] == "duplicate_submission" for h in AssetLibrary(self.tmp / "lib").get("w1").history))

    def test_duplicate_does_not_disturb_an_approved_original(self):
        self.lib.add(rec("a1"), self.media("one.mp4", b"same"))
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        with self.assertRaises(DuplicateAssetError):
            self.lib.add(cc("CC-BY-4.0", asset_id="a2", creator="X"), self.media("two.mp4", b"same"))
        self.assertEqual(self.lib.get("a1").state, State.APPROVED)

    def test_provenance_preserved_across_state_changes(self):
        self.lib.add(rec(), self.media())
        self.lib.add_evidence("a1", Evidence("screenshot", "shot", strength="document"))
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.lib.update_rights("a1", creator="New Name")
        r = self.lib.get("a1")
        self.assertEqual((len(r.evidence), len(r.attestations)), (2, 1))
        self.assertEqual([h["event"] for h in r.history].count("rights_updated"), 1)
        self.assertEqual(r.history[0]["event"], "added")


class TestMalformedManifests(unittest.TestCase):
    def test_type_confusion_and_optional_fields(self):
        good = rec().to_dict()
        att = {"verifier": "v", "method": "human_review", "note": "", "at": "", "fingerprint": ""}
        bad_values = {
            "commercial_use_allowed": ["yes", 1, 0, "true", {}],
            "modification_allowed": ["yes", 1],
            "attribution_required": ["no", 0],
            "managed_copy": ["false", 1, None],
            "is_preview": ["no", 0, None],
            "tags": ["abc", {"a": 1}, [1, 2], [None], None, ["x"] * 51],
            "projects": ["abc", [1], ["../x"], None],
            "evidence": ["x", {}, [None], [[]], [{"kind": "other"}], None],
            "attestations": ["x", {}, [None], [{"verifier": "v"}], None, [{**att, "method": "automated"}],
                             [{**att, "fingerprint": "zz"}], [{**att, "x": 1}], [{**att, "verifier": 5}]],
            "technical": [[], "x", {"width": "1920"}, {"width": float("nan")}, {"fps": float("inf")}, {"height": True}, {"width": 0}],
            "quality": [[], {"visual_score": float("nan")}, {"audio_score": -0.1}, {"visual_score": True}, {"visual_score": "0.5"}],
            "history": ["x", [1], {}],
            "file_size": [-1, "5", True, 1.5],
            "acquisition_date": ["2026-13-45", 20261108, "08/11/2026"],
            "scope": ["project:", "Global", "project:A", "project:../x", 5],
            "state": ["approvedd", None, True],
            "category": [None, 5, "BROLL"],
            "rights_basis": [None, "public domain"],
            "verification_status": ["VERIFIED", None],
            "watermark_status": ["none", None],
            "title": [5, [], "a\x1bb"],
            "license_name": [5, ["CC-BY-4.0"]],
            "checksum_sha256": ["A" * 64, "a" * 63, 5, "a" * 64 + "\n"],
            "asset_id": [None, 5, "", "A", "x" * 65, "nul", "a1\n"],
            "schema_version": [None, 0, "1", True],
        }
        for key, values in bad_values.items():
            for v in values:
                with self.assertRaises(ManifestError, msg=(key, v)):
                    AssetRecord.from_dict({**good, key: v})

    def test_verified_flag_without_attestations_rejected(self):
        with self.assertRaises(ManifestError):
            AssetRecord.from_dict({**rec().to_dict(), "verification_status": "verified"})

    def test_extra_and_missing_keys(self):
        good = rec().to_dict()
        with self.assertRaises(ManifestError):
            AssetRecord.from_dict({**good, "__proto__": {}})
        for k in ("asset_id", "title", "category", "media_type", "schema_version"):
            d = dict(good)
            del d[k]
            with self.assertRaises(ManifestError, msg=k):
                AssetRecord.from_dict(d)

    def test_oversized_lists_and_text(self):
        good = rec().to_dict()
        with self.assertRaises(ManifestError):
            AssetRecord.from_dict({**good, "restrictions": ["r"] * 101})
        with self.assertRaises(ManifestError):
            AssetRecord.from_dict({**good, "notes": "n" * 5000})
        with self.assertRaises(ManifestError):
            AssetRecord.from_dict({**good, "evidence": [{"kind": "other", "description": "d"}] * 101})

    def test_strict_index_structure(self):
        with tempfile.TemporaryDirectory() as t:
            idx = Path(t) / "index.json"
            for content in ('[]', '{"schema_version": 2, "assets": []}', '{"schema_version": 1}',
                            '{"schema_version": 1, "assets": {}}', '{"schema_version": 1, "assets": [5]}', 'null', ''):
                idx.write_text(content, encoding="utf-8")
                with self.assertRaises(ManifestError, msg=content):
                    AssetLibrary(t)
                self.assertEqual(idx.read_text(encoding="utf-8"), content)   # never overwritten

    def test_duplicate_ids_or_checksums_in_index_refused(self):
        with tempfile.TemporaryDirectory() as t:
            a = rec("a1", checksum_sha256="a" * 64).to_dict()
            b = rec("a1", checksum_sha256="b" * 64).to_dict()
            c = rec("a2", checksum_sha256="a" * 64).to_dict()
            for assets in ([a, b], [a, c]):
                (Path(t) / "index.json").write_text(json.dumps({"schema_version": 1, "assets": assets}), encoding="utf-8")
                with self.assertRaises(ManifestError):
                    AssetLibrary(t)

    def test_constructor_built_garbage_is_caught_on_add(self):
        with tempfile.TemporaryDirectory() as t:
            lib = AssetLibrary(Path(t) / "lib")
            f = Path(t) / "f.mp4"
            f.write_bytes(b"1")
            for bad in (rec(rights_basis="user_owned"), rec(category="broll"), rec(tags="mushroom"),
                        rec(technical=[1]), rec(evidence=["x"]), rec(title=5)):
                with self.assertRaises(ManifestError):
                    lib.add(bad, f)
            self.assertEqual(lib.all(), [])


class TestFinalAuditRegressions(Base):
    def test_cleared_for_rechecks_the_file_and_quarantines_on_change(self):
        self.lib.add(rec(), self.media(), mode="copy")
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.assertTrue(self.lib.cleared_for("a1", UseContext()))
        (self.tmp / "lib" / self.lib.get("a1").file_path).write_bytes(b"tampered")
        self.assertFalse(self.lib.cleared_for("a1", UseContext()))
        self.assertEqual(self.lib.get("a1").state, State.QUARANTINED)
        self.assertEqual(AssetLibrary(self.tmp / "lib").get("a1").state, State.QUARANTINED)   # persisted

    def test_cleared_for_false_when_referenced_original_is_missing_and_original_untouched(self):
        src = self.media()
        self.lib.add(rec(), src)
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.assertTrue(self.lib.cleared_for("a1", UseContext()))
        src.write_bytes(b"changed elsewhere")
        self.assertFalse(self.lib.cleared_for("a1", UseContext()))
        self.assertEqual(src.read_bytes(), b"changed elsewhere")   # the library never "repairs" originals
        src.unlink()
        self.assertEqual(self.lib.verify_files()[0]["problem"], "file missing")

    def test_obligations_survive_acceptance(self):
        ctx = UseContext(accept_share_alike=True)
        self.lib.add(cc("CC-BY-SA-4.0"), self.media(), ctx=ctx)
        self.lib.add_evidence("a1", Evidence("license_page", "page", strength="document", license_name="CC-BY-SA-4.0"), ctx)
        self.lib.attest("a1", "Luka", "human_review", "checked", ctx)
        self.assertEqual(self.lib.get("a1").state, State.APPROVED)
        ob = self.lib.obligations("a1", ctx)
        self.assertTrue(any(o.startswith("share_alike:") for o in ob))
        self.assertTrue(any(o == "attribution: Photo by Someone, CC BY 4.0" for o in ob))
        self.assertFalse(self.lib.cleared_for("a1", UseContext()))   # a context that did not accept SA is refused
        self.assertTrue(self.lib.cleared_for("a1", ctx))

    def test_attribution_obligation_is_reported_for_permitted_cc_by(self):
        d = evaluate_rights(verified(cc("CC-BY-4.0")))
        self.assertTrue(d.permitted)
        self.assertEqual(d.obligations, ["attribution: Photo by Someone, CC BY 4.0"])
        self.assertEqual(evaluate_rights(verified(cc("CC0-1.0"))).obligations, [])

    def test_cc0_pdm_and_public_domain_statement_are_distinguished(self):
        ev = [Evidence("license_page", "page", strength="document")]
        kinds = {}
        for name in ("CC0-1.0", "PDM-1.0", "Public domain", "CC-BY-4.0"):
            basis = RightsBasis.PUBLIC_DOMAIN if name != "CC-BY-4.0" else RightsBasis.CREATIVE_COMMONS
            kinds[name] = evaluate_rights(verified(rec(rights_basis=basis, license_name=name, source="a", evidence=ev,
                                                       attribution_text="x"))).license_kind
        self.assertEqual(kinds, {"CC0-1.0": "cc0", "PDM-1.0": "pdm", "Public domain": "public_domain_statement",
                                 "CC-BY-4.0": "cc_license"})
        # a Public Domain Mark is not a Creative Commons licence
        d = evaluate_rights(verified(cc("PDM-1.0")))
        self.assertIn("BASIS_LICENSE_MISMATCH", d.codes())
        self.assertFalse(d.permitted)
        # CC0 is acceptable under either basis
        self.assertTrue(evaluate_rights(verified(cc("CC0-1.0"))).permitted)

    def test_public_domain_needs_document_evidence_and_attestation(self):
        base = dict(rights_basis=RightsBasis.PUBLIC_DOMAIN, license_name="Public domain", source="archive")
        self.assertFalse(evaluate_rights(verified(rec(evidence=[], **base))).permitted)
        decl = [Evidence("owner_declaration", "it is PD, trust me", strength="declaration")]
        self.assertIn("NO_DOCUMENT_EVIDENCE", evaluate_rights(verified(rec(evidence=decl, **base))).codes())
        doc = [Evidence("license_page", "archive statement", strength="document")]
        self.assertIn("UNVERIFIED", evaluate_rights(rec(evidence=doc, file_path="C:/x/a.mp4", checksum_sha256="a" * 64, **base)).codes())
        self.assertTrue(evaluate_rights(verified(rec(evidence=doc, **base))).permitted)

    def test_generated_requires_captured_provider_terms_even_if_attested(self):
        base = dict(rights_basis=RightsBasis.GENERATED, source="gen-tool", attribution_required=False,
                    category=Category.GENERATED)
        claim_only = [Evidence("provider_claim", "tool says commercial OK", strength="claim")]
        d = evaluate_rights(verified(rec(evidence=claim_only, **base)))
        self.assertFalse(d.permitted)
        self.assertIn("NO_PROVIDER_TERMS", d.codes())
        terms = [Evidence("provider_terms", "captured terms of service", strength="document")]
        self.assertTrue(evaluate_rights(verified(rec(evidence=terms, **base))).permitted)
        self.assertFalse(evaluate_rights(verified(rec(evidence=terms, commercial_use_allowed=None, **base))).permitted)


class TestHardeningDefensiveCopies(Base):
    """Finding 1: nothing a caller does to a returned object may reach the library's internal state."""

    @staticmethod
    def _forge(obj):
        from assets import Attestation
        from assets.rights import rights_fingerprint
        obj.state, obj.state_reason = State.APPROVED, ""
        obj.verification_status = Verification.VERIFIED
        obj.attestations.append(Attestation("Forger", "human_review", "n", "t", rights_fingerprint(obj)))
        obj.attestations.append(Attestation("Forger", "user_declaration", "n", "t", rights_fingerprint(obj)))
        obj.evidence.append(Evidence("license_page", "forged", strength="document"))
        obj.tags.append("forged")
        obj.history.clear()
        obj.technical["width"] = 1
        obj.commercial_use_allowed = obj.modification_allowed = True
        obj.checksum_sha256, obj.file_path = "f" * 64, "C:/forged.mp4"

    def test_every_record_returning_method_returns_a_copy(self):
        snapshot = lambda: json.dumps(self.lib.get("a1").to_dict(), sort_keys=True)
        steps = [
            ("add", lambda: self.lib.add(rec(), self.media())),
            ("get", lambda: self.lib.get("a1")),
            ("add_evidence", lambda: self.lib.add_evidence("a1", Evidence("screenshot", "s", strength="document"))),
            ("attest", lambda: self.lib.attest("a1", "Luka", "user_declaration", "mine")),
            ("update_rights", lambda: self.lib.update_rights("a1", creator="X")),
            ("dispute", lambda: self.lib.dispute("a1", "doubt")),
            ("link_project", lambda: self.lib.link_project("a1", "proj")),
            ("quarantine", lambda: self.lib.quarantine("a1", "hold")),
            ("reject", lambda: self.lib.reject("a1", "no")),
            ("release_hold", lambda: self.lib.release_hold("a1", "Luka", "ok")),
            ("all", lambda: self.lib.all()[0]),
            ("search", lambda: self.lib.search(states=list(State))[0]),
        ]
        for name, call in steps:
            out = call()
            before = snapshot()
            self._forge(out)
            self.assertEqual(snapshot(), before, name)
        r = self.lib.get("a1")
        self.assertNotEqual(r.state, State.APPROVED)       # nothing forged through a returned object stuck
        self.assertEqual(self.lib.search(), [])
        self.assertFalse(self.lib.cleared_for("a1", UseContext()))
        self.assertEqual([a.verifier for a in r.attestations], ["Luka"])

    def test_forged_copy_cannot_be_handed_back_to_approve(self):
        self.lib.add(rec(), self.media())
        forged = self.lib.get("a1")
        self._forge(forged)
        self.assertFalse(self.lib.cleared_for("a1", UseContext()))
        self.assertEqual(self.lib.search(states=[State.APPROVED]), [])
        self.assertEqual(AssetLibrary(self.tmp / "lib").get("a1").state, State.QUARANTINED)

    def test_decisions_evaluations_and_obligation_lists_are_copies(self):
        self.lib.add(cc(), self.media())
        self.lib.add_evidence("a1", Evidence("license_page", "p", strength="document"))
        self.lib.attest("a1", "Luka", "human_review", "checked")
        d = self.lib.reevaluate("a1")
        d.verdict = "permitted"
        d.reasons.clear()
        d.obligations.append("forged")
        self.assertEqual(self.lib.reevaluate("a1").obligations, ["attribution: Photo by Someone, CC BY 4.0"])
        ob = self.lib.obligations("a1", UseContext())
        ob.clear()
        self.assertEqual(len(self.lib.obligations("a1", UseContext())), 1)
        self.lib.quarantine("a1", "hold")
        ev = self.lib.evaluate("a1")
        ev.verdict = "usable"
        ev.reasons.clear()
        self.assertEqual(self.lib.evaluate("a1").verdict, "quarantine")


class TestHardeningHoldsPersist(Base):
    """Finding 2: only an explicit release_hold() lifts a manual quarantine/rejection."""

    def approved(self, aid="a1", data=b"x"):
        self.lib.add(rec(aid), self.media(f"{aid}.mp4", data))
        self.lib.attest(aid, "Luka", "user_declaration", "mine")
        self.assertEqual(self.lib.get(aid).state, State.APPROVED)

    def held(self, label, state=State.QUARANTINED):
        r = self.lib.get("a1")
        self.assertEqual(r.state, state, label)
        self.assertTrue(r.state_reason.startswith("manual:"), (label, r.state_reason))
        self.assertFalse(self.lib.cleared_for("a1", UseContext()), label)
        self.assertEqual(self.lib.search(), [], label)

    def test_manual_quarantine_survives_everything_until_released(self):
        self.approved()
        self.lib.quarantine("a1", "suspicious")
        self.held("quarantine")
        self.lib.reevaluate("a1")
        self.held("reevaluate")
        self.lib.update_rights("a1", creator="Luka K")
        self.held("update_rights")
        self.lib.attest("a1", "Luka", "user_declaration", "mine again")
        self.held("attest")
        self.lib.add_evidence("a1", Evidence("screenshot", "s", strength="document"))
        self.held("add_evidence")
        self.lib.attest("a1", "Luka", "user_declaration", "again")
        self.held("attest 2")
        self.lib.dispute("a1", "x")
        self.held("dispute")
        self.lib.attest("a1", "Luka", "user_declaration", "after dispute")
        self.held("attest 3")
        self.lib.link_project("a1", "p")
        self.assertEqual(self.lib.verify_files(), [])
        self.held("verify_files")
        self.assertEqual(AssetLibrary(self.tmp / "lib").get("a1").state, State.QUARANTINED)   # persisted
        self.assertTrue(AssetLibrary(self.tmp / "lib").get("a1").state_reason.startswith("manual:"))
        self.assertEqual(self.lib.release_hold("a1", "Luka", "reviewed").state, State.APPROVED)

    def test_manual_reject_is_never_downgraded_or_lifted(self):
        self.approved()
        self.lib.reject("a1", "bad content")
        self.held("reject", State.REJECTED)
        steps = [("reevaluate", lambda: self.lib.reevaluate("a1")),
                 ("update_rights", lambda: self.lib.update_rights("a1", creator="n")),
                 ("attest", lambda: self.lib.attest("a1", "Luka", "user_declaration", "mine")),
                 ("add_evidence", lambda: self.lib.add_evidence("a1", Evidence("screenshot", "s", strength="document"))),
                 ("dispute", lambda: self.lib.dispute("a1", "x")),
                 ("verify_files", lambda: self.lib.verify_files()),
                 ("quarantine", lambda: self.lib.quarantine("a1", "softer"))]
        for label, call in steps:
            call()
            self.held(label, State.REJECTED)
        self.assertEqual(AssetLibrary(self.tmp / "lib").get("a1").state, State.REJECTED)

    def test_quarantine_never_softens_a_rights_rejection(self):
        self.lib.add(rec(watermark_status=Watermark.WATERMARKED), self.media())
        self.assertEqual(self.lib.get("a1").state, State.REJECTED)
        self.lib.quarantine("a1", "also held")
        self.assertEqual(self.lib.get("a1").state, State.REJECTED)

    def test_a_hold_can_only_get_stricter_through_evaluation(self):
        self.approved()
        self.lib.quarantine("a1", "hold")
        self.lib.update_rights("a1", commercial_use_allowed=False)       # rights now reject
        r = self.lib.get("a1")
        self.assertEqual(r.state, State.REJECTED)
        self.assertTrue(r.state_reason.startswith("manual:"))
        self.lib.release_hold("a1", "Luka", "reviewed")
        self.assertEqual(self.lib.get("a1").state, State.REJECTED)       # released, but the rights still reject

    def test_integrity_hold_on_a_rights_quarantined_asset_cannot_be_attested_away(self):
        src = self.media(data=b"original-bytes")
        self.lib.add(rec(), src, mode="copy")                              # quarantined by rights (unattested)
        managed = self.tmp / "lib" / self.lib.get("a1").file_path
        managed.write_bytes(b"tampered")
        self.assertEqual(self.lib.verify_files()[0]["problem"], "checksum changed")
        managed.write_bytes(b"original-bytes")                             # file restored
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.assertEqual(self.lib.get("a1").state, State.QUARANTINED)      # the hold survives attestation
        self.assertEqual(self.lib.release_hold("a1", "Luka", "file restored").state, State.APPROVED)

    def test_forged_manual_prefix_on_an_approved_record_loads_as_quarantined(self):
        self.approved()
        idx = self.tmp / "lib" / "index.json"
        data = json.loads(idx.read_text(encoding="utf-8"))
        data["assets"][0]["state_reason"] = "manual: forged hold"
        idx.write_text(json.dumps(data), encoding="utf-8")
        r = AssetLibrary(self.tmp / "lib").get("a1")
        self.assertEqual(r.state, State.QUARANTINED)
        self.assertTrue(r.state_reason.startswith("manual:"))


class TestHardeningReleaseHoldIntegrity(Base):
    """Finding 3: release_hold() verifies the file and path before it can approve anything."""

    def setUp(self):
        super().setUp()
        self.src = self.media(data=b"original-bytes")

    def make_held(self, mode="copy"):
        self.lib.add(rec(), self.src, mode=mode)
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.lib.quarantine("a1", "hold")

    def managed(self):
        return self.tmp / "lib" / self.lib.get("a1").file_path

    def assert_refused(self, fragment):
        r = self.lib.release_hold("a1", "Luka", "please")
        self.assertEqual(r.state, State.QUARANTINED)
        self.assertTrue(r.state_reason.startswith("manual:"), r.state_reason)
        self.assertIn(fragment, r.state_reason)
        self.assertTrue(any(h["event"] == "hold_release_refused" for h in r.history))
        self.assertFalse(self.lib.cleared_for("a1", UseContext()))
        self.assertEqual(self.lib.search(), [])
        self.assertEqual(AssetLibrary(self.tmp / "lib").get("a1").state, State.QUARANTINED)   # persisted

    def test_changed_managed_copy(self):
        self.make_held()
        self.managed().write_bytes(b"tampered")
        self.assert_refused("checksum changed")
        self.managed().write_bytes(b"original-bytes")
        self.assertEqual(self.lib.release_hold("a1", "Luka", "restored").state, State.APPROVED)

    def test_missing_managed_copy(self):
        self.make_held()
        self.managed().unlink()                               # our own test copy, never an original
        self.assert_refused("file missing")

    def test_changed_and_missing_referenced_original(self):
        self.make_held("reference")
        self.src.write_bytes(b"changed")
        self.assert_refused("checksum changed")
        self.assertEqual(self.src.read_bytes(), b"changed")   # never "repaired"
        self.src.unlink()
        self.assert_refused("file missing")

    def test_untouched_file_releases_normally(self):
        self.make_held()
        self.assertEqual(self.lib.release_hold("a1", "Luka", "ok").state, State.APPROVED)
        self.assertTrue(self.lib.cleared_for("a1", UseContext()))

    def test_link_inside_the_library_blocks_release(self):
        self.make_held()
        broll = self.tmp / "lib" / "files" / "broll"
        moved = self.tmp / "moved-broll"
        os.rename(broll, moved)
        if not self.dir_link(broll, moved):
            os.rename(moved, broll)
            self.skipTest("cannot create a symlink or junction here")
        self.assert_refused("symlink/junction")

    def test_link_in_a_parent_of_a_referenced_original_blocks_release(self):
        real = self.tmp / "media-real"
        real.mkdir()
        f = real / "clip.mp4"
        f.write_bytes(b"ref-bytes")
        self.lib.add(rec(), f)
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.lib.quarantine("a1", "hold")
        moved = self.tmp / "media-moved"
        os.rename(real, moved)
        if not self.dir_link(real, moved):
            os.rename(moved, real)
            self.skipTest("cannot create a symlink or junction here")
        self.assert_refused("symlink/junction")

    def test_failed_release_never_approves_even_if_rights_pass(self):
        self.make_held()
        self.managed().write_bytes(b"tampered")
        for _ in range(3):
            self.assertNotEqual(self.lib.release_hold("a1", "Luka", "again").state, State.APPROVED)


class TestHardeningStoredStateInEvaluate(Base):
    """Finding 4: evaluate() never reports a stored QUARANTINED / REJECTED record as usable."""
    REQ = AssetRequest(keywords=["mushroom"])

    def codes(self, ev):
        return {r.code for r in ev.reasons}

    def test_quarantined_record_is_not_usable(self):
        self.lib.add(rec(), self.media())
        ev = self.lib.evaluate("a1", self.REQ)
        self.assertEqual(ev.verdict, "quarantine")
        self.assertFalse(ev.usable)
        self.assertIn("STORED_STATE_QUARANTINED", self.codes(ev))

    def test_approved_record_is_usable_until_a_hold_is_placed(self):
        self.lib.add(rec(), self.media())
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        self.assertTrue(self.lib.evaluate("a1", self.REQ).usable)
        self.lib.quarantine("a1", "hold")        # rights alone would still permit it
        ev = self.lib.evaluate("a1", self.REQ)
        self.assertEqual((ev.verdict, ev.usable), ("quarantine", False))
        self.lib.reject("a1", "no")
        ev = self.lib.evaluate("a1", self.REQ)
        self.assertEqual((ev.verdict, ev.usable), ("reject", False))
        self.assertIn("STORED_STATE_REJECTED", self.codes(ev))

    def test_rights_rejected_record_stays_reject(self):
        self.lib.add(rec(watermark_status=Watermark.WATERMARKED), self.media())
        self.assertEqual(self.lib.evaluate("a1", self.REQ).verdict, "reject")

    def test_module_level_evaluate_also_respects_state(self):
        from assets import evaluate
        r = owned_verified()                                  # rights would permit; state defaults to QUARANTINED
        self.assertTrue(evaluate_rights(r).permitted)
        self.assertEqual(evaluate(r, self.REQ).verdict, "quarantine")
        r.state = State.REJECTED
        self.assertEqual(evaluate(r, self.REQ).verdict, "reject")
        r.state = State.APPROVED
        self.assertEqual(evaluate(r, self.REQ).verdict, "usable")

    def test_changed_file_is_not_reported_usable_and_evaluate_does_not_mutate(self):
        self.lib.add(rec(), self.media(), mode="copy")
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        (self.tmp / "lib" / self.lib.get("a1").file_path).write_bytes(b"tampered")
        ev = self.lib.evaluate("a1", self.REQ)
        self.assertEqual((ev.verdict, ev.usable), ("quarantine", False))
        self.assertIn("FILE_INTEGRITY", self.codes(ev))
        self.assertEqual(self.lib.get("a1").state, State.APPROVED)   # evaluate is read-only


class TestHardeningParentDirectoryLinks(Base):
    """Finding 5: every parent directory of a source is inspected, not just the file itself."""

    def test_source_below_a_linked_directory_is_refused(self):
        real = self.tmp / "real"
        (real / "sub").mkdir(parents=True)
        (real / "sub" / "clip.mp4").write_bytes(b"data")
        link = self.tmp / "linkdir"
        if not self.dir_link(link, real):
            self.skipTest("cannot create a symlink or junction here")
        for p in (link / "sub" / "clip.mp4", link / "sub" / ".." / "sub" / "clip.mp4"):
            with self.assertRaises(UnsafePathError, msg=str(p)):
                self.lib.add(rec(), p)
            with self.assertRaises(UnsafePathError, msg=str(p)):
                self.lib.add(rec(), p, mode="copy")
        self.assertEqual(self.lib.all(), [])
        self.assertFalse((self.tmp / "lib" / "files").exists())
        self.lib.add(rec(), real / "sub" / "clip.mp4")        # the real path is fine

    def test_link_deeper_in_the_chain_is_found(self):
        real = self.tmp / "real2"
        real.mkdir()
        (real / "clip.mp4").write_bytes(b"data2")
        outer = self.tmp / "outer"
        outer.mkdir()
        if not self.dir_link(outer / "inner", real):
            self.skipTest("cannot create a symlink or junction here")
        with self.assertRaises(UnsafePathError):
            self.lib.add(rec(), outer / "inner" / "clip.mp4")

    def test_link_in_chain_helper(self):
        real = self.tmp / "real3"
        real.mkdir()
        self.assertIsNone(AssetLibrary._link_in_chain(real))
        link = self.tmp / "link3"
        if not self.dir_link(link, real):
            self.skipTest("cannot create a symlink or junction here")
        self.assertEqual(AssetLibrary._link_in_chain(link / "x" / "y.mp4"), link)


class TestHardeningTechnicalUnknown(Base):
    """Finding 6: unknown technical compatibility is unknown, never compatible."""

    def test_no_applicable_requirement_is_unknown(self):
        from assets import technical_compat
        video = rec(technical={"width": 1920, "height": 1080, "duration_s": 5})
        self.assertEqual(technical_compat(video, TargetSpec()).status, "unknown")
        self.assertEqual(technical_compat(video, TargetSpec(min_sample_rate=44100)).status, "unknown")
        audio = rec(category=Category.MUSIC, media_type=MediaType.AUDIO, technical={"sample_rate": 48000})
        self.assertEqual(technical_compat(audio, TargetSpec(min_width=1920)).status, "unknown")
        self.assertEqual(technical_compat(rec(media_type=MediaType.IMAGE, technical={"width": 9}),
                                          TargetSpec(min_duration_s=3)).status, "unknown")

    def test_unrecorded_values_are_unknown_not_compatible(self):
        from assets import technical_compat
        self.assertEqual(technical_compat(rec(), TargetSpec(min_width=1)).status, "unknown")
        self.assertEqual(technical_compat(rec(technical={"width": 1920}),
                                          TargetSpec(min_width=1920, min_height=1080)).status, "unknown")
        self.assertEqual(technical_compat(rec(), TargetSpec(allowed_extensions=(".mp4",))).status, "unknown")
        self.assertEqual(technical_compat(rec(), None).status, "unknown")

    def test_checked_and_passing_is_compatible_and_failures_stay_incompatible(self):
        from assets import technical_compat
        ok = rec(technical={"width": 1920, "height": 1080}, file_path="C:/x/a.mp4")
        self.assertEqual(technical_compat(ok, TargetSpec(min_width=1920, min_height=1080,
                                                         allowed_extensions=(".mp4",))).status, "compatible")
        small = rec(technical={"width": 640})
        self.assertEqual(technical_compat(small, TargetSpec(min_width=1920)).status, "incompatible")
        mixed = rec(technical={"width": 640})
        self.assertEqual(technical_compat(mixed, TargetSpec(min_width=1920, min_height=1080)).status, "incompatible")

    def test_unknown_technical_is_not_reported_usable_when_a_spec_was_requested(self):
        self.lib.add(rec(technical={"width": 1920}), self.media())
        self.lib.attest("a1", "Luka", "user_declaration", "mine")
        req = AssetRequest(keywords=["mushroom"])
        ev = self.lib.evaluate("a1", req, TargetSpec(min_width=1920, min_height=1080))
        self.assertEqual((ev.technical.status, ev.verdict, ev.usable), ("unknown", "review", False))
        ok = self.lib.evaluate("a1", req, TargetSpec(min_width=1920))
        self.assertEqual((ok.technical.status, ok.verdict), ("compatible", "usable"))
        self.assertTrue(self.lib.evaluate("a1", req).usable)          # no spec requested: nothing to be unknown about


class TestProviderTrust(Base):
    def test_candidate_with_everything_claimed_is_still_never_permitted(self):
        c = AssetCandidate(provider="p", external_id="1", title="Clip", category=Category.BROLL, media_type=MediaType.VIDEO,
                           source_url="https://example.invalid/1", claimed_license_name="CC0-1.0",
                           claimed_commercial_use=True, claimed_modification=True, claims_watermarked=False)
        d = evaluate_rights(candidate_to_record(c, "c1", "2026-11-08"))
        self.assertFalse(d.permitted)
        self.assertTrue({"NO_FILE", "UNVERIFIED", "CLAIM_ONLY_EVIDENCE"} <= set(d.codes()))


if __name__ == "__main__":
    unittest.main()
