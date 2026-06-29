import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

from graph.memory_store import (
    InMemoryConversationStore,
    NullConversationStore,
    PostgresConversationStore,
    create_conversation_store,
)


class MemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_store_round_trips_conversation_history(self):
        store = InMemoryConversationStore()

        await store.save_turn(
            tenant_id="demo",
            conversation_id="conv-1",
            user_id="user-1",
            question="show gmv last month",
            contextualized_question="show gmv last month",
            sql="SELECT 100 AS gmv",
            rows=[{"region": "East", "gmv": "1.28M"}],
            answer="GMV is 100.",
            message_type="table",
            ok=True,
            error="",
            trace=[{"node": "finalize", "ok": True}],
        )

        context = await store.load_context(
            tenant_id="demo",
            conversation_id="conv-1",
            user_id="user-1",
            limit=4,
        )

        self.assertEqual([item["role"] for item in context["history"]], ["user", "assistant"])
        self.assertEqual(context["history"][0]["content"], "show gmv last month")
        self.assertEqual(context["history"][1]["metadata"]["sql"], "SELECT 100 AS gmv")
        self.assertEqual(
            context["history"][1]["metadata"]["rows"],
            [{"region": "East", "gmv": "1.28M"}],
        )
        self.assertEqual(context["history"][1]["metadata"]["message_type"], "table")

    async def test_in_memory_store_round_trips_user_memories(self):
        store = InMemoryConversationStore()

        await store.upsert_user_memory(
            tenant_id="demo",
            user_id="user-1",
            memory_key="preferred_region",
            memory_value="华东",
            metadata={"source": "test"},
        )

        context = await store.load_context(
            tenant_id="demo",
            conversation_id="conv-1",
            user_id="user-1",
            limit=4,
        )

        self.assertEqual(
            context["user_memories"],
            [
                {
                    "memory_key": "preferred_region",
                    "memory_value": "华东",
                    "metadata": {"source": "test"},
                }
            ],
        )


    async def test_postgres_store_serializes_non_json_native_assistant_metadata(self):
        class FakeConnection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, *args):
                self.calls.append((sql, args))

            async def close(self):
                self.calls.append(("close", ()))

        conn = FakeConnection()
        store = PostgresConversationStore("postgresql://memory-db")

        with mock.patch("graph.memory_store.asyncpg.connect", new=mock.AsyncMock(return_value=conn)):
            await store.save_turn(
                tenant_id="demo",
                conversation_id="conv-1",
                user_id="user-1",
                question="show latest orders",
                contextualized_question="show latest orders",
                sql="SELECT amount, created_at FROM orders",
                rows=[
                    {
                        "amount": Decimal("1751.64"),
                        "created_at": datetime(2024, 12, 30, 23, 45, tzinfo=timezone.utc),
                    }
                ],
                answer="Returned the latest orders.",
                message_type="table",
                ok=True,
                error="",
                trace=[{"node": "execute_sql", "ok": True}],
            )

        assistant_metadata = conn.calls[2][1][4]
        self.assertIn('"amount": "1751.64"', assistant_metadata)
        self.assertIn('"created_at": "2024-12-30T23:45:00+00:00"', assistant_metadata)


class ConversationStoreFactoryTests(unittest.TestCase):
    def test_factory_does_not_use_query_database_url_for_memory_store(self):
        with mock.patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://query-db"},
            clear=True,
        ):
            store = create_conversation_store()

        self.assertIsInstance(store, NullConversationStore)

    def test_factory_uses_memory_database_url_for_memory_store(self):
        with mock.patch.dict(
            "os.environ",
            {
                "DATABASE_URL": "postgresql://query-db",
                "MEMORY_DATABASE_URL": "postgresql://memory-db",
            },
            clear=True,
        ):
            store = create_conversation_store()

        self.assertIsInstance(store, PostgresConversationStore)
        self.assertEqual(store._resolve_dsn(), "postgresql://memory-db")

    def test_explicit_memory_dsn_takes_precedence(self):
        with mock.patch.dict(
            "os.environ",
            {"MEMORY_DATABASE_URL": "postgresql://memory-db"},
            clear=True,
        ):
            store = create_conversation_store("postgresql://explicit-memory")

        self.assertIsInstance(store, PostgresConversationStore)
        self.assertEqual(store._resolve_dsn(), "postgresql://explicit-memory")


if __name__ == "__main__":
    unittest.main()
