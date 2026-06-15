import unittest
from unittest import mock

from engine.models import QueryIntent
from graph import node, pipeline


class FakeLLM:
    async def complete(self, prompt, system="", max_output_tokens=2048):
        return "unused"


class FakeEmbeddings:
    model_name = "fake"
    dimension = 3

    async def embed_text(self, text):
        return [0.1, 0.2, 0.3]

    async def embed_texts(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


def metrics_result():
    return {
        "ok": True,
        "metrics": [{"metric_name": "gmv", "base_table": "orders", "join_tables": []}],
        "message": "success",
    }


def schema_result():
    return {
        "ok": True,
        "schema": [{"table_name": "orders", "columns": []}],
        "message": "success",
    }


def valid_sql():
    return {
        "ok": True,
        "normalized_sql": "SELECT amount FROM orders LIMIT 100",
        "violations": [],
        "warnings": [],
        "message": "success",
    }


class GraphPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_execute_flow_returns_validated_sql(self):
        with mock.patch.object(
            node, "parse_intent", new=mock.AsyncMock(return_value=QueryIntent(metrics=["gmv"]))
        ), mock.patch.object(
            node, "search_metrics", new=mock.AsyncMock(return_value=metrics_result())
        ), mock.patch.object(
            node, "search_schema", new=mock.AsyncMock(return_value=schema_result())
        ), mock.patch.object(
            node, "generate_sql", new=mock.AsyncMock(return_value="SELECT amount FROM orders")
        ), mock.patch.object(
            node, "validate_sql", new=mock.AsyncMock(return_value=valid_sql())
        ):
            result = await pipeline.run_nl2sql(
                "show gmv",
                llm=FakeLLM(),
                embeddings=FakeEmbeddings(),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sql"], "SELECT amount FROM orders LIMIT 100")
        self.assertEqual(
            [item["node"] for item in result["trace"]],
            ["initialize", "parse_intent", "search_metrics", "search_schema", "generate_sql", "validate_sql"],
        )

    async def test_validation_failure_regenerates_sql(self):
        invalid = {
            "ok": False,
            "normalized_sql": "",
            "violations": [{"code": "table_not_allowed", "message": "users"}],
            "warnings": [],
            "message": "SQL validation failed.",
        }
        with mock.patch.object(
            node, "parse_intent", new=mock.AsyncMock(return_value=QueryIntent(metrics=["gmv"]))
        ), mock.patch.object(
            node, "search_metrics", new=mock.AsyncMock(return_value=metrics_result())
        ), mock.patch.object(
            node, "search_schema", new=mock.AsyncMock(return_value=schema_result())
        ), mock.patch.object(
            node,
            "generate_sql",
            new=mock.AsyncMock(side_effect=["SELECT * FROM users", "SELECT amount FROM orders"]),
        ) as generator, mock.patch.object(
            node, "validate_sql", new=mock.AsyncMock(side_effect=[invalid, valid_sql()])
        ):
            result = await pipeline.run_nl2sql(
                "show gmv",
                llm=FakeLLM(),
                embeddings=FakeEmbeddings(),
                max_validation_attempts=2,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(generator.await_count, 2)

    async def test_execute_flow_explains_rows(self):
        with mock.patch.object(
            node, "parse_intent", new=mock.AsyncMock(return_value=QueryIntent(metrics=["gmv"]))
        ), mock.patch.object(
            node, "search_metrics", new=mock.AsyncMock(return_value=metrics_result())
        ), mock.patch.object(
            node, "search_schema", new=mock.AsyncMock(return_value=schema_result())
        ), mock.patch.object(
            node, "generate_sql", new=mock.AsyncMock(return_value="SELECT amount FROM orders")
        ), mock.patch.object(
            node, "validate_sql", new=mock.AsyncMock(return_value=valid_sql())
        ), mock.patch.object(
            node,
            "execute_sql",
            new=mock.AsyncMock(return_value={"ok": True, "rows": [{"amount": 100}], "message": "success"}),
        ), mock.patch.object(
            node,
            "explain_result",
            new=mock.AsyncMock(return_value={"ok": True, "explanation": "GMV is 100.", "message": "success"}),
        ):
            result = await pipeline.run_nl2sql(
                "show gmv",
                execute=True,
                llm=FakeLLM(),
                embeddings=FakeEmbeddings(),
                dsn="postgresql://example/db",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], [{"amount": 100}])
        self.assertEqual(result["answer"], "GMV is 100.")


if __name__ == "__main__":
    unittest.main()
