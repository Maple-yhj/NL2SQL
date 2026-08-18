"""Compatibility adapters for embedded v1 semantic metrics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from data_agent.datasources.models import SemanticMetricDefinition


class _LegacyMetric(Protocol):
    metric_ref: str
    display_name: str
    description: str
    operation: str
    field_ref: str | None
    unit: str | None
    grain: str | None
    synonyms: tuple[str, ...]

from .ast import MetricAggregateFormula, MetricFieldExpression
from .models import (
    MetricProvenance,
    MetricProvenanceKind,
    SemanticMetricDefinitionV2,
)


class LegacyMetricAdapter:
    """Convert a legacy single-field metric without changing its semantics."""

    @staticmethod
    def to_v2(metric: _LegacyMetric) -> SemanticMetricDefinitionV2:
        operand = (
            MetricFieldExpression(ref=metric.field_ref)
            if metric.field_ref is not None
            else None
        )
        return SemanticMetricDefinitionV2(
            metric_ref=metric.metric_ref,
            display_name=metric.display_name,
            description=metric.description,
            synonyms=metric.synonyms,
            formula=MetricAggregateFormula(
                operation=metric.operation,
                operand=operand,
            ),
            grain=metric.grain,
            unit=metric.unit,
            provenance=(
                MetricProvenance(
                    kind=MetricProvenanceKind.LEGACY_ADAPTER,
                    reference=f"embedded-v1:{metric.metric_ref}",
                ),
            ),
        )

    @classmethod
    def adapt_all(
        cls,
        metrics: tuple[_LegacyMetric, ...],
    ) -> tuple[SemanticMetricDefinitionV2, ...]:
        return tuple(cls.to_v2(metric) for metric in metrics)


__all__ = ["LegacyMetricAdapter"]
