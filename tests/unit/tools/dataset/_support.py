from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import duckdb
from pydantic import BaseModel

from data_agent.analysis_agent.artifacts import SQLiteArtifactStore
from data_agent.analysis_agent.models import DatasetAuthority
from data_agent.dataset_query import DatasetQueryCompiler, DatasetQueryExecutor
from data_agent.datasources import SemanticBindingRecord, SemanticFieldMapping
from data_agent.runtime.models import AgentMode, PrincipalContext
from data_agent.tools import ToolBudget, ToolCall, ToolInvocationContext, ToolInvoker
from data_agent.tools.connectors import DuckDBConnector
from data_agent.tools.providers.dataset import (
    DatasetCredentialBroker,
    DatasetToolRuntime,
    build_dataset_tool_registry,
)
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot
from data_agent.semantic_metrics import EffectiveMetricCatalog


class DatasetToolHarness:
    tenant_id = "tenant-1"
    user_id = "user-1"
    source_id = "orders-source"
    source_version = 1
    binding_id = "orders-binding"
    binding_version = 1
    connection_ref = "snapshot://tenant-1/orders-source/v1"
    schema_fingerprint = "sha256:" + "a" * 64
    bundle_digest = hashlib.sha256(b"dataset-tool-harness").hexdigest()

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        database_path = self.root / "orders.duckdb"
        connection = duckdb.connect(str(database_path))
        try:
            connection.execute("CREATE SCHEMA public")
            connection.execute(
                "CREATE TABLE public.orders (state VARCHAR, amount DOUBLE, quantity INTEGER)"
            )
            connection.executemany(
                "INSERT INTO public.orders VALUES (?, ?, ?)",
                (("RJ", 10.0, 1), ("SP", 20.0, 2), ("SP", 35.0, 3)),
            )
        finally:
            connection.close()

        self.catalog = CatalogSnapshot(
            schema_fingerprint=self.schema_fingerprint,
            relations=(
                CatalogRelation(
                    relation="public.orders",
                    columns=(
                        CatalogColumn(name="state", data_type="VARCHAR", nullable=False),
                        CatalogColumn(name="amount", data_type="DOUBLE", nullable=False),
                        CatalogColumn(name="quantity", data_type="INTEGER", nullable=False),
                    ),
                    estimated_rows=3,
                ),
            ),
        )
        self.binding = SemanticBindingRecord(
            binding_id=self.binding_id,
            tenant_id=self.tenant_id,
            source_id=self.source_id,
            source_snapshot_version=self.source_version,
            domain_id="dataset-orders",
            version=self.binding_version,
            status="active",
            mappings=(
                SemanticFieldMapping(
                    logical_ref="dataset.orders.state",
                    physical_relation="public.orders",
                    physical_column="state",
                ),
                SemanticFieldMapping(
                    logical_ref="dataset.orders.amount",
                    physical_relation="public.orders",
                    physical_column="amount",
                ),
                SemanticFieldMapping(
                    logical_ref="dataset.orders.quantity",
                    physical_relation="public.orders",
                    physical_column="quantity",
                ),
            ),
        )
        self.connector = DuckDBConnector(
            database_path,
            allowed_relations=("public.orders",),
            schema_fingerprint=self.schema_fingerprint,
            source=self.source_id,
            connection_ref=self.connection_ref,
            bundle_digest=self.bundle_digest,
        )
        self.artifacts = SQLiteArtifactStore(self.root / "state")
        self.registry = build_dataset_tool_registry()
        self.principal = PrincipalContext(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            roles=("analyst",),
        )

    def close(self) -> None:
        self.temporary.cleanup()

    def authority(
        self,
        mode: AgentMode,
        *,
        allowed_relations: tuple[str, ...] = ("public.orders",),
    ) -> DatasetAuthority:
        return DatasetAuthority(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            source_id=self.source_id,
            source_version=self.source_version,
            binding_id=self.binding_id,
            binding_version=self.binding_version,
            schema_fingerprint=self.schema_fingerprint,
            allowed_relation_ids=allowed_relations,
            mode=mode,
        )

    def invocation(
        self,
        mode: AgentMode,
        *,
        run_id: str = "run-1",
        max_rows: int = 100,
        statement_timeout_ms: int = 2_500,
        allowed_relations: tuple[str, ...] = ("public.orders",),
        credential_broker=None,
    ) -> tuple[DatasetToolRuntime, ToolInvoker, ToolInvocationContext]:
        authority = self.authority(mode, allowed_relations=allowed_relations)
        runtime = DatasetToolRuntime(
            authority=authority,
            catalog=self.catalog,
            binding=self.binding,
            metric_catalog=EffectiveMetricCatalog.build(),
            connector=self.connector,
            connection_ref=self.connection_ref,
            bundle_digest=self.bundle_digest,
            artifacts=self.artifacts,
            compiler=DatasetQueryCompiler(),
            executor=DatasetQueryExecutor(),
        )
        invoker = ToolInvoker(
            self.registry,
            credential_broker=credential_broker or DatasetCredentialBroker(runtime),
        )
        context = ToolInvocationContext(
            principal=self.principal,
            skill_id="dataset.analytics",
            skill_version="1.0.0",
            allowed_tools=self.registry.names(),
            budget=ToolBudget(max_calls=40),
            authority=authority,
            mode=mode,
            runtime_resources=runtime,
            max_rows=max_rows,
            statement_timeout_ms=statement_timeout_ms,
            run_id=run_id,
        )
        return runtime, invoker, context


async def invoke(
    invoker: ToolInvoker,
    context: ToolInvocationContext,
    *,
    call_id: str,
    tool_name: str,
    input_data: BaseModel,
):
    required_idempotency = tool_name in {
        "data.profile",
        "query.execute",
        "query.explain",
        "query.preview",
    }
    return await invoker.invoke(
        ToolCall(
            call_id=call_id,
            tool_name=tool_name,
            tool_version="1.0.0",
            input_data=input_data,
            idempotency_key=(
                f"idempotency-{call_id}" if required_idempotency else None
            ),
        ),
        context,
    )
