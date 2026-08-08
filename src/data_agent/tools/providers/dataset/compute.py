"""Restricted statistics DSL over current-run artifacts; no arbitrary code."""

from __future__ import annotations

import math
import statistics

from data_agent.runtime.models import AgentMode
from data_agent.tools.models import ProviderContext, ToolSpec
from data_agent.tools.schemas import TabularResult

from .base import dataset_runtime, store_output
from .contracts import ComputationSpec, DatasetArtifactOutput


ANALYSIS_COMPUTE_SPEC = ToolSpec(
    name="analysis.compute", version="1.0.0", description="Run a restricted statistics operation",
    input_schema=ComputationSpec, output_schema=DatasetArtifactOutput, risk_level="low",
    side_effects="none", required_capabilities=("analysis.compute",), idempotency="safe",
    timeout_seconds=20, authority_kinds=("dataset",),
    allowed_modes=(AgentMode.PREVIEW, AgentMode.EXECUTE), artifact_policy="derived",
    credential_requirement="none",
)


def _numeric(result: TabularResult, field: str) -> list[float]:
    try:
        index = result.columns.index(field)
    except ValueError as exc:
        raise ValueError(f"unknown computation field: {field}") from exc
    return [
        float(row.values[index]) for row in result.rows
        if isinstance(row.values[index], (int, float))
        and not isinstance(row.values[index], bool)
        and math.isfinite(float(row.values[index]))
    ]


def _compute(spec: ComputationSpec, result: TabularResult) -> dict[str, object]:
    values = {field: _numeric(result, field) for field in spec.fields}
    if spec.operation == "describe":
        return {field: {
            "count": len(items), "min": min(items) if items else None,
            "max": max(items) if items else None,
            "mean": statistics.fmean(items) if items else None,
        } for field, items in values.items()}
    if spec.operation == "quantiles":
        return {field: statistics.quantiles(items, n=4) if len(items) >= 2 else [] for field, items in values.items()}
    if spec.operation == "correlation":
        left_index, right_index = (
            result.columns.index(field) for field in spec.fields
        )
        pairs = [
            (float(row.values[left_index]), float(row.values[right_index]))
            for row in result.rows
            if all(
                isinstance(row.values[index], (int, float))
                and not isinstance(row.values[index], bool)
                and math.isfinite(float(row.values[index]))
                for index in (left_index, right_index)
            )
        ]
        return {
            "correlation": (
                statistics.correlation(
                    [pair[0] for pair in pairs],
                    [pair[1] for pair in pairs],
                )
                if len(pairs) >= 2
                else None
            )
        }
    if spec.operation == "growth_rate":
        return {field: [
            None if prior == 0 else (current - prior) / prior
            for prior, current in zip(items, items[1:])
        ] for field, items in values.items()}
    if spec.operation == "moving_average":
        window = int(spec.parameters.get("window", 3))
        return {field: [statistics.fmean(items[max(0, index-window+1):index+1]) for index in range(len(items))] for field, items in values.items()}
    if spec.operation == "rank":
        return {field: sorted(enumerate(items), key=lambda item: item[1], reverse=True) for field, items in values.items()}
    if spec.operation == "outlier_iqr":
        output: dict[str, object] = {}
        for field, items in values.items():
            if len(items) < 4:
                output[field] = []
                continue
            q1, _, q3 = statistics.quantiles(items, n=4)
            lower, upper = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
            output[field] = [value for value in items if value < lower or value > upper]
        return output
    raise ValueError("unsupported restricted computation")


class AnalysisComputeProvider:
    spec = ANALYSIS_COMPUTE_SPEC

    async def invoke(self, payload: ComputationSpec, context: ProviderContext) -> DatasetArtifactOutput:
        runtime = dataset_runtime(context)
        document = await runtime.artifacts.get_json(
            tenant_id=runtime.authority.tenant_id, user_id=runtime.authority.user_id,
            run_id=context.run_id, artifact_id=payload.artifact_id,
        )
        result = TabularResult.model_validate(document)
        computed = _compute(payload, result)
        return await store_output(
            context=context, kind="computation", payload=computed,
            summary=f"Computed restricted operation {payload.operation}", sensitivity="derived",
        )


__all__ = ["ANALYSIS_COMPUTE_SPEC", "AnalysisComputeProvider"]
