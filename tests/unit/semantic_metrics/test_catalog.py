from __future__ import annotations

import pytest

from data_agent.semantic_metrics import (
    EffectiveMetricCatalog,
    MetricAggregateFormula,
    MetricCatalogEntry,
    MetricCatalogOrigin,
    MetricFieldExpression,
    MetricResolutionStatus,
    SemanticMetricDefinitionV2,
    SemanticMetricError,
    SemanticMetricErrorCode,
)


def _definition(
    metric_ref: str,
    display_name: str,
    *,
    synonyms: tuple[str, ...] = (),
    field_ref: str = "orders.amount",
) -> SemanticMetricDefinitionV2:
    return SemanticMetricDefinitionV2(
        metric_ref=metric_ref,
        display_name=display_name,
        description=f"Governed definition of {display_name}",
        synonyms=synonyms,
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricFieldExpression(ref=field_ref),
        ),
    )


def _entry(
    definition: SemanticMetricDefinitionV2,
    origin: MetricCatalogOrigin,
    authority: str,
) -> MetricCatalogEntry:
    return MetricCatalogEntry.create(
        definition=definition,
        origin=origin,
        authority_ref=authority,
    )


def test_catalog_resolves_ref_leaf_display_name_and_synonym_exactly() -> None:
    gmv = _definition(
        "commerce.gmv",
        "Gross Merchandise Value",
        synonyms=("GMV", "成交总额"),
    )
    catalog = EffectiveMetricCatalog.build(
        governed=(
            _entry(gmv, MetricCatalogOrigin.GOVERNED, "metric-set:commerce:1"),
        )
    )

    for term in ("commerce.gmv", "gmv", "Gross Merchandise Value", "成交总额"):
        result = catalog.resolve(term)
        assert result.status == MetricResolutionStatus.RESOLVED
        assert result.matches[0].metric_ref == "commerce.gmv"

    assert catalog.resolve("revenue").status == MetricResolutionStatus.UNRESOLVED
    assert catalog.require("GMV").definition == gmv


def test_governed_metric_supersedes_same_ref_legacy_fallback() -> None:
    governed = _definition("commerce.gmv", "GMV", field_ref="orders.price")
    legacy = _definition("commerce.gmv", "Legacy GMV", field_ref="orders.payment")
    catalog = EffectiveMetricCatalog.build(
        governed=(
            _entry(governed, MetricCatalogOrigin.GOVERNED, "metric-set:1"),
        ),
        legacy=(
            _entry(legacy, MetricCatalogOrigin.LEGACY, "binding:legacy"),
        ),
    )

    assert len(catalog.entries) == 1
    assert catalog.entries[0].definition == governed


def test_overlay_cannot_shadow_governed_metric_by_default() -> None:
    governed = _definition("commerce.gmv", "GMV", field_ref="orders.price")
    overlay = _definition("commerce.gmv", "Temporary GMV", field_ref="orders.payment")

    with pytest.raises(SemanticMetricError) as caught:
        EffectiveMetricCatalog.build(
            governed=(
                _entry(governed, MetricCatalogOrigin.GOVERNED, "metric-set:1"),
            ),
            overlays=(
                _entry(overlay, MetricCatalogOrigin.OVERLAY, "overlay:run-1"),
            ),
        )

    assert caught.value.code == SemanticMetricErrorCode.METRIC_CONFLICT


def test_alias_collision_fails_closed_or_can_be_reported_as_ambiguous() -> None:
    first = _definition("commerce.gmv", "GMV", synonyms=("trade value",))
    second = _definition(
        "finance.trade_value",
        "Trade Value",
        synonyms=("GMV",),
        field_ref="payments.value",
    )
    entries = (
        _entry(first, MetricCatalogOrigin.GOVERNED, "metric-set:commerce"),
        _entry(second, MetricCatalogOrigin.GOVERNED, "metric-set:finance"),
    )

    with pytest.raises(SemanticMetricError) as caught:
        EffectiveMetricCatalog(entries)
    assert caught.value.code == SemanticMetricErrorCode.METRIC_CONFLICT

    catalog = EffectiveMetricCatalog(entries, reject_alias_conflicts=False)
    result = catalog.resolve("GMV")
    assert result.status == MetricResolutionStatus.AMBIGUOUS
    assert {item.metric_ref for item in result.matches} == {
        "commerce.gmv",
        "finance.trade_value",
    }


def test_catalog_digest_is_independent_of_input_order() -> None:
    first = _entry(
        _definition("commerce.gmv", "GMV"),
        MetricCatalogOrigin.GOVERNED,
        "metric-set:commerce",
    )
    second = _entry(
        _definition("commerce.aov", "AOV", field_ref="orders.average"),
        MetricCatalogOrigin.GOVERNED,
        "metric-set:commerce",
    )

    assert EffectiveMetricCatalog((first, second)).digest == EffectiveMetricCatalog(
        (second, first)
    ).digest


def test_require_raises_stable_unresolved_error() -> None:
    catalog = EffectiveMetricCatalog(())

    with pytest.raises(SemanticMetricError) as caught:
        catalog.require("GMV")
    assert caught.value.code == SemanticMetricErrorCode.METRIC_UNRESOLVED
