import unittest
from types import SimpleNamespace
from unittest import mock

from engine.models import QueryIntent
from graph.context import GraphContext
from graph import node


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


def runtime(**overrides):
    values = {
        "llm": FakeLLM(),
        "embeddings": FakeEmbeddings(),
        "dsn": "postgresql://example/db",
        "timeout_ms": 1200,
        "max_limit": 100,
        "max_validation_attempts": 2,
    }
    values.update(overrides)
    return SimpleNamespace(context=GraphContext(**values))


class GraphNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_intent_uses_context_llm(self):
        intent = QueryIntent(metrics=["gmv"])
        rt = runtime()
        with mock.patch.object(
            node, "parse_intent", new=mock.AsyncMock(return_value=intent)
        ) as parser:
            result = await node.parse_intent_node(
                {"question": "show gmv", "tenant_id": "demo", "execute": False},
                rt,
            )

        self.assertEqual(result["intent"], intent)
        parser.assert_awaited_once_with("show gmv", llm=rt.context.llm)

    async def test_parse_intent_uses_contextualized_question_when_present(self):
        intent = QueryIntent(metrics=["gmv"])
        rt = runtime()
        with mock.patch.object(
            node, "parse_intent", new=mock.AsyncMock(return_value=intent)
        ) as parser:
            result = await node.parse_intent_node(
                {
                    "question": "那按地区呢",
                    "contextualized_question": "show gmv by region",
                    "tenant_id": "demo",
                    "execute": False,
                },
                rt,
            )

        self.assertEqual(result["intent"], intent)
        parser.assert_awaited_once_with("show gmv by region", llm=rt.context.llm)

    async def test_load_memory_reads_conversation_context_from_runtime_store(self):
        class Store:
            async def load_context(self, **kwargs):
                self.kwargs = kwargs
                return {
                    "history": [{"role": "user", "content": "show gmv", "metadata": {}}],
                    "user_memories": [{"memory_key": "timezone", "memory_value": "Asia/Shanghai", "metadata": {}}],
                }

            async def save_turn(self, **kwargs):
                pass

            async def upsert_user_memory(self, **kwargs):
                pass

        store = Store()
        rt = runtime(memory_store=store, memory_history_limit=6)

        result = await node.load_memory_node(
            {
                "question": "那按地区呢",
                "tenant_id": "demo",
                "execute": False,
                "conversation_id": "conv-1",
                "user_id": "user-1",
            },
            rt,
        )

        self.assertEqual(result["conversation_history"][0]["content"], "show gmv")
        self.assertEqual(result["user_memories"][0]["memory_value"], "Asia/Shanghai")
        self.assertEqual(store.kwargs["limit"], 6)

    async def test_contextualize_question_node_uses_loaded_memory(self):
        rt = runtime()
        with mock.patch.object(
            node,
            "contextualize_question",
            new=mock.AsyncMock(return_value="show gmv by region"),
        ) as contextualizer:
            result = await node.contextualize_question_node(
                {
                    "question": "那按地区呢",
                    "tenant_id": "demo",
                    "execute": False,
                    "conversation_history": [{"role": "user", "content": "show gmv", "metadata": {}}],
                    "user_memories": [{"memory_key": "timezone", "memory_value": "Asia/Shanghai", "metadata": {}}],
                },
                rt,
            )

        self.assertEqual(result["contextualized_question"], "show gmv by region")
        contextualizer.assert_awaited_once()

    async def test_persist_memory_saves_turn_without_affecting_result(self):
        class Store:
            async def load_context(self, **kwargs):
                return {"history": [], "user_memories": []}

            async def save_turn(self, **kwargs):
                self.kwargs = kwargs

            async def upsert_user_memory(self, **kwargs):
                pass

        store = Store()
        rt = runtime(memory_store=store)

        result = await node.persist_memory_node(
            {
                "question": "show gmv",
                "contextualized_question": "show gmv",
                "tenant_id": "demo",
                "execute": False,
                "conversation_id": "conv-1",
                "user_id": "user-1",
                "validated_sql": "SELECT 1",
                "answer": "GMV is 100.",
                "error": "",
                "trace": [{"node": "validate_sql", "ok": True}],
            },
            rt,
        )

        self.assertEqual(store.kwargs["conversation_id"], "conv-1")
        self.assertEqual(store.kwargs["sql"], "SELECT 1")
        self.assertEqual(result["trace"][-1]["node"], "persist_memory")

    async def test_metric_search_derives_schema_table_scope(self):
        metrics_result = {
            "ok": True,
            "metrics": [
                {
                    "base_table": "orders o",
                    "join_tables": ["LEFT JOIN refunds r ON r.order_id = o.id"],
                }
            ],
        }
        rt = runtime()
        with mock.patch.object(
            node, "search_metrics", new=mock.AsyncMock(return_value=metrics_result)
        ) as search:
            result = await node.search_metrics_node(
                {"question": "show gmv", "tenant_id": "demo", "execute": False},
                rt,
            )

        self.assertEqual(result["table_names"], ["orders", "refunds"])
        search.assert_awaited_once_with(
            query="show gmv",
            tenant_id="demo",
            embedding_client=rt.context.embeddings,
        )

    async def test_schema_search_sets_allowed_tables(self):
        result_payload = {
            "ok": True,
            "schema": [{"table_name": "orders"}, {"table_name": "refunds"}],
        }
        rt = runtime()
        state = {
            "question": "show gmv",
            "tenant_id": "demo",
            "execute": False,
            "table_names": ["orders", "refunds"],
        }
        with mock.patch.object(
            node, "search_schema", new=mock.AsyncMock(return_value=result_payload)
        ):
            result = await node.search_schema_node(state, rt)

        self.assertEqual(result["allowed_tables"], ["orders", "refunds"])

    async def test_validation_failure_builds_retry_feedback(self):
        validation = {
            "ok": False,
            "message": "SQL validation failed.",
            "violations": [{"code": "table_not_allowed", "message": "users"}],
            "warnings": [],
        }
        rt = runtime()
        state = {
            "question": "show gmv",
            "tenant_id": "demo",
            "execute": False,
            "candidate_sql": "SELECT * FROM users",
            "allowed_tables": ["orders"],
            "validation_attempts": 0,
            "validated_sql": "stale",
            "rows": [{"stale": True}],
        }
        with mock.patch.object(
            node, "validate_sql", new=mock.AsyncMock(return_value=validation)
        ):
            result = await node.validate_sql_node(state, rt)

        self.assertEqual(result["validation_attempts"], 1)
        self.assertEqual(result["validated_sql"], "")
        self.assertEqual(result["rows"], [])
        self.assertIn("SELECT * FROM users", result["retry_feedback"])
        self.assertIn("table_not_allowed", result["retry_feedback"])

    async def test_finalize_exposes_only_stable_output(self):
        intent = QueryIntent(metrics=["gmv"])
        result = await node.finalize_node(
            {
                "question": "show gmv",
                "tenant_id": "demo",
                "execute": False,
                "intent": intent,
                "validated_sql": "SELECT 1",
                "rows": [],
                "answer": "",
                "trace": [{"node": "validate_sql", "ok": True}],
            }
        )

        self.assertEqual(
            result,
            {
                "ok": True,
                "question": "show gmv",
                "contextualized_question": "show gmv",
                "conversation_id": "",
                "user_id": "",
                "tenant_id": "demo",
                "intent": {
                    "metrics": ["gmv"],
                    "time_range": {},
                    "dimensions": [],
                    "filters": [],
                },
                "sql": "SELECT 1",
                "rows": [],
                "answer": "",
                "error": "",
                "trace": [{"node": "validate_sql", "ok": True}],
            },
        )


if __name__ == "__main__":
    unittest.main()
