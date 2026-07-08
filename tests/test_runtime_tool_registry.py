import inspect
import unittest

from graph.tools.registry import default_tool_registry
from graph.tools.runtime_registry import build_runtime_tool_registry


async def fake_handler(state, runtime, inputs):
    return {"ok": True}


class RuntimeToolRegistryTests(unittest.TestCase):
    def test_runtime_registry_includes_manifest_tools_and_binds_handlers(self):
        manifest = default_tool_registry()
        runtime = build_runtime_tool_registry(
            {
                "search_metrics": fake_handler,
                "search_schema": fake_handler,
                "generate_sql": fake_handler,
                "prepare_sql": fake_handler,
                "validate_sql": fake_handler,
                "execute_sql": fake_handler,
                "explain_result": fake_handler,
                "explain_table_result": fake_handler,
            }
        )

        self.assertEqual(set(runtime.names()), set(manifest.names()))
        self.assertIs(runtime.get("prepare_sql").input_schema, manifest.get("prepare_sql").input_schema)
        self.assertEqual(runtime.get("execute_sql").risk_level, "high")
        self.assertTrue(inspect.iscoroutinefunction(runtime.get("prepare_sql").handler))

    def test_runtime_registry_rejects_undeclared_handlers(self):
        with self.assertRaisesRegex(ValueError, "not declared"):
            build_runtime_tool_registry({"missing_tool": fake_handler})


if __name__ == "__main__":
    unittest.main()
