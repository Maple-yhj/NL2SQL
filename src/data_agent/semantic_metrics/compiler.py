"""Deterministic SQLGlot lowering for governed semantic metric ASTs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from sqlglot import exp

from .ast import (
    MetricAggregateFormula,
    MetricBinaryExpression,
    MetricBooleanPredicate,
    MetricComparisonPredicate,
    MetricConstantFormula,
    MetricFieldExpression,
    MetricFormulaBinary,
    MetricFormulaExpression,
    MetricFunctionExpression,
    MetricLiteralExpression,
    MetricNotPredicate,
    MetricNullPredicate,
    MetricPredicate,
    MetricScalar,
    MetricSetPredicate,
    MetricUnaryExpression,
    MetricValueExpression,
)
from .models import MetricNullPolicy, SemanticMetricDefinitionV2


Dialect = Literal["postgres", "sqlite", "duckdb"]
FieldResolver = Callable[[str], exp.Expression]
ParameterFactory = Callable[[MetricScalar, str], exp.Expression]


class SemanticMetricSqlCompiler:
    """Lower the finite metric language without accepting any SQL text."""

    def compile(
        self,
        definition: SemanticMetricDefinitionV2,
        *,
        field: FieldResolver,
        parameter: ParameterFactory,
        dialect: Dialect,
    ) -> exp.Expression:
        if definition.null_policy == MetricNullPolicy.ERROR:
            raise ValueError(
                "null_policy=error requires a validated non-null data contract"
            )
        return self._formula(
            definition.formula,
            default_filter=definition.default_filter,
            null_policy=definition.null_policy,
            field=field,
            parameter=parameter,
            dialect=dialect,
        )

    def _formula(
        self,
        formula: MetricFormulaExpression,
        *,
        default_filter: MetricPredicate | None,
        null_policy: MetricNullPolicy,
        field: FieldResolver,
        parameter: ParameterFactory,
        dialect: Dialect,
    ) -> exp.Expression:
        if isinstance(formula, MetricAggregateFormula):
            operand = (
                self._value(
                    formula.operand,
                    field=field,
                    parameter=parameter,
                    dialect=dialect,
                )
                if formula.operand is not None
                else None
            )
            if operand is not None and null_policy == MetricNullPolicy.ZERO:
                operand = exp.Coalesce(
                    this=operand,
                    expressions=[exp.Literal.number(0)],
                )
            conditions = tuple(
                predicate
                for predicate in (default_filter, formula.filter)
                if predicate is not None
            )
            condition = (
                self._combine_and(
                    self._predicate(
                        predicate,
                        field=field,
                        parameter=parameter,
                        dialect=dialect,
                    )
                    for predicate in conditions
                )
                if conditions
                else None
            )
            return self._aggregate(
                formula.operation,
                operand=operand,
                condition=condition,
                dialect=dialect,
            )
        if isinstance(formula, MetricConstantFormula):
            return parameter(formula.value, "binding_constant")
        assert isinstance(formula, MetricFormulaBinary)
        left = self._formula(
            formula.left,
            default_filter=default_filter,
            null_policy=null_policy,
            field=field,
            parameter=parameter,
            dialect=dialect,
        )
        right = self._formula(
            formula.right,
            default_filter=default_filter,
            null_policy=null_policy,
            field=field,
            parameter=parameter,
            dialect=dialect,
        )
        if formula.operation == "divide":
            right = exp.Nullif(this=right, expression=exp.Literal.number(0))
        operation = {
            "add": exp.Add,
            "subtract": exp.Sub,
            "multiply": exp.Mul,
            "divide": exp.Div,
        }[formula.operation]
        return operation(
            this=exp.Paren(this=left),
            expression=exp.Paren(this=right),
        )

    def _value(
        self,
        value: MetricValueExpression,
        *,
        field: FieldResolver,
        parameter: ParameterFactory,
        dialect: Dialect,
    ) -> exp.Expression:
        if isinstance(value, MetricFieldExpression):
            return field(value.ref).copy()
        if isinstance(value, MetricLiteralExpression):
            return (
                exp.Null()
                if value.value is None
                else parameter(value.value, "binding_constant")
            )
        if isinstance(value, MetricUnaryExpression):
            return exp.Neg(
                this=self._value(
                    value.operand,
                    field=field,
                    parameter=parameter,
                    dialect=dialect,
                )
            )
        if isinstance(value, MetricBinaryExpression):
            left = self._value(
                value.left,
                field=field,
                parameter=parameter,
                dialect=dialect,
            )
            right = self._value(
                value.right,
                field=field,
                parameter=parameter,
                dialect=dialect,
            )
            if value.operation == "divide":
                right = exp.Nullif(this=right, expression=exp.Literal.number(0))
            operation = {
                "add": exp.Add,
                "subtract": exp.Sub,
                "multiply": exp.Mul,
                "divide": exp.Div,
            }[value.operation]
            return operation(
                this=exp.Paren(this=left),
                expression=exp.Paren(this=right),
            )
        assert isinstance(value, MetricFunctionExpression)
        arguments = [
            self._value(
                argument,
                field=field,
                parameter=parameter,
                dialect=dialect,
            )
            for argument in value.arguments
        ]
        if value.operation == "coalesce":
            return exp.Coalesce(this=arguments[0], expressions=arguments[1:])
        if value.operation == "nullif":
            return exp.Nullif(this=arguments[0], expression=arguments[1])
        if value.operation == "abs":
            return exp.Abs(this=arguments[0])
        assert value.operation == "cast_decimal"
        assert value.precision is not None and value.scale is not None
        return exp.cast(
            arguments[0],
            exp.DataType.build(f"DECIMAL({value.precision},{value.scale})"),
        )

    def _predicate(
        self,
        predicate: MetricPredicate,
        *,
        field: FieldResolver,
        parameter: ParameterFactory,
        dialect: Dialect,
    ) -> exp.Expression:
        value = lambda item: self._value(
            item,
            field=field,
            parameter=parameter,
            dialect=dialect,
        )
        if isinstance(predicate, MetricComparisonPredicate):
            operation = {
                "eq": exp.EQ,
                "neq": exp.NEQ,
                "gt": exp.GT,
                "gte": exp.GTE,
                "lt": exp.LT,
                "lte": exp.LTE,
            }[predicate.operation]
            return operation(
                this=value(predicate.left),
                expression=value(predicate.right),
            )
        if isinstance(predicate, MetricSetPredicate):
            membership = exp.In(
                this=value(predicate.operand),
                expressions=[
                    parameter(item, "binding_constant") for item in predicate.values
                ],
            )
            return exp.Not(this=membership) if predicate.operation == "not_in" else membership
        if isinstance(predicate, MetricNullPredicate):
            check = exp.Is(this=value(predicate.operand), expression=exp.Null())
            return exp.Not(this=check) if predicate.operation == "is_not_null" else check
        if isinstance(predicate, MetricBooleanPredicate):
            compiled = [
                self._predicate(
                    item,
                    field=field,
                    parameter=parameter,
                    dialect=dialect,
                )
                for item in predicate.operands
            ]
            combined = compiled[0]
            for item in compiled[1:]:
                combined = (
                    exp.and_(combined, item)
                    if predicate.operation == "and"
                    else exp.or_(combined, item)
                )
            return combined
        assert isinstance(predicate, MetricNotPredicate)
        return exp.Not(
            this=self._predicate(
                predicate.operand,
                field=field,
                parameter=parameter,
                dialect=dialect,
            )
        )

    @staticmethod
    def _aggregate(
        operation: str,
        *,
        operand: exp.Expression | None,
        condition: exp.Expression | None,
        dialect: Dialect,
    ) -> exp.Expression:
        if operation == "count" and condition is None:
            return exp.Count(this=operand or exp.Star())
        if operation == "count_distinct" and condition is None:
            assert operand is not None
            return exp.Count(this=exp.Distinct(expressions=[operand]))
        if condition is not None:
            if operation == "count" and operand is None:
                return exp.Sum(
                    this=exp.Case(
                        ifs=[exp.If(this=condition, true=exp.Literal.number(1))],
                        default=exp.Literal.number(0),
                    )
                )
            filtered = exp.Case(
                ifs=[
                    exp.If(
                        this=condition,
                        true=operand or exp.Literal.number(1),
                    )
                ],
                default=exp.Null(),
            )
            if operation == "count":
                return exp.Count(this=filtered)
            if operation == "count_distinct":
                return exp.Count(this=exp.Distinct(expressions=[filtered]))
            operand = filtered
        assert operand is not None
        if operation == "median":
            return (
                exp.Anonymous(this="MEDIAN", expressions=[operand])
                if dialect == "sqlite"
                else exp.Median(this=operand)
            )
        return {
            "sum": exp.Sum,
            "avg": exp.Avg,
            "min": exp.Min,
            "max": exp.Max,
        }[operation](this=operand)

    @staticmethod
    def _combine_and(expressions) -> exp.Expression:
        values = list(expressions)
        if not values:
            raise ValueError("at least one metric predicate is required")
        combined = values[0]
        for value in values[1:]:
            combined = exp.and_(combined, value)
        return combined


__all__ = ["SemanticMetricSqlCompiler"]
