"""Compile, explain, preview and execute governed dataset queries."""

from __future__ import annotations

from data_agent.relationships.router import GraphRouteError
from data_agent.runtime.models import AgentMode
from data_agent.tools.models import ProviderContext, ToolErrorCode, ToolSpec

from .base import (
    DatasetProviderError,
    dataset_runtime,
    execution_authority,
    store_output,
    validate_prepared_authority,
)
from .contracts import (
    DatasetArtifactOutput,
    PreparedQueryArtifactPayload,
    QueryCompileInput,
    QueryCompileOutput,
    QueryRunInput,
    prepared_from_payload,
)


def _spec(
    name: str,
    input_schema,
    output_schema,
    *,
    modes: tuple[AgentMode, ...],
    credential: bool,
    artifact_policy: str,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=f"Governed dataset operation {name}",
        input_schema=input_schema,
        output_schema=output_schema,
        risk_level="medium" if credential else "low",
        side_effects="read" if credential else "none",
        required_capabilities=(name,),
        idempotency="required" if credential else "safe",
        timeout_seconds=30,
        authority_kinds=("dataset",),
        allowed_modes=modes,
        artifact_policy=artifact_policy,
        credential_requirement="required" if credential else "none",
    )


QUERY_COMPILE_SPEC = _spec(
    "query.compile",
    QueryCompileInput,
    QueryCompileOutput,
    modes=(AgentMode.PLAN, AgentMode.PREVIEW, AgentMode.EXECUTE),
    credential=False,
    artifact_policy="metadata",
)
QUERY_EXPLAIN_SPEC = _spec(
    "query.explain",
    QueryRunInput,
    DatasetArtifactOutput,
    modes=(AgentMode.PREVIEW, AgentMode.EXECUTE),
    credential=True,
    artifact_policy="metadata",
)
QUERY_PREVIEW_SPEC = _spec(
    "query.preview",
    QueryRunInput,
    DatasetArtifactOutput,
    modes=(AgentMode.PREVIEW, AgentMode.EXECUTE),
    credential=True,
    artifact_policy="row_data",
)
QUERY_EXECUTE_SPEC = _spec(
    "query.execute",
    QueryRunInput,
    DatasetArtifactOutput,
    modes=(AgentMode.EXECUTE,),
    credential=True,
    artifact_policy="row_data",
)


async def _load_prepared(payload: QueryRunInput, context: ProviderContext):
    runtime = dataset_runtime(context)
    document = await runtime.artifacts.get_json(
        tenant_id=runtime.authority.tenant_id,
        user_id=runtime.authority.user_id,
        run_id=context.run_id,
        artifact_id=payload.artifact_id,
    )
    prepared = prepared_from_payload(document)
    validate_prepared_authority(prepared, context)
    return runtime, prepared


class QueryCompileProvider:
    spec = QUERY_COMPILE_SPEC

    async def invoke(
        self,
        payload: QueryCompileInput,
        context: ProviderContext,
    ) -> QueryCompileOutput:
        runtime = dataset_runtime(context)
        dialect = runtime.connector.capabilities().dialect
        if dialect not in {"postgres", "sqlite", "duckdb"}:
            raise DatasetProviderError(
                ToolErrorCode.SQL_COMPILE_ERROR,
                "the active dataset connector dialect is not supported",
            )
        try:
            prepared = runtime.compiler.compile(
                plan=payload.plan,
                binding=runtime.binding,
                catalog=runtime.catalog,
                dialect=dialect,
                schema_fingerprint=runtime.authority.schema_fingerprint,
                bundle_digest=runtime.bundle_digest,
                metric_catalog=runtime.metric_catalog,
            )
        except GraphRouteError as exc:
            graph_codes = {
                "GRAPH_NO_PATH": ToolErrorCode.GRAPH_NO_PATH,
                "GRAPH_AMBIGUOUS_PATH": ToolErrorCode.GRAPH_AMBIGUOUS_PATH,
                "GRAPH_UNSAFE_FANOUT": ToolErrorCode.GRAPH_UNSAFE_FANOUT,
            }
            raw_code = str(getattr(exc.code, "value", exc.code))
            raise DatasetProviderError(
                graph_codes.get(raw_code, ToolErrorCode.LOGICAL_PLAN_INVALID),
                f"query stage relationship route is invalid ({raw_code})",
            ) from exc
        except ValueError as exc:
            raise DatasetProviderError(
                ToolErrorCode.SQL_COMPILE_ERROR,
                "governed query program could not be compiled because it references "
                "unavailable logical fields or unsupported operations",
            ) from exc
        validate_prepared_authority(prepared, context)
        stored = await store_output(
            context=context,
            kind="prepared_query",
            payload=PreparedQueryArtifactPayload(
                plan=payload.plan,
                prepared=prepared,
            ).model_dump(mode="json"),
            summary="Compiled a deterministic read-only query",
            sensitivity="metadata",
            schema_digest=runtime.authority.schema_fingerprint,
        )
        return QueryCompileOutput(
            **stored.model_dump(mode="python"),
            logical_plan_hash=prepared.logical_plan_hash,
            query_hash=prepared.sql_ast_hash,
            policy_decision_id=prepared.policy_decision_id,
        )


class QueryExplainProvider:
    spec = QUERY_EXPLAIN_SPEC

    async def invoke(self, payload: QueryRunInput, context: ProviderContext) -> DatasetArtifactOutput:
        runtime, prepared = await _load_prepared(payload, context)
        explained = await runtime.executor.explain(
            prepared=prepared,
            authority=execution_authority(runtime, context),
            connector=runtime.connector,
        )
        return await store_output(
            context=context,
            kind="profile",
            payload=explained.model_dump(mode="json"),
            summary="Explained the deterministic query",
            sensitivity="metadata",
        )


class QueryPreviewProvider:
    spec = QUERY_PREVIEW_SPEC

    async def invoke(self, payload: QueryRunInput, context: ProviderContext) -> DatasetArtifactOutput:
        runtime, prepared = await _load_prepared(payload, context)
        result = await runtime.executor.execute(
            prepared=prepared,
            authority=execution_authority(runtime, context),
            connector=runtime.connector,
            mode=AgentMode.PREVIEW,
            preview_rows=payload.preview_rows,
        )
        return await store_output(
            context=context,
            kind="query_preview",
            payload=result.model_dump(mode="json"),
            summary=f"Preview returned {len(result.rows)} rows",
            sensitivity="row_data",
            row_count=len(result.rows),
        )


class QueryExecuteProvider:
    spec = QUERY_EXECUTE_SPEC

    async def invoke(self, payload: QueryRunInput, context: ProviderContext) -> DatasetArtifactOutput:
        runtime, prepared = await _load_prepared(payload, context)
        result = await runtime.executor.execute(
            prepared=prepared,
            authority=execution_authority(runtime, context),
            connector=runtime.connector,
            mode=AgentMode.EXECUTE,
        )
        return await store_output(
            context=context,
            kind="query_result",
            payload=result.model_dump(mode="json"),
            summary=f"Query returned {len(result.rows)} rows",
            sensitivity="row_data",
            row_count=len(result.rows),
        )


__all__ = [
    "QUERY_COMPILE_SPEC",
    "QUERY_EXECUTE_SPEC",
    "QUERY_EXPLAIN_SPEC",
    "QUERY_PREVIEW_SPEC",
    "QueryCompileProvider",
    "QueryExecuteProvider",
    "QueryExplainProvider",
    "QueryPreviewProvider",
]
