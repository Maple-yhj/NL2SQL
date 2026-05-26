import unittest

from agent.react_planner import (
    PLANNER_SYSTEM,
    PlannedAction,
    PlannerOutputError,
    build_planner_prompt,
    choose_action,
    parse_action,
)
from agent.react_state import ReactRuntimeConfig, ReactState
from agent.tools.registry import get_tool_spec
from engine.models import QueryIntent


class FakeLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    async def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, **kwargs})
        return self.text


class AgentReactPlannerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _state() -> ReactState:
        return ReactState(
            question="show gmv by region",
            config=ReactRuntimeConfig(tenant_id="private-tenant"),
            intent=QueryIntent(metrics=["gmv"], dimensions=["region"]),
            metrics_result={"ok": True, "metrics": [{"metric_name": "gmv"}]},
            table_names=["orders"],
            raw_sql="SELECT confidential_value FROM orders",
            retry_feedback="secret validation feedback",
        )

    def test_prompt_exposes_summary_and_schema_without_sensitive_context(self):
        prompt = build_planner_prompt(
            question="show gmv by region",
            state=self._state(),
            available_tools=[get_tool_spec("search_schema")],
        )

        self.assertIn('"metric_names": [', prompt)
        self.assertIn('"gmv"', prompt)
        self.assertIn('"metric_table_names": [', prompt)
        self.assertIn('"search_schema"', prompt)
        self.assertNotIn("private-tenant", prompt)
        self.assertNotIn("confidential_value", prompt)
        self.assertNotIn("secret validation feedback", prompt)

    def test_parse_action_accepts_allowed_tool_arguments(self):
        action = parse_action(
            '```json\n{"tool_name": "search_schema", "arguments": {"query": "orders amount region"}}\n```',
            [get_tool_spec("search_schema")],
        )

        self.assertEqual(
            action,
            PlannedAction(
                tool_name="search_schema",
                arguments={"query": "orders amount region"},
            ),
        )

    def test_parse_action_rejects_tool_not_currently_available(self):
        with self.assertRaisesRegex(PlannerOutputError, "not available"):
            parse_action(
                '{"tool_name": "validate_sql", "arguments": {}}',
                [get_tool_spec("search_schema")],
            )

    def test_parse_action_rejects_sensitive_or_unsupported_arguments(self):
        with self.assertRaisesRegex(PlannerOutputError, "unsupported arguments"):
            parse_action(
                '{"tool_name": "validate_sql", "arguments": {"sql": "SELECT * FROM users"}}',
                [get_tool_spec("validate_sql")],
            )

    def test_parse_action_rejects_non_object_arguments(self):
        with self.assertRaisesRegex(PlannerOutputError, "arguments must be an object"):
            parse_action(
                '{"tool_name": "search_schema", "arguments": "orders"}',
                [get_tool_spec("search_schema")],
            )

    async def test_choose_action_skips_llm_for_single_parameterless_tool(self):
        llm = FakeLLM('{"tool_name": "search_schema", "arguments": {}}')

        action = await choose_action(
            question="show gmv",
            state=self._state(),
            available_tools=[get_tool_spec("generate_sql")],
            llm=llm,
        )

        self.assertEqual(action, PlannedAction(tool_name="generate_sql", arguments={}))
        self.assertEqual(llm.calls, [])

    async def test_choose_action_uses_llm_for_search_arguments(self):
        llm = FakeLLM(
            '{"tool_name": "search_schema", "arguments": {"query": "orders amount region"}}'
        )

        action = await choose_action(
            question="show gmv by region",
            state=self._state(),
            available_tools=[get_tool_spec("search_schema")],
            llm=llm,
        )

        self.assertEqual(action.tool_name, "search_schema")
        self.assertEqual(action.arguments, {"query": "orders amount region"})
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["system"], PLANNER_SYSTEM)
        self.assertIn("<runtime_json>", llm.calls[0]["prompt"])

    async def test_choose_action_rejects_empty_available_tool_set(self):
        with self.assertRaisesRegex(PlannerOutputError, "No available tools"):
            await choose_action(
                question="show gmv",
                state=self._state(),
                available_tools=[],
                llm=FakeLLM("{}"),
            )


if __name__ == "__main__":
    unittest.main()
