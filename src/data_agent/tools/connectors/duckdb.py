"""Read-only DuckDB connector for immutable file datasource snapshots."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

import duckdb

from data_agent.dataset_query.contracts import PreparedQuery

from ..models import AccessGrant, CredentialLease
from ..schemas import (
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
    CellValue,
    ConnectorCapabilities,
    ExplainResult,
    QueryRow,
    TabularResult,
)
from .base import ConnectorError, ConnectorErrorCode


_Result = TypeVar("_Result")


def _cell(value: object) -> CellValue:
    if value is None or isinstance(
        value,
        (str, int, float, bool, Decimal, date, datetime),
    ):
        return value
    if isinstance(value, bytes):
        return "base64:" + base64.b64encode(value).decode("ascii")
    return str(value)


class DuckDBConnector:
    """Execute only compiler-produced SQL against one immutable snapshot."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        allowed_relations: Sequence[str],
        schema_fingerprint: str,
        source: str,
        connection_ref: str,
        bundle_digest: str | None = None,
        memory_limit: str = "512MB",
        threads: int = 2,
    ) -> None:
        path = Path(database_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("DuckDB datasource must be a regular file")
        if not allowed_relations:
            raise ValueError("DuckDBConnector requires a relation allowlist")
        self._database_path = path
        self._allowed_relations = tuple(dict.fromkeys(allowed_relations))
        self._schema_fingerprint = schema_fingerprint
        self._source = source
        self._connection_ref = connection_ref
        self._bundle_digest = bundle_digest
        self._memory_limit = memory_limit
        self._threads = threads

    @staticmethod
    def capabilities() -> ConnectorCapabilities:
        return ConnectorCapabilities(dialect="duckdb")

    def _connect(self) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(
            str(self._database_path),
            read_only=True,
            config={
                "enable_external_access": "false",
                "allow_community_extensions": "false",
                "autoload_known_extensions": "false",
                "autoinstall_known_extensions": "false",
                "memory_limit": self._memory_limit,
                "threads": str(self._threads),
            },
        )
        connection.execute("SET lock_configuration = true")
        return connection

    def _validate_common_grant(
        self,
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        expected_tool: str,
    ) -> None:
        now = datetime.now(UTC)
        if grant.expires_at <= now:
            raise ConnectorError(
                ConnectorErrorCode.GRANT_EXPIRED,
                "access grant has expired",
            )
        if lease.expires_at <= now:
            raise ConnectorError(
                ConnectorErrorCode.CREDENTIAL_EXPIRED,
                "credential lease has expired",
            )
        if (
            grant.tool_name != expected_tool
            or grant.tool_version != "1.0.0"
            or not grant.read_only
            or grant.source != self._source
            or grant.schema_fingerprint != self._schema_fingerprint
            or lease.issued_at > now
            or lease.expires_at > grant.expires_at
            or lease.grant_id != grant.grant_id
            or lease.bundle_digest != grant.bundle_digest
            or lease.source != self._source
            or lease.source != grant.source
            or lease.connection_ref != self._connection_ref
            or expected_tool not in lease.capabilities
            or (
                self._bundle_digest is not None
                and (
                    grant.bundle_digest != self._bundle_digest
                    or lease.bundle_digest != self._bundle_digest
                )
            )
            or not lease.secret.get_secret_value()
        ):
            raise ConnectorError(
                ConnectorErrorCode.CREDENTIAL_MISMATCH,
                "credential lease does not authorize this DuckDB snapshot",
            )

    def _validate_query(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> None:
        self._validate_common_grant(
            grant,
            lease,
            expected_tool="query.execute",
        )
        if (
            prepared.dialect != "duckdb"
            or grant.bundle_digest != prepared.bundle_digest
            or grant.schema_fingerprint != prepared.schema_fingerprint
            or grant.policy_decision_id != prepared.policy_decision_id
            or grant.logical_plan_hash != prepared.logical_plan_hash
            or grant.prepared_query_hash != prepared.sql_ast_hash
        ):
            raise ConnectorError(
                ConnectorErrorCode.GRANT_MISMATCH,
                "prepared query is not bound to this DuckDB access grant",
            )
        relations = set(prepared.allowed_relations)
        if not relations.issubset(set(self._allowed_relations)) or not relations.issubset(
            set(grant.allowed_relations)
        ):
            raise ConnectorError(
                ConnectorErrorCode.RELATION_NOT_ALLOWED,
                "prepared query relation is not authorized",
            )
        if prepared.max_rows > grant.max_rows:
            raise ConnectorError(
                ConnectorErrorCode.ROW_LIMIT_EXCEEDED,
                "prepared query exceeds the grant row cap",
            )

    @staticmethod
    def _remaining_timeout(
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> float:
        now = datetime.now(UTC)
        remaining = min(
            grant.statement_timeout_ms / 1000,
            (grant.expires_at - now).total_seconds(),
            (lease.expires_at - now).total_seconds(),
        )
        return max(0.001, remaining)

    async def _run_interruptible(
        self,
        operation: Callable[[duckdb.DuckDBPyConnection], _Result],
        *,
        timeout: float,
        error_message: str,
    ) -> _Result:
        holder: dict[str, duckdb.DuckDBPyConnection] = {}

        def run() -> _Result:
            connection = self._connect()
            holder["connection"] = connection
            try:
                return operation(connection)
            finally:
                holder.pop("connection", None)
                connection.close()

        task = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            connection = holder.get("connection")
            if connection is not None:
                connection.interrupt()
            raise
        except asyncio.TimeoutError as exc:
            connection = holder.get("connection")
            if connection is not None:
                connection.interrupt()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            except (asyncio.TimeoutError, duckdb.Error):
                pass
            raise ConnectorError(
                ConnectorErrorCode.TIMEOUT,
                error_message,
            ) from exc
        except duckdb.Error as exc:
            raise ConnectorError(
                ConnectorErrorCode.DATABASE_UNAVAILABLE,
                "DuckDB operation failed",
            ) from exc

    @staticmethod
    def _parameters(prepared: PreparedQuery) -> list[object]:
        return [parameter.value for parameter in prepared.parameters]

    def _query_operation(
        self,
        *,
        sql: str,
        parameters: Sequence[object],
        fetch_limit: int,
    ) -> Callable[
        [duckdb.DuckDBPyConnection],
        tuple[tuple[str, ...], tuple[QueryRow, ...], bool],
    ]:
        def operation(
            connection: duckdb.DuckDBPyConnection,
        ) -> tuple[tuple[str, ...], tuple[QueryRow, ...], bool]:
            cursor = connection.execute(sql, list(parameters))
            columns = tuple(
                str(description[0]) for description in (cursor.description or ())
            )
            records = cursor.fetchmany(fetch_limit + 1)
            truncated = len(records) > fetch_limit
            rows = tuple(
                QueryRow(values=tuple(_cell(value) for value in record))
                for record in records[:fetch_limit]
            )
            return columns, rows, truncated

        return operation

    async def execute_readonly(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> TabularResult:
        self._validate_query(prepared, grant, lease)
        columns, rows, truncated = await self._run_interruptible(
            self._query_operation(
                sql=prepared.executable_sql,
                parameters=self._parameters(prepared),
                fetch_limit=grant.max_rows,
            ),
            timeout=self._remaining_timeout(grant, lease),
            error_message="DuckDB statement exceeded its timeout",
        )
        if truncated:
            raise ConnectorError(
                ConnectorErrorCode.ROW_LIMIT_EXCEEDED,
                "database result exceeded the grant row cap",
            )
        return TabularResult(columns=columns, rows=rows)

    async def preview(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        preview_rows: int,
    ) -> TabularResult:
        self._validate_query(prepared, grant, lease)
        if preview_rows < 1 or preview_rows > grant.max_rows:
            raise ConnectorError(
                ConnectorErrorCode.ROW_LIMIT_EXCEEDED,
                "preview row cap is outside the grant",
            )
        columns, rows, truncated = await self._run_interruptible(
            self._query_operation(
                sql=prepared.executable_sql,
                parameters=self._parameters(prepared),
                fetch_limit=preview_rows,
            ),
            timeout=self._remaining_timeout(grant, lease),
            error_message="DuckDB preview exceeded its timeout",
        )
        return TabularResult(
            columns=columns,
            rows=rows,
            truncated=truncated,
        )

    async def explain(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> ExplainResult:
        self._validate_query(prepared, grant, lease)
        _, rows, _ = await self._run_interruptible(
            self._query_operation(
                sql="EXPLAIN " + prepared.executable_sql,
                parameters=self._parameters(prepared),
                fetch_limit=grant.max_rows,
            ),
            timeout=self._remaining_timeout(grant, lease),
            error_message="DuckDB explain exceeded its timeout",
        )
        return ExplainResult(
            plan_text="\n".join(
                " | ".join(str(value) for value in row.values) for row in rows
            )
            or "DuckDB returned an empty query plan",
            estimated_rows=None,
            estimated_cost=None,
        )

    async def introspect_schema(
        self,
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        relations: Sequence[str] = (),
    ) -> CatalogSnapshot:
        self._validate_common_grant(
            grant,
            lease,
            expected_tool="data.inspect",
        )
        requested = tuple(relations) or grant.allowed_relations
        if not set(requested).issubset(set(self._allowed_relations)) or not set(
            requested
        ).issubset(set(grant.allowed_relations)):
            raise ConnectorError(
                ConnectorErrorCode.RELATION_NOT_ALLOWED,
                "catalog relation is not authorized",
            )

        def operation(
            connection: duckdb.DuckDBPyConnection,
        ) -> CatalogSnapshot:
            rows = connection.execute(
                """
                SELECT table_schema, table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                ORDER BY table_schema, table_name, ordinal_position
                """
            ).fetchall()
            grouped: dict[str, list[CatalogColumn]] = {}
            for schema, table, column, data_type, is_nullable in rows:
                relation = f"{schema}.{table}"
                if relation not in requested:
                    continue
                grouped.setdefault(relation, []).append(
                    CatalogColumn(
                        name=str(column),
                        data_type=str(data_type),
                        nullable=str(is_nullable).upper() == "YES",
                    )
                )
            return CatalogSnapshot(
                schema_fingerprint=self._schema_fingerprint,
                relations=tuple(
                    CatalogRelation(
                        relation=relation,
                        columns=tuple(grouped[relation]),
                    )
                    for relation in requested
                    if relation in grouped
                ),
            )

        return await self._run_interruptible(
            operation,
            timeout=self._remaining_timeout(grant, lease),
            error_message="DuckDB catalog inspection exceeded its timeout",
        )


__all__ = ["DuckDBConnector"]
