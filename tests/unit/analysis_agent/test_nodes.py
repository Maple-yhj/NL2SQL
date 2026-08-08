from __future__ import annotations

import unittest

from data_agent.analysis_agent.models import AgentAction
from data_agent.analysis_agent.nodes import DatasetAgentToolInvoker, stable_call_id
from data_agent.runtime.models import AgentMode
from data_agent.tools.providers.dataset.contracts import EmptyInput

from tests.unit.tools.dataset._support import DatasetToolHarness

from ._decision_support import context


class AgentNodeTests(unittest.IsolatedAsyncioTestCase):
    def test_stable_call_id_is_replay_deterministic_and_argument_sensitive(self) -> None:
        first = stable_call_id("run-1", "action-1", {"value": 1})
        replay = stable_call_id("run-1", "action-1", {"value": 1})
        changed = stable_call_id("run-1", "action-1", {"value": 2})
        self.assertEqual(first, replay)
        self.assertNotEqual(first, changed)
        self.assertRegex(first, r"^call-[0-9a-f]{64}$")

    async def test_dataset_tool_adapter_always_uses_unified_invoker(self) -> None:
        harness = DatasetToolHarness()
        try:
            runtime, invoker, invocation_context = harness.invocation(AgentMode.PLAN)
            adapter = DatasetAgentToolInvoker(
                registry=harness.registry,
                invoker=invoker,
                principal=harness.principal,
                runtime_resources=runtime,
            )
            action = AgentAction(
                action_id="catalog-action",
                tool_name="catalog.inspect",
                arguments=EmptyInput().model_dump(mode="json"),
                purpose="Inspect catalog",
                expected_evidence=("catalog",),
            )
            call_id = stable_call_id("run-1", action.action_id, action.arguments)
            state = {
                "run_id": "run-1",
                "authority": runtime.authority,
                "context": context(allowed_tool_names=harness.registry.names()),
            }
            observation = await adapter.invoke(
                call_id=call_id,
                action=action,
                state=state,
            )
            replay = await adapter.invoke(
                call_id=call_id,
                action=action,
                state=state,
            )
            self.assertEqual(observation.status, "succeeded")
            self.assertEqual(observation.artifact_refs, replay.artifact_refs)
            self.assertEqual(invocation_context.authority, runtime.authority)
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
