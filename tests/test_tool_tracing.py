import unittest
from types import SimpleNamespace

from graph.dynamic_executor import execute_dynamic_graph
from graph.tools.registry import ToolRegistry, ToolSpec
from graph.tools.tracing import summarize_tool_payload


class ToolTracingTests(unittest.IsolatedAsyncioTestCase):
    def test_sql_payload_summary_redacts_raw_sql_and_records_shape(self):
        sql = "SELECT order_id FROM olist_orders_dataset LIMIT 10"

        summary = summarize_tool_payload("sql.validate", {"sql": sql})

        self.assertEqual(summary["keys"], ["sql"])
        self.assertEqual(summary["sql"]["statement_type"], "SELECT")
        self.assertEqual(summary["sql"]["tables"], ["olist_orders_dataset"])
        self.assertEqual(summary["sql"]["char_count"], len(sql))
        self.assertIn("sha256", summary["sql"])
        self.assertNotIn("order_id FROM", str(summary))

    async def test_dynamic_executor_records_structured_tool_trace(self):
        async def first(state, runtime, inputs):
            return {"candidate_sql": "SELECT 1"}

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="generate_sql",
                description="generate",
                aliases=("sql.generate",),
                risk_level="medium",
                handler=first,
            )
        )
        state = {
            "question": "show gmv",
            "tenant_id": "admin",
            "execute": False,
            "execution_graph": {"steps": [{"id": "generate", "tool": "sql.generate"}]},
            "trace": [],
        }

        result = await execute_dynamic_graph(
            state,
            SimpleNamespace(context=SimpleNamespace(allowed_tool_risk_levels=("low", "medium"))),
            registry=registry,
        )

        event = result["tool_trace"][0]
        self.assertEqual(event["tool_name"], "sql.generate")
        self.assertEqual(event["canonical_name"], "generate_sql")
        self.assertTrue(event["ok"])
        self.assertEqual(event["error_code"], "")
        self.assertIn("duration_ms", event)
        self.assertIn("input_summary", event)
        self.assertIn("output_summary", event)
        self.assertEqual(event["retry_count"], 0)


if __name__ == "__main__":
    unittest.main()
