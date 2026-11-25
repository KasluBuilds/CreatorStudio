"""Structured validation output: issues with stable codes, and the report that carries them."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ERROR, WARNING = "error", "warning"


@dataclass(frozen=True)
class PlanIssue:
    severity: str          # "error" blocks the plan; "warning" must be read by a reviewer
    code: str              # stable, upper-case, documented in the README
    path: str              # JSON-pointer-like location in the plan ("/segments/3/source_out")
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "path": self.path, "message": self.message}


def error(code: str, path: str, message: str) -> PlanIssue:
    return PlanIssue(ERROR, code, path, message)


def warning(code: str, path: str, message: str) -> PlanIssue:
    return PlanIssue(WARNING, code, path, message)


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[PlanIssue, ...] = ()
    total_duration_frames: int = 0                         # timeline frames, from frame 0 to the end of the last item
    gaps: tuple[dict[str, Any], ...] = ()                  # {"track_id", "start", "end"}: half-open timeline frames
    plan_hash: str | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def errors(self) -> tuple[PlanIssue, ...]:
        return tuple(i for i in self.issues if i.severity == ERROR)

    @property
    def warnings(self) -> tuple[PlanIssue, ...]:
        return tuple(i for i in self.issues if i.severity == WARNING)

    @property
    def ok(self) -> bool:
        """True only when there are no errors. Warnings do not block, but they are never hidden."""
        return not self.errors

    def codes(self, severity: str | None = None) -> list[str]:
        return [i.code for i in self.issues if severity is None or i.severity == severity]

    def has(self, code: str) -> bool:
        return any(i.code == code for i in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error_count": len(self.errors), "warning_count": len(self.warnings),
                "total_duration_frames": self.total_duration_frames, "gaps": [dict(g) for g in self.gaps],
                "plan_hash": self.plan_hash, "notes": list(self.notes),
                "issues": [i.to_dict() for i in self.issues]}
