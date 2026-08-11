"""Credential-bound, immutable, read-only SQLite connector."""

from __future__ import annotations

import asyncio
import base64
import re
import sqlite3
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from data_agent.dataset_query.contracts import PreparedQuery

from ..models import AccessGrant, CredentialLease
from ..schemas import (
    CatalogColumn,
    CatalogKey,
    CatalogRelation,
    CatalogSnapshot,
    stable_catalog_id,
    CellValue,
    ConnectorCapabilities,
    ExplainResult,
    QueryRow,
    TabularResult,
)
from .base import ConnectorError, ConnectorErrorCode


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_DENIED_ACTIONS = frozenset(
    action
    for name in (
        "SQLITE_ALTER_TABLE",
        "SQLITE_ANALYZE",
        "SQLITE_ATTACH",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_VTABLE",
        "SQLITE_DELETE",
        "SQLITE_DETACH",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_DROP_VTABLE",
        "SQLITE_INSERT",
        "SQLITE_PRAGMA",
        "SQLITE_REINDEX",
        "SQLITE_UPDATE",
    )
    if (action := getattr(sqlite3, name, None)) is not None
)
_BLOCKED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile"})


def _cell(value: object) -> CellValue:
    if value is None or isinstance(
        value,
        (str, int, float, bool, Decimal, date, datetime),
    ):
        return value
    if isinstance(value, bytes):
        return "base64:" + base64.b64encode(value).decode("ascii")
    return str(value)


class _MedianAggregate:
    """Exact numeric median for governed SQLite query programs."""

    def __init__(self) -> None:
        self._values: list[float] = []

    def step(self, value: object) -> None:
        if value is not None:
            self._values.append(float(value))

    def finalize(self) -> float | None:
        if not self._values:
            return None
        values = sorted(self._values)
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) / 2


