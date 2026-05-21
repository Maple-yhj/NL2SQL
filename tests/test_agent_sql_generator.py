import unittest

from agent.sql_generator import (
    SQL_SYSTEM,
    _extract_sql,
    build_sql_prompt,
    generate_sql,
)
from engine.models import QueryIntent


class FakeLLM:
    def __init__(self, text: str):
        self.text = text
        self.calls = []

    async def complete(self, prompt: str, system: str = "", **kwargs):
        self.calls.append({"prompt": prompt, "system": system, **kwargs})
        return self.text


def sample_intent() -> QueryIntent:
    return QueryIntent(
        metrics=["gmv"],
        time_range={"start": "2026-04-01", "end": "2026-05-01"},
        dimensions=["region"],
        filters=["paid orders only"],
    )


def sample_metrics_result() -> dict:
    return {
        "ok": True,
        "query": "gmv by region",
        "tenant_id": "demo",
        "metrics": [
            {
                "metric_name": "gmv",
                "display_name": "GMV",
                "business_def": "Paid gross merchandise value.",
                "sql_expr": "SUM(orders.amount)",
                "base_table": "orders",
                "time_column": "orders.paid_at",
                "dimensions": ["orders.region"],
                "filters": ["orders.status = 'paid'"],
                "join_tables": [],
                "forbidden": ["refund amount"],
                "synonyms": ["sales amount"],
                "score": 0.91,
            }
        ],
        "message": "success",
    }


def sample_schema_result() -> dict:
    return {
        "ok": True,
        "query": "gmv by region",
        "tenant_id": "demo",
        "schema": [
            {
                "table_name": "orders",
                "table_comment": "Order fact table",
                "score": 0.88,
                "columns": [
                    {
                        "column_name": "amount",
                        "data_type": "numeric",
                        "nullable": False,
                        "default": None,
                        "comment": "Paid order amount",
                        "sample_values": ["99.00", "128.50"],
                        "score": 0.95,
                    },
                    {
                        "column_name": "region",
                        "data_type": "text",
                        "nullable": True,
                        "default": None,
                        "comment": "Order region",
                        "sample_values": ["east", "south"],
                        "score": 0.90,
                    },
                ],
            }
        ],
        "message": "success",
    }


class AgentSqlGeneratorTests(unittest.IsolatedAsyncioTestCase):
    def test_build_sql_prompt_includes_metric_and_schema_context(self):
        prompt = build_sql_prompt(
            question="Show GMV by region last month",
            intent=sample_intent(),
            metrics_result=sample_metrics_result(),
            schema_result=sample_schema_result(),
            retry_feedback=None,
        )

        self.assertIn("Question:", prompt)
        self.assertIn("Show GMV by region last month", prompt)
        self.assertIn("[METRIC CONTEXT]", prompt)
        self.assertIn("Metric: gmv", prompt)
        self.assertIn("SUM(orders.amount)", prompt)
        self.assertIn("orders.status = 'paid'", prompt)
        self.assertIn("[SCHEMA CONTEXT]", prompt)
        self.assertIn("Table:orders", prompt)
        self.assertIn("-amount|numeric|not null|Paid order amount|['99.00', '128.50']", prompt)
        self.assertIn("-region|text|nullable|Order region|['east', 'south']", prompt)

    def test_extract_sql_supports_fenced_sql_and_removes_semicolon(self):
        sql = _extract_sql(
            """
            ```sql
            select region,
                   sum(amount) as gmv
            from orders
            group by region;
            ```
            """
        )

        self.assertEqual(
            sql,
            "select region, sum(amount) as gmv from orders group by region",
        )

    def test_extract_sql_rejects_empty_model_output(self):
        with self.assertRaisesRegex(ValueError, "empty SQL"):
            _extract_sql("```sql\n   \n```")

    async def test_generate_sql_calls_llm_with_prompt_and_extracts_sql(self):
        llm = FakeLLM(
            """
            ```sql
            select region, sum(amount) as gmv
            from orders
            where status = 'paid'
            group by region;
            ```
            """
        )

        sql = await generate_sql(
            question="Show GMV by region last month",
            intent=sample_intent(),
            metrics_result=sample_metrics_result(),
            schema_result=sample_schema_result(),
            retry_feedback=None,
            llm=llm,
        )

        self.assertEqual(
            sql,
            "select region, sum(amount) as gmv from orders where status = 'paid' group by region",
        )
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["system"], SQL_SYSTEM)
        self.assertIn("[METRIC CONTEXT]", llm.calls[0]["prompt"])
        self.assertIn("[SCHEMA CONTEXT]", llm.calls[0]["prompt"])

    async def test_generate_sql_rejects_missing_intent(self):
        with self.assertRaisesRegex(ValueError, "intent"):
            await generate_sql(
                question="Show GMV",
                intent=None,
                metrics_result=sample_metrics_result(),
                schema_result=sample_schema_result(),
                retry_feedback=None,
                llm=FakeLLM("select 1"),
            )

    def test_build_sql_prompt_includes_retry_feedback_when_present(self):
        prompt = build_sql_prompt(
            question="Show GMV by region last month",
            intent=sample_intent(),
            metrics_result=sample_metrics_result(),
            schema_result=sample_schema_result(),
            retry_feedback="Previous SQL:\nselect * from users\nViolations:\n- table_not_allowed",
        )

        self.assertIn("Previous SQL:", prompt)
        self.assertIn("table_not_allowed", prompt)


if __name__ == "__main__":
    unittest.main()
