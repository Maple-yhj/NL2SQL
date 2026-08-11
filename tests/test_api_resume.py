from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from api.app import create_app
from api.datasource_service import DataSourceService
from data_agent.analysis_agent.checkpoints import InMemoryCheckpointerFactory
from data_agent.analysis_agent.composition import build_analysis_runtime_from_resolver
from data_agent.memory import NullMemoryManager
from tests.integration.test_analysis_agent_runtime import (
    TestAnalysisResolver,
    clarification_decision,
)
from data_agent.analysis_agent.models import AgentInputRequest
from tests.test_api_runtime_contract import (
    TEST_JWT_SECRET,
    _RecordingRuntime,
    _auth_headers,
)
from tests.unit.analysis_agent._graph_support import analysis_plan, finish_decision


def _request_body(*, conversation_id: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "question": "Analyze the selected orders",
        "source_id": "orders-source",
        "source_version": 2,
        "binding_id": "orders-binding",
        "binding_version": 3,
        "mode": "plan",
    }
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return body


def _sse_events(text: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


class _ApiComposition:
    def __init__(self, analysis) -> None:
        self.runtime = _RecordingRuntime()
        self.analysis_runtime = analysis.runtime
        self.dependencies = SimpleNamespace(memory=NullMemoryManager())
        self._analysis = analysis

    async def close(self) -> None:
        await self._analysis.close()


class ApiResumeTests(unittest.TestCase):
    def _open(self, decisions):
        temporary = tempfile.TemporaryDirectory()
        service = DataSourceService(state_root=temporary.name)
        holder: dict[str, _ApiComposition] = {}

        async def factory():
            analysis = await build_analysis_runtime_from_resolver(
                resolver=TestAnalysisResolver(decisions),
                checkpointer_factory=InMemoryCheckpointerFactory(),
            )
            composition = _ApiComposition(analysis)
            holder["composition"] = composition
            return composition

        app = create_app(
            runtime_factory=factory,
            data_source_service=service,
        )
        environment = mock.patch.dict(
            "os.environ",
            {"JWT_SECRET_KEY": TEST_JWT_SECRET},
        )
        environment.start()
        client_context = TestClient(app)
        client = client_context.__enter__()
        return temporary, environment, client_context, client, holder

    def _close(self, resources) -> None:
        temporary, environment, client_context, _, _ = resources
        client_context.__exit__(None, None, None)
        environment.stop()
        temporary.cleanup()

    def test_normal_waiting_resume_and_monotonic_replay(self) -> None:
        resources = self._open(
            [clarification_decision(), finish_decision(analysis_plan("pending"))]
        )
        client = resources[3]
        try:
            waiting = client.post(
                "/api/nl2sql",
                headers=_auth_headers(),
                json=_request_body(),
            )
            self.assertEqual(waiting.status_code, 202, waiting.text)
            payload = waiting.json()
            self.assertEqual(payload["status"], "waiting")
            run_id = payload["run_id"]
            interrupt_id = payload["event"]["data"]["input_request"]["interrupt_id"]

            stale = client.post(
                f"/api/runs/{run_id}/resume",
                headers=_auth_headers(),
                json={"interrupt_id": "interrupt-stale", "message": "Last year"},
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(stale.json()["error"]["code"], "AGENT_INTERRUPT_STALE")

            other_user = client.post(
                f"/api/runs/{run_id}/resume",
                headers=_auth_headers(user_id="other-user"),
                json={"interrupt_id": interrupt_id, "message": "Last year"},
            )
            self.assertEqual(other_user.status_code, 404)

            resumed = client.post(
                f"/api/runs/{run_id}/resume",
                headers=_auth_headers(),
                json={"interrupt_id": interrupt_id, "message": "Use the last year"},
            )
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertTrue(resumed.json()["ok"])

            replay = client.get(
                f"/api/runs/{run_id}/events",
                headers=_auth_headers(),
            )
            events = replay.json()["items"]
            self.assertEqual(
                [item["sequence"] for item in events],
                list(range(len(events))),
            )
            self.assertIn("run_waiting", [item["type"] for item in events])
            self.assertIn("run_resumed", [item["type"] for item in events])
            self.assertEqual(events[-1]["type"], "run_completed")

            duplicate = client.post(
                f"/api/runs/{run_id}/resume",
                headers=_auth_headers(),
                json={"interrupt_id": interrupt_id, "message": "Again"},
            )
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            self.assertEqual(duplicate.json()["error"]["code"], "AGENT_RESUME_CONFLICT")
        finally:
            self._close(resources)

    def test_streaming_resume_appends_to_the_same_event_history(self) -> None:
        resources = self._open(
            [clarification_decision(), finish_decision(analysis_plan("pending"))]
        )
        client = resources[3]
        try:
            waiting_response = client.post(
                "/api/nl2sql/stream",
                headers=_auth_headers(),
                json=_request_body(),
            )
            waiting_events = _sse_events(waiting_response.text)
            self.assertEqual(waiting_events[-1]["type"], "run_waiting")
            run_id = waiting_events[-1]["run_id"]
            interrupt_id = waiting_events[-1]["data"]["input_request"]["interrupt_id"]

            resumed_response = client.post(
                f"/api/runs/{run_id}/resume/stream",
                headers=_auth_headers(),
                json={"interrupt_id": interrupt_id, "message": "Use 2025"},
            )
            self.assertEqual(resumed_response.status_code, 200, resumed_response.text)
            resumed_events = _sse_events(resumed_response.text)
            self.assertEqual(resumed_events[0]["type"], "run_resumed")
            self.assertEqual(resumed_events[-1]["type"], "run_completed")
            self.assertEqual(
                resumed_events[0]["sequence"],
                waiting_events[-1]["sequence"] + 1,
            )
        finally:
            self._close(resources)

    def test_selected_clarification_choice_is_accepted_and_validated(self) -> None:
        clarification = clarification_decision()
        clarification = clarification.model_copy(
            update={
                "clarification": AgentInputRequest(
                    interrupt_id="interrupt-time-range",
                    reason="clarification",
                    prompt="Which time range should be used?",
                    choices=("Last 30 days", "This quarter"),
                )
            }
        )
        resources = self._open(
            [clarification, finish_decision(analysis_plan("pending"))]
        )
        client = resources[3]
        try:
            waiting = client.post(
                "/api/nl2sql",
                headers=_auth_headers(),
                json=_request_body(),
            ).json()
            run_id = waiting["run_id"]
            interrupt_id = waiting["event"]["data"]["input_request"]["interrupt_id"]

            invalid = client.post(
                f"/api/runs/{run_id}/resume",
                headers=_auth_headers(),
                json={
                    "interrupt_id": interrupt_id,
                    "message": "Unknown range",
                    "selected_choice": "Unknown range",
                },
            )
            self.assertEqual(invalid.status_code, 409, invalid.text)
            self.assertEqual(invalid.json()["error"]["code"], "AGENT_INTERRUPT_STALE")

            resumed = client.post(
                f"/api/runs/{run_id}/resume",
                headers=_auth_headers(),
                json={
                    "interrupt_id": interrupt_id,
                    "message": "Last 30 days",
                    "selected_choice": "Last 30 days",
                },
            )
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertTrue(resumed.json()["ok"])
        finally:
            self._close(resources)

    def test_waiting_cancel_is_persisted_and_blocks_resume(self) -> None:
        resources = self._open([clarification_decision()])
        client = resources[3]
        try:
            waiting = client.post(
                "/api/nl2sql",
                headers=_auth_headers(),
                json=_request_body(),
            ).json()
            run_id = waiting["run_id"]
            interrupt_id = waiting["event"]["data"]["input_request"]["interrupt_id"]
            cancelled = client.post(
                f"/api/runs/{run_id}/cancel",
                headers=_auth_headers(),
            )
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            replay = client.get(
                f"/api/runs/{run_id}/events",
                headers=_auth_headers(),
            ).json()["items"]
            self.assertEqual(replay[-1]["type"], "run_failed")
            self.assertEqual(replay[-1]["data"]["error_code"], "CANCELLED")
            rejected = client.post(
                f"/api/runs/{run_id}/resume",
                headers=_auth_headers(),
                json={"interrupt_id": interrupt_id, "message": "Continue"},
            )
            self.assertEqual(rejected.status_code, 409, rejected.text)
        finally:
            self._close(resources)

    def test_waiting_run_blocks_a_second_run_in_the_same_conversation(self) -> None:
        resources = self._open([clarification_decision()])
        client = resources[3]
        try:
            first = client.post(
                "/api/nl2sql",
                headers=_auth_headers(),
                json=_request_body(conversation_id="conversation-one-active-run"),
            )
            self.assertEqual(first.status_code, 202, first.text)
            second = client.post(
                "/api/nl2sql",
                headers=_auth_headers(),
                json=_request_body(conversation_id="conversation-one-active-run"),
            )
            self.assertEqual(second.status_code, 409, second.text)
            self.assertEqual(second.json()["error"]["code"], "AGENT_RESUME_CONFLICT")
        finally:
            self._close(resources)


if __name__ == "__main__":
    unittest.main()
