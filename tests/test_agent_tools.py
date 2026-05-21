import importlib
import types
import unittest
from unittest import mock

from agent.tools import sql_store
from agent.tools.descriptions import get_tool_description
from agent.tools.validate_sql import validate_sql

execute_sql_tool = importlib.import_module("agent.tools.execute_sql")
explain_result_tool = importlib.import_module("agent.tools.explain_result")


class FakeEmbeddingClient:
    def __init__(self):
        self.queries = []

    async def embed_text(self, text: str):
        self.queries.append(text)
        return [0.1, 0.2, 0.3]


def metric_hit(name: str, score: float):
    return types.SimpleNamespace(
        object_type="metric",
        similarity=score,
        metadata={
            "metric_name": name,
            "display_name": name.upper(),
            "business_def": f"{name} business definition",
            "sql_expr": f"SUM({name})",
            "base_table": "orders",
            "time_column": "paid_at",
            "dimensions": ["region"],
            "filters": ["status = 'paid'"],
            "join_tables": [],
            "forbidden": [],
            "synonyms": [name, f"{name}_alias"],
        },
    )


def table_hit(table_name: str, score: float, comment: str = ""):
    return types.SimpleNamespace(
        object_type="table",
        similarity=score,
        metadata={
            "table_name": table_name,
            "comment": comment,
        },
    )


def column_hit(table_name: str, column_name: str, score: float, comment: str = ""):
    return types.SimpleNamespace(
        object_type="column",
        similarity=score,
        metadata={
            "table_name": table_name,
            "column_name": column_name,
            "data_type": "numeric",
            "nullable": False,
            "default": None,
            "comment": comment,
            "sample_values": ["1", "2"],
        },
    )


class AgentToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_metrics_returns_hits_and_respects_min_score(self):
        calls = {}

        async def fake_search_semantic_index(
            query_embedding,
            *,
            tenant_id,
            object_types,
            top_k,
        ):
            calls["query_embedding"] = query_embedding
            calls["tenant_id"] = tenant_id
            calls["object_types"] = object_types
            calls["top_k"] = top_k
            return [
                metric_hit("gmv", 0.91),
                metric_hit("refund_rate", 0.42),
            ]

        with mock.patch.object(sql_store, "GeminiEmbeddingClient", FakeEmbeddingClient), mock.patch.object(
            sql_store,
            "search_semantic_index",
            side_effect=fake_search_semantic_index,
        ):
            result = await sql_store.search_metrics(
                query="GMV",
                tenant_id="demo",
                top_k=3,
                min_score=0.5,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["query"], "GMV")
        self.assertEqual(result["tenant_id"], "demo")
        self.assertEqual(calls["query_embedding"], [0.1, 0.2, 0.3])
        self.assertEqual(calls["object_types"], ["metric"])
        self.assertEqual(calls["top_k"], 3)
        self.assertEqual(len(result["metrics"]), 1)
        self.assertEqual(result["metrics"][0]["metric_name"], "gmv")
        self.assertEqual(result["metrics"][0]["score"], 0.91)

    async def test_search_schema_merges_columns_and_keeps_highest_score(self):
        async def fake_search_semantic_index(
            query_embedding,
            *,
            tenant_id,
            object_types,
            top_k,
        ):
            return [
                table_hit("orders", 0.70, "Order fact table"),
                column_hit("orders", "amount", 0.50, "lower score amount"),
                column_hit("orders", "amount", 0.95, "higher score amount"),
                column_hit("orders", "paid_at", 0.80, "Paid time"),
                column_hit("users", "region", 0.90, "Filtered out by table_names"),
            ]

        with mock.patch.object(sql_store, "GeminiEmbeddingClient", FakeEmbeddingClient), mock.patch.object(
            sql_store,
            "search_semantic_index",
            side_effect=fake_search_semantic_index,
        ):
            result = await sql_store.search_schema(
                query="orders amount paid time",
                tenant_id="demo",
                top_k=8,
                table_names=["orders"],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["schema"]), 1)

        orders = result["schema"][0]
        self.assertEqual(orders["table_name"], "orders")
        self.assertEqual(orders["table_comment"], "Order fact table")
        self.assertIsInstance(orders["columns"], list)
        self.assertEqual([col["column_name"] for col in orders["columns"]], ["amount", "paid_at"])
        self.assertEqual(orders["columns"][0]["score"], 0.95)
        self.assertEqual(orders["columns"][0]["comment"], "higher score amount")

    async def test_search_schema_rejects_invalid_object_type(self):
        with self.assertRaisesRegex(ValueError, "object_types"):
            await sql_store.search_schema(
                query="orders",
                tenant_id="demo",
                object_types=("metric",),
            )

    async def test_validate_sql_adds_limit_and_returns_normalized_sql(self):
        result = await validate_sql(
            "select * from orders",
            tenant_id="demo",
            allowed_tables=["orders"],
            max_limit=1000,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["tables"], ["orders"])
        self.assertEqual(result["limit"], 1000)
        self.assertEqual(result["normalized_sql"], "SELECT * FROM orders LIMIT 1000")
        self.assertEqual(result["warnings"], ["LIMIT was missing and has been set to 1000."])

    async def test_validate_sql_blocks_disallowed_table(self):
        result = await validate_sql(
            "select * from users",
            tenant_id="demo",
            allowed_tables=["orders"],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["tables"], ["users"])
        self.assertEqual(result["violations"][0]["code"], "table_not_allowed")

    async def test_validate_sql_blocks_multiple_statements(self):
        result = await validate_sql(
            "select * from orders; drop table users",
            tenant_id="demo",
            allowed_tables=["orders"],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["violations"][0]["code"], "multiple_statements")

    async def test_execute_sql_runs_normalized_sql_and_reports_row_count(self):
        class FakeConn:
            def __init__(self):
                self.executed = []
                self.fetched_sql = None
                self.closed = False

            async def execute(self, sql):
                self.executed.append(sql)

            async def fetch(self, sql):
                self.fetched_sql = sql
                return [{"id": 1}, {"id": 2}]

            async def close(self):
                self.closed = True

        conn = FakeConn()

        with mock.patch.object(
            execute_sql_tool,
            "_connect",
            new=mock.AsyncMock(return_value=conn),
        ):
            result = await execute_sql_tool.execute_sql(
                "select * from orders",
                "demo",
                dsn="postgres://example",
                allowed_tables=["orders"],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["normalized_sql"], "SELECT * FROM orders LIMIT 1000")
        self.assertEqual(conn.fetched_sql, "SELECT * FROM orders LIMIT 1000")
        self.assertEqual(result["rows"], [{"id": 1}, {"id": 2}])
        self.assertEqual(result["row_count"], 2)
        self.assertTrue(conn.closed)

    async def test_execute_sql_returns_failure_when_db_connection_fails(self):
        with mock.patch.object(
            execute_sql_tool,
            "_connect",
            new=mock.AsyncMock(side_effect=RuntimeError("database unavailable")),
        ):
            result = await execute_sql_tool.execute_sql(
                "select * from orders",
                "demo",
                dsn="postgres://example",
                allowed_tables=["orders"],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["normalized_sql"], "SELECT * FROM orders LIMIT 1000")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["row_count"], 0)
        self.assertIn("SQL execution failed", result["message"])
        self.assertIn("database unavailable", result["message"])

    async def test_execute_sql_returns_failure_and_closes_connection_when_fetch_fails(self):
        class FakeConn:
            def __init__(self):
                self.closed = False
                self.fetched_sql = None

            async def execute(self, sql):
                return None

            async def fetch(self, sql):
                self.fetched_sql = sql
                raise RuntimeError("fetch failed")

            async def close(self):
                self.closed = True

        conn = FakeConn()

        with mock.patch.object(
            execute_sql_tool,
            "_connect",
            new=mock.AsyncMock(return_value=conn),
        ):
            result = await execute_sql_tool.execute_sql(
                "select * from orders",
                "demo",
                dsn="postgres://example",
                allowed_tables=["orders"],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["normalized_sql"], "SELECT * FROM orders LIMIT 1000")
        self.assertEqual(conn.fetched_sql, "SELECT * FROM orders LIMIT 1000")
        self.assertTrue(conn.closed)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["row_count"], 0)
        self.assertIn("SQL execution failed", result["message"])
        self.assertIn("fetch failed", result["message"])

    async def test_execute_sql_has_tool_description(self):
        description = get_tool_description("execute_sql")

        self.assertIn("execute_sql", description)
        self.assertIn("validate_sql", description)
        self.assertIn("rows", description)
        self.assertIn("row_count", description)

    async def test_explain_result_summarizes_rows(self):
        result = await explain_result_tool.explain_result(
            question="各地区 GMV 是多少？",
            sql="select region, gmv from orders",
            rows=[
                {"region": "华东", "gmv": 100},
                {"region": "华南", "gmv": 80},
            ],
            metrics_result={
                "metrics": [
                    {
                        "metric_name": "gmv",
                        "display_name": "GMV",
                    }
                ]
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["columns"], ["region", "gmv"])
        self.assertEqual(result["preview_rows"], [{"region": "华东", "gmv": 100}, {"region": "华南", "gmv": 80}])
        self.assertIn("返回 2 行", result["explanation"])
        self.assertIn("GMV", result["explanation"])
        self.assertIn("region=华东", result["explanation"])
        self.assertIn("gmv=100", result["explanation"])

    async def test_explain_result_handles_empty_rows(self):
        result = await explain_result_tool.explain_result(
            question="今日订单数是多少？",
            sql="select count(*) as order_count from orders",
            rows=[],
            metrics_result={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["columns"], [])
        self.assertEqual(result["preview_rows"], [])
        self.assertIn("没有返回数据", result["explanation"])

    async def test_explain_result_has_tool_description(self):
        description = get_tool_description("explain_result")

        self.assertIn("explain_result", description)
        self.assertIn("rows", description)
        self.assertIn("explanation", description)


if __name__ == "__main__":
    unittest.main()
