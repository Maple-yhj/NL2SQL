"""Governed logical-plan compilation and read-only query execution providers."""

from __future__ import annotations

from data_agent.runtime.binding import BindingCompiler

from ..models import ProviderContext, RetryPolicy, ToolErrorCode, ToolSpec
from .contracts import (
    QueryCompileInput,
    QueryCompileOutput,
    QueryData,
    QueryExecuteInput,
    QueryExecutionOutput,
    QueryMode,
)
from .evidence import EvidenceSigner


QUERY_COMPILE_SPEC = ToolSpec(
    name="query.compile",
    version="1.0.0",
    description="Bind a governed logical plan and compile a parameterized PostgreSQL query.",
    input_schema=QueryCompileInput,
    output_schema=QueryCompileOutput,
    risk_level="medium",
    side_effects="none",
    required_capabilities=("query.compile",),
    idempotency="safe",
    timeout_seconds=5,
    retry_policy=RetryPolicy(max_attempts=1),
    eval_tags=("binding", "compiler", "offline"),
)


QUERY_EXECUTE_SPEC = ToolSpec(
    name="query.execute",
    version="1.0.0",
    description="Explain, preview, or execute a verified read-only PreparedQuery.",
    input_schema=QueryExecuteInput,
    output_schema=QueryExecutionOutput,
    risk_level="high",
    side_effects="read",
    required_capabilities=("query.execute",),
    idempotency="safe",
    timeout_seconds=10,
    retry_policy=RetryPolicy(max_attempts=1),
    eval_tags=("postgres", "authorized-read"),
)


class QueryProviderError(PermissionError):
    """Typed provider boundary error preserved by the governed invoker."""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class QueryCompileProvider:
    spec = QUERY_COMPILE_SPEC

    def __init__(self, compiler: BindingCompiler) -> None:
        self._compiler = compiler

    async def invoke(
        self,
        payload: QueryCompileInput,
        context: ProviderContext,
    ) -> QueryCompileOutput:
        bound = self._compiler.bind(payload.logical_plan, context.principal)
        prepared = self._compiler.compile(bound, context.principal)
        if prepared.policy_decision_id != context.access_grant.policy_decision_id:
            raise QueryProviderError(
                ToolErrorCode.POLICY_VIOLATION,
                "trusted policy decision mismatch",
            )
        return QueryCompileOutput(bound_plan=bound, prepared_query=prepared)


class QueryExecuteProvider:
    spec = QUERY_EXECUTE_SPEC

    def __init__(
        self,
        connector: object,
        compiler: BindingCompiler,
        evidence_signer: EvidenceSigner,
    ) -> None:
        self._connector = connector
        self._compiler = compiler
        self._evidence_signer = evidence_signer

    async def invoke(
        self,
        payload: QueryExecuteInput,
        context: ProviderContext,
    ) -> QueryExecutionOutput:
        prepared = payload.prepared_query
        grant = context.access_grant
        expected = self._compiler.compile(
            self._compiler.bind(prepared.logical_plan, context.principal),
            context.principal,
        )
        if (
            prepared != expected
            or
            prepared.policy_decision_id != grant.policy_decision_id
            or prepared.sql_ast_hash != grant.prepared_query_hash
        ):
            raise QueryProviderError(
                ToolErrorCode.POLICY_VIOLATION,
                "prepared query is not bound to the access grant",
            )
        if payload.mode == QueryMode.EXPLAIN:
            if context.credential is None:
                raise QueryProviderError(
                    ToolErrorCode.ACCESS_DENIED,
                    "query execution requires a credential lease",
                )
            explain = await self._connector.explain(
                prepared,
                grant,
                context.credential,
            )
            return QueryExecutionOutput(mode=payload.mode, explain=explain)
        if payload.mode == QueryMode.PREVIEW:
            if context.credential is None:
                raise QueryProviderError(
                    ToolErrorCode.ACCESS_DENIED,
                    "query execution requires a credential lease",
                )
            table = await self._connector.preview(
                prepared,
                grant,
                context.credential,
                preview_rows=payload.preview_rows,
            )
        else:
            if context.credential is None:
                raise QueryProviderError(
                    ToolErrorCode.ACCESS_DENIED,
                    "query execution requires a credential lease",
                )
            table = await self._connector.execute_readonly(
                prepared,
                grant,
                context.credential,
            )
        rows = table.rows
        verification_token = self._evidence_signer.sign(
            logical_plan_hash=prepared.logical_plan_hash,
            query_hash=prepared.sql_ast_hash,
            policy_decision_id=prepared.policy_decision_id,
            columns=table.columns,
            rows=rows,
        )
        data = QueryData(
            logical_plan_hash=prepared.logical_plan_hash,
            query_hash=prepared.sql_ast_hash,
            policy_decision_id=prepared.policy_decision_id,
            verification_token=verification_token,
            columns=table.columns,
            rows=rows,
        )
        return QueryExecutionOutput(mode=payload.mode, data=data)
