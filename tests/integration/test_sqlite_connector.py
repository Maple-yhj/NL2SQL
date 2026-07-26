from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from data_agent.runtime import (
    load_bundle_manifest,
    load_domain_pack,
    load_enterprise_binding,
)
from data_agent.runtime.binding import BindingCompiler
from data_agent.runtime.models import PrincipalContext
from data_agent.skills import logical_plan_from_eval_case
from data_agent.tools import AccessGrant, CredentialLease
from data_agent.tools.connectors import (
    ConnectorError,
    ConnectorErrorCode,
    DataSourceConnector,
    SQLiteConnector,
)


ROOT = Path(__file__).resolve().parents[2]


class SQLiteConnectorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain = load_domain_pack(ROOT / "packs" / "domains" / "commerce")
        cls.enterprise = load_enterprise_binding(
            ROOT / "packs" / "enterprises" / "olist"
        )
        cls.bundle = load_bundle_manifest(
            ROOT / "generated" / "bundles" / "olist-local.json",
            pack_lock=ROOT / "packs" / "enterprises" / "olist" / "pack.lock",
            schema_catalog=ROOT / "schema_catalog.json",
        )
        case = next(
            item
            for item in cls.domain.spec.evals
            if item.id == "commerce.metric_002"
        )
        cls.principal = PrincipalContext(
            tenant_id="seller-42",
            user_id="sqlite-user",
            roles=("seller",),
        )
        compiler = BindingCompiler(
            cls.domain,
            cls.enterprise,
            cls.bundle,
            dialect="sqlite",
        )
        cls.prepared = compiler.compile(
            compiler.bind(
                logical_plan_from_eval_case(case, cls.domain),
                cls.principal,
            ),
            cls.principal,
        )
        cls.allowed_relations = tuple(
            cls.enterprise.spec.policies.relation_allowlist
        )
        cls.connection_ref = cls.enterprise.spec.sources["sales"].connection_ref

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
            schema_fingerprint=self.bundle.schema_fingerprint,
            source="sales",
            connection_ref=self.connection_ref,
            bundle_digest=self.bundle.digest,
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
            "bundle_digest": self.bundle.digest,
            "schema_fingerprint": self.bundle.schema_fingerprint,
            "source": "sales",
            "read_only": True,
            "principal_user_id": self.principal.user_id,
            "tenant_id": self.principal.tenant_id,
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
            "bundle_digest": self.bundle.digest,
            "source": "sales",
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
        postgres_compiler = BindingCompiler(
            self.domain,
            self.enterprise,
            self.bundle,
        )
        postgres_prepared = postgres_compiler.compile(
            postgres_compiler.bind(
                self.prepared.logical_plan,
                self.principal,
            ),
            self.principal,
        )
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
