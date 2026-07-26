"""Credential-bound, pool-resolved, read-only PostgreSQL connector."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

from data_agent.runtime.binding import PreparedQuery

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


class PoolResolver(Protocol):
    def session(
        self,
        *,
        lease: CredentialLease,
        grant: AccessGrant,
        required_capability: str,
    ) -> object: ...


class StaticPoolResolver:
    """Credential-authorized adapter for an already-created single-source pool."""

    def __init__(
        self,
        pool: object,
        *,
        source: str,
        connection_ref: str,
        bundle_digest: str | None,
    ) -> None:
        self._pool = pool
        self._source = source
        self._connection_ref = connection_ref
        self._bundle_digest = bundle_digest

    def _validate(
        self,
        lease: CredentialLease,
        grant: AccessGrant,
        required_capability: str,
    ) -> None:
        now = datetime.now(UTC)
        if lease.expires_at <= now:
            raise ConnectorError(
                ConnectorErrorCode.CREDENTIAL_EXPIRED,
                "credential lease has expired",
            )
        if (
            lease.issued_at > now
            or lease.expires_at > grant.expires_at
            or lease.grant_id != grant.grant_id
            or lease.bundle_digest != grant.bundle_digest
            or lease.source != self._source
            or lease.source != grant.source
            or lease.connection_ref != self._connection_ref
            or required_capability not in lease.capabilities
            or (
                self._bundle_digest is not None
                and lease.bundle_digest != self._bundle_digest
            )
            or not lease.secret.get_secret_value()
        ):
            raise ConnectorError(
                ConnectorErrorCode.CREDENTIAL_MISMATCH,
                "credential lease does not authorize this pool",
            )

    @asynccontextmanager
    async def session(
        self,
        *,
        lease: CredentialLease,
        grant: AccessGrant,
        required_capability: str,
    ) -> AsyncIterator[object]:
        self._validate(lease, grant, required_capability)
        async with self._pool.acquire() as connection:  # type: ignore[attr-defined]
            self._validate(lease, grant, required_capability)
            yield connection


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_INTROSPECTION_SQL = """SELECT table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = ANY($1::text[])
  AND table_name = ANY($2::text[])
