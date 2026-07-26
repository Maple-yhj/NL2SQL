"""Tenant-isolated datasource, snapshot, binding, and connector registries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from data_agent.tools.connectors import DataSourceConnector

from .models import (
    ConversationDataSourcePin,
    DataSourceDefinition,
    DataSourceKind,
    DataSourceSnapshot,
    DataSourceStatus,
    SemanticBindingRecord,
    SemanticBindingStatus,
)


class DataSourceRegistryErrorCode(StrEnum):
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    INVALID_BINDING = "INVALID_BINDING"
    CONNECTOR_MISMATCH = "CONNECTOR_MISMATCH"


class DataSourceRegistryError(RuntimeError):
    def __init__(
        self,
        code: DataSourceRegistryErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class DataSourceRegistry(Protocol):
    """Persistence boundary for datasource control-plane state."""

    async def create(
        self,
        definition: DataSourceDefinition,
    ) -> DataSourceDefinition: ...

    async def get(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceDefinition: ...

    async def list(
        self,
        *,
        tenant_id: str,
    ) -> tuple[DataSourceDefinition, ...]: ...

    async def publish_snapshot(
        self,
        snapshot: DataSourceSnapshot,
    ) -> DataSourceDefinition: ...

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        source_id: str,
        version: int,
    ) -> DataSourceSnapshot: ...

    async def save_binding(
        self,
        binding: SemanticBindingRecord,
    ) -> SemanticBindingRecord: ...

    async def list_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticBindingRecord, ...]: ...

    async def activate_binding(
        self,
        *,
        tenant_id: str,
        binding_id: str,
    ) -> SemanticBindingRecord: ...

    async def pin_conversation(
        self,
        pin: ConversationDataSourcePin,
    ) -> ConversationDataSourcePin: ...

    async def get_conversation_pin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationDataSourcePin | None: ...


class InMemoryDataSourceRegistry:
    """Reference control-plane implementation with strict tenant isolation."""

    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], DataSourceDefinition] = {}
        self._snapshots: dict[tuple[str, str, int], DataSourceSnapshot] = {}
        self._bindings: dict[tuple[str, str], SemanticBindingRecord] = {}
        self._conversation_pins: dict[
            tuple[str, str, str], ConversationDataSourcePin
        ] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        definition: DataSourceDefinition,
    ) -> DataSourceDefinition:
        key = (definition.tenant_id, definition.source_id)
        async with self._lock:
            if key in self._sources:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "datasource already exists",
                )
            self._sources[key] = definition
            return definition

    async def get(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> DataSourceDefinition:
        async with self._lock:
            source = self._sources.get((tenant_id, source_id))
            if source is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource was not found",
                )
            return source

    async def list(
        self,
        *,
        tenant_id: str,
    ) -> tuple[DataSourceDefinition, ...]:
        async with self._lock:
            return tuple(
                source
                for (candidate_tenant, _), source in sorted(
                    self._sources.items()
                )
                if candidate_tenant == tenant_id
            )

    async def publish_snapshot(
        self,
        snapshot: DataSourceSnapshot,
    ) -> DataSourceDefinition:
        source_key = (snapshot.tenant_id, snapshot.source_id)
        snapshot_key = (
            snapshot.tenant_id,
            snapshot.source_id,
            snapshot.version,
        )
        async with self._lock:
            source = self._sources.get(source_key)
            if source is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource was not found",
                )
            expected_version = source.active_snapshot_version + 1
            if snapshot.version != expected_version or snapshot_key in self._snapshots:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.VERSION_CONFLICT,
                    "datasource snapshot version is not the next version",
                )
            self._snapshots[snapshot_key] = snapshot
            updated = source.model_copy(
                update={
                    "revision": source.revision + 1,
                    "active_snapshot_version": snapshot.version,
                    "status": DataSourceStatus.READY,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._sources[source_key] = updated
            return updated

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        source_id: str,
        version: int,
    ) -> DataSourceSnapshot:
        async with self._lock:
            snapshot = self._snapshots.get((tenant_id, source_id, version))
            if snapshot is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "datasource snapshot was not found",
                )
            return snapshot

    async def save_binding(
        self,
        binding: SemanticBindingRecord,
    ) -> SemanticBindingRecord:
        key = (binding.tenant_id, binding.binding_id)
        async with self._lock:
            if key in self._bindings:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.ALREADY_EXISTS,
                    "semantic binding already exists",
                )
            if (
                binding.tenant_id,
                binding.source_id,
                binding.source_snapshot_version,
            ) not in self._snapshots:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding source snapshot was not found",
                )
            if binding.status != SemanticBindingStatus.DRAFT:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.INVALID_BINDING,
                    "new semantic bindings must start as drafts",
                )
            self._bindings[key] = binding
            return binding

    async def list_bindings(
        self,
        *,
        tenant_id: str,
        source_id: str,
    ) -> tuple[SemanticBindingRecord, ...]:
        async with self._lock:
            return tuple(
                binding
                for (candidate_tenant, _), binding in sorted(
                    self._bindings.items()
                )
                if candidate_tenant == tenant_id
                and binding.source_id == source_id
            )

    async def activate_binding(
        self,
        *,
        tenant_id: str,
        binding_id: str,
    ) -> SemanticBindingRecord:
        key = (tenant_id, binding_id)
        async with self._lock:
            binding = self._bindings.get(key)
            if binding is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding was not found",
                )
            snapshot = self._snapshots.get(
                (
                    tenant_id,
                    binding.source_id,
                    binding.source_snapshot_version,
                )
            )
            if snapshot is None:
                raise DataSourceRegistryError(
                    DataSourceRegistryErrorCode.NOT_FOUND,
                    "semantic binding source snapshot was not found",
                )
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
            for candidate_key, candidate in tuple(self._bindings.items()):
                if (
                    candidate_key != key
                    and candidate.tenant_id == tenant_id
                    and candidate.source_id == binding.source_id
                    and candidate.domain_id == binding.domain_id
                    and candidate.status == SemanticBindingStatus.ACTIVE
                ):
                    self._bindings[candidate_key] = candidate.model_copy(
                        update={
                            "status": SemanticBindingStatus.RETIRED,
                            "updated_at": now,
                        }
                    )
            activated = binding.model_copy(
                update={
                    "status": SemanticBindingStatus.ACTIVE,
                    "updated_at": now,
                }
            )
            self._bindings[key] = activated
            return activated

    async def pin_conversation(
        self,
        pin: ConversationDataSourcePin,
    ) -> ConversationDataSourcePin:
        key = (pin.tenant_id, pin.user_id, pin.conversation_id)
        async with self._lock:
            current = self._conversation_pins.get(key)
            if current is not None and current != pin:
                comparable = current.model_dump(exclude={"created_at"})
                requested = pin.model_dump(exclude={"created_at"})
                if comparable != requested:
                    raise DataSourceRegistryError(
                        DataSourceRegistryErrorCode.VERSION_CONFLICT,
                        "conversation is already pinned to another datasource binding",
                    )
                return current
            self._conversation_pins[key] = pin
            return pin

    async def get_conversation_pin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> ConversationDataSourcePin | None:
        async with self._lock:
            return self._conversation_pins.get(
                (tenant_id, user_id, conversation_id)
            )


class ConnectorRegistry:
    """Bind immutable datasource versions to connector instances."""

    _EXPECTED_DIALECT = {
        DataSourceKind.POSTGRES: "postgres",
        DataSourceKind.SQLITE: "sqlite",
        DataSourceKind.XLSX: "duckdb",
        DataSourceKind.CSV: "duckdb",
    }

    def __init__(self) -> None:
        self._connectors: dict[tuple[str, str, int], DataSourceConnector] = {}

    def register(
        self,
        definition: DataSourceDefinition,
        connector: DataSourceConnector,
        *,
        source_version: int,
    ) -> None:
        key = (definition.tenant_id, definition.source_id, source_version)
        if key in self._connectors:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.ALREADY_EXISTS,
                "connector is already registered for this datasource version",
            )
        capabilities = connector.capabilities()
        if (
            not capabilities.read_only
            or capabilities.dialect != self._EXPECTED_DIALECT[definition.kind]
        ):
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.CONNECTOR_MISMATCH,
                "connector capabilities do not match datasource kind",
            )
        self._connectors[key] = connector

    def get(
        self,
        *,
        tenant_id: str,
        source_id: str,
        source_version: int,
    ) -> DataSourceConnector:
        connector = self._connectors.get(
            (tenant_id, source_id, source_version)
        )
        if connector is None:
            raise DataSourceRegistryError(
                DataSourceRegistryErrorCode.NOT_FOUND,
                "connector was not found for this datasource version",
            )
        return connector


__all__ = [
    "ConnectorRegistry",
    "DataSourceRegistry",
    "DataSourceRegistryError",
    "DataSourceRegistryErrorCode",
    "InMemoryDataSourceRegistry",
]
