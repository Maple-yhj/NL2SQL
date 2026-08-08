"""Pinned semantic binding inspection provider."""

from __future__ import annotations

from data_agent.tools.models import ProviderContext, ToolSpec

from .base import dataset_runtime, store_output
from .contracts import DatasetArtifactOutput, EmptyInput


SEMANTIC_INSPECT_SPEC = ToolSpec(
    name="semantic.inspect",
    version="1.0.0",
    description="Inspect logical fields in the pinned active semantic binding",
    input_schema=EmptyInput,
    output_schema=DatasetArtifactOutput,
    risk_level="low",
    side_effects="none",
    required_capabilities=("semantic.inspect",),
    idempotency="safe",
    timeout_seconds=10,
    authority_kinds=("dataset",),
    artifact_policy="metadata",
    credential_requirement="none",
)


class SemanticInspectProvider:
    spec = SEMANTIC_INSPECT_SPEC

    async def invoke(
        self,
        payload: EmptyInput,
        context: ProviderContext,
    ) -> DatasetArtifactOutput:
        del payload
        runtime = dataset_runtime(context)
        return await store_output(
            context=context,
            kind="logical_plan",
            payload=runtime.binding.model_dump(mode="json"),
            summary=f"Semantic binding contains {len(runtime.binding.mappings)} logical fields",
            sensitivity="metadata",
            schema_digest=context.authority.schema_fingerprint,
        )


__all__ = ["SEMANTIC_INSPECT_SPEC", "SemanticInspectProvider"]
