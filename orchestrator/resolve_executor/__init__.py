"""Safe, validated DaVinci Resolve execution layer (task T-RSV-002, milestone M-RSV-02).

Editor plan -> typed `operations` -> `validation` (pure) -> `Executor` (capability gate, idempotency journal,
identity checks, preconditions) -> `adapter.ResolveAdapter` (`live.LiveResolveAdapter` over the local bridge, or
`fake.FakeResolve` offline) -> read-after-write verification -> journal record. Renders are verified from the file
(`probe`). Frame arithmetic lives in `timebase`. Package notes: `README.md`.

Standard library only. The validation layer never calls Resolve; nothing here exposes the raw scripting API.
"""

from .capabilities import CAPABILITIES, CAPABILITY_BY_NAME, IMPLEMENTED, NOT_TESTED, UNSUPPORTED, VERIFIED, capability_for
from .errors import AdapterError, ExecutorError, FailureClass, RECOVERY
from .executor import Executor, OperationResult, Outcome
from .journal import Journal, MemoryJournal
from .operations import (AddMarker, AddSubtitle, AddTrack, AddTransition, AppendSegment, CreateProject, CreateTimeline,
                         DeleteItems, ImportMedia, InspectProject, InspectTimeline, ItemRef, PlaceTitle, RenderSpec,
                         RenderTimeline, SaveProject, SetClipAudioLevel, SetTransform, VerifyExport)
from .timebase import FrameRange, FrameRate, TimebaseError, parse_rate
from .validation import Policy, validate

__all__ = [
    "AdapterError", "AddMarker", "AddSubtitle", "AddTrack", "AddTransition", "AppendSegment", "CAPABILITIES",
    "CAPABILITY_BY_NAME", "CreateProject", "CreateTimeline", "DeleteItems", "Executor", "ExecutorError",
    "FailureClass", "FrameRange", "FrameRate", "IMPLEMENTED", "ImportMedia", "InspectProject", "InspectTimeline",
    "ItemRef", "Journal", "MemoryJournal", "NOT_TESTED", "OperationResult", "Outcome", "PlaceTitle", "Policy",
    "RECOVERY", "RenderSpec", "RenderTimeline", "SaveProject", "SetClipAudioLevel", "SetTransform", "TimebaseError",
    "UNSUPPORTED", "VERIFIED", "VerifyExport", "capability_for", "parse_rate", "validate",
]
