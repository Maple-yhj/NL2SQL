"""Stable request, identity, budget, and response contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StringConstraints,
    model_validator,
)

from data_agent.skills.models import LogicalQueryPlan

from .errors import AgentError


NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ContractModel(BaseModel):
    """Strict base for public contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class PublicContractModel(ContractModel):
    """Public boundary that cannot use Pydantic's unchecked construction path."""

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


class AgentRequest(ContractModel):
    question: NonBlankText
    enterprise_id: NonBlankText = "user-dataset"
    domain_id: NonBlankText = "dataset"
    conversation_id: NonBlankText | None = None
    source_id: NonBlankText | None = None
    source_version: int | None = Field(default=None, ge=1)
    binding_id: NonBlankText | None = None
    binding_version: int | None = Field(default=None, ge=1)
    mode: AgentMode = AgentMode.EXECUTE
    requested_output: NonBlankText = "answer"
    include_trace: bool = False

    @model_validator(mode="after")
    def validate_datasource_pins(self) -> "AgentRequest":
        pins = (
            self.source_id,
            self.source_version,
            self.binding_id,
            self.binding_version,
        )
        if any(item is not None for item in pins) and not all(
            item is not None for item in pins
        ):
            raise ValueError(
                "source_id, source_version, binding_id, and binding_version "
                "must be supplied together"
            )
        return self


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


class AgentRow(RootModel[dict[NonBlankText, JsonValue]]):
    model_config = ConfigDict(frozen=True, revalidate_instances="always")


class ChartSpec(PublicContractModel):
    chart_type: Literal["bar"] = "bar"
    title: NonBlankText
    x_field: NonBlankText
    y_field: NonBlankText


class AgentTraceEntry(PublicContractModel):
    node: NonBlankText
    status: NonBlankText
    error_code: NonBlankText | None = None


class ProposalSummary(PublicContractModel):
    scope: Literal["working", "conversation", "user", "episodic", "enterprise"]
    source: NonBlankText
    status: Literal["pending_approval"] = "pending_approval"


class ComponentVersionPin(PublicContractModel):
    component: NonBlankText
    version: NonBlankText


class RuntimeVersionPins(PublicContractModel):
    bundle_digest: Digest
    runtime_version: NonBlankText
    domain_pack_digest: Digest
    enterprise_binding_digest: Digest
    deployment_profile_digest: Digest
    schema_fingerprint: Digest
    skill_id: NonBlankText
    skill_version: NonBlankText
    graph_id: NonBlankText
    graph_version: NonBlankText
    graph_digest: Digest
    tool_registry_version: NonBlankText
    tool_versions: tuple[ComponentVersionPin, ...] = Field(min_length=1)
    model_versions: tuple[ComponentVersionPin, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_components(self) -> "RuntimeVersionPins":
        for pins in (self.tool_versions, self.model_versions):
            components = tuple(item.component for item in pins)
            if len(components) != len(set(components)):
                raise ValueError("runtime version pin components must be unique")
        return self


class ConversationSummary(PublicContractModel):
    tenant_id: NonBlankText
    user_id: NonBlankText
    domain_id: NonBlankText
    conversation_id: NonBlankText
    title: str = ""
    archived: bool = False
    created_at: datetime
    updated_at: datetime


class ConversationMessageMetadata(PublicContractModel):
    message_type: NonBlankText = "text"
    contextualized_question: str | None = None
    answer: str | None = None
    ok: bool | None = None
    error_code: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    trace: tuple[AgentTraceEntry, ...] = ()


class ConversationMessage(PublicContractModel):
    role: Literal["user", "assistant", "system"]
    content: str
    metadata: ConversationMessageMetadata = Field(
        default_factory=ConversationMessageMetadata
    )


class AgentResponse(PublicContractModel):
    ok: bool
    question: NonBlankText
    contextualized_question: str | None = None
    conversation_id: str | None = None
    tenant_id: str | None = None
    logical_plan: LogicalQueryPlan | None = None
    sql: str | None = None
    message_type: NonBlankText = "text"
    rows: tuple[AgentRow, ...] = ()
    chart: ChartSpec | None = None
    answer: str | None = None
    error: AgentError | None = None
    trace: tuple[AgentTraceEntry, ...] = ()
    pending_memory_updates: tuple[ProposalSummary, ...] = ()
    version_pins: RuntimeVersionPins | None = None

    @model_validator(mode="after")
    def validate_response_state(self) -> "AgentResponse":
        if self.ok == (self.error is not None):
            raise ValueError("successful responses omit errors and failed responses require one")
        if self.logical_plan is not None:
            raw_plan = dict(getattr(self.logical_plan, "__dict__", {}))
            plan_extra = getattr(self.logical_plan, "__pydantic_extra__", None)
            if isinstance(plan_extra, dict):
                raw_plan.update(plan_extra)
            object.__setattr__(
                self,
                "logical_plan",
                LogicalQueryPlan.model_validate(raw_plan),
            )
        object.__setattr__(
            self,
            "rows",
            tuple(AgentRow.model_validate(row.root) for row in self.rows),
        )
        if self.chart is not None:
            chart = ChartSpec.model_validate(dict(self.chart.__dict__))
            columns = {
                column
                for row in self.rows
                for column in row.root
            }
            if not self.rows or not {
                chart.x_field,
                chart.y_field,
            }.issubset(columns):
                raise ValueError(
                    "chart fields must reference returned result columns"
                )
            if not any(
                _is_finite_chart_number(row.root.get(chart.y_field))
                for row in self.rows
            ):
                raise ValueError("chart y_field requires numeric result values")
            object.__setattr__(self, "chart", chart)
        object.__setattr__(
            self,
            "trace",
            tuple(
                AgentTraceEntry.model_validate(dict(item.__dict__))
                for item in self.trace
            ),
        )
        object.__setattr__(
            self,
            "pending_memory_updates",
            tuple(
                ProposalSummary.model_validate(dict(item.__dict__))
                for item in self.pending_memory_updates
            ),
        )
        if self.error is not None:
            raw_error = dict(getattr(self.error, "__dict__", {}))
            error_extra = getattr(self.error, "__pydantic_extra__", None)
            if isinstance(error_extra, dict):
                raw_error.update(error_extra)
            object.__setattr__(self, "error", AgentError.model_validate(raw_error))
        return self


def _is_finite_chart_number(value: JsonValue | None) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == value and value not in {float("inf"), float("-inf")}
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return False
        return parsed == parsed and parsed not in {float("inf"), float("-inf")}
    return False
