from __future__ import annotations

import inspect
import unittest
from collections.abc import AsyncIterator
from unittest import mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from api.auth import AuthPrincipal, AuthSettings, create_access_token
from api.schemas import Nl2SqlRequest
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
) -> dict[str, str]:
    token = create_access_token(
        AuthPrincipal(
            tenant_id=tenant_id,
            user_id=user_id,
            username="analyst",
            roles=["analyst"],
            token_version=1,
            token_id="test-token-id",
        ),
        AuthSettings(secret_key=TEST_JWT_SECRET),
    )
    return {"Authorization": f"Bearer {token}"}


class _RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[AgentRequest, PrincipalContext]] = []

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
