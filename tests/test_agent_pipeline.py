import unittest
from unittest import mock

from agent import pipeline
from engine.models import QueryIntent


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
                "score": 0.9,
                "columns": [
                    {
                        "column_name": "amount",
                        "data_type": "numeric",
                        "nullable": False,
                        "comment": "Paid amount",
                    }
                ],
            }
        ],
        "message": "success",
    }


def sample_intent() -> QueryIntent:
    return QueryIntent(
        metrics=["gmv"],
        time_range={},
        dimensions=["region"],
        filters=[],
    )


def failed_validation(code: str = "table_not_allowed") -> dict:
    return {
        "ok": False,
        "normalized_sql": "",
        "violations": [
            {
                "code": code,
                "message": "SQL uses tables outside allowed_tables: users",
            }
        ],
        "warnings": [],
        "message": "SQL validation failed.",
    }


def successful_validation(sql: str) -> dict:
    return {
        "ok": True,
        "normalized_sql": sql.upper(),
        "violations": [],
        "warnings": [],
        "message": "success",
    }


class AgentPipelineRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_agent_retries_once_after_validation_failure(self):
        with mock.patch.object(
            pipeline,
            "search_metrics",
            new=mock.AsyncMock(return_value=sample_metrics_result()),
        ) as search_metrics, mock.patch.object(
            pipeline,
            "search_schema",
            new=mock.AsyncMock(return_value=sample_schema_result()),
        ) as search_schema, mock.patch.object(
            pipeline,
            "parse_intent",
            new=mock.AsyncMock(return_value=sample_intent()),
        ) as parse_intent, mock.patch.object(
            pipeline,
            "generate_sql",
            new=mock.AsyncMock(side_effect=["select * from users", "select amount from orders"]),
        ) as generate_sql, mock.patch.object(
            pipeline,
            "validate_sql",
            new=mock.AsyncMock(
                side_effect=[
                    failed_validation(),
                    successful_validation("select amount from orders"),
                ]
            ),
        ) as validate_sql:
            result = await pipeline.run_agent_nl2sql("show gmv", llm=object())

        self.assertTrue(result["ok"])
        self.assertEqual(result["sql"], "select amount from orders")
        self.assertEqual(result["executed_sql"], "SELECT AMOUNT FROM ORDERS")
        self.assertEqual(generate_sql.await_count, 2)
        self.assertIsNone(generate_sql.await_args_list[0].kwargs["retry_feedback"])

        retry_feedback = generate_sql.await_args_list[1].kwargs["retry_feedback"]
        self.assertIn("Previous SQL:", retry_feedback)
        self.assertIn("select * from users", retry_feedback)
        self.assertIn("table_not_allowed", retry_feedback)

        self.assertEqual(validate_sql.await_count, 2)
        self.assertEqual(validate_sql.await_args_list[0].kwargs["allowed_tables"], ["orders"])
        search_metrics.assert_awaited_once()
        search_schema.assert_awaited_once()
        parse_intent.assert_awaited_once()

    async def test_run_agent_returns_failure_after_retry_is_exhausted(self):
        with mock.patch.object(
            pipeline,
            "search_metrics",
            new=mock.AsyncMock(return_value=sample_metrics_result()),
        ), mock.patch.object(
            pipeline,
            "search_schema",
            new=mock.AsyncMock(return_value=sample_schema_result()),
        ), mock.patch.object(
            pipeline,
            "parse_intent",
            new=mock.AsyncMock(return_value=sample_intent()),
        ), mock.patch.object(
            pipeline,
            "generate_sql",
            new=mock.AsyncMock(side_effect=["select * from users", "select * from refunds"]),
        ) as generate_sql, mock.patch.object(
            pipeline,
            "validate_sql",
            new=mock.AsyncMock(side_effect=[failed_validation(), failed_validation("not_select")]),
        ) as validate_sql:
            result = await pipeline.run_agent_nl2sql("show gmv", llm=object())

        self.assertFalse(result["ok"])
        self.assertEqual(result["sql"], "select * from refunds")
        self.assertEqual(result["validation"]["violations"][0]["code"], "not_select")
        self.assertEqual(result["message"], "SQL validation failed.")
        self.assertEqual(generate_sql.await_count, 2)
        self.assertEqual(validate_sql.await_count, 2)

    async def test_run_agent_does_not_retry_when_first_validation_passes(self):
        with mock.patch.object(
            pipeline,
            "search_metrics",
            new=mock.AsyncMock(return_value=sample_metrics_result()),
        ), mock.patch.object(
            pipeline,
            "search_schema",
            new=mock.AsyncMock(return_value=sample_schema_result()),
        ), mock.patch.object(
            pipeline,
            "parse_intent",
            new=mock.AsyncMock(return_value=sample_intent()),
        ), mock.patch.object(
            pipeline,
            "generate_sql",
            new=mock.AsyncMock(return_value="select amount from orders"),
        ) as generate_sql, mock.patch.object(
            pipeline,
            "validate_sql",
            new=mock.AsyncMock(return_value=successful_validation("select amount from orders")),
        ) as validate_sql:
            result = await pipeline.run_agent_nl2sql("show gmv", llm=object())

        self.assertTrue(result["ok"])
        self.assertEqual(generate_sql.await_count, 1)
        self.assertEqual(validate_sql.await_count, 1)

    async def test_run_agent_executes_sql_through_agent_tool(self):
        execution_result = {
            "ok": True,
            "sql": "SELECT AMOUNT FROM ORDERS",
            "normalized_sql": "SELECT AMOUNT FROM ORDERS",
            "tenant_id": "demo",
            "rows": [{"amount": 100}],
            "row_count": 1,
            "message": "success",
        }
        explanation_result = {
            "ok": True,
            "explanation": "查询返回 1 行。前 1 行示例：amount=100。",
            "message": "success",
        }

        with mock.patch.object(
            pipeline,
            "search_metrics",
            new=mock.AsyncMock(return_value=sample_metrics_result()),
        ), mock.patch.object(
            pipeline,
            "search_schema",
            new=mock.AsyncMock(return_value=sample_schema_result()),
        ), mock.patch.object(
            pipeline,
            "parse_intent",
            new=mock.AsyncMock(return_value=sample_intent()),
        ), mock.patch.object(
            pipeline,
            "generate_sql",
            new=mock.AsyncMock(return_value="select amount from orders"),
        ), mock.patch.object(
            pipeline,
            "validate_sql",
            new=mock.AsyncMock(return_value=successful_validation("select amount from orders")),
        ), mock.patch.object(
            pipeline,
            "execute_readonly_sql",
            new=mock.AsyncMock(return_value=[{"amount": 999}]),
            create=True,
        ), mock.patch.object(
            pipeline,
            "execute_sql",
            new=mock.AsyncMock(return_value=execution_result),
        ) as execute_sql, mock.patch.object(
            pipeline,
            "explain_result",
            new=mock.AsyncMock(return_value=explanation_result),
            create=True,
        ) as explain_result:
            result = await pipeline.run_agent_nl2sql(
                "show gmv",
                llm=object(),
                execute=True,
                dsn="postgres://example",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], [{"amount": 100}])
        execute_sql.assert_awaited_once_with(
            sql="SELECT AMOUNT FROM ORDERS",
            tenant_id="demo",
            dsn="postgres://example",
            max_limit=1000,
            allowed_tables=["orders"],
        )
        explain_result.assert_awaited_once_with(
            question="show gmv",
            sql="SELECT AMOUNT FROM ORDERS",
            rows=[{"amount": 100}],
            metrics_result=sample_metrics_result(),
            llm=mock.ANY,
        )
        self.assertEqual(result["explanation"], "查询返回 1 行。前 1 行示例：amount=100。")
        self.assertEqual(result["explanation_result"], explanation_result)


if __name__ == "__main__":
    unittest.main()
