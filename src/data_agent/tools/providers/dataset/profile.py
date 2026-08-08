"""Bounded source and result profiling providers."""

from __future__ import annotations

from collections import Counter

from data_agent.runtime.models import AgentMode
from data_agent.tools.models import ProviderContext, ToolSpec
from data_agent.tools.schemas import TabularResult

from .base import (
    dataset_runtime,
    execution_authority,
    store_output,
    validate_prepared_authority,
)
from .contracts import ArtifactInput, DatasetArtifactOutput, QueryRunInput, prepared_from_payload


DATA_PROFILE_SPEC = ToolSpec(
    name="data.profile", version="1.0.0", description="Profile a bounded query preview",
    input_schema=QueryRunInput, output_schema=DatasetArtifactOutput, risk_level="medium",
    side_effects="read", required_capabilities=("data.profile",), idempotency="required",
    timeout_seconds=30, authority_kinds=("dataset",),
    allowed_modes=(AgentMode.PREVIEW, AgentMode.EXECUTE), artifact_policy="derived",
    credential_requirement="required",
)
RESULT_PROFILE_SPEC = ToolSpec(
    name="result.profile", version="1.0.0", description="Profile a current-run result artifact",
    input_schema=ArtifactInput, output_schema=DatasetArtifactOutput, risk_level="low",
    side_effects="none", required_capabilities=("result.profile",), idempotency="safe",
    timeout_seconds=15, authority_kinds=("dataset",),
    allowed_modes=(AgentMode.PREVIEW, AgentMode.EXECUTE), artifact_policy="derived",
    credential_requirement="none",
)


def _profile(result: TabularResult) -> dict[str, object]:
    columns: list[dict[str, object]] = []
    for index, name in enumerate(result.columns):
        values = [row.values[index] for row in result.rows]
        types = Counter(type(value).__name__ for value in values if value is not None)
        columns.append({
            "field": name,
            "null_count": sum(value is None for value in values),
            "unique_count": len({str(value) for value in values if value is not None}),
            "types": dict(sorted(types.items())),
        })
    return {"row_count": len(result.rows), "truncated": result.truncated, "columns": columns}


class DataProfileProvider:
    spec = DATA_PROFILE_SPEC

    async def invoke(self, payload: QueryRunInput, context: ProviderContext) -> DatasetArtifactOutput:
        runtime = dataset_runtime(context)
        document = await runtime.artifacts.get_json(
            tenant_id=runtime.authority.tenant_id, user_id=runtime.authority.user_id,
            run_id=context.run_id, artifact_id=payload.artifact_id,
        )
        prepared = prepared_from_payload(document)
        validate_prepared_authority(prepared, context)
        result = await runtime.executor.execute(
            prepared=prepared, authority=execution_authority(runtime, context), connector=runtime.connector,
            mode=AgentMode.PREVIEW, preview_rows=payload.preview_rows,
        )
        return await store_output(
            context=context, kind="profile", payload=_profile(result),
            summary=f"Profiled {len(result.rows)} preview rows", sensitivity="derived",
        )


class ResultProfileProvider:
    spec = RESULT_PROFILE_SPEC

    async def invoke(self, payload: ArtifactInput, context: ProviderContext) -> DatasetArtifactOutput:
        runtime = dataset_runtime(context)
        document = await runtime.artifacts.get_json(
            tenant_id=runtime.authority.tenant_id, user_id=runtime.authority.user_id,
            run_id=context.run_id, artifact_id=payload.artifact_id,
        )
        result = TabularResult.model_validate(document)
        return await store_output(
            context=context, kind="profile", payload=_profile(result),
            summary=f"Profiled {len(result.rows)} result rows", sensitivity="derived",
        )


__all__ = ["DATA_PROFILE_SPEC", "RESULT_PROFILE_SPEC", "DataProfileProvider", "ResultProfileProvider"]
