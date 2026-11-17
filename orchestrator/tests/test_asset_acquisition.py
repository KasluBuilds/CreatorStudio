"""Asset acquisition tests (T-AST-003). Offline, synthetic tiny files only, no real media, no network.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_asset_acquisition.py -v

The Openverse fixtures are authored from the public API documentation, NOT recorded from the live service;
nothing here is evidence that the adapter works against the real API.
"""
import hashlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assets import (AcquisitionContext, AcquisitionPolicy, AssetCandidate, AssetLibrary, AssetRequest, Category,
                    FetchError, LocalFolderProvider, ManifestError, MediaType, OpenverseProvider, ProviderRegistry,
                    QuarantineLedger, State, TransportRequest, TransportResponse, UseContext, Verification, Watermark,
                    acquire, find_and_acquire, reverify_license, scrub)
from assets import acquisition as acq
from assets import openverse as ov
from assets.acquisition import (check_url, classify, content_type_problem, fetch_to, make_asset_id, sniff_family)
from assets.local_provider import SIDECAR_SUFFIX, validate_sidecar

CLOCK = lambda: "2026-11-17T00:00:00+00:00"
TODAY = "2026-11-17"
CDN = "cdn.example.test"
CC_URL = "https://creativecommons.org/licenses/by/4.0/"
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-body" * 4
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-png-body" * 4
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"synthetic-mp3" * 4
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16
LICENSE_TEXT = b"<html>Creative Commons Attribution 4.0 legal text (synthetic)</html>"
SECRET_BIT = "tok" + "en=hunter2hunter2hunter2"