ORDER BY table_schema, table_name, ordinal_position"""
_SET_TIMEOUT_SQL = "SELECT set_config('statement_timeout', $1, true)"


def _as_mapping(record: object) -> Mapping[str, object]:
    if isinstance(record, Mapping):
        return record
    try:
        return dict(record)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConnectorError(
            ConnectorErrorCode.DATABASE_UNAVAILABLE,
            "database returned an unsupported row representation",
        ) from exc


def _cell(value: object) -> CellValue:
    if value is None or isinstance(
        value,
        (str, int, float, bool, Decimal, date, datetime),
    ):
        return value
    return str(value)


def _find_numeric(value: object, key: str) -> float | int | None:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return candidate
        for nested in value.values():
            found = _find_numeric(nested, key)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            found = _find_numeric(nested, key)
            if found is not None:
                return found
    return None


class PostgresConnector:
    """Execute only compiler-produced queries through a credential-bound resolver."""

    def __init__(
        self,
        pool: object | None,
        *,
        allowed_relations: Sequence[str],
        schema_fingerprint: str,
        source: str = "sales",
        connection_ref: str = "secret://olist/local/database",
        bundle_digest: str | None = None,
        pool_resolver: PoolResolver | None = None,
    ) -> None:
        if not allowed_relations:
            raise ValueError("PostgresConnector requires a relation allowlist")
        for relation in allowed_relations:
            pieces = relation.split(".", 1)
            if len(pieces) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in pieces):
                raise ValueError("connector relation allowlist contains an invalid identifier")
        if pool_resolver is None:
            if pool is None:
                raise ValueError("connector requires a pool or PoolResolver")
            pool_resolver = StaticPoolResolver(
                pool,
                source=source,
                connection_ref=connection_ref,
                bundle_digest=bundle_digest,
            )
        self._pool_resolver = pool_resolver
        self._allowed_relations = tuple(dict.fromkeys(allowed_relations))
        self._schema_fingerprint = schema_fingerprint
        self._source = source
        self._connection_ref = connection_ref
        self._bundle_digest = bundle_digest

    @staticmethod
    def quote_identifier(identifier: str) -> str:
        if not _IDENTIFIER.fullmatch(identifier):
            raise ValueError("invalid PostgreSQL identifier")
        return f'"{identifier}"'

    @staticmethod
    def capabilities() -> ConnectorCapabilities:
        return ConnectorCapabilities()

    def _validate_common_grant(
        self,
        grant: AccessGrant,
        *,
        expected_tool: str,
    ) -> None:
        now = datetime.now(UTC)
        if grant.expires_at <= now:
            raise ConnectorError(
                ConnectorErrorCode.GRANT_EXPIRED,
                "access grant has expired",
            )
        if (
            grant.tool_name != expected_tool
            or grant.tool_version != "1.0.0"
            or not grant.read_only
            or grant.source != self._source
            or grant.schema_fingerprint != self._schema_fingerprint
            or (
                self._bundle_digest is not None
                and grant.bundle_digest != self._bundle_digest
            )
        ):
            raise ConnectorError(
                ConnectorErrorCode.GRANT_MISMATCH,
                "access grant does not match connector authority",
            )

    def _validate_query_grant(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
    ) -> None:
        self._validate_common_grant(grant, expected_tool="query.execute")
        if (
            prepared.dialect != "postgres"
            or grant.bundle_digest != prepared.bundle_digest
            or grant.schema_fingerprint != prepared.schema_fingerprint
            or grant.policy_decision_id != prepared.policy_decision_id
            or grant.logical_plan_hash != prepared.logical_plan_hash
            or grant.prepared_query_hash != prepared.sql_ast_hash
        ):
            raise ConnectorError(
                ConnectorErrorCode.GRANT_MISMATCH,
                "access grant is not bound to this prepared query",
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
    def _remaining_timeout(grant: AccessGrant, lease: CredentialLease) -> float:
        now = datetime.now(UTC)
        remaining = min(
            grant.statement_timeout_ms / 1000,
            (grant.expires_at - now).total_seconds(),
            (lease.expires_at - now).total_seconds(),
        )
        if lease.expires_at <= now:
            raise ConnectorError(
                ConnectorErrorCode.CREDENTIAL_EXPIRED,
                "credential lease has expired",
            )
        if grant.expires_at <= now:
            raise ConnectorError(
                ConnectorErrorCode.GRANT_EXPIRED,
                "access grant has expired",
            )
        return max(0.001, remaining)

    async def _fetch_in_readonly_transaction(
        self,
        sql: str,
        values: Sequence[object],
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        required_capability: str,
        cursor_limit: int | None = None,
    ) -> list[object]:
        try:
            async with self._pool_resolver.session(
                lease=lease,
                grant=grant,
                required_capability=required_capability,
            ) as connection:
                async with connection.transaction(readonly=True):
                    timeout = self._remaining_timeout(grant, lease)
                    await connection.execute(
                        _SET_TIMEOUT_SQL,
                        f"{max(1, int(timeout * 1000))}ms",
                    )
                    if cursor_limit is None:
                        records = await connection.fetch(
                            sql,
                            *values,
                            timeout=timeout,
                        )
                    else:
                        statement = await connection.prepare(sql, timeout=timeout)
                        cursor = await statement.cursor(*values, timeout=timeout)
                        records = await cursor.fetch(cursor_limit, timeout=timeout)
                    self._remaining_timeout(grant, lease)
                    return list(records)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            if lease.expires_at <= datetime.now(UTC):
                raise ConnectorError(
                    ConnectorErrorCode.CREDENTIAL_EXPIRED,
                    "credential lease expired during database access",
                ) from exc
            raise ConnectorError(
                ConnectorErrorCode.TIMEOUT,
                "PostgreSQL statement exceeded its timeout",
            ) from exc
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(
                ConnectorErrorCode.DATABASE_UNAVAILABLE,
                "PostgreSQL operation failed",
            ) from exc

    @staticmethod
    def _tabular(
        prepared: PreparedQuery,
        records: Sequence[object],
        *,
        truncated: bool = False,
    ) -> TabularResult:
        if records:
            columns = tuple(str(item) for item in _as_mapping(records[0]).keys())
        else:
            from sqlglot import parse_one

            statement = parse_one(prepared.executable_sql, read="postgres")
            columns = tuple(
                expression.alias_or_name
                for expression in statement.expressions
                if expression.alias_or_name
            )
        rows = tuple(
            QueryRow(
                values=tuple(_cell(_as_mapping(record).get(column)) for column in columns)
            )
            for record in records
        )
        return TabularResult(columns=columns, rows=rows, truncated=truncated)

    async def execute_readonly(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> TabularResult:
        self._validate_query_grant(prepared, grant)
        records = await self._fetch_in_readonly_transaction(
            prepared.executable_sql,
            tuple(item.value for item in prepared.parameters),
            grant,
            lease,
            required_capability="query.execute",
        )
        if len(records) > grant.max_rows:
            raise ConnectorError(
                ConnectorErrorCode.ROW_LIMIT_EXCEEDED,
                "database result exceeded the grant row cap",
            )
        return self._tabular(prepared, records)

    async def preview(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        preview_rows: int,
    ) -> TabularResult:
        self._validate_query_grant(prepared, grant)
        if preview_rows < 1 or preview_rows > grant.max_rows:
            raise ConnectorError(
                ConnectorErrorCode.ROW_LIMIT_EXCEEDED,
                "preview row cap is outside the grant",
            )
        records = await self._fetch_in_readonly_transaction(
            prepared.executable_sql,
            tuple(item.value for item in prepared.parameters),
            grant,
            lease,
            required_capability="query.execute",
            cursor_limit=preview_rows + 1,
        )
        truncated = len(records) > preview_rows
        return self._tabular(
            prepared,
            records[:preview_rows],
            truncated=truncated,
        )

    async def explain(
        self,
        prepared: PreparedQuery,
        grant: AccessGrant,
        lease: CredentialLease,
    ) -> ExplainResult:
        self._validate_query_grant(prepared, grant)
        records = await self._fetch_in_readonly_transaction(
            "EXPLAIN (FORMAT JSON) " + prepared.executable_sql,
            tuple(item.value for item in prepared.parameters),
            grant,
            lease,
            required_capability="query.execute",
        )
        raw = [_as_mapping(item) for item in records]
        cost = _find_numeric(raw, "Total Cost")
        rows = _find_numeric(raw, "Plan Rows")
        return ExplainResult(
            plan_text=json.dumps(raw, ensure_ascii=False, default=str, sort_keys=True),
            estimated_cost=float(cost) if cost is not None else None,
            estimated_rows=int(rows) if rows is not None else None,
        )

    async def introspect_schema(
        self,
        grant: AccessGrant,
        lease: CredentialLease,
        *,
        relations: Sequence[str] = (),
    ) -> CatalogSnapshot:
        self._validate_common_grant(grant, expected_tool="data.inspect")
        requested = tuple(relations) or grant.allowed_relations
        if not set(requested).issubset(set(self._allowed_relations)) or not set(
            requested
        ).issubset(set(grant.allowed_relations)):
            raise ConnectorError(
                ConnectorErrorCode.RELATION_NOT_ALLOWED,
                "catalog relation is not authorized",
            )
        schemas = tuple(dict.fromkeys(item.split(".", 1)[0] for item in requested))
        tables = tuple(dict.fromkeys(item.split(".", 1)[1] for item in requested))
        records = await self._fetch_in_readonly_transaction(
            _INTROSPECTION_SQL,
            (schemas, tables),
            grant,
            lease,
            required_capability="data.inspect",
        )
        grouped: dict[str, list[CatalogColumn]] = defaultdict(list)
        for record in records:
            value = _as_mapping(record)
            try:
                relation = f"{value['table_schema']}.{value['table_name']}"
                if relation not in requested:
                    continue
                grouped[relation].append(
                    CatalogColumn(
                        name=str(value["column_name"]),
                        data_type=str(value["data_type"]),
                        nullable=str(value["is_nullable"]).upper() == "YES",
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ConnectorError(
                    ConnectorErrorCode.CATALOG_INVALID,
                    "PostgreSQL catalog row is incomplete",
                ) from exc
        return CatalogSnapshot(
            schema_fingerprint=self._schema_fingerprint,
            relations=tuple(
                CatalogRelation(relation=relation, columns=tuple(grouped[relation]))
                for relation in requested
                if relation in grouped
            ),
        )
