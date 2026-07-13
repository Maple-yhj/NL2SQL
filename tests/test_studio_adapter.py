from __future__ import annotations

import json
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from unittest import mock

from data_agent.runtime import (
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AgentResponse,
    PrincipalContext,
)
from data_agent.runtime.events import RunCompletedPayload


ROOT = Path(__file__).resolve().parents[1]


class _Runtime:
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
        )
        yield AgentEvent(
            type=AgentEventType.RUN_COMPLETED,
            run_id="studio-run",
            sequence=0,
            data=RunCompletedPayload(),
            response=response,
        )


class _Composition:
    def __init__(self) -> None:
        self.runtime = _Runtime()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class StudioRuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_output_is_exactly_the_direct_runtime_response(self):
        from data_agent.adapters.studio import build_studio_graph

        composition = _Composition()
        factory = mock.AsyncMock(return_value=composition)
        studio_graph = build_studio_graph(runtime_factory=factory)

        output = await studio_graph.ainvoke(
            {
                "request": {
                    "question": " show gmv ",
                    "mode": "preview",
                    "conversation_id": "conv-1",
                },
                "principal": {
                    "tenant_id": "tenant-1",
                    "user_id": "user-1",
                    "roles": ["analyst"],
                },
            }
        )

        expected = AgentResponse(
            ok=True,
            question="show gmv",
            contextualized_question="show gmv",
            conversation_id="conv-1",
            tenant_id="tenant-1",
            sql="SELECT 1",
            answer="done",
        ).model_dump(mode="json")
        self.assertEqual(output, expected)
        factory.assert_awaited_once_with()
        self.assertEqual(composition.close_calls, 1)
        request, principal = composition.runtime.calls[-1]
        self.assertEqual(request.mode.value, "preview")
        self.assertEqual(principal.roles, ("analyst",))

    def test_studio_config_points_only_to_runtime_wrapper(self):
        config = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8"))
        graph_targets = tuple(config["graphs"].values())

        self.assertEqual(
            graph_targets,
            ("./src/data_agent/adapters/studio.py:graph",),
        )
        serialized = json.dumps(config)
        for forbidden in ("graph/pipeline", "graph.pipeline", "execution"):
            self.assertNotIn(forbidden, serialized)

    def test_wrapper_source_does_not_import_internal_execution_or_legacy_graph(self):
        source = (ROOT / "src" / "data_agent" / "adapters" / "studio.py").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "data_agent.execution",
            "graph.pipeline",
            "run_nl2sql",
            "graph.tools",
            "graph.memory_store",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
