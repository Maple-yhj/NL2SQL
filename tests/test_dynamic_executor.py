import unittest
from types import SimpleNamespace

from graph.dynamic_executor import execute_dynamic_graph
from graph.tools.registry import ToolRegistry, ToolSpec


class DynamicExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_dynamic_graph_runs_steps_in_dependency_order(self):
        calls = []

        async def first(state, runtime, inputs):
            calls.append("first")
            return {"first_value": "ready"}

        async def second(state, runtime, inputs):
            calls.append(f"second:{state['first_value']}")
            return {"second_value": "done"}

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="first",
                description="first",
                output_keys=("first_value",),
                handler=first,
            )
        )
        registry.register(
            ToolSpec(
                name="second",
                description="second",
                input_keys=("first_value",),
                output_keys=("second_value",),
                handler=second,
            )
        )
        state = {
            "question": "show gmv",
            "tenant_id": "admin",
            "execute": False,
            "execution_graph": {
                "steps": [
                    {"id": "second_step", "tool": "second", "depends_on": ["first_step"]},
                    {"id": "first_step", "tool": "first"},
                ]
            },
            "trace": [],
        }

        result = await execute_dynamic_graph(state, SimpleNamespace(), registry=registry)

        self.assertEqual(calls, ["first", "second:ready"])
        self.assertEqual(result["first_value"], "ready")
        self.assertEqual(result["second_value"], "done")
        self.assertEqual(
            [item["node"] for item in result["trace"]],
            ["dynamic:first", "dynamic:second"],
        )

    async def test_execute_dynamic_graph_returns_error_for_unknown_tool(self):
        state = {
            "question": "show gmv",
            "tenant_id": "admin",
            "execute": False,
            "execution_graph": {"steps": [{"id": "bad", "tool": "missing"}]},
            "trace": [],
        }

        result = await execute_dynamic_graph(state, SimpleNamespace(), registry=ToolRegistry())

        self.assertEqual(result["error"], "Unknown tool: missing")
        self.assertFalse(result["trace"][-1]["ok"])

    async def test_execute_dynamic_graph_accepts_namespaced_tool_alias(self):
        async def prepare(state, runtime, inputs):
            return {"validated_sql": "SELECT 1"}

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="prepare_sql",
                description="prepare",
                aliases=("sql.prepare",),
                output_keys=("validated_sql",),
                handler=prepare,
            )
        )
        state = {
            "question": "show gmv",
            "tenant_id": "admin",
            "execute": False,
            "execution_graph": {"steps": [{"id": "prepare", "tool": "sql.prepare"}]},
            "trace": [],
        }

        result = await execute_dynamic_graph(state, SimpleNamespace(), registry=registry)

        self.assertEqual(result["validated_sql"], "SELECT 1")
        self.assertEqual(result["trace"][-1]["node"], "dynamic:prepare_sql")

    async def test_execute_dynamic_graph_applies_pre_call_policy(self):
        calls = []

        async def execute(state, runtime, inputs):
            calls.append("called")
            return {"rows": [{"value": 1}]}

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="execute_sql",
                description="execute",
                risk_level="high",
                side_effects="read",
                handler=execute,
            )
        )
        state = {
            "question": "show gmv",
            "tenant_id": "admin",
            "execute": True,
            "execution_graph": {"steps": [{"id": "execute", "tool": "execute_sql"}]},
            "trace": [],
        }

        result = await execute_dynamic_graph(
            state,
            SimpleNamespace(context=SimpleNamespace()),
            registry=registry,
        )

        self.assertEqual(calls, [])
        self.assertEqual(result["error"], "Tool risk level 'high' is not allowed for this runtime.")
        self.assertEqual(result["trace"][-1]["node"], "dynamic:execute_sql")
        self.assertFalse(result["trace"][-1]["policy_ok"])


if __name__ == "__main__":
    unittest.main()
