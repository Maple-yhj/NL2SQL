from __future__ import annotations

import tempfile
import unittest
import sqlite3
from functools import partial
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from api.app import create_app
from api.datasource_service import DataSourceService
from tests.test_api_runtime_contract import (
    TEST_JWT_SECRET,
    _RecordingComposition,
    _auth_headers,
)


class _PostgresConnection:
    async def fetch(self, query: str):
        self.query = query
        return [
            {
                "table_schema": "analytics",
                "table_name": "orders",
                "column_name": "order_id",
                "data_type": "text",
                "is_nullable": "NO",
            },
            {
                "table_schema": "analytics",
                "table_name": "orders",
                "column_name": "amount",
                "data_type": "numeric",
                "is_nullable": "YES",
            },
        ]


class _PostgresAcquire:
    def __init__(self, connection: _PostgresConnection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _PostgresPool:
    def __init__(self) -> None:
        self.connection = _PostgresConnection()
        self.closed = False

    def acquire(self):
        return _PostgresAcquire(self.connection)

    async def close(self) -> None:
        self.closed = True


class ApiDatasourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.composition = _RecordingComposition()
        self.environment = mock.patch.dict(
            "os.environ",
            {"JWT_SECRET_KEY": TEST_JWT_SECRET},
        )
        self.environment.start()
        app = create_app(
            runtime_factory=mock.AsyncMock(return_value=self.composition),
            data_source_service=DataSourceService(
                state_root=self.temporary_directory.name
            ),
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.headers = _auth_headers()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_file_upload_catalog_and_binding_lifecycle(self) -> None:
        uploaded = self.client.post(
            "/api/data-sources/files",
            headers=self.headers,
            data={
                "name": "Orders",
                "source_id": "orders-file",
            },
            files={
                "files": (
                    "orders.csv",
                    b"order_id,amount\nA-1,10.5\nA-2,20.0\n",
                    "text/csv",
                )
            },
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        source = uploaded.json()
        self.assertEqual(source["source_id"], "orders-file")
        self.assertEqual(source["kind"], "csv")
        self.assertEqual(source["status"], "ready")
        self.assertNotIn("location_ref", source)

        listed = self.client.get(
            "/api/data-sources",
            headers=self.headers,
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item["source_id"] for item in listed.json()["items"]],
            ["orders-file"],
        )
        isolated = self.client.get(
            "/api/data-sources",
            headers=_auth_headers(tenant_id="other-tenant"),
        )
        self.assertEqual(isolated.json(), {"items": []})

        catalog = self.client.get(
            "/api/data-sources/orders-file/catalog",
            headers=self.headers,
        )
        self.assertEqual(catalog.status_code, 200)
        relation = catalog.json()["catalog"]["relations"][0]
        self.assertEqual(relation["relation"], "public.orders")
        self.assertEqual(
            [column["name"] for column in relation["columns"]],
            ["order_id", "amount"],
        )

        binding = self.client.post(
            "/api/data-sources/orders-file/bindings",
            headers=self.headers,
            json={
                "domain_id": "dataset.orders",
                "mappings": [
                    {
                        "logical_ref": "dataset.Order.order_id",
                        "physical_relation": "public.orders",
                        "physical_column": "order_id",
                    },
                    {
                        "logical_ref": "dataset.Order.amount",
                        "physical_relation": "public.orders",
                        "physical_column": "amount",
                    },
                ],
            },
        )
        self.assertEqual(binding.status_code, 201, binding.text)
        self.assertEqual(binding.json()["status"], "draft")

        activated = self.client.post(
            (
                "/api/data-sources/orders-file/bindings/"
                f"{binding.json()['binding_id']}/activate"
            ),
            headers=self.headers,
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(activated.json()["status"], "active")

    def test_postgres_registration_requires_secret_reference_not_password(self) -> None:
        response = self.client.post(
            "/api/data-sources/postgres",
            headers=self.headers,
            json={
                "source_id": "warehouse",
                "name": "Warehouse",
                "credential_ref": "secret://tenant/warehouse",
                "host": "db.example.test",
                "port": 5432,
                "database": "analytics",
                "ssl_mode": "require",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "registered")
        self.assertNotIn("credential_ref", response.json())

        rejected = self.client.post(
            "/api/data-sources/postgres",
            headers=self.headers,
            json={
                "source_id": "unsafe",
                "name": "Unsafe",
                "credential_ref": "secret://tenant/unsafe",
                "host": "db.example.test",
                "database": "analytics",
                "password": "must-not-be-accepted",
            },
        )
        self.assertEqual(rejected.status_code, 422)

    def test_sqlite_upload_publishes_readonly_catalog(self) -> None:
        database_path = Path(self.temporary_directory.name) / "source.sqlite"
        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                "CREATE TABLE customers (customer_id TEXT, city TEXT)"
            )
            connection.execute(
                "INSERT INTO customers VALUES ('C-1', 'Shanghai')"
            )
            connection.commit()
        finally:
            connection.close()

        response = self.client.post(
            "/api/data-sources/sqlite",
            headers=self.headers,
            data={
                "name": "Customers",
                "source_id": "customers-sqlite",
            },
            files={
                "file": (
                    "customers.sqlite",
                    database_path.read_bytes(),
                    "application/vnd.sqlite3",
                )
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["kind"], "sqlite")
        catalog = self.client.get(
            "/api/data-sources/customers-sqlite/catalog",
            headers=self.headers,
        )
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(
            catalog.json()["catalog"]["relations"][0]["relation"],
            "main.customers",
        )

    def test_file_source_and_connector_survive_service_restart(self) -> None:
        uploaded = self.client.post(
            "/api/data-sources/files",
            headers=self.headers,
            data={"name": "Restartable", "source_id": "restartable"},
            files={
                "files": (
                    "restartable.csv",
                    b"id,value\n1,ready\n",
                    "text/csv",
                )
            },
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)

        restarted = DataSourceService(
            state_root=self.temporary_directory.name,
        )
        sources = self.client.portal.call(
            partial(restarted.list_sources, tenant_id="tenant-from-token"),
        )
        self.assertEqual([source.source_id for source in sources], ["restartable"])
        connector = self.client.portal.call(
            partial(
                restarted.get_connector,
                tenant_id="tenant-from-token",
                source_id="restartable",
            ),
        )
        self.assertEqual(connector.capabilities().dialect, "duckdb")

    def test_postgres_catalog_is_lazily_snapshotted_from_a_secret_ref(self) -> None:
        pools: list[_PostgresPool] = []
        resolved_refs: list[str] = []
        seen_dsns: list[str] = []

        def resolve_secret(reference: str) -> str:
            resolved_refs.append(reference)
            return "postgresql://resolved-at-runtime"

        async def create_pool(dsn: str):
            seen_dsns.append(dsn)
            pool = _PostgresPool()
            pools.append(pool)
            return pool

        service = DataSourceService(
            state_root=self.temporary_directory.name,
            secret_resolver=resolve_secret,
            postgres_pool_factory=create_pool,
        )
        source = self.client.portal.call(
            partial(
                service.register_postgres,
                tenant_id="tenant-from-token",
                source_id="warehouse-lazy",
                name="Warehouse",
                credential_ref="secret://tenant/warehouse",
                options={"host": "metadata-only", "database": "analytics"},
            )
        )
        self.assertEqual(source.status.value, "registered")

        snapshot = self.client.portal.call(
            partial(
                service.get_snapshot,
                tenant_id="tenant-from-token",
                source_id="warehouse-lazy",
            )
        )
        self.assertEqual(
            [item.relation for item in snapshot.catalog.relations],
            ["analytics.orders"],
        )
        connector = self.client.portal.call(
            partial(
                service.get_connector,
                tenant_id="tenant-from-token",
                source_id="warehouse-lazy",
            )
        )
        self.assertEqual(connector.capabilities().dialect, "postgres")
        self.assertEqual(resolved_refs, ["secret://tenant/warehouse"])
        self.assertEqual(seen_dsns, ["postgresql://resolved-at-runtime"])
        self.client.portal.call(service.close)
        self.assertTrue(pools[0].closed)


if __name__ == "__main__":
    unittest.main()
