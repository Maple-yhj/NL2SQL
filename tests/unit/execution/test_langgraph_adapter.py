from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.execution import (
    COMMERCE_EXECUTION_GRAPH,
    BudgetLimits,
    CheckpointMismatchError,
    EdgeCondition,
    EdgeSpec,
    ExecutionDependencies,
    InternalGraphExecutor,
    LangGraphAdapter,
    GraphSpec,
    LANGGRAPH_RECURSION_MARGIN,
    graph_static_max_steps,
)
from data_agent.runtime.composition import stable_digest
from data_agent.runtime.models import AgentMode
from data_agent.runtime.models import PrincipalContext
from data_agent.tools import ToolError, ToolErrorCode, ToolResult, ToolTrace
from data_agent.tools.providers import QueryExecutionOutput, QueryMode
from data_agent.tools.schemas import ExplainResult
from langgraph.errors import GraphRecursionError
from tests.unit.execution import test_internal_executor as fixture
from tests.unit.execution import test_checkpoint_budget as budget_fixture


class _FailCompileOnceInvoker(fixture._ScriptedInvoker):
    def __init__(self, compiled) -> None:
        super().__init__(compiled)
        self.failed = False

    async def invoke(self, call, context):
        if call.tool_name != "query.compile" or self.failed:
            return await super().invoke(call, context)
        self.failed = True
        self.calls.append(call)
        now = datetime.now(UTC)
        trace = ToolTrace(
            call_id=call.call_id,
            tool_name=call.tool_name,
            tool_version=call.tool_version,
            status="error",
            attempts=1,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            input_schema=type(call.input_data).__name__,
            output_schema="QueryCompileOutput",
            error_code=ToolErrorCode.PROVIDER_ERROR,
        )
        return ToolResult(
            status="error",
            structured_error=ToolError(
                code=ToolErrorCode.PROVIDER_ERROR,
                message="scripted compile failure",
                retryable=True,
            ),
            redacted_trace=trace,
        )


class _MaximumCostRecoveryInvoker(fixture._ScriptedInvoker):
    def __init__(self, compiled) -> None:
        super().__init__(compiled)
        self.explain_calls = 0

    async def invoke(self, call, context):
        result = await super().invoke(call, context)
        if (
            call.tool_name == "query.execute"
            and call.input_data.mode == QueryMode.EXPLAIN
        ):
            self.explain_calls += 1
            if self.explain_calls == 3:
                output = QueryExecutionOutput(
                    mode=QueryMode.EXPLAIN,
                    explain=ExplainResult(
                        plan_text='[{"Plan":{"Total Cost":0.5,"Plan Rows":3}}]',
                        estimated_cost=0.5,
                        estimated_rows=3,
                    ),
                )
                result = result.model_copy(update={"typed_data": output})
        return result


class _RecursingBackend:
    async def ainvoke(self, payload, config=None):
        raise GraphRecursionError("synthetic backend recursion")


