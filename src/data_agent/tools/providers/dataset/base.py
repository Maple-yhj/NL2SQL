"""Shared validation and artifact helpers for dataset tool providers."""

from __future__ import annotations

from pydantic import JsonValue
from pydantic_core import to_jsonable_python

from data_agent.analysis_agent.models import DatasetAuthority
from data_agent.dataset_query import DatasetExecutionAuthority
from data_agent.dataset_query.contracts import PreparedQuery
from data_agent.tools.models import ProviderContext, ToolErrorCode

from .contracts import DatasetArtifactOutput, DatasetToolRuntime


class DatasetProviderError(ValueError):
    def __init__(self, code: ToolErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


def dataset_runtime(context: ProviderContext) -> DatasetToolRuntime:
    if not isinstance(context.authority, DatasetAuthority):
        raise DatasetProviderError(
            ToolErrorCode.ACCESS_DENIED,
            "dataset tool requires dataset authority",
        )
    runtime = context.runtime_resources
    if not isinstance(runtime, DatasetToolRuntime):
        raise DatasetProviderError(
            ToolErrorCode.CONNECTOR_UNAVAILABLE,
            "dataset runtime resources are unavailable",
        )
    if runtime.authority != context.authority:
        raise DatasetProviderError(
            ToolErrorCode.BINDING_STALE,
            "dataset runtime authority changed",
        )
    if (
        runtime.binding.source_id != context.authority.source_id
        or runtime.binding.source_snapshot_version != context.authority.source_version
        or runtime.binding.binding_id != context.authority.binding_id
        or runtime.binding.version != context.authority.binding_version
    ):
        raise DatasetProviderError(
            ToolErrorCode.BINDING_STALE,
            "dataset binding pins changed",
        )
    return runtime


def execution_authority(
    runtime: DatasetToolRuntime,
    context: ProviderContext,
) -> DatasetExecutionAuthority:
    return DatasetExecutionAuthority(
        tenant_id=runtime.authority.tenant_id,
        user_id=runtime.authority.user_id,
        source_id=runtime.authority.source_id,
        bundle_digest=runtime.bundle_digest,
        schema_fingerprint=runtime.authority.schema_fingerprint,
        connection_ref=runtime.connection_ref,
        allowed_relations=runtime.authority.allowed_relation_ids,
        max_rows=context.access_grant.max_rows,
        statement_timeout_ms=context.access_grant.statement_timeout_ms,
    )


def validate_prepared_authority(
    prepared: PreparedQuery,
    context: ProviderContext,
) -> None:
    runtime = dataset_runtime(context)
    if not set(prepared.allowed_relations).issubset(
        set(runtime.authority.allowed_relation_ids)
    ):
        raise DatasetProviderError(
            ToolErrorCode.RELATION_NOT_ALLOWED,
            "prepared query relations exceed dataset authority",
        )
    if prepared.max_rows > context.access_grant.max_rows:
        raise DatasetProviderError(
            ToolErrorCode.ROW_LIMIT_EXCEEDED,
            "prepared query row limit exceeds dataset authority",
        )


async def store_output(
    *,
    context: ProviderContext,
    kind: str,
    payload: JsonValue,
    summary: str,
    sensitivity: str,
    row_count: int | None = None,
    schema_digest: str | None = None,
) -> DatasetArtifactOutput:
    runtime = dataset_runtime(context)
    authority = context.authority
    assert isinstance(authority, DatasetAuthority)
    artifact = await runtime.artifacts.put_json(
        tenant_id=authority.tenant_id,
        user_id=authority.user_id,
        run_id=context.run_id,
        call_id=context.call_id,
        kind=kind,
        payload=to_jsonable_python(payload),
        sensitivity=sensitivity,
        row_count=row_count,
        schema_digest=schema_digest,
    )
    preview = await runtime.artifacts.get_safe_preview(
        tenant_id=authority.tenant_id,
        user_id=authority.user_id,
        run_id=artifact.run_id,
        artifact_id=artifact.artifact_id,
    )
    return DatasetArtifactOutput(
        summary=summary,
        artifact=artifact,
        safe_preview=preview,
    )


__all__ = [
    "DatasetProviderError",
    "dataset_runtime",
    "execution_authority",
    "store_output",
    "validate_prepared_authority",
]
