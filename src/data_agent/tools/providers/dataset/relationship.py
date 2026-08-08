"""Deterministic relationship-route inspection provider."""

from __future__ import annotations

from data_agent.datasources import SemanticGraphBindingRecord
from data_agent.relationships.router import GraphRouteRequest, GraphRouteResolver
from data_agent.tools.models import ProviderContext, ToolSpec

from .base import DatasetProviderError, dataset_runtime, store_output
from .contracts import DatasetArtifactOutput, RelationshipRouteInput
from data_agent.tools.models import ToolErrorCode


RELATIONSHIP_ROUTE_SPEC = ToolSpec(
    name="relationship.route",
    version="1.0.0",
    description="Resolve the deterministic relationship route for logical fields",
    input_schema=RelationshipRouteInput,
    output_schema=DatasetArtifactOutput,
    risk_level="low",
    side_effects="none",
    required_capabilities=("relationship.route",),
    idempotency="safe",
    timeout_seconds=10,
    authority_kinds=("dataset",),
    artifact_policy="metadata",
    credential_requirement="none",
)


class RelationshipRouteProvider:
    spec = RELATIONSHIP_ROUTE_SPEC

    async def invoke(
        self,
        payload: RelationshipRouteInput,
        context: ProviderContext,
    ) -> DatasetArtifactOutput:
        runtime = dataset_runtime(context)
        mappings = {item.logical_ref: item for item in runtime.binding.mappings}
        unknown = set(payload.logical_refs) - set(mappings)
        if unknown:
            raise DatasetProviderError(
                ToolErrorCode.LOGICAL_PLAN_INVALID,
                "relationship route references unknown logical fields",
            )
        if isinstance(runtime.binding, SemanticGraphBindingRecord):
            required = tuple(dict.fromkeys(mappings[item].node_id for item in payload.logical_refs))
            route = GraphRouteResolver().resolve(
                runtime.binding.graph,
                GraphRouteRequest(required_node_ids=required),
            )
            document = route.model_dump(mode="json")
            summary = f"Resolved {len(route.steps)} relationship steps"
        else:
            relations = tuple(
                dict.fromkeys(mappings[item].physical_relation for item in payload.logical_refs)
            )
            document = {
                "root_relation": runtime.binding.primary_relation,
                "relations": relations,
                "relationship_ids": [
                    item.relationship_id
                    for item in runtime.binding.relationships
                    if item.left_relation in relations or item.right_relation in relations
                ],
            }
            summary = f"Resolved {len(relations)} bound relations"
        return await store_output(
            context=context,
            kind="logical_plan",
            payload=document,
            summary=summary,
            sensitivity="metadata",
            schema_digest=context.authority.schema_fingerprint,
        )


__all__ = ["RELATIONSHIP_ROUTE_SPEC", "RelationshipRouteProvider"]
