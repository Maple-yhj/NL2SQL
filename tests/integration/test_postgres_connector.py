from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
DOMAIN_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
ENTERPRISE_ROOT = PROJECT_ROOT / "packs" / "enterprises" / "olist"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.runtime import (
    load_bundle_manifest,
    load_domain_pack,
    load_enterprise_binding,
)
from data_agent.runtime.binding import BindingCompiler
from data_agent.runtime.models import PrincipalContext
from data_agent.skills import logical_plan_from_eval_case
from data_agent.tools import AccessGrant, CredentialLease
from data_agent.tools.connectors.postgres import (
    ConnectorError,
    ConnectorErrorCode,
    PostgresConnector,
)


class _Transaction:
    def __init__(self, connection, kwargs) -> None:
        self.connection = connection
        self.kwargs = kwargs

    async def __aenter__(self):
        self.connection.transaction_entries.append(self.kwargs)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.transaction_exits.append(exc_type)
        return False


class _Connection:
    def __init__(self, rows=(), *, block=False) -> None:
        self.rows = list(rows)
        self.block = block
        self.fetch_started = asyncio.Event()
        self.transaction_entries: list[dict] = []
        self.transaction_exits: list[type[BaseException] | None] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_calls: list[
            tuple[str, tuple[object, ...], float | None]
        ] = []
        self.prepare_calls: list[tuple[str, float | None]] = []
        self.cursor_fetch_sizes: list[int] = []

    def transaction(self, **kwargs):
        return _Transaction(self, kwargs)

    async def execute(self, sql: str, *args: object):
        self.execute_calls.append((sql, args))
        return "SELECT 1"

    async def fetch(self, sql: str, *args: object, timeout: float | None = None):
        self.fetch_calls.append((sql, args, timeout))
        self.fetch_started.set()
        if self.block:
            await asyncio.Event().wait()
        if "information_schema.columns" in sql:
            return [
                {
                    "table_schema": "public",
                    "table_name": "olist_order_items_dataset",
                    "column_name": "seller_id",
                    "data_type": "text",
                    "is_nullable": "NO",
                },
                {
                    "table_schema": "public",
                    "table_name": "olist_order_items_dataset",
                    "column_name": "price",
                    "data_type": "numeric",
                    "is_nullable": "NO",
                },
            ]
        if sql.startswith("EXPLAIN"):
            return [{"QUERY PLAN": [{"Plan": {"Total Cost": 12.5, "Plan Rows": 3}}]}]
        return list(self.rows)

    async def prepare(self, sql: str, *, timeout: float | None = None):
        self.prepare_calls.append((sql, timeout))
        connection = self

        class _Cursor:
            async def fetch(self, size: int, *, timeout: float | None = None):
                connection.cursor_fetch_sizes.append(size)
                return list(connection.rows[:size])

        class _CursorFactory:
            def __await__(self):
                async def resolve():
                    return _Cursor()

                return resolve().__await__()

        class _Statement:
            def cursor(self, *args: object, timeout: float | None = None):
                connection.prepared_cursor_args = args
                return _CursorFactory()

        return _Statement()


class _Acquire:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def __aenter__(self):
        self.pool.acquire_count += 1
        return self.pool.connection

    async def __aexit__(self, exc_type, exc, traceback):
        self.pool.release_count += 1
        return False


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.acquire_count = 0
        self.release_count = 0

    def acquire(self):
        return _Acquire(self)


class PostgresConnectorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        domain = load_domain_pack(DOMAIN_ROOT)
        enterprise = load_enterprise_binding(ENTERPRISE_ROOT)
        bundle = load_bundle_manifest(
            PROJECT_ROOT / "generated" / "bundles" / "olist-local.json",
            pack_lock=ENTERPRISE_ROOT / "pack.lock",
            schema_catalog=PROJECT_ROOT / "schema_catalog.json",
        )
        case = next(item for item in domain.spec.evals if item.id == "commerce.metric_002")
        plan = logical_plan_from_eval_case(case, domain)
        cls.principal = PrincipalContext(
            tenant_id="seller-42",
            user_id="user-7",
            roles=("seller",),
        )
        cls.bundle = bundle
        compiler = BindingCompiler(domain, enterprise, bundle)
        cls.prepared = compiler.compile(
            compiler.bind(plan, cls.principal),
            cls.principal,
        )
        cls.allowed_relations = tuple(
            enterprise.spec.policies.relation_allowlist
        )

    def _grant(self, **updates: object) -> AccessGrant:
        now = datetime.now(UTC)
        values = {
            "grant_id": "grant-1",
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
            "credential_id": "lease-1",
            "grant_id": "grant-1",
            "bundle_digest": self.bundle.digest,
            "source": "sales",
            "connection_ref": "secret://olist/local/database",
            "capabilities": ("data.inspect", "query.execute"),
            "secret": "postgresql://redacted",
            "issued_at": now,
            "expires_at": now + timedelta(seconds=20),
        }
        values.update(updates)
        return CredentialLease(**values)

    async def test_execute_uses_pool_readonly_transaction_timeout_and_exact_query(self) -> None:
        connection = _Connection(
            rows=(
                {"seller_id": "s-1", "gmv": 9.5},
                {"seller_id": "s-2", "gmv": 7.0},
            )
        )
        pool = _Pool(connection)
        connector = PostgresConnector(
            pool,
            allowed_relations=self.allowed_relations,
            schema_fingerprint=self.bundle.schema_fingerprint,
        )

        result = await connector.execute_readonly(
            self.prepared,
            self._grant(),
            self._lease(),
        )

        self.assertEqual(pool.acquire_count, 1)
        self.assertEqual(pool.release_count, 1)
        self.assertEqual(connection.transaction_entries, [{"readonly": True}])
        self.assertEqual(connection.transaction_exits, [None])
        self.assertEqual(
            connection.execute_calls,
            [
                (
                    "SELECT set_config('statement_timeout', $1, true)",
                    ("2500ms",),
                )
            ],
        )
        query_sql, args, timeout = connection.fetch_calls[-1]
        self.assertEqual(query_sql, self.prepared.executable_sql)
        self.assertEqual(args, tuple(item.value for item in self.prepared.parameters))
        self.assertEqual(timeout, 2.5)
        self.assertEqual(result.columns, ("seller_id", "gmv"))
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.rows[0].values, ("s-1", 9.5))

    async def test_expired_or_mismatched_grants_fail_before_pool_acquire(self) -> None:
        pool = _Pool(_Connection())
        connector = PostgresConnector(
            pool,
            allowed_relations=self.allowed_relations,
            schema_fingerprint=self.bundle.schema_fingerprint,
        )
        now = datetime.now(UTC)
        cases = (
            (
                self._grant(
                    issued_at=now - timedelta(seconds=60),
                    expires_at=now - timedelta(seconds=1),
                ),
                ConnectorErrorCode.GRANT_EXPIRED,
            ),
            (self._grant(policy_decision_id="policy-forged"), ConnectorErrorCode.GRANT_MISMATCH),
            (self._grant(prepared_query_hash="0" * 64), ConnectorErrorCode.GRANT_MISMATCH),
            (self._grant(logical_plan_hash="0" * 64), ConnectorErrorCode.GRANT_MISMATCH),
            (self._grant(bundle_digest="0" * 64), ConnectorErrorCode.GRANT_MISMATCH),
            (self._grant(schema_fingerprint="0" * 64), ConnectorErrorCode.GRANT_MISMATCH),
            (
                self._grant(allowed_relations=("public.olist_sellers_dataset",)),
                ConnectorErrorCode.RELATION_NOT_ALLOWED,
            ),
        )

        for grant, code in cases:
            with self.subTest(code=code), self.assertRaises(ConnectorError) as raised:
                await connector.execute_readonly(self.prepared, grant, self._lease())
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(pool.acquire_count, 0)

    async def test_row_cap_fails_closed(self) -> None:
        connection = _Connection(rows=({"value": index} for index in range(3)))
        connector = PostgresConnector(
            _Pool(connection),
            allowed_relations=self.allowed_relations,
            schema_fingerprint=self.bundle.schema_fingerprint,
        )

        with self.assertRaises(ConnectorError) as raised:
            await connector.execute_readonly(
                self.prepared,
                self._grant(max_rows=2),
                self._lease(),
            )

        self.assertEqual(raised.exception.code, ConnectorErrorCode.ROW_LIMIT_EXCEEDED)

    async def test_explain_and_introspection_are_authorized_and_typed(self) -> None:
        connection = _Connection()
        connector = PostgresConnector(
            _Pool(connection),
            allowed_relations=self.allowed_relations,
            schema_fingerprint=self.bundle.schema_fingerprint,
        )

        explain = await connector.explain(
            self.prepared,
            self._grant(),
            self._lease(),
        )
        catalog = await connector.introspect_schema(
            self._grant(
                tool_name="data.inspect",
                prepared_query_hash=None,
                logical_plan_hash=None,
            ),
            self._lease(),
            relations=("public.olist_order_items_dataset",),
        )

        self.assertEqual(explain.estimated_cost, 12.5)
        self.assertEqual(explain.estimated_rows, 3)
        self.assertEqual(len(catalog.relations), 1)
        self.assertEqual(
            catalog.relations[0].relation,
            "public.olist_order_items_dataset",
        )
        self.assertEqual(
            tuple(column.name for column in catalog.relations[0].columns),
            ("seller_id", "price"),
        )
        introspect_sql, introspect_args, _ = connection.fetch_calls[-1]
        self.assertIn("information_schema.columns", introspect_sql)
        self.assertNotIn("olist_order_items_dataset", introspect_sql)
        self.assertIn("olist_order_items_dataset", introspect_args[1])

    async def test_cancellation_propagates_and_releases_transaction_and_pool(self) -> None:
        connection = _Connection(block=True)
        pool = _Pool(connection)
        connector = PostgresConnector(
            pool,
            allowed_relations=self.allowed_relations,
            schema_fingerprint=self.bundle.schema_fingerprint,
        )
        task = asyncio.create_task(
            connector.execute_readonly(
                self.prepared,
                self._grant(),
                self._lease(),
            )
        )
        await connection.fetch_started.wait()

        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(pool.release_count, 1)
        self.assertEqual(connection.transaction_exits, [asyncio.CancelledError])

    async def test_lease_authorizes_pool_use_and_preview_fetches_only_sentinel(self) -> None:
        connection = _Connection(
            rows=tuple({"value": index} for index in range(100))
        )
        pool = _Pool(connection)
        connector = PostgresConnector(
            pool,
            allowed_relations=self.allowed_relations,
            schema_fingerprint=self.bundle.schema_fingerprint,
            bundle_digest=self.bundle.digest,
            connection_ref="secret://olist/local/database",
        )
        lease = self._lease()

        preview = await connector.preview(
            self.prepared,
            self._grant(),
            lease,
            preview_rows=2,
        )

        self.assertEqual(len(preview.rows), 2)
        self.assertTrue(preview.truncated)
        self.assertEqual(connection.fetch_calls, [])
        self.assertEqual(connection.prepare_calls[0][0], self.prepared.executable_sql)
        self.assertEqual(connection.cursor_fetch_sizes, [3])
        self.assertEqual(
            connection.prepared_cursor_args,
            tuple(item.value for item in self.prepared.parameters),
        )

        invalid = (
            self._lease(source="other"),
            self._lease(connection_ref="secret://other/database"),
            self._lease(capabilities=("data.inspect",)),
            self._lease(bundle_digest="0" * 64),
            self._lease(grant_id="other-grant"),
            self._lease(
                issued_at=datetime.now(UTC) - timedelta(seconds=60),
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        )
        before = pool.acquire_count
        for candidate in invalid:
            with self.subTest(lease=candidate), self.assertRaises(ConnectorError) as raised:
                await connector.preview(
                    self.prepared,
                    self._grant(),
                    candidate,
                    preview_rows=2,
                )
            self.assertIn(
                raised.exception.code,
                {
                    ConnectorErrorCode.CREDENTIAL_EXPIRED,
                    ConnectorErrorCode.CREDENTIAL_MISMATCH,
                },
            )
        self.assertEqual(pool.acquire_count, before)


if __name__ == "__main__":
    unittest.main()
