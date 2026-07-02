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
            conversation_history=[],
            user_memories=[],
        )
        self.assertIn("table_not_allowed", prompt)

    def test_prompt_includes_plan_context_when_provided(self):
        prompt = build_sql_prompt(
            question="show gmv trend",
            intent=QueryIntent(metrics=["gmv"]),
            metrics_result={"metrics": []},
            schema_result={"schema": []},
            retry_feedback=None,
            conversation_history=[],
            user_memories=[],
            plan_context='{"plan":{"analysis_type":"trend"}}',
        )

        self.assertIn("[PLAN DSL]", prompt)
        self.assertIn('"analysis_type":"trend"', prompt)

    def test_prompt_includes_conversation_context_and_user_memories(self):
        prompt = build_sql_prompt(
            question="show gmv by region last month",
            intent=QueryIntent(metrics=["gmv"]),
            metrics_result={"metrics": []},
            schema_result={"schema": []},
            retry_feedback=None,
            conversation_history=[
                {"role": "user", "content": "show gmv last month", "metadata": {}},
                {"role": "assistant", "content": "GMV is 100.", "metadata": {"sql": "SELECT 100 AS gmv"}},
            ],
            user_memories=[{"memory_key": "preferred_region", "memory_value": "华东", "metadata": {}}],
        )

        self.assertIn("[CONVERSATION CONTEXT]", prompt)
        self.assertIn("show gmv last month", prompt)
        self.assertIn("preferred_region: 华东", prompt)

    def test_prompt_includes_seller_scope_for_non_admin_tenant(self):
        prompt = build_sql_prompt(
            question="show my gmv",
            intent=QueryIntent(metrics=["gmv"]),
            metrics_result={"metrics": []},
            schema_result={"schema": []},
            retry_feedback=None,
            conversation_history=[],
            user_memories=[],
            tenant_id="seller-1",
        )

        self.assertIn("[TENANT SCOPE]", prompt)
        self.assertIn("seller_id = 'seller-1'", prompt)

    def test_prompt_does_not_include_seller_scope_for_admin_tenant(self):
        prompt = build_sql_prompt(
            question="show all gmv",
            intent=QueryIntent(metrics=["gmv"]),
            metrics_result={"metrics": []},
            schema_result={"schema": []},
            retry_feedback=None,
            conversation_history=[],
            user_memories=[],
            tenant_id="admin",
        )

        self.assertNotIn("[TENANT SCOPE]", prompt)


if __name__ == "__main__":
    unittest.main()
