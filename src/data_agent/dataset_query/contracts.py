"""Reusable logical-plan and prepared-query contracts for dataset analysis."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, FiniteFloat, model_validator
from sqlglot import exp, parse

from data_agent.public_contracts import NonBlankText, PublicContractModel


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class LogicalContractModel(PublicContractModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        revalidate_instances="always",
        validate_default=True,
    )

    def model_copy(
        self,
        *,
        update: dict[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        unknown = set(update) - set(type(self).model_fields)
        if unknown:
            raise ValueError(
                "model_copy update contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        values = {
            name: deepcopy(getattr(self, name)) if deep else getattr(self, name)
            for name in type(self).model_fields
        }
        values.update(deepcopy(update) if deep else update)
        return type(self).model_validate(values)


class PreparedQueryModel(PublicContractModel):
    def model_copy(
        self,
        *,
        update: dict[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        unknown = set(update) - set(type(self).model_fields)
        if unknown:
            raise ValueError(
                "model_copy update contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        values = {
            name: deepcopy(getattr(self, name)) if deep else getattr(self, name)
            for name in type(self).model_fields
        }
        values.update(deepcopy(update) if deep else update)
        return type(self).model_validate(values)


class AnalysisType(StrEnum):
    METRIC = "metric"
    TREND = "trend"
    RANKING = "ranking"
    DETAIL = "detail"
    COMPARISON = "comparison"
    CROSS_TAB = "cross_tab"
    DISTRIBUTION = "distribution"
    DERIVED = "derived"
    FOLLOW_UP = "follow_up"
    TENANT_SCOPED = "tenant_scoped"


class ResultShape(StrEnum):
    SCALAR = "scalar"
    TABLE = "table"
    TIME_SERIES = "time_series"
    RANKING = "ranking"
    DETAIL = "detail"
    CROSS_TAB = "cross_tab"
    DISTRIBUTION = "distribution"


class FilterOperator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    CONTAINS = "contains"


class CalculationOperation(StrEnum):
    SUM = "sum"
    AVERAGE = "average"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    GROWTH = "growth"
    LAG = "lag"
    DATE_DIFFERENCE = "date_difference"
    COMPOSITE_KEY = "composite_key"


class TimeGrain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


PlanScalar = NonBlankText | int | FiniteFloat | bool


class LogicalFilter(LogicalContractModel):
    ref: NonBlankText
    operator: FilterOperator
    value: PlanScalar | tuple[PlanScalar, ...] | None = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> "LogicalFilter":
        if self.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            if self.value is not None:
                raise ValueError("null predicates must not define a value")
        elif self.value is None:
            raise ValueError("predicate requires a value")
        elif self.operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
            if not isinstance(self.value, tuple) or not self.value:
                raise ValueError("set predicates require a non-empty value list")
        elif isinstance(self.value, tuple):
            raise ValueError("scalar predicates require a scalar value")
        return self


class TimeRange(LogicalContractModel):
    field: NonBlankText
    start: NonBlankText | None = None
    end: NonBlankText | None = None


class LogicalOrdering(LogicalContractModel):
    ref: NonBlankText
    direction: SortDirection


class SeriesAxis(LogicalContractModel):
    kind: Literal["time", "numeric"]
    field: NonBlankText
    time_grain: TimeGrain | None = None

    @model_validator(mode="after")
    def validate_axis_grain(self) -> "SeriesAxis":
        if self.kind == "time" and self.time_grain is None:
            raise ValueError("time series axis requires a time grain")
        if self.kind == "numeric" and self.time_grain is not None:
            raise ValueError("numeric series axis cannot define a time grain")
        return self


class RankingSpec(LogicalContractModel):
    mode: Literal["top_n", "full"]
    measure: NonBlankText


class CrossTabSpec(LogicalContractModel):
    row_axis: NonBlankText
    column_axis: NonBlankText
    values: tuple[NonBlankText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_axes(self) -> "CrossTabSpec":
        if self.row_axis == self.column_axis:
            raise ValueError("cross-tab row and column axes must be independent")
        if len(self.values) != len(set(self.values)):
            raise ValueError("cross-tab values must be unique")
        return self


class DerivedCalculation(LogicalContractModel):
    id: NonBlankText
    operation: CalculationOperation
    inputs: tuple[NonBlankText, ...] = Field(min_length=1)
    partition_by: tuple[NonBlankText, ...] = ()


class WindowSpec(LogicalContractModel):
    id: NonBlankText
    calculation: NonBlankText
    axis_ref: NonBlankText
    partition_by: tuple[NonBlankText, ...] = ()
    ordering: tuple[LogicalOrdering, ...] = Field(min_length=1)
    output_grain: tuple[NonBlankText, ...] = Field(min_length=1)


class GrainAlignment(LogicalContractModel):
    source_entity: NonBlankText
    target_entity: NonBlankText
    strategy: Literal["pre_aggregate", "distinct"]
    relationship_path: tuple[NonBlankText, ...] = Field(min_length=1)
    join_grain: tuple[NonBlankText, ...] = Field(min_length=1)


class PlanContext(LogicalContractModel):
    mode: Literal["standalone", "follow_up"] = "standalone"
    tenant_scope: Literal["all", "seller"] = "all"
    prior_question: NonBlankText | None = None
    preserve: tuple[
        Literal["metrics", "filters", "time_range", "tenant_scope", "grain"],
        ...,
    ] = ()


EvidenceKind = Literal[
    "semantic_resolution",
    "logical_plan",
    "query_result",
    "result_profile",
    "calculation_trace",
]


class RelationshipRouteEvidence(LogicalContractModel):
    route_digest: NonBlankText
    logical_node_ids: tuple[NonBlankText, ...]
    edge_ids: tuple[NonBlankText, ...]
    cardinality_by_node: tuple[
        tuple[NonBlankText, Literal["one", "many", "unknown"]], ...
    ]
    fanout_decision: NonBlankText
    preaggregation_required: bool = False


class LogicalQueryPlan(LogicalContractModel):
    analysis_type: AnalysisType
    metrics: tuple[NonBlankText, ...] = ()
    entities: tuple[NonBlankText, ...] = ()
    relationships: tuple[NonBlankText, ...] = ()
    dimensions: tuple[NonBlankText, ...] = ()
    fields: tuple[NonBlankText, ...] = ()
    filters: tuple[LogicalFilter, ...] = ()
    time_range: TimeRange | None = None
    time_grain: TimeGrain | None = None
    series_axis: SeriesAxis | None = None
    ordering: tuple[LogicalOrdering, ...] = ()
    limit: int | None = Field(default=None, ge=1)
    ranking: RankingSpec | None = None
    cross_tab: CrossTabSpec | None = None
    expected_grain: tuple[NonBlankText, ...] = ()
    assumptions: tuple[NonBlankText, ...] = ()
    relationship_evidence: RelationshipRouteEvidence | None = None
    requested_evidence: tuple[EvidenceKind, ...] = ()
    derived_calculations: tuple[DerivedCalculation, ...] = ()
    having: tuple[LogicalFilter, ...] = ()
    window_specs: tuple[WindowSpec, ...] = ()
    grain_alignment: tuple[GrainAlignment, ...] = ()
    result_shape: ResultShape
    context: PlanContext = Field(default_factory=PlanContext)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True, warnings=False),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def stable_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


ParameterScalar = str | int | float | bool
SqlDialect = Literal["postgres", "sqlite", "duckdb"]


class QueryParameter(PreparedQueryModel):
    position: int = Field(ge=1)
    value: ParameterScalar
    logical_type: NonBlankText | None = None
    purpose: Literal[
        "filter",
        "time_start",
        "time_end",
        "tenant_context",
        "tenant_scope",
        "binding_constant",
        "enum_mapping",
        "algorithm_constant",
        "limit",
    ]


def _statement_relations(statement: exp.Expression) -> tuple[str, ...]:
    relations: list[str] = []
    for table in statement.find_all(exp.Table):
        relation = f"{table.db}.{table.name}" if table.db else table.name
        if relation not in relations:
            relations.append(relation)
    return tuple(relations)


class PreparedQuery(PreparedQueryModel):
    dialect: SqlDialect = "postgres"
    logical_plan: LogicalQueryPlan
    logical_plan_hash: NonBlankText
    sql_ast_hash: NonBlankText
    logical_sql: NonBlankText
    executable_sql: NonBlankText
    parameters: tuple[QueryParameter, ...]
    allowed_relations: tuple[NonBlankText, ...] = Field(min_length=1)
    policy_decision_id: NonBlankText
    estimated_cost: float | None = Field(default=None, ge=0)
    max_rows: int = Field(ge=1)
    bundle_digest: NonBlankText
    schema_fingerprint: NonBlankText
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_readonly_statement(self) -> "PreparedQuery":
        try:
            statements = parse(self.executable_sql, read=self.dialect)
        except Exception as exc:
            raise ValueError(f"prepared query is not valid {self.dialect} SQL") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError("prepared query must contain one read-only SELECT")
        statement = statements[0]
        forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command)
        if any(statement.find(kind) is not None for kind in forbidden):
            raise ValueError("prepared query contains a non-read-only operation")
        observed = set(_statement_relations(statement))
        if not observed or not observed.issubset(set(self.allowed_relations)):
            raise ValueError("prepared query references an unauthorized relation")
        positions = tuple(item.position for item in self.parameters)
        if positions != tuple(range(1, len(positions) + 1)):
            raise ValueError("prepared query parameters must be contiguous")
        placeholders = {
            int(item.this.this)
            for item in statement.find_all(exp.Parameter)
            if isinstance(item.this, exp.Literal) and str(item.this.this).isdigit()
        }
        placeholders.update(
            int(item.this)
            for item in statement.find_all(exp.Placeholder)
            if str(item.this).isdigit()
        )
        if placeholders != set(positions):
            raise ValueError("prepared query placeholders do not match parameters")
        expected = hashlib.sha256(self.executable_sql.encode("utf-8")).hexdigest()
        if self.sql_ast_hash != expected:
            raise ValueError("prepared query AST hash does not match SQL")
        return self


__all__ = [
    "AnalysisType",
    "CalculationOperation",
    "CrossTabSpec",
    "DerivedCalculation",
    "FilterOperator",
    "GrainAlignment",
    "LogicalFilter",
    "LogicalOrdering",
    "LogicalQueryPlan",
    "PlanContext",
    "PreparedQuery",
    "QueryParameter",
    "RankingSpec",
    "RelationshipRouteEvidence",
    "ResultShape",
    "SeriesAxis",
    "SortDirection",
    "TimeGrain",
    "TimeRange",
    "WindowSpec",
]
