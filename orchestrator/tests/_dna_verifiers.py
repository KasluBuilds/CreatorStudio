"""TEST FIXTURES ONLY - never import this module from production code.

`DNAStore` refuses footage evidence unless an injected verifier answers VERIFIED and a current consent
covers style learning. Tests that exercise other behaviour need a store that admits their synthetic
evidence. This module provides two clearly named doubles:

* `RegistryVerifier`      a faithful fake of an upstream attribution store: it only verifies stamps that a
                          test registered, and checks media, record, channel and time range against what was
                          registered. Use it to prove the verification contract.
* `TrustingVerifierTestOnly`  accepts every stamp. It exists so older tests of unrelated behaviour (scoring,
                          layers, persistence, handoff plumbing) can build a strict store without a real
                          upstream. It is NOT a security control and must never appear in production wiring.

`strict_test_store()` builds a real, strict `DNAStore` (verifier + project + consent), not a subclass that
overrides checks.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import creator_dna as dna
from creator_dna.verification import Verification, VerificationUnavailable

TEST_PROJECT = "proj_test"


class TrustingVerifierTestOnly:
    """Accepts any stamp. TEST ONLY."""
    def verify(self, request):
        return Verification.verified("TEST ONLY: every stamp is accepted")


class RegistryVerifier:
    """Faithful fake of an upstream attribution store. `register(stamp)` records what upstream decided;
    `state[decision_id]` may be set to 'stale'; `offline = True` simulates an unreachable upstream."""

    def __init__(self):
        self.decisions = {}      # decision_id -> dict(media_id, record_id, channel, start_s, end_s, state)
        self.offline = False
        self.calls = []

    def register(self, stamp, state="current"):
        self.decisions[stamp.decision_id] = dict(
            media_id=stamp.media_id, record_id=stamp.candidate_record_id, channel=stamp.channel,
            start_s=stamp.start_s, end_s=stamp.end_s, state=state)
        return stamp

    def verify(self, request):
        self.calls.append(request.evidence_id)
        if self.offline:
            raise VerificationUnavailable("upstream attribution store is offline")
        st = request.stamp
        d = self.decisions.get(st.decision_id)
        if d is None:
            return Verification.unknown("no such decision upstream")
        if d["state"] != "current":
            return Verification.stale(f"decision is {d['state']}")
        chan = {"transcript": "speech", "audio": "speech", "visual": "visual"}.get(request.modality)
        if (d["media_id"] != st.media_id or d["record_id"] != st.candidate_record_id or d["channel"] != chan
                or (request.media_id is not None and request.media_id != d["media_id"])
                or (request.record_id is not None and request.record_id != d["record_id"])
                or request.start_s is None
                or request.start_s < d["start_s"] - 1e-3
                or (request.end_s if request.end_s is not None else request.start_s) > d["end_s"] + 1e-3):
            return Verification.invalid("decision does not cover this evidence")
        return Verification.verified()


def strict_test_store(directory, clock=None, project_id=TEST_PROJECT, verifier=None, consent=True):
    """A real strict DNAStore for tests: injected verifier, project id and (by default) a style-learning consent."""
    kw = {} if clock is None else {"clock": clock}
    store = dna.DNAStore(directory, verifier=verifier if verifier is not None else TrustingVerifierTestOnly(),
                         project_id=project_id, **kw)
    if consent and not store.consents:
        store.record_consent("creator", project_id, [dna.Purpose.STYLE_LEARNING], "human:test")
    return store
