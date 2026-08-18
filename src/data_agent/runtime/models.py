"""Stable request, identity, budget, and response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    model_validator,
)

from data_agent.analysis_agent.models import (
    AgentArtifactKind,
    AnalysisPlan,
)
from data_agent.dataset_query.contracts import LogicalQueryPlan

from .base import (
    AgentMode,
    ContractModel,
    Digest,
    NonBlankText,
    PublicContractModel,
    SchemaFingerprint,
)
from .errors import AgentError


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


class DatasetRuntimeVersionPins(PublicContractModel):
    kind: Literal["dataset"] = "dataset"
    runtime_version: NonBlankText
    graph_id: NonBlankText
    graph_version: NonBlankText
    graph_digest: Digest
    tool_registry_version: NonBlankText
    analysis_skill_id: NonBlankText = "dataset.analytics"
    analysis_skill_version: NonBlankText = "1.0.0"
    domain_pack_id: NonBlankText | None = None
    domain_pack_version: NonBlankText | None = None
    domain_pack_digest: Digest | None = None
    model_versions: tuple[ComponentVersionPin, ...] = Field(min_length=1)
    source_id: NonBlankText
    source_version: int = Field(ge=1)
    binding_id: NonBlankText
    binding_version: int = Field(ge=1)
    metric_set_id: NonBlankText | None = None
    metric_set_version: int | None = Field(default=None, ge=1)
    metric_set_digest: Digest | None = None
    metric_overlay_id: NonBlankText | None = None
    metric_overlay_digest: Digest | None = None
    schema_fingerprint: SchemaFingerprint
    relationship_graph_digest: Digest | None = None

    @model_validator(mode="after")
    def validate_unique_model_components(self) -> "DatasetRuntimeVersionPins":
        components = tuple(item.component for item in self.model_versions)
        if len(components) != len(set(components)):
            raise ValueError("runtime version pin components must be unique")
        domain_pack_values = (
            self.domain_pack_id,
            self.domain_pack_version,
            self.domain_pack_digest,
        )
        if any(value is not None for value in domain_pack_values) and not all(
            value is not None for value in domain_pack_values
        ):
            raise ValueError(
                "domain pack id, version, and digest must be pinned together"
            )
        return self


RuntimeVersionPins = DatasetRuntimeVersionPins


class AnalysisStepSummary(PublicContractModel):
    step_id: NonBlankText
    objective: NonBlankText
    status: Literal[
        "pending",
        "running",
        "completed",
        "blocked",
        "skipped",
        "waiting_input",
        "failed",
    ]
    tool_names: tuple[NonBlankText, ...] = ()
    evidence_ids: tuple[NonBlankText, ...] = ()

    @model_validator(mode="after")
    def validate_unique_refs(self) -> "AnalysisStepSummary":
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("step tool names must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("step evidence ids must be unique")
        return self


class AgentArtifactSummary(PublicContractModel):
    artifact_id: NonBlankText
    kind: AgentArtifactKind
    digest: Digest
    row_count: int | None = Field(default=None, ge=0)
    sensitivity: Literal["metadata", "derived", "row_data"]
    created_at: datetime


class EvidenceSummary(PublicContractModel):
    evidence_id: NonBlankText
    claim_key: NonBlankText
    artifact_id: NonBlankText
    field_refs: tuple[NonBlankText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> "EvidenceSummary":
        if len(self.field_refs) != len(set(self.field_refs)):
            raise ValueError("evidence summary field refs must be unique")
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
    logical_plan: LogicalQueryPlan | None = None
    dataset_query_plan: dict[str, JsonValue] | None = None
    sql: str | None = None
    rows: tuple[AgentRow, ...] = ()
    chart: ChartSpec | None = None
    answer: str | None = None
    ok: bool | None = None
    error: AgentError | None = None
    error_code: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    trace: tuple[AgentTraceEntry, ...] = ()
    pending_memory_updates: tuple[ProposalSummary, ...] = ()
    version_pins: RuntimeVersionPins | None = None
    analysis_plan: AnalysisPlan | None = None
    analysis_steps: tuple[AnalysisStepSummary, ...] = ()
    artifacts: tuple[AgentArtifactSummary, ...] = ()
    evidence: tuple[EvidenceSummary, ...] = ()
    limitations: tuple[NonBlankText, ...] = ()


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
    dataset_query_plan: dict[str, JsonValue] | None = None
    sql: str | None = None
    message_type: NonBlankText = "text"
    rows: tuple[AgentRow, ...] = ()
    chart: ChartSpec | None = None
    answer: str | None = None
    error: AgentError | None = None
    trace: tuple[AgentTraceEntry, ...] = ()
    pending_memory_updates: tuple[ProposalSummary, ...] = ()
    version_pins: RuntimeVersionPins | None = None
    analysis_plan: AnalysisPlan | None = None
    analysis_steps: tuple[AnalysisStepSummary, ...] = ()
    artifacts: tuple[AgentArtifactSummary, ...] = ()
    evidence: tuple[EvidenceSummary, ...] = ()
    limitations: tuple[NonBlankText, ...] = ()

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
        if self.analysis_plan is not None:
            object.__setattr__(
                self,
                "analysis_plan",
                AnalysisPlan.model_validate(self.analysis_plan),
            )
        object.__setattr__(
            self,
            "analysis_steps",
            tuple(AnalysisStepSummary.model_validate(item) for item in self.analysis_steps),
        )
        object.__setattr__(
            self,
            "artifacts",
            tuple(AgentArtifactSummary.model_validate(item) for item in self.artifacts),
        )
        object.__setattr__(
            self,
            "evidence",
            tuple(EvidenceSummary.model_validate(item) for item in self.evidence),
        )
        for values, field_name in (
            (tuple(item.step_id for item in self.analysis_steps), "analysis step ids"),
            (tuple(item.artifact_id for item in self.artifacts), "artifact ids"),
            (tuple(item.evidence_id for item in self.evidence), "evidence ids"),
            (self.limitations, "limitations"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        artifact_ids = {item.artifact_id for item in self.artifacts}
        if artifact_ids:
            for item in self.evidence:
                if item.artifact_id not in artifact_ids:
                    raise ValueError("evidence summary must reference a response artifact")
        evidence_ids = {item.evidence_id for item in self.evidence}
        if evidence_ids:
            for step in self.analysis_steps:
                if not set(step.evidence_ids).issubset(evidence_ids):
                    raise ValueError("step summary references unknown evidence")
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
