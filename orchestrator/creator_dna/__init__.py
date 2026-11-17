"""Creator DNA: evidence-backed, append-only model of the creator's observable style.

Standalone (stdlib only); media_analysis feeds it through adapters, nothing here analyses media.
See inputs.py (input shapes), metrics.py (audio metric contract), confidence.py (scoring),
verification.py (attribution verifier contract), consent.py (purpose-scoped consent), analyzers.py, review.py.
"""
from .analyzers import EnergyRangeAnalyzer, PauseProfileAnalyzer, SentenceRhythmAnalyzer
from .confidence import DEFAULT_CONFIG, Assessment, ScoringConfig, assess
from .consent import PURPOSES, ConsentDecision, ConsentRecord, Purpose
from .inputs import (AudioWindow, RecurringPhraseAnalyzer, TranscriptSegment, VisualObservation,
                     evidence_from_visual, evidence_from_window, quote_in_transcript)
from .metrics import METRICS, MetricSpec, validate_metrics
from .schema import (SCHEMA_VERSION, Category, DNAError, Evidence, EvidenceAttribution, Layer, Method, Modality, Origin,
                     Polarity, ReviewInfo, Scope, Status, TraitCandidate, trait_id_for)
from .review import ReviewDecisionError, apply_decisions, diff_exports, review_queue
from .store import AuditReport, DNALogError, DNAStore, IngestReport
from .verification import (AttributionVerifier, Outcome, Verification, VerificationRequest, VerificationUnavailable)

__all__ = [n for n in dir() if not n.startswith("_")]
