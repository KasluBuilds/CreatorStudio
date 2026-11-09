"""Creator DNA: evidence-backed, append-only model of the creator's observable style.

Standalone (stdlib only); media_analysis feeds it through adapters, nothing here analyses media.
See inputs.py (input shapes), metrics.py (audio metric contract), confidence.py (scoring).
"""
from .confidence import DEFAULT_CONFIG, Assessment, ScoringConfig, assess
from .inputs import (AudioWindow, RecurringPhraseAnalyzer, TranscriptSegment, VisualObservation,
                     evidence_from_visual, evidence_from_window, quote_in_transcript)
from .metrics import METRICS, MetricSpec, validate_metrics
from .schema import (SCHEMA_VERSION, Category, DNAError, Evidence, EvidenceAttribution, Layer, Method, Modality, Origin,
                     Polarity, ReviewInfo, Scope, Status, TraitCandidate, trait_id_for)
from .store import DNALogError, DNAStore, IngestReport

__all__ = [n for n in dir() if not n.startswith("_")]
