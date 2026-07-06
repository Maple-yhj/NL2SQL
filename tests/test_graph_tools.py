import builtins
import importlib
import os
import types
import unittest
from unittest import mock

from graph.tools.explain_result import explain_result
from graph.tools.explain_table_result import explain_table_result
from graph.tools.domain_sql_validator import validate_domain_sql
from graph.tools.validate_sql import validate_sql


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, prompt, system="", max_output_tokens=2048):
        self.calls.append({"prompt": prompt, "system": system})
        return "GMV is 100."


class GraphToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_domain_validation_accepts_required_group_by_inside_cte(self):
        result = validate_domain_sql(
            sql=(
                "WITH monthly AS ("
                "SELECT date_trunc('month', shipping_limit_date) AS month, "
                "SUM(price + freight_value) AS gmv "
                "FROM olist_order_items_dataset "
                "GROUP BY month"
                "), growth AS ("
                "SELECT month, gmv - LAG(gmv) OVER (ORDER BY month) AS growth "
                "FROM monthly"
                ") "
                "SELECT month, growth FROM growth ORDER BY growth DESC LIMIT 1"
            ),
            constraints={
                "matched_rules": ["monthly_gmv_growth"],
                "required_group_by": ["month"],
                "required_order_by": ["growth DESC"],
                "required_sql_fragments": [
                    "date_trunc('month'",
                    "shipping_limit_date",
                    "AS month",
                    "SUM(",
                ],
            },
        )

        self.assertTrue(result["ok"], result["violations"])

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

    async def test_validation_ignores_cte_names_when_authorizing_tables(self):
        result = await validate_sql(
            (
                "WITH monthly AS ("
                "SELECT date_trunc('month', shipping_limit_date) AS month, "
                "SUM(price + freight_value) AS gmv "
                "FROM olist_order_items_dataset "
                "GROUP BY month"
                ") "
                "SELECT month, gmv FROM monthly ORDER BY gmv DESC"
            ),
            tenant_id="admin",
            allowed_tables=["olist_order_items_dataset"],
        )

        self.assertTrue(result["ok"], result["violations"])
        self.assertEqual(result["tables"], ["olist_order_items_dataset"])

    async def test_domain_validation_accepts_product_volume_with_equivalent_factor_order(self):
        result = validate_domain_sql(
            sql=(
                "SELECT product_id, product_category_name, product_length_cm, "
                "product_width_cm, product_height_cm, product_weight_g, "
                "product_length_cm * product_height_cm * product_width_cm AS volume "
                "FROM olist_products_dataset "
                "WHERE product_length_cm IS NOT NULL "
                "AND product_width_cm IS NOT NULL "
                "AND product_height_cm IS NOT NULL "
                "ORDER BY volume DESC LIMIT 25"
            ),
            constraints={
                "matched_rules": ["product_volume_detail"],
                "required_tables": ["olist_products_dataset"],
                "default_columns": [
                    "olist_products_dataset.product_id",
                    "olist_products_dataset.product_category_name",
                    "olist_products_dataset.product_length_cm",
                    "olist_products_dataset.product_width_cm",
                    "olist_products_dataset.product_height_cm",
                    "olist_products_dataset.product_weight_g",
                    "product_length_cm * product_width_cm * product_height_cm AS volume",
                ],
                "required_filters": [
                    "olist_products_dataset.product_length_cm IS NOT NULL",
                    "olist_products_dataset.product_width_cm IS NOT NULL",
                    "olist_products_dataset.product_height_cm IS NOT NULL",
                ],
                "required_order_by": ["volume DESC"],
                "required_sql_fragments": [
                    "product_length_cm * product_width_cm * product_height_cm AS volume"
                ],
            },
        )

        self.assertTrue(result["ok"], result["violations"])

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

    async def test_table_explanation_requests_insights_without_repeating_rows(self):
        llm = FakeLLM()
        result = await explain_table_result(
            question="show yesterday order records",
            sql="SELECT order_id, amount, created_at FROM orders ORDER BY created_at DESC",
            rows=[
                {"order_id": "O-1", "amount": 100, "created_at": "2024-12-30T23:45:00Z"},
                {"order_id": "O-2", "amount": 200, "created_at": "2024-12-30T22:15:00Z"},
            ],
            metrics_result={},
            llm=llm,
        )

        self.assertEqual(result["explanation"], "GMV is 100.")
        self.assertIn("row_count", llm.calls[0]["prompt"])
        self.assertIn("trend", llm.calls[0]["system"].lower())
        self.assertIn("do not list records row by row", llm.calls[0]["system"].lower())
        self.assertIn("frontend table paginates all returned rows", llm.calls[0]["system"].lower())
        self.assertIn("do not say only a subset is available", llm.calls[0]["system"].lower())

    async def test_execute_sql_reads_environment_without_runtime_dotenv_load(self):
        execute_sql_module = importlib.import_module("graph.tools.execute_sql")

        class FakeConnection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql):
                self.calls.append(("execute", sql))

            async def fetch(self, sql):
                self.calls.append(("fetch", sql))
                return [{"value": 1}]

            async def close(self):
                self.calls.append(("close",))

        conn = FakeConnection()
        validation = {
            "ok": True,
            "normalized_sql": "SELECT 1 LIMIT 1000",
            "violations": [],
            "message": "valid",
        }
        with mock.patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://example/db"},
            clear=True,
        ), mock.patch.object(
            execute_sql_module,
            "load_dotenv",
            create=True,
            side_effect=AssertionError("runtime dotenv load is blocking"),
        ), mock.patch.object(
            execute_sql_module,
            "validate_sql",
            new=mock.AsyncMock(return_value=validation),
        ), mock.patch.object(
            execute_sql_module,
            "_connect",
            new=mock.AsyncMock(return_value=conn),
        ) as connect:
            result = await execute_sql_module.execute_sql(
                "SELECT 1",
                "demo",
                allowed_tables=["orders"],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], [{"value": 1}])
        connect.assert_awaited_once_with("postgresql://example/db")
        self.assertEqual(conn.calls[-1], ("close",))

    async def test_execute_sql_applies_seller_scope_before_fetch(self):
        execute_sql_module = importlib.import_module("graph.tools.execute_sql")

        class FakeConnection:
            def __init__(self):
                self.fetch_sql = ""

            async def execute(self, sql):
                return None

            async def fetch(self, sql):
                self.fetch_sql = sql
                return [{"gmv": 100}]

            async def close(self):
                return None

        conn = FakeConnection()
        validation = {
            "ok": True,
            "normalized_sql": "SELECT SUM(price) AS gmv FROM olist_order_items_dataset LIMIT 1000",
            "violations": [],
            "message": "valid",
        }
        with mock.patch.object(
            execute_sql_module,
            "validate_sql",
            new=mock.AsyncMock(return_value=validation),
        ), mock.patch.object(
            execute_sql_module,
            "_connect",
            new=mock.AsyncMock(return_value=conn),
        ):
            result = await execute_sql_module.execute_sql(
                "SELECT SUM(price) AS gmv FROM olist_order_items_dataset",
                "seller-1",
                dsn="postgresql://example/db",
                allowed_tables=["olist_order_items_dataset"],
            )

        self.assertTrue(result["ok"])
        self.assertIn("seller_id = 'seller-1'", conn.fetch_sql)
        self.assertIn("seller_id = 'seller-1'", result["normalized_sql"])

    async def test_execute_sql_connect_uses_preloaded_asyncpg_driver(self):
        execute_sql_module = importlib.import_module("graph.tools.execute_sql")
        original_import = builtins.__import__

        def reject_runtime_dependency_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "asyncpg":
                raise AssertionError("runtime dependency import is blocking")
            return original_import(name, globals, locals, fromlist, level)

        fake_asyncpg = types.SimpleNamespace(
            connect=mock.AsyncMock(return_value="connection")
        )
        with mock.patch.object(
            execute_sql_module,
            "asyncpg",
            fake_asyncpg,
            create=True,
        ), mock.patch(
            "builtins.__import__",
            side_effect=reject_runtime_dependency_import,
        ):
            result = await execute_sql_module._connect("postgresql://example/db")

        self.assertEqual(result, "connection")
        fake_asyncpg.connect.assert_awaited_once_with(
            "postgresql://example/db",
            ssl=False,
        )


if __name__ == "__main__":
    unittest.main()
