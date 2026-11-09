"""media_analysis: structured, evidence-oriented media records (Phase 2) and their handoff to creator_dna.

Uses creator_state's probe helpers and project/stage API (read-only imports, see pipeline.py) and offers
adapters to creator_dna's input shapes (see INTEGRATION_CREATOR_DNA.md). Real transcription / speech-activity /
vision engines are NOT included: only their interfaces, "unavailable" placeholders and synthetic fixtures.
"""
from .adapters import (CONTRACT_NOTES, DnaEligibility, DnaHandoff, HandoffReport, SourceResolver, build_handoff,
                       to_dna_audio, to_dna_transcript, to_dna_visual)
from .audio_analysis import (ActivityRegion, AudioAnalysis, AudioWindow, EnergyEvent, QuietGap, SpeechRegion,
                             analyze_wav, load_audio_analysis, save_audio_analysis)
from .inventory import (build_item, build_manifest, discover, find_duplicate_content, get_ref,
                        is_variable_frame_rate, load_manifest, save_manifest, validate_item, verify_item)
from .models import (CapabilityError, MediaAnalysisError, MediaRef, Provenance, RecordKind)
from .moment_index import EvidenceRef, Interpretation, Moment, MomentIndex
from .proxies import AnalysisCopyRegistry
from .review import (ReviewedSegment, ReviewedTranscript, SegmentReview, TranscriptReview, apply_review, extend_review,
                     latest_review, load_review, make_review, save_review)
from .scene_detection import (FfmpegSceneDetector, FixtureDetector, FrameRef, SceneDetector, ShotBoundary,
                              UnavailableDetector, VisualEvent, load_boundaries, parse_scene_output, save_boundaries)
from .transcription import (FixtureProvider, LanguageSpan, ProviderCapabilities, Transcript, TranscriptSegment,
                            TranscriptionRequest, UnavailableProvider, Word, load_transcript, run_transcription,
                            save_transcript)

__all__ = [n for n in dir() if not n.startswith("_")]
