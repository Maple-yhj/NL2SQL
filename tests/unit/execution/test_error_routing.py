from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.execution import (
    COMMERCE_EXECUTION_GRAPH,
    ErrorCode,
    ExecutionDependencies,
    ExecutionFault,
    ExecutionStatus,
    InternalGraphExecutor,
)
from data_agent.runtime.binding import BindingError, BindingErrorCode
from data_agent.runtime.models import AgentMode
from data_agent.tools import (
    CredentialLease,
    ToolInvoker,
    ToolLineage,
    ToolRegistry,
    ToolResult,
    ToolTrace,
)
from data_agent.tools.connectors import ConnectorError, ConnectorErrorCode
from data_agent.tools.providers import (
    DataInspectOutput,
    QUERY_COMPILE_SPEC,
    QUERY_EXECUTE_SPEC,
    QueryExecutionOutput,
    QueryMode,
)
from data_agent.tools.schemas import CatalogSnapshot, ExplainResult
from tests.unit.execution import test_internal_executor as fixture
from tests.unit.tools import test_builtin_providers as provider_fixture


class _InspectableInvoker(fixture._ScriptedInvoker):
    async def invoke(self, call, context):
        if call.tool_name != "data.inspect":
            return await super().invoke(call, context)
        self.calls.append(call)
        output = DataInspectOutput(
            catalog=CatalogSnapshot(
                schema_fingerprint=context.bundle.schema_fingerprint,
                relations=(),
            )
        )
        now = datetime.now(UTC)
        trace = ToolTrace(
            call_id=call.call_id,
            tool_name=call.tool_name,
            tool_version=call.tool_version,
            status="success",
            attempts=1,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            input_schema=type(call.input_data).__name__,
            output_schema=type(output).__name__,
        )
        return ToolResult(
            status="success",
            typed_data=output,
            lineage=ToolLineage(),
            policy_decision_id=self.compiled.prepared_query.policy_decision_id,
            redacted_trace=trace,
        )


class _FaultOnceExecutor(InternalGraphExecutor):
    def __init__(self, *args, fault_node: str, fault_code: ErrorCode, retryable=True):
        super().__init__(*args)
        self.fault_node = fault_node
        self.fault_code = fault_code
        self.retryable = retryable
        self.fired = False

    async def _execute_node(self, node, state, frame):
        if node.id == self.fault_node and not self.fired:
            self.fired = True
            raise ExecutionFault(
                self.fault_code,
                f"injected {self.fault_code.value}",
                retryable=self.retryable,
            )
        return await super()._execute_node(node, state, frame)


class _BindingFailureProvider:
    spec = QUERY_COMPILE_SPEC

    def __init__(self, compiled, code: BindingErrorCode) -> None:
        self.compiled = compiled
        self.code = code
        self.failed = False

    async def invoke(self, payload, context):
        if not self.failed:
            self.failed = True
            raise BindingError(self.code, "typed provider failure")
        return self.compiled


class _RowLimitProvider:
    spec = QUERY_EXECUTE_SPEC

    def __init__(self) -> None:
        self.failed_preview = False

    async def invoke(self, payload, context):
        prepared = payload.prepared_query
        if payload.mode == QueryMode.EXPLAIN:
            return QueryExecutionOutput(
                mode=payload.mode,
                explain=ExplainResult(
                    plan_text='[{"Plan":{"Total Cost":2.5,"Plan Rows":3}}]',
                    estimated_cost=2.5,
                    estimated_rows=3,
                ),
            )
        if payload.mode == QueryMode.PREVIEW and not self.failed_preview:
            self.failed_preview = True
            raise ConnectorError(
                ConnectorErrorCode.ROW_LIMIT_EXCEEDED,
                "typed row limit",
            )
        return QueryExecutionOutput(
            mode=payload.mode,
            data=fixture._ScriptedInvoker._query_data(
                prepared,
                row_count=2 if payload.mode == QueryMode.PREVIEW else 3,
            ),
        )


class _LeaseBroker:
    async def acquire(self, *, grant, source: str | None):
        now = datetime.now(UTC)
        return CredentialLease(
            credential_id="execution-error-lease",
            grant_id=grant.grant_id,
            bundle_digest=grant.bundle_digest,
            source=source,
            connection_ref="secret://olist/local/database",
            capabilities=(grant.tool_name,),
            secret="postgresql://redacted",
            issued_at=now,
            expires_at=grant.expires_at,
        )


def _real_invoker(provider, *, credential_broker=None) -> ToolInvoker:
    registry = ToolRegistry(version="1.0.0")
    registry.register(provider.spec, provider)
    registry.freeze()
    return ToolInvoker(registry, credential_broker=credential_broker)


