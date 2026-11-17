"""Asset Agent: metadata schema, rights engine, evaluation, library, provider interfaces, acquisition.

Offline by construction: the acquisition step fetches only through an injected Transport (no network code lives
here), and nothing is approved without a named human's attestation. No automated licence verification.
Unknown rights are never permission. See rights.py for the decision rules and acquisition.py for the fetch gate.
"""
from .acquisition import (AcquisitionContext, AcquisitionPolicy, AcquisitionReport, FetchError, LicenseCheck,
                          QuarantineLedger, ReportItem, Transport, TransportRequest, TransportResponse, acquire,
                          find_and_acquire, reverify_license, scrub)
from .local_provider import LocalFolderProvider
from .openverse import OpenverseProvider
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
