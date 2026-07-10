"""Stable request, identity, budget, and response contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .errors import AgentError


NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class ContractModel(BaseModel):
    """Strict base for public contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentMode(StrEnum):
    PLAN = "plan"
    PREVIEW = "preview"
    EXECUTE = "execute"


class AgentRequest(ContractModel):
    question: NonBlankText
    enterprise_id: NonBlankText = "olist"
    domain_id: NonBlankText = "commerce"
    conversation_id: NonBlankText | None = None
    mode: AgentMode = AgentMode.EXECUTE
    requested_output: NonBlankText = "answer"
    include_trace: bool = False


class PrincipalContext(ContractModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    roles: tuple[NonBlankText, ...] = ()


class RunBudget(ContractModel):
    max_tool_calls: int = Field(default=24, ge=1)
    max_correction_rounds: int = Field(default=2, ge=0)
    max_sql_compile_attempts: int = Field(default=3, ge=1)
    max_duration_seconds: int = Field(default=120, ge=1)
    max_result_rows: int = Field(default=1000, ge=1)


class AgentResponse(ContractModel):
    ok: bool
    question: NonBlankText
    contextualized_question: str | None = None
    conversation_id: str | None = None
    tenant_id: str | None = None
    logical_plan: dict[str, Any] | None = None
    sql: str | None = None
    message_type: str = "text"
    rows: list[dict[str, Any]] = Field(default_factory=list)
    answer: str | None = None
    error: AgentError | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    pending_memory_updates: list[dict[str, Any]] = Field(default_factory=list)
