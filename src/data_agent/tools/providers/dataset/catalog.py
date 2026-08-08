"""Pinned catalog inspection provider."""

from __future__ import annotations

from data_agent.tools.models import ProviderContext, ToolSpec

from .base import dataset_runtime, store_output
from .contracts import DatasetArtifactOutput, EmptyInput


CATALOG_INSPECT_SPEC = ToolSpec(
    name="catalog.inspect",
    version="1.0.0",
    description="Inspect the pinned dataset catalog and field metadata",
    input_schema=EmptyInput,
    output_schema=DatasetArtifactOutput,
    risk_level="low",
    side_effects="none",
    required_capabilities=("catalog.inspect",),
    idempotency="safe",
    timeout_seconds=10,
    authority_kinds=("dataset",),
    artifact_policy="metadata",
    credential_requirement="none",
)


class CatalogInspectProvider:
    spec = CATALOG_INSPECT_SPEC

    async def invoke(
        self,
        payload: EmptyInput,
        context: ProviderContext,
    ) -> DatasetArtifactOutput:
        del payload
        runtime = dataset_runtime(context)
        return await store_output(
            context=context,
            kind="catalog",
            payload=runtime.catalog.model_dump(mode="json"),
            summary=f"Catalog contains {len(runtime.catalog.relations)} relations",
            sensitivity="metadata",
            schema_digest=context.authority.schema_fingerprint,
        )


__all__ = ["CATALOG_INSPECT_SPEC", "CatalogInspectProvider"]
