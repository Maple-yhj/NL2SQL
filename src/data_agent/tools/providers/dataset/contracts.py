"""Strict input/output contracts and injected resources for dataset tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from data_agent.analysis_agent.artifacts import SQLiteArtifactStore
from data_agent.analysis_agent.models import (
    AgentArtifactRef,
    DatasetAuthority,
    EvidenceRef,
    Identifier,
)
from data_agent.dataset_query import DatasetQueryCompiler, DatasetQueryExecutor, DatasetQueryPlan
from data_agent.datasources import SemanticBindingRecord, SemanticGraphBindingRecord
from data_agent.dataset_query.contracts import PreparedQuery
from data_agent.tools.connectors import DataSourceConnector
from data_agent.tools.models import CredentialLease, NonBlankText, ToolModel
from data_agent.tools.schemas import CatalogSnapshot


@dataclass(frozen=True, slots=True)
class DatasetToolRuntime:
    authority: DatasetAuthority
    catalog: CatalogSnapshot
    binding: SemanticBindingRecord | SemanticGraphBindingRecord
    connector: DataSourceConnector
    connection_ref: str
    bundle_digest: str
    artifacts: SQLiteArtifactStore
    compiler: DatasetQueryCompiler
    executor: DatasetQueryExecutor


class DatasetCredentialBroker:
    def __init__(self, runtime: DatasetToolRuntime) -> None:
        self._runtime = runtime

    async def acquire(self, *, grant, source: str | None) -> CredentialLease | None:
        if source != self._runtime.authority.source_id:
            return None
        now = datetime.now(UTC)
        return CredentialLease(
            credential_id=f"dataset-broker-{grant.grant_id}",
            grant_id=grant.grant_id,
            bundle_digest=grant.bundle_digest,
            source=source,
            connection_ref=self._runtime.connection_ref,
            capabilities=(grant.tool_name,),
            secret="composition-injected-readonly-lease",
            issued_at=now,
            expires_at=grant.expires_at,
        )


class EmptyInput(ToolModel):
    pass


class ArtifactInput(ToolModel):
    artifact_id: NonBlankText


class RelationshipRouteInput(ToolModel):
    logical_refs: tuple[NonBlankText, ...] = Field(min_length=1)


class QueryCompileInput(ToolModel):
    plan: DatasetQueryPlan


class QueryRunInput(ToolModel):
    artifact_id: NonBlankText
    preview_rows: int = Field(default=20, ge=1, le=100)


class ComputationSpec(ToolModel):
    operation: Literal[
        "describe",
        "quantiles",
        "correlation",
        "growth_rate",
        "moving_average",
        "rank",
        "outlier_iqr",
    ]
    artifact_id: NonBlankText
    fields: tuple[NonBlankText, ...] = Field(min_length=1)
    partition_by: tuple[NonBlankText, ...] = ()
    order_by: tuple[NonBlankText, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("fields", "partition_by", "order_by")
    @classmethod
    def unique_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("computation field references must be unique")
        return values

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "ComputationSpec":
        if self.operation == "correlation" and len(self.fields) != 2:
            raise ValueError("correlation requires exactly two fields")
        allowed_parameters = {"window"} if self.operation == "moving_average" else set()
        unknown = set(self.parameters) - allowed_parameters
        if unknown:
            raise ValueError("unsupported computation parameters")
        if "window" in self.parameters:
            window = self.parameters["window"]
            if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 100:
                raise ValueError("moving average window must be an integer from 1 to 100")
        if self.partition_by or self.order_by:
            raise ValueError("partitioned computation is not available in this registry version")
        return self


class ChartRenderInput(ToolModel):
    artifact_id: NonBlankText
    title: NonBlankText
    x_field: NonBlankText
    y_field: NonBlankText


class EvidenceCollectInput(ToolModel):
    artifact_id: NonBlankText
    claim_key: Identifier
    field_refs: tuple[NonBlankText, ...] = Field(min_length=1)
    sql_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DatasetArtifactOutput(ToolModel):
    summary: NonBlankText
    artifact: AgentArtifactRef
    safe_preview: JsonValue


class QueryCompileOutput(DatasetArtifactOutput):
    logical_plan_hash: NonBlankText
    query_hash: NonBlankText
    policy_decision_id: NonBlankText


class PreparedQueryArtifactPayload(ToolModel):
    plan: DatasetQueryPlan
    prepared: PreparedQuery


class EvidenceCollectOutput(ToolModel):
    summary: NonBlankText
    artifact: AgentArtifactRef
    evidence: EvidenceRef


def prepared_from_payload(payload: JsonValue) -> PreparedQuery:
    if isinstance(payload, dict) and "prepared" in payload:
        return PreparedQueryArtifactPayload.model_validate(payload).prepared
    return PreparedQuery.model_validate(payload)


def dataset_plan_from_payload(payload: JsonValue) -> DatasetQueryPlan | None:
    if not isinstance(payload, dict) or "prepared" not in payload:
        return None
    return PreparedQueryArtifactPayload.model_validate(payload).plan


__all__ = [
    "ArtifactInput",
    "ChartRenderInput",
    "ComputationSpec",
    "DatasetArtifactOutput",
    "DatasetCredentialBroker",
    "DatasetToolRuntime",
    "EmptyInput",
    "EvidenceCollectInput",
    "EvidenceCollectOutput",
    "QueryCompileInput",
    "QueryCompileOutput",
    "PreparedQueryArtifactPayload",
    "QueryRunInput",
    "RelationshipRouteInput",
    "prepared_from_payload",
    "dataset_plan_from_payload",
]
