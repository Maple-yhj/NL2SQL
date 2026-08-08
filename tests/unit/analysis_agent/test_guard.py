from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from data_agent.analysis_agent.guard import (
    AgentGuardError,
    consume_budget,
    ensure_node_entry,
    guard_planner_decision,
)
from data_agent.analysis_agent.models import AgentBudgetState, AgentRunBudget, AgentStatus
from data_agent.public_contracts import ErrorCode
from data_agent.runtime.models import AgentMode
from data_agent.tools.providers.dataset import build_dataset_tool_registry

from ._graph_support import act_decision, analysis_plan


class AgentGuardTests(unittest.TestCase):
    def test_budget_consumption_is_validated_before_any_counter_changes(self) -> None:
        now = datetime.now(UTC)
        budget = AgentBudgetState(
            model_calls=1,
            started_at=now,
            deadline_at=now + timedelta(seconds=10),
        )
        limits = AgentRunBudget(max_model_calls=1)
        with self.assertRaises(AgentGuardError) as captured:
            consume_budget(budget, limits, "tool_calls", "model_calls")
        self.assertEqual(captured.exception.code, ErrorCode.AGENT_BUDGET_EXCEEDED)
        self.assertEqual(budget.tool_calls, 0)

    def test_deadline_and_cancellation_are_checked_at_node_entry(self) -> None:
        now = datetime.now(UTC)
        budget = AgentBudgetState(
            started_at=now - timedelta(seconds=2),
            deadline_at=now - timedelta(seconds=1),
        )
        with self.assertRaises(AgentGuardError) as deadline:
            ensure_node_entry(
                status=AgentStatus.RUNNING,
                budget=budget,
                now=now,
                cancelled=lambda: False,
            )
        self.assertEqual(deadline.exception.code, ErrorCode.DEADLINE_EXCEEDED)
        with self.assertRaises(AgentGuardError) as cancelled:
            ensure_node_entry(
                status=AgentStatus.RUNNING,
                budget=budget.model_copy(
                    update={"deadline_at": now + timedelta(seconds=10)}
                ),
                now=now,
                cancelled=lambda: True,
            )
        self.assertEqual(cancelled.exception.code, ErrorCode.CANCELLED)

    def test_guard_rejects_mode_bypass_before_tool_node(self) -> None:
        registry = build_dataset_tool_registry()
        decision = act_decision(
            action_id="execute",
            plan_value=analysis_plan("pending"),
            tool_name="query.execute",
        )
        with self.assertRaises(AgentGuardError) as captured:
            guard_planner_decision(
                decision,
                mode=AgentMode.PREVIEW,
                allowed_tool_names=registry.names(),
                specs={spec.name: spec for spec in registry.specs()},
            )
        self.assertEqual(captured.exception.code, ErrorCode.AGENT_ACTION_NOT_ALLOWED)


if __name__ == "__main__":
    unittest.main()
