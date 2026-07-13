from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.execution import (
    ArtifactDigestPin,
    BudgetLimits,
    COMMERCE_EXECUTION_GRAPH,
    CheckpointMismatchError,
    ErrorCode,
    ExecutionCheckpoint,
    ExecutionDependencies,
    ExecutionStatus,
    ExecutionVersionPins,
    InternalGraphExecutor,
    VersionPin,
)
from data_agent.runtime.models import AgentMode
from data_agent.runtime import load_enterprise_binding
from data_agent.runtime.binding import BindingCompiler
from data_agent.runtime.models import PrincipalContext
from data_agent.tools import ToolError, ToolErrorCode, ToolResult, ToolTrace
from tests.unit.execution import test_internal_executor as fixture


class _AlwaysCompileErrorInvoker(fixture._ScriptedInvoker):
    async def invoke(self, call, context):
        if call.tool_name != "query.compile":
            return await super().invoke(call, context)
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
                message="compile failed",
                retryable=True,
            ),
            redacted_trace=trace,
        )


class _TooManyRowsInvoker(fixture._ScriptedInvoker):
    @staticmethod
    def _query_data(prepared, *, row_count: int):
        return fixture._ScriptedInvoker._query_data(prepared, row_count=1001)


class _SlowResolver:
    async def resolve(self, context):
        await asyncio.sleep(2)


class _BlockingResolver:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def resolve(self, context):
        self.started.set()
        await asyncio.Event().wait()


class CheckpointAndBudgetTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture.InternalExecutorTests.setUpClass()
        cls.domain = fixture.InternalExecutorTests.domain
        cls.plan = fixture.InternalExecutorTests.plan
        cls.compiled = fixture.InternalExecutorTests.compiled
        cls.bundle = fixture.InternalExecutorTests.bundle
        cls.principal = fixture.InternalExecutorTests.principal
        cls.context_factory = fixture.InternalExecutorTests._context

    def _context(self, mode: AgentMode = AgentMode.EXECUTE):
        return self.context_factory(mode)

    def _executor(self, invoker=None, resolver=None):
        return InternalGraphExecutor(
            COMMERCE_EXECUTION_GRAPH,
            ExecutionDependencies(
                invoker=invoker or fixture._ScriptedInvoker(self.compiled),
                context_resolver=resolver or fixture._Resolver(),
                planner=fixture._Planner(self.plan),
                domain_pack=self.domain,
            ),
        )

    async def test_tool_call_budget_fails_before_call_twenty_five(self) -> None:
        invoker = fixture._ScriptedInvoker(self.compiled)
        context = self._context(AgentMode.PLAN).model_copy(
            update={"budget": BudgetLimits(max_tool_calls=1)}
        )

        result = await self._executor(invoker).execute(context)

        self.assertEqual(result.state.status, ExecutionStatus.FAILED)
        self.assertEqual(result.state.error.code, "BUDGET_EXCEEDED")
        self.assertEqual(result.state.tool_calls, 1)
        self.assertEqual(len(invoker.calls), 1)

    async def test_compile_attempt_budget_stops_retry_loop(self) -> None:
        invoker = _AlwaysCompileErrorInvoker(self.compiled)
        context = self._context(AgentMode.PLAN).model_copy(
            update={"budget": BudgetLimits(max_sql_compile_attempts=2)}
        )

        result = await self._executor(invoker).execute(context)

        self.assertEqual(result.state.status, ExecutionStatus.FAILED)
        self.assertEqual(result.state.error.code, ErrorCode.SQL_COMPILE_ERROR.value)
        self.assertEqual(result.state.sql_compile_attempts, 2)
        self.assertEqual(
            [call.tool_name for call in invoker.calls].count("query.compile"),
            2,
        )

    async def test_row_budget_is_enforced_even_for_scripted_provider(self) -> None:
        result = await self._executor(_TooManyRowsInvoker(self.compiled)).execute(
            self._context(AgentMode.PREVIEW)
        )

        self.assertEqual(result.state.status, ExecutionStatus.FAILED)
        self.assertEqual(result.state.error.code, "ROW_LIMIT_EXCEEDED")
        self.assertLessEqual(result.state.result_rows, 1000)

    async def test_cost_corrections_preserve_every_tool_call_in_the_budget(self) -> None:
        invoker = fixture._ScriptedInvoker(self.compiled)
        context = self._context(AgentMode.PREVIEW).model_copy(
            update={"max_estimated_cost": 1.0}
        )

        result = await self._executor(invoker).execute(context)

        self.assertEqual(result.state.status, ExecutionStatus.FAILED)
        self.assertEqual(result.state.error.code, ErrorCode.COST_EXCEEDED.value)
        self.assertEqual(result.state.correction_rounds, 2)
        self.assertEqual(result.state.tool_calls, len(invoker.calls))
        self.assertEqual(result.state.tool_calls, 7)
        self.assertEqual(result.state.node_trace.count("explain_cost"), 3)

    async def test_timeout_and_cancellation_return_stable_final_states(self) -> None:
        timeout_context = self._context(AgentMode.PLAN).model_copy(
            update={"budget": BudgetLimits(max_duration_seconds=1)}
        )
        timed_out = await self._executor(resolver=_SlowResolver()).execute(
            timeout_context
        )
        self.assertEqual(timed_out.state.status, ExecutionStatus.TIMED_OUT)
        self.assertEqual(timed_out.state.error.code, "TIMEOUT")
        self.assertEqual(timed_out.final_artifact.payload.status, ExecutionStatus.TIMED_OUT)

        resolver = _BlockingResolver()
        task = asyncio.create_task(
            self._executor(resolver=resolver).execute(self._context(AgentMode.PLAN))
        )
        await resolver.started.wait()
        task.cancel()
        cancelled = await task
        self.assertEqual(cancelled.state.status, ExecutionStatus.CANCELLED)
        self.assertEqual(cancelled.state.error.code, "CANCELLED")
        self.assertEqual(cancelled.final_artifact.payload.status, ExecutionStatus.CANCELLED)
        self.assertEqual(cancelled.state.node_trace, ("resolve_context",))

    async def test_checkpoint_pins_versions_and_resume_matches_uninterrupted_run(self) -> None:
        context = self._context(AgentMode.EXECUTE)
        executor = self._executor()

        checkpoint = await executor.create_checkpoint(
            context,
            after_node="validate_query",
        )

        self.assertIsInstance(checkpoint, ExecutionCheckpoint)
        self.assertEqual(checkpoint.state.status, ExecutionStatus.PAUSED)
        self.assertEqual(checkpoint.state.next_node, "explain_cost")
        self.assertEqual(checkpoint.pins.bundle_digest, context.bundle.digest)
        self.assertEqual(checkpoint.pins.skill_version, context.skill_version)
        self.assertEqual(checkpoint.pins.graph_digest, COMMERCE_EXECUTION_GRAPH.digest)
        self.assertEqual(checkpoint.pins.tool_versions, context.tool_versions)
        self.assertEqual(checkpoint.pins.model_versions, context.model_versions)
        self.assertEqual(checkpoint.pins.authority.tenant_id, context.principal.tenant_id)
        self.assertEqual(checkpoint.pins.authority.user_id, context.principal.user_id)
        self.assertEqual(checkpoint.pins.authority.enterprise_id, "olist")
        self.assertEqual(checkpoint.pins.authority.domain_id, "commerce")
        self.assertEqual(
            checkpoint.artifact_digests,
            tuple(
                ArtifactDigestPin(
                    artifact_id=item.artifact_id,
                    digest=item.digest,
                )
                for item in checkpoint.state.artifacts
            ),
        )
        restored = ExecutionCheckpoint.model_validate_json(
            checkpoint.model_dump_json()
        )
        self.assertEqual(restored, checkpoint)

        legacy_payload = checkpoint.model_dump(mode="json")
        del legacy_payload["pins"]["authority"]
        with self.assertRaises(ValidationError):
            ExecutionCheckpoint.model_validate(legacy_payload)

        resumed = await executor.resume(restored, context)
        uninterrupted = await self._executor().execute(context)
        self.assertEqual(resumed.state.node_trace, uninterrupted.state.node_trace)
        self.assertEqual(resumed.state.tool_trace, uninterrupted.state.tool_trace)
        self.assertEqual(
            resumed.final_artifact.digest,
            uninterrupted.final_artifact.digest,
        )

    async def test_replay_rejects_any_version_or_digest_drift(self) -> None:
        context = self._context(AgentMode.EXECUTE)
        executor = self._executor()
        checkpoint = await executor.create_checkpoint(
            context,
            after_node="validate_query",
        )
        drifted_pins = checkpoint.pins.model_copy(
            update={
                "model_versions": (
                    VersionPin(component="planner", version="scripted-v2"),
                )
            }
        )
        other_checkpoint = ExecutionCheckpoint.capture(
            pins=drifted_pins,
            state=checkpoint.state,
        )

        with self.assertRaises(CheckpointMismatchError):
            await executor.replay(other_checkpoint, context)

        with self.assertRaises(ValidationError):
            checkpoint.model_copy(
                update={
                    "artifact_digests": (
                        ArtifactDigestPin(
                            artifact_id=checkpoint.artifact_digests[0].artifact_id,
                            digest="0" * 64,
                        ),
                    )
                }
            )

    async def test_resume_rejects_authority_and_request_drift_before_restoring_artifacts(self) -> None:
        context = self._context(AgentMode.EXECUTE).model_copy(
            update={"enterprise_id": "olist", "domain_id": "commerce"}
        )
        executor = self._executor()
        post_result = await executor.create_checkpoint(
            context,
            after_node="validate_result",
        )

        drifted_contexts = {
            "tenant": context.model_copy(
                update={
                    "principal": context.principal.model_copy(
                        update={"tenant_id": "other-seller"}
                    )
                }
            ),
            "user": context.model_copy(
                update={
                    "principal": context.principal.model_copy(
                        update={"user_id": "other-user"}
                    )
                }
            ),
            "roles": context.model_copy(
                update={
                    "principal": context.principal.model_copy(
                        update={"roles": ("admin",)}
                    )
                }
            ),
            "question": context.model_copy(update={"question": "Different request"}),
            "request": context.model_copy(update={"run_id": "other-request"}),
            "enterprise": context.model_copy(update={"enterprise_id": "other"}),
            "domain": context.model_copy(update={"domain_id": "other"}),
        }
        for drift, drifted in drifted_contexts.items():
            with self.subTest(drift=drift):
                with self.assertRaises(CheckpointMismatchError):
                    await executor.resume(post_result, drifted)

        pre_query = await executor.create_checkpoint(
            context,
            after_node="validate_logical_plan",
        )
        with self.assertRaises(CheckpointMismatchError):
            await executor.resume(
                pre_query,
                context.model_copy(update={"question": "Pre-query drift"}),
            )

    async def test_equivalent_normalized_roles_resume_the_same_checkpoint(self) -> None:
        context = self._context(AgentMode.EXECUTE).model_copy(
            update={
                "principal": self.principal.model_copy(
                    update={"roles": ("Seller", "analyst", "seller")}
                )
            }
        )
        executor = self._executor()
        checkpoint = await executor.create_checkpoint(
            context,
            after_node="validate_result",
        )
        self.assertEqual(
            checkpoint.pins.authority.normalized_roles,
            ("Seller", "analyst", "seller"),
        )

        resumed = await executor.resume(
            checkpoint,
            context.model_copy(
                update={
                    "principal": context.principal.model_copy(
                        update={"roles": ("seller", "analyst", "Seller", "seller")}
                    )
                }
            ),
        )
        self.assertEqual(resumed.state.status, ExecutionStatus.SUCCEEDED)

    async def test_admin_role_case_drift_is_rejected_at_post_answer_and_pre_query(self) -> None:
        compiler = BindingCompiler(
            self.domain,
            load_enterprise_binding(PROJECT_ROOT / "packs" / "enterprises" / "olist"),
            self.bundle,
        )
        admin = PrincipalContext(
            tenant_id="admin-eval",
            user_id="admin-user",
            roles=("admin",),
        )
        upper_admin = admin.model_copy(update={"roles": ("ADMIN",)})
        self.assertTrue(compiler.bind(self.plan, admin).required_access.admin_bypass)
        self.assertFalse(
            compiler.bind(self.plan, upper_admin).required_access.admin_bypass
        )

        context = self._context(AgentMode.EXECUTE).model_copy(
            update={"principal": admin}
        )
        drifted = context.model_copy(update={"principal": upper_admin})
        executor = self._executor()
        for boundary in ("render_answer", "validate_logical_plan"):
            with self.subTest(boundary=boundary):
                checkpoint = await executor.create_checkpoint(
                    context,
                    after_node=boundary,
                )
                with self.assertRaises(CheckpointMismatchError):
                    await executor.resume(checkpoint, drifted)


if __name__ == "__main__":
    unittest.main()
