import unittest

from graph.tools.explain_result import explain_result
from graph.tools.validate_sql import validate_sql


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, prompt, system="", max_output_tokens=2048):
        self.calls.append({"prompt": prompt, "system": system})
        return "GMV is 100."


class GraphToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_enforces_authorized_tables_and_limit(self):
        valid = await validate_sql(
            "SELECT amount FROM orders",
            tenant_id="demo",
            allowed_tables=["orders"],
            max_limit=100,
        )
        invalid = await validate_sql(
            "SELECT * FROM users",
            tenant_id="demo",
            allowed_tables=["orders"],
            max_limit=100,
        )

        self.assertTrue(valid["ok"])
        self.assertIn("LIMIT 100", valid["normalized_sql"])
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["violations"][0]["code"], "table_not_allowed")

    async def test_validation_rejects_missing_authorized_table_scope(self):
        result = await validate_sql(
            "SELECT amount FROM orders",
            tenant_id="demo",
            allowed_tables=[],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["violations"][0]["code"], "missing_allowed_tables")

    async def test_explanation_uses_injected_langchain_llm(self):
        llm = FakeLLM()
        result = await explain_result(
            question="show gmv",
            sql="SELECT 100 AS gmv",
            rows=[{"gmv": 100}],
            metrics_result={"metrics": [{"display_name": "GMV"}]},
            llm=llm,
        )

        self.assertEqual(result["explanation"], "GMV is 100.")
        self.assertIn("show gmv", llm.calls[0]["prompt"])
        self.assertIn("100", llm.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
