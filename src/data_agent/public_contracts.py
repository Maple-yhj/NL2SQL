"""Dependency-free public primitives shared across runtime and Agent packages."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, StringConstraints


NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SchemaFingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^(?:sha256:)?[0-9a-f]{64}$"),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class PublicContractModel(ContractModel):
    @classmethod
    def model_construct(
        cls,
        _fields_set: set[str] | None = None,
        **values: object,
    ) -> Self:
        del _fields_set
        return cls.model_validate(values)

    def _revalidated(self) -> Self:
        values = dict(getattr(self, "__dict__", {}))
        extra = getattr(self, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            values.update(extra)
        return type(self).model_validate(values)

    def model_dump(self, **kwargs: object) -> dict[str, object]:
        return BaseModel.model_dump(self._revalidated(), **kwargs)

    def model_dump_json(self, **kwargs: object) -> str:
        return BaseModel.model_dump_json(self._revalidated(), **kwargs)


class AgentMode(StrEnum):
    PLAN = "plan"
    PREVIEW = "preview"
    EXECUTE = "execute"


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
    GRAPH_NO_PATH = "GRAPH_NO_PATH"
    GRAPH_AMBIGUOUS_PATH = "GRAPH_AMBIGUOUS_PATH"
    GRAPH_UNSAFE_FANOUT = "GRAPH_UNSAFE_FANOUT"
    GRAPH_STALE_SNAPSHOT = "GRAPH_STALE_SNAPSHOT"
    GRAPH_REVISION_CONFLICT = "GRAPH_REVISION_CONFLICT"
    GRAPH_VALIDATION_FAILED = "GRAPH_VALIDATION_FAILED"
    RELATIONSHIP_RECOMMENDATION_FAILED = "RELATIONSHIP_RECOMMENDATION_FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    RESULT_SEMANTIC_MISMATCH = "RESULT_SEMANTIC_MISMATCH"
    TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
    CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"
    AGENT_DECISION_INVALID = "AGENT_DECISION_INVALID"
    AGENT_ACTION_NOT_ALLOWED = "AGENT_ACTION_NOT_ALLOWED"
    AGENT_BUDGET_EXCEEDED = "AGENT_BUDGET_EXCEEDED"
    AGENT_MAX_STEPS_EXCEEDED = "AGENT_MAX_STEPS_EXCEEDED"
    AGENT_EVIDENCE_INSUFFICIENT = "AGENT_EVIDENCE_INSUFFICIENT"
    AGENT_RESPONSE_UNGROUNDED = "AGENT_RESPONSE_UNGROUNDED"
    AGENT_WAITING_FOR_INPUT = "AGENT_WAITING_FOR_INPUT"
    AGENT_INTERRUPT_STALE = "AGENT_INTERRUPT_STALE"
    AGENT_RESUME_CONFLICT = "AGENT_RESUME_CONFLICT"
    AGENT_CLARIFICATION_LOOP = "AGENT_CLARIFICATION_LOOP"
    QUERY_UNSUPPORTED = "QUERY_UNSUPPORTED"
    AGENT_ARTIFACT_NOT_FOUND = "AGENT_ARTIFACT_NOT_FOUND"
    AGENT_ARTIFACT_INTEGRITY_ERROR = "AGENT_ARTIFACT_INTEGRITY_ERROR"
    AGENT_CHECKPOINT_UNAVAILABLE = "AGENT_CHECKPOINT_UNAVAILABLE"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AgentError(PublicContractModel):
    """Error safe to expose at an API, CLI, or event boundary."""

    code: ErrorCode
    message: NonBlankText
    retryable: bool = False


__all__ = [
    "AgentError",
    "AgentMode",
    "ContractModel",
    "Digest",
    "ErrorCode",
    "NonBlankText",
    "PublicContractModel",
    "SchemaFingerprint",
]
