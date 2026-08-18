"""Stable error contracts for semantic metric resolution and governance."""

from __future__ import annotations

from enum import StrEnum


class SemanticMetricErrorCode(StrEnum):
    INVALID_METRIC_DEFINITION = "INVALID_METRIC_DEFINITION"
    METRIC_UNRESOLVED = "METRIC_UNRESOLVED"
    METRIC_AMBIGUOUS = "METRIC_AMBIGUOUS"
    METRIC_CONFLICT = "METRIC_CONFLICT"
    METRIC_VALIDATION_FAILED = "METRIC_VALIDATION_FAILED"
    METRIC_APPROVAL_REQUIRED = "METRIC_APPROVAL_REQUIRED"
    METRIC_SET_STALE = "METRIC_SET_STALE"
    METRIC_CONTEXT_STALE = "METRIC_CONTEXT_STALE"


class SemanticMetricError(RuntimeError):
    def __init__(self, code: SemanticMetricErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


__all__ = ["SemanticMetricError", "SemanticMetricErrorCode"]
