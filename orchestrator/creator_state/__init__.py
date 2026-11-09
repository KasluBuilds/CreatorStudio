"""Persistent project state, checkpoints, handoff and recovery for AI Creator Studio.

Phase 1 foundation. Standard library only. Nothing here talks to DaVinci Resolve
and nothing here modifies or deletes media; it only records and verifies state.
"""
from .schema import SCHEMA_VERSION, STAGES, STAGE_IDS
from .project import Project, StateError, VerificationError, UncertainOperationError
from . import recovery, handoff

__all__ = [
    "SCHEMA_VERSION", "STAGES", "STAGE_IDS",
    "Project", "StateError", "VerificationError", "UncertainOperationError",
    "recovery", "handoff",
]
