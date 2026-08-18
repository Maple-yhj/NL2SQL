from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from data_agent.semantic_metrics import (
    LegacyMetricAdapter,
    MetricAggregateFormula,
    MetricBinaryExpression,
    MetricComparisonPredicate,
    MetricContextPin,
    MetricFieldExpression,
    MetricFormulaBinary,
    MetricFunctionExpression,
    MetricLiteralExpression,
    MetricScopeConvention,
    MetricSetIdentity,
    MetricSetPredicate,
    SemanticMetricDefinitionV2,
    analyze_metric_ast,
    canonical_json,
    semantic_digest,
)


def _field(ref: str) -> MetricFieldExpression:
    return MetricFieldExpression(ref=ref)


def _gmv_definition() -> SemanticMetricDefinitionV2:
    return SemanticMetricDefinitionV2(
        metric_ref="commerce.gmv",
        display_name="GMV",
        description="Gross merchandise value based on item price and freight",
        synonyms=("成交总额", "商品交易总额"),
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricBinaryExpression(
                operation="add",
                left=_field("orders.item_price"),
                right=_field("orders.freight_value"),
            ),
        ),
        default_filter=MetricSetPredicate(
            operation="not_in",
            operand=_field("orders.status"),
            values=("canceled", "unavailable"),
        ),
        default_time_ref="orders.purchased_at",
        allowed_time_refs=("orders.purchased_at", "orders.approved_at"),
        entity_key_refs=("orders.order_id",),
        grain="order_item",
        unit="currency",
        currency="BRL",
        scope=MetricScopeConvention(
            status_ref="orders.status",
            excluded_statuses=("canceled", "unavailable"),
            refund_treatment="not_available",
            includes_freight=True,
        ),
        limitations=("Refund data is unavailable in the source snapshot",),
    )


def test_compound_gmv_definition_collects_all_execution_field_refs() -> None:
    metric = _gmv_definition()

    assert metric.ast_field_refs == (
        "orders.item_price",
        "orders.freight_value",
        "orders.status",
    )
    assert metric.all_field_refs == (
        "orders.item_price",
        "orders.freight_value",
        "orders.status",
        "orders.purchased_at",
        "orders.approved_at",
        "orders.order_id",
    )
    stats = analyze_metric_ast(metric.formula, metric.default_filter)
    assert stats.node_count == 6
    assert stats.maximum_depth == 3
    assert stats.literal_count == 2


def test_formula_can_combine_aggregates_without_metric_dependencies() -> None:
    aov = SemanticMetricDefinitionV2(
        metric_ref="commerce.aov",
        display_name="AOV",
        description="Average order value",
        formula=MetricFormulaBinary(
            operation="divide",
            left=MetricAggregateFormula(
                operation="sum",
                operand=_field("orders.payment_value"),
            ),
            right=MetricAggregateFormula(
                operation="count_distinct",
                operand=_field("orders.order_id"),
            ),
        ),
        entity_key_refs=("orders.order_id",),
        unit="currency_per_order",
        currency="BRL",
    )

    assert aov.ast_field_refs == (
        "orders.payment_value",
        "orders.order_id",
    )


def test_metric_schema_rejects_raw_sql_and_unknown_fields() -> None:
    payload = _gmv_definition().model_dump(mode="json")
    payload["formula"] = {"kind": "raw_sql", "sql": "SUM(price); DROP TABLE x"}

    with pytest.raises(ValidationError):
        SemanticMetricDefinitionV2.model_validate(payload)

    payload = _gmv_definition().model_dump(mode="json")
    payload["raw_sql"] = "SUM(price)"
    with pytest.raises(ValidationError):
        SemanticMetricDefinitionV2.model_validate(payload)


def test_metric_ast_enforces_function_shape_and_complexity_depth() -> None:
    with pytest.raises(ValidationError, match="cast_decimal requires precision and scale"):
        MetricFunctionExpression(
            operation="cast_decimal",
            arguments=(_field("orders.item_price"),),
        )

    expression = _field("orders.item_price")
    for _ in range(12):
        expression = MetricBinaryExpression(
            operation="add",
            left=expression,
            right=MetricLiteralExpression(value=1),
        )
    with pytest.raises(ValidationError, match="maximum depth"):
        SemanticMetricDefinitionV2(
            metric_ref="commerce.too_deep",
            display_name="Too deep",
            description="Invalid deeply nested formula",
            formula=MetricAggregateFormula(operation="sum", operand=expression),
        )


def test_metric_schema_rejects_non_finite_numbers_and_invalid_scope() -> None:
    with pytest.raises(ValidationError):
        MetricLiteralExpression(value=float("nan"))

    with pytest.raises(ValidationError, match="status conventions require status_ref"):
        MetricScopeConvention(excluded_statuses=("canceled",))

    with pytest.raises(ValidationError, match="refund treatment requires refund_ref"):
        MetricScopeConvention(refund_treatment="net_of_refunds")


def test_metric_digest_is_canonical_and_sensitive_to_semantics() -> None:
    metric = _gmv_definition()
    equivalent = SemanticMetricDefinitionV2.model_validate(
        metric.model_dump(mode="json")
    )
    price_only = metric.model_copy(
        update={
            "formula": MetricAggregateFormula(
                operation="sum",
                operand=_field("orders.item_price"),
            )
        }
    )

    assert canonical_json(metric) == canonical_json(equivalent)
    assert semantic_digest(metric) == semantic_digest(equivalent)
    assert semantic_digest(metric) != semantic_digest(price_only)
    assert semantic_digest({"b": 2, "a": 1}) == semantic_digest({"a": 1, "b": 2})


@dataclass(frozen=True)
class _LegacyMetric:
    metric_ref: str = "legacy.total_amount"
    display_name: str = "Total amount"
    description: str = "Legacy amount sum"
    operation: str = "sum"
    field_ref: str | None = "orders.amount"
    unit: str | None = "currency"
    grain: str | None = "order"
    synonyms: tuple[str, ...] = ("total value",)


def test_legacy_adapter_preserves_single_field_metric_semantics() -> None:
    adapted = LegacyMetricAdapter.to_v2(_LegacyMetric())

    assert adapted.metric_ref == "legacy.total_amount"
    assert adapted.formula == MetricAggregateFormula(
        operation="sum",
        operand=_field("orders.amount"),
    )
    assert adapted.unit == "currency"
    assert adapted.grain == "order"
    assert adapted.synonyms == ("total value",)
    assert adapted.provenance[0].kind == "legacy_adapter"


def test_metric_context_pin_requires_complete_overlay_authority() -> None:
    base = {
        "tenant_id": "tenant",
        "source_id": "olist",
        "source_version": 1,
        "binding_id": "olist-binding",
        "binding_version": 2,
        "metric_set": MetricSetIdentity(
            metric_set_id="commerce-metrics",
            version=1,
            digest="sha256:metric-set",
        ),
    }
    with pytest.raises(ValidationError, match="pinned together"):
        MetricContextPin(**base, overlay_id="run-overlay")

    pin = MetricContextPin(
        **base,
        overlay_id="run-overlay",
        overlay_digest="sha256:overlay",
    )
    assert pin.overlay_digest == "sha256:overlay"
