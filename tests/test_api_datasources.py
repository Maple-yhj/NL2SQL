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
        graph = self.client.get(
            "/api/data-sources/orders-file/relationship-graphs/draft",
            headers=self.headers,
        )
        self.assertEqual(graph.status_code, 200, graph.text)
        denied_graph = self.client.get(
            "/api/data-sources/orders-file/relationship-graphs/draft",
            headers=_auth_headers(tenant_id="other-tenant"),
        )
        self.assertEqual(denied_graph.status_code, 404)
        run_id = source["relationship_discovery"]["run_id"]
        denied_run = self.client.get(
            f"/api/data-sources/orders-file/relationship-recommendations/{run_id}",
            headers=_auth_headers(tenant_id="other-tenant"),
        )
        self.assertEqual(denied_run.status_code, 404)

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

        snapshot_root = (
            Path(self.temporary_directory.name)
            / "snapshots"
            / "tenant-from-token"
            / "orders-file"
        )
        self.assertTrue(snapshot_root.exists())
        isolated_delete = self.client.delete(
            "/api/data-sources/orders-file",
            headers=_auth_headers(tenant_id="other-tenant"),
        )
        self.assertEqual(isolated_delete.status_code, 404)

        deleted = self.client.delete(
            "/api/data-sources/orders-file",
            headers=self.headers,
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(
            deleted.json(),
            {"source_id": "orders-file", "deleted": True},
        )
        self.assertFalse(snapshot_root.exists())
        self.assertEqual(
            self.client.get(
                "/api/data-sources",
                headers=self.headers,
            ).json(),
            {"items": []},
        )
        self.assertEqual(
            self.client.get(
                "/api/data-sources/orders-file/catalog",
                headers=self.headers,
            ).status_code,
            404,
        )

    def test_multi_file_binding_accepts_an_explicit_join_graph(self) -> None:
        uploaded = self.client.post(
            "/api/data-sources/files",
            headers=self.headers,
            data={
                "name": "Regional sales",
                "source_id": "regional-sales",
            },
            files=[
                (
                    "files",
                    (
                        "customers.csv",
                        b"customer_id,region\nC-1,East\nC-2,South\n",
                        "text/csv",
                    ),
                ),
                (
                    "files",
                    (
                        "orders.csv",
                        b"order_id,customer_id,amount\nO-1,C-1,10\nO-2,C-2,20\n",
                        "text/csv",
                    ),
                ),
            ],
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)

        binding = self.client.post(
            "/api/data-sources/regional-sales/bindings",
            headers=self.headers,
            json={
                "domain_id": "dataset.regional-sales",
                "primary_relation": "public.customers",
                "mappings": [
                    {
                        "logical_ref": "dataset.Customers.region",
                        "physical_relation": "public.customers",
                        "physical_column": "region",
                    },
                    {
                        "logical_ref": "dataset.Orders.amount",
                        "physical_relation": "public.orders",
                        "physical_column": "amount",
                    },
                ],
                "relationships": [
                    {
                        "relationship_id": "customers_orders",
                        "left_relation": "public.customers",
                        "left_column": "customer_id",
                        "right_relation": "public.orders",
                        "right_column": "customer_id",
                        "join_type": "inner",
                    }
                ],
            },
        )
        self.assertEqual(binding.status_code, 201, binding.text)
        self.assertEqual(
            binding.json()["primary_relation"],
            "public.customers",
        )
        self.assertEqual(
            binding.json()["relationships"][0]["relationship_id"],
            "customers_orders",
        )

        activated = self.client.post(
            (
                "/api/data-sources/regional-sales/bindings/"
                f"{binding.json()['binding_id']}/activate"
            ),
            headers=self.headers,
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(activated.json()["status"], "active")

    def test_multi_file_upload_error_names_the_invalid_file_and_reason(self) -> None:
        response = self.client.post(
            "/api/data-sources/files",
            headers=self.headers,
            data={"name": "Invalid Olist", "source_id": "invalid-olist"},
            files=[
                (
                    "files",
                    (
                        "olist_orders_dataset.csv",
                        b"order_id\nO-1\n",
                        "text/csv",
                    ),
                ),
                (
                    "files",
                    (
                        "olist_order_reviews_dataset.csv",
                        b"review_id\nreview-1\xff\n",
                        "text/csv",
                    ),
                ),
            ],
        )

        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()["detail"]
        self.assertIn("olist_order_reviews_dataset.csv", detail)
        self.assertIn("not valid UTF-8 CSV", detail)

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
        self.assertEqual(
            response.json()["relationship_discovery"]["status"],
            "retryable_failed",
        )
        self.assertTrue(response.json()["relationship_discovery"]["graph_id"])
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
