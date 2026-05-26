import unittest

from agent.react_planner import PlannedAction
from agent.react_state import ReactRuntimeConfig
from engine.models import QueryIntent


def metrics_result() -> dict:
    return {
        "ok": True,
        "metrics": [{"metric_name": "gmv", "base_table": "orders"}],
        "message": "success",
    }


def schema_result() -> dict:
    return {
        "ok": True,
        "schema": [{"table_name": "orders", "columns": []}],
        "message": "success",
    }


def valid_sql_result() -> dict:
    return {
        "ok": True,
        "normalized_sql": "SELECT amount FROM orders LIMIT 1000",
        "violations": [],
        "warnings": [],
        "message": "success",
    }


class SequencePlanner:
    def __init__(self, actions: list[PlannedAction]) -> None:
        self.actions = list(actions)
        self.available_tools: list[list[str]] = []

    async def __call__(self, *, available_tools, **kwargs) -> PlannedAction:
        self.available_tools.append([tool.name for tool in available_tools])
        return self.actions.pop(0)


class FakeToolRunner:
    def __init__(self, results: dict[str, list[dict]]) -> None:
        self.results = {name: list(values) for name, values in results.items()}
        self.calls: list[tuple[str, dict, object]] = []

    async def __call__(self, name: str, arguments: dict, context) -> dict:
        self.calls.append((name, arguments, context))
        return self.results[name].pop(0)


class AgentReactLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from agent.react_loop import run_react_nl2sql

        self.run_react_nl2sql = run_react_nl2sql
        self.intent_calls = 0

    async def _parse_intent(self, question: str, llm) -> QueryIntent:
        self.intent_calls += 1
        return QueryIntent(metrics=["gmv"])

    async def test_non_execute_flow_completes_through_controlled_tool_sequence(self):
        planner = SequencePlanner(
            [
                PlannedAction("search_metrics", {"query": "gmv"}),
                PlannedAction("search_schema", {"query": "orders amount"}),
                PlannedAction("generate_sql", {}),
                PlannedAction("validate_sql", {}),
            ]
        )
        tools = FakeToolRunner(
            {
                "search_metrics": [metrics_result()],
                "search_schema": [schema_result()],
                "generate_sql": [
                    {"ok": True, "sql": "SELECT amount FROM orders", "message": "success"}
                ],
                "validate_sql": [valid_sql_result()],
            }
        )

        result = await self.run_react_nl2sql(
            "show gmv",
            config=ReactRuntimeConfig(tenant_id="demo", max_steps=6),
            planner=planner,
            intent_parser=self._parse_intent,
            tool_runner=tools,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sql"], "SELECT amount FROM orders")
        self.assertEqual(result["executed_sql"], "SELECT amount FROM orders LIMIT 1000")
        self.assertEqual(self.intent_calls, 1)
        self.assertEqual([call[0] for call in tools.calls], [
            "search_metrics",
            "search_schema",
            "generate_sql",
            "validate_sql",
        ])
        self.assertEqual(planner.available_tools, [
            ["search_metrics"],
            ["search_schema"],
            ["generate_sql"],
            ["validate_sql"],
        ])

    async def test_validation_failure_regenerates_using_retry_feedback(self):
        planner = SequencePlanner(
            [
                PlannedAction("search_metrics", {}),
                PlannedAction("search_schema", {}),
                PlannedAction("generate_sql", {}),
                PlannedAction("validate_sql", {}),
                PlannedAction("generate_sql", {}),
                PlannedAction("validate_sql", {}),
            ]
        )
        tools = FakeToolRunner(
            {
                "search_metrics": [metrics_result()],
                "search_schema": [schema_result()],
                "generate_sql": [
                    {"ok": True, "sql": "SELECT * FROM users", "message": "success"},
                    {"ok": True, "sql": "SELECT amount FROM orders", "message": "success"},
                ],
                "validate_sql": [
                    {
                        "ok": False,
                        "normalized_sql": "",
                        "violations": [
                            {"code": "table_not_allowed", "message": "users is not allowed"}
                        ],
                        "warnings": [],
                        "message": "SQL validation failed.",
                    },
                    valid_sql_result(),
                ],
            }
        )

        result = await self.run_react_nl2sql(
            "show gmv",
            config=ReactRuntimeConfig(tenant_id="demo", max_steps=8),
            planner=planner,
            intent_parser=self._parse_intent,
            tool_runner=tools,
        )

        self.assertTrue(result["ok"])
        second_generation_context = tools.calls[4][2]
        self.assertIn("Previous SQL:\nSELECT * FROM users", second_generation_context.retry_feedback)
        self.assertIn("table_not_allowed", second_generation_context.retry_feedback)

    async def test_execute_flow_continues_through_explanation(self):
        planner = SequencePlanner(
            [
                PlannedAction("search_metrics", {}),
                PlannedAction("search_schema", {}),
                PlannedAction("generate_sql", {}),
                PlannedAction("validate_sql", {}),
                PlannedAction("execute_sql", {}),
                PlannedAction("explain_result", {}),
            ]
        )
        tools = FakeToolRunner(
            {
                "search_metrics": [metrics_result()],
                "search_schema": [schema_result()],
                "generate_sql": [
                    {"ok": True, "sql": "SELECT amount FROM orders", "message": "success"}
                ],
                "validate_sql": [valid_sql_result()],
                "execute_sql": [
                    {"ok": True, "rows": [{"amount": 100}], "message": "success"}
                ],
                "explain_result": [
                    {"ok": True, "explanation": "GMV is 100.", "message": "success"}
                ],
            }
        )

        result = await self.run_react_nl2sql(
            "show gmv",
            config=ReactRuntimeConfig(
                tenant_id="demo",
                execute_enabled=True,
                max_steps=8,
            ),
            planner=planner,
            intent_parser=self._parse_intent,
            tool_runner=tools,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], [{"amount": 100}])
        self.assertEqual(result["explanation"], "GMV is 100.")
        self.assertEqual([call[0] for call in tools.calls][-2:], ["execute_sql", "explain_result"])

    async def test_dead_end_returns_failure_after_failed_metrics_retrieval(self):
        planner = SequencePlanner([PlannedAction("search_metrics", {})])
        tools = FakeToolRunner(
            {
                "search_metrics": [
                    {"ok": False, "metrics": [], "message": "retrieval failed"}
                ]
            }
        )

        result = await self.run_react_nl2sql(
            "show gmv",
            config=ReactRuntimeConfig(tenant_id="demo", max_steps=4),
            planner=planner,
            intent_parser=self._parse_intent,
            tool_runner=tools,
        )

        self.assertFalse(result["ok"])
        self.assertIn("No available tools", result["message"])
        self.assertEqual(result["trace"][0]["status"], "executed")

    async def test_unauthorized_actions_consume_turns_until_max_steps(self):
        planner = SequencePlanner(
            [
                PlannedAction("execute_sql", {}),
                PlannedAction("execute_sql", {}),
            ]
        )
        tools = FakeToolRunner({})

        result = await self.run_react_nl2sql(
            "show gmv",
            config=ReactRuntimeConfig(tenant_id="demo", max_steps=2),
            planner=planner,
            intent_parser=self._parse_intent,
            tool_runner=tools,
        )

        self.assertFalse(result["ok"])
        self.assertIn("maximum steps", result["message"])
        self.assertEqual(tools.calls, [])
        self.assertEqual(
            [entry["status"] for entry in result["trace"]],
            ["rejected", "rejected"],
        )


if __name__ == "__main__":
    unittest.main()