class SQLiteConnector:
    """Execute compiler-produced SQLite queries against one immutable file."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        allowed_relations: Sequence[str],
        schema_fingerprint: str,
        source: str,
        connection_ref: str,
        bundle_digest: str | None = None,
    ) -> None:
        path = Path(database_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("SQLite datasource must be a regular file")
        if not allowed_relations:
            raise ValueError("SQLiteConnector requires a relation allowlist")
        schemas: set[str] = set()
        for relation in allowed_relations:
            pieces = relation.split(".", 1)
            if len(pieces) != 2 or not all(
                _IDENTIFIER.fullmatch(part) for part in pieces
            ):
                raise ValueError(
                    "connector relation allowlist contains an invalid identifier"
                )
            schemas.add(pieces[0])
        if len(schemas) != 1:
            raise ValueError("one SQLite source can expose exactly one schema alias")
        self._database_path = path
        self._allowed_relations = tuple(dict.fromkeys(allowed_relations))
        self._schema_alias = next(iter(schemas))
        self._schema_fingerprint = schema_fingerprint
        self._source = source
        self._connection_ref = connection_ref
        self._bundle_digest = bundle_digest

    @staticmethod
    def capabilities() -> ConnectorCapabilities:
        return ConnectorCapabilities(dialect="sqlite")

    def _database_uri(self) -> str:
        return self._database_path.as_uri() + "?mode=ro&immutable=1"

    def _connect(self, *, authorize_query: bool) -> sqlite3.Connection:
        if self._schema_alias == "main":
            connection = sqlite3.connect(
                self._database_uri(),
                uri=True,
                check_same_thread=False,
            )
        else:
            connection = sqlite3.connect(
                ":memory:",
                uri=True,
                check_same_thread=False,
            )
            alias = self._schema_alias.replace('"', '""')
            connection.execute(
                f'ATTACH DATABASE ? AS "{alias}"',
                (self._database_uri(),),
            )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.create_aggregate("median", 1, _MedianAggregate)
        disable_extensions = getattr(connection, "enable_load_extension", None)
        if callable(disable_extensions):
            disable_extensions(False)
        if authorize_query:
            connection.set_authorizer(self._authorize)
        return connection

    @staticmethod
    def _authorize(
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del argument_one, database_name, trigger_name
        if action in _DENIED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if (
            action == getattr(sqlite3, "SQLITE_FUNCTION", -1)
            and (argument_two or "").lower() in _BLOCKED_FUNCTIONS
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

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
                "credential lease does not authorize this SQLite source",
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
            prepared.dialect != "sqlite"
            or grant.bundle_digest != prepared.bundle_digest
            or grant.schema_fingerprint != prepared.schema_fingerprint
            or grant.policy_decision_id != prepared.policy_decision_id
            or grant.logical_plan_hash != prepared.logical_plan_hash
            or grant.prepared_query_hash != prepared.sql_ast_hash
        ):
            raise ConnectorError(
                ConnectorErrorCode.GRANT_MISMATCH,
                "prepared query is not bound to this SQLite access grant",
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

    def _query_sync(
        self,
        sql: str,
        parameters: dict[str, object],
        *,
        timeout: float,
        fetch_limit: int,
    ) -> tuple[tuple[str, ...], tuple[QueryRow, ...], bool]:
        connection = self._connect(authorize_query=True)
        deadline = time.monotonic() + timeout
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline),
            500,
        )
        try:
            cursor = connection.execute(sql, parameters)
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
        finally:
            connection.close()

    async def _query(
        self,
        sql: str,
        parameters: dict[str, object],
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        fetch_limit: int,
    ) -> tuple[tuple[str, ...], tuple[QueryRow, ...], bool]:
        timeout = self._remaining_timeout(grant, lease)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._query_sync,
                    sql,
                    parameters,
                    timeout=timeout,
                    fetch_limit=fetch_limit,
                ),
                timeout=timeout + 0.1,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ConnectorError(
                ConnectorErrorCode.TIMEOUT,
                "SQLite statement exceeded its timeout",
            ) from exc
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                raise ConnectorError(
                    ConnectorErrorCode.TIMEOUT,
                    "SQLite statement exceeded its timeout",
                ) from exc
            raise ConnectorError(
                ConnectorErrorCode.DATABASE_UNAVAILABLE,
                "SQLite operation failed",
            ) from exc

    @staticmethod
    def _parameters(prepared: PreparedQuery) -> dict[str, object]:
        return {
            str(parameter.position): parameter.value
            for parameter in prepared.parameters
        }

    async def execute_readonly(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> TabularResult:
        self._validate_query(prepared, grant, lease)
        columns, rows, truncated = await self._query(
            prepared.executable_sql,
            self._parameters(prepared),
            grant,
            lease,
            fetch_limit=grant.max_rows,
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
        columns, rows, truncated = await self._query(
            prepared.executable_sql,
            self._parameters(prepared),
            grant,
            lease,
            fetch_limit=preview_rows,
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
        _, rows, _ = await self._query(
            "EXPLAIN QUERY PLAN " + prepared.executable_sql,
            self._parameters(prepared),
            grant,
            lease,
            fetch_limit=grant.max_rows,
        )
        return ExplainResult(
            plan_text="\n".join(
                " | ".join(str(value) for value in row.values) for row in rows
            )
            or "SQLite returned an empty query plan",
            estimated_rows=None,
            estimated_cost=None,
        )

    def _introspect_sync(
        self,
        relations: tuple[str, ...],
        *,
        timeout: float,
    ) -> CatalogSnapshot:
        connection = self._connect(authorize_query=False)
        deadline = time.monotonic() + timeout
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline),
            500,
        )
        try:
            catalog_relations: list[CatalogRelation] = []
            alias = self._schema_alias.replace('"', '""')
            for relation in relations:
                table = relation.split(".", 1)[1].replace('"', '""')
                records = connection.execute(
                    f'PRAGMA "{alias}".table_info("{table}")'
                ).fetchall()
                if not records:
                    continue
                columns = tuple(
                    CatalogColumn(
                        column_id=stable_catalog_id("column", relation, str(record[1])),
                        name=str(record[1]), data_type=str(record[2] or "unknown"),
                        nullable=not bool(record[3]), ordinal=index,
                    ) for index, record in enumerate(records, start=1)
                )
                by_name = {column.name: column.column_id for column in columns}
                primary = tuple(by_name[str(record[1])] for record in sorted(records, key=lambda item: int(item[5] or 0)) if int(record[5] or 0) > 0)
                key_items = [CatalogKey(kind="primary", column_ids=primary)] if primary else []
                indexes = connection.execute(f'PRAGMA "{alias}".index_list("{table}")').fetchall()
                for index in indexes:
                    if not bool(index[2]) or str(index[3]) == "pk":
                        continue
                    index_name = str(index[1]).replace('"', '""')
                    fields = connection.execute(f'PRAGMA "{alias}".index_info("{index_name}")').fetchall()
                    column_ids = tuple(by_name[str(field[2])] for field in fields if field[2] is not None)
                    if column_ids:
                        key_items.append(CatalogKey(kind="unique", column_ids=column_ids))
                keys = tuple(key_items)
                catalog_relations.append(CatalogRelation(relation=relation, columns=columns, keys=keys))
            return CatalogSnapshot(
                schema_fingerprint=self._schema_fingerprint,
                relations=tuple(catalog_relations),
            )
        finally:
            connection.close()

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
        timeout = self._remaining_timeout(grant, lease)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._introspect_sync,
                    requested,
                    timeout=timeout,
                ),
                timeout=timeout + 0.1,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ConnectorError(
                ConnectorErrorCode.TIMEOUT,
                "SQLite catalog inspection exceeded its timeout",
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise ConnectorError(
                ConnectorErrorCode.CATALOG_INVALID,
                "SQLite catalog inspection failed",
            ) from exc


__all__ = ["SQLiteConnector"]
