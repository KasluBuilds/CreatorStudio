"""Research Agent foundation (Phase 4). PROPOSED design + offline implementation.

No provider is bundled, nothing touches the network, and nothing here is wired into the
pipeline or `creator_state`. See the report accompanying this package for integration needs.
"""
from .agent import BriefBuilder, ResearchAgent, sanitize_source
from .discovery import (DiscoveryProvider, DiscoveryRequest, ProviderError, ScoredCandidate,
                        TopicCandidate, WeeklyReport, rank_candidates)
from .formats import check_format_study
from .models import SCHEMA_VERSION, ResearchBrief
from .outputs import (load_brief, save_brief, to_asset_requests, to_creative_director_packet,
                      to_markdown)
from .providers import (Gathered, ProviderRegistry, QuarantinedRecord, RawHit, ResearchBudget,
                        ResearchProvider, SearchQuery, StaticProvider)
from .safety import md_code, md_escape, redact_text, sanitize_text
from .scoring import CreatorInterests, ScoreWeights, TopicSignals, score_topic
from .attestation import (MethodRegistry, RevocationList, TrustPolicy, VerificationMethod, retrieval_message,
                          excerpt_message, signing_message, unsigned_attestation)
from .sources import (SourceCatalog, SourceIdConflict, assess_all, assess_claim, assess_source,
                      citation_level, classify_source, confirm_source, explain, independence_groups,
                      make_source)
from .validation import BriefInvalid, ValidationReport, validate_brief

__all__ = [n for n in dir() if not n.startswith("_")]
