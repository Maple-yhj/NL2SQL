from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
from openpyxl import Workbook

from data_agent.datasources import (
    FileSnapshotError,
    FileSnapshotErrorCode,
    FileSnapshotImporter,
)
from data_agent.runtime import (
    load_bundle_manifest,
    load_domain_pack,
    load_enterprise_binding,
)
from data_agent.runtime.binding import BindingCompiler
from data_agent.runtime.models import PrincipalContext
from data_agent.skills import logical_plan_from_eval_case
from data_agent.tools import AccessGrant, CredentialLease
from data_agent.tools.connectors import DuckDBConnector


ROOT = Path(__file__).resolve().parents[2]


class FileDatasourceIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        cls.principal = PrincipalContext(
            tenant_id="seller-42",
            user_id="file-user",
            roles=("seller",),
        )
        case = next(
            item
            for item in cls.domain.spec.evals
            if item.id == "commerce.metric_002"
        )
        compiler = BindingCompiler(
            cls.domain,
            cls.enterprise,
            cls.bundle,
            dialect="duckdb",
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
        self.root = Path(self.temporary_directory.name)
        self.staging = self.root / "staging"
        self.snapshots = self.root / "snapshots"
        self.staging.mkdir()
        self.snapshots.mkdir()

    def tearDown(self) -> None:
        for path in self.snapshots.glob("*.duckdb"):
            path.chmod(0o600)
        self.temporary_directory.cleanup()

    def _write_csv(
        self,
        name: str,
        rows: list[tuple[object, ...]],
    ) -> Path:
        path = self.staging / name
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerows(rows)
        return path

    def _grant(self, **updates: object) -> AccessGrant:
        now = datetime.now(UTC)
        values = {
            "grant_id": "duckdb-grant",
            "tool_name": "query.execute",
            "tool_version": "1.0.0",
            "skill_id": "commerce.analytics",
            "bundle_digest": self.bundle.digest,
            "schema_fingerprint": self.prepared.schema_fingerprint,
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
            "credential_id": "duckdb-lease",
            "grant_id": "duckdb-grant",
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

    async def test_csv_group_imports_and_executes_governed_duckdb_query(self) -> None:
        order_items = self._write_csv(
            "olist_order_items_dataset.csv",
            [
                (
                    "seller_id",
                    "price",
                    "freight_value",
                    "shipping_limit_date",
                ),
                ("seller-42", 10.0, 2.0, "2017-03-01"),
                ("seller-42", 3.0, 1.0, "2017-04-01"),
                ("seller-42", 100.0, 5.0, "2018-04-01"),
                ("seller-other", 50.0, 2.0, "2017-03-01"),
            ],
        )
        sellers = self._write_csv(
            "olist_sellers_dataset.csv",
            [
                ("seller_id",),
                ("seller-42",),
                ("seller-other",),
            ],
        )
        importer = FileSnapshotImporter(staging_root=self.staging)
        snapshot = importer.import_files(
            (order_items, sellers),
            output_directory=self.snapshots,
            source_id="olist-files",
            version=1,
        )
        self.assertEqual(len(snapshot.catalog.relations), 2)
        self.assertEqual(
            {item.relation for item in snapshot.relations},
            {
                "public.olist_order_items_dataset",
                "public.olist_sellers_dataset",
            },
        )

        connector = DuckDBConnector(
            snapshot.database_path,
            allowed_relations=self.allowed_relations,
            schema_fingerprint=self.bundle.schema_fingerprint,
            source="sales",
            connection_ref=self.connection_ref,
            bundle_digest=self.bundle.digest,
        )
        result = await connector.execute_readonly(
            self.prepared,
            self._grant(),
            self._lease(),
        )
        self.assertEqual(result.columns, ("seller_id", "gmv"))
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].values[0], "seller-42")
        self.assertAlmostEqual(float(result.rows[0].values[1]), 16.0)

        safe_connection = connector._connect()
        try:
            with self.assertRaises(duckdb.Error):
                safe_connection.execute(
                    "SELECT * FROM read_csv('/etc/passwd')"
                ).fetchall()
        finally:
            safe_connection.close()

    async def test_xlsx_import_uses_values_without_active_content(self) -> None:
        workbook_path = self.staging / "sales.xlsx"
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Monthly Sales"
        worksheet.append(("month", "amount"))
        worksheet.append(("2026-01", 10))
        worksheet.append(("2026-02", 12))
        workbook.save(workbook_path)
        workbook.close()

        snapshot = FileSnapshotImporter(
            staging_root=self.staging
        ).import_files(
            (workbook_path,),
            output_directory=self.snapshots,
            source_id="sales",
            version=1,
        )

        self.assertEqual(len(snapshot.relations), 1)
        self.assertEqual(snapshot.relations[0].row_count, 2)
        self.assertEqual(
            tuple(
                column.name
                for column in snapshot.catalog.relations[0].columns
            ),
            ("month", "amount"),
        )

    async def test_rejects_legacy_excel_and_outside_staging_paths(self) -> None:
        legacy = self._write_csv("legacy.xls", [("a",), (1,)])
        importer = FileSnapshotImporter(staging_root=self.staging)
        with self.assertRaises(FileSnapshotError) as captured:
            importer.import_files(
                (legacy,),
                output_directory=self.snapshots,
                source_id="legacy",
                version=1,
            )
        self.assertEqual(
            captured.exception.code,
            FileSnapshotErrorCode.UNSUPPORTED_FORMAT,
        )

        outside = self.root / "outside.csv"
        outside.write_text("a\n1\n", encoding="utf-8")
        with self.assertRaises(FileSnapshotError) as outside_captured:
            importer.import_files(
                (outside,),
                output_directory=self.snapshots,
                source_id="outside",
                version=1,
            )
        self.assertEqual(
            outside_captured.exception.code,
            FileSnapshotErrorCode.FILE_NOT_FOUND,
        )


if __name__ == "__main__":
    unittest.main()
