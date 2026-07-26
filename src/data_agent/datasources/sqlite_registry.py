"""Durable SQLite implementation of the datasource control-plane registry."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, TypeVar

from .models import (
    ConversationDataSourcePin,
    DataSourceDefinition,
    DataSourceSnapshot,
    DataSourceStatus,
    SemanticBindingRecord,
    SemanticBindingStatus,
)
from .registry import DataSourceRegistryError, DataSourceRegistryErrorCode


_T = TypeVar("_T")


class SQLiteDataSourceRegistry:
    """Persist versioned datasource metadata without storing credentials."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path).expanduser().resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._initialize()

    async def create(
        self,
        definition: DataSourceDefinition,
    ) -> DataSourceDefinition:
        def operation(connection: sqlite3.Connection) -> DataSourceDefinition:
            try:
                connection.execute(
                    """
                    INSERT INTO data_sources (tenant_id, source_id, payload)
                    VALUES (?, ?, ?)
                    """,
                    (
                        definition.tenant_id,
                        definition.source_id,
                        definition.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "datasource already exists",
                ) from exc
            return definition

        return await self._write(operation)

    async def get(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceDefinition:
        def operation(connection: sqlite3.Connection) -> DataSourceDefinition:
            row = connection.execute(
                """
                SELECT payload
                FROM data_sources
                WHERE tenant_id = ? AND source_id = ?
                """,
                (tenant_id, source_id),
            ).fetchone()
            if row is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource was not found",
                )
            return DataSourceDefinition.model_validate_json(row[0])

        return await self._read(operation)

    async def list(
        self,
        *,
        tenant_id: str,
    ) -> tuple[DataSourceDefinition, ...]:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[DataSourceDefinition, ...]:
            rows = connection.execute(
                """
                SELECT payload
                FROM data_sources
                WHERE tenant_id = ?
                ORDER BY source_id
                """,
                (tenant_id,),
            ).fetchall()
            return tuple(
                DataSourceDefinition.model_validate_json(row[0])
                for row in rows
            )

        return await self._read(operation)

    async def publish_snapshot(
        self,
        snapshot: DataSourceSnapshot,
    ) -> DataSourceDefinition:
        def operation(connection: sqlite3.Connection) -> DataSourceDefinition:
            row = connection.execute(
                """
                SELECT payload
                FROM data_sources
                WHERE tenant_id = ? AND source_id = ?
                """,
                (snapshot.tenant_id, snapshot.source_id),
            ).fetchone()
            if row is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource was not found",
                )
            source = DataSourceDefinition.model_validate_json(row[0])
            expected_version = source.active_snapshot_version + 1
            if snapshot.version != expected_version:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "datasource snapshot version is not the next version",
                )
            try:
                connection.execute(
                    """
                    INSERT INTO data_source_snapshots (
                        tenant_id, source_id, version, payload
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        snapshot.tenant_id,
                        snapshot.source_id,
                        snapshot.version,
                        snapshot.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "datasource snapshot version is not the next version",
                ) from exc
            updated = source.model_copy(
                update={
                    "revision": source.revision + 1,
                    "active_snapshot_version": snapshot.version,
                    "status": DataSourceStatus.READY,
                    "updated_at": datetime.now(UTC),
                }
            )
            connection.execute(
                """
                UPDATE data_sources
                SET payload = ?
                WHERE tenant_id = ? AND source_id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.tenant_id,
                    updated.source_id,
                ),
            )
            return updated

        return await self._write(operation)

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        source_id: str,
        version: int,
    ) -> DataSourceSnapshot:
        def operation(connection: sqlite3.Connection) -> DataSourceSnapshot:
            row = connection.execute(
                """
                SELECT payload
                FROM data_source_snapshots
                WHERE tenant_id = ? AND source_id = ? AND version = ?
                """,
                (tenant_id, source_id, version),
            ).fetchone()
            if row is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource snapshot was not found",
                )
            return DataSourceSnapshot.model_validate_json(row[0])

        return await self._read(operation)

    async def save_binding(
        self,
        binding: SemanticBindingRecord,
    ) -> SemanticBindingRecord:
        def operation(connection: sqlite3.Connection) -> SemanticBindingRecord:
            snapshot = connection.execute(
                """
                SELECT 1
                FROM data_source_snapshots
                WHERE tenant_id = ? AND source_id = ? AND version = ?
                """,
                (
                    binding.tenant_id,
                    binding.source_id,
                    binding.source_snapshot_version,
                ),
            ).fetchone()
            if snapshot is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding source snapshot was not found",
                )
            if binding.status != SemanticBindingStatus.DRAFT:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_BINDING,
                    "new semantic bindings must start as drafts",
                )
            try:
                connection.execute(
                    """
                    INSERT INTO semantic_bindings (
                        tenant_id, binding_id, source_id, domain_id, payload
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        binding.tenant_id,
                        binding.binding_id,
                        binding.source_id,
                        binding.domain_id,
                        binding.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "semantic binding already exists",
                ) from exc
            return binding

        return await self._write(operation)

    async def list_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticBindingRecord, ...]:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[SemanticBindingRecord, ...]:
            rows = connection.execute(
                """
                SELECT payload
                FROM semantic_bindings
                WHERE tenant_id = ? AND source_id = ?
                ORDER BY binding_id
                """,
                (tenant_id, source_id),
            ).fetchall()
            return tuple(
                SemanticBindingRecord.model_validate_json(row[0])
                for row in rows
            )

        return await self._read(operation)

    async def activate_binding(
        self,
        *,
        tenant_id: str,
        binding_id: str,
    ) -> SemanticBindingRecord:
        def operation(connection: sqlite3.Connection) -> SemanticBindingRecord:
            row = connection.execute(
                """
                SELECT payload
                FROM semantic_bindings
                WHERE tenant_id = ? AND binding_id = ?
                """,
                (tenant_id, binding_id),
            ).fetchone()
            if row is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding was not found",
                )
            binding = SemanticBindingRecord.model_validate_json(row[0])
            snapshot_row = connection.execute(
                """
                SELECT payload
                FROM data_source_snapshots
                WHERE tenant_id = ? AND source_id = ? AND version = ?
                """,
                (
                    tenant_id,
                    binding.source_id,
                    binding.source_snapshot_version,
                ),
            ).fetchone()
            if snapshot_row is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding source snapshot was not found",
                )
            snapshot = DataSourceSnapshot.model_validate_json(snapshot_row[0])
            catalog = {
                relation.relation: {column.name for column in relation.columns}
                for relation in snapshot.catalog.relations
            }
            invalid = tuple(
                mapping.logical_ref
                for mapping in binding.mappings
                if mapping.physical_relation not in catalog
                or mapping.physical_column
                not in catalog[mapping.physical_relation]
            )
            if invalid:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_BINDING,
                    "semantic binding references unknown physical fields: "
                    + ", ".join(invalid),
                )
            now = datetime.now(UTC)
            active_rows = connection.execute(
                """
                SELECT binding_id, payload
                FROM semantic_bindings
                WHERE tenant_id = ? AND source_id = ? AND domain_id = ?
                """,
                (tenant_id, binding.source_id, binding.domain_id),
            ).fetchall()
            for candidate_id, payload in active_rows:
                if candidate_id == binding_id:
                    continue
                candidate = SemanticBindingRecord.model_validate_json(payload)
                if candidate.status != SemanticBindingStatus.ACTIVE:
                    continue
                retired = candidate.model_copy(
                    update={
                        "status": SemanticBindingStatus.RETIRED,
                        "updated_at": now,
                    }
                )
                connection.execute(
                    """
                    UPDATE semantic_bindings
                    SET payload = ?
                    WHERE tenant_id = ? AND binding_id = ?
                    """,
                    (retired.model_dump_json(), tenant_id, candidate_id),
                )
            activated = binding.model_copy(
                update={
                    "status": SemanticBindingStatus.ACTIVE,
                    "updated_at": now,
                }
            )
            connection.execute(
                """
                UPDATE semantic_bindings
                SET payload = ?
                WHERE tenant_id = ? AND binding_id = ?
                """,
                (activated.model_dump_json(), tenant_id, binding_id),
            )
            return activated

        return await self._write(operation)

    async def pin_conversation(
        self,
        pin: ConversationDataSourcePin,
    ) -> ConversationDataSourcePin:
        def operation(
            connection: sqlite3.Connection,
        ) -> ConversationDataSourcePin:
            row = connection.execute(
                """
                SELECT payload
                FROM conversation_datasource_pins
                WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?
                """,
                (pin.tenant_id, pin.user_id, pin.conversation_id),
            ).fetchone()
            if row is not None:
                current = ConversationDataSourcePin.model_validate_json(row[0])
                if current.model_dump(exclude={"created_at"}) != pin.model_dump(
                    exclude={"created_at"}
                ):
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.VERSION_CONFLICT,
                        "conversation is already pinned to another datasource binding",
                    )
                return current
            connection.execute(
                """
                INSERT INTO conversation_datasource_pins (
                    tenant_id, user_id, conversation_id, payload
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    pin.tenant_id,
                    pin.user_id,
                    pin.conversation_id,
                    pin.model_dump_json(),
                ),
            )
            return pin

        return await self._write(operation)

    async def get_conversation_pin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationDataSourcePin | None:
        def operation(
            connection: sqlite3.Connection,
        ) -> ConversationDataSourcePin | None:
            row = connection.execute(
                """
                SELECT payload
                FROM conversation_datasource_pins
                WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?
                """,
                (tenant_id, user_id, conversation_id),
            ).fetchone()
            return (
                ConversationDataSourcePin.model_validate_json(row[0])
                if row is not None
                else None
            )

        return await self._read(operation)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS data_sources (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS data_source_snapshots (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id, version)
                );
                CREATE TABLE IF NOT EXISTS semantic_bindings (
                    tenant_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, binding_id)
                );
                CREATE INDEX IF NOT EXISTS semantic_bindings_source_idx
                ON semantic_bindings (tenant_id, source_id, domain_id);
                CREATE TABLE IF NOT EXISTS conversation_datasource_pins (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, conversation_id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def _read(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        async with self._lock:
            return await asyncio.to_thread(self._run_read, operation)

    async def _write(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        async with self._lock:
            return await asyncio.to_thread(self._run_write, operation)

    def _run_read(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        with self._connect() as connection:
            return operation(connection)

    def _run_write(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return operation(connection)


__all__ = ["SQLiteDataSourceRegistry"]
