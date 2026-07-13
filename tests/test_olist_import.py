import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import import_olist_dataset
from scripts.import_olist_dataset import (
    OLIST_TABLES,
    build_copy_sql,
    build_create_table_sql,
    create_indexes_sql,
    parse_args,
    prepare_schema,
)


ROOT = Path(__file__).resolve().parents[1]


class RecordingCursor:
    def __init__(self):
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str) -> None:
        self.statements.append(sql)


class RecordingConnection:
    def __init__(self):
        self.cursor_instance = RecordingCursor()

    def cursor(self):
        return self.cursor_instance


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

    def test_geolocation_uses_generated_surrogate_primary_key(self):
        spec = OLIST_TABLES["olist_geolocation_dataset"]
        create_sql = build_create_table_sql(spec)
        copy_sql = build_copy_sql(spec)

        self.assertIn("geolocation_row_id BIGSERIAL", create_sql)
        self.assertIn("PRIMARY KEY (geolocation_row_id)", create_sql)
        self.assertNotIn("geolocation_row_id", copy_sql)

    def test_table_primary_keys_match_the_governed_schema_catalog(self):
        catalog = json.loads((ROOT / "schema_catalog.json").read_text(encoding="utf-8"))
        expected = {
            table["table"]: tuple(table["unique_keys"][0])
            for table in catalog
        }

        self.assertEqual(
            {name: spec.primary_key for name, spec in OLIST_TABLES.items()},
            expected,
        )
        for name, spec in OLIST_TABLES.items():
            primary_key = ", ".join(expected[name])
            self.assertIn(
                f"PRIMARY KEY ({primary_key})",
                build_create_table_sql(spec),
            )

    def test_prepare_schema_creates_only_nine_olist_tables_and_indexes(self):
        connection = RecordingConnection()

        prepare_schema(connection)

        statements = connection.cursor_instance.statements
        create_tables = [
            statement
            for statement in statements
            if statement.startswith("CREATE TABLE IF NOT EXISTS")
        ]
        self.assertEqual(len(create_tables), 9)
        self.assertEqual(
            statements[9:],
            create_indexes_sql(),
        )
        serialized = "\n".join(statements).casefold()
        for forbidden in ("vector", "semantic_index", "metrics_registry", "embedding"):
            self.assertNotIn(forbidden, serialized)

    def test_cli_has_no_embedding_dimension_option(self):
        arguments = parse_args(["--schema-only"])

        self.assertTrue(arguments.schema_only)
        self.assertFalse(hasattr(arguments, "embedding_dim"))

    def test_main_rejects_legacy_postgres_dsn_without_connecting(self):
        connect = MagicMock()
        fake_psycopg2 = MagicMock(connect=connect)

        with (
            patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://legacy"}, clear=True),
            patch.object(import_olist_dataset, "load_dotenv"),
            patch.object(sys, "argv", ["import_olist_dataset.py", "--schema-only"]),
            patch.dict(sys.modules, {"psycopg2": fake_psycopg2}),
        ):
            error = None
            try:
                import_olist_dataset.main()
            except ValueError as exc:
                error = str(exc)

        self.assertEqual(
            (error, connect.call_count),
            ("Missing DATABASE_URL.", 0),
        )


if __name__ == "__main__":
    unittest.main()
