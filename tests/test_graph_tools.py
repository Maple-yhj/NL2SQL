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

    async def test_explanation_removes_false_preview_limit_claims(self):
        class PreviewLimitLLM(FakeLLM):
            async def complete(self, prompt, system="", max_output_tokens=2048):
                self.calls.append({"prompt": prompt, "system": system})
                return (
                    "查询返回的是按客户数降序排列的前20个州-城市组合，"
                    "以下为前10个城市及其客户数（由于只显示了10行，其余10个未列出）。"
                    "如需完整的前20个城市列表，请执行原查询获取全部20行数据。"
                    "圣保罗客户数明显领先。"
                )

        llm = PreviewLimitLLM()
        rows = [
            {"customer_state": "SP", "customer_city": f"city-{index}", "customer_count": index}
            for index in range(20)
        ]

        result = await explain_result(
            question="top 20 customer state-city combinations by customer count",
            sql=(
                "SELECT customer_state, customer_city, COUNT(*) AS customer_count "
                "FROM customers GROUP BY customer_state, customer_city "
                "ORDER BY customer_count DESC LIMIT 20"
            ),
            rows=rows,
            metrics_result={},
            llm=llm,
        )

        self.assertNotIn("前10", result["explanation"])
        self.assertNotIn("只显示", result["explanation"])
        self.assertNotIn("未列出", result["explanation"])
        self.assertNotIn("执行原查询", result["explanation"])
        self.assertIn("圣保罗客户数明显领先", result["explanation"])
        self.assertIn("row_count", llm.calls[0]["prompt"])

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

    async def test_table_explanation_removes_false_preview_limit_claims(self):
        class PreviewLimitLLM(FakeLLM):
            async def complete(self, prompt, system="", max_output_tokens=2048):
                self.calls.append({"prompt": prompt, "system": system})
                return (
                    "以下是客户数排名前 10 的城市"
                    "（因数据只展示了前 10 行，实际应返回前 20 个城市）。"
                    "圣保罗客户数明显领先。"
                )

        llm = PreviewLimitLLM()
        rows = [
            {"customer_state": "SP", "customer_city": f"city-{index}", "customer_count": index}
            for index in range(20)
        ]

        result = await explain_table_result(
            question="客户最多的州和城市分别有哪些？返回前 20 个城市",
            sql="SELECT customer_state, customer_city, customer_count FROM customers LIMIT 20",
            rows=rows,
            metrics_result={},
            llm=llm,
        )

        self.assertNotIn("前 10", result["explanation"])
        self.assertNotIn("只展示", result["explanation"])
        self.assertNotIn("实际应返回", result["explanation"])
        self.assertIn("圣保罗客户数明显领先", result["explanation"])
        self.assertIn("row_count is the complete number", llm.calls[0]["system"].lower())
        self.assertIn("preview_rows are not a display limit", llm.calls[0]["system"].lower())

    async def test_table_explanation_removes_row_by_row_bullet_descriptions(self):
        class RowListLLM(FakeLLM):
            async def complete(self, prompt, system="", max_output_tokens=2048):
                self.calls.append({"prompt": prompt, "system": system})
                return "\n".join(
                    [
                        "\u6839\u636e\u67e5\u8be2\u7ed3\u679c\uff0c\u6309\u5356\u5bb6\u5dde\u7edf\u8ba1\u7684\u5e73\u5747\u4ece\u8d2d\u4e70\u5230\u4ea4\u7ed9\u627f\u8fd0\u5546\u7684\u65f6\u95f4\u5982\u4e0b\uff1a",
                        "",
                        "- AC: \u65e0\u6570\u636e (null)",
                        "- AM: 3\u592910\u5c0f\u65f619\u520612\u79d2",
                        "- BA: 3\u59299\u5c0f\u65f630\u520616\u79d2",
                        "\uff08\u5171\u8fd4\u56de23\u6761\u8bb0\u5f55\uff0c\u4ec5\u5217\u51fa\u63d0\u4f9b\u768410\u6761\u3002\u5176\u4f59\u5dde\u7684\u5e73\u5747\u65f6\u957f\u672a\u5728\u7ed3\u679c\u4e2d\u5c55\u793a\u3002\uff09",
                    ]
                )

        rows = [
            {"seller_state": "AC", "avg_carrier_handoff_duration": None},
            {"seller_state": "AM", "avg_carrier_handoff_duration": "P3DT10H19M12.333333S"},
            {"seller_state": "BA", "avg_carrier_handoff_duration": "P3DT9H30M16.553259S"},
        ]

        result = await explain_table_result(
            question="\u6309\u5356\u5bb6\u5dde\u7edf\u8ba1\u5e73\u5747\u4ece\u8d2d\u4e70\u5230\u4ea4\u7ed9\u627f\u8fd0\u5546\u7684\u65f6\u95f4",
            sql=(
                "SELECT seller_state, AVG(order_delivered_carrier_date - order_purchase_timestamp) "
                "AS avg_carrier_handoff_duration FROM orders GROUP BY seller_state"
            ),
            rows=rows,
            metrics_result={},
            llm=RowListLLM(),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["explanation"], "")

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
