from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from data_agent.tools import AccessGrant, CredentialLease
from data_agent.tools.connectors import (
    ConnectorError,
    ConnectorErrorCode,
    DataSourceConnector,
    SQLiteConnector,
)
from tests.support.connector_query import (
    ALLOWED_RELATIONS,
    BINDING_DIGEST,
    CONNECTION_REF,
    SCHEMA_FINGERPRINT,
    SOURCE,
    TENANT_ID,
    governed_sales_query,
)


class SQLiteConnectorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prepared = governed_sales_query("sqlite")
        cls.allowed_relations = ALLOWED_RELATIONS
        cls.connection_ref = CONNECTION_REF

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "olist.sqlite"
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE olist_order_items_dataset (
                    seller_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    freight_value REAL NOT NULL,
                    shipping_limit_date TEXT NOT NULL
                );
                CREATE TABLE olist_sellers_dataset (
                    seller_id TEXT PRIMARY KEY
                );
                INSERT INTO olist_sellers_dataset VALUES
                    ('seller-42'),
                    ('seller-other');
                INSERT INTO olist_order_items_dataset VALUES
                    ('seller-42', 10.0, 2.0, '2017-03-01'),
                    ('seller-42', 3.0, 1.0, '2017-04-01'),
                    ('seller-42', 100.0, 5.0, '2018-04-01'),
                    ('seller-other', 50.0, 2.0, '2017-03-01');
                """
            )
            connection.commit()
        finally:
            connection.close()
        self.connector = SQLiteConnector(
            self.database_path,
            allowed_relations=self.allowed_relations,
            schema_fingerprint=SCHEMA_FINGERPRINT,
            source=SOURCE,
            connection_ref=self.connection_ref,
            bundle_digest=BINDING_DIGEST,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _grant(self, **updates: object) -> AccessGrant:
        now = datetime.now(UTC)
        values = {
            "grant_id": "sqlite-grant",
            "tool_name": "query.execute",
            "tool_version": "1.0.0",
            "skill_id": "commerce.analytics",
            "bundle_digest": BINDING_DIGEST,
            "schema_fingerprint": SCHEMA_FINGERPRINT,
            "source": SOURCE,
            "read_only": True,
            "principal_user_id": "sqlite-user",
            "tenant_id": TENANT_ID,
            "admin_bypass": False,
            "allowed_relations": self.allowed_relations,
            "max_rows": 100,
            "statement_timeout_ms": 2500,
            "policy_decision_id": self.prepared.policy_decision_id,
            "logical_plan_hash": self.prepared.logical_plan_hash,
            "prepared_query_hash": self.prepared.sql_ast_hash,
            "issued_at": now,
            "expires_at": now + timedelta(seconds=30),
        }
        values.update(updates)
        return AccessGrant(**values)

    def _lease(self, **updates: object) -> CredentialLease:
        now = datetime.now(UTC)
        values = {
            "credential_id": "sqlite-lease",
            "grant_id": "sqlite-grant",
            "bundle_digest": BINDING_DIGEST,
            "source": SOURCE,
            "connection_ref": self.connection_ref,
            "capabilities": ("data.inspect", "query.execute"),
            "secret": "snapshot://tenant-a/olist/v1",
            "issued_at": now,
            "expires_at": now + timedelta(seconds=20),
        }
        values.update(updates)
        return CredentialLease(**values)

    async def test_executes_compiler_produced_sqlite_query_readonly(self) -> None:
        self.assertIsInstance(self.connector, DataSourceConnector)
        self.assertEqual(self.prepared.dialect, "sqlite")

        result = await self.connector.execute_readonly(
            self.prepared,
            self._grant(),
            self._lease(),
        )

        self.assertEqual(result.columns, ("seller_id", "gmv"))
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].values[0], "seller-42")
        self.assertAlmostEqual(float(result.rows[0].values[1]), 16.0)

        verification = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                verification.execute(
                    "SELECT COUNT(*) FROM olist_order_items_dataset"
                ).fetchone()[0],
                4,
            )
        finally:
            verification.close()

    async def test_explain_and_catalog_introspection_use_safe_interfaces(self) -> None:
        explain = await self.connector.explain(
            self.prepared,
            self._grant(),
            self._lease(),
        )
        self.assertIn("SEARCH", explain.plan_text.upper())

        catalog = await self.connector.introspect_schema(
            self._grant(
                tool_name="data.inspect",
                logical_plan_hash=None,
                prepared_query_hash=None,
            ),
            self._lease(),
            relations=(
                "public.olist_order_items_dataset",
                "public.olist_sellers_dataset",
            ),
        )
        self.assertEqual(len(catalog.relations), 2)
        self.assertEqual(
            catalog.relations[0].columns[0].name,
            "seller_id",
        )

    async def test_rejects_postgres_query_before_opening_database(self) -> None:
        postgres_prepared = governed_sales_query("postgres")
        with self.assertRaises(ConnectorError) as captured:
            await self.connector.execute_readonly(
                postgres_prepared,
                self._grant(
                    prepared_query_hash=postgres_prepared.sql_ast_hash,
                ),
                self._lease(),
            )
        self.assertEqual(
            captured.exception.code,
            ConnectorErrorCode.GRANT_MISMATCH,
        )


if __name__ == "__main__":
    unittest.main()
