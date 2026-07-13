"""Strict input/output schemas for the six stable built-in tools."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from data_agent.runtime.binding import BoundQueryPlan, PreparedQuery
from data_agent.skills.models import LogicalQueryPlan

from ..models import ToolModel
from ..schemas import CatalogSnapshot, CellValue, ExplainResult, QueryRow


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SemanticKind(StrEnum):
    ENTITY = "entity"
    FIELD = "field"
    METRIC = "metric"
    RELATIONSHIP = "relationship"
    POLICY = "policy"


class SemanticSearchInput(ToolModel):
    query: NonBlankText
    kinds: tuple[SemanticKind, ...] = ()
    limit: int = Field(default=10, ge=1, le=50)


class SemanticMatch(ToolModel):
    ref: NonBlankText
    kind: SemanticKind
    label: NonBlankText
    description: str = ""
    score: float = Field(ge=0, le=1)


class SemanticSearchOutput(ToolModel):
    matches: tuple[SemanticMatch, ...]


class DataInspectInput(ToolModel):
    relations: tuple[NonBlankText, ...] = ()
    include_statistics: bool = False


class DataInspectOutput(ToolModel):
    catalog: CatalogSnapshot


class QueryCompileInput(ToolModel):
    logical_plan: LogicalQueryPlan


class QueryCompileOutput(ToolModel):
    bound_plan: BoundQueryPlan
    prepared_query: PreparedQuery


class QueryMode(StrEnum):
    EXPLAIN = "explain"
    PREVIEW = "preview"
    EXECUTE = "execute"


class QueryExecuteInput(ToolModel):
    prepared_query: PreparedQuery
    mode: QueryMode
    preview_rows: int = Field(default=20, ge=1, le=100)


class QueryData(ToolModel):
    logical_plan_hash: NonBlankText
    query_hash: NonBlankText
    policy_decision_id: NonBlankText
    verification_token: NonBlankText
    columns: tuple[NonBlankText, ...]
    rows: tuple[QueryRow, ...]

    @model_validator(mode="after")
    def validate_row_width(self) -> "QueryData":
        if any(len(row.values) != len(self.columns) for row in self.rows):
            raise ValueError("query row width must match the columns")
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("query columns must be unique")
        return self


class QueryExecutionOutput(ToolModel):
    mode: QueryMode
    data: QueryData | None = None
    explain: ExplainResult | None = None

    @model_validator(mode="after")
    def validate_mode_payload(self) -> "QueryExecutionOutput":
        if self.mode == QueryMode.EXPLAIN:
            if self.explain is None or self.data is not None:
                raise ValueError("explain mode requires only an explain result")
        elif self.data is None or self.explain is not None:
            raise ValueError("preview and execute modes require only query data")
        return self


class ResultProfileInput(ToolModel):
    data: QueryData


class ColumnProfile(ToolModel):
    name: NonBlankText
    null_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    min_value: CellValue = None
    max_value: CellValue = None


class ResultProfileOutput(ToolModel):
    logical_plan_hash: NonBlankText
    query_hash: NonBlankText
    policy_decision_id: NonBlankText
    row_count: int = Field(ge=0)
    columns: tuple[ColumnProfile, ...]
    warnings: tuple[NonBlankText, ...] = ()


class AnswerRenderInput(ToolModel):
    question: NonBlankText
    data: QueryData
    profile: ResultProfileOutput | None = None


class AnswerRenderOutput(ToolModel):
    answer: NonBlankText
    table_markdown: str = ""
    evidence_query_hash: NonBlankText
    policy_decision_id: NonBlankText
