"""Application service for tenant-isolated datasource registration and uploads."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
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
    SemanticBindingStatus,
    SemanticFieldMapping,
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


_SOURCE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_SAFE_FILENAME = re.compile(r"[^a-zA-Z0-9._-]+")
PostgresPoolFactory = Callable[[str], Awaitable[Any]]
SecretResolver = Callable[[str], str]


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
        self._max_upload_bytes = max_upload_bytes

    @property
    def registry(self) -> DataSourceRegistry:
        return self._registry

    @property
    def connectors(self) -> ConnectorRegistry:
        return self._connectors

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
        resources, self._resources = self._resources, []
        for resource in reversed(resources):
            close = getattr(resource, "close", None)
            if close is None:
                continue
            value = close()
            if inspect.isawaitable(value):
                await value

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
    ) -> tuple[SemanticBindingRecord, ...]:
        return await self._registry.list_bindings(
            tenant_id=tenant_id,
            source_id=source_id,
        )

    async def resolve_active_binding(
        self,
        *,
        tenant_id: str,
        source_id: str,
        source_version: int,
        binding_id: str,
        binding_version: int,
        domain_id: str,
    ) -> tuple[
        DataSourceDefinition,
        DataSourceSnapshot,
        SemanticBindingRecord,
        DataSourceConnector,
    ]:
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
        binding = next(
            (
                item
                for item in bindings
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
        return source, snapshot, binding, connector

    async def pin_conversation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        binding: SemanticBindingRecord,
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
    ) -> SemanticBindingRecord | None:
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
        return next(
            (
                binding
                for binding in bindings
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
            raise ValueError("upload file type is not supported")
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
                        raise ValueError("upload exceeds the file size limit")
                    stream.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if total == 0:
            path.unlink(missing_ok=True)
            raise ValueError("upload is empty")
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
            self._resources.append(pool)
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
            self._resources.append(pool)
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


__all__ = ["DataSourceService"]
