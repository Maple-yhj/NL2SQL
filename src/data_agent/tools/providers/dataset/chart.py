"""Safe chart specification rendering from current-run result artifacts."""

from __future__ import annotations

from data_agent.runtime.models import AgentMode, ChartSpec
from data_agent.tools.models import ProviderContext, ToolSpec
from data_agent.tools.schemas import TabularResult

from .base import dataset_runtime, store_output
from .contracts import ChartRenderInput, DatasetArtifactOutput


CHART_RENDER_SPEC = ToolSpec(
    name="chart.render", version="1.0.0", description="Render a safe chart specification",
    input_schema=ChartRenderInput, output_schema=DatasetArtifactOutput, risk_level="low",
    side_effects="none", required_capabilities=("chart.render",), idempotency="safe",
    timeout_seconds=15, authority_kinds=("dataset",),
    allowed_modes=(AgentMode.PREVIEW, AgentMode.EXECUTE), artifact_policy="derived",
    credential_requirement="none",
)


class ChartRenderProvider:
    spec = CHART_RENDER_SPEC

    async def invoke(self, payload: ChartRenderInput, context: ProviderContext) -> DatasetArtifactOutput:
        runtime = dataset_runtime(context)
        document = await runtime.artifacts.get_json(
            tenant_id=runtime.authority.tenant_id, user_id=runtime.authority.user_id,
            run_id=context.run_id, artifact_id=payload.artifact_id,
        )
        result = TabularResult.model_validate(document)
        if payload.x_field not in result.columns or payload.y_field not in result.columns:
            raise ValueError("chart fields must belong to the result artifact")
        y_index = result.columns.index(payload.y_field)
        if not any(
            isinstance(row.values[y_index], (int, float))
            and not isinstance(row.values[y_index], bool)
            for row in result.rows
        ):
            raise ValueError("chart y field must contain numeric values")
        chart = ChartSpec(
            title=payload.title, x_field=payload.x_field, y_field=payload.y_field,
        )
        return await store_output(
            context=context, kind="chart", payload=chart.model_dump(mode="json"),
            summary=f"Rendered chart {payload.title}", sensitivity="derived",
        )


__all__ = ["CHART_RENDER_SPEC", "ChartRenderProvider"]
