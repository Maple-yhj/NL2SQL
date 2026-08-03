"""Durable SQLite implementation of the datasource control-plane registry."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, TypeVar

from data_agent.relationships.models import (
    RelationshipGraphDraft,
    RelationshipRecommendationRun,
)

from .models import (
    ConversationDataSourcePin,
    DataSourceDefinition,
    DataSourceSnapshot,
    DataSourceStatus,
    SemanticBindingRecord,
    SemanticBindingStatus,
    SemanticGraphBindingRecord,
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

    async def delete(
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
            source = DataSourceDefinition.model_validate_json(row[0])
            pin_rows = connection.execute(
                """
                SELECT user_id, conversation_id, payload
                FROM conversation_datasource_pins
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchall()
            for user_id, conversation_id, payload in pin_rows:
                pin = ConversationDataSourcePin.model_validate_json(payload)
                if pin.source_id != source_id:
                    continue
                connection.execute(
                    """
                    DELETE FROM conversation_datasource_pins
                    WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?
                    """,
                    (tenant_id, user_id, conversation_id),
                )
            connection.execute(
                "DELETE FROM semantic_bindings WHERE tenant_id = ? AND source_id = ?",
                (tenant_id, source_id),
            )
            connection.execute(
                "DELETE FROM relationship_graph_drafts WHERE tenant_id = ? AND source_id = ?",
                (tenant_id, source_id),
            )
            connection.execute(
                "DELETE FROM relationship_recommendation_runs WHERE tenant_id = ? AND source_id = ?",
                (tenant_id, source_id),
            )
            connection.execute(
                "DELETE FROM semantic_graph_bindings WHERE tenant_id = ? AND source_id = ?",
                (tenant_id, source_id),
            )
            connection.execute(
                "DELETE FROM data_source_snapshots WHERE tenant_id = ? AND source_id = ?",
                (tenant_id, source_id),
            )
            connection.execute(
                "DELETE FROM data_sources WHERE tenant_id = ? AND source_id = ?",
                (tenant_id, source_id),
            )
            return source

        return await self._write(operation)

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
            invalid_relationships = tuple(
                relationship.relationship_id
                for relationship in binding.relationships
                if relationship.left_relation not in catalog
                or relationship.left_column
                not in catalog.get(relationship.left_relation, set())
                or relationship.right_relation not in catalog
                or relationship.right_column
                not in catalog.get(relationship.right_relation, set())
            )
            if invalid or invalid_relationships:
                details = [
                    *invalid,
                    *(f"relationship:{item}" for item in invalid_relationships),
                ]
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_BINDING,
                    "semantic binding references unknown physical fields: "
                    + ", ".join(details),
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

    async def create_graph_draft(
        self, draft: RelationshipGraphDraft
    ) -> RelationshipGraphDraft:
        def operation(connection: sqlite3.Connection) -> RelationshipGraphDraft:
            snapshot = connection.execute(
                """SELECT 1 FROM data_source_snapshots
                WHERE tenant_id = ? AND source_id = ? AND version = ?""",
                (draft.tenant_id, draft.source_id, draft.source_snapshot_version),
            ).fetchone()
            if snapshot is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "relationship graph source snapshot was not found",
                )
            if draft.revision != 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "new relationship graph drafts must start at revision 1",
                )
            try:
                connection.execute(
                    """INSERT INTO relationship_graph_drafts
                    (tenant_id, graph_id, source_id, source_snapshot_version, revision, status, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        draft.tenant_id, draft.graph_id, draft.source_id,
                        draft.source_snapshot_version, draft.revision, draft.status,
                        draft.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "relationship graph draft already exists",
                ) from exc
            return draft

        return await self._write(operation)

    async def get_graph_draft(
        self,
        *,
        tenant_id: str,
        source_id: str,
        source_snapshot_version: int,
    ) -> RelationshipGraphDraft | None:
        def operation(connection: sqlite3.Connection) -> RelationshipGraphDraft | None:
            row = connection.execute(
                """SELECT payload FROM relationship_graph_drafts
                WHERE tenant_id = ? AND source_id = ? AND source_snapshot_version = ?
                ORDER BY graph_id LIMIT 1""",
                (tenant_id, source_id, source_snapshot_version),
            ).fetchone()
            return RelationshipGraphDraft.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def save_graph_draft(
        self,
        draft: RelationshipGraphDraft,
        *,
        expected_revision: int,
    ) -> RelationshipGraphDraft:
        def operation(connection: sqlite3.Connection) -> RelationshipGraphDraft:
            current = connection.execute(
                """SELECT source_id, source_snapshot_version, revision, payload
                FROM relationship_graph_drafts WHERE tenant_id = ? AND graph_id = ?""",
                (draft.tenant_id, draft.graph_id),
            ).fetchone()
            if current is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "relationship graph draft was not found",
                )
            if int(current[2]) != expected_revision or draft.revision != expected_revision + 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.GRAPH_REVISION_CONFLICT,
                    "relationship graph draft revision is stale",
                )
            existing = RelationshipGraphDraft.model_validate_json(current[3])
            if (
                existing.source_id != draft.source_id
                or existing.source_snapshot_version != draft.source_snapshot_version
                or existing.schema_fingerprint != draft.schema_fingerprint
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "relationship graph draft cannot change its source snapshot",
                )
            changed = connection.execute(
                """UPDATE relationship_graph_drafts
                SET revision = ?, status = ?, payload = ?
                WHERE tenant_id = ? AND graph_id = ? AND revision = ?""",
                (
                    draft.revision, draft.status, draft.model_dump_json(),
                    draft.tenant_id, draft.graph_id, expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.GRAPH_REVISION_CONFLICT,
                    "relationship graph draft revision is stale",
                )
            return draft

        return await self._write(operation)

    async def save_recommendation_run(
        self, run: RelationshipRecommendationRun
    ) -> RelationshipRecommendationRun:
        def operation(connection: sqlite3.Connection) -> RelationshipRecommendationRun:
            graph = connection.execute(
                """SELECT payload FROM relationship_graph_drafts
                WHERE tenant_id = ? AND graph_id = ?""",
                (run.tenant_id, run.graph_id),
            ).fetchone()
            if graph is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "recommendation run graph draft was not found",
                )
            draft = RelationshipGraphDraft.model_validate_json(graph[0])
            if (
                draft.source_id != run.source_id
                or draft.source_snapshot_version != run.source_snapshot_version
                or draft.schema_fingerprint != run.schema_fingerprint
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "recommendation run does not match its graph draft",
                )
            connection.execute(
                """INSERT INTO relationship_recommendation_runs
                (tenant_id, run_id, source_id, source_snapshot_version, status, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, run_id) DO UPDATE SET
                    status = excluded.status, payload = excluded.payload""",
                (
                    run.tenant_id, run.run_id, run.source_id,
                    run.source_snapshot_version, run.status, run.model_dump_json(),
                ),
            )
            return run

        return await self._write(operation)

    async def get_recommendation_run(
        self, *, tenant_id: str, run_id: str
    ) -> RelationshipRecommendationRun | None:
        def operation(connection: sqlite3.Connection) -> RelationshipRecommendationRun | None:
            row = connection.execute(
                """SELECT payload FROM relationship_recommendation_runs
                WHERE tenant_id = ? AND run_id = ?""",
                (tenant_id, run_id),
            ).fetchone()
            return RelationshipRecommendationRun.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def activate_graph_binding(self, binding: SemanticGraphBindingRecord) -> SemanticGraphBindingRecord:
        def operation(connection: sqlite3.Connection) -> SemanticGraphBindingRecord:
            snapshot = connection.execute("SELECT payload FROM data_source_snapshots WHERE tenant_id=? AND source_id=? AND version=?", (binding.tenant_id, binding.source_id, binding.source_snapshot_version)).fetchone()
            if snapshot is None or DataSourceSnapshot.model_validate_json(snapshot[0]).fingerprint != binding.schema_fingerprint:
                raise DataSourceRegistryError(DataSourceRegistryErrorCode.GRAPH_STALE_SNAPSHOT, "graph binding source snapshot is stale")
            current = connection.execute("SELECT 1 FROM semantic_graph_bindings WHERE tenant_id=? AND binding_id=?", (binding.tenant_id, binding.binding_id)).fetchone()
            if current:
                raise DataSourceRegistryError(DataSourceRegistryErrorCode.ALREADY_EXISTS, "graph binding already exists")
            now = datetime.now(UTC)
            legacy_rows = connection.execute(
                "SELECT binding_id, payload FROM semantic_bindings WHERE tenant_id=? AND source_id=? AND domain_id=?",
                (binding.tenant_id, binding.source_id, binding.domain_id),
            ).fetchall()
            for binding_id, payload in legacy_rows:
                previous = SemanticBindingRecord.model_validate_json(payload)
                if previous.status == SemanticBindingStatus.ACTIVE:
                    retired = previous.model_copy(
                        update={"status": SemanticBindingStatus.RETIRED, "updated_at": now}
                    )
                    connection.execute(
                        "UPDATE semantic_bindings SET payload=? WHERE tenant_id=? AND binding_id=?",
                        (retired.model_dump_json(), binding.tenant_id, binding_id),
                    )
            rows = connection.execute("SELECT binding_id, payload FROM semantic_graph_bindings WHERE tenant_id=? AND source_id=? AND domain_id=?", (binding.tenant_id, binding.source_id, binding.domain_id)).fetchall()
            for binding_id, payload in rows:
                previous = SemanticGraphBindingRecord.model_validate_json(payload)
                if previous.status == SemanticBindingStatus.ACTIVE:
                    retired = previous.model_copy(update={"status": SemanticBindingStatus.RETIRED, "updated_at": now})
                    connection.execute("UPDATE semantic_graph_bindings SET payload=? WHERE tenant_id=? AND binding_id=?", (retired.model_dump_json(), binding.tenant_id, binding_id))
            active = binding.model_copy(update={"status": SemanticBindingStatus.ACTIVE, "updated_at": now})
            connection.execute("INSERT INTO semantic_graph_bindings (tenant_id,binding_id,source_id,domain_id,payload) VALUES (?,?,?,?,?)", (active.tenant_id, active.binding_id, active.source_id, active.domain_id, active.model_dump_json()))
            return active
        return await self._write(operation)

    async def list_graph_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticGraphBindingRecord, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[SemanticGraphBindingRecord, ...]:
            rows = connection.execute(
                """SELECT payload FROM semantic_graph_bindings
                WHERE tenant_id = ? AND source_id = ? ORDER BY binding_id""",
                (tenant_id, source_id),
            ).fetchall()
            return tuple(
                SemanticGraphBindingRecord.model_validate_json(row[0])
                for row in rows
            )

        return await self._read(operation)

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
                CREATE TABLE IF NOT EXISTS relationship_graph_drafts (
                    tenant_id TEXT NOT NULL,
                    graph_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_snapshot_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, graph_id)
                );
                CREATE INDEX IF NOT EXISTS relationship_graph_drafts_source_idx
                ON relationship_graph_drafts (tenant_id, source_id, source_snapshot_version);
                CREATE TABLE IF NOT EXISTS relationship_recommendation_runs (
                    tenant_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_snapshot_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS semantic_graph_bindings (
                    tenant_id TEXT NOT NULL, binding_id TEXT NOT NULL,
                    source_id TEXT NOT NULL, domain_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, binding_id)
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
