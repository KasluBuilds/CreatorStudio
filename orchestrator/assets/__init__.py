"""Asset Agent foundation: metadata schema, rights engine, evaluation, library, provider interfaces.

Foundation only: no external API, no downloader, no automated licence verification.
Unknown rights are never permission. See rights.py for the decision rules.
"""
from .evaluation import (AssetEvaluation, AssetRequest, TargetSpec, evaluate, find_duplicates, quality_score,
                         relevance, technical_compat)
from .library import AssetLibrary
from .models import (AssetError, AssetExistsError, AssetRecord, Attestation, Category, DuplicateAssetError,
                     Evidence, ManifestError, MediaType, RightsBasis, State, UnsafePathError, Verification,
                     Watermark, sha256_file)
from .providers import (AssetCandidate, AssetGenerationProvider, AssetSearchProvider, ProviderError,
                        ProviderRegistry, candidate_to_record, sanitize_candidate, triage)
from .rights import (PERMITTED, QUARANTINE, REJECT, Reason, RightsDecision, UseContext, evaluate_rights,
                     parse_license)
