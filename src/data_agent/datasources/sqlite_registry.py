"""Durable SQLite implementation of the datasource control-plane registry."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, TypeVar

from data_agent.relationships.models import (
    RelationshipGraphDraft,
    RelationshipRecommendationRun,
)
from data_agent.semantic_metrics.digest import semantic_digest
from data_agent.semantic_metrics.models import (
    ActiveMetricSetPointer,
    ConversationMetricPin,
    DomainPackAssignment,
    MetricOverlay,
    MetricProposal,
    MetricSetRecord,
    MetricSetStatus,
    MetricValidationReport,
    SemanticAuditEvent,
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
            for table in (
                "metric_proposals",
                "metric_validation_reports",
                "metric_sets",
                "active_metric_sets",
                "metric_overlays",
                "conversation_metric_pins",
                "domain_pack_assignments",
                "semantic_audit_events",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ? AND source_id = ?",
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

    async def save_metric_proposal(self, proposal: MetricProposal) -> MetricProposal:
        def operation(connection: sqlite3.Connection) -> MetricProposal:
            snapshot_row = connection.execute(
                "SELECT payload FROM data_source_snapshots WHERE tenant_id=? AND source_id=? AND version=?",
                (
                    proposal.tenant_id,
                    proposal.source_id,
                    proposal.source_snapshot_version,
                ),
            ).fetchone()
            snapshot = (
                DataSourceSnapshot.model_validate_json(snapshot_row[0])
                if snapshot_row
                else None
            )
            binding = self._active_binding_authority(
                connection,
                tenant_id=proposal.tenant_id,
                source_id=proposal.source_id,
                binding_id=proposal.base_binding_id,
                binding_version=proposal.base_binding_version,
            )
            if (
                snapshot is None
                or snapshot.fingerprint != proposal.schema_fingerprint
                or binding is None
                or binding.domain_id != proposal.domain_id
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric proposal authority is stale or unavailable",
                )
            if proposal.revision != 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "new metric proposals must start at revision 1",
                )
            try:
                connection.execute(
                    """INSERT INTO metric_proposals
                    (tenant_id, proposal_id, source_id, domain_id, revision, status, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        proposal.tenant_id,
                        proposal.proposal_id,
                        proposal.source_id,
                        proposal.domain_id,
                        proposal.revision,
                        proposal.status.value,
                        proposal.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "metric proposal already exists",
                ) from exc
            return proposal

        return await self._write(operation)

    async def update_metric_proposal(
        self,
        proposal: MetricProposal,
        *,
        expected_revision: int,
    ) -> MetricProposal:
        def operation(connection: sqlite3.Connection) -> MetricProposal:
            row = connection.execute(
                "SELECT payload FROM metric_proposals WHERE tenant_id=? AND proposal_id=?",
                (proposal.tenant_id, proposal.proposal_id),
            ).fetchone()
            if row is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "metric proposal was not found",
                )
            current = MetricProposal.model_validate_json(row[0])
            if (
                current.revision != expected_revision
                or proposal.revision != expected_revision + 1
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "metric proposal revision is stale",
                )
            immutable = (
                "tenant_id",
                "source_id",
                "source_snapshot_version",
                "schema_fingerprint",
                "domain_id",
                "base_binding_id",
                "base_binding_version",
                "requested_term",
                "created_by",
                "created_at",
            )
            if any(getattr(current, field) != getattr(proposal, field) for field in immutable):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric proposal cannot change its base authority",
                )
            cursor = connection.execute(
                """UPDATE metric_proposals
                SET revision=?, status=?, payload=?
                WHERE tenant_id=? AND proposal_id=? AND revision=?""",
                (
                    proposal.revision,
                    proposal.status.value,
                    proposal.model_dump_json(),
                    proposal.tenant_id,
                    proposal.proposal_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "metric proposal revision is stale",
                )
            return proposal

        return await self._write(operation)

    async def get_metric_proposal(
        self,
        *,
        tenant_id: str,
        proposal_id: str,
    ) -> MetricProposal | None:
        def operation(connection: sqlite3.Connection) -> MetricProposal | None:
            row = connection.execute(
                "SELECT payload FROM metric_proposals WHERE tenant_id=? AND proposal_id=?",
                (tenant_id, proposal_id),
            ).fetchone()
            return MetricProposal.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def list_metric_proposals(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[MetricProposal, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[MetricProposal, ...]:
            rows = connection.execute(
                """SELECT payload FROM metric_proposals
                WHERE tenant_id=? AND source_id=? ORDER BY proposal_id""",
                (tenant_id, source_id),
            ).fetchall()
            return tuple(MetricProposal.model_validate_json(row[0]) for row in rows)

        return await self._read(operation)

    async def save_metric_validation_report(
        self,
        report: MetricValidationReport,
    ) -> MetricValidationReport:
        def operation(connection: sqlite3.Connection) -> MetricValidationReport:
            proposal_row = connection.execute(
                "SELECT payload FROM metric_proposals WHERE tenant_id=? AND proposal_id=?",
                (report.tenant_id, report.proposal_id),
            ).fetchone()
            proposal = (
                MetricProposal.model_validate_json(proposal_row[0])
                if proposal_row
                else None
            )
            candidate = next(
                (
                    item
                    for item in proposal.candidates
                    if item.candidate_id == report.candidate_id
                ),
                None,
            ) if proposal is not None else None
            if (
                proposal is None
                or proposal.revision != report.proposal_revision
                or proposal.content_digest != report.proposal_digest
                or candidate is None
                or semantic_digest(candidate.definition) != report.definition_digest
                or proposal.source_id != report.source_id
                or proposal.source_snapshot_version != report.source_snapshot_version
                or proposal.schema_fingerprint != report.schema_fingerprint
                or proposal.base_binding_id != report.binding_id
                or proposal.base_binding_version != report.binding_version
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric validation report does not match its proposal authority",
                )
            try:
                connection.execute(
                    """INSERT INTO metric_validation_reports
                    (tenant_id, report_id, proposal_id, candidate_id, source_id, payload)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        report.tenant_id,
                        report.report_id,
                        report.proposal_id,
                        report.candidate_id,
                        report.source_id,
                        report.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "metric validation report already exists",
                ) from exc
            return report

        return await self._write(operation)

    async def get_metric_validation_report(
        self,
        *,
        tenant_id: str,
        report_id: str,
    ) -> MetricValidationReport | None:
        def operation(connection: sqlite3.Connection) -> MetricValidationReport | None:
            row = connection.execute(
                "SELECT payload FROM metric_validation_reports WHERE tenant_id=? AND report_id=?",
                (tenant_id, report_id),
            ).fetchone()
            return MetricValidationReport.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def save_metric_set(self, metric_set: MetricSetRecord) -> MetricSetRecord:
        def operation(connection: sqlite3.Connection) -> MetricSetRecord:
            snapshot_row = connection.execute(
                "SELECT payload FROM data_source_snapshots WHERE tenant_id=? AND source_id=? AND version=?",
                (
                    metric_set.tenant_id,
                    metric_set.source_id,
                    metric_set.source_snapshot_version,
                ),
            ).fetchone()
            snapshot = (
                DataSourceSnapshot.model_validate_json(snapshot_row[0])
                if snapshot_row
                else None
            )
            binding = self._active_binding_authority(
                connection,
                tenant_id=metric_set.tenant_id,
                source_id=metric_set.source_id,
                binding_id=metric_set.binding_id,
                binding_version=metric_set.binding_version,
            )
            if (
                snapshot is None
                or snapshot.fingerprint != metric_set.schema_fingerprint
                or binding is None
                or binding.domain_id != metric_set.domain_id
                or metric_set.status != MetricSetStatus.DRAFT
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric set authority is stale or the new version is not a draft",
                )
            row = connection.execute(
                """SELECT MAX(version) FROM metric_sets
                WHERE tenant_id=? AND metric_set_id=?""",
                (metric_set.tenant_id, metric_set.metric_set_id),
            ).fetchone()
            next_version = (row[0] or 0) + 1
            if metric_set.version != next_version:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "metric set version is not the next version",
                )
            logical_refs = {item.logical_ref for item in binding.mappings}
            unknown = sorted(
                {
                    ref
                    for definition in metric_set.definitions
                    for ref in definition.all_field_refs
                    if ref not in logical_refs
                }
            )
            if unknown:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric set references unknown logical fields: " + ", ".join(unknown),
                )
            try:
                connection.execute(
                    """INSERT INTO metric_sets
                    (tenant_id, metric_set_id, version, source_id, domain_id,
                     binding_id, binding_version, status, content_digest, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        metric_set.tenant_id,
                        metric_set.metric_set_id,
                        metric_set.version,
                        metric_set.source_id,
                        metric_set.domain_id,
                        metric_set.binding_id,
                        metric_set.binding_version,
                        metric_set.status.value,
                        metric_set.content_digest,
                        metric_set.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "metric set version already exists",
                ) from exc
            return metric_set

        return await self._write(operation)

    async def get_metric_set(
        self,
        *,
        tenant_id: str,
        metric_set_id: str,
        version: int,
    ) -> MetricSetRecord | None:
        def operation(connection: sqlite3.Connection) -> MetricSetRecord | None:
            row = connection.execute(
                """SELECT payload FROM metric_sets
                WHERE tenant_id=? AND metric_set_id=? AND version=?""",
                (tenant_id, metric_set_id, version),
            ).fetchone()
            return MetricSetRecord.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def list_metric_sets(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> tuple[MetricSetRecord, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[MetricSetRecord, ...]:
            rows = connection.execute(
                """SELECT payload FROM metric_sets
                WHERE tenant_id=? AND source_id=? AND domain_id=?
                ORDER BY metric_set_id, version""",
                (tenant_id, source_id, domain_id),
            ).fetchall()
            return tuple(MetricSetRecord.model_validate_json(row[0]) for row in rows)

        return await self._read(operation)

    async def activate_metric_set(
        self,
        *,
        tenant_id: str,
        metric_set_id: str,
        version: int,
        expected_pointer_revision: int,
    ) -> ActiveMetricSetPointer:
        def operation(connection: sqlite3.Connection) -> ActiveMetricSetPointer:
            row = connection.execute(
                """SELECT payload FROM metric_sets
                WHERE tenant_id=? AND metric_set_id=? AND version=?""",
                (tenant_id, metric_set_id, version),
            ).fetchone()
            if row is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "metric set version was not found",
                )
            metric_set = MetricSetRecord.model_validate_json(row[0])
            if metric_set.status == MetricSetStatus.REVOKED:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_SET_REVOKED,
                    "revoked metric sets cannot be activated",
                )
            pointer_row = connection.execute(
                """SELECT payload FROM active_metric_sets
                WHERE tenant_id=? AND source_id=? AND domain_id=?""",
                (tenant_id, metric_set.source_id, metric_set.domain_id),
            ).fetchone()
            current = (
                ActiveMetricSetPointer.model_validate_json(pointer_row[0])
                if pointer_row
                else None
            )
            current_revision = current.revision if current else 0
            if current_revision != expected_pointer_revision:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "active metric set pointer revision is stale",
                )
            if metric_set.status != MetricSetStatus.PUBLISHED:
                metric_set = metric_set.model_copy(
                    update={
                        "status": MetricSetStatus.PUBLISHED,
                        "updated_at": datetime.now(UTC),
                    }
                )
                connection.execute(
                    """UPDATE metric_sets SET status=?, payload=?
                    WHERE tenant_id=? AND metric_set_id=? AND version=?""",
                    (
                        metric_set.status.value,
                        metric_set.model_dump_json(),
                        tenant_id,
                        metric_set_id,
                        version,
                    ),
                )
            pointer = ActiveMetricSetPointer(
                tenant_id=tenant_id,
                source_id=metric_set.source_id,
                domain_id=metric_set.domain_id,
                binding_id=metric_set.binding_id,
                binding_version=metric_set.binding_version,
                metric_set_id=metric_set.metric_set_id,
                metric_set_version=metric_set.version,
                metric_set_digest=metric_set.content_digest,
                revision=current_revision + 1,
            )
            if current is None:
                connection.execute(
                    """INSERT INTO active_metric_sets
                    (tenant_id, source_id, domain_id, revision, payload)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        tenant_id,
                        metric_set.source_id,
                        metric_set.domain_id,
                        pointer.revision,
                        pointer.model_dump_json(),
                    ),
                )
            else:
                cursor = connection.execute(
                    """UPDATE active_metric_sets SET revision=?, payload=?
                    WHERE tenant_id=? AND source_id=? AND domain_id=? AND revision=?""",
                    (
                        pointer.revision,
                        pointer.model_dump_json(),
                        tenant_id,
                        metric_set.source_id,
                        metric_set.domain_id,
                        expected_pointer_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                        "active metric set pointer revision is stale",
                    )
            return pointer

        return await self._write(operation)

    async def get_active_metric_set(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> ActiveMetricSetPointer | None:
        def operation(connection: sqlite3.Connection) -> ActiveMetricSetPointer | None:
            row = connection.execute(
                """SELECT payload FROM active_metric_sets
                WHERE tenant_id=? AND source_id=? AND domain_id=?""",
                (tenant_id, source_id, domain_id),
            ).fetchone()
            return ActiveMetricSetPointer.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def save_metric_overlay(self, overlay: MetricOverlay) -> MetricOverlay:
        def operation(connection: sqlite3.Connection) -> MetricOverlay:
            proposal_row = connection.execute(
                "SELECT payload FROM metric_proposals WHERE tenant_id=? AND proposal_id=?",
                (overlay.tenant_id, overlay.proposal_id),
            ).fetchone()
            report_row = connection.execute(
                "SELECT payload FROM metric_validation_reports WHERE tenant_id=? AND report_id=?",
                (overlay.tenant_id, overlay.validation_report_id),
            ).fetchone()
            proposal = (
                MetricProposal.model_validate_json(proposal_row[0])
                if proposal_row
                else None
            )
            report = (
                MetricValidationReport.model_validate_json(report_row[0])
                if report_row
                else None
            )
            candidate = next(
                (
                    item
                    for item in proposal.candidates
                    if item.candidate_id == proposal.selected_candidate_id
                ),
                None,
            ) if proposal is not None else None
            context = overlay.base_context
            if (
                proposal is None
                or proposal.revision != overlay.proposal_revision
                or proposal.content_digest != overlay.proposal_digest
                or candidate is None
                or candidate.definition != overlay.definition
                or report is None
                or report.digest != overlay.validation_report_digest
                or not report.activation_allowed
                or report.candidate_id != candidate.candidate_id
                or context.tenant_id != proposal.tenant_id
                or context.source_id != proposal.source_id
                or context.source_version != proposal.source_snapshot_version
                or context.binding_id != proposal.base_binding_id
                or context.binding_version != proposal.base_binding_version
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "metric overlay does not match an approved validation authority",
                )
            if context.metric_set is not None:
                metric_set_row = connection.execute(
                    """SELECT payload FROM metric_sets
                    WHERE tenant_id=? AND metric_set_id=? AND version=?""",
                    (
                        overlay.tenant_id,
                        context.metric_set.metric_set_id,
                        context.metric_set.version,
                    ),
                ).fetchone()
                metric_set = (
                    MetricSetRecord.model_validate_json(metric_set_row[0])
                    if metric_set_row
                    else None
                )
                if (
                    metric_set is None
                    or metric_set.content_digest != context.metric_set.digest
                    or any(
                        item.metric_ref.casefold()
                        == overlay.definition.metric_ref.casefold()
                        for item in metric_set.definitions
                    )
                ):
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                        "metric overlay cannot shadow a governed metric",
                    )
            try:
                connection.execute(
                    """INSERT INTO metric_overlays
                    (tenant_id, overlay_id, source_id, user_id, scope, revision,
                     expires_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        overlay.tenant_id,
                        overlay.overlay_id,
                        overlay.base_context.source_id,
                        overlay.user_id,
                        overlay.scope,
                        overlay.revision,
                        overlay.expires_at.isoformat(),
                        overlay.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "metric overlay already exists",
                ) from exc
            return overlay

        return await self._write(operation)

    async def get_metric_overlay(
        self,
        *,
        tenant_id: str,
        overlay_id: str,
    ) -> MetricOverlay | None:
        def operation(connection: sqlite3.Connection) -> MetricOverlay | None:
            row = connection.execute(
                "SELECT payload FROM metric_overlays WHERE tenant_id=? AND overlay_id=?",
                (tenant_id, overlay_id),
            ).fetchone()
            return MetricOverlay.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def get_run_metric_overlay(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> MetricOverlay | None:
        def operation(connection: sqlite3.Connection) -> MetricOverlay | None:
            rows = connection.execute(
                """SELECT payload FROM metric_overlays
                WHERE tenant_id=? AND user_id=? AND scope='run' AND expires_at>?
                ORDER BY expires_at DESC""",
                (tenant_id, user_id, datetime.now(UTC).isoformat()),
            ).fetchall()
            matches = tuple(
                overlay
                for row in rows
                for overlay in (MetricOverlay.model_validate_json(row[0]),)
                if overlay.run_id == run_id and overlay.revoked_at is None
            )
            if len(matches) > 1:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "run has more than one active metric overlay",
                )
            return matches[0] if matches else None

        return await self._read(operation)

    async def put_conversation_metric_pin(
        self,
        pin: ConversationMetricPin,
        *,
        expected_revision: int,
    ) -> ConversationMetricPin:
        def operation(connection: sqlite3.Connection) -> ConversationMetricPin:
            row = connection.execute(
                """SELECT payload FROM conversation_metric_pins
                WHERE tenant_id=? AND user_id=? AND conversation_id=?""",
                (pin.tenant_id, pin.user_id, pin.conversation_id),
            ).fetchone()
            current = ConversationMetricPin.model_validate_json(row[0]) if row else None
            current_revision = current.revision if current else 0
            if (
                current_revision != expected_revision
                or pin.revision != expected_revision + 1
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "conversation metric pin revision is stale",
                )
            if current is not None and pin.created_at != current.created_at:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "conversation metric repin must preserve created_at",
                )
            context = pin.context
            if context.metric_set is not None:
                metric_set_row = connection.execute(
                    """SELECT payload FROM metric_sets
                    WHERE tenant_id=? AND metric_set_id=? AND version=?""",
                    (
                        pin.tenant_id,
                        context.metric_set.metric_set_id,
                        context.metric_set.version,
                    ),
                ).fetchone()
                metric_set = (
                    MetricSetRecord.model_validate_json(metric_set_row[0])
                    if metric_set_row
                    else None
                )
                if (
                    metric_set is None
                    or metric_set.content_digest != context.metric_set.digest
                    or metric_set.status == MetricSetStatus.REVOKED
                    or metric_set.source_id != context.source_id
                    or metric_set.binding_id != context.binding_id
                    or metric_set.domain_id != pin.domain_id
                ):
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                        "conversation metric set pin is unavailable or stale",
                    )
            if context.overlay_id is not None:
                overlay_row = connection.execute(
                    "SELECT payload FROM metric_overlays WHERE tenant_id=? AND overlay_id=?",
                    (pin.tenant_id, context.overlay_id),
                ).fetchone()
                overlay = (
                    MetricOverlay.model_validate_json(overlay_row[0])
                    if overlay_row
                    else None
                )
                if (
                    overlay is None
                    or overlay.content_digest != context.overlay_digest
                    or overlay.user_id != pin.user_id
                    or overlay.scope != "conversation"
                    or overlay.conversation_id != pin.conversation_id
                    or overlay.revoked_at is not None
                    or overlay.expires_at <= datetime.now(UTC)
                ):
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                        "conversation metric overlay is unavailable or stale",
                    )
            if current is None:
                connection.execute(
                    """INSERT INTO conversation_metric_pins
                    (tenant_id, user_id, conversation_id, source_id, revision, payload)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        pin.tenant_id,
                        pin.user_id,
                        pin.conversation_id,
                        pin.context.source_id,
                        pin.revision,
                        pin.model_dump_json(),
                    ),
                )
            else:
                cursor = connection.execute(
                    """UPDATE conversation_metric_pins SET source_id=?, revision=?, payload=?
                    WHERE tenant_id=? AND user_id=? AND conversation_id=? AND revision=?""",
                    (
                        pin.context.source_id,
                        pin.revision,
                        pin.model_dump_json(),
                        pin.tenant_id,
                        pin.user_id,
                        pin.conversation_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                        "conversation metric pin revision is stale",
                    )
            return pin

        return await self._write(operation)

    async def get_conversation_metric_pin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationMetricPin | None:
        def operation(connection: sqlite3.Connection) -> ConversationMetricPin | None:
            row = connection.execute(
                """SELECT payload FROM conversation_metric_pins
                WHERE tenant_id=? AND user_id=? AND conversation_id=?""",
                (tenant_id, user_id, conversation_id),
            ).fetchone()
            return ConversationMetricPin.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def put_domain_pack_assignment(
        self,
        assignment: DomainPackAssignment,
        *,
        expected_revision: int,
    ) -> DomainPackAssignment:
        def operation(connection: sqlite3.Connection) -> DomainPackAssignment:
            source = connection.execute(
                "SELECT 1 FROM data_sources WHERE tenant_id=? AND source_id=?",
                (assignment.tenant_id, assignment.source_id),
            ).fetchone()
            if source is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "domain pack assignment datasource was not found",
                )
            row = connection.execute(
                """SELECT payload FROM domain_pack_assignments
                WHERE tenant_id=? AND source_id=? AND domain_id=?""",
                (assignment.tenant_id, assignment.source_id, assignment.domain_id),
            ).fetchone()
            current = DomainPackAssignment.model_validate_json(row[0]) if row else None
            current_revision = current.revision if current else 0
            if (
                current_revision != expected_revision
                or assignment.revision != expected_revision + 1
            ):
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                    "domain pack assignment revision is stale",
                )
            if current is not None and assignment.created_at != current.created_at:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_METRIC_STATE,
                    "domain pack reassignment must preserve created_at",
                )
            if current is None:
                connection.execute(
                    """INSERT INTO domain_pack_assignments
                    (tenant_id, source_id, domain_id, revision, payload)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        assignment.tenant_id,
                        assignment.source_id,
                        assignment.domain_id,
                        assignment.revision,
                        assignment.model_dump_json(),
                    ),
                )
            else:
                cursor = connection.execute(
                    """UPDATE domain_pack_assignments SET revision=?, payload=?
                    WHERE tenant_id=? AND source_id=? AND domain_id=? AND revision=?""",
                    (
                        assignment.revision,
                        assignment.model_dump_json(),
                        assignment.tenant_id,
                        assignment.source_id,
                        assignment.domain_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.METRIC_REVISION_CONFLICT,
                        "domain pack assignment revision is stale",
                    )
            return assignment

        return await self._write(operation)

    async def get_domain_pack_assignment(
        self,
        *,
        tenant_id: str,
        source_id: str,
        domain_id: str,
    ) -> DomainPackAssignment | None:
        def operation(connection: sqlite3.Connection) -> DomainPackAssignment | None:
            row = connection.execute(
                """SELECT payload FROM domain_pack_assignments
                WHERE tenant_id=? AND source_id=? AND domain_id=?""",
                (tenant_id, source_id, domain_id),
            ).fetchone()
            return DomainPackAssignment.model_validate_json(row[0]) if row else None

        return await self._read(operation)

    async def append_semantic_audit_event(
        self,
        event: SemanticAuditEvent,
    ) -> SemanticAuditEvent:
        def operation(connection: sqlite3.Connection) -> SemanticAuditEvent:
            try:
                connection.execute(
                    """INSERT INTO semantic_audit_events
                    (tenant_id, event_id, source_id, resource_type, resource_id,
                     created_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.tenant_id,
                        event.event_id,
                        event.source_id,
                        event.resource_type,
                        event.resource_id,
                        event.created_at.isoformat(),
                        event.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "semantic audit event already exists",
                ) from exc
            return event

        return await self._write(operation)

    async def list_semantic_audit_events(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> tuple[SemanticAuditEvent, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[SemanticAuditEvent, ...]:
            rows = connection.execute(
                """SELECT payload FROM semantic_audit_events
                WHERE tenant_id=? AND resource_type=? AND resource_id=?
                ORDER BY created_at, event_id""",
                (tenant_id, resource_type, resource_id),
            ).fetchall()
            return tuple(SemanticAuditEvent.model_validate_json(row[0]) for row in rows)

        return await self._read(operation)

    @staticmethod
    def _active_binding_authority(
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        source_id: str,
        binding_id: str,
        binding_version: int,
    ) -> SemanticBindingRecord | SemanticGraphBindingRecord | None:
        row = connection.execute(
            "SELECT payload FROM semantic_bindings WHERE tenant_id=? AND binding_id=?",
            (tenant_id, binding_id),
        ).fetchone()
        binding: SemanticBindingRecord | SemanticGraphBindingRecord | None = (
            SemanticBindingRecord.model_validate_json(row[0]) if row else None
        )
        if binding is None:
            row = connection.execute(
                "SELECT payload FROM semantic_graph_bindings WHERE tenant_id=? AND binding_id=?",
                (tenant_id, binding_id),
            ).fetchone()
            binding = (
                SemanticGraphBindingRecord.model_validate_json(row[0])
                if row
                else None
            )
        if (
            binding is None
            or binding.source_id != source_id
            or binding.version != binding_version
            or binding.status != SemanticBindingStatus.ACTIVE
        ):
            return None
        return binding

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
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
                CREATE TABLE IF NOT EXISTS control_schema_migrations (
                    version INTEGER NOT NULL PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO control_schema_migrations
                    (version, description, applied_at)
                VALUES
                    (1, 'legacy datasource control-plane baseline', CURRENT_TIMESTAMP);

                CREATE TABLE IF NOT EXISTS metric_proposals (
                    tenant_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, proposal_id)
                );
                CREATE INDEX IF NOT EXISTS metric_proposals_source_idx
                ON metric_proposals (tenant_id, source_id, domain_id, status);

                CREATE TABLE IF NOT EXISTS metric_validation_reports (
                    tenant_id TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, report_id)
                );
                CREATE INDEX IF NOT EXISTS metric_validation_reports_proposal_idx
                ON metric_validation_reports (tenant_id, proposal_id, candidate_id);

                CREATE TABLE IF NOT EXISTS metric_sets (
                    tenant_id TEXT NOT NULL,
                    metric_set_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    binding_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, metric_set_id, version)
                );
                CREATE INDEX IF NOT EXISTS metric_sets_source_idx
                ON metric_sets (tenant_id, source_id, domain_id, status);

                CREATE TABLE IF NOT EXISTS active_metric_sets (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id, domain_id)
                );

                CREATE TABLE IF NOT EXISTS metric_overlays (
                    tenant_id TEXT NOT NULL,
                    overlay_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, overlay_id)
                );
                CREATE INDEX IF NOT EXISTS metric_overlays_scope_idx
                ON metric_overlays (tenant_id, user_id, scope, expires_at);

                CREATE TABLE IF NOT EXISTS conversation_metric_pins (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS domain_pack_assignments (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id, domain_id)
                );

                CREATE TABLE IF NOT EXISTS semantic_audit_events (
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    source_id TEXT,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS semantic_audit_resource_idx
                ON semantic_audit_events
                    (tenant_id, resource_type, resource_id, created_at);

                INSERT OR IGNORE INTO control_schema_migrations
                    (version, description, applied_at)
                VALUES
                    (2, 'semantic metric governance control plane', CURRENT_TIMESTAMP);
                """
            )
            connection.commit()

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
        with closing(self._connect()) as connection:
            return operation(connection)

    def _run_write(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = operation(connection)
            connection.commit()
            return result


__all__ = ["SQLiteDataSourceRegistry"]
