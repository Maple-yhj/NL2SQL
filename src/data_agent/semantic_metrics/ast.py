"""A finite, SQL-free expression language for governed metric definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .policy import (
    MAX_AST_DEPTH,
    MAX_AST_NODES,
    MAX_BOOLEAN_OPERANDS,
    MAX_FUNCTION_ARGUMENTS,
    MAX_IN_VALUES,
    MAX_LITERAL_STRING_LENGTH,
)
from .types import NonBlankText


MetricScalar: TypeAlias = StrictStr | StrictInt | StrictFloat | StrictBool


class MetricAstModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
    )

    def model_copy(
        self,
        *,
        update: dict[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        fields = type(self).model_fields
        unknown = set(update) - set(fields)
        if unknown:
            raise ValueError(
                "model_copy update contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        values = {name: getattr(self, name) for name in fields}
        values.update(update)
        return type(self).model_validate(values)


class MetricFieldExpression(MetricAstModel):
    kind: Literal["field"] = "field"
    ref: NonBlankText


class MetricLiteralExpression(MetricAstModel):
    kind: Literal["literal"] = "literal"
    value: MetricScalar | None

    @field_validator("value")
    @classmethod
    def validate_literal_size(cls, value: MetricScalar | None) -> MetricScalar | None:
        if isinstance(value, str) and len(value) > MAX_LITERAL_STRING_LENGTH:
            raise ValueError("metric literal exceeds the maximum string length")
        return value


class MetricUnaryExpression(MetricAstModel):
    kind: Literal["unary"] = "unary"
    operation: Literal["negate"]
    operand: "MetricValueExpression"


class MetricBinaryExpression(MetricAstModel):
    kind: Literal["binary"] = "binary"
    operation: Literal["add", "subtract", "multiply", "divide"]
    left: "MetricValueExpression"
    right: "MetricValueExpression"


class MetricFunctionExpression(MetricAstModel):
    kind: Literal["function"] = "function"
    operation: Literal["coalesce", "nullif", "cast_decimal", "abs"]
    arguments: tuple["MetricValueExpression", ...] = Field(
        min_length=1,
        max_length=MAX_FUNCTION_ARGUMENTS,
    )
    precision: int | None = Field(default=None, ge=1, le=38)
    scale: int | None = Field(default=None, ge=0, le=38)

    @model_validator(mode="after")
    def validate_function_shape(self) -> "MetricFunctionExpression":
        minimum, maximum = {
            "coalesce": (2, MAX_FUNCTION_ARGUMENTS),
            "nullif": (2, 2),
            "cast_decimal": (1, 1),
            "abs": (1, 1),
        }[self.operation]
        if not minimum <= len(self.arguments) <= maximum:
            raise ValueError(
                f"{self.operation} requires between {minimum} and {maximum} arguments"
            )
        if self.operation == "cast_decimal":
            if self.precision is None or self.scale is None:
                raise ValueError("cast_decimal requires precision and scale")
            if self.scale > self.precision:
                raise ValueError("cast_decimal scale cannot exceed precision")
        elif self.precision is not None or self.scale is not None:
            raise ValueError("only cast_decimal can define precision and scale")
        return self


MetricValueExpression: TypeAlias = Annotated[
    MetricFieldExpression
    | MetricLiteralExpression
    | MetricUnaryExpression
    | MetricBinaryExpression
    | MetricFunctionExpression,
    Field(discriminator="kind"),
]


class MetricComparisonPredicate(MetricAstModel):
    kind: Literal["comparison"] = "comparison"
    operation: Literal["eq", "neq", "gt", "gte", "lt", "lte"]
    left: MetricValueExpression
    right: MetricValueExpression


class MetricSetPredicate(MetricAstModel):
    kind: Literal["set"] = "set"
    operation: Literal["in", "not_in"]
    operand: MetricValueExpression
    values: tuple[MetricScalar, ...] = Field(min_length=1, max_length=MAX_IN_VALUES)

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: tuple[MetricScalar, ...]) -> tuple[MetricScalar, ...]:
        for value in values:
            if isinstance(value, str) and len(value) > MAX_LITERAL_STRING_LENGTH:
                raise ValueError("metric set literal exceeds the maximum string length")
        if len({(type(value).__name__, value) for value in values}) != len(values):
            raise ValueError("metric set predicate values must be unique")
        return values


class MetricNullPredicate(MetricAstModel):
    kind: Literal["null"] = "null"
    operation: Literal["is_null", "is_not_null"]
    operand: MetricValueExpression


class MetricBooleanPredicate(MetricAstModel):
    kind: Literal["boolean"] = "boolean"
    operation: Literal["and", "or"]
    operands: tuple["MetricPredicate", ...] = Field(
        min_length=2,
        max_length=MAX_BOOLEAN_OPERANDS,
    )


class MetricNotPredicate(MetricAstModel):
    kind: Literal["not"] = "not"
    operand: "MetricPredicate"


MetricPredicate: TypeAlias = Annotated[
    MetricComparisonPredicate
    | MetricSetPredicate
    | MetricNullPredicate
    | MetricBooleanPredicate
    | MetricNotPredicate,
    Field(discriminator="kind"),
]


class MetricAggregateFormula(MetricAstModel):
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
    operand: MetricValueExpression | None = None
    filter: MetricPredicate | None = None

    @model_validator(mode="after")
    def validate_aggregate_shape(self) -> "MetricAggregateFormula":
        if self.operation != "count" and self.operand is None:
            raise ValueError("only count may omit its operand")
        return self


class MetricFormulaBinary(MetricAstModel):
    kind: Literal["formula_binary"] = "formula_binary"
    operation: Literal["add", "subtract", "multiply", "divide"]
    left: "MetricFormulaExpression"
    right: "MetricFormulaExpression"


class MetricConstantFormula(MetricAstModel):
    kind: Literal["constant"] = "constant"
    value: StrictInt | StrictFloat


MetricFormulaExpression: TypeAlias = Annotated[
    MetricAggregateFormula | MetricFormulaBinary | MetricConstantFormula,
    Field(discriminator="kind"),
]


@dataclass(frozen=True, slots=True)
class MetricAstStats:
    node_count: int
    maximum_depth: int
    literal_count: int
    field_refs: tuple[str, ...]


def analyze_metric_ast(
    formula: MetricFormulaExpression,
    default_filter: MetricPredicate | None = None,
) -> MetricAstStats:
    node_count = 0
    maximum_depth = 0
    literal_count = 0
    field_refs: list[str] = []

    def visit_value(value: MetricValueExpression, depth: int) -> None:
        nonlocal node_count, maximum_depth, literal_count
        node_count += 1
        maximum_depth = max(maximum_depth, depth)
        if isinstance(value, MetricFieldExpression):
            field_refs.append(value.ref)
        elif isinstance(value, MetricLiteralExpression):
            literal_count += 1
        elif isinstance(value, MetricUnaryExpression):
            visit_value(value.operand, depth + 1)
        elif isinstance(value, MetricBinaryExpression):
            visit_value(value.left, depth + 1)
            visit_value(value.right, depth + 1)
        else:
            assert isinstance(value, MetricFunctionExpression)
            for argument in value.arguments:
                visit_value(argument, depth + 1)

    def visit_predicate(predicate: MetricPredicate, depth: int) -> None:
        nonlocal node_count, maximum_depth, literal_count
        node_count += 1
        maximum_depth = max(maximum_depth, depth)
        if isinstance(predicate, MetricComparisonPredicate):
            visit_value(predicate.left, depth + 1)
            visit_value(predicate.right, depth + 1)
        elif isinstance(predicate, MetricSetPredicate):
            visit_value(predicate.operand, depth + 1)
            literal_count += len(predicate.values)
        elif isinstance(predicate, MetricNullPredicate):
            visit_value(predicate.operand, depth + 1)
        elif isinstance(predicate, MetricBooleanPredicate):
            for operand in predicate.operands:
                visit_predicate(operand, depth + 1)
        else:
            assert isinstance(predicate, MetricNotPredicate)
            visit_predicate(predicate.operand, depth + 1)

    def visit_formula(value: MetricFormulaExpression, depth: int) -> None:
        nonlocal node_count, maximum_depth, literal_count
        node_count += 1
        maximum_depth = max(maximum_depth, depth)
        if isinstance(value, MetricAggregateFormula):
            if value.operand is not None:
                visit_value(value.operand, depth + 1)
            if value.filter is not None:
                visit_predicate(value.filter, depth + 1)
        elif isinstance(value, MetricFormulaBinary):
            visit_formula(value.left, depth + 1)
            visit_formula(value.right, depth + 1)
        else:
            assert isinstance(value, MetricConstantFormula)
            literal_count += 1

    visit_formula(formula, 1)
    if default_filter is not None:
        visit_predicate(default_filter, 1)
    if node_count > MAX_AST_NODES:
        raise ValueError(f"metric AST exceeds {MAX_AST_NODES} nodes")
    if maximum_depth > MAX_AST_DEPTH:
        raise ValueError(f"metric AST exceeds maximum depth {MAX_AST_DEPTH}")
    return MetricAstStats(
        node_count=node_count,
        maximum_depth=maximum_depth,
        literal_count=literal_count,
        field_refs=tuple(dict.fromkeys(field_refs)),
    )


for model in (
    MetricUnaryExpression,
    MetricBinaryExpression,
    MetricFunctionExpression,
    MetricComparisonPredicate,
    MetricSetPredicate,
    MetricNullPredicate,
    MetricBooleanPredicate,
    MetricNotPredicate,
    MetricAggregateFormula,
    MetricFormulaBinary,
):
    model.model_rebuild()


__all__ = [
    "MetricAggregateFormula",
    "MetricAstModel",
    "MetricAstStats",
    "MetricBinaryExpression",
    "MetricBooleanPredicate",
    "MetricComparisonPredicate",
    "MetricConstantFormula",
    "MetricFieldExpression",
    "MetricFormulaBinary",
    "MetricFormulaExpression",
    "MetricFunctionExpression",
    "MetricLiteralExpression",
    "MetricNotPredicate",
    "MetricNullPredicate",
    "MetricPredicate",
    "MetricScalar",
    "MetricSetPredicate",
    "MetricUnaryExpression",
    "MetricValueExpression",
    "analyze_metric_ast",
]
