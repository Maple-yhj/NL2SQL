import unittest

from agent.react_state import ReactRuntimeConfig, ReactState
from engine.models import QueryIntent


class AgentReactStateTests(unittest.TestCase):
    @staticmethod
    def _state() -> ReactState:
        return ReactState(
            question="show gmv",
            config=ReactRuntimeConfig(tenant_id="demo", llm=object()),
            intent=QueryIntent(metrics=["gmv"]),
            metrics_result={"ok": True, "metrics": [{"metric_name": "gmv"}]},
            schema_result={"ok": True, "schema": [{"table_name": "orders"}]},
            allowed_tables=["orders"],
        )

    def test_generate_sql_observation_stores_candidate_in_tool_context(self):
        state = self._state()

        state.apply_observation(
            "generate_sql",
            {},
            {"ok": True, "sql": "SELECT amount FROM orders", "message": "success"},
        )

        context = state.to_tool_context()
        self.assertEqual(state.raw_sql, "SELECT amount FROM orders")
        self.assertEqual(context.candidate_sql, "SELECT amount FROM orders")
        self.assertEqual(context.intent, state.intent)
        self.assertEqual(context.schema_result, state.schema_result)

    def test_failed_validation_builds_feedback_for_next_generation(self):
        state = self._state()
        state.apply_observation(
            "generate_sql",
            {},
            {"ok": True, "sql": "SELECT * FROM users", "message": "success"},
        )

        state.apply_observation(
            "validate_sql",
            {},
            {
                "ok": False,
                "normalized_sql": "",
                "violations": [
                    {
                        "code": "table_not_allowed",
                        "message": "users is not authorized",
                    }
                ],
                "warnings": [],
                "message": "SQL validation failed.",
            },
        )

        self.assertIn("Previous SQL:\nSELECT * FROM users", state.retry_feedback)
        self.assertIn("table_not_allowed", state.retry_feedback)
        self.assertEqual(state.to_tool_context().retry_feedback, state.retry_feedback)

        state.apply_observation(
            "generate_sql",
            {},
            {"ok": True, "sql": "SELECT amount FROM orders", "message": "success"},
        )

        self.assertIsNone(state.validation_result)
        self.assertIsNone(state.retry_feedback)


if __name__ == "__main__":
    unittest.main()
