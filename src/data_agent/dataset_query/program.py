"""Versioned, dataset-neutral query-program contracts.

The program IR is intentionally smaller than SQL.  Model-produced programs may
only reference activated logical fields, previous stage outputs, literals, and
the deterministic operations declared below.  Compilers remain responsible for
relationship routing, dialect lowering, parameterization, and read-only policy.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from .models import (
    DatasetPlanModel,
    DatasetPlanStatus,
    NonBlankText,
    SafeAlias,
    Scalar,
)


class DatasetFieldExpression(DatasetPlanModel):
    kind: Literal["field"] = "field"
    ref: NonBlankText


class DatasetOutputExpression(DatasetPlanModel):
    kind: Literal["output"] = "output"
    stage_id: SafeAlias
    name: SafeAlias


class DatasetLiteralExpression(DatasetPlanModel):
    kind: Literal["literal"] = "literal"
    value: Scalar | None


class DatasetUnaryExpression(DatasetPlanModel):
    kind: Literal["unary"] = "unary"
    operation: Literal["is_null", "is_not_null", "not", "negate"]
    operand: "DatasetScalarExpression"


class DatasetBinaryExpression(DatasetPlanModel):
    kind: Literal["binary"] = "binary"
    operation: Literal[
        "add",
        "subtract",
        "multiply",
        "divide",
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "and",
        "or",
    ]
    left: "DatasetScalarExpression"
    right: "DatasetScalarExpression"


class DatasetFunctionExpression(DatasetPlanModel):
    kind: Literal["function"] = "function"
    operation: Literal[
        "time_bucket",
        "coalesce",
        "nullif",
        "cast_float",
        "date_diff_days",
        "date_diff_months",
        "date_part",
        "contains_ci",
        "lower",
        "power",
        "abs",
        "sqrt",
        "sin",
        "cos",
        "asin",
        "radians",
    ]
    arguments: tuple["DatasetScalarExpression", ...] = Field(min_length=1)
    time_grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    date_part: Literal["year", "month", "day", "hour", "weekday"] | None = None

    @model_validator(mode="after")
    def validate_function_shape(self) -> "DatasetFunctionExpression":
        expected = {
            "time_bucket": (1, 1),
            "coalesce": (2, 32),
            "nullif": (2, 2),
            "cast_float": (1, 1),
            "date_diff_days": (2, 2),
            "date_diff_months": (2, 2),
            "date_part": (1, 1),
            "contains_ci": (2, 2),
            "lower": (1, 1),
            "power": (2, 2),
            "abs": (1, 1),
            "sqrt": (1, 1),
            "sin": (1, 1),
            "cos": (1, 1),
            "asin": (1, 1),
            "radians": (1, 1),
        }[self.operation]
        if not expected[0] <= len(self.arguments) <= expected[1]:
            raise ValueError(
                f"{self.operation} requires between {expected[0]} and "
                f"{expected[1]} arguments"
            )
        if self.operation == "time_bucket":
            if self.time_grain is None:
                raise ValueError("time_bucket requires a time grain")
        elif self.time_grain is not None:
            raise ValueError("only time_bucket can define a time grain")
        if self.operation == "date_part":
            if self.date_part is None:
                raise ValueError("date_part requires a date part")
        elif self.date_part is not None:
            raise ValueError("only date_part can define a date part")
        return self


DatasetScalarExpression: TypeAlias = Annotated[
    DatasetFieldExpression
    | DatasetOutputExpression
    | DatasetLiteralExpression
    | DatasetUnaryExpression
    | DatasetBinaryExpression
    | DatasetFunctionExpression,
    Field(discriminator="kind"),
]


class DatasetAggregateExpression(DatasetPlanModel):
    kind: Literal["aggregate"] = "aggregate"
    operation: Literal[
        "count",
        "count_distinct",
        "sum",
        "avg",
        "min",
        "max",
        "median",
    ]
    operand: DatasetScalarExpression | None = None
    filter: DatasetScalarExpression | None = None

    @model_validator(mode="after")
    def validate_aggregate_shape(self) -> "DatasetAggregateExpression":
        if self.operation != "count" and self.operand is None:
            raise ValueError("only count may omit its operand")
        return self


class DatasetMetricExpression(DatasetPlanModel):
    kind: Literal["metric"] = "metric"
    ref: NonBlankText


DatasetProjectionExpression: TypeAlias = Annotated[
    DatasetScalarExpression | DatasetAggregateExpression | DatasetMetricExpression,
    Field(discriminator="kind"),
]


class DatasetProjection(DatasetPlanModel):
    alias: SafeAlias
    expression: DatasetProjectionExpression


class DatasetProgramOrdering(DatasetPlanModel):
    name: SafeAlias
    direction: Literal["asc", "desc"] = "asc"


class DatasetRootSource(DatasetPlanModel):
    kind: Literal["dataset"] = "dataset"
    anchor_ref: NonBlankText | None = None


class DatasetStageSource(DatasetPlanModel):
    kind: Literal["stage"] = "stage"
    stage_id: SafeAlias


class DatasetStageJoinCondition(DatasetPlanModel):
    left_name: SafeAlias
    right_name: SafeAlias


class DatasetJoinSource(DatasetPlanModel):
    kind: Literal["join"] = "join"
    left_stage_id: SafeAlias
    right_stage_id: SafeAlias
    join_type: Literal["inner", "left", "cross"] = "inner"
    conditions: tuple[DatasetStageJoinCondition, ...] = ()

    @model_validator(mode="after")
    def validate_join_shape(self) -> "DatasetJoinSource":
        if self.left_stage_id == self.right_stage_id:
            raise ValueError("stage joins require two different inputs")
        if self.join_type == "cross" and self.conditions:
            raise ValueError("cross joins cannot define join conditions")
        if self.join_type != "cross" and not self.conditions:
            raise ValueError("non-cross joins require at least one condition")
        return self


DatasetProgramSource: TypeAlias = Annotated[
    DatasetRootSource | DatasetStageSource | DatasetJoinSource,
    Field(discriminator="kind"),
]


class DatasetQueryStage(DatasetPlanModel):
    kind: Literal["query"] = "query"
    stage_id: SafeAlias
    input: DatasetProgramSource
    projections: tuple[DatasetProjection, ...] = Field(min_length=1)
    filters: tuple[DatasetScalarExpression, ...] = ()
    group_by: tuple[DatasetScalarExpression, ...] = ()
    order_by: tuple[DatasetProgramOrdering, ...] = ()
    limit: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_query_stage(self) -> "DatasetQueryStage":
        aliases = tuple(item.alias for item in self.projections)
        if len(aliases) != len(set(aliases)):
            raise ValueError("query stage projection aliases must be unique")
        unknown_ordering = {item.name for item in self.order_by} - set(aliases)
        if unknown_ordering:
            raise ValueError(
                "query stage ordering references unknown outputs: "
                + ", ".join(sorted(unknown_ordering))
            )
        return self


class DatasetUnionStage(DatasetPlanModel):
    kind: Literal["union_all"] = "union_all"
    stage_id: SafeAlias
    input_stage_ids: tuple[SafeAlias, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_union_inputs(self) -> "DatasetUnionStage":
        if len(self.input_stage_ids) != len(set(self.input_stage_ids)):
            raise ValueError("union inputs must be unique")
        return self


DatasetProgramStage: TypeAlias = Annotated[
    DatasetQueryStage | DatasetUnionStage,
    Field(discriminator="kind"),
]


class DatasetQueryProgram(DatasetPlanModel):
    schema_version: Literal[2] = 2
    status: DatasetPlanStatus = DatasetPlanStatus.READY
    clarification_question: str | None = None
    stages: tuple[DatasetProgramStage, ...] = ()
    output_stage_id: SafeAlias | None = None
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_program(self) -> "DatasetQueryProgram":
        if self.status != DatasetPlanStatus.READY:
            if not (self.clarification_question or "").strip():
                raise ValueError("non-ready programs require a clarification question")
            if self.stages or self.output_stage_id is not None:
                raise ValueError("non-ready programs cannot include executable stages")
            return self
        if self.clarification_question is not None:
            raise ValueError("ready programs cannot include a clarification question")
        if not self.stages or self.output_stage_id is None:
            raise ValueError("ready programs require stages and an output stage")
        stage_ids = tuple(item.stage_id for item in self.stages)
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("query program stage identifiers must be unique")
        known: set[str] = set()
        for stage in self.stages:
            dependencies: tuple[str, ...]
            if isinstance(stage, DatasetUnionStage):
                dependencies = stage.input_stage_ids
            elif isinstance(stage.input, DatasetStageSource):
                dependencies = (stage.input.stage_id,)
            elif isinstance(stage.input, DatasetJoinSource):
                dependencies = (
                    stage.input.left_stage_id,
                    stage.input.right_stage_id,
                )
            else:
                dependencies = ()
            missing = set(dependencies) - known
            if missing:
                raise ValueError(
                    f"stage {stage.stage_id} references unavailable prior stages: "
                    + ", ".join(sorted(missing))
                )
            known.add(stage.stage_id)
        if self.output_stage_id not in known:
            raise ValueError("program output stage is unavailable")
        return self


for model in (
    DatasetUnaryExpression,
    DatasetBinaryExpression,
    DatasetFunctionExpression,
    DatasetAggregateExpression,
    DatasetProjection,
    DatasetQueryStage,
):
    model.model_rebuild()


__all__ = [
    "DatasetAggregateExpression",
    "DatasetBinaryExpression",
    "DatasetFieldExpression",
    "DatasetFunctionExpression",
    "DatasetJoinSource",
    "DatasetLiteralExpression",
    "DatasetMetricExpression",
    "DatasetOutputExpression",
    "DatasetProgramOrdering",
    "DatasetProgramSource",
    "DatasetProgramStage",
    "DatasetProjection",
    "DatasetProjectionExpression",
    "DatasetQueryProgram",
    "DatasetQueryStage",
    "DatasetRootSource",
    "DatasetScalarExpression",
    "DatasetStageJoinCondition",
    "DatasetStageSource",
    "DatasetUnaryExpression",
    "DatasetUnionStage",
]
