import unittest
from types import SimpleNamespace
from unittest import mock

from engine.models import QueryIntent
from graph.context import GraphContext
from graph.data_memory import DataMemory
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

    async def test_plan_query_node_stores_plan_execution_graph_and_planned_intent(self):
        intent = QueryIntent(metrics=["gmv"], dimensions=["region"])
        rt = runtime()

        result = await node.plan_query_node(
            {
                "question": "show gmv by region",
                "tenant_id": "demo",
                "execute": False,
                "intent": intent,
                "trace": [],
            },
            rt,
        )

        self.assertEqual(result["plan"]["analysis_type"], "multi_dimensional")
        self.assertEqual(result["planned_intent"].metrics, ["gmv"])
        self.assertEqual(result["execution_graph"]["mode"], "fixed_dag")
        self.assertIn('"analysis_type": "multi_dimensional"', result["plan_context"])
        self.assertEqual(result["trace"][-1]["node"], "plan_query")

    async def test_load_memory_reads_conversation_context_from_runtime_store(self):
        class Store:
            async def create_conversation(self, **kwargs):
                return {}

            async def list_conversations(self, **kwargs):
                return []

            async def get_conversation(self, **kwargs):
                return None

            async def update_conversation(self, **kwargs):
                return None

            async def list_messages(self, **kwargs):
                return []

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

    async def test_recall_data_memory_reads_scoped_data_memory(self):
        class Store:
            async def search(self, **kwargs):
                self.kwargs = kwargs
                return [
                    DataMemory(
                        text="Use net GMV after refunds.",
                        scope="global",
                        source="approved",
                    )
                ]

            async def add_episode(self, **kwargs):
                pass

        store = Store()
        rt = runtime(data_memory_store=store, data_memory_recall_limit=3)

        result = await node.recall_data_memory_node(
            {
                "question": "gmv trend",
                "contextualized_question": "gmv trend",
                "tenant_id": "demo",
                "execute": False,
                "conversation_id": "conv-1",
                "user_id": "user-1",
                "trace": [],
            },
            rt,
        )

        self.assertEqual(result["data_memories"][0]["text"], "Use net GMV after refunds.")
        self.assertEqual(result["data_memories"][0]["scope"], "global")
        self.assertEqual(store.kwargs["tenant_id"], "demo")
        self.assertEqual(store.kwargs["limit"], 3)

    async def test_persist_memory_saves_turn_without_affecting_result(self):
        class Store:
            async def create_conversation(self, **kwargs):
                return {}

            async def list_conversations(self, **kwargs):
                return []

            async def get_conversation(self, **kwargs):
                return None

            async def update_conversation(self, **kwargs):
                return None

            async def list_messages(self, **kwargs):
                return []

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

    async def test_propose_memory_updates_exposes_pending_updates_without_writing(self):
        rt = runtime()

        result = await node.propose_memory_updates_node(
            {
                "question": "remember: GMV excludes refunded orders by default",
                "contextualized_question": "remember: GMV excludes refunded orders by default",
                "tenant_id": "demo",
                "execute": False,
                "conversation_id": "conv-1",
                "user_id": "user-1",
                "validated_sql": "SELECT 1",
                "answer": "",
                "error": "",
                "trace": [],
            },
            rt,
        )

        self.assertEqual(result["pending_memory_updates"][0]["scope"], "user")
        self.assertIn("GMV excludes refunded orders", result["pending_memory_updates"][0]["text"])
        self.assertEqual(result["trace"][-1]["node"], "propose_memory_updates")

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
                {
                    "question": "show gmv",
                    "tenant_id": "demo",
                    "execute": False,
                    "plan": {"analysis_type": "single_metric", "metrics": [{"name": "gmv"}]},
                },
                rt,
            )

        self.assertEqual(result["table_names"], ["orders", "refunds"])
        search.assert_awaited_once_with(
            query="show gmv\nanalysis_type: single_metric\nmetrics: gmv",
            tenant_id="demo",
            embedding_client=rt.context.embeddings,
        )

    async def test_metric_search_extends_olist_schema_scope_from_domain_profile(self):
        metrics_result = {
            "ok": True,
            "metrics": [
                {
                    "metric_name": "gmv",
                    "base_table": "olist_order_items_dataset",
                    "join_tables": [],
                }
            ],
        }
        rt = runtime()
        with mock.patch.object(
            node, "search_metrics", new=mock.AsyncMock(return_value=metrics_result)
        ):
            result = await node.search_metrics_node(
                {
                    "question": "按客户州统计 2018 年 GMV",
                    "tenant_id": "admin",
                    "execute": False,
                    "intent": QueryIntent(metrics=["gmv"], dimensions=["customer_state"]),
                    "plan": {
                        "analysis_type": "multi_dimensional",
                        "metrics": [{"name": "gmv"}],
                        "dimensions": [{"name": "customer_state"}],
                    },
                },
                rt,
            )

        self.assertEqual(
            result["table_names"],
            [
                "olist_order_items_dataset",
                "olist_orders_dataset",
                "olist_customers_dataset",
            ],
        )
        self.assertIn("DOMAIN: OList E-Commerce", result["domain_context"])

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
        ) as search:
            result = await node.search_schema_node(state, rt)

        self.assertEqual(result["allowed_tables"], ["orders", "refunds"])
        search.assert_awaited_once_with(
            query="show gmv",
            tenant_id="demo",
            embedding_client=rt.context.embeddings,
            table_names=["orders", "refunds"],
        )

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

    async def test_validation_uses_explicit_intent_limit_as_max_limit(self):
        validation = {
            "ok": True,
            "normalized_sql": "SELECT amount FROM orders LIMIT 20",
            "message": "success",
            "violations": [],
            "warnings": [],
        }
        rt = runtime(max_limit=100)
        state = {
            "question": "show top orders, return 20",
            "tenant_id": "demo",
            "execute": False,
            "candidate_sql": "SELECT amount FROM orders",
            "allowed_tables": ["orders"],
            "planned_intent": QueryIntent(dimensions=["order_id"], limit=20),
            "validation_attempts": 0,
        }
        with mock.patch.object(
            node, "validate_sql", new=mock.AsyncMock(return_value=validation)
        ) as validator:
            result = await node.validate_sql_node(state, rt)

        self.assertEqual(result["validated_sql"], "SELECT amount FROM orders LIMIT 20")
        validator.assert_awaited_once_with(
            sql="SELECT amount FROM orders",
            tenant_id="demo",
            allowed_tables=["orders"],
            max_limit=20,
        )

    async def test_validation_failure_applies_domain_constraints_after_safe_sql_check(self):
        rt = runtime()
        state = {
            "question": "customers by state and city count",
            "tenant_id": "admin",
            "execute": False,
            "candidate_sql": (
                "SELECT 'state' AS state, 'city' AS city, "
                "COUNT(DISTINCT olist_order_items_dataset.order_id) AS customer_count "
                "FROM olist_order_items_dataset "
                "JOIN olist_order_reviews_dataset "
                "ON olist_order_items_dataset.order_id = olist_order_reviews_dataset.order_id "
                "GROUP BY state, city "
                "ORDER BY customer_count DESC "
                "LIMIT 20"
            ),
            "allowed_tables": [
                "olist_customers_dataset",
                "olist_order_items_dataset",
                "olist_order_reviews_dataset",
            ],
            "domain_constraints": {
                "matched_rules": ["customer_count_by_location"],
                "required_tables": ["olist_customers_dataset"],
                "required_columns": [
                    "olist_customers_dataset.customer_state",
                    "olist_customers_dataset.customer_city",
                    "olist_customers_dataset.customer_id",
                ],
                "required_group_by": ["customer_state", "customer_city"],
                "required_order_by": ["customer_count DESC"],
                "required_sql_fragments": ["COUNT(DISTINCT", "customer_id"],
                "forbidden_tables": [
                    "olist_order_items_dataset",
                    "olist_order_reviews_dataset",
                ],
            },
            "validation_attempts": 0,
            "rows": [{"stale": True}],
        }

        result = await node.validate_sql_node(state, rt)

        self.assertEqual(result["validation_attempts"], 1)
        self.assertEqual(result["validated_sql"], "")
        self.assertFalse(result["validation_result"]["ok"])
        self.assertIn("domain_forbidden_table", result["retry_feedback"])
        self.assertIn("olist_customers_dataset", result["retry_feedback"])

    async def test_explain_node_uses_table_explanation_for_detail_rows(self):
        rt = runtime()
        with mock.patch.object(
            node,
            "explain_table_result",
            new=mock.AsyncMock(
                return_value={
                    "ok": True,
                    "explanation": "Orders are concentrated late in the day.",
                    "message": "success",
                }
            ),
        ) as table_explainer, mock.patch.object(
            node,
            "explain_result",
            new=mock.AsyncMock(return_value={"ok": True, "explanation": "unused"}),
        ) as summary_explainer:
            result = await node.explain_node(
                {
                    "question": "show yesterday order records",
                    "tenant_id": "demo",
                    "execute": True,
                    "validated_sql": (
                        "SELECT order_id, amount, created_at "
                        "FROM orders ORDER BY created_at DESC"
                    ),
                    "rows": [{"order_id": "O-1", "amount": 100}],
                    "metrics_result": {},
                    "trace": [],
                },
                rt,
            )

        self.assertEqual(result["answer"], "Orders are concentrated late in the day.")
        table_explainer.assert_awaited_once()
        summary_explainer.assert_not_awaited()

    async def test_explain_node_uses_table_explanation_for_topn_aggregate_lists(self):
        rt = runtime()
        with mock.patch.object(
            node,
            "explain_table_result",
            new=mock.AsyncMock(
                return_value={
                    "ok": True,
                    "explanation": "Sao Paulo leads the returned city ranking.",
                    "message": "success",
                }
            ),
        ) as table_explainer, mock.patch.object(
            node,
            "explain_result",
            new=mock.AsyncMock(return_value={"ok": True, "explanation": "unused"}),
        ) as summary_explainer:
            result = await node.explain_node(
                {
                    "question": "top 20 customer state-city combinations by customer count",
                    "tenant_id": "demo",
                    "execute": True,
                    "validated_sql": (
                        "SELECT customer_state, customer_city, COUNT(*) AS customer_count "
                        "FROM customers GROUP BY customer_state, customer_city "
                        "ORDER BY customer_count DESC LIMIT 20"
                    ),
                    "rows": [
                        {"customer_state": "SP", "customer_city": "sao paulo", "customer_count": 15540},
                        {"customer_state": "RJ", "customer_city": "rio de janeiro", "customer_count": 6882},
                    ],
                    "metrics_result": {},
                    "trace": [],
                },
                rt,
            )

        self.assertEqual(result["answer"], "Sao Paulo leads the returned city ranking.")
        table_explainer.assert_awaited_once()
        summary_explainer.assert_not_awaited()

    async def test_explain_node_uses_summary_explanation_for_aggregate_rows(self):
        rt = runtime()
        with mock.patch.object(
            node,
            "explain_result",
            new=mock.AsyncMock(
                return_value={
                    "ok": True,
                    "explanation": "GMV is higher in East.",
                    "message": "success",
                }
            ),
        ) as summary_explainer, mock.patch.object(
            node,
            "explain_table_result",
            new=mock.AsyncMock(return_value={"ok": True, "explanation": "unused"}),
        ) as table_explainer:
            result = await node.explain_node(
                {
                    "question": "show gmv by region",
                    "tenant_id": "demo",
                    "execute": True,
                    "validated_sql": "SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region",
                    "rows": [{"region": "East", "gmv": 100}],
                    "metrics_result": {},
                    "trace": [],
                },
                rt,
            )

        self.assertEqual(result["answer"], "GMV is higher in East.")
        summary_explainer.assert_awaited_once()
        table_explainer.assert_not_awaited()

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
                "message_type": "text",
                "rows": [],
                "answer": "",
                "error": "",
                "trace": [{"node": "validate_sql", "ok": True}],
                "pending_memory_updates": [],
            },
        )

    async def test_finalize_keeps_scalar_row_results_as_text_messages(self):
        result = await node.finalize_node(
            {
                "question": "count orders",
                "tenant_id": "demo",
                "execute": True,
                "validated_sql": "SELECT COUNT(*) AS count FROM orders",
                "execution_result": {"ok": True},
                "rows": [{"count": 50000}],
                "answer": "There are 50,000 orders.",
                "error": "",
                "trace": [{"node": "execute_sql", "ok": True}],
            }
        )

        self.assertEqual(result["message_type"], "text")

    async def test_finalize_marks_detail_record_results_as_table_messages(self):
        result = await node.finalize_node(
            {
                "question": "show yesterday order records",
                "tenant_id": "demo",
                "execute": True,
                "validated_sql": (
                    "SELECT order_id, customer_id, amount, created_at "
                    "FROM orders ORDER BY created_at DESC"
                ),
                "execution_result": {"ok": True},
                "rows": [{"order_id": "O-1", "customer_id": "C-1", "amount": 100}],
                "answer": "Found 1 order.",
                "error": "",
                "trace": [{"node": "execute_sql", "ok": True}],
            }
        )

        self.assertEqual(result["message_type"], "table")

    async def test_finalize_marks_errors_as_error_messages(self):
        result = await node.finalize_node(
            {
                "question": "show gmv",
                "tenant_id": "demo",
                "execute": False,
                "validated_sql": "",
                "rows": [],
                "answer": "",
                "error": "SQL validation failed.",
                "trace": [{"node": "validate_sql", "ok": False}],
            }
        )

        self.assertEqual(result["message_type"], "error")


if __name__ == "__main__":
    unittest.main()