class FakeTransport:
    """Serves canned responses by URL and records every request. Never touches a network."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests: list[TransportRequest] = []

    def add(self, url, body=b"", content_type="image/jpeg", status=200, length="auto", final_url="", chunk=7, raises=None,
            midstream=None):
        self.routes[url] = dict(body=body, content_type=content_type, status=status, length=length,
                                final_url=final_url, chunk=chunk, raises=raises, midstream=midstream)

    def fetch(self, request):
        self.requests.append(request)
        r = self.routes.get(request.url)
        if r is None:
            raise OSError("no route for this URL in the fake transport")
        if r["raises"]:
            raise r["raises"]
        body = r["body"]
        length = len(body) if r["length"] == "auto" else r["length"]

        def gen():
            for i in range(0, len(body), r["chunk"]):
                yield body[i:i + r["chunk"]]
            if r["midstream"]:
                raise r["midstream"]
        return TransportResponse(r["status"], r["content_type"], length, r["final_url"], gen())

    @property
    def urls(self):
        return [q.url for q in self.requests]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def cc_cand(ext="i1", **kw) -> AssetCandidate:
    base = dict(provider="openverse", external_id=ext, title="Mushroom macro photo", category=Category.IMAGE,
                media_type=MediaType.IMAGE, source_url=f"https://example.test/photo/{ext}", creator="A Photographer",
                tags=["mushroom", "macro"], claimed_license_name="CC-BY-4.0", claimed_license_url=CC_URL,
                claimed_attribution_text="Mushroom macro photo by A Photographer, CC BY 4.0",
                download_url=f"https://{CDN}/{ext}.jpg", license_text_url=CC_URL)
    base.update(kw)
    return AssetCandidate(**base)


def make_policy(**kw) -> AcquisitionPolicy:
    base = dict(provider_hosts={"openverse": frozenset({CDN}), "other": frozenset({CDN})}, max_bytes=10_000)
    base.update(kw)
    return AcquisitionPolicy(**base)


def ctx_for(policy=None, **kw) -> AcquisitionContext:
    return AcquisitionContext(policy or make_policy(), today=TODAY, **kw)


def served(ext="i1", body=JPEG, **kw) -> FakeTransport:
    t = FakeTransport()
    t.add(CC_URL, LICENSE_TEXT, "text/html; charset=utf-8")
    t.add(f"https://{CDN}/{ext}.jpg", body, kw.pop("content_type", "image/jpeg"), **kw)
    return t


def tree_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class LibCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "lib"
        self.lib = AssetLibrary(self.root, clock=CLOCK)

    def run_one(self, cand, transport, **kw):
        return acquire(cand, self.lib, transport, ctx_for(**kw), QuarantineLedger(self.root))

    def assert_clean(self):
        """No staging leftovers, no managed files, no evidence snapshots, nothing indexed."""
        staging = self.root / acq.STAGING
        self.assertEqual([], [p.name for p in staging.iterdir()] if staging.exists() else [])
        self.assertEqual([], [f for f in tree_files(self.root) if f.startswith(("files/", "evidence/", ".staging/"))])
        self.assertEqual([], self.lib.all())


# ======================================================================================================
class TestScrub(unittest.TestCase):
    def test_key_value_pairs_removed(self):
        self.assertNotIn("hunter2", scrub("failed: " + SECRET_BIT))

    def test_bearer_removed(self):
        out = scrub("Authorization: Bearer abcdef0123456789abcdef")
        self.assertNotIn("abcdef0123456789abcdef", out)

    def test_url_userinfo_and_query_removed(self):
        out = scrub("GET https://user:passw0rd@host.example/path?sig=abc123&x=1 failed")
        self.assertNotIn("passw0rd", out)
        self.assertNotIn("abc123", out)
        self.assertIn("host.example/path", out)

    def test_prefixed_keys_removed(self):
        for s in ("sk" + "-" + "a" * 24, "gh" + "p_" + "b" * 24, "AK" + "IA" + "C" * 16):
            self.assertNotIn(s, scrub(f"leaked {s} here"))

    def test_long_opaque_string_removed(self):
        self.assertNotIn("Z" * 40, scrub("value " + "Z" * 40))

    def test_plain_message_kept_and_capped(self):
        self.assertEqual("connection refused", scrub("connection refused"))
        self.assertLessEqual(len(scrub("x " * 500)), 300)

    def test_control_characters_flattened(self):
        self.assertNotIn("\n", scrub("line1\nline2\x00"))


class TestSniffAndUrl(unittest.TestCase):
    def test_sniff_known_families(self):
        for data, fam in ((JPEG, "jpeg"), (PNG, "png"), (WAV, "wav"), (MP3, "mp3"), (MP4, "isobmff"),
                          (b"GIF89a" + b"\0" * 8, "gif"), (b"OggS" + b"\0" * 8, "ogg"), (b"fLaC" + b"\0" * 8, "flac"),
                          (b"RIFF\0\0\0\0WEBPVP8 ", "webp"), (b"\x1a\x45\xdf\xa3" + b"\0" * 8, "matroska")):
            self.assertEqual(fam, sniff_family(data))
        self.assertIsNone(sniff_family(b"<html>not media</html>"))

    def test_content_type_and_signature_must_agree(self):
        p = make_policy()
        self.assertIsNone(content_type_problem("image/jpeg", MediaType.IMAGE, p, JPEG))
        self.assertEqual("CONTENT_MISMATCH", content_type_problem("image/jpeg", MediaType.IMAGE, p, PNG).code)
        self.assertEqual("CONTENT_TYPE_NOT_ALLOWED", content_type_problem("text/html", MediaType.IMAGE, p, JPEG).code)
        self.assertEqual("CONTENT_TYPE_NOT_ALLOWED", content_type_problem("", MediaType.IMAGE, p, JPEG).code)
        self.assertEqual("CONTENT_TYPE_NOT_ALLOWED", content_type_problem("image/svg+xml", MediaType.IMAGE, p, JPEG).code)

    def test_content_type_for_wrong_media_type_refused(self):
        self.assertEqual("CONTENT_TYPE_NOT_ALLOWED", content_type_problem("audio/mpeg", MediaType.IMAGE, make_policy(), MP3).code)

    def test_http_url_refused(self):
        with self.assertRaises(FetchError) as cm:
            check_url("http://cdn.example.test/a.jpg", [CDN])
        self.assertEqual("URL_NOT_ALLOWED", cm.exception.code)

    def test_credentials_in_url_refused(self):
        with self.assertRaises(FetchError):
            check_url("https://user:pw@cdn.example.test/a.jpg", [CDN])

    def test_odd_port_and_non_https_schemes_refused(self):
        for u in ("https://cdn.example.test:8443/a.jpg", "file:///etc/passwd", "ftp://cdn.example.test/a", "//cdn.example.test/a"):
            with self.assertRaises(FetchError):
                check_url(u, [CDN])

    def test_host_must_be_allow_listed_exactly(self):
        for u in ("https://evil.example.test/a.jpg", "https://cdn.example.test.evil.test/a.jpg", "https://xcdn.example.test/a"):
            with self.assertRaises(FetchError) as cm:
                check_url(u, [CDN])
            self.assertEqual("HOST_NOT_ALLOWED", cm.exception.code)

    def test_host_comparison_ignores_case(self):
        self.assertEqual(CDN, check_url("https://CDN.Example.Test/a.jpg", [CDN]))


class TestFetchTo(unittest.TestCase):
    def go(self, t, url=f"https://{CDN}/i1.jpg", max_bytes=10_000):
        sink = io.BytesIO()
        res = fetch_to(t, url, hosts=[CDN], max_bytes=max_bytes, timeout_s=5, accept=("image/jpeg",), sink=sink)
        return res, sink.getvalue()

    def err(self, t, **kw):
        with self.assertRaises(FetchError) as cm:
            self.go(t, **kw)
        return cm.exception

    def test_success_hashes_and_passes_limits_to_transport(self):
        t = served()
        res, data = self.go(t)
        self.assertEqual((sha(JPEG), len(JPEG), "image/jpeg", JPEG[:32]), (res.sha256, res.size, res.content_type, res.head))
        self.assertEqual(JPEG, data)
        self.assertEqual((5.0, 10_000, ("image/jpeg",)), (t.requests[0].timeout_s, t.requests[0].max_bytes, t.requests[0].accept))

    def test_transport_exception_is_contained_and_scrubbed(self):
        t = FakeTransport()
        t.add("https://cdn.example.test/i1.jpg", raises=ConnectionError("refused " + SECRET_BIT))
        e = self.err(t)
        self.assertEqual("TRANSPORT_ERROR", e.code)
        self.assertNotIn("hunter2", str(e))

    def test_non_200_status(self):
        self.assertEqual("HTTP_STATUS", self.err(served(status=404)).code)

    def test_declared_length_over_limit_refused_before_reading(self):
        t = served(length=99_999)
        self.assertEqual("TOO_LARGE", self.err(t).code)

    def test_streamed_oversize_without_declared_length(self):
        t = served(body=JPEG + b"x" * 500, length=None)
        self.assertEqual("TOO_LARGE", self.err(t, max_bytes=100).code)

    def test_truncated_download(self):
        t = served(length=len(JPEG) + 50)
        self.assertEqual("TRUNCATED", self.err(t).code)

    def test_more_bytes_than_declared(self):
        t = served(length=len(JPEG) - 5)
        self.assertEqual("LENGTH_MISMATCH", self.err(t).code)

    def test_empty_body(self):
        self.assertEqual("EMPTY", self.err(served(body=b"")).code)

    def test_stream_dies_midway(self):
        t = served(midstream=ConnectionResetError("reset by peer"))
        self.assertEqual("TRANSPORT_ERROR", self.err(t).code)

    def test_redirect_to_other_host_refused(self):
        t = served(final_url="https://evil.example.test/i1.jpg")
        self.assertEqual("HOST_NOT_ALLOWED", self.err(t).code)

    def test_redirect_to_plain_http_refused(self):
        t = served(final_url="http://cdn.example.test/i1.jpg")
        self.assertEqual("REDIRECT_NOT_ALLOWED", self.err(t).code)

    def test_non_bytes_chunk_refused(self):
        class T:
            def fetch(self, req):
                return TransportResponse(200, "image/jpeg", None, "", iter(["text"]))
        self.assertEqual("TRANSPORT_ERROR", self.err(T()).code)

    def test_invalid_response_object_refused(self):
        class T:
            def fetch(self, req):
                return "not a response"
        self.assertEqual("TRANSPORT_ERROR", self.err(T()).code)

    def test_bad_max_bytes_policy_refused(self):
        self.assertEqual("BAD_POLICY", self.err(served(), max_bytes=0).code)


# ======================================================================================================
class TestClassify(unittest.TestCase):
    use = UseContext()

    def test_clean_cc_by_candidate_is_acquirable(self):
        self.assertEqual("acquire", classify(cc_cand(), self.use, TODAY).outcome)

    def test_unknown_rights_never_acquirable(self):
        c = cc_cand(claimed_license_name="", claimed_license_url="")
        r = classify(c, self.use, TODAY)
        self.assertEqual("quarantine", r.outcome)
        self.assertIn("UNKNOWN_RIGHTS", r.reasons)

    def test_non_commercial_licence_rejected(self):
        r = classify(cc_cand(claimed_license_name="CC-BY-NC-4.0", claimed_license_url="https://creativecommons.org/licenses/by-nc/4.0/"), self.use, TODAY)
        self.assertEqual("reject", r.outcome)
        self.assertIn("NO_COMMERCIAL", r.reasons)

    def test_no_derivatives_licence_rejected_for_editing(self):
        r = classify(cc_cand(claimed_license_name="CC-BY-ND-4.0", claimed_license_url="https://creativecommons.org/licenses/by-nd/4.0/"), self.use, TODAY)
        self.assertEqual("reject", r.outcome)

    def test_unrecognised_licence_quarantined(self):
        r = classify(cc_cand(claimed_license_name="Totally Free Licence 9"), self.use, TODAY)
        self.assertEqual("quarantine", r.outcome)

    def test_missing_attribution_text_quarantined(self):
        r = classify(cc_cand(claimed_attribution_text=""), self.use, TODAY)
        self.assertEqual(("quarantine", ["ATTRIBUTION_TEXT_MISSING"]), (r.outcome, r.reasons))

    def test_share_alike_needs_explicit_acceptance(self):
        c = cc_cand(claimed_license_name="CC-BY-SA-4.0", claimed_license_url="https://creativecommons.org/licenses/by-sa/4.0/")
        self.assertEqual("quarantine", classify(c, self.use, TODAY).outcome)
        self.assertEqual("acquire", classify(c, UseContext(accept_share_alike=True), TODAY).outcome)

    def test_no_licence_text_url_means_hold_not_fetch(self):
        r = classify(cc_cand(license_text_url=""), self.use, TODAY)
        self.assertEqual(("quarantine", ["NO_LICENSE_TEXT_URL"]), (r.outcome, r.reasons))

    def test_no_download_source(self):
        r = classify(cc_cand(download_url=""), self.use, TODAY)
        self.assertIn("NO_DOWNLOAD_SOURCE", r.reasons)

    def test_preview_hint_in_title_quarantined(self):
        self.assertEqual("quarantine", classify(cc_cand(title="Watermarked preview sample"), self.use, TODAY).outcome)

    def test_claimed_watermark_rejected(self):
        self.assertEqual("reject", classify(cc_cand(claims_watermarked=True), self.use, TODAY).outcome)

    def test_triage_still_never_permits(self):
        from assets import triage
        for c in (cc_cand(), cc_cand(claimed_license_name="")):
            self.assertFalse(triage(c, self.use, TODAY).permitted)

    def test_provider_cannot_assert_a_clear_watermark(self):
        # a provider claim of "no watermark" is not accepted: the watermark stays unchecked after acquisition
        with tempfile.TemporaryDirectory() as d:
            lib = AssetLibrary(Path(d) / "lib", clock=CLOCK)
            item = acquire(cc_cand(claims_watermarked=False), lib, served(), ctx_for(), None)
            self.assertEqual(Watermark.UNCHECKED, lib.get(item.asset_id).watermark_status)
            self.assertIn("WATERMARK_UNCHECKED", item.reasons)


class TestSanitizeNewFields(unittest.TestCase):
    def bad(self, **kw):
        with self.assertRaises(ManifestError):
            from assets import sanitize_candidate
            sanitize_candidate(cc_cand(**kw))

    def test_http_download_url_rejected(self):
        self.bad(download_url="http://cdn.example.test/a.jpg")

    def test_credentials_in_download_url_rejected(self):
        self.bad(download_url="https://u:p@cdn.example.test/a.jpg")

    def test_bad_expected_sha_rejected(self):
        self.bad(expected_sha256="nothex")

    def test_unknown_claimed_basis_rejected(self):
        self.bad(claimed_basis="public-ish")

    def test_download_and_local_path_mutually_exclusive(self):
        self.bad(local_path="/tmp/x.jpg")

    def test_old_style_candidate_still_works(self):
        from assets import sanitize_candidate
        c = sanitize_candidate(AssetCandidate("p", "1", "T", Category.BROLL, MediaType.VIDEO))
        self.assertEqual(("", "", "", ""), (c.download_url, c.license_text_url, c.local_path, c.claimed_basis))


# ======================================================================================================
class TestAcquire(LibCase):
    def test_happy_path_lands_quarantined_with_licence_evidence(self):
        t = served()
        item = self.run_one(cc_cand(), t)
        self.assertEqual("acquired", item.outcome, item)
        rec = self.lib.get(item.asset_id)
        self.assertEqual((State.QUARANTINED, True, sha(JPEG), len(JPEG), TODAY),
                         (rec.state, rec.managed_copy, rec.checksum_sha256, rec.file_size, rec.acquisition_date))
        self.assertTrue((self.root / rec.file_path).is_file())
        self.assertEqual(JPEG, (self.root / rec.file_path).read_bytes())
        self.assertEqual(Verification.UNVERIFIED, rec.verification_status)
        self.assertEqual([], rec.attestations)

    def test_licence_snapshot_recorded_with_hash_url_date_verifier(self):
        item = self.run_one(cc_cand(), served())
        rec = self.lib.get(item.asset_id)
        ev = [e for e in rec.evidence if e.kind == "license_page"]
        self.assertEqual(1, len(ev))
        e = ev[0]
        self.assertEqual((sha(LICENSE_TEXT), CC_URL, TODAY, "document"), (e.sha256, e.url, e.captured_at, e.strength))
        self.assertIn(acq.VERIFIER, e.description)
        self.assertEqual(LICENSE_TEXT, (self.root / e.local_path).read_bytes())

    def test_provider_claim_evidence_is_kept_as_claim_only(self):
        rec = self.lib.get(self.run_one(cc_cand(), served()).asset_id)
        claims = [e for e in rec.evidence if e.kind == "provider_claim"]
        self.assertEqual(["claim"], [e.strength for e in claims])

    def test_nothing_is_approved_by_acquisition(self):
        item = self.run_one(cc_cand(), served())
        self.assertFalse(self.lib.cleared_for(item.asset_id, UseContext()))
        self.assertEqual([], self.lib.search())          # the default search returns approved assets only
        self.assertIn("UNVERIFIED", item.reasons)

    def test_human_attestation_still_approves_through_existing_model(self):
        item = self.run_one(cc_cand(), served())
        self.lib.update_rights(item.asset_id, watermark_status=Watermark.CLEAR)   # a reviewer checked the image
        self.lib.attest(item.asset_id, "Lukas", "human_review", "licence page and image checked")
        rec = self.lib.get(item.asset_id)
        self.assertEqual((State.APPROVED, 1), (rec.state, len(rec.attestations)))
        self.assertTrue(self.lib.cleared_for(item.asset_id, UseContext()))
        self.assertEqual(["attribution: Mushroom macro photo by A Photographer, CC BY 4.0"],
                         self.lib.obligations(item.asset_id, UseContext()))

    def test_reject_candidate_is_never_fetched(self):
        t = served()
        c = cc_cand(claimed_license_name="CC-BY-NC-4.0", claimed_license_url="https://creativecommons.org/licenses/by-nc/4.0/")
        item = self.run_one(c, t)
        self.assertEqual(("rejected", []), (item.outcome, t.requests))
        self.assert_clean()

    def test_quarantine_candidate_is_never_fetched_and_is_recorded(self):
        t = served()
        led = QuarantineLedger(self.root)
        item = acquire(cc_cand(claimed_license_name="", claimed_license_url=""), self.lib, t, ctx_for(), led)
        led.save()
        self.assertEqual(("quarantined", []), (item.outcome, t.requests))
        entry = QuarantineLedger(self.root).entries()[0]
        self.assertEqual(("openverse", "i1"), (entry["provider"], entry["external_id"]))
        self.assertIn("UNKNOWN_RIGHTS", entry["reasons"])
        self.assert_clean()

    def test_duplicate_content_by_sha256_detected(self):
        t = served()
        t.add(f"https://{CDN}/i2.jpg", JPEG, "image/jpeg")
        first = self.run_one(cc_cand("i1"), t)
        second = self.run_one(cc_cand("i2"), t)
        self.assertEqual(("acquired", "duplicate", first.asset_id), (first.outcome, second.outcome, second.asset_id))
        self.assertEqual(1, len(self.lib.all()))
        self.assertEqual(1, len([f for f in tree_files(self.root) if f.startswith("files/")]))

    def test_second_run_of_same_candidate_skips_the_fetch(self):
        t = served()
        self.run_one(cc_cand(), t)
        before = len(t.requests)
        item = self.run_one(cc_cand(), t)
        self.assertEqual(("duplicate", ["ALREADY_ACQUIRED"], before), (item.outcome, list(item.reasons), len(t.requests)))

    def test_never_overwrites_an_existing_managed_file(self):
        item = self.run_one(cc_cand(), served())
        rec = self.lib.get(item.asset_id)
        path = self.root / rec.file_path
        path.write_bytes(b"tampered but present")
        t = served(body=PNG, content_type="image/png")
        again = self.run_one(cc_cand(), t)
        self.assertEqual(("duplicate", b"tampered but present"), (again.outcome, path.read_bytes()))

    def test_wrong_signature_for_declared_type_fails(self):
        item = self.run_one(cc_cand(), served(body=PNG))
        self.assertEqual(("failed", ["CONTENT_MISMATCH"]), (item.outcome, list(item.reasons)))
        self.assert_clean()

    def test_hash_mismatch_fails_without_partial_file(self):
        item = self.run_one(cc_cand(expected_sha256="0" * 64), served())
        self.assertEqual(("failed", ["HASH_MISMATCH"]), (item.outcome, list(item.reasons)))
        self.assert_clean()

    def test_matching_expected_hash_accepted(self):
        self.assertEqual("acquired", self.run_one(cc_cand(expected_sha256=sha(JPEG)), served()).outcome)

    def test_every_failure_path_leaves_nothing_behind(self):
        cases = {
            "TRANSPORT_ERROR": served(midstream=ConnectionResetError("reset")),
            "TRUNCATED": served(length=len(JPEG) + 9),
            "TOO_LARGE": served(body=JPEG + b"x" * 20_000),
            "CONTENT_TYPE_NOT_ALLOWED": served(content_type="text/html"),
            "HTTP_STATUS": served(status=503),
            "HOST_NOT_ALLOWED": served(final_url="https://evil.example.test/x.jpg"),
        }
        for code, t in cases.items():
            with self.subTest(code):
                item = self.run_one(cc_cand(), t)
                self.assertEqual(("failed", code), (item.outcome, item.reasons[0]))
                self.assert_clean()

    def test_library_write_error_leaves_no_partial_file(self):
        real_add = self.lib.add

        def failing_add(rec, src, mode="reference", ctx=None):
            real_add(rec, src, mode, ctx)            # the copy happens, then the index write "fails"
            raise OSError("disk full while saving index")
        with mock.patch.object(self.lib, "add", failing_add):
            item = self.run_one(cc_cand(), served())
        self.assertEqual(("failed", "LIBRARY_WRITE_ERROR"), (item.outcome, item.reasons[0]))
        self.assertEqual([], [f for f in tree_files(self.root) if f.startswith(("files/", "evidence/", ".staging/"))])

    def test_library_write_error_before_copy_leaves_nothing(self):
        with mock.patch.object(self.lib, "add", side_effect=PermissionError("read-only library")):
            item = self.run_one(cc_cand(), served())
        self.assertEqual("failed", item.outcome)
        self.assertEqual([], [f for f in tree_files(self.root) if f.startswith(("files/", "evidence/", ".staging/"))])

    def test_errors_are_scrubbed(self):
        t = FakeTransport({CC_URL: dict(body=LICENSE_TEXT, content_type="text/html", status=200, length="auto",
                                         final_url="", chunk=9, raises=None, midstream=None)})
        t.add(f"https://{CDN}/i1.jpg", raises=ConnectionError("proxy failed " + SECRET_BIT + " Authorization: Bearer abcdef0123456789abcdef"))
        item = self.run_one(cc_cand(), t)
        blob = json.dumps(item.to_dict())
        for bad in ("hunter2", "abcdef0123456789abcdef"):
            self.assertNotIn(bad, blob)

    def test_licence_text_unavailable_means_no_asset_and_no_media_fetch(self):
        t = FakeTransport()
        t.add(f"https://{CDN}/i1.jpg", JPEG, "image/jpeg")
        item = self.run_one(cc_cand(), t)          # CC_URL has no route -> transport error
        self.assertEqual("failed", item.outcome)
        self.assertEqual([CC_URL], t.urls)
        self.assert_clean()

    def test_licence_text_must_come_from_allowed_licence_host(self):
        t = served()
        t.add("https://evil.example.test/licence", LICENSE_TEXT, "text/html")
        item = self.run_one(cc_cand(license_text_url="https://evil.example.test/licence"), t)
        self.assertEqual(("failed", "HOST_NOT_ALLOWED"), (item.outcome, item.reasons[0]))
        self.assertEqual([], t.requests)

    def test_licence_text_wrong_type_refused(self):
        t = served()
        t.add(CC_URL, LICENSE_TEXT, "application/octet-stream")
        self.assertEqual("failed", self.run_one(cc_cand(), t).outcome)
        self.assert_clean()

    def test_provider_without_host_allow_list_downloads_nothing(self):
        t = served()
        item = self.run_one(cc_cand(), t, policy=AcquisitionPolicy())
        self.assertEqual(("failed", "HOST_NOT_ALLOWED"), (item.outcome, item.reasons[0]))
        self.assert_clean()

    def test_path_traversal_in_external_id_cannot_escape(self):
        c = cc_cand("../../etc/passwd", download_url=f"https://{CDN}/p.jpg")
        t = served(ext="p")
        item = self.run_one(c, t)
        self.assertEqual("acquired", item.outcome)
        self.assertRegex(item.asset_id, r"^[a-z0-9][a-z0-9_-]{0,63}$")
        for f in tree_files(self.root):
            self.assertFalse(f.startswith(".."))
        self.assertEqual([], [p for p in self.tmp.rglob("passwd")])

    def test_asset_ids_are_deterministic_and_distinct(self):
        self.assertEqual(make_asset_id("openverse", "a/b"), make_asset_id("openverse", "a/b"))
        self.assertNotEqual(make_asset_id("openverse", "a/b"), make_asset_id("openverse", "a-b"))
        self.assertLessEqual(len(make_asset_id("x" * 90, "y" * 90)), 64)

    def test_malformed_candidate_is_rejected_not_raised(self):
        bad = cc_cand(download_url="http://cdn.example.test/a.jpg")
        item = acquire(bad, self.lib, served(), ctx_for(), None)
        self.assertEqual(("rejected", ("MALFORMED_CANDIDATE",)), (item.outcome, item.reasons))

    def test_rights_reject_discovered_after_download_never_indexed(self):
        # the NOT-yet-visible watermark hint only shows after we know the file name / checks run: simulate by a
        # rights decision that turns to reject once the facts are complete
        from assets import rights
        real = rights.evaluate_rights
        calls = []

        def late_reject(rec, ctx=None):
            d = real(rec, ctx)
            calls.append(rec.file_path)
            if rec.file_path and rec.checksum_sha256 == sha(JPEG):
                d.reasons.append(rights.Reason("WATERMARKED_PREVIEW", "found after download", "reject"))
                d.verdict = rights.REJECT
            return d
        with mock.patch.object(acq, "evaluate_rights", late_reject):
            item = self.run_one(cc_cand(), served())
        self.assertEqual(("rejected", ["WATERMARKED_PREVIEW"]), (item.outcome, list(item.reasons)))
        self.assert_clean()

    def test_staging_directory_is_removed_after_success(self):
        self.run_one(cc_cand(), served())
        self.assertEqual([], list((self.root / acq.STAGING).glob("*")) if (self.root / acq.STAGING).exists() else [])


class TestLedger(LibCase):
    def test_entries_sorted_and_deduplicated(self):
        led = QuarantineLedger(self.root)
        for ext in ("b", "a", "b"):
            led.record(cc_cand(ext), ["X", "A"], CLOCK())
        led.save()
        entries = QuarantineLedger(self.root).entries()
        self.assertEqual(["a", "b"], [e["external_id"] for e in entries])
        self.assertEqual(["A", "X"], entries[0]["reasons"])

    def test_query_strings_are_not_stored(self):
        led = QuarantineLedger(self.root)
        led.record(cc_cand(source_url="https://example.test/p?" + SECRET_BIT), ["X"], CLOCK())
        led.save()
        self.assertNotIn("hunter2", (self.root / acq.QUARANTINE_LEDGER).read_text())

    def test_damaged_ledger_is_not_overwritten(self):
        (self.root).mkdir(exist_ok=True)
        path = self.root / acq.QUARANTINE_LEDGER
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ManifestError):
            QuarantineLedger(self.root)
        self.assertEqual("{ not json", path.read_text())

    def test_invalid_structure_refused(self):
        self.root.mkdir(exist_ok=True)
        (self.root / acq.QUARANTINE_LEDGER).write_text(json.dumps({"schema_version": 1, "candidates": [{"x": 1}]}))
        with self.assertRaises(ManifestError):
            QuarantineLedger(self.root)

    def test_save_is_atomic_no_temp_left(self):
        led = QuarantineLedger(self.root)
        led.record(cc_cand(), ["X"], CLOCK())
        led.save()
        self.assertEqual([acq.QUARANTINE_LEDGER], [p.name for p in self.root.iterdir()])


# ======================================================================================================
class TestLicenceDrift(LibCase):
    def acquired(self):
        t = served()
        item = self.run_one(cc_cand(), t)
        self.assertEqual("acquired", item.outcome)
        return item.asset_id

    def approve(self, aid):
        self.lib.update_rights(aid, watermark_status=Watermark.CLEAR)
        self.lib.attest(aid, "Lukas", "human_review", "checked")
        self.assertEqual(State.APPROVED, self.lib.get(aid).state)

    def test_unchanged_text_reports_unchanged(self):
        aid = self.acquired()
        self.approve(aid)
        res = reverify_license(self.lib, aid, served(), make_policy(), TODAY)
        self.assertEqual("unchanged", res.status)
        self.assertEqual(State.APPROVED, self.lib.get(aid).state)

    def test_changed_text_moves_asset_to_quarantine_without_deleting(self):
        aid = self.acquired()
        self.approve(aid)
        path = self.root / self.lib.get(aid).file_path
        t = served()
        t.add(CC_URL, LICENSE_TEXT + b" EDITED", "text/html")
        res = reverify_license(self.lib, aid, t, make_policy(), TODAY)
        rec = self.lib.get(aid)
        self.assertEqual(("drifted", State.QUARANTINED), (res.status, rec.state))
        self.assertTrue(path.is_file())
        self.assertTrue(rec.state_reason.startswith("manual:"))
        self.assertIn("licence text changed", rec.state_reason)

    def test_attestation_history_is_kept_but_no_longer_counts(self):
        aid = self.acquired()
        self.approve(aid)
        t = served()
        t.add(CC_URL, b"<html>different</html>", "text/html")
        reverify_license(self.lib, aid, t, make_policy(), TODAY)
        rec = self.lib.get(aid)
        self.assertEqual((1, Verification.UNVERIFIED), (len(rec.attestations), rec.verification_status))
        self.assertFalse(self.lib.cleared_for(aid, UseContext()))

    def test_drift_hold_persists_through_re_attestation(self):
        aid = self.acquired()
        self.approve(aid)
        t = served()
        t.add(CC_URL, b"<html>different</html>", "text/html")
        reverify_license(self.lib, aid, t, make_policy(), TODAY)
        self.lib.attest(aid, "Lukas", "human_review", "looked again")
        self.assertEqual(State.QUARANTINED, self.lib.get(aid).state)       # only release_hold lifts it

    def test_new_text_is_saved_as_a_snapshot_and_becomes_the_baseline(self):
        aid = self.acquired()
        t = served()
        t.add(CC_URL, b"<html>different</html>", "text/html")
        reverify_license(self.lib, aid, t, make_policy(), TODAY)
        docs = [e for e in self.lib.get(aid).evidence if e.kind == "license_page"]
        self.assertEqual(2, len(docs))
        self.assertEqual(b"<html>different</html>", (self.root / docs[-1].local_path).read_bytes())
        self.assertEqual("unchanged", reverify_license(self.lib, aid, t, make_policy(), TODAY).status)

    def test_human_can_release_the_hold_after_review(self):
        aid = self.acquired()
        self.approve(aid)
        t = served()
        t.add(CC_URL, b"<html>different</html>", "text/html")
        reverify_license(self.lib, aid, t, make_policy(), TODAY)
        self.lib.release_hold(aid, "Lukas", "new text read, terms unchanged")
        self.lib.attest(aid, "Lukas", "human_review", "re-attested after drift")
        self.assertEqual(State.APPROVED, self.lib.get(aid).state)

    def test_unreachable_licence_page_is_unverifiable_and_changes_nothing(self):
        aid = self.acquired()
        self.approve(aid)
        t = FakeTransport()
        res = reverify_license(self.lib, aid, t, make_policy(), TODAY)
        self.assertEqual(("unverifiable", State.APPROVED), (res.status, self.lib.get(aid).state))

    def test_unreachable_page_can_be_made_to_quarantine(self):
        aid = self.acquired()
        self.approve(aid)
        res = reverify_license(self.lib, aid, FakeTransport(), make_policy(), TODAY, quarantine_on_unavailable=True)
        self.assertEqual(("unverifiable", State.QUARANTINED), (res.status, self.lib.get(aid).state))

    def test_asset_without_snapshot_reports_no_snapshot(self):
        folder = self.tmp / "loc"
        folder.mkdir()
        f = folder / "a.jpg"
        f.write_bytes(JPEG)
        (folder / ("a.jpg" + SIDECAR_SUFFIX)).write_text(json.dumps(owned_sidecar()))
        rep = find_and_acquire(AssetRequest(description="mushroom"), registry_of(LocalFolderProvider(folder)), self.lib,
                               FakeTransport(), ctx_for())
        aid = rep.acquired[0].asset_id
        self.assertEqual("no_snapshot", reverify_license(self.lib, aid, FakeTransport(), make_policy()).status)

    def test_licence_host_is_enforced_on_reverify(self):
        aid = self.acquired()
        res = reverify_license(self.lib, aid, served(), make_policy(license_hosts=frozenset({"other.example.test"})), TODAY)
        self.assertEqual("unverifiable", res.status)


# ======================================================================================================
def owned_sidecar(**kw):
    base = {"schema_version": 1, "title": "Mushroom closeup", "category": "image", "rights_basis": "user_owned",
            "owner_declaration": "I took this photo myself", "commercial_use_allowed": True,
            "modification_allowed": True, "tags": ["mushroom", "macro"]}
    base.update(kw)
    return base


def registry_of(*providers) -> ProviderRegistry:
    reg = ProviderRegistry()
    for p in providers:
        reg.register_search(p)
    return reg


class TestLocalFolder(LibCase):
    def setUp(self):
        super().setUp()
        self.folder = self.tmp / "userfiles"
        self.folder.mkdir()

    def put(self, name, data=JPEG, sidecar=None, raw=None):
        name = name.replace("m.jpg", "mushroom.jpg")      # file names carry the search terms when a sidecar is bad
        (self.folder / name).write_bytes(data)
        if sidecar is not None:
            (self.folder / (name + SIDECAR_SUFFIX)).write_text(json.dumps(sidecar), encoding="utf-8")
        if raw is not None:
            (self.folder / (name + SIDECAR_SUFFIX)).write_text(raw, encoding="utf-8")

    def run_local(self, **kw):
        return find_and_acquire(AssetRequest(description="mushroom macro", **kw), registry_of(LocalFolderProvider(self.folder)),
                                self.lib, FakeTransport(), ctx_for())

    def test_valid_user_owned_file_is_copied_and_stays_quarantined(self):
        self.put("m.jpg", sidecar=owned_sidecar())
        rep = self.run_local()
        self.assertEqual(1, len(rep.acquired), rep.to_dict())
        rec = self.lib.get(rep.acquired[0].asset_id)
        self.assertEqual((State.QUARANTINED, True, sha(JPEG)), (rec.state, rec.managed_copy, rec.checksum_sha256))
        self.assertEqual("user_owned", rec.rights_basis.value)

    def test_originals_are_never_modified_moved_or_deleted(self):
        self.put("m.jpg", sidecar=owned_sidecar())
        self.put("bad.jpg", JPEG + b"2", raw="{broken")
        before = {p.name: (sha(p.read_bytes()), p.stat().st_mtime_ns) for p in self.folder.iterdir()}
        self.run_local()
        after = {p.name: (sha(p.read_bytes()), p.stat().st_mtime_ns) for p in self.folder.iterdir()}
        self.assertEqual(before, after)

    def test_human_attestation_makes_it_usable(self):
        self.put("m.jpg", sidecar=owned_sidecar())
        aid = self.run_local().acquired[0].asset_id
        self.lib.update_rights(aid, watermark_status=Watermark.CLEAR)
        self.lib.attest(aid, "Lukas", "user_declaration", "my own photo")
        self.assertEqual(State.APPROVED, self.lib.get(aid).state)

    def test_missing_sidecar_is_quarantined_not_copied(self):
        self.put("m.jpg")
        rep = self.run_local()
        self.assertEqual((0, 1), (len(rep.acquired), len(rep.quarantined)))
        self.assertIn("UNKNOWN_RIGHTS", rep.quarantined[0].reasons)
        self.assertEqual([], self.lib.all())

    def test_unknown_key_quarantines_the_whole_sidecar(self):
        self.put("m.jpg", sidecar=owned_sidecar(surprise=True))
        rep = self.run_local()
        self.assertEqual(("quarantined", 0), (rep.items[0].outcome, len(rep.acquired)))

    def test_missing_required_rights_keys_quarantined(self):
        sc = owned_sidecar()
        del sc["rights_basis"]
        self.put("m.jpg", sidecar=sc)
        self.assertEqual(1, len(self.run_local().quarantined))

    def test_invalid_json_quarantined(self):
        self.put("m.jpg", raw="{ nope")
        self.assertEqual(1, len(self.run_local().quarantined))

    def test_user_owned_without_declaration_quarantined(self):
        sc = owned_sidecar()
        del sc["owner_declaration"]
        self.put("m.jpg", sidecar=sc)
        self.assertEqual(1, len(self.run_local().quarantined))

    def test_unknown_commercial_status_quarantined(self):
        self.put("m.jpg", sidecar=owned_sidecar(commercial_use_allowed=None))
        rep = self.run_local()
        self.assertEqual(("quarantined", True), (rep.items[0].outcome, "COMMERCIAL_UNKNOWN" in rep.items[0].reasons))

    def test_wrong_types_quarantined(self):
        for bad in (owned_sidecar(tags="mushroom"), owned_sidecar(commercial_use_allowed="yes"),
                    owned_sidecar(category="nonsense"), owned_sidecar(schema_version=True),
                    owned_sidecar(rights_basis="whatever"), owned_sidecar(media_type="video")):
            with self.subTest(bad=bad):
                _, problems = validate_sidecar(bad, MediaType.IMAGE)
                self.assertTrue(problems)

    def test_extension_not_matching_content_fails(self):
        self.put("m.jpg", data=PNG, sidecar=owned_sidecar())
        rep = self.run_local()
        self.assertEqual(("failed", ("CONTENT_MISMATCH",)), (rep.items[0].outcome, rep.items[0].reasons))
        self.assertEqual([], self.lib.all())

    def test_empty_and_oversize_local_files_fail(self):
        self.put("e.jpg", data=b"", sidecar=owned_sidecar(title="empty mushroom macro"))
        self.put("big.jpg", data=JPEG + b"x" * 20_000, sidecar=owned_sidecar(title="big mushroom macro"))
        rep = self.run_local()
        self.assertEqual({"EMPTY", "TOO_LARGE"}, {i.reasons[0] for i in rep.failed})

    def test_non_media_extension_ignored(self):
        self.put("notes.txt", data=b"hello")
        self.assertEqual([], LocalFolderProvider(self.folder).search(AssetRequest(description="notes")))

    def test_request_filters_apply(self):
        self.put("m.jpg", sidecar=owned_sidecar())
        self.put("s.wav", data=WAV, sidecar=owned_sidecar(category="sfx", title="Bell ring", tags=[]))
        prov = LocalFolderProvider(self.folder)
        self.assertEqual(["mushroom.jpg"], [c.external_id for c in prov.search(AssetRequest(description="mushroom"))])
        self.assertEqual(["s.wav"], [c.external_id for c in prov.search(AssetRequest(media_types=[MediaType.AUDIO]))])
        self.assertEqual([], prov.search(AssetRequest(description="giraffe")))
        self.assertEqual(1, len(prov.search(AssetRequest(), max_results=1)))

    def test_cc_claim_in_sidecar_is_only_a_claim(self):
        sc = owned_sidecar(rights_basis="creative_commons", license_name="CC-BY-4.0",
                           license_url=CC_URL, attribution_text="Photo by me, CC BY 4.0")
        del sc["owner_declaration"]
        self.put("m.jpg", sidecar=sc)
        rep = self.run_local()
        aid = rep.acquired[0].asset_id
        rec = self.lib.get(aid)
        self.assertEqual(State.QUARANTINED, rec.state)
        self.assertIn("CLAIM_ONLY_EVIDENCE", rep.acquired[0].reasons)

    def test_user_owned_attestation_method_required_to_be_user_declaration(self):
        self.put("m.jpg", sidecar=owned_sidecar())
        aid = self.run_local().acquired[0].asset_id
        self.lib.update_rights(aid, watermark_status=Watermark.CLEAR)
        self.lib.attest(aid, "Reviewer", "human_review", "looks right")
        self.assertEqual(State.QUARANTINED, self.lib.get(aid).state)       # NO_OWNER_DECLARATION until the user declares

    def test_symlinked_file_is_skipped(self):
        target = self.tmp / "outside.jpg"
        target.write_bytes(JPEG)
        link = self.folder / "link.jpg"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual([], LocalFolderProvider(self.folder).search(AssetRequest()))

    def test_missing_folder_gives_no_candidates(self):
        self.assertEqual([], LocalFolderProvider(self.tmp / "nope").search(AssetRequest()))


# ======================================================================================================
OPENVERSE_IMAGES = {   # authored from the public API documentation; not a recorded live response
    "result_count": 4, "page_count": 1,
    "results": [
        {"id": "img-1", "title": "Mushroom macro photo", "creator": "A Photographer", "license": "by", "license_version": "4.0",
         "license_url": CC_URL, "url": f"https://{CDN}/img-1.jpg", "foreign_landing_url": "https://example.test/photo/img-1",
         "attribution": "\"Mushroom macro photo\" by A Photographer is licensed under CC BY 4.0.",
         "tags": [{"name": "mushroom"}, {"name": "macro"}], "mature": False, "filetype": "jpg"},
        {"id": "img-2", "title": "Fern", "license": "by-nc", "license_version": "4.0",
         "license_url": "https://creativecommons.org/licenses/by-nc/4.0/", "url": f"https://{CDN}/img-2.jpg",
         "attribution": "Fern, CC BY-NC", "tags": [{"name": "mushroom"}], "mature": False},
        {"id": "img-3", "title": "Mystery mushroom", "license": "custom-thing", "license_version": None,
         "license_url": "", "url": f"https://{CDN}/img-3.jpg", "tags": [{"name": "mushroom"}], "mature": False},
        {"id": "img-4", "title": "Adult content", "license": "cc0", "license_version": "1.0", "url": f"https://{CDN}/img-4.jpg",
         "mature": True},
    ]}


def ov_body(obj=None) -> bytes:
    return json.dumps(OPENVERSE_IMAGES if obj is None else obj).encode("utf-8")


class TestOpenverse(LibCase):
    def test_search_url_is_pure_and_carries_no_credentials(self):
        url = ov.build_search_url(AssetRequest(description="mushroom", keywords=["macro"]), MediaType.IMAGE, 5)
        self.assertTrue(url.startswith("https://api.openverse.org/v1/images/?"))
        for needle in ("q=mushroom+macro", "page_size=5", "mature=false", "license_type=commercial%2Cmodification"):
            self.assertIn(needle, url)
        self.assertNotRegex(url.lower(), r"key|token|secret|password")

    def test_video_has_no_endpoint(self):
        from assets import ProviderError
        with self.assertRaises(ProviderError):
            ov.build_search_url(AssetRequest(), MediaType.VIDEO, 5)

    def test_page_size_is_clamped(self):
        self.assertIn("page_size=20", ov.build_search_url(AssetRequest(description="x"), MediaType.IMAGE, 5000))
        self.assertIn("page_size=1&", ov.build_search_url(AssetRequest(description="x"), MediaType.IMAGE, 0))

    def test_licence_names_only_from_documented_fields(self):
        cases = [("by", "4.0", "CC-BY-4.0"), ("by-sa", "3.0", "CC-BY-SA-3.0"), ("cc0", "1.0", "CC0-1.0"),
                 ("pdm", None, "PDM-1.0"), ("by", None, ""), ("by", "banana", ""), ("sampling+", "1.0", ""),
                 (None, "4.0", ""), ("BY-NC", "2.0", "CC-BY-NC-2.0")]
        for code, ver, want in cases:
            self.assertEqual(want, ov.license_name(code, ver), (code, ver))

    def test_parse_fixture(self):
        cands, problems = ov.parse_search_response(ov_body(), MediaType.IMAGE)
        self.assertEqual(["img-1", "img-2", "img-3"], [c.external_id for c in cands])
        c = cands[0]
        self.assertEqual(("CC-BY-4.0", CC_URL, f"https://{CDN}/img-1.jpg", ["mushroom", "macro"]),
                         (c.claimed_license_name, c.license_text_url, c.download_url, c.tags))
        self.assertTrue(any("mature" in p for p in problems))
        self.assertEqual("", cands[2].claimed_license_name)

    def test_parse_rejects_hostile_payloads_without_raising(self):
        for body in (b"not json", b"[]", b"{}", json.dumps({"results": "x"}).encode(), b"\xff\xfe",
                     b"x" * (ov.MAX_RESPONSE_BYTES + 1), None):
            with self.subTest(body=str(body)[:20]):
                cands, problems = ov.parse_search_response(body, MediaType.IMAGE)
                self.assertEqual(([], True), (cands, bool(problems)))

    def test_parse_skips_malformed_results(self):
        body = ov_body({"results": [5, {"id": ""}, {"id": "ok", "url": "http://x.test/a.jpg"},
                                    {"id": "ok2", "url": f"https://{CDN}/a.jpg", "title": "T", "license": "cc0", "license_version": "1.0"}]})
        cands, problems = ov.parse_search_response(body, MediaType.IMAGE)
        self.assertEqual(["ok2"], [c.external_id for c in cands])
        self.assertEqual(3, len(problems))

    def test_provider_searches_through_the_transport(self):
        t = FakeTransport()
        url = ov.build_search_url(AssetRequest(description="mushroom"), MediaType.IMAGE, 10)
        t.add(url, ov_body(), "application/json")
        cands = OpenverseProvider(t).search(AssetRequest(description="mushroom", media_types=[MediaType.IMAGE]), 10)
        self.assertEqual(3, len(cands))
        self.assertEqual([url], t.urls)

    def test_provider_failure_is_contained_by_the_registry(self):
        reg = registry_of(OpenverseProvider(FakeTransport()))
        cands, problems = reg.search_all(AssetRequest(description="mushroom", media_types=[MediaType.IMAGE]))
        self.assertEqual(([], 1), (cands, len(problems)))

    def test_video_request_makes_no_call(self):
        t = FakeTransport()
        self.assertEqual([], OpenverseProvider(t).search(AssetRequest(media_types=[MediaType.VIDEO]), 5))
        self.assertEqual([], t.requests)

    def test_end_to_end_find_and_acquire(self):
        req = AssetRequest(description="mushroom", media_types=[MediaType.IMAGE], project="demo")
        t = FakeTransport()
        t.add(ov.build_search_url(req, MediaType.IMAGE, 10), ov_body(), "application/json")
        t.add(CC_URL, LICENSE_TEXT, "text/html")
        t.add(f"https://{CDN}/img-1.jpg", JPEG, "image/jpeg")
        rep = find_and_acquire(req, registry_of(OpenverseProvider(t)), self.lib, t, ctx_for())
        self.assertEqual((["img-1"], ["img-2"], ["img-3"], []),
                         ([i.external_id for i in rep.acquired], [i.external_id for i in rep.rejected],
                          [i.external_id for i in rep.quarantined], rep.failed))
        self.assertNotIn(f"https://{CDN}/img-2.jpg", t.urls)          # rejected: never fetched
        self.assertNotIn(f"https://{CDN}/img-3.jpg", t.urls)          # unknown rights: never fetched
        self.assertEqual(["demo"], self.lib.get(rep.acquired[0].asset_id).projects)


# ======================================================================================================
class FakeProvider:
    def __init__(self, name, cands=None, error=None):
        self.name, self.cands, self.error = name, cands or [], error

    def search(self, request, max_results=10):
        if self.error:
            raise self.error
        return list(self.cands)


class TestFindAndAcquire(LibCase):
    def routes(self):
        t = served("i1")
        t.add(f"https://{CDN}/i2.jpg", PNG, "image/jpeg")          # declared JPEG, PNG signature -> CONTENT_MISMATCH
        t.add(f"https://{CDN}/i3.jpg", JPEG + b"3", "image/jpeg")
        return t

    def cands(self):
        return [cc_cand("i3"), cc_cand("i1"), cc_cand("i2"),
                cc_cand("q1", claimed_license_name=""), cc_cand("r1", claimed_license_name="CC-BY-NC-4.0",
                                                                 claimed_license_url="https://creativecommons.org/licenses/by-nc/4.0/"),
                cc_cand("f1", download_url=f"https://{CDN}/missing.jpg")]

    def run_into(self, root, provider_name="openverse"):
        lib = AssetLibrary(root, clock=CLOCK)
        reg = registry_of(FakeProvider(provider_name, self.cands()))
        return find_and_acquire(AssetRequest(description="mushroom", project="demo"), reg, lib, self.routes(), ctx_for())

    def test_report_lists_every_outcome_with_reasons(self):
        rep = self.run_into(self.root)
        self.assertEqual({"acquired": 2, "quarantined": 1, "rejected": 1, "failed": 2, "duplicate": 0}, rep.to_dict()["counts"])
        self.assertEqual("NO_COMMERCIAL", rep.rejected[0].reasons[0])
        self.assertIn("UNKNOWN_RIGHTS", rep.quarantined[0].reasons)
        self.assertEqual({"CONTENT_MISMATCH", "TRANSPORT_ERROR"}, {i.reasons[0] for i in rep.failed})

    def test_ordering_is_stable_regardless_of_provider_order(self):
        rep = self.run_into(self.root)
        self.assertEqual(["i1", "i3"], [i.external_id for i in rep.acquired])

    def test_report_is_byte_identical_across_runs_and_libraries(self):
        a = self.run_into(self.tmp / "libA").to_json()
        b = self.run_into(self.tmp / "libB").to_json()
        self.assertEqual(a, b)
        self.assertEqual(json.loads(a), json.loads(b))

    def test_rerun_marks_everything_already_acquired(self):
        self.run_into(self.root)
        rep = self.run_into(self.root)
        self.assertEqual([], rep.acquired)
        self.assertEqual({"ALREADY_ACQUIRED"}, {i.reasons[0] for i in rep.duplicates})

    def test_failing_provider_is_reported_not_raised(self):
        reg = registry_of(FakeProvider("a", error=RuntimeError("down " + SECRET_BIT)), FakeProvider("openverse", [cc_cand()]))
        rep = find_and_acquire(AssetRequest(description="mushroom"), reg, self.lib, served(), ctx_for())
        self.assertEqual((1, 1), (len(rep.acquired), len(rep.provider_problems)))
        self.assertNotIn("hunter2", rep.to_json())

    def test_project_is_linked_to_new_and_existing_assets(self):
        rep = self.run_into(self.root)
        lib = AssetLibrary(self.root, clock=CLOCK)
        for i in rep.acquired:
            self.assertEqual(["demo"], lib.get(i.asset_id).projects)

    def test_nothing_acquired_is_approved(self):
        rep = self.run_into(self.root)
        lib = AssetLibrary(self.root, clock=CLOCK)
        self.assertTrue(all(lib.get(i.asset_id).state is State.QUARANTINED for i in rep.acquired))

    def test_quarantined_candidates_are_in_the_ledger(self):
        self.run_into(self.root)
        self.assertEqual(["q1"], [e["external_id"] for e in QuarantineLedger(self.root).entries()])

    def test_library_is_unchanged_by_rejected_quarantined_and_failed_candidates(self):
        self.run_into(self.root)
        self.assertEqual(2, len(self.lib.__class__(self.root, clock=CLOCK).all()))

    def test_same_candidate_from_two_search_passes_is_processed_once(self):
        reg = registry_of(FakeProvider("openverse", [cc_cand("i1"), cc_cand("i1")]))
        rep = find_and_acquire(AssetRequest(description="mushroom"), reg, self.lib, served(), ctx_for())
        self.assertEqual(1, len(rep.items))

    def test_report_json_round_trips_and_has_schema(self):
        d = json.loads(self.run_into(self.root).to_json())
        self.assertEqual(1, d["schema_version"])
        self.assertEqual({"schema_version", "counts", "acquired", "quarantined", "rejected", "failed", "duplicates",
                          "provider_problems"}, set(d))

    def test_no_network_is_touched(self):
        def boom(*a, **k):
            raise AssertionError("network access attempted")
        with mock.patch.object(socket, "socket", boom), mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket, "getaddrinfo", boom):
            rep = self.run_into(self.root)
        self.assertEqual(2, len(rep.acquired))

    def test_acquisition_modules_import_no_network_code(self):
        banned = re.compile(r"^\s*(import|from)\s+(socket|ssl|http|urllib\.request|urllib3|requests|httpx|aiohttp|ftplib|smtplib|asyncio)\b",
                            re.M)
        for name in ("acquisition", "openverse", "local_provider", "providers"):
            src = (Path(__file__).resolve().parents[1] / "assets" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIsNone(banned.search(src), name)


class TestExistingBehaviourUntouched(unittest.TestCase):
    def test_existing_suite_helpers_still_importable(self):
        import assets
        for name in ("AssetLibrary", "evaluate_rights", "triage", "ProviderRegistry", "sanitize_candidate"):
            self.assertTrue(hasattr(assets, name))

    def test_triage_of_new_style_candidate_is_not_permitted(self):
        from assets import triage
        self.assertEqual("quarantine", triage(cc_cand(), UseContext(), TODAY).verdict)


if __name__ == "__main__":
    unittest.main()
