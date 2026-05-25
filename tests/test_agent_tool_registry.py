import unittest
from unittest import mock

from agent.tools import registry


class AgentToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_registry_lists_existing_react_tools(self):
        specs = registry.list_tool_specs()

        self.assertEqual(
            [spec.name for spec in specs],
            [
                "search_metrics",
                "search_schema",
                "validate_sql",
                "execute_sql",
                "explain_result",
            ],
        )
        self.assertNotIn(
            "tenant_id",
            registry.get_tool_spec("search_metrics").input_schema["properties"],
        )

    async def test_search_metrics_uses_controlled_tenant_context(self):
        result = {"ok": True, "metrics": []}
        context = registry.ToolContext(question="查询 GMV", tenant_id="tenant-a")

        with mock.patch.object(
            registry,
            "search_metrics",
            new=mock.AsyncMock(return_value=result),
        ) as search_metrics:
            actual = await registry.call_tool(
                "search_metrics",
                {"query": "GMV", "top_k": 2},
                context,
            )

        self.assertEqual(actual, result)
        search_metrics.assert_awaited_once_with(
            query="GMV",
            tenant_id="tenant-a",
            top_k=2,
            min_score=None,
        )

    async def test_call_tool_rejects_model_supplied_context_parameter(self):
        context = registry.ToolContext(question="查询 GMV", tenant_id="tenant-a")

        with self.assertRaisesRegex(ValueError, "tenant_id"):
            await registry.call_tool(
                "search_metrics",
                {"query": "GMV", "tenant_id": "tenant-b"},
                context,
            )

    async def test_search_schema_uses_table_scope_from_context(self):
        result = {"ok": True, "schema": []}
        context = registry.ToolContext(
            question="查询 GMV",
            tenant_id="tenant-a",
            table_names=["orders"],
        )

        with mock.patch.object(
            registry,
            "search_schema",
            new=mock.AsyncMock(return_value=result),
        ) as search_schema:
            actual = await registry.call_tool(
                "search_schema",
                {"query": "订单金额"},
                context,
            )

        self.assertEqual(actual, result)
        search_schema.assert_awaited_once_with(
            query="订单金额",
            tenant_id="tenant-a",
            top_k=8,
            min_score=None,
            table_names=["orders"],
        )

    async def test_execute_sql_requires_explicit_execution_permission(self):
        context = registry.ToolContext(
            question="查询 GMV",
            tenant_id="tenant-a",
            validated_sql="SELECT amount FROM orders LIMIT 1000",
        )

        with self.assertRaisesRegex(PermissionError, "disabled"):
            await registry.call_tool("execute_sql", {}, context)

    async def test_validate_sql_uses_allowed_tables_from_context(self):
        result = {"ok": True, "normalized_sql": "SELECT amount FROM orders LIMIT 100"}
        context = registry.ToolContext(
            question="查询 GMV",
            tenant_id="tenant-a",
            allowed_tables=["orders"],
            max_limit=100,
        )

        with mock.patch.object(
            registry,
            "validate_sql",
            new=mock.AsyncMock(return_value=result),
        ) as validate_sql:
            actual = await registry.call_tool(
                "validate_sql",
                {"sql": "select amount from orders"},
                context,
            )

        self.assertEqual(actual, result)
        validate_sql.assert_awaited_once_with(
            sql="select amount from orders",
            tenant_id="tenant-a",
            allowed_tables=["orders"],
            max_limit=100,
        )

    async def test_execute_sql_uses_validated_sql_and_execution_context(self):
        result = {"ok": True, "rows": [{"amount": 100}]}
        context = registry.ToolContext(
            question="查询 GMV",
            tenant_id="tenant-a",
            allowed_tables=["orders"],
            validated_sql="SELECT amount FROM orders LIMIT 100",
            execute_enabled=True,
            dsn="postgres://example",
            timeout_ms=1200,
            max_limit=100,
        )

        with mock.patch.object(
            registry,
            "execute_sql",
            new=mock.AsyncMock(return_value=result),
        ) as execute_sql:
            actual = await registry.call_tool("execute_sql", {}, context)

        self.assertEqual(actual, result)
        execute_sql.assert_awaited_once_with(
            sql="SELECT amount FROM orders LIMIT 100",
            tenant_id="tenant-a",
            dsn="postgres://example",
            timeout_ms=1200,
            max_limit=100,
            allowed_tables=["orders"],
        )

    async def test_explain_result_uses_query_execution_state(self):
        result = {"ok": True, "explanation": "查询返回 1 行。"}
        context = registry.ToolContext(
            question="查询 GMV",
            tenant_id="tenant-a",
            metrics_result={"metrics": [{"metric_name": "gmv"}]},
            validated_sql="SELECT amount FROM orders LIMIT 100",
            execution_rows=[{"amount": 100}],
            llm=object(),
        )

        with mock.patch.object(
            registry,
            "explain_result",
            new=mock.AsyncMock(return_value=result),
        ) as explain_result:
            actual = await registry.call_tool(
                "explain_result",
                {"max_preview_rows": 1},
                context,
            )

        self.assertEqual(actual, result)
        explain_result.assert_awaited_once_with(
            question="查询 GMV",
            sql="SELECT amount FROM orders LIMIT 100",
            rows=[{"amount": 100}],
            metrics_result={"metrics": [{"metric_name": "gmv"}]},
            llm=context.llm,
            max_preview_rows=1,
        )


if __name__ == "__main__":
    unittest.main()