class LangGraphAdapterTests(unittest.IsolatedAsyncioTestCase):
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

    def _dependencies(self, invoker):
        return ExecutionDependencies(
            invoker=invoker,
            context_resolver=fixture._Resolver(),
            planner=fixture._Planner(self.plan),
            domain_pack=self.domain,
        )

    async def _run_pair(self, mode: AgentMode, invoker_type=fixture._ScriptedInvoker):
        internal = InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(invoker_type(self.compiled)),
        )
        adapter = LangGraphAdapter(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(invoker_type(self.compiled)),
        )
        context = self._context(mode)
        return await internal.execute(context), await adapter.execute(context), adapter

    async def test_real_compiled_state_graph_contains_every_spec_node(self) -> None:
        _, _, adapter = await self._run_pair(AgentMode.PLAN)

        self.assertTrue(adapter.available)
        self.assertTrue(
            type(adapter.compiled_graph).__module__.startswith("langgraph.")
        )
        backend_nodes = set(adapter.compiled_graph.get_graph().nodes)
        self.assertTrue(
            {node.id for node in COMMERCE_EXECUTION_GRAPH.nodes}.issubset(
                backend_nodes
            )
        )

    async def test_plan_preview_execute_have_backend_parity(self) -> None:
        for mode in (AgentMode.PLAN, AgentMode.PREVIEW, AgentMode.EXECUTE):
            with self.subTest(mode=mode):
                internal, langgraph, _ = await self._run_pair(mode)
                self.assertEqual(langgraph.state.node_trace, internal.state.node_trace)
                self.assertEqual(langgraph.state.tool_trace, internal.state.tool_trace)
                self.assertEqual(
                    langgraph.final_artifact.digest,
                    internal.final_artifact.digest,
                )
                self.assertEqual(
                    type(langgraph).__module__,
                    "data_agent.execution.contracts",
                )

    async def test_recoverable_error_route_has_backend_parity(self) -> None:
        internal, langgraph, _ = await self._run_pair(
            AgentMode.EXECUTE,
            _FailCompileOnceInvoker,
        )

        self.assertEqual(langgraph.state.node_trace, internal.state.node_trace)
        self.assertEqual(langgraph.state.tool_trace, internal.state.tool_trace)
        self.assertEqual(langgraph.state.route_attempts, internal.state.route_attempts)
        self.assertEqual(
            langgraph.final_artifact.digest,
            internal.final_artifact.digest,
        )

    async def test_checkpoint_and_resume_have_backend_parity(self) -> None:
        context = self._context(AgentMode.EXECUTE)
        internal = InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(fixture._ScriptedInvoker(self.compiled)),
        )
        adapter = LangGraphAdapter(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(fixture._ScriptedInvoker(self.compiled)),
        )

        internal_checkpoint = await internal.create_checkpoint(
            context,
            after_node="validate_query",
        )
        langgraph_checkpoint = await adapter.create_checkpoint(
            context,
            after_node="validate_query",
        )
        self.assertEqual(langgraph_checkpoint, internal_checkpoint)

        internal_result = await internal.resume(internal_checkpoint, context)
        langgraph_result = await adapter.resume(langgraph_checkpoint, context)
        self.assertEqual(langgraph_result.state.node_trace, internal_result.state.node_trace)
        self.assertEqual(langgraph_result.state.tool_trace, internal_result.state.tool_trace)
        self.assertEqual(
            langgraph_result.final_artifact.digest,
            internal_result.final_artifact.digest,
        )

    async def test_resume_rejects_authority_drift_before_langgraph_restore(self) -> None:
        context = self._context(AgentMode.EXECUTE)
        adapter = LangGraphAdapter(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(fixture._ScriptedInvoker(self.compiled)),
        )
        checkpoint = await adapter.create_checkpoint(
            context,
            after_node="validate_result",
        )

        for drifted in (
            context.model_copy(
                update={
                    "principal": context.principal.model_copy(
                        update={"tenant_id": "other-seller"}
                    )
                }
            ),
            context.model_copy(update={"question": "other question"}),
        ):
            with self.subTest(drift=drifted):
                with self.assertRaises(CheckpointMismatchError):
                    await adapter.resume(checkpoint, drifted)

    async def test_admin_role_case_drift_is_rejected_before_langgraph_restore(self) -> None:
        context = self._context(AgentMode.EXECUTE).model_copy(
            update={
                "principal": PrincipalContext(
                    tenant_id="admin-eval",
                    user_id="admin-user",
                    roles=("admin",),
                )
            }
        )
        drifted = context.model_copy(
            update={
                "principal": context.principal.model_copy(
                    update={"roles": ("ADMIN",)}
                )
            }
        )
        adapter = LangGraphAdapter(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(fixture._ScriptedInvoker(self.compiled)),
        )
        for boundary in ("render_answer", "validate_logical_plan"):
            with self.subTest(boundary=boundary):
                checkpoint = await adapter.create_checkpoint(
                    context,
                    after_node=boundary,
                )
                with self.assertRaises(CheckpointMismatchError):
                    await adapter.resume(checkpoint, drifted)

    async def test_invalid_uncompiled_route_is_stable_and_matches_internal(self) -> None:
        extra = EdgeSpec(
            source="validate_query",
            target="explain_cost",
            condition=EdgeCondition.MODE_PLAN,
        )
        payload = {
            "graph_id": COMMERCE_EXECUTION_GRAPH.graph_id,
            "version": COMMERCE_EXECUTION_GRAPH.version,
            "entry_node": COMMERCE_EXECUTION_GRAPH.entry_node,
            "terminal_nodes": COMMERCE_EXECUTION_GRAPH.terminal_nodes,
            "nodes": COMMERCE_EXECUTION_GRAPH.nodes,
            "edges": (*COMMERCE_EXECUTION_GRAPH.edges, extra),
            "limits": COMMERCE_EXECUTION_GRAPH.limits,
        }
        unsafe = GraphSpec(**payload, digest=stable_digest(payload))
        context = self._context(AgentMode.PLAN)
        internal = InternalGraphExecutor(
            unsafe,
            self._dependencies(fixture._ScriptedInvoker(self.compiled)),
        )
        adapter = LangGraphAdapter(
            unsafe,
            self._dependencies(fixture._ScriptedInvoker(self.compiled)),
        )

        try:
            internal_result = await internal.execute(context)
            langgraph_result = await adapter.execute(context)
        except Exception as exc:
            self.fail(f"backend leaked an untyped routing exception: {exc}")

        self.assertEqual(internal_result.state.status.value, "failed")
        self.assertEqual(langgraph_result.state.status.value, "failed")
        self.assertEqual(internal_result.state.error.code, "GRAPH_ROUTING_ERROR")
        self.assertEqual(langgraph_result.state.error.code, "GRAPH_ROUTING_ERROR")
        self.assertEqual(
            internal_result.final_artifact.digest,
            langgraph_result.final_artifact.digest,
        )

    async def test_recursion_limit_is_derived_and_maximum_legal_recovery_has_parity(self) -> None:
        context = self._context(AgentMode.PREVIEW).model_copy(
            update={"max_estimated_cost": 1.0}
        )
        internal = InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(_MaximumCostRecoveryInvoker(self.compiled)),
        )
        adapter = LangGraphAdapter(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(_MaximumCostRecoveryInvoker(self.compiled)),
        )

        self.assertEqual(
            adapter.static_max_steps,
            graph_static_max_steps(COMMERCE_EXECUTION_GRAPH),
        )
        self.assertEqual(
            adapter.recursion_limit,
            adapter.static_max_steps + LANGGRAPH_RECURSION_MARGIN,
        )
        internal_result = await internal.execute(context)
        langgraph_result = await adapter.execute(context)

        self.assertEqual(internal_result.state.status.value, "succeeded")
        self.assertEqual(internal_result.state.correction_rounds, 2)
        self.assertEqual(internal_result.state.sql_compile_attempts, 3)
        self.assertEqual(langgraph_result.state.node_trace, internal_result.state.node_trace)
        self.assertEqual(langgraph_result.state.tool_trace, internal_result.state.tool_trace)
        self.assertEqual(
            langgraph_result.final_artifact.digest,
            internal_result.final_artifact.digest,
        )

    async def test_backend_recursion_error_becomes_typed_stable_final(self) -> None:
        adapter = LangGraphAdapter(
            COMMERCE_EXECUTION_GRAPH,
            self._dependencies(fixture._ScriptedInvoker(self.compiled)),
        )
        adapter.compiled_graph = _RecursingBackend()

        try:
            result = await adapter.execute(self._context(AgentMode.PLAN))
        except GraphRecursionError as exc:
            self.fail(f"backend recursion leaked to the public boundary: {exc}")

        self.assertEqual(result.state.status.value, "failed")
        self.assertEqual(result.state.error.code, "GRAPH_RECURSION_LIMIT")
        self.assertEqual(result.final_artifact.payload.error_code, "GRAPH_RECURSION_LIMIT")

    async def test_timeout_and_cancellation_have_stable_backend_parity(self) -> None:
        timeout_context = self._context(AgentMode.PLAN).model_copy(
            update={"budget": BudgetLimits(max_duration_seconds=1)}
        )

        def dependencies(resolver):
            return ExecutionDependencies(
                invoker=fixture._ScriptedInvoker(self.compiled),
                context_resolver=resolver,
                planner=fixture._Planner(self.plan),
                domain_pack=self.domain,
            )

        internal_timeout, langgraph_timeout = await asyncio.gather(
            InternalGraphExecutor(
                COMMERCE_EXECUTION_GRAPH,
                dependencies(budget_fixture._SlowResolver()),
            ).execute(timeout_context),
            LangGraphAdapter(
                COMMERCE_EXECUTION_GRAPH,
                dependencies(budget_fixture._SlowResolver()),
            ).execute(timeout_context),
        )
        self.assertEqual(langgraph_timeout.state.status, internal_timeout.state.status)
        self.assertEqual(langgraph_timeout.state.error.code, internal_timeout.state.error.code)
        self.assertEqual(
            langgraph_timeout.final_artifact.digest,
            internal_timeout.final_artifact.digest,
        )

        internal_resolver = budget_fixture._BlockingResolver()
        langgraph_resolver = budget_fixture._BlockingResolver()
        internal_task = asyncio.create_task(
            InternalGraphExecutor(
                COMMERCE_EXECUTION_GRAPH,
                dependencies(internal_resolver),
            ).execute(self._context(AgentMode.PLAN))
        )
        langgraph_task = asyncio.create_task(
            LangGraphAdapter(
                COMMERCE_EXECUTION_GRAPH,
                dependencies(langgraph_resolver),
            ).execute(self._context(AgentMode.PLAN))
        )
        await asyncio.gather(
            internal_resolver.started.wait(),
            langgraph_resolver.started.wait(),
        )
        internal_task.cancel()
        langgraph_task.cancel()
        internal_cancelled, langgraph_cancelled = await asyncio.gather(
            internal_task,
            langgraph_task,
        )
        self.assertEqual(
            langgraph_cancelled.state.status,
            internal_cancelled.state.status,
        )
        self.assertEqual(
            langgraph_cancelled.state.error.code,
            internal_cancelled.state.error.code,
        )
        self.assertEqual(
            langgraph_cancelled.final_artifact.digest,
            internal_cancelled.final_artifact.digest,
        )


if __name__ == "__main__":
    unittest.main()
