"""TEST FIXTURE ONLY — not part of any package.

`creator_dna.DNAStore` is strict: footage evidence without an eligible-creator attribution is refused.
Many older tests exercise unrelated behaviour (scoring, layers, review, persistence) with hand-built evidence.
`LenientStore` lets them keep doing that by stamping a SYNTHETIC attribution on evidence that has none,
standing in for an upstream eligibility decision. Production code never does this; tests of the attribution
rules themselves use the strict `DNAStore` directly.
"""
import dataclasses

import creator_dna as dna

_CHANNEL = {dna.Modality.TRANSCRIPT: "speech", dna.Modality.AUDIO: "speech", dna.Modality.VISUAL: "visual"}


def synthetic_stamp(ev: dna.Evidence) -> dna.EvidenceAttribution:
    start = 0.0 if ev.start_s is None else ev.start_s
    end = start if ev.end_s is None else ev.end_s
    return dna.EvidenceAttribution(
        decision_id="dec_test_fixture", candidate_record_id=(ev.origin.record_id if ev.origin and ev.origin.record_id else "rec_test"),
        media_id=ev.origin.media_id if ev.origin else "m_test", channel=_CHANNEL[ev.modality], start_s=start, end_s=end,
        identity_basis="test_fixture", confidence=0.95, policy_version="test", attribution_evidence_ids=("ae_test_fixture",))


class LenientStore(dna.DNAStore):
    def _check_attribution(self, ev):      # stamping happens in add_evidence, after ingest's pre-check
        return None

    def add_evidence(self, trait_id, evidence, actor):
        if evidence.method is not dna.Method.CREATOR_STATEMENT and evidence.attribution is None:
            if evidence.start_s is None:
                evidence = dataclasses.replace(evidence, start_s=0.0)
            evidence = dataclasses.replace(evidence, attribution=synthetic_stamp(evidence))
        return super().add_evidence(trait_id, evidence, actor)
