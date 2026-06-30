import unittest

from graph.tools.tenant_scope import apply_tenant_scope


class TenantScopeTests(unittest.TestCase):
    def test_admin_sql_is_not_rewritten(self):
        sql = "SELECT COUNT(*) AS order_count FROM olist_orders_dataset"

        self.assertEqual(apply_tenant_scope(sql, tenant_id="admin"), sql)

    def test_seller_scope_filters_order_items_by_seller_id(self):
        rewritten = apply_tenant_scope(
            "SELECT SUM(price) AS gmv FROM olist_order_items_dataset WHERE price > 0",
            tenant_id="seller-1",
        )

        self.assertIn("olist_order_items_dataset", rewritten)
        self.assertIn("seller_id = 'seller-1'", rewritten)
        self.assertIn("price > 0", rewritten)

    def test_seller_scope_filters_orders_through_order_items(self):
        rewritten = apply_tenant_scope(
            "SELECT COUNT(*) AS order_count FROM olist_orders_dataset",
            tenant_id="seller-1",
        )

        self.assertIn("olist_orders_dataset", rewritten)
        self.assertIn("order_id IN", rewritten)
        self.assertIn("olist_order_items_dataset", rewritten)
        self.assertIn("seller_id = 'seller-1'", rewritten)

    def test_seller_scope_escapes_tenant_literal(self):
        rewritten = apply_tenant_scope(
            "SELECT COUNT(*) FROM olist_sellers_dataset",
            tenant_id="seller'1",
        )

        self.assertIn("seller_id = 'seller''1'", rewritten)


if __name__ == "__main__":
    unittest.main()
