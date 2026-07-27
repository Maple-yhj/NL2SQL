from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from api.auth import AuthPrincipal, AuthSettings, create_access_token
from api.datasource_service import DataSourceService
from api.schemas import Nl2SqlRequest
from data_agent.memory import (
    MemoryCandidate,
    NullMemoryManager,
    UserMemoryContent,
    UserMemoryOwner,
)
from data_agent.runtime import (
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AgentResponse,
    PrincipalContext,
)
from data_agent.runtime.events import RunCompletedPayload
from data_agent.runtime.models import AgentTraceEntry


TEST_JWT_SECRET = "test-secret-key-with-at-least-32-bytes"


def _auth_headers(
    *,
    tenant_id: str = "tenant-from-token",
    user_id: str = "user-from-token",
    roles: list[str] | None = None,
) -> dict[str, str]:
    token = create_access_token(
        AuthPrincipal(
            tenant_id=tenant_id,
            user_id=user_id,
            username="analyst",
            roles=roles or ["analyst"],
            token_version=1,
            token_id="test-token-id",
        ),
        AuthSettings(secret_key=TEST_JWT_SECRET),
    )
    return {"Authorization": f"Bearer {token}"}


class _RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[AgentRequest, PrincipalContext]] = []
        self.recorded_turns: list[
            tuple[AgentRequest, PrincipalContext, AgentResponse]
        ] = []

    async def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> AsyncIterator[AgentEvent]:
        self.calls.append((request, principal))
        response = AgentResponse(
            ok=True,
            question=request.question,
            contextualized_question=request.question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            sql="SELECT 1",
            answer="done",
            trace=(
                AgentTraceEntry(node="finalize", status="completed"),
            )
            if request.include_trace
            else (),
        )
        yield AgentEvent(
            type=AgentEventType.RUN_COMPLETED,
            run_id="run-1",
            sequence=0,
            data=RunCompletedPayload(),
            response=response,
        )

    async def record_conversation_turn(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        response: AgentResponse,
    ) -> None:
        self.recorded_turns.append((request, principal, response))


class _ExplodingRuntime(_RecordingRuntime):
    async def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> AsyncIterator[AgentEvent]:
        del request, principal
        raise RuntimeError(
            "postgresql://admin:secret@database/private "
            "C:\\internal\\runtime.py api_key=do-not-leak"
        )
        yield  # pragma: no cover - keeps this an async generator


