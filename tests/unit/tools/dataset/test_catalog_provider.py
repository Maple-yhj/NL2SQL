from __future__ import annotations

import unittest
from dataclasses import replace

from data_agent.datasources import (
    SemanticBindingStatus,
    SemanticGraphBindingRecord,
    SemanticGraphFieldMapping,
)
from data_agent.relationships.models import ActivatedRelationshipGraph, RelationshipGraphNode
from data_agent.runtime.models import AgentMode
from data_agent.tools.providers.dataset.contracts import EmptyInput, RelationshipRouteInput

from ._support import DatasetToolHarness, invoke


class DatasetCatalogProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = DatasetToolHarness()

    def tearDown(self) -> None:
        self.harness.close()

    async def test_registry_has_only_the_twelve_governed_dataset_tools(self) -> None:
        expected = (
            "catalog.inspect",
            "semantic.inspect",
            "relationship.route",
            "query.compile",
            "query.explain",
            "query.preview",
            "query.execute",
            "data.profile",
            "result.profile",
            "analysis.compute",
            "chart.render",
            "evidence.collect",
        )
        self.assertEqual(self.harness.registry.names(), expected)
        self.assertTrue(all(spec.authority_kinds == ("dataset",) for spec in self.harness.registry.specs()))

    async def test_catalog_semantic_and_relationship_outputs_are_artifacts(self) -> None:
        runtime, invoker, context = self.harness.invocation(AgentMode.PLAN)
        catalog = await invoke(
            invoker,
            context,
            call_id="catalog-inspect",
            tool_name="catalog.inspect",
            input_data=EmptyInput(),
        )
        semantic = await invoke(
            invoker,
            context,
            call_id="semantic-inspect",
            tool_name="semantic.inspect",
            input_data=EmptyInput(),
        )
        route = await invoke(
            invoker,
            context,
            call_id="relationship-route",
            tool_name="relationship.route",
            input_data=RelationshipRouteInput(logical_refs=("dataset.orders.amount",)),
        )

        for result in (catalog, semantic, route):
            self.assertEqual(result.status, "success")
            self.assertEqual(len(result.artifact_refs), 1)
            self.assertEqual(result.redacted_trace.artifact_ids, (result.artifact_refs[0].artifact_id,))
            self.assertEqual(len(result.redacted_trace.safe_args_digest or ""), 64)

        refs = await runtime.artifacts.list_for_run(
            tenant_id=self.harness.tenant_id,
            user_id=self.harness.user_id,
            run_id="run-1",
        )
        self.assertEqual({item.kind.value for item in refs}, {"catalog", "logical_plan"})

    async def test_relationship_route_serializes_graph_binding_result(self) -> None:
        runtime, invoker, context = self.harness.invocation(AgentMode.PLAN)
        relation = self.harness.catalog.relations[0]
        amount = next(column for column in relation.columns if column.name == "amount")
        graph_binding = SemanticGraphBindingRecord(
            binding_id=self.harness.binding_id,
            tenant_id=self.harness.tenant_id,
            source_id=self.harness.source_id,
            source_snapshot_version=self.harness.source_version,
            schema_fingerprint=self.harness.schema_fingerprint,
            domain_id="dataset-orders",
            version=self.harness.binding_version,
            status=SemanticBindingStatus.ACTIVE,
            graph=ActivatedRelationshipGraph(
                graph_id="orders-graph",
                revision=1,
                nodes=(
                    RelationshipGraphNode(
                        node_id="orders",
                        relation_id=relation.relation_id,
                        role_name="orders",
                        logical_entity="Orders",
                    ),
                ),
                edges=(),
                components=(),
            ),
            mappings=(
                SemanticGraphFieldMapping(
                    logical_ref="dataset.orders.amount",
                    node_id="orders",
                    column_id=amount.column_id,
                ),
            ),
            validation_report_digest="sha256:test-report",
        )
        graph_runtime = replace(runtime, binding=graph_binding)
        graph_context = replace(context, runtime_resources=graph_runtime)

        result = await invoke(
            invoker,
            graph_context,
            call_id="relationship-route-graph",
            tool_name="relationship.route",
            input_data=RelationshipRouteInput(logical_refs=("dataset.orders.amount",)),
        )

        self.assertEqual(result.status, "success")
        assert result.typed_data is not None
        self.assertEqual(result.typed_data.summary, "Resolved 0 relationship steps")
        preview = result.typed_data.safe_preview
        self.assertIsInstance(preview, dict)
        self.assertEqual(preview["root_node_id"], "orders")


if __name__ == "__main__":
    unittest.main()
