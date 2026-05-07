import unittest

from engine.metrics import Metric
from rag.documents import build_metric_document,build_schema_documents


class RagDocumentTests(unittest.TestCase):
    def test_build_metric_document_returns_vector_index_document(self):
        metric = Metric(
            id=1,
            tenant_id="demo",
            name="gmv",
            display_name="GMV",
            business_def="Paid gross merchandise value.",
            sql_expr="sum(orders.amount)",
            base_table="orders",
            time_column="paid_at",
            activate=True,
            dimensions=("region", "paid_date", "product_id", "user_id"),
            join_tables=(),
            filters=("status = 'paid'",),
            forbidden=(),
            synonyms=("sales", "GMV"),
        )

        doc = build_metric_document(metric)

        self.assertEqual(doc.tenant_id, "demo")
        self.assertEqual(doc.object_type, "metric")
        self.assertEqual(doc.object_key, "metric:gmv")
        self.assertEqual(doc.source_table, "metrics_registry")
        self.assertEqual(doc.source_id, 1)
        self.assertIn("Metric: gmv", doc.content)
        self.assertIn("Business definition: Paid gross merchandise value.", doc.content)
        self.assertEqual(
            doc.metadata,
            {
                "metric_name": "gmv",
                "display_name": "GMV",
                "business_def": "Paid gross merchandise value.",
                "sql_expr": "sum(orders.amount)",
                "base_table": "orders",
                "join_tables": [],
                "time_column": "paid_at",
                "dimensions": ["region", "paid_date", "product_id", "user_id"],
                "filters": ["status = 'paid'"],
                "forbidden": [],
                "synonyms": ["sales", "GMV"],
            },
        )

    def test_content_hash_is_stable_for_same_document_content(self):
        metric = Metric(
            id=1,
            tenant_id="demo",
            name="gmv",
            display_name="GMV",
            business_def="Paid gross merchandise value.",
            sql_expr="sum(orders.amount)",
            base_table="orders",
            time_column="paid_at",
            activate=True,
            dimensions=("region",),
            join_tables=(),
            filters=(),
            forbidden=(),
            synonyms=(),
        )

        first = build_metric_document(metric)
        second = build_metric_document(metric)

        self.assertEqual(first.content_hash(), second.content_hash())

def test_build_schema_documents_creates_table_and_column_documents(self):
    catalog = [
        {
            "table": "orders",
            "comment": "Order fact table",
            "columns": [
                {
                    "name": "amount",
                    "type": "numeric",
                    "nullable": False,
                    "default": None,
                    "comment": "Paid order amount",
                    "sample_values": ["99.00", "128.50"],
                }
            ],
        }
    ]

    docs = build_schema_documents(catalog, tenant_id="demo")

    self.assertEqual(len(docs), 2)
    self.assertEqual(docs[0].object_type, "table")
    self.assertEqual(docs[0].object_key, "table:orders")
    self.assertEqual(docs[1].object_type, "column")
    self.assertEqual(docs[1].object_key, "column:orders.amount")
    self.assertIn("Order fact table", docs[0].content)
    self.assertIn("Paid order amount", docs[1].content)


if __name__ == "__main__":
    unittest.main()
