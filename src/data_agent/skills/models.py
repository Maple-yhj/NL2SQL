"""Strict logical contracts shared by built-in Data Agent skills."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    model_validator,
)

from data_agent.runtime.packs import (
    CanonicalEntityId,
    CanonicalFieldRef,
    CanonicalLogicalId,
    CanonicalSemanticRef,
    DomainPack,
    SemanticVersion,
)


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
SkillId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$",
    ),
]
ToolCapability = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9]*\.[a-z][a-z0-9]*$",
    ),
]


class SkillModel(BaseModel):
    """Frozen, exact-field base model for public Skill contracts."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        validate_default=True,
    )

    def model_copy(
        self,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return a validated copy and reject Pydantic's unchecked extra updates."""

        if not update:
            return super().model_copy(deep=deep)
        fields = type(self).model_fields
        unknown = set(update) - set(fields)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"model_copy update contains unknown fields: {names}")
        values = {
            name: deepcopy(getattr(self, name)) if deep else getattr(self, name)
            for name in fields
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


class LogicalFilter(SkillModel):
    ref: CanonicalSemanticRef
    operator: FilterOperator
    value: PlanScalar | tuple[PlanScalar, ...] | None = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> "LogicalFilter":
        if self.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            if self.value is not None:
                raise ValueError("null predicates must not define a value")
            return self
        if self.value is None:
            raise ValueError("predicate requires a value")
        if self.operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
            if not isinstance(self.value, tuple) or not self.value:
                raise ValueError("set predicates require a non-empty list value")
        elif isinstance(self.value, tuple):
            raise ValueError("scalar predicates require a scalar value")
        return self


class TimeRange(SkillModel):
    """Canonical event-time selection with optional half-open bounds."""

    field: CanonicalFieldRef
    start: NonBlankText | None = None
    end: NonBlankText | None = None


class LogicalOrdering(SkillModel):
    ref: CanonicalSemanticRef
    direction: SortDirection


class SeriesAxis(SkillModel):
    kind: Literal["time", "numeric"]
    field: CanonicalFieldRef
    time_grain: TimeGrain | None = None

    @model_validator(mode="after")
    def validate_axis_grain(self) -> "SeriesAxis":
        if self.kind == "time" and self.time_grain is None:
            raise ValueError("time series axis requires a time grain")
        if self.kind == "numeric" and self.time_grain is not None:
            raise ValueError("numeric series axis cannot define a time grain")
        return self


class RankingSpec(SkillModel):
    mode: Literal["top_n", "full"]
    measure: CanonicalSemanticRef


class CrossTabSpec(SkillModel):
    row_axis: CanonicalFieldRef
    column_axis: CanonicalFieldRef
    values: tuple[CanonicalSemanticRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_axes(self) -> "CrossTabSpec":
        if self.row_axis == self.column_axis:
            raise ValueError("cross-tab row and column axes must be independent")
        if len(self.values) != len(set(self.values)):
            raise ValueError("cross-tab values must be unique")
        return self


class DerivedCalculation(SkillModel):
    id: CanonicalLogicalId
    operation: CalculationOperation
    inputs: tuple[CanonicalSemanticRef, ...] = Field(min_length=1)
    partition_by: tuple[CanonicalFieldRef, ...] = ()


class WindowSpec(SkillModel):
    """Logical window metadata; it never contains executable expressions."""

    id: CanonicalLogicalId
    calculation: CanonicalLogicalId
    axis_ref: CanonicalFieldRef
    partition_by: tuple[CanonicalFieldRef, ...] = ()
    ordering: tuple[LogicalOrdering, ...] = Field(min_length=1)
    output_grain: tuple[CanonicalFieldRef, ...] = Field(min_length=1)


class GrainAlignment(SkillModel):
    """Explicit proof that an expanding semantic path is handled safely."""

    source_entity: CanonicalEntityId
    target_entity: CanonicalEntityId
    strategy: Literal["pre_aggregate", "distinct"]
    relationship_path: tuple[CanonicalLogicalId, ...] = Field(min_length=1)
    join_grain: tuple[CanonicalFieldRef, ...] = Field(min_length=1)


class PlanContext(SkillModel):
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


class RelationshipRouteEvidence(SkillModel):
    """The immutable graph route and grain decision used for one query."""

    route_digest: NonBlankText
    logical_node_ids: tuple[NonBlankText, ...]
    edge_ids: tuple[NonBlankText, ...]
    cardinality_by_node: tuple[
        tuple[NonBlankText, Literal["one", "many", "unknown"]], ...
    ]
    fanout_decision: NonBlankText
    preaggregation_required: bool = False


class LogicalQueryPlan(SkillModel):
    """A complete, physical-system-independent analytical query plan."""

    analysis_type: AnalysisType
    metrics: tuple[CanonicalLogicalId, ...] = ()
    entities: tuple[CanonicalEntityId, ...] = ()
    relationships: tuple[CanonicalLogicalId, ...] = ()
    dimensions: tuple[CanonicalFieldRef, ...] = ()
    fields: tuple[CanonicalFieldRef, ...] = ()
    filters: tuple[LogicalFilter, ...] = ()
    time_range: TimeRange | None = None
    time_grain: TimeGrain | None = None
    series_axis: SeriesAxis | None = None
    ordering: tuple[LogicalOrdering, ...] = ()
    limit: int | None = Field(default=None, ge=1)
    ranking: RankingSpec | None = None
    cross_tab: CrossTabSpec | None = None
    expected_grain: tuple[CanonicalFieldRef, ...] = ()
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


class SkillManifest(SkillModel):
    skill_id: SkillId
    version: SemanticVersion
    domain: SkillId
    intent_signatures: tuple[NonBlankText, ...] = Field(min_length=1)
    required_semantic_ids: tuple[CanonicalSemanticRef, ...] = Field(min_length=1)
    required_tool_capabilities: tuple[ToolCapability, ...] = Field(min_length=1)
    allowed_tools: tuple[ToolCapability, ...] = Field(min_length=1)
    graph_fragment: tuple[NonBlankText, ...] = Field(min_length=1)
    logical_plan_schema: NonBlankText
    validators: tuple[CanonicalLogicalId, ...] = Field(min_length=1)
    output_schema: NonBlankText
    memory_write_policy: Literal["proposal_only", "disabled"]
    eval_suite_ref: NonBlankText


class SkillInput(SkillModel):
    question: NonBlankText
    contextualized_question: NonBlankText
    commerce_semantic_snapshot: DomainPack
    accessible_semantic_resources: tuple[CanonicalSemanticRef, ...] = ()
    approved_memories: tuple[NonBlankText, ...] = ()
    conversation_summary: NonBlankText | None = None
