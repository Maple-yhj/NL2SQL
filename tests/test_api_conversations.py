import asyncio
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from api.app import create_app
from graph.memory_store import InMemoryConversationStore


def graph_output(**overrides):
    output = {
        "ok": True,
        "question": "那按地区呢",
        "contextualized_question": "按地区统计上月GMV",
        "conversation_id": "conv-1",
        "user_id": "user-1",
        "tenant_id": "demo",
        "intent": {"metrics": ["gmv"], "dimensions": ["region"]},
        "sql": "SELECT region, sum(amount) AS gmv FROM orders GROUP BY region LIMIT 1000",
        "rows": [],
        "answer": "",
        "error": "",
        "trace": [{"node": "initialize", "ok": True, "message": "success"}],
    }
    output.update(overrides)
    return output


class ApiConversationTests(unittest.TestCase):
    def test_create_conversation_returns_session(self):
        client = TestClient(create_app())
        store = InMemoryConversationStore()

        with mock.patch("api.routes.create_conversation_store", return_value=store):
            response = client.post(
                "/api/conversations",
                json={
                    "tenant_id": "demo",
                    "user_id": "user-1",
                    "title": "GMV analysis",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["tenant_id"], "demo")
        self.assertEqual(payload["user_id"], "user-1")
        self.assertEqual(payload["title"], "GMV analysis")
        self.assertFalse(payload["archived"])
        self.assertTrue(payload["conversation_id"])

    def test_list_conversations_filters_by_user(self):
        client = TestClient(create_app())
        store = InMemoryConversationStore()

        with mock.patch("api.routes.create_conversation_store", return_value=store):
            client.post(
                "/api/conversations",
                json={"tenant_id": "demo", "user_id": "user-1", "title": "User 1"},
            )
            client.post(
                "/api/conversations",
                json={"tenant_id": "demo", "user_id": "user-2", "title": "User 2"},
            )
            response = client.get(
                "/api/conversations",
                params={"tenant_id": "demo", "user_id": "user-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["title"] for item in response.json()["items"]], ["User 1"])

    def test_get_conversation_requires_matching_user(self):
        client = TestClient(create_app())
        store = InMemoryConversationStore()

        with mock.patch("api.routes.create_conversation_store", return_value=store):
            created = client.post(
                "/api/conversations",
                json={"tenant_id": "demo", "user_id": "user-1", "title": "User 1"},
            ).json()
            response = client.get(
                f"/api/conversations/{created['conversation_id']}",
                params={"tenant_id": "demo", "user_id": "user-2"},
            )

        self.assertEqual(response.status_code, 404)

    def test_patch_conversation_updates_title_and_archived_status(self):
        client = TestClient(create_app())
        store = InMemoryConversationStore()

        with mock.patch("api.routes.create_conversation_store", return_value=store):
            created = client.post(
                "/api/conversations",
                json={"tenant_id": "demo", "user_id": "user-1", "title": "Old"},
            ).json()
            response = client.patch(
                f"/api/conversations/{created['conversation_id']}",
                json={
                    "tenant_id": "demo",
                    "user_id": "user-1",
                    "title": "New",
                    "archived": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "New")
        self.assertTrue(response.json()["archived"])

    def test_get_messages_returns_conversation_history(self):
        client = TestClient(create_app())
        store = InMemoryConversationStore()

        with mock.patch("api.routes.create_conversation_store", return_value=store):
            created = client.post(
                "/api/conversations",
                json={"tenant_id": "demo", "user_id": "user-1", "title": "GMV"},
            ).json()
            asyncio.run(
                store.save_turn(
                    tenant_id="demo",
                    conversation_id=created["conversation_id"],
                    user_id="user-1",
                    question="show gmv",
                    contextualized_question="show gmv",
                    sql="SELECT 1",
                    answer="GMV is 100.",
                    ok=True,
                    error="",
                    trace=[],
                )
            )
            response = client.get(
                f"/api/conversations/{created['conversation_id']}/messages",
                params={"tenant_id": "demo", "user_id": "user-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["role"] for item in response.json()["items"]], ["user", "assistant"])

    def test_post_message_calls_graph_with_conversation_identity(self):
        client = TestClient(create_app())
        store = InMemoryConversationStore()

        with mock.patch("api.routes.create_conversation_store", return_value=store), mock.patch(
            "api.routes.run_nl2sql",
            new=mock.AsyncMock(return_value=graph_output()),
        ) as run_nl2sql:
            created = client.post(
                "/api/conversations",
                json={"tenant_id": "demo", "user_id": "user-1", "title": "GMV"},
            ).json()
            response = client.post(
                f"/api/conversations/{created['conversation_id']}/messages",
                json={
                    "tenant_id": "demo",
                    "user_id": "user-1",
                    "question": " 那按地区呢 ",
                    "execute": False,
                    "timeout_ms": 5000,
                    "max_limit": 200,
                    "max_validation_attempts": 3,
                    "memory_history_limit": 6,
                },
            )

        self.assertEqual(response.status_code, 200)
        run_nl2sql.assert_awaited_once_with(
            "那按地区呢",
            tenant_id="demo",
            execute=False,
            conversation_id=created["conversation_id"],
            user_id="user-1",
            timeout_ms=5000,
            max_limit=200,
            max_validation_attempts=3,
            memory_history_limit=6,
            memory_store=store,
        )

    def test_post_message_rejects_unknown_conversation(self):
        client = TestClient(create_app())
        store = InMemoryConversationStore()

        with mock.patch("api.routes.create_conversation_store", return_value=store):
            response = client.post(
                "/api/conversations/missing/messages",
                json={"tenant_id": "demo", "user_id": "user-1", "question": "show gmv"},
            )

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
