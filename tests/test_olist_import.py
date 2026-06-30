import unittest

from scripts.import_olist_dataset import (
    OLIST_TABLES,
    build_copy_sql,
    build_create_table_sql,
)


class OlistImportTests(unittest.TestCase):
    def test_order_items_table_uses_seller_id_without_tenant_id(self):
        spec = OLIST_TABLES["olist_order_items_dataset"]
        column_names = [column.name for column in spec.columns]

        self.assertIn("seller_id", column_names)
        self.assertNotIn("tenant_id", column_names)

    def test_create_table_sql_preserves_native_olist_columns(self):
        sql = build_create_table_sql(OLIST_TABLES["olist_orders_dataset"])

        self.assertIn("CREATE TABLE IF NOT EXISTS olist_orders_dataset", sql)
        self.assertIn("order_id TEXT", sql)
        self.assertIn("order_purchase_timestamp TIMESTAMP", sql)
        self.assertNotIn("tenant_id", sql)

    def test_copy_sql_uses_csv_header(self):
        sql = build_copy_sql(OLIST_TABLES["olist_sellers_dataset"])

        self.assertEqual(
            sql,
            "COPY olist_sellers_dataset (seller_id, seller_zip_code_prefix, seller_city, seller_state) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)",
        )


if __name__ == "__main__":
    unittest.main()
