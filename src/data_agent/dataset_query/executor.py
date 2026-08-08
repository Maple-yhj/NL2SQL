"""Governed read-only execution of compiler-created dataset queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import uuid4

from data_agent.dataset_query.contracts import PreparedQuery
from data_agent.runtime.models import AgentMode
from data_agent.tools import AccessGrant, CredentialLease
from data_agent.tools.connectors import DataSourceConnector
from data_agent.tools.schemas import ExplainResult, TabularResult


@dataclass(frozen=True, slots=True)
class DatasetExecutionAuthority:
    tenant_id: str
    user_id: str
    source_id: str
    bundle_digest: str
    schema_fingerprint: str
    connection_ref: str
    allowed_relations: tuple[str, ...] | None = None
    max_rows: int | None = None
    statement_timeout_ms: int | None = None


class DatasetQueryExecutor:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        grant_seconds: int = 60,
        statement_timeout_ms: int = 15_000,
    ) -> None:
        if grant_seconds < 1 or statement_timeout_ms < 1:
            raise ValueError("execution grant limits must be positive")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._grant_seconds = grant_seconds
        self._statement_timeout_ms = statement_timeout_ms

    async def execute(
        self,
        *,
        prepared: PreparedQuery,
        authority: DatasetExecutionAuthority,
        connector: DataSourceConnector,
        mode: AgentMode,
        preview_rows: int = 20,
    ) -> TabularResult:
        if mode == AgentMode.PLAN:
            raise ValueError("plan mode cannot execute a dataset query")
        if prepared.bundle_digest != authority.bundle_digest:
            raise ValueError("prepared query and execution authority bundle differ")
        if prepared.schema_fingerprint != authority.schema_fingerprint:
            raise ValueError("prepared query and execution authority schema differ")
        if preview_rows < 1:
            raise ValueError("preview_rows must be positive")
        grant, lease = self.authorize(prepared=prepared, authority=authority)
        if mode == AgentMode.PREVIEW:
            return await connector.preview(
                prepared,
                grant,
                lease,
                preview_rows=min(preview_rows, prepared.max_rows),
            )
        return await connector.execute_readonly(prepared, grant, lease)

    async def explain(
        self,
        *,
        prepared: PreparedQuery,
        authority: DatasetExecutionAuthority,
        connector: DataSourceConnector,
    ) -> ExplainResult:
        grant, lease = self.authorize(prepared=prepared, authority=authority)
        return await connector.explain(prepared, grant, lease)

    def authorize(
        self,
        *,
        prepared: PreparedQuery,
        authority: DatasetExecutionAuthority,
    ) -> tuple[AccessGrant, CredentialLease]:
        if prepared.bundle_digest != authority.bundle_digest:
            raise ValueError("prepared query and execution authority bundle differ")
        if prepared.schema_fingerprint != authority.schema_fingerprint:
            raise ValueError("prepared query and execution authority schema differ")
        if authority.allowed_relations is not None and not set(
            prepared.allowed_relations
        ).issubset(set(authority.allowed_relations)):
            raise ValueError("prepared query relations exceed execution authority")
        if authority.max_rows is not None and prepared.max_rows > authority.max_rows:
            raise ValueError("prepared query row limit exceeds execution authority")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("execution clock must return a timezone-aware datetime")
        expires_at = now + timedelta(seconds=self._grant_seconds)
        grant_id = "dataset-grant-" + self._id_factory()
        grant = AccessGrant(
            grant_id=grant_id,
            tool_name="query.execute",
            tool_version="1.0.0",
            skill_id="dataset.analytics",
            bundle_digest=authority.bundle_digest,
            schema_fingerprint=authority.schema_fingerprint,
            source=authority.source_id,
            principal_user_id=authority.user_id,
            tenant_id=authority.tenant_id,
            admin_bypass=False,
            allowed_relations=prepared.allowed_relations,
            max_rows=prepared.max_rows,
            statement_timeout_ms=min(
                self._statement_timeout_ms,
                authority.statement_timeout_ms or self._statement_timeout_ms,
            ),
            policy_decision_id=prepared.policy_decision_id,
            logical_plan_hash=prepared.logical_plan_hash,
            prepared_query_hash=prepared.sql_ast_hash,
            issued_at=now,
            expires_at=expires_at,
        )
        lease = CredentialLease(
            credential_id="dataset-lease-" + self._id_factory(),
            grant_id=grant_id,
            bundle_digest=authority.bundle_digest,
            source=authority.source_id,
            connection_ref=authority.connection_ref,
            capabilities=("query.execute",),
            secret="broker-authorized-readonly-lease",
            issued_at=now,
            expires_at=expires_at,
        )
        return grant, lease


__all__ = ["DatasetExecutionAuthority", "DatasetQueryExecutor"]
