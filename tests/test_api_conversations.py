from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest import mock

from fastapi.testclient import TestClient

from api.app import create_app
from api.auth import AuthPrincipal, AuthSettings, create_access_token
from data_agent.runtime import (
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AgentResponse,
    ConversationMessage,
    ConversationMessageMetadata,
    ConversationSummary,
    PrincipalContext,
)
from data_agent.runtime.events import RunCompletedPayload, RunStartedPayload


TEST_JWT_SECRET = "test-secret-key-with-at-least-32-bytes"
NOW = datetime(2026, 7, 12, tzinfo=UTC)


def auth_headers(
    tenant_id: str = "demo",
    user_id: str = "user-1",
) -> dict[str, str]:
    token = create_access_token(
        AuthPrincipal(
            tenant_id=tenant_id,
            user_id=user_id,
            username="analyst",
            roles=["analyst"],
            token_version=1,
            token_id=f"token-{tenant_id}-{user_id}",
        ),
        AuthSettings(secret_key=TEST_JWT_SECRET),
    )
    return {"Authorization": f"Bearer {token}"}


class _ConversationRuntime:
    def __init__(self) -> None:
        self.items: dict[str, ConversationSummary] = {}
        self.run_calls: list[tuple[AgentRequest, PrincipalContext]] = []

    async def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> AsyncIterator[AgentEvent]:
        self.run_calls.append((request, principal))
        response = AgentResponse(
            ok=True,
            question=request.question,
            contextualized_question=request.question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            sql="SELECT 1",
            answer="done",
        )
        yield AgentEvent(
            type=AgentEventType.RUN_STARTED,
            run_id="run-1",
            sequence=0,
            data=RunStartedPayload(
                mode=request.mode,
                enterprise_id=request.enterprise_id,
                domain_id=request.domain_id,
            ),
        )
        yield AgentEvent(
            type=AgentEventType.RUN_COMPLETED,
            run_id="run-1",
            sequence=1,
            data=RunCompletedPayload(),
            response=response,
        )

    async def create_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        title: str = "",
    ) -> ConversationSummary:
        conversation_id = f"conv-{len(self.items) + 1}"
        item = ConversationSummary(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            domain_id=domain_id,
            conversation_id=conversation_id,
            title=title,
            created_at=NOW,
            updated_at=NOW,
        )
        self.items[conversation_id] = item
        return item

    async def list_conversations(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        limit: int,
        include_archived: bool = False,
    ) -> tuple[ConversationSummary, ...]:
        return tuple(
            item
            for item in self.items.values()
            if item.tenant_id == principal.tenant_id
            and item.user_id == principal.user_id
            and item.domain_id == domain_id
            and (include_archived or not item.archived)
        )[:limit]

    async def get_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
    ) -> ConversationSummary | None:
        item = self.items.get(conversation_id)
        if (
            item is None
            or item.tenant_id != principal.tenant_id
            or item.user_id != principal.user_id
            or item.domain_id != domain_id
        ):
            return None
        return item

    async def update_conversation(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationSummary | None:
        item = await self.get_conversation(
            principal=principal,
            domain_id=domain_id,
            conversation_id=conversation_id,
        )
        if item is None:
            return None
        updated = item.model_copy(
            update={
                "title": item.title if title is None else title,
                "archived": item.archived if archived is None else archived,
            }
        )
        self.items[conversation_id] = updated
        return updated

    async def list_conversation_messages(
        self,
        *,
        principal: PrincipalContext,
        domain_id: str,
        conversation_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        item = await self.get_conversation(
            principal=principal,
            domain_id=domain_id,
            conversation_id=conversation_id,
        )
        if item is None:
            return ()
        return (
            ConversationMessage(role="user", content="show gmv"),
            ConversationMessage(
                role="assistant",
                content="done",
                metadata=ConversationMessageMetadata(
                    answer="done",
                    ok=True,
                ),
            ),
        )[-limit:]


class _Composition:
    def __init__(self, runtime: _ConversationRuntime) -> None:
        self.runtime = runtime

    async def close(self) -> None:
        return None


class ApiConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _ConversationRuntime()
        self.env = mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET})
        self.env.start()
        app = create_app(
            runtime_factory=mock.AsyncMock(return_value=_Composition(self.runtime))
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.env.stop()

    def _create(self, *, user_id: str = "user-1") -> dict:
        response = self.client.post(
            "/api/conversations",
            headers=auth_headers(user_id=user_id),
            json={"title": "Dataset analysis", "domain_id": "dataset"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_conversation_routes_require_token(self):
        cases = (
            ("post", "/api/conversations", {"json": {"title": "GMV"}}),
            ("get", "/api/conversations", {}),
            ("get", "/api/conversations/missing", {}),
            ("patch", "/api/conversations/missing", {"json": {"title": "New"}}),
            ("get", "/api/conversations/missing/messages", {}),
            (
                "post",
                "/api/conversations/missing/messages",
                {"json": {"question": "show gmv"}},
            ),
        )
        for method, url, kwargs in cases:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url, **kwargs).status_code, 401)

    def test_crud_uses_authenticated_principal_and_runtime_facade(self):
        created = self._create()
        self.assertEqual(created["tenant_id"], "demo")
        self.assertEqual(created["user_id"], "user-1")
        self.assertEqual(created["domain_id"], "dataset")

        listed = self.client.get(
            "/api/conversations",
            headers=auth_headers(),
            params={"domain_id": "dataset", "limit": 20},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"], [created])
        fetched = self.client.get(
            f"/api/conversations/{created['conversation_id']}",
            headers=auth_headers(),
            params={"domain_id": "dataset"},
        )
        self.assertEqual(fetched.json(), created)

        updated = self.client.patch(
            f"/api/conversations/{created['conversation_id']}",
            headers=auth_headers(),
            params={"domain_id": "dataset"},
            json={"title": "Final GMV", "archived": True},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["title"], "Final GMV")
        self.assertTrue(updated.json()["archived"])

    def test_identity_fields_are_forbidden_in_conversation_bodies(self):
        for field in ("tenant_id", "user_id"):
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/conversations",
                    headers=auth_headers(),
                    json={"title": "GMV", field: "attacker"},
                )
                self.assertEqual(response.status_code, 422)

    def test_messages_and_agent_turn_use_only_runtime(self):
        created = self._create()
        conversation_id = created["conversation_id"]
        history = self.client.get(
            f"/api/conversations/{conversation_id}/messages",
            headers=auth_headers(),
            params={"domain_id": "dataset", "limit": 50},
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual([item["role"] for item in history.json()["items"]], ["user", "assistant"])

        response = self.client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers=auth_headers(),
            json={
                "question": " show gmv ",
                "domain_id": "dataset",
                "enterprise_id": "user-dataset",
                "mode": "preview",
                "requested_output": "answer",
                "include_trace": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        request, principal = self.runtime.run_calls[-1]
        self.assertEqual(request.conversation_id, conversation_id)
        self.assertEqual(request.mode.value, "preview")
        self.assertEqual(principal.tenant_id, "demo")
        self.assertEqual(principal.user_id, "user-1")

    def test_unknown_or_cross_user_conversation_is_not_found(self):
        created = self._create(user_id="user-1")
        response = self.client.get(
            f"/api/conversations/{created['conversation_id']}",
            headers=auth_headers(user_id="user-2"),
            params={"domain_id": "dataset"},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
