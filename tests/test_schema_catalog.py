import unittest

from catalog.schema_catalog import DEFAULT_TABLES


class SchemaCatalogTests(unittest.TestCase):
    def test_default_tables_target_olist_dataset(self):
        self.assertIn("olist_order_items_dataset", DEFAULT_TABLES)
        self.assertIn("olist_sellers_dataset", DEFAULT_TABLES)
        self.assertNotIn("orders", DEFAULT_TABLES)


if __name__ == "__main__":
    unittest.main()
