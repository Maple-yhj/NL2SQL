import unittest

from graph.tools.prepare_sql import prepare_sql


class PrepareSqlTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_sql_returns_tenant_scoped_executable_sql(self):
        result = await prepare_sql(
            sql="SELECT SUM(price) AS gmv FROM olist_order_items_dataset",
            tenant_id="seller-1",
            allowed_tables=["olist_order_items_dataset"],
            max_limit=1000,
        )

        self.assertTrue(result["ok"], result["violations"])
        self.assertTrue(result["valid"], result["violations"])
        self.assertIn("seller_id = 'seller-1'", result["executable_sql"])
        self.assertEqual(result["normalized_sql"], result["executable_sql"])
        self.assertEqual(result["logical_sql"], "SELECT SUM(price) AS gmv FROM olist_order_items_dataset LIMIT 1000")
        self.assertEqual(result["validation"]["tenant_id"], "seller-1")

    async def test_prepare_sql_merges_domain_violations(self):
        result = await prepare_sql(
            sql="SELECT COUNT(*) AS customer_count FROM olist_order_items_dataset",
            tenant_id="admin",
            allowed_tables=["olist_order_items_dataset", "olist_customers_dataset"],
            constraints={
                "matched_rules": ["customer_count_by_location"],
                "required_tables": ["olist_customers_dataset"],
                "forbidden_tables": ["olist_order_items_dataset"],
            },
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["valid"])
        codes = [item["code"] for item in result["violations"]]
        self.assertIn("domain_missing_table", codes)
        self.assertIn("domain_forbidden_table", codes)
        self.assertEqual(result["executable_sql"], "")


if __name__ == "__main__":
    unittest.main()
