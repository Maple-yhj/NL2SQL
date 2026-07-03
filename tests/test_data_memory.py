import unittest
from unittest import mock

from graph.data_memory import (
    DataMemory,
    DataMemoryScope,
    GraphitiDataMemoryStore,
    InMemoryDataMemoryStore,
    NullDataMemoryStore,
    create_data_memory_store,
    data_memory_group_id,
    extract_pending_memory_updates,
    format_data_memories,
)


class DataMemoryScopeTests(unittest.IsolatedAsyncioTestCase):
    def test_data_memory_group_id_scopes_by_tenant_user_and_conversation(self):
        self.assertEqual(
            data_memory_group_id(
                tenant_id="demo",
                scope=DataMemoryScope.GLOBAL,
            ),
            "tenant:demo:global",
        )
        self.assertEqual(
            data_memory_group_id(
                tenant_id="demo",
                user_id="user-1",
                scope=DataMemoryScope.USER,
            ),
            "tenant:demo:user:user-1",
        )
        self.assertEqual(
            data_memory_group_id(
                tenant_id="demo",
                conversation_id="conv-1",
                scope=DataMemoryScope.CONVERSATION,
            ),
            "tenant:demo:conversation:conv-1",
        )

    def test_data_memory_group_id_rejects_missing_scope_identity(self):
        with self.assertRaises(ValueError):
            data_memory_group_id(tenant_id="demo", scope=DataMemoryScope.USER)
        with self.assertRaises(ValueError):
            data_memory_group_id(tenant_id="demo", scope=DataMemoryScope.CONVERSATION)
        with self.assertRaises(ValueError):
            data_memory_group_id(tenant_id="", scope=DataMemoryScope.GLOBAL)

    async def test_null_store_returns_empty_context_and_accepts_updates(self):
        store = NullDataMemoryStore()

        self.assertEqual(
            await store.search(
                tenant_id="demo",
                user_id="user-1",
                conversation_id="conv-1",
                query="gmv by seller",
                limit=5,
            ),
            [],
        )
        await store.add_episode(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            scope=DataMemoryScope.USER,
            name="correction",
            body={"text": "Use net GMV after refunds."},
            source_description="manual correction",
        )


class DataMemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_store_searches_allowed_scopes_only(self):
        store = InMemoryDataMemoryStore()
        await store.add_episode(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            scope=DataMemoryScope.GLOBAL,
            name="metric-rule",
            body={"text": "GMV uses paid order amount."},
            source_description="global",
        )
        await store.add_episode(
            tenant_id="demo",
            user_id="user-2",
            conversation_id="conv-1",
            scope=DataMemoryScope.USER,
            name="private-rule",
            body={"text": "User 2 prefers refunds excluded."},
            source_description="private",
        )
        await store.add_episode(
            tenant_id="other",
            user_id="user-1",
            conversation_id="conv-1",
            scope=DataMemoryScope.GLOBAL,
            name="other-tenant-rule",
            body={"text": "GMV means something else."},
            source_description="other",
        )

        results = await store.search(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            query="gmv refunds",
            limit=10,
        )

        self.assertEqual([item.text for item in results], ["GMV uses paid order amount."])

    async def test_in_memory_store_returns_newest_matching_memory_first(self):
        store = InMemoryDataMemoryStore()
        await store.add_episode(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            scope=DataMemoryScope.USER,
            name="older",
            body="GMV uses gross order amount.",
            source_description="manual",
        )
        await store.add_episode(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            scope=DataMemoryScope.USER,
            name="newer",
            body="GMV uses net order amount.",
            source_description="manual",
        )

        results = await store.search(
            tenant_id="demo",
            user_id="user-1",
            conversation_id="conv-1",
            query="gmv order amount",
            limit=2,
        )

        self.assertEqual([item.text for item in results], ["GMV uses net order amount.", "GMV uses gross order amount."])

    def test_format_data_memories_uses_scope_and_source(self):
        rendered = format_data_memories(
            [
                DataMemory(text="Use orders for GMV.", scope="global", source="approved"),
                DataMemory(text="Prefer last 30 days.", scope="user", source="manual"),
            ]
        )

        self.assertIn("- [global] Use orders for GMV. (source: approved)", rendered)
        self.assertIn("- [user] Prefer last 30 days. (source: manual)", rendered)


