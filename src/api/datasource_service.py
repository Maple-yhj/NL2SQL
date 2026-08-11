"""Application service for tenant-isolated datasource registration and uploads."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import UploadFile

from data_agent.datasources import (
    ConnectorRegistry,
    ConversationDataSourcePin,
    DataSourceDefinition,
    DataSourceKind,
    DataSourceRegistry,
    DataSourceRegistryError,
    DataSourceRegistryErrorCode,
    DataSourceSnapshot,
    FileSnapshotImporter,
    SemanticBindingRecord,
    SemanticGraphBindingRecord,
    SemanticGraphFieldMapping,
    SemanticMetricDefinition,
    SemanticBindingStatus,
    SemanticFieldMapping,
    SemanticRelationship,
    SQLiteSnapshotImporter,
    SQLiteDataSourceRegistry,
)
from data_agent.tools.connectors import (
    DataSourceConnector,
    DuckDBConnector,
    PostgresConnector,
    SQLiteConnector,
)
from data_agent.tools.schemas import (
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
)
from data_agent.relationships.models import (
    RelationshipCondition,
    RelationshipComponent,
    RelationshipEdge,
    RelationshipEdgeQuality,
    RelationshipGraphDraft,
    RelationshipGraphNode,
    RelationshipProvenance,
    RelationshipRecommendationRun,
)
from data_agent.relationships.recommender import RelationshipRecommender
from data_agent.relationships.models import ActivatedRelationshipGraph, validate_graph_catalog
from data_agent.relationships.validator import validate_graph
from data_agent.model_client import ModelClient


_SOURCE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_SAFE_FILENAME = re.compile(r"[^a-zA-Z0-9._-]+")
PostgresPoolFactory = Callable[[str], Awaitable[Any]]
SecretResolver = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class DataSourceExecutionContext:
    """Resolved, version-pinned authority required for one query execution."""

    source: DataSourceDefinition
    snapshot: DataSourceSnapshot
    binding: SemanticBindingRecord | SemanticGraphBindingRecord
    connector: DataSourceConnector
    connection_ref: str


async def _default_postgres_pool_factory(dsn: str) -> Any:
    import asyncpg

    return await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)


def _default_secret_resolver(credential_ref: str) -> str:
    prefix = "secret://"
    if not credential_ref.startswith(prefix):
        raise ValueError("PostgreSQL credential_ref must use secret://")
    name = credential_ref.removeprefix(prefix).strip("/")
    if not name:
        raise ValueError("PostgreSQL credential_ref is invalid")
    environment_name = "DATA_SOURCE_SECRET_" + re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        name,
    ).strip("_").upper()
    value = os.environ.get(environment_name, "").strip()
    if not value:
        raise ValueError(
            f"datasource credential {credential_ref} is unavailable"
        )
    return value


class DataSourceService:
    """Keep upload mechanics outside HTTP routes and runtime execution."""

    def __init__(
        self,
        *,
        state_root: str | Path | None = None,
        registry: DataSourceRegistry | None = None,
        connector_registry: ConnectorRegistry | None = None,
        postgres_pool_factory: PostgresPoolFactory | None = None,
        secret_resolver: SecretResolver | None = None,
        relationship_model_client: ModelClient | None = None,
        max_upload_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        configured_root = state_root or os.environ.get("DATA_AGENT_STATE_DIR")
        self._state_root = Path(configured_root or "var/data-agent").expanduser()
        self._registry = registry or SQLiteDataSourceRegistry(
            self._state_root / "control" / "datasources.sqlite3"
        )
        self._connectors = connector_registry or ConnectorRegistry()
        self._postgres_pool_factory = (
            postgres_pool_factory or _default_postgres_pool_factory
        )
        self._secret_resolver = secret_resolver or _default_secret_resolver
        self._resources: list[Any] = []
        self._source_resources: dict[tuple[str, str], list[Any]] = {}
        self._max_upload_bytes = max_upload_bytes
        self._relationship_model_client = relationship_model_client
        self._relationship_tasks: set[asyncio.Task[None]] = set()
        self._latest_relationship_runs: dict[
            tuple[str, str, int], RelationshipRecommendationRun
        ] = {}

    @property
    def registry(self) -> DataSourceRegistry:
        return self._registry

    @property
    def connectors(self) -> ConnectorRegistry:
        return self._connectors

    @property
    def state_root(self) -> Path:
        return self._state_root

    async def ensure_relationship_discovery(
        self, *, tenant_id: str, source_id: str
    ) -> tuple[RelationshipGraphDraft, RelationshipRecommendationRun]:
        """Create the non-blocking draft/run lifecycle after catalog publication."""

        snapshot = await self.get_snapshot(tenant_id=tenant_id, source_id=source_id)
        existing = await self._registry.get_graph_draft(
            tenant_id=tenant_id,
            source_id=source_id,
            source_snapshot_version=snapshot.version,
        )
        graph = existing
        if graph is None:
            graph_id = f"graph-{uuid4().hex[:20]}"
            nodes = tuple(
                RelationshipGraphNode(
                    node_id=f"node-{relation.relation_id.split(':', 1)[-1][:20]}",
                    relation_id=relation.relation_id,
                    role_name=relation.relation.rsplit('.', 1)[-1],
                    logical_entity=relation.relation.replace('.', '_'),
                )
                for relation in snapshot.catalog.relations
            )
            nodes_by_relation = {node.relation_id: node for node in nodes}
            constraint_edges: list[RelationshipEdge] = []
            for relation in snapshot.catalog.relations:
                for foreign_key in relation.foreign_keys:
                    left = nodes_by_relation.get(foreign_key.from_relation_id)
                    right = nodes_by_relation.get(foreign_key.to_relation_id)
                    if left is None or right is None:
                        continue
                    target = next(
                        (
                            candidate
                            for candidate in snapshot.catalog.relations
                            if candidate.relation_id == foreign_key.to_relation_id
                        ),
                        None,
                    )
                    target_is_unique = target is not None and any(
                        set(key.column_ids) == set(foreign_key.to_column_ids)
                        for key in target.keys
                    )
                    constraint_edges.append(
                        RelationshipEdge(
                            edge_id=f"edge-{foreign_key.foreign_key_id}",
                            from_node_id=left.node_id,
                            to_node_id=right.node_id,
                            conditions=tuple(
                                RelationshipCondition(
                                    from_column_id=from_column,
                                    to_column_id=to_column,
                                )
                                for from_column, to_column in zip(
                                    foreign_key.from_column_ids,
                                    foreign_key.to_column_ids,
                                    strict=True,
                                )
                            ),
                            cardinality="many_to_one" if target_is_unique else "unknown",
                            provenance=RelationshipProvenance(
                                source="database_constraint",
                                explanation="Declared database foreign key.",
                            ),
                            quality=RelationshipEdgeQuality(evidence_level="high"),
                        )
                    )
            graph = RelationshipGraphDraft(
                graph_id=graph_id,
                tenant_id=tenant_id,
                source_id=source_id,
                source_snapshot_version=snapshot.version,
                schema_fingerprint=snapshot.fingerprint,
                revision=1,
                status="draft",
                nodes=nodes,
                edges=tuple(constraint_edges),
                components=tuple(
                    RelationshipComponent(
                        component_id=f"component-{node.node_id.removeprefix('node-')}",
                        anchor_node_id=node.node_id,
                        grain_column_ids=(),
                    )
                    for node in nodes
                ),
            )
            await self._registry.create_graph_draft(graph)
        run = RelationshipRecommendationRun(
            run_id=f"recommendation-{uuid4().hex[:20]}",
            tenant_id=tenant_id,
            source_id=source_id,
            source_snapshot_version=snapshot.version,
            graph_id=graph.graph_id,
            status="queued",
            schema_fingerprint=snapshot.fingerprint,
            prompt_version=RelationshipRecommender.prompt_version,
            profiler_version="profile-v1",
        )
        if self._relationship_model_client is None:
            run = run.model_copy(
                update={
                    "status": "retryable_failed",
                    "error_code": "RELATIONSHIP_RECOMMENDATION_FAILED",
                    "error_message": "Relationship recommendation model is unavailable.",
                }
            )
            await self._registry.save_recommendation_run(run)
        else:
            await self._registry.save_recommendation_run(run)
            task = asyncio.create_task(self._run_relationship_recommendations(graph, run))
            self._relationship_tasks.add(task)
            task.add_done_callback(self._relationship_tasks.discard)
        self._latest_relationship_runs[(tenant_id, source_id, snapshot.version)] = run
        return graph, run

    async def latest_relationship_discovery(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[RelationshipGraphDraft, RelationshipRecommendationRun] | None:
        """Return the run created during this process's most recent publish."""

        snapshot = await self.get_snapshot(tenant_id=tenant_id, source_id=source_id)
        run = self._latest_relationship_runs.get(
            (tenant_id, source_id, snapshot.version)
        )
        if run is None:
            return None
        graph = await self._registry.get_graph_draft(
            tenant_id=tenant_id,
            source_id=source_id,
            source_snapshot_version=snapshot.version,
        )
        return (graph, run) if graph is not None else None

    async def _run_relationship_recommendations(
        self, graph: RelationshipGraphDraft, run: RelationshipRecommendationRun
    ) -> None:
        """Best-effort discovery: failures never affect an already published datasource."""
        assert self._relationship_model_client is not None
        running = run.model_copy(update={"status": "running", "model_id": self._relationship_model_client.model_id})
        await self._registry.save_recommendation_run(running)
        try:
            snapshot = await self._registry.get_snapshot(tenant_id=run.tenant_id, source_id=run.source_id, version=run.source_snapshot_version)
            recommendations = await RelationshipRecommender().recommend(catalog=snapshot.catalog, model_client=self._relationship_model_client)
            current = await self._registry.get_graph_draft(
                tenant_id=run.tenant_id,
                source_id=run.source_id,
                source_snapshot_version=run.source_snapshot_version,
            )
            if current is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "relationship graph draft was not found",
                )
            nodes = {node.relation_id: node for node in current.nodes}
            edges = list(current.edges)
            known_conditions = {
                tuple(
                    (condition.from_column_id, condition.to_column_id)
                    for condition in edge.conditions
                )
                for edge in edges
            }
            for index, item in enumerate(recommendations, start=1):
                left, right = nodes.get(item.from_relation_id), nodes.get(item.to_relation_id)
                if left is None or right is None:
                    continue
                conditions = ((item.from_column_id, item.to_column_id),)
                if conditions in known_conditions:
                    continue
                known_conditions.add(conditions)
                edges.append(RelationshipEdge(
                    edge_id=f"llm-{run.run_id.removeprefix('recommendation-')}-{index}",
                    from_node_id=left.node_id, to_node_id=right.node_id,
                    conditions=(RelationshipCondition(from_column_id=item.from_column_id, to_column_id=item.to_column_id),),
                    cardinality=item.cardinality_hint if item.cardinality_hint in {"one_to_one", "one_to_many", "many_to_one", "many_to_many", "unknown"} else "unknown",
                    provenance=RelationshipProvenance(source="llm", run_id=run.run_id, model_id=self._relationship_model_client.model_id, prompt_version=RelationshipRecommender.prompt_version, explanation=item.explanation),
                    quality=RelationshipEdgeQuality(evidence_level="recommended" if item.confidence >= .6 else "low"),
                ))
            if tuple(edges) != current.edges:
                updated = current.model_copy(update={"edges": tuple(edges), "revision": current.revision + 1, "status": "draft"})
                await self._registry.save_graph_draft(updated, expected_revision=current.revision)
            await self._registry.save_recommendation_run(running.model_copy(update={"status": "succeeded"}))
        except Exception:
            await self._registry.save_recommendation_run(running.model_copy(update={"status": "retryable_failed", "error_code": "RELATIONSHIP_RECOMMENDATION_FAILED", "error_message": "Relationship recommendation could not be completed."}))

    async def get_relationship_draft(
        self, *, tenant_id: str, source_id: str
    ) -> RelationshipGraphDraft | None:
        snapshot = await self.get_snapshot(tenant_id=tenant_id, source_id=source_id)
        return await self._registry.get_graph_draft(
            tenant_id=tenant_id, source_id=source_id, source_snapshot_version=snapshot.version
        )

    async def activate_relationship_graph(
        self, *, tenant_id: str, source_id: str, graph_id: str, domain_id: str,
        mappings: tuple[SemanticGraphFieldMapping, ...],
        metrics: tuple[SemanticMetricDefinition, ...] = (),
        binding_id: str | None = None,
    ) -> SemanticGraphBindingRecord:
        snapshot = await self.get_snapshot(tenant_id=tenant_id, source_id=source_id)
        graph = await self.get_relationship_draft(tenant_id=tenant_id, source_id=source_id)
        if graph is None or graph.graph_id != graph_id:
            raise DataSourceRegistryError(DataSourceRegistryErrorCode.NOT_FOUND, "relationship graph draft was not found")
        validate_graph_catalog(graph, snapshot.catalog)
        report = validate_graph(graph)
        if not report.activation_allowed:
            raise ValueError("relationship graph validation does not permit activation")
        nodes = {node.node_id for node in graph.nodes}
        columns = {
            column.column_id: relation.relation_id
            for relation in snapshot.catalog.relations
            for column in relation.columns
        }
        node_relations = {node.node_id: node.relation_id for node in graph.nodes}
        if any(
            item.node_id not in nodes
            or item.column_id not in columns
            or node_relations[item.node_id] != columns[item.column_id]
            for item in mappings
        ):
            raise ValueError("graph binding mapping references an unknown graph field")
        existing = await self._registry.list_bindings(tenant_id=tenant_id, source_id=source_id)
        graph_bindings = await self._registry.list_graph_bindings(tenant_id=tenant_id, source_id=source_id)
        activated_graph = ActivatedRelationshipGraph(
            graph_id=graph.graph_id,
            revision=graph.revision,
            nodes=graph.nodes,
            edges=graph.edges,
            components=graph.components,
            route_rules=graph.route_rules,
        )
        for current in graph_bindings:
            if (
                current.domain_id == domain_id
                and current.status == SemanticBindingStatus.ACTIVE
                and current.source_snapshot_version == snapshot.version
                and current.schema_fingerprint == snapshot.fingerprint
                and current.graph == activated_graph
                and current.mappings == mappings
                and current.metrics == metrics
                and current.validation_report_digest == report.report_digest
            ):
                return current
        version = max(
            (item.version for item in (*existing, *graph_bindings) if item.domain_id == domain_id),
            default=0,
        ) + 1
        domain_digest = hashlib.sha256(domain_id.encode("utf-8")).hexdigest()[:12]
        binding = SemanticGraphBindingRecord(
            binding_id=(
                binding_id
                or f"{source_id}-graph-{domain_digest}-binding-{version}"
            ),
            tenant_id=tenant_id,
            source_id=source_id, source_snapshot_version=snapshot.version, schema_fingerprint=snapshot.fingerprint,
            domain_id=domain_id, version=version,
            graph=activated_graph,
            mappings=mappings, metrics=metrics,
            validation_report_digest=report.report_digest,
        )
        return await self._registry.activate_graph_binding(binding)

    @staticmethod
    def _source_id(value: str | None) -> str:
        source_id = (value or "").strip() or f"source-{uuid4().hex[:16]}"
        if not _SOURCE_ID.fullmatch(source_id):
            raise ValueError("source_id contains unsupported characters")
        return source_id

    @staticmethod
    def _path_component(value: str) -> str:
        component = _SAFE_FILENAME.sub("_", value).strip("._")
        if not component:
            raise ValueError("identifier cannot be used as a storage path")
        return component

    async def list_sources(
        self,
        *,
        tenant_id: str,
    ) -> tuple[DataSourceDefinition, ...]:
        return await self._registry.list(tenant_id=tenant_id)

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceSnapshot:
        source = await self._registry.get(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        if (
            source.active_snapshot_version < 1
            and source.kind == DataSourceKind.POSTGRES
        ):
            source = await self._materialize_postgres_source(source)
        if source.active_snapshot_version < 1:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.NOT_FOUND,
                "datasource has no published snapshot",
            )
        return await self._registry.get_snapshot(
            tenant_id=tenant_id,
            source_id=source_id,
            version=source.active_snapshot_version,
        )

    async def get_connector(
        self,
        *,
        tenant_id: str,
        source_id: str,
        source_version: int | None = None,
    ) -> DataSourceConnector:
        """Return or reconstruct an immutable file connector after restart."""

        source = await self._registry.get(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        version = source_version or source.active_snapshot_version
        if version < 1:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.NOT_FOUND,
                "datasource has no published snapshot",
            )
        try:
            return self._connectors.get(
                tenant_id=tenant_id,
                source_id=source_id,
                source_version=version,
            )
        except DataSourceRegistryError as exc:
            if exc.code != DataSourceRegistryErrorCode.NOT_FOUND:
                raise
        snapshot = await self._registry.get_snapshot(
            tenant_id=tenant_id,
            source_id=source_id,
            version=version,
        )
        if source.kind == DataSourceKind.POSTGRES:
            connector = await self._restore_postgres_connector(
                source,
                snapshot,
            )
        else:
            connector = self._restore_file_connector(source, snapshot)
        try:
            self._connectors.register(
                source,
                connector,
                source_version=version,
            )
        except DataSourceRegistryError as exc:
            if exc.code != DataSourceRegistryErrorCode.ALREADY_EXISTS:
                raise
            return self._connectors.get(
                tenant_id=tenant_id,
                source_id=source_id,
                source_version=version,
            )
        return connector

    async def register_postgres(
        self,
        *,
        tenant_id: str,
        source_id: str | None,
        name: str,
        credential_ref: str,
        options: dict[str, str | int | float | bool],
    ) -> DataSourceDefinition:
        definition = DataSourceDefinition(
            source_id=self._source_id(source_id),
            tenant_id=tenant_id,
            name=name,
            kind=DataSourceKind.POSTGRES,
            credential_ref=credential_ref,
            options=options,
        )
        return await self._registry.create(definition)

    async def close(self) -> None:
        tasks, self._relationship_tasks = self._relationship_tasks, set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        resources, self._resources = self._resources, []
        self._source_resources = {}
        for resource in reversed(resources):
            close = getattr(resource, "close", None)
            if close is None:
                continue
            value = close()
            if inspect.isawaitable(value):
                await value

    async def delete_source(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceDefinition:
        source = await self._registry.delete(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        self._connectors.remove(tenant_id=tenant_id, source_id=source_id)
        resources = self._source_resources.pop((tenant_id, source_id), [])
        for resource in reversed(resources):
            self._resources = [
                candidate
                for candidate in self._resources
                if candidate is not resource
            ]
            await self._close_resource(resource)
        if source.kind != DataSourceKind.POSTGRES:
            snapshot_base = (self._state_root / "snapshots").resolve()
            snapshot_root = (
                snapshot_base
                / self._path_component(tenant_id)
                / self._path_component(source_id)
            ).resolve()
            if not snapshot_root.is_relative_to(snapshot_base):
                raise ValueError("datasource snapshot path is outside the storage root")
            if snapshot_root.exists():
                shutil.rmtree(snapshot_root)
        for key in tuple(self._latest_relationship_runs):
            if key[:2] == (tenant_id, source_id):
                self._latest_relationship_runs.pop(key, None)
        return source

    async def import_file_source(
        self,
        *,
        tenant_id: str,
        source_id: str | None,
        name: str,
        uploads: list[UploadFile],
    ) -> DataSourceDefinition:
        resolved_source_id = self._source_id(source_id)
        try:
            await self._registry.get(
                tenant_id=tenant_id,
                source_id=resolved_source_id,
            )
        except DataSourceRegistryError as exc:
            if exc.code != DataSourceRegistryErrorCode.NOT_FOUND:
                raise
        else:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.ALREADY_EXISTS,
                "datasource already exists",
            )

        tenant_staging = (
            self._state_root
            / "staging"
            / _SAFE_FILENAME.sub("_", tenant_id)
            / uuid4().hex
        )
        snapshot_root = (
            self._state_root
            / "snapshots"
            / self._path_component(tenant_id)
            / self._path_component(resolved_source_id)
        )
        tenant_staging.mkdir(parents=True, exist_ok=False)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        staged: list[Path] = []
        try:
            for upload in uploads:
                staged.append(
                    await self._stage_upload(
                        upload,
                        destination=tenant_staging,
                        allowed_suffixes={".csv", ".xlsx"},
                    )
                )
            suffixes = {path.suffix.lower() for path in staged}
            if not suffixes or not suffixes.issubset({".csv", ".xlsx"}):
                raise ValueError("only CSV and XLSX uploads are supported")
            kind = (
                DataSourceKind.XLSX
                if ".xlsx" in suffixes
                else DataSourceKind.CSV
            )
            imported = FileSnapshotImporter(
                staging_root=tenant_staging,
                max_file_bytes=self._max_upload_bytes,
            ).import_files(
                staged,
                output_directory=snapshot_root,
                source_id=resolved_source_id,
                version=1,
            )
            location_ref = (
                f"snapshot://{tenant_id}/{resolved_source_id}/v1"
            )
            definition = DataSourceDefinition(
                source_id=resolved_source_id,
                tenant_id=tenant_id,
                name=name,
                kind=kind,
                location_ref=location_ref,
                options={
                    "dialect": "duckdb",
                    "file_count": len(staged),
                },
            )
            await self._registry.create(definition)
            snapshot = DataSourceSnapshot(
                snapshot_id=f"{resolved_source_id}:v1",
                tenant_id=tenant_id,
                source_id=resolved_source_id,
                version=1,
                fingerprint=imported.fingerprint,
                catalog=imported.catalog,
            )
            updated = await self._registry.publish_snapshot(snapshot)
            connector = DuckDBConnector(
                imported.database_path,
                allowed_relations=tuple(
                    relation.relation
                    for relation in imported.catalog.relations
                ),
                schema_fingerprint=imported.fingerprint,
                source=resolved_source_id,
                connection_ref=location_ref,
            )
            self._connectors.register(
                updated,
                connector,
                source_version=1,
            )
            await self.ensure_relationship_discovery(
                tenant_id=tenant_id, source_id=resolved_source_id
            )
            return updated
        finally:
            shutil.rmtree(tenant_staging, ignore_errors=True)
            for upload in uploads:
                await upload.close()

    async def import_sqlite_source(
        self,
        *,
        tenant_id: str,
        source_id: str | None,
        name: str,
        upload: UploadFile,
    ) -> DataSourceDefinition:
        resolved_source_id = self._source_id(source_id)
        try:
            await self._registry.get(
                tenant_id=tenant_id,
                source_id=resolved_source_id,
            )
        except DataSourceRegistryError as exc:
            if exc.code != DataSourceRegistryErrorCode.NOT_FOUND:
                raise
        else:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.ALREADY_EXISTS,
                "datasource already exists",
            )
        tenant_staging = (
            self._state_root
            / "staging"
            / _SAFE_FILENAME.sub("_", tenant_id)
            / uuid4().hex
        )
        snapshot_root = (
            self._state_root
            / "snapshots"
            / self._path_component(tenant_id)
            / self._path_component(resolved_source_id)
        )
        tenant_staging.mkdir(parents=True, exist_ok=False)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        try:
            staged = await self._stage_upload(
                upload,
                destination=tenant_staging,
                allowed_suffixes={".db", ".sqlite", ".sqlite3"},
            )
            imported = SQLiteSnapshotImporter(
                max_file_bytes=self._max_upload_bytes
            ).import_file(
                staged,
                output_directory=snapshot_root,
                source_id=resolved_source_id,
                version=1,
            )
            location_ref = (
                f"snapshot://{tenant_id}/{resolved_source_id}/v1"
            )
            definition = DataSourceDefinition(
                source_id=resolved_source_id,
                tenant_id=tenant_id,
                name=name,
                kind=DataSourceKind.SQLITE,
                location_ref=location_ref,
                options={"dialect": "sqlite", "file_count": 1},
            )
            await self._registry.create(definition)
            snapshot = DataSourceSnapshot(
                snapshot_id=f"{resolved_source_id}:v1",
                tenant_id=tenant_id,
                source_id=resolved_source_id,
                version=1,
                fingerprint=imported.fingerprint,
                catalog=imported.catalog,
            )
            updated = await self._registry.publish_snapshot(snapshot)
            self._connectors.register(
                updated,
                SQLiteConnector(
                    imported.database_path,
                    allowed_relations=tuple(
                        relation.relation
                        for relation in imported.catalog.relations
                    ),
                    schema_fingerprint=imported.fingerprint,
                    source=resolved_source_id,
                    connection_ref=location_ref,
                ),
                source_version=1,
            )
            await self.ensure_relationship_discovery(
                tenant_id=tenant_id, source_id=resolved_source_id
            )
            return updated
        finally:
            shutil.rmtree(tenant_staging, ignore_errors=True)
            await upload.close()

    async def create_binding(
        self,
        *,
        tenant_id: str,
        source_id: str,
        binding_id: str | None,
        domain_id: str,
        mappings: tuple[SemanticFieldMapping, ...],
        metrics: tuple[SemanticMetricDefinition, ...] = (),
        primary_relation: str | None = None,
        relationships: tuple[SemanticRelationship, ...] = (),
    ) -> SemanticBindingRecord:
        snapshot = await self.get_snapshot(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        existing = await self._registry.list_bindings(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        domain_versions = tuple(
            item.version for item in existing if item.domain_id == domain_id
        )
        version = max(domain_versions, default=0) + 1
        binding = SemanticBindingRecord(
            binding_id=self._source_id(binding_id)
            if binding_id
            else f"{source_id}-binding-{version}",
            tenant_id=tenant_id,
            source_id=source_id,
            source_snapshot_version=snapshot.version,
            domain_id=domain_id,
            version=version,
            mappings=mappings,
            metrics=metrics,
            primary_relation=primary_relation,
            relationships=relationships,
        )
        return await self._registry.save_binding(binding)

    async def activate_binding(
        self,
        *,
        tenant_id: str,
        source_id: str,
        binding_id: str,
    ) -> SemanticBindingRecord:
        bindings = await self._registry.list_bindings(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        if binding_id not in {binding.binding_id for binding in bindings}:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.NOT_FOUND,
                "semantic binding was not found for this datasource",
            )
        return await self._registry.activate_binding(
            tenant_id=tenant_id,
            binding_id=binding_id,
        )

    async def list_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticBindingRecord | SemanticGraphBindingRecord, ...]:
        bindings = await self._registry.list_bindings(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        graph_bindings = await self._registry.list_graph_bindings(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        return tuple((*bindings, *graph_bindings))

    async def resolve_active_binding(
        self,
        *,
        tenant_id: str,
        source_id: str,
        source_version: int,
        binding_id: str,
        binding_version: int,
        domain_id: str,
    ) -> DataSourceExecutionContext:
        source = await self._registry.get(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        if source.active_snapshot_version != source_version:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.VERSION_CONFLICT,
                "requested datasource snapshot is no longer active",
            )
        snapshot = await self._registry.get_snapshot(
            tenant_id=tenant_id,
            source_id=source_id,
            version=source_version,
        )
        bindings = await self._registry.list_bindings(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        graph_bindings = await self._registry.list_graph_bindings(
            tenant_id=tenant_id,
            source_id=source_id,
        )
        binding = next(
            (
                item
                for item in (*bindings, *graph_bindings)
                if item.binding_id == binding_id
            ),
            None,
        )
        if binding is None:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.NOT_FOUND,
                "semantic binding was not found for this datasource",
            )
        if (
            binding.status != SemanticBindingStatus.ACTIVE
            or binding.version != binding_version
            or binding.source_snapshot_version != source_version
            or binding.domain_id != domain_id
        ):
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.INVALID_BINDING,
                "requested semantic binding is inactive, stale, or out of scope",
            )
        connector = await self.get_connector(
            tenant_id=tenant_id,
            source_id=source_id,
            source_version=source_version,
        )
        return DataSourceExecutionContext(
            source=source,
            snapshot=snapshot,
            binding=binding,
            connector=connector,
            connection_ref=self._execution_connection_ref(source),
        )

    async def pin_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
    ) -> ConversationDataSourcePin:
        return await self._registry.pin_conversation(
            ConversationDataSourcePin(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                domain_id=binding.domain_id,
                source_id=binding.source_id,
                source_version=binding.source_snapshot_version,
                binding_id=binding.binding_id,
                binding_version=binding.version,
            )
        )

    async def get_conversation_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> SemanticBindingRecord | SemanticGraphBindingRecord | None:
        pin = await self._registry.get_conversation_pin(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if pin is None:
            return None
        bindings = await self._registry.list_bindings(
            tenant_id=tenant_id,
            source_id=pin.source_id,
        )
        graph_bindings = await self._registry.list_graph_bindings(
            tenant_id=tenant_id,
            source_id=pin.source_id,
        )
        return next(
            (
                binding
                for binding in (*bindings, *graph_bindings)
                if binding.binding_id == pin.binding_id
                and binding.version == pin.binding_version
                and binding.source_snapshot_version == pin.source_version
                and binding.domain_id == pin.domain_id
            ),
            None,
        )

    async def _stage_upload(
        self,
        upload: UploadFile,
        *,
        destination: Path,
        allowed_suffixes: set[str],
    ) -> Path:
        original = Path(upload.filename or "upload").name
        safe_name = _SAFE_FILENAME.sub("_", original).strip("._")
        suffix = Path(original).suffix.lower()
        if suffix not in allowed_suffixes:
            raise ValueError(
                f"file {original!r} has unsupported type {suffix or '(none)'}"
            )
        if not safe_name.lower().endswith(suffix):
            safe_name = (safe_name or "upload") + suffix
        path = destination / safe_name
        if path.exists():
            path = destination / f"{uuid4().hex[:8]}-{safe_name}"
        total = 0
        try:
            with path.open("xb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > self._max_upload_bytes:
                        raise ValueError(
                            f"file {original!r} exceeds the file size limit of "
                            f"{self._max_upload_bytes:,} bytes"
                        )
                    stream.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if total == 0:
            path.unlink(missing_ok=True)
            raise ValueError(f"file {original!r} is empty")
        return path

    def _restore_file_connector(
        self,
        source: DataSourceDefinition,
        snapshot: DataSourceSnapshot,
    ) -> DataSourceConnector:
        snapshot_root = (
            self._state_root
            / "snapshots"
            / self._path_component(source.tenant_id)
            / self._path_component(source.source_id)
        )
        suffix = (
            ".sqlite"
            if source.kind == DataSourceKind.SQLITE
            else ".duckdb"
        )
        matches = tuple(
            path
            for path in snapshot_root.glob(f"*-v{snapshot.version}-*{suffix}")
            if path.is_file()
        )
        if len(matches) != 1:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.NOT_FOUND,
                "immutable datasource snapshot file was not found",
            )
        allowed_relations = tuple(
            relation.relation for relation in snapshot.catalog.relations
        )
        common = {
            "allowed_relations": allowed_relations,
            "schema_fingerprint": snapshot.fingerprint,
            "source": source.source_id,
            "connection_ref": source.location_ref or source.source_id,
        }
        if source.kind == DataSourceKind.SQLITE:
            return SQLiteConnector(matches[0], **common)
        return DuckDBConnector(matches[0], **common)

    async def _materialize_postgres_source(
        self,
        source: DataSourceDefinition,
    ) -> DataSourceDefinition:
        pool = await self._create_postgres_pool(source)
        try:
            catalog = await self._postgres_catalog(pool)
            snapshot = DataSourceSnapshot(
                snapshot_id=f"{source.source_id}:v1",
                tenant_id=source.tenant_id,
                source_id=source.source_id,
                version=1,
                fingerprint=catalog.schema_fingerprint,
                catalog=catalog,
            )
            updated = await self._registry.publish_snapshot(snapshot)
            self._register_postgres_connector(updated, snapshot, pool)
            self._track_resource(source, pool)
            await self.ensure_relationship_discovery(
                tenant_id=source.tenant_id, source_id=source.source_id
            )
            return updated
        except BaseException:
            await self._close_resource(pool)
            raise

    async def _restore_postgres_connector(
        self,
        source: DataSourceDefinition,
        snapshot: DataSourceSnapshot,
    ) -> DataSourceConnector:
        pool = await self._create_postgres_pool(source)
        try:
            current = await self._postgres_catalog(pool)
            if current.schema_fingerprint != snapshot.fingerprint:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "PostgreSQL schema changed after the active snapshot",
                )
            connector = self._register_postgres_connector(
                source,
                snapshot,
                pool,
                register=False,
            )
            self._track_resource(source, pool)
            return connector
        except BaseException:
            await self._close_resource(pool)
            raise

    async def _create_postgres_pool(
        self,
        source: DataSourceDefinition,
    ) -> Any:
        if source.credential_ref is None:
            raise ValueError("PostgreSQL datasource has no credential reference")
        dsn = self._secret_resolver(source.credential_ref)
        return await self._postgres_pool_factory(dsn)

    def _register_postgres_connector(
        self,
        source: DataSourceDefinition,
        snapshot: DataSourceSnapshot,
        pool: Any,
        *,
        register: bool = True,
    ) -> DataSourceConnector:
        connector = PostgresConnector(
            pool,
            allowed_relations=tuple(
                relation.relation for relation in snapshot.catalog.relations
            ),
            schema_fingerprint=snapshot.fingerprint,
            source=source.source_id,
            connection_ref=source.credential_ref or source.source_id,
            bundle_digest=None,
        )
        if register:
            self._connectors.register(
                source,
                connector,
                source_version=snapshot.version,
            )
        return connector

    @staticmethod
    def _execution_connection_ref(source: DataSourceDefinition) -> str:
        """Return the connector authority reference without exposing its secret."""

        if source.kind == DataSourceKind.POSTGRES:
            if source.credential_ref is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.CONNECTOR_MISMATCH,
                    "PostgreSQL datasource credential authority is unavailable",
                )
            return source.credential_ref
        if source.location_ref is None:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.CONNECTOR_MISMATCH,
                "file datasource snapshot authority is unavailable",
            )
        return source.location_ref

    @staticmethod
    async def _postgres_catalog(pool: Any) -> CatalogSnapshot:
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    table_schema,
                    table_name,
                    column_name,
                    data_type,
                    is_nullable
                FROM information_schema.columns
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name, ordinal_position
                """
            )
        grouped: dict[tuple[str, str], list[CatalogColumn]] = {}
        for row in rows:
            schema = str(row["table_schema"])
            table = str(row["table_name"])
            grouped.setdefault((schema, table), []).append(
                CatalogColumn(
                    name=str(row["column_name"]),
                    data_type=str(row["data_type"]),
                    nullable=str(row["is_nullable"]).upper() == "YES",
                )
            )
        if not grouped:
            raise ValueError("PostgreSQL datasource has no accessible relations")
        relation_payload = [
            {
                "relation": f"{schema}.{table}",
                "columns": [
                    column.model_dump(mode="json")
                    for column in columns
                ],
            }
            for (schema, table), columns in sorted(grouped.items())
        ]
        fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                relation_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return CatalogSnapshot(
            schema_fingerprint=fingerprint,
            relations=tuple(
                CatalogRelation(
                    relation=item["relation"],
                    columns=tuple(
                        CatalogColumn.model_validate(column)
                        for column in item["columns"]
                    ),
                )
                for item in relation_payload
            ),
        )

    @staticmethod
    async def _close_resource(resource: Any) -> None:
        close = getattr(resource, "close", None)
        if close is None:
            return
        value = close()
        if inspect.isawaitable(value):
            await value

    def _track_resource(self, source: DataSourceDefinition, resource: Any) -> None:
        self._resources.append(resource)
        self._source_resources.setdefault(
            (source.tenant_id, source.source_id),
            [],
        ).append(resource)


__all__ = ["DataSourceExecutionContext", "DataSourceService"]
