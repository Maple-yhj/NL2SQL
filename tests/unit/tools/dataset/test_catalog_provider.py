from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
