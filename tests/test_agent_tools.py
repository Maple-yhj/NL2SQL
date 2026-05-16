import types
import unittest
from unittest import mock

from agent.tools import sql_store
from agent.tools.validate_sql import validate_sql


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


if __name__ == "__main__":
    unittest.main()
