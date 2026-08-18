from __future__ import annotations

import duckdb
import pytest
from sqlglot import exp

from data_agent.semantic_metrics import (
    MetricAggregateFormula,
    MetricBinaryExpression,
    MetricComparisonPredicate,
    MetricConstantFormula,
    MetricFieldExpression,
    MetricFormulaBinary,
    MetricLiteralExpression,
    MetricNullPolicy,
    MetricSetPredicate,
    SemanticMetricDefinitionV2,
    SemanticMetricSqlCompiler,
)


def _compile(definition: SemanticMetricDefinitionV2):
    parameters: list[object] = []
    fields = {
        "commerce.item.price": exp.column("price", quoted=True),
        "commerce.item.freight": exp.column("freight", quoted=True),
        "commerce.order.status": exp.column("status", quoted=True),
    }

    def parameter(value, purpose):
        assert purpose == "binding_constant"
        parameters.append(value)
        return exp.Placeholder()

    compiled = SemanticMetricSqlCompiler().compile(
        definition,
        field=lambda ref: fields[ref],
        parameter=parameter,
        dialect="duckdb",
    )
    query = exp.select(exp.alias_(compiled, "value", quoted=True)).from_("activity")
    return query.sql(dialect="duckdb"), parameters


def test_compound_metric_and_default_scope_execute_deterministically() -> None:
    definition = SemanticMetricDefinitionV2(
        metric_ref="commerce.gmv",
        display_name="GMV",
        description="Item price plus freight for delivered orders",
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricBinaryExpression(
                operation="add",
                left=MetricFieldExpression(ref="commerce.item.price"),
                right=MetricFieldExpression(ref="commerce.item.freight"),
            ),
        ),
        default_filter=MetricSetPredicate(
            operation="in",
            operand=MetricFieldExpression(ref="commerce.order.status"),
            values=("delivered", "shipped"),
        ),
    )

    sql, parameters = _compile(definition)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE activity(price DOUBLE, freight DOUBLE, status VARCHAR)"
        )
        connection.executemany(
            "INSERT INTO activity VALUES (?, ?, ?)",
            ((10, 2, "delivered"), (20, 3, "cancelled"), (5, 1, "shipped")),
        )
        result = connection.execute(sql, parameters).fetchone()
    finally:
        connection.close()

    assert result == (18.0,)
    assert parameters == ["delivered", "shipped"]
    assert "SUM" in sql and "CASE WHEN" in sql


def test_formula_division_uses_nullif_and_parameters_for_constants() -> None:
    definition = SemanticMetricDefinitionV2(
        metric_ref="commerce.rate",
        display_name="Rate",
        description="Filtered amount divided by a governed constant",
        formula=MetricFormulaBinary(
            operation="divide",
            left=MetricAggregateFormula(
                operation="sum",
                operand=MetricFieldExpression(ref="commerce.item.price"),
                filter=MetricComparisonPredicate(
                    operation="gt",
                    left=MetricFieldExpression(ref="commerce.item.price"),
                    right=MetricLiteralExpression(value=0),
                ),
            ),
            right=MetricConstantFormula(value=100),
        ),
    )

    sql, parameters = _compile(definition)

    assert "NULLIF" in sql
    assert parameters == [0, 100]


def test_zero_null_policy_coalesces_before_average() -> None:
    definition = SemanticMetricDefinitionV2(
        metric_ref="commerce.average_price",
        display_name="Average price",
        description="Average with missing prices treated as zero",
        formula=MetricAggregateFormula(
            operation="avg",
            operand=MetricFieldExpression(ref="commerce.item.price"),
        ),
        null_policy=MetricNullPolicy.ZERO,
    )

    sql, _ = _compile(definition)

    assert "AVG(COALESCE" in sql


def test_error_null_policy_fails_closed() -> None:
    definition = SemanticMetricDefinitionV2(
        metric_ref="commerce.strict_price",
        display_name="Strict price",
        description="Reject missing values",
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricFieldExpression(ref="commerce.item.price"),
        ),
        null_policy=MetricNullPolicy.ERROR,
    )

    with pytest.raises(ValueError, match="non-null data contract"):
        _compile(definition)