class GraphitiDataMemoryFactoryTests(unittest.TestCase):
    def test_factory_returns_null_store_when_data_memory_is_disabled(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(create_data_memory_store(), NullDataMemoryStore)

    def test_factory_returns_null_store_for_unknown_provider(self):
        with mock.patch.dict("os.environ", {"DATA_MEMORY_PROVIDER": "other"}, clear=True):
            self.assertIsInstance(create_data_memory_store(), NullDataMemoryStore)

    def test_factory_creates_graphiti_store_when_configured(self):
        with mock.patch.dict(
            "os.environ",
            {
                "DATA_MEMORY_PROVIDER": "graphiti",
                "GRAPHITI_NEO4J_URI": "bolt://localhost:7687",
                "GRAPHITI_NEO4J_USER": "neo4j",
                "GRAPHITI_NEO4J_PASSWORD": "secret",
            },
            clear=True,
        ):
            store = create_data_memory_store()

        self.assertIsInstance(store, GraphitiDataMemoryStore)
        self.assertEqual(store.neo4j_uri, "bolt://localhost:7687")
        self.assertEqual(store.neo4j_user, "neo4j")
        self.assertEqual(store.neo4j_password, "secret")

    def test_factory_requires_complete_graphiti_configuration(self):
        with mock.patch.dict(
            "os.environ",
            {"DATA_MEMORY_PROVIDER": "graphiti", "GRAPHITI_NEO4J_URI": "bolt://localhost:7687"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                create_data_memory_store()


class DataMemoryProposalTests(unittest.TestCase):
    def test_extract_pending_updates_only_for_explicit_memory_intent(self):
        updates = extract_pending_memory_updates(
            question="remember: GMV excludes refunded orders by default",
            contextualized_question="remember: GMV excludes refunded orders by default",
            sql="SELECT 1",
            answer="Saved.",
            error="",
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["scope"], "user")
        self.assertEqual(updates[0]["source"], "explicit_user_instruction")
        self.assertIn("GMV excludes refunded orders", updates[0]["text"])
        self.assertTrue(updates[0]["metadata"]["requires_confirmation"])

    def test_extract_pending_updates_ignores_normal_questions(self):
        self.assertEqual(
            extract_pending_memory_updates(
                question="show gmv by region",
                contextualized_question="show gmv by region",
                sql="SELECT 1",
                answer="",
                error="",
            ),
            [],
        )

    def test_extract_pending_updates_requires_explicit_trigger_boundary(self):
        self.assertEqual(
            extract_pending_memory_updates(
                question="remembered gmv by region yesterday",
                contextualized_question="remembered gmv by region yesterday",
                sql="SELECT 1",
                answer="",
                error="",
            ),
            [],
        )

    def test_extract_pending_updates_accepts_chinese_memory_trigger(self):
        updates = extract_pending_memory_updates(
            question="\u8bb0\u4f4f\uff1aGMV \u9ed8\u8ba4\u6392\u9664\u9000\u6b3e",
            contextualized_question="\u8bb0\u4f4f\uff1aGMV \u9ed8\u8ba4\u6392\u9664\u9000\u6b3e",
            sql="SELECT 1",
            answer="",
            error="",
        )

        self.assertEqual(len(updates), 1)
        self.assertIn("GMV", updates[0]["text"])

    def test_extract_pending_updates_ignores_failed_turns(self):
        self.assertEqual(
            extract_pending_memory_updates(
                question="remember: GMV excludes refunds",
                contextualized_question="remember: GMV excludes refunds",
                sql="",
                answer="",
                error="SQL validation failed",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
