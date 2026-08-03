from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile

from api.dataset_query_service import (
    DataSourceQueryService,
    DatasetConversationContext,
    DatasetPlanStatus,
    DatasetQueryPlan,
)
from api.datasource_service import DataSourceService
from data_agent.datasources import SemanticFieldMapping, SemanticRelationship
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


class _PostgresConnection:
    def __init__(self) -> None:
        self.executed_queries: list[str] = []

    @asynccontextmanager
    async def transaction(self, *, readonly: bool):
        assert readonly
        yield

    async def execute(self, query: str, *args) -> str:
        del args
        self.executed_queries.append(query)
        return "SELECT 1"

    async def fetch(self, query: str, *args, timeout=None):
        del args, timeout
        self.executed_queries.append(query)
        if "information_schema.columns" in query:
            return [
                {
                    "table_schema": "analytics",
                    "table_name": "orders",
                    "column_name": "order_id",
                    "data_type": "text",
                    "is_nullable": "NO",
                },
                {
                    "table_schema": "analytics",
                    "table_name": "orders",
                    "column_name": "amount",
                    "data_type": "numeric",
                    "is_nullable": "YES",
                },
            ]
        return [{"order_id": "PG-1", "total": 42}]


class _PostgresPool:
    def __init__(self) -> None:
        self.connection = _PostgresConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def close(self) -> None:
        return None


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
                        b"order_id,amount,state\n"
                        b"A-1,5,RJ\nA-2,20,SP\nA-3,10,SP\n"
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
                SemanticFieldMapping(
                    logical_ref="dataset.Orders.state",
                    physical_relation="public.orders",
                    physical_column="state",
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

    def request(
        self,
        *,
        binding_version: int | None = None,
        question: str = "show orders at least 10",
    ) -> AgentRequest:
        return AgentRequest(
            question=question,
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

    async def test_executes_explicit_multi_table_relationship_query(self) -> None:
        await self.data_sources.import_file_source(
            tenant_id="tenant-a",
            source_id="sales-dataset",
            name="Sales dataset",
            uploads=[
                UploadFile(
                    filename="customers.csv",
                    file=BytesIO(
                        "customer_id,customer_name,region\n"
                        "C-1,East Retail,East\n"
                        "C-2,South Trade,South\n"
                        "C-3,North Store,North\n".encode()
                    ),
                ),
                UploadFile(
                    filename="orders.csv",
                    file=BytesIO(
                        b"order_id,customer_id,amount\n"
                        b"O-1,C-1,1200\n"
                        b"O-2,C-1,800\n"
                        b"O-3,C-2,900\n"
                        b"O-4,C-3,450\n"
                    ),
                ),
            ],
        )
        draft = await self.data_sources.create_binding(
            tenant_id="tenant-a",
            source_id="sales-dataset",
            binding_id="sales-dataset-binding",
            domain_id="dataset.sales",
            primary_relation="public.customers",
            mappings=(
                SemanticFieldMapping(
                    logical_ref="dataset.Customers.region",
                    physical_relation="public.customers",
                    physical_column="region",
                ),
                SemanticFieldMapping(
                    logical_ref="dataset.Orders.amount",
                    physical_relation="public.orders",
                    physical_column="amount",
                ),
            ),
            relationships=(
                SemanticRelationship(
                    relationship_id="customers_orders",
                    left_relation="public.customers",
                    left_column="customer_id",
                    right_relation="public.orders",
                    right_column="customer_id",
                ),
            ),
        )
        binding = await self.data_sources.activate_binding(
            tenant_id="tenant-a",
            source_id="sales-dataset",
            binding_id=draft.binding_id,
        )
        model = _DatasetModel(
            {
                "analysis_type": "aggregate",
                "aggregations": [
                    {
                        "ref": "dataset.Orders.amount",
                        "operation": "sum",
                        "alias": "total_amount",
                    }
                ],
                "group_by": ["dataset.Customers.region"],
                "order_by": [
                    {"ref": "total_amount", "direction": "desc"}
                ],
                "limit": 10,
            }
        )
        service = DataSourceQueryService(self.data_sources, model)
        request = AgentRequest(
            question="sum order amount by region",
            enterprise_id="user-dataset",
            domain_id="dataset.sales",
            conversation_id="conversation-multi-table",
            source_id="sales-dataset",
            source_version=1,
            binding_id=binding.binding_id,
            binding_version=binding.version,
            include_trace=True,
        )

        response = await service.run(request, self.principal)

        self.assertTrue(response.ok, response.error)
        self.assertEqual(
            [(row.root["region"], row.root["total_amount"]) for row in response.rows],
            [("East", 2000), ("South", 900), ("North", 450)],
        )
        self.assertIn('INNER JOIN "public"."orders"', response.sql or "")
        self.assertIn(
            '"dataset_1"."customer_id" = "dataset_2"."customer_id"',
            response.sql or "",
        )
        self.assertNotIn("public.customers", model.prompts[0])
        self.assertNotIn("public.orders", model.prompts[0])

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

    async def test_refund_rate_requires_clarification_instead_of_substitution(
        self,
    ) -> None:
        model = _DatasetModel(
            {
                "analysis_type": "aggregate",
                "aggregations": [
                    {
                        "ref": "dataset.Orders.order_id",
                        "operation": "count",
                        "alias": "order_count",
                    }
                ],
                "group_by": ["dataset.Orders.state"],
                "limit": 10,
            }
        )
        service = DataSourceQueryService(self.data_sources, model)

        response = await service.run(
            self.request(question="退款率是多少？"),
            self.principal,
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.message_type, "clarification")
        self.assertIn("没有可识别的退款或退货字段", response.answer or "")
        self.assertIsNone(response.sql)
        self.assertEqual(response.rows, ())
        self.assertEqual(
            response.dataset_query_plan["status"],
            DatasetPlanStatus.NEEDS_CLARIFICATION.value,
        )

    async def test_follow_up_patch_preserves_aggregation_and_adds_filter(
        self,
    ) -> None:
        prior_plan = DatasetQueryPlan(
            analysis_type="aggregate",
            aggregations=(
                {
                    "ref": "dataset.Orders.total",
                    "operation": "sum",
                    "alias": "total_amount",
                },
            ),
            group_by=("dataset.Orders.state",),
            order_by=({"ref": "total_amount", "direction": "desc"},),
            limit=10,
        )
        model = _DatasetModel(
            {
                "add_filters": [
                    {
                        "ref": "dataset.Orders.state",
                        "operator": "eq",
                        "value": "SP",
                    }
                ]
            }
        )
        service = DataSourceQueryService(self.data_sources, model)

        response = await service.run(
            self.request(question="只看 SP 州。"),
            self.principal,
            conversation_context=DatasetConversationContext(
                prior_question="按州汇总订单金额",
                prior_plan=prior_plan,
            ),
        )

        self.assertTrue(response.ok, response.error)
        self.assertEqual(
            [row.root for row in response.rows],
            [{"state": "SP", "total_amount": 30}],
        )
        persisted_plan = DatasetQueryPlan.model_validate(
            response.dataset_query_plan
        )
        self.assertEqual(persisted_plan.group_by, ("dataset.Orders.state",))
        self.assertEqual(persisted_plan.aggregations, prior_plan.aggregations)
        self.assertEqual(persisted_plan.filters[0].value, "SP")
        self.assertIn("追问：只看 SP 州。", response.contextualized_question or "")
        self.assertIn("followUpQuestion", model.prompts[0])
        self.assertIn("priorPlan", model.prompts[0])

    async def test_independent_question_replaces_prior_plan(self) -> None:
        prior_plan = DatasetQueryPlan(
            analysis_type="aggregate",
            aggregations=(
                {
                    "ref": "dataset.Orders.total",
                    "operation": "sum",
                    "alias": "total_amount",
                },
            ),
            group_by=("dataset.Orders.state",),
            filters=(
                {
                    "ref": "dataset.Orders.state",
                    "operator": "eq",
                    "value": "SP",
                },
            ),
        )
        model = _DatasetModel(
            {
                "mode": "replace",
                "plan": {
                    "analysis_type": "detail",
                    "select": ["dataset.Orders.order_id"],
                    "limit": 2,
                },
            }
        )
        service = DataSourceQueryService(self.data_sources, model)

        response = await service.run(
            self.request(question="列出两个订单编号"),
            self.principal,
            conversation_context=DatasetConversationContext(
                prior_question="只看 SP 州的州级汇总",
                prior_plan=prior_plan,
            ),
        )

        self.assertTrue(response.ok, response.error)
        replacement = DatasetQueryPlan.model_validate(
            response.dataset_query_plan
        )
        self.assertEqual(replacement.analysis_type, "detail")
        self.assertEqual(replacement.filters, ())
        self.assertEqual(response.contextualized_question, "列出两个订单编号")

    async def test_model_can_explicitly_mark_a_question_unsupported(self) -> None:
        service = DataSourceQueryService(
            self.data_sources,
            _DatasetModel(
                {
                    "status": "unsupported",
                    "clarification_question": "当前数据集不包含天气数据。",
                }
            ),
        )

        response = await service.run(
            self.request(question="明天会下雨吗？"),
            self.principal,
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.message_type, "clarification")
        self.assertEqual(response.answer, "当前数据集不包含天气数据。")
        self.assertEqual(
            response.dataset_query_plan["status"],
            DatasetPlanStatus.UNSUPPORTED.value,
        )

    async def test_postgres_query_uses_resolved_credential_authority(
        self,
    ) -> None:
        pool = _PostgresPool()

        async def create_pool(dsn: str):
            self.assertEqual(dsn, "postgresql://resolved")
            return pool

        data_sources = DataSourceService(
            state_root=self.temporary_directory.name,
            secret_resolver=lambda ref: (
                "postgresql://resolved"
                if ref == "secret://tenant/warehouse"
                else ""
            ),
            postgres_pool_factory=create_pool,
        )
        await data_sources.register_postgres(
            tenant_id="tenant-a",
            source_id="warehouse",
            name="Warehouse",
            credential_ref="secret://tenant/warehouse",
            options={"host": "metadata-only", "database": "analytics"},
        )
        snapshot = await data_sources.get_snapshot(
            tenant_id="tenant-a",
            source_id="warehouse",
        )
        draft = await data_sources.create_binding(
            tenant_id="tenant-a",
            source_id="warehouse",
            binding_id="warehouse-binding",
            domain_id="dataset.warehouse",
            mappings=(
                SemanticFieldMapping(
                    logical_ref="dataset.Orders.order_id",
                    physical_relation="analytics.orders",
                    physical_column="order_id",
                ),
                SemanticFieldMapping(
                    logical_ref="dataset.Orders.total",
                    physical_relation="analytics.orders",
                    physical_column="amount",
                ),
            ),
        )
        binding = await data_sources.activate_binding(
            tenant_id="tenant-a",
            source_id="warehouse",
            binding_id=draft.binding_id,
        )
        service = DataSourceQueryService(data_sources, _DatasetModel())

        response = await service.run(
            AgentRequest(
                question="show orders at least 10",
                enterprise_id="user-dataset",
                domain_id=binding.domain_id,
                source_id=binding.source_id,
                source_version=snapshot.version,
                binding_id=binding.binding_id,
                binding_version=binding.version,
            ),
            self.principal,
        )

        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.rows[0].root["order_id"], "PG-1")
        self.assertEqual(response.rows[0].root["total"], 42)
        self.assertTrue(
            any(
                '"analytics"."orders"' in query
                for query in pool.connection.executed_queries
            )
        )
        await data_sources.close()


if __name__ == "__main__":
    unittest.main()
