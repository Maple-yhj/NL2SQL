import unittest

from graph.memory_store import InMemoryConversationStore


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
            answer="GMV is 100.",
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


if __name__ == "__main__":
    unittest.main()
