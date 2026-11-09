"""speaker_attribution: who is speaking / visible, and whether evidence is eligible for Creator DNA.

Sits between media_analysis and creator_dna (it imports media_analysis records and produces
`creator_dna.EvidenceAttribution` stamps). It identifies nobody by itself: attribution comes from human
confirmation, project metadata, and (future, optional) supporting providers. Unknown stays unknown.
See INGESTION.md / CREATOR_DNA.md "Speaker attribution and eligibility".
"""
from .eligibility import decide
from .handoff import (build_attributed_handoff, candidates_from_audio, candidates_from_transcript,
                      candidates_from_visual, dna_stamp, summarize, sync_revocations)
from .models import (AttributionError, AttributionEvidence, AttributionMethod, Candidate, Channel, Decision, Delivery,
                     EligibilityDecision, Identity, PolicyConfig, Resolution, Status)
from .providers import (AttributionProvider, AttributionRequest, FixtureProvider, ProviderCapabilities,
                        UnavailableProvider, run_provider)
from .resolution import resolve
from .store import AttributionLogError, AttributionStore

__all__ = [n for n in dir() if not n.startswith("_")]
