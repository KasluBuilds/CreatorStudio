"""Creative Director foundation (Phase 5). PROPOSED design + offline implementation.

Turns transcript material (+ optional Creator DNA view and Research packet) into a versioned,
evidence-linked STORY BRIEF, and hands a validated EDITOR VIEW to a future Editor Agent. It is not the
Editor Agent and not the shared edit-plan schema. It writes nothing, calls nothing external, and is not
wired into the pipeline or `creator_state`. Tested on synthetic data only.

Creator DNA is informational here: it supplies labelled considerations, not structural control.
"""
from .analysis import Analysis, analyze, depends_on_context
from .director import BriefInvalid, CreativeDirector, LoadedBrief
from .editor_view import (EditorView, build_editor_view, editor_view_from_dict, validate_editor_view_dict)
from .inputs import (Annotation, CallbackAnnotation, ClaimLink, CreatorProfileView, EvidenceError, Judgement,
                     Material, MaterialError, ResearchContext, Segment, TranscriptIndex, material_digest)
from .safety import validate_brief_dict
from .schema import (DNA_NOTICE, EDITOR_VIEW_CONTRACT, SCHEMA_VERSION, Mode, MomentKind, StoryBrief)
from .structure import DirectorConfig
from .textsafety import attribution_risk

__all__ = [n for n in dir() if not n.startswith("_")]
