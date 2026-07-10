"""Stable, safe error contracts for runtime boundaries."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    CONFIG_INVALID = "CONFIG_INVALID"
    BUNDLE_NOT_FOUND = "BUNDLE_NOT_FOUND"
    LOGICAL_PLAN_INVALID = "LOGICAL_PLAN_INVALID"
    BINDING_STALE = "BINDING_STALE"
    SQL_COMPILE_ERROR = "SQL_COMPILE_ERROR"
    SQL_POLICY_VIOLATION = "SQL_POLICY_VIOLATION"
    COST_EXCEEDED = "COST_EXCEEDED"
    EMPTY_RESULT = "EMPTY_RESULT"
    JOIN_EXPLOSION = "JOIN_EXPLOSION"
    ACCESS_DENIED = "ACCESS_DENIED"
    RESULT_SEMANTIC_MISMATCH = "RESULT_SEMANTIC_MISMATCH"
    TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AgentError(BaseModel):
    """Error safe to expose at an API, CLI, or event boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: NonBlankText
    retryable: bool = False
