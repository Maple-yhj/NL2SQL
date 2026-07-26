from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile

from api.dataset_query_service import DataSourceQueryService
from api.datasource_service import DataSourceService
from data_agent.datasources import SemanticFieldMapping
from data_agent.runtime.errors import ErrorCode
from data_agent.runtime.models import AgentRequest, PrincipalContext


class _DatasetModel:
    model_id = "test.dataset-planner"
    version = "1"

    def __init__(self, document: dict[str, object] | None = None) -> None:
        self.prompts: list[str] = []
        self.document = document

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        del system, max_output_tokens
        self.prompts.append(prompt)
        return json.dumps(
            self.document
            or {
                "analysis_type": "detail",
                "select": [
                    "dataset.Orders.order_id",
                    "dataset.Orders.total",
                ],
                "filters": [
                    {
                        "ref": "dataset.Orders.total",
                        "operator": "gte",
                        "value": 10,
                    }
                ],
                "order_by": [
                    {
                        "ref": "dataset.Orders.total",
                        "direction": "desc",
                    }
                ],
                "limit": 10,
            }
        )


class DatasetQueryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_sources = DataSourceService(
            state_root=self.temporary_directory.name,
        )
        await self.data_sources.import_file_source(
            tenant_id="tenant-a",
            source_id="orders",
            name="Orders",
            uploads=[
                UploadFile(
                    filename="orders.csv",
                    file=BytesIO(
                        b"order_id,amount\nA-1,5\nA-2,20\nA-3,10\n"
                    ),
                )
            ],
        )
        binding = await self.data_sources.create_binding(
            tenant_id="tenant-a",
            source_id="orders",
            binding_id="orders-binding",
            domain_id="dataset.orders",
            mappings=(
                SemanticFieldMapping(
                    logical_ref="dataset.Orders.order_id",
                    physical_relation="public.orders",
                    physical_column="order_id",
                ),
                SemanticFieldMapping(
                    logical_ref="dataset.Orders.total",
                    physical_relation="public.orders",
                    physical_column="amount",
                ),
            ),
        )
        self.binding = await self.data_sources.activate_binding(
            tenant_id="tenant-a",
            source_id="orders",
            binding_id=binding.binding_id,
        )
        self.model = _DatasetModel()
        self.service = DataSourceQueryService(self.data_sources, self.model)
        self.principal = PrincipalContext(
            tenant_id="tenant-a",
            user_id="analyst-a",
        )

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    def request(self, *, binding_version: int | None = None) -> AgentRequest:
        return AgentRequest(
            question="show orders at least 10",
            enterprise_id="user-dataset",
            domain_id="dataset.orders",
            conversation_id="conversation-a",
            source_id="orders",
            source_version=1,
            binding_id=self.binding.binding_id,
            binding_version=binding_version or self.binding.version,
            include_trace=True,
        )

    async def test_executes_compiler_generated_query_from_logical_refs(self) -> None:
        response = await self.service.run(self.request(), self.principal)

        self.assertTrue(response.ok)
        self.assertEqual(
            [row.root["order_id"] for row in response.rows],
            ["A-2", "A-3"],
        )
        self.assertIn('"public"."orders"', response.sql or "")
        self.assertIn("$1", response.sql or "")
        self.assertEqual(
            [item.node for item in response.trace],
            [
                "resolve_datasource",
                "plan_dataset_query",
                "compile_dataset_query",
                "execute_dataset_query",
            ],
        )
        prompt = self.model.prompts[0]
        self.assertNotIn("public.orders", prompt)
        self.assertNotIn('"amount"', prompt)
        self.assertIn("dataset.Orders.total", prompt)
        pinned = await self.data_sources.get_conversation_binding(
            tenant_id="tenant-a",
            user_id="analyst-a",
            conversation_id="conversation-a",
        )
        self.assertEqual(pinned, self.binding)

    async def test_rejects_stale_binding_before_model_planning(self) -> None:
        response = await self.service.run(
            self.request(binding_version=self.binding.version + 1),
            self.principal,
        )

        self.assertFalse(response.ok)
        self.assertEqual(response.error.code, ErrorCode.BINDING_STALE)
        self.assertEqual(self.model.prompts, [])

    async def test_aggregate_result_emits_only_a_field_bound_safe_chart(self) -> None:
        model = _DatasetModel(
            {
                "analysis_type": "aggregate",
                "aggregations": [
                    {
                        "ref": "dataset.Orders.total",
                        "operation": "sum",
                        "alias": "total_amount",
                    }
                ],
                "group_by": ["dataset.Orders.order_id"],
                "order_by": [
                    {"ref": "total_amount", "direction": "desc"}
                ],
                "limit": 10,
            }
        )
        service = DataSourceQueryService(self.data_sources, model)

        response = await service.run(self.request(), self.principal)

        self.assertTrue(response.ok)
        self.assertEqual(response.message_type, "chart")
        self.assertEqual(response.chart.x_field, "order_id")
        self.assertEqual(response.chart.y_field, "total_amount")
        self.assertEqual(len(response.rows), 3)
        self.assertIn("order_id=A-2", response.answer or "")
        self.assertIn("total_amount 最高", response.answer or "")

    async def test_executes_uploaded_sqlite_through_the_same_query_path(self) -> None:
        source_path = Path(self.temporary_directory.name) / "customers.sqlite"
        connection = sqlite3.connect(source_path)
        try:
            connection.execute(
                "CREATE TABLE customers (customer_id TEXT, city TEXT)"
            )
            connection.executemany(
                "INSERT INTO customers VALUES (?, ?)",
                [("C-1", "Shanghai"), ("C-2", "Beijing")],
            )
            connection.commit()
        finally:
            connection.close()
        await self.data_sources.import_sqlite_source(
            tenant_id="tenant-a",
            source_id="customers",
            name="Customers",
            upload=UploadFile(
                filename="customers.sqlite",
                file=BytesIO(source_path.read_bytes()),
            ),
        )
        draft = await self.data_sources.create_binding(
            tenant_id="tenant-a",
            source_id="customers",
            binding_id="customers-binding",
            domain_id="dataset.customers",
            mappings=(
                SemanticFieldMapping(
                    logical_ref="dataset.Customers.customer_id",
                    physical_relation="main.customers",
                    physical_column="customer_id",
                ),
                SemanticFieldMapping(
                    logical_ref="dataset.Customers.city",
                    physical_relation="main.customers",
                    physical_column="city",
                ),
            ),
        )
        binding = await self.data_sources.activate_binding(
            tenant_id="tenant-a",
            source_id="customers",
            binding_id=draft.binding_id,
        )
        model = _DatasetModel(
            {
                "analysis_type": "detail",
                "select": [
                    "dataset.Customers.customer_id",
                    "dataset.Customers.city",
                ],
                "filters": [
                    {
                        "ref": "dataset.Customers.city",
                        "operator": "eq",
                        "value": "Shanghai",
                    }
                ],
                "limit": 10,
            }
        )
        service = DataSourceQueryService(self.data_sources, model)

        response = await service.run(
            AgentRequest(
                question="customers in Shanghai",
                enterprise_id="user-dataset",
                domain_id=binding.domain_id,
                source_id=binding.source_id,
                source_version=binding.source_snapshot_version,
                binding_id=binding.binding_id,
                binding_version=binding.version,
            ),
            self.principal,
        )

        self.assertTrue(response.ok)
        self.assertEqual(
            [row.root for row in response.rows],
            [{"customer_id": "C-1", "city": "Shanghai"}],
        )
        self.assertIn("@1", response.sql or "")


if __name__ == "__main__":
    unittest.main()
