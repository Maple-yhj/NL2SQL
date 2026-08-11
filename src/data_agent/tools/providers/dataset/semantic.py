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
        binding = runtime.binding
        document = {
            "schemaVersion": binding.schema_version,
            "bindingId": binding.binding_id,
            "bindingVersion": binding.version,
            "domainId": binding.domain_id,
            "fields": [
                {
                    "logicalRef": item.logical_ref,
                    "displayName": item.display_name,
                    "description": item.description,
                    "semanticRole": item.semantic_role,
                    "entity": item.entity,
                    "grain": item.grain,
                    "unit": item.unit,
                    "lifecycleStage": item.lifecycle_stage,
                    "synonyms": list(item.synonyms),
                }
                for item in binding.mappings
            ],
            "metrics": [
                {
                    "metricRef": item.metric_ref,
                    "displayName": item.display_name,
                    "description": item.description,
                    "operation": item.operation,
                    "fieldRef": item.field_ref,
                    "unit": item.unit,
                    "grain": item.grain,
                    "synonyms": list(item.synonyms),
                }
                for item in binding.metrics
            ],
            "relationshipCount": (
                len(binding.graph.edges)
                if binding.schema_version == 2
                else len(binding.relationships)
            ),
        }
        return await store_output(
            context=context,
            kind="logical_plan",
            payload=document,
            summary=f"Semantic binding contains {len(runtime.binding.mappings)} logical fields",
            sensitivity="metadata",
            schema_digest=context.authority.schema_fingerprint,
        )


__all__ = ["SEMANTIC_INSPECT_SPEC", "SemanticInspectProvider"]