class _HybridInvoker(_InspectableInvoker):
    def __init__(self, compiled, tool_name: str, real_invoker: ToolInvoker) -> None:
        super().__init__(compiled)
        self.tool_name = tool_name
        self.real_invoker = real_invoker
        self.real_results = []

    async def invoke(self, call, context):
        if call.tool_name != self.tool_name:
            return await super().invoke(call, context)
        self.calls.append(call)
        result = await self.real_invoker.invoke(call, context)
        self.real_results.append(result)
        return result


class ErrorRoutingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture.InternalExecutorTests.setUpClass()
        cls.domain = fixture.InternalExecutorTests.domain
        cls.plan = fixture.InternalExecutorTests.plan
        cls.compiled = fixture.InternalExecutorTests.compiled
        cls.bundle = fixture.InternalExecutorTests.bundle
        cls.principal = fixture.InternalExecutorTests.principal
        cls.context_factory = fixture.InternalExecutorTests._context

    def _context(self, mode: AgentMode):
        return self.context_factory(mode)

    async def _run_fault(
        self,
        *,
        node: str,
        code: ErrorCode,
        mode: AgentMode,
        retryable: bool = True,
    ):
        invoker = _InspectableInvoker(self.compiled)
        executor = _FaultOnceExecutor(
            COMMERCE_EXECUTION_GRAPH,
            ExecutionDependencies(
                invoker=invoker,
                context_resolver=fixture._Resolver(),
                planner=fixture._Planner(self.plan),
                domain_pack=self.domain,
            ),
            fault_node=node,
            fault_code=code,
            retryable=retryable,
        )
        result = await executor.execute(self._context(mode))
        return result, invoker

    async def test_all_recoverable_error_classes_follow_their_bounded_route(self) -> None:
        cases = (
            (
                ErrorCode.LOGICAL_PLAN_INVALID,
                "validate_logical_plan",
                "build_logical_plan",
                AgentMode.PLAN,
            ),
            (
                ErrorCode.BINDING_STALE,
                "compile_query",
                "inspect_binding",
                AgentMode.PREVIEW,
            ),
            (
                ErrorCode.SQL_COMPILE_ERROR,
                "compile_query",
                "compile_query",
                AgentMode.PLAN,
            ),
            (
                ErrorCode.SQL_POLICY_VIOLATION,
                "validate_query",
                "compile_query",
                AgentMode.PLAN,
            ),
            (
                ErrorCode.COST_EXCEEDED,
                "explain_cost",
                "build_logical_plan",
                AgentMode.PREVIEW,
            ),
            (
                ErrorCode.EMPTY_RESULT,
                "validate_preview",
                "build_logical_plan",
                AgentMode.PREVIEW,
            ),
            (
                ErrorCode.JOIN_EXPLOSION,
                "validate_preview",
                "build_logical_plan",
                AgentMode.PREVIEW,
            ),
            (
                ErrorCode.RESULT_SEMANTIC_MISMATCH,
                "validate_result",
                "build_logical_plan",
                AgentMode.PREVIEW,
            ),
        )
        for code, fault_node, route_target, mode in cases:
            with self.subTest(code=code):
                result, invoker = await self._run_fault(
                    node=fault_node,
                    code=code,
                    mode=mode,
                )
                self.assertEqual(result.state.status, ExecutionStatus.SUCCEEDED)
                fault_index = result.state.node_trace.index(fault_node)
                self.assertEqual(result.state.node_trace[fault_index + 1], route_target)
                self.assertEqual(
                    sum(item.attempts for item in result.state.route_attempts),
                    1,
                )
                self.assertLessEqual(result.state.correction_rounds, 2)
                self.assertLessEqual(result.state.sql_compile_attempts, 3)
                self.assertLessEqual(result.state.tool_calls, 24)
                self.assertEqual(
                    self._context(mode).principal,
                    self._context(mode).principal,
                )
                self.assertTrue(
                    all(call.tool_name in self._context(mode).allowed_tools for call in invoker.calls)
                )

    async def test_access_denied_is_terminal_and_never_retried(self) -> None:
        result, invoker = await self._run_fault(
            node="semantic_search",
            code=ErrorCode.ACCESS_DENIED,
            mode=AgentMode.EXECUTE,
            retryable=True,
        )

        self.assertEqual(result.state.status, ExecutionStatus.FAILED)
        self.assertEqual(result.state.error.code, ErrorCode.ACCESS_DENIED.value)
        self.assertEqual(result.state.node_trace.count("semantic_search"), 1)
        self.assertEqual(invoker.calls, [])
        self.assertEqual(result.state.correction_rounds, 0)

    async def test_nonretryable_policy_violation_is_terminal(self) -> None:
        result, invoker = await self._run_fault(
            node="validate_query",
            code=ErrorCode.SQL_POLICY_VIOLATION,
            mode=AgentMode.PLAN,
            retryable=False,
        )

        self.assertEqual(result.state.status, ExecutionStatus.FAILED)
        self.assertEqual(
            result.state.error.code,
            ErrorCode.SQL_POLICY_VIOLATION.value,
        )
        self.assertEqual(
            [call.tool_name for call in invoker.calls],
            ["semantic.search", "query.compile"],
        )
        self.assertEqual(result.state.node_trace.count("compile_query"), 1)

    async def test_real_binding_errors_keep_typed_classification_and_retry_policy(self) -> None:
        cases = (
            (
                BindingErrorCode.BUNDLE_MISMATCH,
                "BINDING_STALE",
                "inspect_binding",
                AgentMode.PREVIEW,
                ExecutionStatus.SUCCEEDED,
            ),
            (
                BindingErrorCode.SQL_COMPILE_ERROR,
                "SQL_COMPILE_ERROR",
                "compile_query",
                AgentMode.PLAN,
                ExecutionStatus.SUCCEEDED,
            ),
            (
                BindingErrorCode.POLICY_INVALID,
                "POLICY_VIOLATION",
                None,
                AgentMode.PLAN,
                ExecutionStatus.FAILED,
            ),
        )
        for binding_code, tool_code, route_target, mode, status in cases:
            with self.subTest(binding_code=binding_code):
                provider = _BindingFailureProvider(self.compiled, binding_code)
                invoker = _HybridInvoker(
                    self.compiled,
                    "query.compile",
                    _real_invoker(provider),
                )
                result = await InternalGraphExecutor(
                    COMMERCE_EXECUTION_GRAPH,
                    ExecutionDependencies(
                        invoker=invoker,
                        context_resolver=fixture._Resolver(),
                        planner=fixture._Planner(self.plan),
                        domain_pack=self.domain,
                    ),
                ).execute(self._context(mode))

                first = invoker.real_results[0]
                self.assertEqual(first.status, "error")
                self.assertEqual(first.structured_error.code.value, tool_code)
                self.assertEqual(
                    first.structured_error.retryable,
                    status == ExecutionStatus.SUCCEEDED,
                )
                self.assertEqual(result.state.status, status)
                if route_target is None:
                    self.assertEqual(
                        result.state.error.code,
                        ErrorCode.SQL_POLICY_VIOLATION.value,
                    )
                    self.assertEqual(result.state.node_trace.count("compile_query"), 1)
                else:
                    first_compile = result.state.node_trace.index("compile_query")
                    self.assertEqual(
                        result.state.node_trace[first_compile + 1],
                        route_target,
                    )

    async def test_real_row_limit_error_routes_as_bounded_join_explosion(self) -> None:
        provider = _RowLimitProvider()
        invoker = _HybridInvoker(
            self.compiled,
            "query.execute",
            _real_invoker(provider, credential_broker=_LeaseBroker()),
        )
        result = await InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            ExecutionDependencies(
                invoker=invoker,
                context_resolver=fixture._Resolver(),
                planner=fixture._Planner(self.plan),
                domain_pack=self.domain,
            ),
        ).execute(self._context(AgentMode.PREVIEW))

        failed = next(item for item in invoker.real_results if item.status == "error")
        self.assertEqual(
            failed.structured_error.code.value,
            "ROW_LIMIT_EXCEEDED",
        )
        self.assertTrue(failed.structured_error.retryable)
        self.assertEqual(result.state.status, ExecutionStatus.SUCCEEDED)
        first_preview = result.state.node_trace.index("execute_preview")
        self.assertEqual(
            result.state.node_trace[first_preview + 1],
            "build_logical_plan",
        )
        self.assertEqual(
            [item.error_code for item in result.state.route_attempts],
            [ErrorCode.JOIN_EXPLOSION.value],
        )

    async def test_builtin_query_provider_reports_tampering_as_nonretryable_policy(self) -> None:
        provider_fixture.BuiltinProviderTests.setUpClass()
        harness = provider_fixture.BuiltinProviderTests(methodName="runTest")
        harness.setUp()
        compiled = await harness._invoke(
            "query.compile",
            provider_fixture.QueryCompileInput(
                logical_plan=provider_fixture.BuiltinProviderTests.logical_plan,
            ),
        )
        prepared = compiled.typed_data.prepared_query.model_copy(
            update={"policy_decision_id": "forged-policy-decision"}
        )

        rejected = await harness._invoke(
            "query.execute",
            provider_fixture.QueryExecuteInput(
                prepared_query=prepared,
                mode=provider_fixture.QueryMode.EXECUTE,
            ),
        )

        self.assertEqual(rejected.status, "error")
        self.assertEqual(
            rejected.structured_error.code.value,
            "POLICY_VIOLATION",
        )
        self.assertFalse(rejected.structured_error.retryable)


if __name__ == "__main__":
    unittest.main()
