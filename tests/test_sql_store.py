import unittest
from types import SimpleNamespace
from unittest import mock

from graph.tools.sql_store import search_metrics, search_schema


class FakeEmbeddings:
    async def embed_text(self, text):
        return [0.1, 0.2]


class SqlStoreTests(unittest.IsolatedAsyncioTestCase):
    @mock.patch("graph.tools.sql_store.search_semantic_index")
    async def test_search_metrics_applies_inclusive_score_filter(self, search):
        search.return_value = [
            SimpleNamespace(
                similarity=0.0,
                metadata={"metric_name": "gmv", "base_table": "orders"},
            )
        ]

        result = await search_metrics(
            "show gmv",
            "demo",
            FakeEmbeddings(),
            min_score=0.0,
        )

        self.assertEqual(result["metrics"][0]["metric_name"], "gmv")

    @mock.patch("graph.tools.sql_store.search_semantic_index")
    async def test_search_schema_groups_columns_by_authorized_table(self, search):
        search.return_value = [
            SimpleNamespace(
                object_type="table",
                similarity=0.8,
                metadata={"table_name": "orders", "comment": "Orders"},
            ),
            SimpleNamespace(
                object_type="column",
                similarity=0.9,
                metadata={
                    "table_name": "orders",
                    "column_name": "amount",
                    "data_type": "numeric",
                },
            ),
            SimpleNamespace(
                object_type="table",
                similarity=0.95,
                metadata={"table_name": "users", "comment": "Users"},
            ),
        ]

        result = await search_schema(
            "show gmv",
            "demo",
            FakeEmbeddings(),
            table_names=["orders"],
        )

        self.assertEqual([item["table_name"] for item in result["schema"]], ["orders"])
        self.assertEqual(result["schema"][0]["columns"][0]["column_name"], "amount")

    @mock.patch("graph.tools.sql_store.search_semantic_index")
    async def test_search_schema_uses_admin_catalog_for_seller_tenant(self, search):
        search.return_value = []

        result = await search_schema(
            "show my orders",
            "seller-1",
            FakeEmbeddings(),
        )

        self.assertEqual(result["tenant_id"], "seller-1")
        self.assertEqual(search.await_args.kwargs["tenant_id"], "admin")

    @mock.patch("graph.tools.sql_store.search_semantic_index")
    async def test_search_metrics_uses_admin_catalog_for_seller_tenant(self, search):
        search.return_value = []

        result = await search_metrics(
            "show my gmv",
            "seller-1",
            FakeEmbeddings(),
        )

        self.assertEqual(result["tenant_id"], "seller-1")
        self.assertEqual(search.await_args.kwargs["tenant_id"], "admin")


if __name__ == "__main__":
    unittest.main()
