"""Edit-plan schema "edit-plan/0.1", validation, capability gate and offline Editor planning (T-EDT-001).

Offline, standard library only. Unit-tested; nothing here is VERIFIED. See README.md.
"""
from ._codec import PlanSchemaError
from .canonical import canonical_dict, canonical_json, plan_hash
from .capability import PlanNotExecutable, build_requests, check_capabilities, to_executor_requests
from .issues import PlanIssue, ValidationReport
from .planner import PlanningResult, plan_from_brief
from .schema import (SCHEMA_VERSION, Caption, EditPlan, ExportTarget, Marker, PlanMeta, QCExpectation, Rate, Rationale,
                     Segment, SourceEntry, TimelineSettings, Track, Transition, TranscriptRange, json_schema)
from .validate import validate_plan, validate_plan_dict

__all__ = ["PlanSchemaError", "canonical_dict", "canonical_json", "plan_hash", "PlanNotExecutable", "build_requests",
           "check_capabilities", "to_executor_requests", "PlanIssue", "ValidationReport", "PlanningResult",
           "plan_from_brief", "SCHEMA_VERSION", "Caption", "EditPlan", "ExportTarget", "Marker", "PlanMeta",
           "QCExpectation", "Rate", "Rationale", "Segment", "SourceEntry", "TimelineSettings", "Track", "Transition",
           "TranscriptRange", "json_schema", "validate_plan", "validate_plan_dict"]