class _RecordingComposition:
    def __init__(self) -> None:
        self.runtime = _RecordingRuntime()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class ApiRuntimeContractTests(unittest.TestCase):
    def test_create_app_supports_an_injected_runtime_factory(self):
        self.assertIn("runtime_factory", inspect.signature(create_app).parameters)

    def test_request_schema_is_strict_and_matches_agent_request_fields(self):
        self.assertEqual(
            set(Nl2SqlRequest.model_fields),
            set(AgentRequest.model_fields),
        )
        for forbidden in (
            "tenant_id",
            "user_id",
            "agent_mode",
            "execute",
            "timeout_ms",
            "max_limit",
            "max_validation_attempts",
            "memory_history_limit",
            "include_tool_trace",
        ):
            with self.subTest(field=forbidden), self.assertRaises(ValidationError):
                Nl2SqlRequest.model_validate(
                    {"question": "show gmv", forbidden: "caller-controlled"}
                )

    def test_lifespan_builds_once_routes_only_through_runtime_and_closes_once(self):
        composition = _RecordingComposition()
        factory = mock.AsyncMock(return_value=composition)
        app = create_app(runtime_factory=factory)

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}), TestClient(
            app
        ) as client:
            for include_trace in (False, True):
                response = client.post(
                    "/api/nl2sql",
                    headers=_auth_headers(),
                    json={
                        "question": " show gmv ",
                        "enterprise_id": "olist",
                        "domain_id": "commerce",
                        "conversation_id": "conv-1",
                        "mode": "plan",
                        "requested_output": "answer",
                        "include_trace": include_trace,
                    },
                )
                self.assertEqual(response.status_code, 200)
                expected = AgentResponse(
                    ok=True,
                    question="show gmv",
                    contextualized_question="show gmv",
                    conversation_id="conv-1",
                    tenant_id="tenant-from-token",
                    sql="SELECT 1",
                    answer="done",
                    trace=(
                        AgentTraceEntry(node="finalize", status="completed"),
                    )
                    if include_trace
                    else (),
                ).model_dump(mode="json")
                self.assertEqual(response.json(), expected)

        factory.assert_awaited_once_with()
        self.assertEqual(composition.close_calls, 1)
        self.assertEqual(len(composition.runtime.calls), 2)
        request, principal = composition.runtime.calls[-1]
        self.assertEqual(request.mode.value, "plan")
        self.assertTrue(request.include_trace)
        self.assertEqual(principal.tenant_id, "tenant-from-token")
        self.assertEqual(principal.user_id, "user-from-token")
        self.assertEqual(principal.roles, ("analyst",))

    def test_pinned_datasource_message_routes_to_dataset_query_service(self):
        composition = _RecordingComposition()
        composition.runtime.get_conversation = mock.AsyncMock(  # type: ignore[attr-defined]
            return_value=object()
        )
        data_query = mock.AsyncMock()
        data_query.run.return_value = AgentResponse(
            ok=True,
            question="show rows",
            contextualized_question="show rows",
            conversation_id="conv-dataset",
            tenant_id="tenant-from-token",
            answer="dataset done",
        )
        app = create_app(
            runtime_factory=mock.AsyncMock(return_value=composition),
            data_source_query_service=data_query,
        )

        with mock.patch.dict(
            "os.environ",
            {"JWT_SECRET_KEY": TEST_JWT_SECRET},
        ), TestClient(app) as client:
            response = client.post(
                "/api/conversations/conv-dataset/messages",
                headers=_auth_headers(),
                json={
                    "question": "show rows",
                    "enterprise_id": "user-dataset",
                    "domain_id": "dataset.orders",
                    "source_id": "orders",
                    "source_version": 1,
                    "binding_id": "orders-binding",
                    "binding_version": 1,
                    "mode": "execute",
                    "requested_output": "answer",
                    "include_trace": False,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["answer"], "dataset done")
        self.assertEqual(composition.runtime.calls, [])
        data_query.run.assert_awaited_once()
        routed_request, routed_principal = data_query.run.await_args.args
        self.assertEqual(routed_request.source_id, "orders")
        self.assertEqual(routed_request.conversation_id, "conv-dataset")
        self.assertEqual(routed_principal.tenant_id, "tenant-from-token")
        self.assertEqual(len(composition.runtime.recorded_turns), 1)
        recorded_request, recorded_principal, recorded_response = (
            composition.runtime.recorded_turns[0]
        )
        self.assertEqual(recorded_request, routed_request)
        self.assertEqual(recorded_principal, routed_principal)
        self.assertEqual(recorded_response.answer, "dataset done")

    def test_strict_http_body_cannot_override_authenticated_principal(self):
        composition = _RecordingComposition()
        app = create_app(runtime_factory=mock.AsyncMock(return_value=composition))

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}), TestClient(
            app
        ) as client:
            response = client.post(
                "/api/nl2sql",
                headers=_auth_headers(),
                json={"question": "show gmv", "tenant_id": "attacker"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(composition.runtime.calls, [])

    def test_streaming_response_is_persisted_for_scoped_replay(self):
        composition = _RecordingComposition()
        with tempfile.TemporaryDirectory() as state_root:
            app = create_app(
                runtime_factory=mock.AsyncMock(return_value=composition),
                data_source_service=DataSourceService(state_root=state_root),
            )

            with mock.patch.dict(
                "os.environ",
                {"JWT_SECRET_KEY": TEST_JWT_SECRET},
            ), TestClient(app) as client:
                response = client.post(
                    "/api/nl2sql/stream",
                    headers=_auth_headers(),
                    json={"question": "show gmv"},
                )
                replay = client.get(
                    "/api/runs/run-1/events",
                    headers=_auth_headers(),
                )
                exhausted_replay = client.get(
                    "/api/runs/run-1/events?after_sequence=0",
                    headers=_auth_headers(),
                )
                other_user = client.get(
                    "/api/runs/run-1/events",
                    headers=_auth_headers(user_id="other-user"),
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        self.assertIn("event: run_completed", response.text)
        self.assertIn('"run_id":"run-1"', response.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(len(replay.json()["items"]), 1)
        self.assertEqual(replay.json()["items"][0]["run_id"], "run-1")
        self.assertEqual(exhausted_replay.status_code, 200)
        self.assertEqual(exhausted_replay.json()["items"], [])
        self.assertEqual(other_user.status_code, 404)

    def test_memory_proposals_are_listed_and_decided_with_owner_authority(
        self,
    ):
        memory = NullMemoryManager()
        own_proposal = asyncio.run(
            memory.propose(
                MemoryCandidate(
                    owner=UserMemoryOwner(
                        tenant_id="tenant-from-token",
                        user_id="user-from-token",
                    ),
                    content=UserMemoryContent(
                        preference_key="report_style",
                        preference_value="concise",
                    ),
                    source="explicit_user_instruction",
                )
            )
        )
        asyncio.run(
            memory.propose(
                MemoryCandidate(
                    owner=UserMemoryOwner(
                        tenant_id="tenant-from-token",
                        user_id="other-user",
                    ),
                    content=UserMemoryContent(
                        preference_key="report_style",
                        preference_value="detailed",
                    ),
                    source="explicit_user_instruction",
                )
            )
        )
        composition = _RecordingComposition()
        composition.dependencies = SimpleNamespace(memory=memory)
        app = create_app(runtime_factory=mock.AsyncMock(return_value=composition))

        with mock.patch.dict(
            "os.environ",
            {"JWT_SECRET_KEY": TEST_JWT_SECRET},
        ), TestClient(app) as client:
            listed = client.get(
                "/api/memory/proposals",
                headers=_auth_headers(),
            )
            unauthorized = client.post(
                f"/api/memory/proposals/{own_proposal}/decision",
                headers=_auth_headers(user_id="other-user"),
                json={"decision": "approve"},
            )
            decided = client.post(
                f"/api/memory/proposals/{own_proposal}/decision",
                headers=_auth_headers(),
                json={"decision": "approve", "reason": "confirmed"},
            )
            committed = client.get(
                "/api/memory/proposals?status=committed",
                headers=_auth_headers(),
            )

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [item["proposal_id"] for item in listed.json()["items"]],
            [own_proposal],
        )
        self.assertEqual(unauthorized.status_code, 404)
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["status"], "committed")
        self.assertEqual(
            [item["proposal_id"] for item in committed.json()["items"]],
            [own_proposal],
        )

    def test_unhandled_runtime_exception_returns_only_typed_safe_error(self):
        composition = _RecordingComposition()
        composition.runtime = _ExplodingRuntime()
        app = create_app(runtime_factory=mock.AsyncMock(return_value=composition))

        with mock.patch.dict("os.environ", {"JWT_SECRET_KEY": TEST_JWT_SECRET}), TestClient(
            app,
            raise_server_exceptions=False,
        ) as client:
            response = client.post(
                "/api/nl2sql",
                headers=_auth_headers(),
                json={"question": "show gmv"},
            )

        self.assertEqual(response.status_code, 500)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "INTERNAL_ERROR")
        self.assertEqual(payload["error"]["message"], "The governed run failed safely.")
        self.assertEqual(payload, AgentResponse.model_validate(payload).model_dump(mode="json"))
        serialized = str(payload)
        for secret in ("postgresql://", "admin", "secret", "runtime.py", "api_key"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(composition.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
