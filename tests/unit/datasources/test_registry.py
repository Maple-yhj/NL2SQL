from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from data_agent.datasources import (
    ConnectorRegistry,
    ConversationDataSourcePin,
    DataSourceDefinition,
    DataSourceKind,
    DataSourceRegistryError,
    DataSourceRegistryErrorCode,
    DataSourceSnapshot,
    DataSourceStatus,
    InMemoryDataSourceRegistry,
    SemanticBindingRecord,
    SemanticBindingStatus,
    SemanticFieldMapping,
    SQLiteDataSourceRegistry,
)
from data_agent.tools.schemas import (
    CatalogColumn,
    CatalogRelation,
    CatalogSnapshot,
    ConnectorCapabilities,
)


def _definition(
    *,
    tenant_id: str = "tenant-a",
    source_id: str = "orders",
) -> DataSourceDefinition:
    return DataSourceDefinition(
        source_id=source_id,
        tenant_id=tenant_id,
        name="Orders",
        kind=DataSourceKind.SQLITE,
        location_ref=f"snapshot://{tenant_id}/{source_id}",
    )


def _snapshot(
    *,
    tenant_id: str = "tenant-a",
    source_id: str = "orders",
    version: int = 1,
) -> DataSourceSnapshot:
    fingerprint = f"sha256:{tenant_id}:{source_id}:{version}"
    return DataSourceSnapshot(
        snapshot_id=f"{source_id}:v{version}",
        tenant_id=tenant_id,
        source_id=source_id,
        version=version,
        fingerprint=fingerprint,
        catalog=CatalogSnapshot(
            schema_fingerprint=fingerprint,
            relations=(
                CatalogRelation(
                    relation="main.orders",
                    columns=(
                        CatalogColumn(
                            name="order_id",
                            data_type="TEXT",
                            nullable=False,
                        ),
                        CatalogColumn(
                            name="amount",
                            data_type="REAL",
                            nullable=False,
                        ),
                    ),
                ),
            ),
        ),
    )


class DataSourceModelTests(unittest.TestCase):
    def test_postgres_requires_credential_reference(self) -> None:
        with self.assertRaises(ValidationError):
            DataSourceDefinition(
                source_id="warehouse",
                tenant_id="tenant-a",
                name="Warehouse",
                kind=DataSourceKind.POSTGRES,
            )

    def test_options_reject_embedded_secrets(self) -> None:
        with self.assertRaises(ValidationError):
            DataSourceDefinition(
                source_id="warehouse",
                tenant_id="tenant-a",
                name="Warehouse",
                kind=DataSourceKind.POSTGRES,
                credential_ref="secret://tenant-a/warehouse",
                options={"password": "must-not-be-stored"},
            )


class DataSourceRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = InMemoryDataSourceRegistry()
        await self.registry.create(_definition())

    async def test_snapshot_publish_is_versioned_and_tenant_isolated(self) -> None:
        updated = await self.registry.publish_snapshot(_snapshot())

        self.assertEqual(updated.status, DataSourceStatus.READY)
        self.assertEqual(updated.active_snapshot_version, 1)
        self.assertEqual(updated.revision, 2)
        self.assertEqual(
            await self.registry.list(tenant_id="tenant-a"),
            (updated,),
        )
        self.assertEqual(
            await self.registry.list(tenant_id="tenant-b"),
            (),
        )

        with self.assertRaises(DataSourceRegistryError) as captured:
            await self.registry.publish_snapshot(_snapshot())
        self.assertEqual(
            captured.exception.code,
            DataSourceRegistryErrorCode.VERSION_CONFLICT,
        )

    async def test_binding_requires_known_catalog_fields_before_activation(self) -> None:
        await self.registry.publish_snapshot(_snapshot())
        binding = SemanticBindingRecord(
            binding_id="orders-binding-v1",
            tenant_id="tenant-a",
            source_id="orders",
            source_snapshot_version=1,
            domain_id="dataset.orders",
            version=1,
            mappings=(
                SemanticFieldMapping(
                    logical_ref="dataset.Order.amount",
                    physical_relation="main.orders",
                    physical_column="amount",
                ),
            ),
        )
        await self.registry.save_binding(binding)
        activated = await self.registry.activate_binding(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
        )
        self.assertEqual(activated.status, SemanticBindingStatus.ACTIVE)

        invalid = binding.model_copy(
            update={
                "binding_id": "orders-binding-v2",
                "version": 2,
                "mappings": (
                    SemanticFieldMapping(
                        logical_ref="dataset.Order.unknown",
                        physical_relation="main.orders",
                        physical_column="unknown",
                    ),
                ),
            }
        )
        await self.registry.save_binding(invalid)
        with self.assertRaises(DataSourceRegistryError) as captured:
            await self.registry.activate_binding(
                tenant_id="tenant-a",
                binding_id=invalid.binding_id,
            )
        self.assertEqual(
            captured.exception.code,
            DataSourceRegistryErrorCode.INVALID_BINDING,
        )

    async def test_delete_cascades_snapshot_binding_and_conversation_pin(self) -> None:
        await self.registry.publish_snapshot(_snapshot())
        binding = SemanticBindingRecord(
            binding_id="orders-binding-v1",
            tenant_id="tenant-a",
            source_id="orders",
            source_snapshot_version=1,
            domain_id="dataset.orders",
            version=1,
            mappings=(
                SemanticFieldMapping(
                    logical_ref="dataset.Order.amount",
                    physical_relation="main.orders",
                    physical_column="amount",
                ),
            ),
        )
        await self.registry.save_binding(binding)
        pin = ConversationDataSourcePin(
            tenant_id="tenant-a",
            user_id="user-a",
            conversation_id="conversation-a",
            domain_id=binding.domain_id,
            source_id=binding.source_id,
            source_version=1,
            binding_id=binding.binding_id,
            binding_version=1,
        )
        await self.registry.pin_conversation(pin)

        deleted = await self.registry.delete(
            tenant_id="tenant-a",
            source_id="orders",
        )

        self.assertEqual(deleted.source_id, "orders")
        self.assertEqual(await self.registry.list(tenant_id="tenant-a"), ())
        self.assertEqual(
            await self.registry.list_bindings(
                tenant_id="tenant-a",
                source_id="orders",
            ),
            (),
        )
        self.assertIsNone(
            await self.registry.get_conversation_pin(
                tenant_id="tenant-a",
                user_id="user-a",
                conversation_id="conversation-a",
            )
        )
        with self.assertRaises(DataSourceRegistryError):
            await self.registry.get_snapshot(
                tenant_id="tenant-a",
                source_id="orders",
                version=1,
            )


class SQLiteDataSourceRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_and_active_binding_survive_registry_restart(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "control.sqlite3"
            registry = SQLiteDataSourceRegistry(database_path)
            await registry.create(_definition())
            updated = await registry.publish_snapshot(_snapshot())
            binding = SemanticBindingRecord(
                binding_id="orders-binding-v1",
                tenant_id="tenant-a",
                source_id="orders",
                source_snapshot_version=1,
                domain_id="dataset.orders",
                version=1,
                mappings=(
                    SemanticFieldMapping(
                        logical_ref="dataset.Order.amount",
                        physical_relation="main.orders",
                        physical_column="amount",
                    ),
                ),
            )
            await registry.save_binding(binding)
            await registry.activate_binding(
                tenant_id="tenant-a",
                binding_id=binding.binding_id,
            )
            pin = ConversationDataSourcePin(
                tenant_id="tenant-a",
                user_id="user-a",
                conversation_id="conversation-a",
                domain_id=binding.domain_id,
                source_id=binding.source_id,
                source_version=1,
                binding_id=binding.binding_id,
                binding_version=1,
            )
            await registry.pin_conversation(pin)

            restarted = SQLiteDataSourceRegistry(database_path)
            self.assertEqual(
                await restarted.list(tenant_id="tenant-a"),
                (updated,),
            )
            restored_bindings = await restarted.list_bindings(
                tenant_id="tenant-a",
                source_id="orders",
            )
            self.assertEqual(len(restored_bindings), 1)
            self.assertEqual(
                restored_bindings[0].status,
                SemanticBindingStatus.ACTIVE,
            )
            self.assertEqual(
                (
                    await restarted.get_snapshot(
                        tenant_id="tenant-a",
                        source_id="orders",
                        version=1,
                    )
                ).fingerprint,
                _snapshot().fingerprint,
            )
            self.assertEqual(
                await restarted.get_conversation_pin(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    conversation_id="conversation-a",
                ),
                pin,
            )

            conflicting = pin.model_copy(
                update={"binding_id": "another-binding"}
            )
            with self.assertRaises(DataSourceRegistryError) as captured:
                await restarted.pin_conversation(conflicting)
            self.assertEqual(
                captured.exception.code,
                DataSourceRegistryErrorCode.VERSION_CONFLICT,
            )

            deleted = await restarted.delete(
                tenant_id="tenant-a",
                source_id="orders",
            )
            self.assertEqual(deleted.source_id, "orders")
            after_delete = SQLiteDataSourceRegistry(database_path)
            self.assertEqual(
                await after_delete.list(tenant_id="tenant-a"),
                (),
            )
            self.assertEqual(
                await after_delete.list_bindings(
                    tenant_id="tenant-a",
                    source_id="orders",
                ),
                (),
            )
            self.assertIsNone(
                await after_delete.get_conversation_pin(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    conversation_id="conversation-a",
                )
            )


class _SQLiteConnectorStub:
    @staticmethod
    def capabilities() -> ConnectorCapabilities:
        return ConnectorCapabilities(dialect="sqlite")


class _WritableConnectorStub:
    @staticmethod
    def capabilities() -> ConnectorCapabilities:
        return ConnectorCapabilities(dialect="sqlite", read_only=False)


class ConnectorRegistryTests(unittest.TestCase):
    def test_connector_must_match_source_kind_and_be_read_only(self) -> None:
        registry = ConnectorRegistry()
        definition = _definition()
        connector = _SQLiteConnectorStub()
        registry.register(definition, connector, source_version=1)  # type: ignore[arg-type]
        self.assertIs(
            registry.get(
                tenant_id=definition.tenant_id,
                source_id=definition.source_id,
                source_version=1,
            ),
            connector,
        )
        self.assertEqual(
            registry.remove(
                tenant_id=definition.tenant_id,
                source_id=definition.source_id,
            ),
            1,
        )
        with self.assertRaises(DataSourceRegistryError):
            registry.get(
                tenant_id=definition.tenant_id,
                source_id=definition.source_id,
                source_version=1,
            )

        with self.assertRaises(DataSourceRegistryError) as captured:
            ConnectorRegistry().register(
                definition,
                _WritableConnectorStub(),  # type: ignore[arg-type]
                source_version=1,
            )
        self.assertEqual(
            captured.exception.code,
            DataSourceRegistryErrorCode.CONNECTOR_MISMATCH,
        )


if __name__ == "__main__":
    unittest.main()
