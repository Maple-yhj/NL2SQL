import unittest
from unittest import mock

from engine.models import QueryIntent
from graph import node, pipeline
from graph.memory_store import InMemoryConversationStore


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
            [
                "initialize",
                "load_memory",
                "contextualize_question",
                "parse_intent",
                "plan_query",
                "search_metrics",
                "search_schema",
                "generate_sql",
                "validate_sql",
                "persist_memory",
            ],
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

    async def test_query_dsn_is_not_reused_for_memory_store(self):
        with mock.patch.object(
            pipeline,
            "create_conversation_store",
            return_value=InMemoryConversationStore(),
        ) as create_memory_store, mock.patch.object(
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
            await pipeline.run_nl2sql(
                "show gmv",
                llm=FakeLLM(),
                embeddings=FakeEmbeddings(),
                dsn="postgresql://query-db",
            )

        create_memory_store.assert_called_once_with(None)

    async def test_memory_dsn_is_passed_to_memory_store_factory(self):
        with mock.patch.object(
            pipeline,
            "create_conversation_store",
            return_value=InMemoryConversationStore(),
        ) as create_memory_store, mock.patch.object(
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
            await pipeline.run_nl2sql(
                "show gmv",
                llm=FakeLLM(),
                embeddings=FakeEmbeddings(),
                dsn="postgresql://query-db",
                memory_dsn="postgresql://memory-db",
            )

        create_memory_store.assert_called_once_with("postgresql://memory-db")

    async def test_conversation_history_is_loaded_and_follow_up_is_contextualized(self):
        store = InMemoryConversationStore()
        await store.save_turn(
            tenant_id="demo",
            conversation_id="conv-1",
            user_id="user-1",
            question="show gmv last month",
            contextualized_question="show gmv last month",
            sql="SELECT 100 AS gmv",
            rows=[],
            answer="GMV is 100.",
            ok=True,
            error="",
            trace=[],
        )

        with mock.patch.object(
            node,
            "contextualize_question",
            new=mock.AsyncMock(return_value="show gmv by region last month"),
        ), mock.patch.object(
            node, "parse_intent", new=mock.AsyncMock(return_value=QueryIntent(metrics=["gmv"], dimensions=["region"]))
        ) as parser, mock.patch.object(
            node, "search_metrics", new=mock.AsyncMock(return_value=metrics_result())
        ), mock.patch.object(
            node, "search_schema", new=mock.AsyncMock(return_value=schema_result())
        ), mock.patch.object(
            node, "generate_sql", new=mock.AsyncMock(return_value="SELECT region, SUM(amount) FROM orders GROUP BY region")
        ), mock.patch.object(
            node, "validate_sql", new=mock.AsyncMock(return_value=valid_sql())
        ):
            result = await pipeline.run_nl2sql(
                "那按地区呢",
                tenant_id="demo",
                execute=False,
                conversation_id="conv-1",
                user_id="user-1",
                llm=FakeLLM(),
                embeddings=FakeEmbeddings(),
                memory_store=store,
            )

        parser.assert_awaited_once_with("show gmv by region last month", llm=mock.ANY)
        self.assertEqual(result["contextualized_question"], "show gmv by region last month")
        saved = await store.load_context(
            tenant_id="demo",
            conversation_id="conv-1",
            user_id="user-1",
            limit=10,
        )
        self.assertEqual(saved["history"][-2]["content"], "那按地区呢")


if __name__ == "__main__":
    unittest.main()
