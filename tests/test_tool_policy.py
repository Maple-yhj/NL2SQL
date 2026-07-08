import unittest
from types import SimpleNamespace

from graph.tools.contracts import ToolSpec
from graph.tools.policy import evaluate_pre_call_policy


class ToolPolicyTests(unittest.TestCase):
    def test_high_risk_tool_is_blocked_without_explicit_runtime_allowance(self):
        spec = ToolSpec(
            name="execute_sql",
            description="execute",
            risk_level="high",
            side_effects="read",
        )

        decision = evaluate_pre_call_policy(
            spec=spec,
            state={"tenant_id": "admin"},
            runtime=SimpleNamespace(context=SimpleNamespace()),
            inputs={},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.violations[0].code, "tool_risk_not_allowed")
        self.assertFalse(decision.to_tool_result().ok)

    def test_high_risk_tool_is_allowed_when_runtime_allows_risk_level(self):
        spec = ToolSpec(
            name="execute_sql",
            description="execute",
            risk_level="high",
            side_effects="read",
        )

        decision = evaluate_pre_call_policy(
            spec=spec,
            state={"tenant_id": "admin"},
            runtime=SimpleNamespace(
                context=SimpleNamespace(allowed_tool_risk_levels=("low", "medium", "high"))
            ),
            inputs={},
        )

        self.assertTrue(decision.allowed)

    def test_required_tenant_identity_is_enforced(self):
        spec = ToolSpec(
            name="sql.validate",
            description="validate",
            input_keys=("sql", "tenant_id"),
            risk_level="medium",
        )

        decision = evaluate_pre_call_policy(
            spec=spec,
            state={},
            runtime=SimpleNamespace(context=SimpleNamespace(allowed_tool_risk_levels=("low", "medium"))),
            inputs={"sql": "SELECT 1"},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.violations[0].code, "missing_tenant_id")

    def test_tool_call_budget_is_enforced(self):
        spec = ToolSpec(name="search_schema", description="search")

        decision = evaluate_pre_call_policy(
            spec=spec,
            state={"tenant_id": "admin", "_tool_call_count": 2},
            runtime=SimpleNamespace(
                context=SimpleNamespace(allowed_tool_risk_levels=("low",), max_tool_calls=2)
            ),
            inputs={},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.violations[0].code, "tool_call_budget_exceeded")

    def test_read_only_mode_blocks_write_tools(self):
        spec = ToolSpec(
            name="memory.write",
            description="write",
            side_effects="write",
        )

        decision = evaluate_pre_call_policy(
            spec=spec,
            state={"tenant_id": "admin"},
            runtime=SimpleNamespace(
                context=SimpleNamespace(allowed_tool_risk_levels=("low",), read_only_tools=True)
            ),
            inputs={},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.violations[0].code, "tool_write_blocked")


if __name__ == "__main__":
    unittest.main()
