import unittest

from agent.react_state import ReactRuntimeConfig, ReactState
from agent.tool_policy import ToolPolicy
from engine.models import QueryIntent


class AgentToolPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ToolPolicy()

    @staticmethod
    def _state(*, execute_enabled: bool = False) -> ReactState:
        return ReactState(
            question="show gmv",
            config=ReactRuntimeConfig(
                tenant_id="demo",
                execute_enabled=execute_enabled,
            ),
            intent=QueryIntent(metrics=["gmv"]),
        )

    @staticmethod
    def _tool_names(policy: ToolPolicy, state: ReactState) -> list[str]:
        return [spec.name for spec in policy.available_tools(state)]

    @staticmethod
    def _apply_metrics(state: ReactState) -> None:
        state.apply_observation(
            "search_metrics",
            {"query": "gmv"},
            {
                "ok": True,
                "metrics": [
                    {
                        "metric_name": "gmv",
                        "base_table": "orders o",
                    }
                ],
                "message": "success",
            },
        )

    @staticmethod
    def _apply_schema(state: ReactState, *, with_table: bool = True) -> None:
        schema = [{"table_name": "orders", "columns": []}] if with_table else []
        state.apply_observation(
            "search_schema",
            {"query": "orders"},
            {
                "ok": True,
                "schema": schema,
                "message": "success",
            },
        )

    @staticmethod
    def _apply_generation(state: ReactState, sql: str = "SELECT amount FROM orders") -> None:
        state.apply_observation(
            "generate_sql",
            {},
            {"ok": True, "sql": sql, "message": "success"},
        )

    @staticmethod
    def _apply_validation(state: ReactState, *, ok: bool) -> None:
        state.apply_observation(
            "validate_sql",
            {},
            {
                "ok": ok,
                "normalized_sql": (
                    "SELECT amount FROM orders LIMIT 1000" if ok else ""
                ),
                "message": "success" if ok else "SQL validation failed.",
            },
        )

    @staticmethod
    def _apply_execution(state: ReactState, rows: list[dict] | None = None) -> None:
        state.apply_observation(
            "execute_sql",
            {},
            {"ok": True, "rows": rows or [], "message": "success"},
        )

    def test_initial_state_only_allows_metric_search(self):
        state = self._state()

        self.assertEqual(self._tool_names(self.policy, state), ["search_metrics"])

    def test_successful_metric_search_enables_schema_search(self):
        state = self._state()
        self._apply_metrics(state)

        self.assertEqual(self._tool_names(self.policy, state), ["search_schema"])

    def test_failed_metric_search_can_leave_no_available_action(self):
        state = self._state()
        state.apply_observation(
            "search_metrics",
            {"query": "gmv"},
            {"ok": False, "metrics": [], "message": "retrieval failed"},
        )

        self.assertEqual(self._tool_names(self.policy, state), [])
        self.assertFalse(self.policy.can_finish(state))
        self.assertEqual(
            self.policy.authorize("search_metrics", {}, state).code,
            "max_calls_exceeded",
        )

    def test_schema_without_allowed_tables_does_not_enable_validation(self):
        state = self._state()
        self._apply_metrics(state)
        self._apply_schema(state, with_table=False)

        self.assertNotIn("generate_sql", self._tool_names(self.policy, state))
        self.assertNotIn("validate_sql", self._tool_names(self.policy, state))

    def test_schema_with_allowed_tables_requires_generation_before_validation(self):
        state = self._state()
        self._apply_metrics(state)
        self._apply_schema(state)

        self.assertEqual(self._tool_names(self.policy, state), ["generate_sql"])

    def test_generated_candidate_enables_validation(self):
        state = self._state()
        self._apply_metrics(state)
        self._apply_schema(state)
        self._apply_generation(state)

        self.assertEqual(self._tool_names(self.policy, state), ["validate_sql"])

    def test_failed_validation_regenerates_sql_until_generation_budget_is_used(self):
        state = self._state()
        self._apply_metrics(state)
        self._apply_schema(state)
        self._apply_generation(state, "SELECT * FROM users")
        self._apply_validation(state, ok=False)

        retry_decision = self.policy.authorize(
            "generate_sql",
            {},
            state,
        )
        self.assertTrue(retry_decision.allowed)
        self.assertNotIn("validate_sql", self._tool_names(self.policy, state))

        self._apply_generation(state)
        self._apply_validation(state, ok=False)

        exhausted_decision = self.policy.authorize(
            "generate_sql",
            {},
            state,
        )
        self.assertFalse(exhausted_decision.allowed)
        self.assertEqual(exhausted_decision.code, "max_calls_exceeded")

    def test_execute_sql_is_rejected_before_successful_validation(self):
        state = self._state(execute_enabled=True)
        self._apply_metrics(state)
        self._apply_schema(state)

        decision = self.policy.authorize("execute_sql", {}, state)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "prerequisite_not_met")

    def test_non_execute_flow_can_finish_after_successful_validation(self):
        state = self._state()
        self._apply_metrics(state)
        self._apply_schema(state)
        self._apply_generation(state)
        self._apply_validation(state, ok=True)

        self.assertTrue(self.policy.can_finish(state))
        self.assertNotIn("execute_sql", self._tool_names(self.policy, state))

    def test_execute_flow_requires_execution_after_successful_validation(self):
        state = self._state(execute_enabled=True)
        self._apply_metrics(state)
        self._apply_schema(state)
        self._apply_generation(state)
        self._apply_validation(state, ok=True)

        self.assertFalse(self.policy.can_finish(state))
        self.assertIn("execute_sql", self._tool_names(self.policy, state))

    def test_successful_empty_execution_allows_result_explanation(self):
        state = self._state(execute_enabled=True)
        self._apply_metrics(state)
        self._apply_schema(state)
        self._apply_generation(state)
        self._apply_validation(state, ok=True)
        self._apply_execution(state)

        self.assertIn("explain_result", self._tool_names(self.policy, state))
        self.assertFalse(self.policy.can_finish(state))

    def test_execute_flow_can_finish_after_successful_explanation(self):
        state = self._state(execute_enabled=True)
        self._apply_metrics(state)
        self._apply_schema(state)
        self._apply_generation(state)
        self._apply_validation(state, ok=True)
        self._apply_execution(state, rows=[{"gmv": 100}])
        state.apply_observation(
            "explain_result",
            {},
            {"ok": True, "explanation": "No rows.", "message": "success"},
        )

        self.assertTrue(self.policy.can_finish(state))

    def test_execute_flow_cannot_finish_after_failed_explanation(self):
        state = self._state(execute_enabled=True)
        self._apply_metrics(state)
        self._apply_schema(state)
        self._apply_generation(state)
        self._apply_validation(state, ok=True)
        self._apply_execution(state)
        state.apply_observation(
            "explain_result",
            {},
            {"ok": False, "explanation": "", "message": "explanation failed"},
        )

        self.assertFalse(self.policy.can_finish(state))

    def test_authorize_rejects_unregistered_tool(self):
        state = self._state()

        decision = self.policy.authorize("drop_database", {}, state)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "unknown_tool")


if __name__ == "__main__":
    unittest.main()
