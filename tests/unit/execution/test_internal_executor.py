from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
DOMAIN_ROOT = PROJECT_ROOT / "packs" / "domains" / "commerce"
ENTERPRISE_ROOT = PROJECT_ROOT / "packs" / "enterprises" / "olist"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.execution import (
    ArtifactKind,
    BudgetLimits,
    COMMERCE_EXECUTION_GRAPH,
    ExecutionContext,
    ExecutionDependencies,
    ExecutionStatus,
    EdgeCondition,
    EdgeSpec,
    GraphSpec,
    InternalGraphExecutor,
    ResolvedContext,
    VersionPin,
)
from data_agent.runtime.composition import stable_digest
from data_agent.runtime import (
    load_bundle_manifest,
    load_domain_pack,
    load_enterprise_binding,
)
from data_agent.runtime.binding import BindingCompiler
from data_agent.runtime.models import AgentMode, PrincipalContext
from data_agent.skills import logical_plan_from_eval_case
from data_agent.tools import ToolLineage, ToolResult, ToolTrace
from data_agent.tools.providers import (
    AnswerRenderOutput,
    ColumnProfile,
    QueryCompileOutput,
    QueryData,
    QueryExecutionOutput,
    QueryMode,
    ResultProfileOutput,
    SemanticMatch,
    SemanticSearchOutput,
)
from data_agent.tools.schemas import ExplainResult, QueryRow


class _Resolver:
    async def resolve(self, context: ExecutionContext) -> ResolvedContext:
        return ResolvedContext(
            contextualized_question=context.question,
            approved_memories=(),
        )


class _Planner:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.calls = 0

    async def build_plan(self, *, context, resolved_context, semantic_matches):
        self.calls += 1
        return self.plan


class _ScriptedInvoker:
    def __init__(self, compiled: QueryCompileOutput) -> None:
        self.compiled = compiled
        self.calls = []

    @staticmethod
    def _query_data(prepared, *, row_count: int) -> QueryData:
        rows = tuple(
            QueryRow(values=(f"seller-{index}", float(10 - index)))
            for index in range(row_count)
        )
        return QueryData(
            logical_plan_hash=prepared.logical_plan_hash,
            query_hash=prepared.sql_ast_hash,
            policy_decision_id=prepared.policy_decision_id,
            verification_token="verified-scripted-evidence",
            columns=("seller_id", "gmv"),
            rows=rows,
        )

    async def invoke(self, call, context):
        self.calls.append(call)
        prepared = self.compiled.prepared_query
        if call.tool_name == "semantic.search":
            output = SemanticSearchOutput(
                matches=(
                    SemanticMatch(
                        ref="commerce.gmv",
                        kind="metric",
                        label="gmv",
                        description="governed metric",
                        score=1.0,
                    ),
                )
            )
        elif call.tool_name == "query.compile":
            output = self.compiled
        elif call.tool_name == "query.execute":
            mode = call.input_data.mode
            if mode == QueryMode.EXPLAIN:
                output = QueryExecutionOutput(
                    mode=mode,
                    explain=ExplainResult(
                        plan_text='[{"Plan":{"Total Cost":2.5,"Plan Rows":3}}]',
                        estimated_cost=2.5,
                        estimated_rows=3,
                    ),
                )
            else:
                output = QueryExecutionOutput(
                    mode=mode,
                    data=self._query_data(
                        prepared,
                        row_count=2 if mode == QueryMode.PREVIEW else 3,
                    ),
                )
        elif call.tool_name == "result.profile":
            data = call.input_data.data
            output = ResultProfileOutput(
                logical_plan_hash=data.logical_plan_hash,
                query_hash=data.query_hash,
                policy_decision_id=data.policy_decision_id,
                row_count=len(data.rows),
                columns=tuple(
                    ColumnProfile(
                        name=name,
                        null_count=0,
                        distinct_count=len(data.rows),
                    )
                    for name in data.columns
                ),
            )
        elif call.tool_name == "answer.render":
            data = call.input_data.data
            output = AnswerRenderOutput(
                answer=f"verified {len(data.rows)} rows",
                evidence_query_hash=data.query_hash,
                policy_decision_id=data.policy_decision_id,
            )
        else:
            raise AssertionError(f"unexpected tool: {call.tool_name}")
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
            rows=int(getattr(output, "row_count", 0)),
            lineage=ToolLineage(
                logical_plan_hash=prepared.logical_plan_hash,
                query_hash=prepared.sql_ast_hash,
            ),
            policy_decision_id=prepared.policy_decision_id,
            redacted_trace=trace,
        )


class InternalExecutorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain = load_domain_pack(DOMAIN_ROOT)
        enterprise = load_enterprise_binding(ENTERPRISE_ROOT)
        cls.bundle = load_bundle_manifest(
            PROJECT_ROOT / "generated" / "bundles" / "olist-local.json",
            pack_lock=ENTERPRISE_ROOT / "pack.lock",
            schema_catalog=PROJECT_ROOT / "schema_catalog.json",
        )
        case = next(
            item for item in cls.domain.spec.evals if item.id == "commerce.metric_002"
        )
        cls.plan = logical_plan_from_eval_case(case, cls.domain)
        cls.principal = PrincipalContext(
            tenant_id="seller-42",
            user_id="user-7",
            roles=("seller",),
        )
        compiler = BindingCompiler(cls.domain, enterprise, cls.bundle)
        bound = compiler.bind(cls.plan, cls.principal)
        prepared = compiler.compile(bound, cls.principal)
        cls.compiled = QueryCompileOutput(
            bound_plan=bound,
            prepared_query=prepared,
        )

    def _context(self, mode: AgentMode) -> ExecutionContext:
        return ExecutionContext(
            run_id=f"run-{mode.value}",
            mode=mode,
            question="Show GMV by seller",
            principal=self.principal,
            bundle=self.bundle,
            skill_id="commerce.analytics",
            skill_version="1.0.0",
            allowed_tools=(
                "semantic.search",
                "data.inspect",
                "query.compile",
                "query.execute",
                "result.profile",
                "answer.render",
            ),
            tool_versions=(
                VersionPin(component="semantic.search", version="1.0.0"),
                VersionPin(component="data.inspect", version="1.0.0"),
                VersionPin(component="query.compile", version="1.0.0"),
                VersionPin(component="query.execute", version="1.0.0"),
                VersionPin(component="result.profile", version="1.0.0"),
                VersionPin(component="answer.render", version="1.0.0"),
            ),
            model_versions=(VersionPin(component="planner", version="scripted-v1"),),
            budget=BudgetLimits(),
            preview_rows=2,
        )

    async def _run(self, mode: AgentMode):
        invoker = _ScriptedInvoker(self.compiled)
        dependencies = ExecutionDependencies(
            invoker=invoker,
            context_resolver=_Resolver(),
            planner=_Planner(self.plan),
            domain_pack=self.domain,
        )
        result = await InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            dependencies,
        ).execute(self._context(mode))
        return result, invoker

    async def test_plan_stops_after_static_validation_without_read_tool(self) -> None:
        result, invoker = await self._run(AgentMode.PLAN)

        self.assertEqual(result.state.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(
            result.state.node_trace,
            (
                "resolve_context",
                "semantic_search",
                "build_logical_plan",
                "validate_logical_plan",
                "compile_query",
                "validate_query",
                "finalize",
            ),
        )
        self.assertEqual(
            [call.tool_name for call in invoker.calls],
            ["semantic.search", "query.compile"],
        )
        self.assertFalse(
            any(
                COMMERCE_EXECUTION_GRAPH.node(node_id).requires_credentials
                for node_id in result.state.node_trace
            )
        )

    async def test_preview_explains_and_previews_but_never_executes(self) -> None:
        result, invoker = await self._run(AgentMode.PREVIEW)

        self.assertEqual(result.state.status, ExecutionStatus.SUCCEEDED)
        query_calls = [
            call for call in invoker.calls if call.tool_name == "query.execute"
        ]
        self.assertEqual(
            [call.input_data.mode for call in query_calls],
            [QueryMode.EXPLAIN, QueryMode.PREVIEW],
        )
        self.assertNotIn("execute_query", result.state.node_trace)
        self.assertEqual(result.final_artifact.payload.row_count, 2)

    async def test_execute_reuses_exact_prepared_query_after_preview(self) -> None:
        result, invoker = await self._run(AgentMode.EXECUTE)

        self.assertEqual(result.state.status, ExecutionStatus.SUCCEEDED)
        query_calls = [
            call for call in invoker.calls if call.tool_name == "query.execute"
        ]
        self.assertEqual(
            [call.input_data.mode for call in query_calls],
            [QueryMode.EXPLAIN, QueryMode.PREVIEW, QueryMode.EXECUTE],
        )
        prepared_queries = [call.input_data.prepared_query for call in query_calls]
        self.assertTrue(
            all(item == self.compiled.prepared_query for item in prepared_queries)
        )
        self.assertEqual(
            {item.sql_ast_hash for item in prepared_queries},
            {self.compiled.prepared_query.sql_ast_hash},
        )
        self.assertEqual(
            {item.policy_decision_id for item in prepared_queries},
            {self.compiled.prepared_query.policy_decision_id},
        )
        prepared_artifact = result.state.artifact(ArtifactKind.PREPARED_QUERY)
        self.assertEqual(prepared_artifact.payload, self.compiled.prepared_query)
        self.assertEqual(result.final_artifact.payload.row_count, 3)
        self.assertEqual(result.state.tool_calls, len(invoker.calls))
        self.assertEqual(len(result.final_artifact.digest), 64)

    async def test_runtime_turns_invalid_uncompiled_routes_into_stable_failures(self) -> None:
        extra = EdgeSpec(
            source="validate_query",
            target="explain_cost",
            condition=EdgeCondition.MODE_PLAN,
        )
        without_validate_edges = tuple(
            edge
            for edge in COMMERCE_EXECUTION_GRAPH.edges
            if edge.source != "validate_query"
        )
        cases = {
            "ambiguous": (*COMMERCE_EXECUTION_GRAPH.edges, extra),
            "missing": without_validate_edges,
        }
        for name, edges in cases.items():
            with self.subTest(name=name):
                payload = {
                    "graph_id": COMMERCE_EXECUTION_GRAPH.graph_id,
                    "version": COMMERCE_EXECUTION_GRAPH.version,
                    "entry_node": COMMERCE_EXECUTION_GRAPH.entry_node,
                    "terminal_nodes": COMMERCE_EXECUTION_GRAPH.terminal_nodes,
                    "nodes": COMMERCE_EXECUTION_GRAPH.nodes,
                    "edges": edges,
                    "limits": COMMERCE_EXECUTION_GRAPH.limits,
                }
                unsafe_graph = GraphSpec(**payload, digest=stable_digest(payload))
                executor = InternalGraphExecutor(
                    unsafe_graph,
                    ExecutionDependencies(
                        invoker=_ScriptedInvoker(self.compiled),
                        context_resolver=_Resolver(),
                        planner=_Planner(self.plan),
                        domain_pack=self.domain,
                    ),
                )

                try:
                    result = await executor.execute(self._context(AgentMode.PLAN))
                except RuntimeError as exc:
                    self.fail(f"routing leaked an untyped runtime exception: {exc}")

                self.assertEqual(result.state.status, ExecutionStatus.FAILED)
                self.assertEqual(result.state.error.code, "GRAPH_ROUTING_ERROR")
                self.assertEqual(result.state.current_node, "validate_query")


if __name__ == "__main__":
    unittest.main()
