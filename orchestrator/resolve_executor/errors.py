"""Failure classification and recovery recommendations.

Every failure the executor reports carries one `FailureClass`. The class decides two things that must never be
guessed at the call site: whether a retry is ever allowed, and what a human or agent should do next.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Optional


class FailureClass(str, Enum):
    VALIDATION = "validation"                    # the request is wrong; nothing was sent to Resolve
    UNSUPPORTED = "unsupported"                  # the capability does not exist or is not proven; nothing sent
    IDENTITY_MISMATCH = "identity_mismatch"      # wrong project/timeline/rate open; nothing sent
    PRECONDITION = "precondition"                # target state does not allow the operation (overlap, missing track)
    BRIDGE_UNAVAILABLE = "bridge_unavailable"    # could not connect; nothing reached Resolve
    BRIDGE_TIMEOUT = "bridge_timeout"            # request sent, no answer: outcome UNKNOWN until inspected
    BRIDGE_PROTOCOL = "bridge_protocol"          # malformed or refused reply
    RESOLVE_REFUSED = "resolve_refused"          # the API returned False/None; readback decides what changed
    READBACK_MISMATCH = "readback_mismatch"      # the call "succeeded" but the timeline is not what was requested
    INTERRUPTED = "interrupted"                  # a mutation may or may not have been applied
    UNCERTAIN_PREVIOUS_ATTEMPT = "uncertain_previous_attempt"  # journal shows a started, unfinished attempt
    RENDER_FAILED = "render_failed"
    RENDER_TIMEOUT = "render_timeout"
    EXPORT_INVALID = "export_invalid"            # the file is missing, incomplete or does not match the spec
    INTERNAL = "internal"


RECOVERY: Mapping[FailureClass, str] = {
    FailureClass.VALIDATION: "Fix the request; nothing was sent to Resolve.",
    FailureClass.UNSUPPORTED: "Do not work around it with raw API calls. Use a supported operation, do it in the Resolve UI, or prove the capability with a live test first.",
    FailureClass.IDENTITY_MISMATCH: "Open the expected project/timeline (or correct the request); nothing was changed.",
    FailureClass.PRECONDITION: "Inspect the timeline and adjust the plan (free the slot, add the track); nothing was changed.",
    FailureClass.BRIDGE_UNAVAILABLE: "Start Workspace > Scripts > resolve_bridge inside Resolve and retry; nothing reached Resolve.",
    FailureClass.BRIDGE_TIMEOUT: "Check Resolve for a modal dialog. Then INSPECT the timeline before anything else: the request may have been applied. Never resend it blindly.",
    FailureClass.BRIDGE_PROTOCOL: "Reinstall or restart the bridge so client and bridge versions match; inspect state before retrying a mutation.",
    FailureClass.RESOLVE_REFUSED: "Resolve rejected the call. The readback shows what changed (usually nothing); fix the cause rather than retrying.",
    FailureClass.READBACK_MISMATCH: "The timeline differs from the request. Inspect the recorded readback, remove the wrong item if needed, and re-plan; do not stack another attempt on top.",
    FailureClass.INTERRUPTED: "Inspect the actual Resolve state, then record whether the operation was applied (Executor.resolve_uncertain). Never repeat it automatically.",
    FailureClass.UNCERTAIN_PREVIOUS_ATTEMPT: "A previous attempt with this idempotency key started but never finished. Inspect Resolve and record the outcome with Executor.resolve_uncertain before trying again.",
    FailureClass.RENDER_FAILED: "Read the job's Error text, fix settings or media, and queue a NEW job; do not reuse the failed job's output.",
    FailureClass.RENDER_TIMEOUT: "Rendering was stopped. Discard the partial file and investigate (dialog, offline media, disk space) before a new job.",
    FailureClass.EXPORT_INVALID: "Do not deliver the file. Compare the export report with the spec, fix the cause, and render again.",
    FailureClass.INTERNAL: "A bug in the executor; inspect state before continuing.",
}

# Classes whose operation certainly did NOT reach Resolve: safe to retry without inspection (bounded).
NOTHING_SENT = frozenset({FailureClass.VALIDATION, FailureClass.UNSUPPORTED, FailureClass.IDENTITY_MISMATCH,
                          FailureClass.PRECONDITION, FailureClass.BRIDGE_UNAVAILABLE})
# Transient transport classes: a READ may be retried; a MUTATION only after readback proved it was not applied.
TRANSIENT = frozenset({FailureClass.BRIDGE_UNAVAILABLE, FailureClass.BRIDGE_TIMEOUT})


class ExecutorError(Exception):
    """A classified failure. `details` must be JSON-serializable and never contain credentials."""

    def __init__(self, failure: FailureClass, message: str, details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(f"{failure.value}: {message}")
        self.failure = failure
        self.message = message
        self.details = dict(details or {})

    @property
    def recovery(self) -> str:
        return RECOVERY[self.failure]

    def to_dict(self) -> dict:
        return {"failure": self.failure.value, "message": self.message, "details": self.details,
                "recovery": self.recovery}


class AdapterError(ExecutorError):
    """Raised by an adapter for transport problems (never for a plain False from Resolve)."""
