import unittest

from engine.models import QueryIntent
from graph.tools.sql_generator import build_sql_prompt, generate_sql


class FakeLLM:
    async def complete(self, prompt, system="", max_output_tokens=2048):
        return "```sql\nSELECT amount FROM orders\n```"


class SqlGeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_sql_extracts_single_statement(self):
        sql = await generate_sql(
            question="show gmv",
            intent=QueryIntent(metrics=["gmv"]),
            metrics_result={"metrics": [{"metric_name": "gmv", "base_table": "orders"}]},
            schema_result={"schema": [{"table_name": "orders", "columns": []}]},
            retry_feedback=None,
            llm=FakeLLM(),
        )
        self.assertEqual(sql, "SELECT amount FROM orders")

    def test_prompt_includes_validation_feedback(self):
        prompt = build_sql_prompt(
            question="show gmv",
            intent=QueryIntent(metrics=["gmv"]),
            metrics_result={"metrics": []},
            schema_result={"schema": []},
            retry_feedback="table_not_allowed",
        )
        self.assertIn("table_not_allowed", prompt)


if __name__ == "__main__":
    unittest.main()
