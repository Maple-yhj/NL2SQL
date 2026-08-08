from __future__ import annotations

import tempfile
import unittest
import asyncio
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile

from api.datasource_service import DataSourceService
from api.run_streams import RunCoordinator, RunEventStore
from data_agent.analysis_agent.artifacts import SQLiteArtifactStore
from data_agent.analysis_agent.checkpoints import (
    InMemoryCheckpointerFactory,
    SQLiteCheckpointerFactory,
    checkpoint_serializer,
)
from data_agent.analysis_agent.composition import (
    DatasetAnalysisRunResolver,
    build_analysis_runtime_from_resolver,
)
from data_agent.analysis_agent.graph import build_dataset_version_pins
from data_agent.analysis_agent.models import (
    AgentContextSnapshot,
    AgentInputReason,
    AgentInputRequest,
    DatasetAuthority,
    PlannerDecision,
)
from data_agent.analysis_agent.nodes import AnalysisGraphContext
from data_agent.analysis_agent.runtime import (
    AnalysisRuntimeError,
    ResolvedAnalysisRun,
)
from data_agent.public_contracts import AgentError, ErrorCode
from data_agent.datasources import SemanticFieldMapping
from data_agent.runtime.events import AgentEventType
from data_agent.runtime.models import (
    AgentMode,
    AgentRequest,
    ComponentVersionPin,
    PrincipalContext,
)
from data_agent.tools.providers.dataset import build_dataset_tool_registry
from data_agent.runtime.composition_root import (
    build_analysis_agent_runtime as build_analysis_composition_root,
)
from tests.unit.analysis_agent._graph_support import (
    GroundedSynthesizer,
    SequenceEvaluator,
    SequencePlanner,
    SequenceToolExecutor,
    analysis_plan,
    finish_decision,
)


PRINCIPAL = PrincipalContext(tenant_id="tenant-1", user_id="user-1")


def analysis_request(*, mode: AgentMode = AgentMode.PLAN) -> AgentRequest:
    return AgentRequest(
        question="Analyze the selected dataset",
        conversation_id="conversation-analysis",
        source_id="orders-source",
        source_version=2,
        binding_id="orders-binding",
        binding_version=3,
        mode=mode,
    )


def clarification_decision() -> PlannerDecision:
    return PlannerDecision(
        plan=analysis_plan("pending"),
        decision="clarify",
        clarification=AgentInputRequest(
            interrupt_id="interrupt-time-range",
            reason=AgentInputReason.CLARIFICATION,
            prompt="Which time range should be used?",
        ),
        rationale_summary="The time range is required.",
    )


class TestAnalysisResolver:
    __test__ = False

    def __init__(
        self,
        decisions: list[PlannerDecision | Exception],
        *,
        binding_version: int = 3,
        persist_turn=None,
    ) -> None:
        self._planner = SequencePlanner(decisions)
        self._binding_version = binding_version
        self._persist_turn = persist_turn
        # These values deliberately remain runtime-only and must never be checkpointed.
        self.connection_secret = "postgresql://checkpoint-user:marker-password@internal/db"
        self.large_runtime_value = "runtime-only-marker-" + "x" * 200_000

    async def resolve(self, *, request, principal, run_id) -> ResolvedAnalysisRun:
        del run_id
        authority = DatasetAuthority(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            source_id=request.source_id,
            source_version=request.source_version,
            binding_id=request.binding_id,
            binding_version=self._binding_version,
            schema_fingerprint="sha256:" + "a" * 64,
            allowed_relation_ids=("public.orders",),
            mode=request.mode,
        )
        registry = build_dataset_tool_registry()

        async def load_context(state):
            del state
            return AgentContextSnapshot(
                catalog_digest="b" * 64,
                binding_digest="c" * 64,
                catalog_summary={"relations": ["public.orders"]},
                semantic_summary={"fields": ["orders.amount"]},
                conversation_summary=None,
                allowed_tool_names=registry.names(),
            )

        context_values = dict(
            planner=self._planner,
            evaluator=SequenceEvaluator(),
            synthesizer=GroundedSynthesizer(),
            tool_executor=SequenceToolExecutor(["success"]),
            context_loader=load_context,
            tool_specs=registry.specs(),
            version_pins=build_dataset_version_pins(
                authority=authority,
                tool_registry_version=registry.version,
                model_versions=(
                    ComponentVersionPin(component="planner", version="fake@test"),
                ),
            ),
        )
        if self._persist_turn is not None:
            context_values["persist_turn"] = self._persist_turn
        context = AnalysisGraphContext(**context_values)
        return ResolvedAnalysisRun(authority=authority, graph_context=context)


class FailingResolver:
    async def resolve(self, **kwargs) -> ResolvedAnalysisRun:
        del kwargs
        raise AnalysisRuntimeError(
            AgentError(code=ErrorCode.BINDING_STALE, message="binding is stale")
        )


class AnalysisAgentRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_graph_cancellation_persists_cancelled_checkpoint_and_event(self) -> None:
        class BlockingPlanner:
            def __init__(self) -> None:
                self.started = asyncio.Event()

            async def decide(self, **kwargs):
                del kwargs
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        with tempfile.TemporaryDirectory() as directory:
            resolver = TestAnalysisResolver([])
            planner = BlockingPlanner()
            resolver._planner = planner
            composition = await build_analysis_runtime_from_resolver(
                resolver=resolver,
                checkpointer_factory=InMemoryCheckpointerFactory(),
            )
            coordinator = RunCoordinator(
                RunEventStore(Path(directory) / "run-events.sqlite3")
            )
            observed = []

            async def consume() -> None:
                async for event in coordinator.observe(
                    PRINCIPAL,
                    composition.runtime.run(
                        analysis_request(),
                        PRINCIPAL,
                        run_id="run-active-cancel",
                    ),
                ):
                    observed.append(event)

            task = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(planner.started.wait(), timeout=2)
                self.assertTrue(
                    await coordinator.cancel(
                        tenant_id=PRINCIPAL.tenant_id,
                        user_id=PRINCIPAL.user_id,
                        run_id="run-active-cancel",
                    )
                )
                await asyncio.wait_for(task, timeout=2)
                self.assertEqual(observed[-1].type, AgentEventType.RUN_FAILED)
                self.assertEqual(observed[-1].data.error_code, ErrorCode.CANCELLED)
                self.assertEqual(
                    await coordinator.status(
                        tenant_id=PRINCIPAL.tenant_id,
                        user_id=PRINCIPAL.user_id,
                        run_id="run-active-cancel",
                    ),
                    "cancelled",
                )
                state = await composition.runtime.state(
                    "run-active-cancel",
                    principal=PRINCIPAL,
                )
                self.assertEqual(str(state["status"]), "cancelled")
            finally:
                if not task.done():
                    task.cancel()
                await composition.close()

    async def test_dataset_resolver_composes_active_binding_and_governed_tools(self) -> None:
        class Model:
            model_id = "resolver-model"
            version = "test"

            async def complete(self, *args, **kwargs):  # pragma: no cover
                del args, kwargs
                raise AssertionError("context resolution must not call the model")

        with tempfile.TemporaryDirectory() as directory:
            service = DataSourceService(state_root=directory)
            try:
                await service.import_file_source(
                    tenant_id="tenant-1",
                    source_id="orders-source",
                    name="Orders",
                    uploads=[
                        UploadFile(
                            filename="orders.csv",
                            file=BytesIO(b"order_id,amount\nA-1,10\n"),
                        )
                    ],
                )
                draft = await service.create_binding(
                    tenant_id="tenant-1",
                    source_id="orders-source",
                    binding_id="orders-binding",
                    domain_id="dataset",
                    mappings=(
                        SemanticFieldMapping(
                            logical_ref="dataset.Orders.order_id",
                            physical_relation="public.orders",
                            physical_column="order_id",
                        ),
                        SemanticFieldMapping(
                            logical_ref="dataset.Orders.amount",
                            physical_relation="public.orders",
                            physical_column="amount",
                        ),
                    ),
                )
                binding = await service.activate_binding(
                    tenant_id="tenant-1",
                    source_id="orders-source",
                    binding_id=draft.binding_id,
                )
                request = analysis_request().model_copy(
                    update={
                        "source_version": binding.source_snapshot_version,
                        "binding_version": binding.version,
                    }
                )
                resolved = await DatasetAnalysisRunResolver(
                    data_sources=service,
                    model_client=Model(),
                    artifacts=SQLiteArtifactStore(directory),
                ).resolve(
                    request=request,
                    principal=PRINCIPAL,
                    run_id="run-resolver",
                )
                self.assertEqual(
                    resolved.authority.allowed_relation_ids,
                    ("public.orders",),
                )
                self.assertEqual(
                    resolved.graph_context.version_pins.binding_version,
                    binding.version,
                )
                self.assertEqual(len(resolved.graph_context.tool_specs), 12)
                snapshot = await resolved.graph_context.context_loader({})
                self.assertIn("query.compile", snapshot.allowed_tool_names)
                self.assertNotIn("query.execute", snapshot.allowed_tool_names)
            finally:
                await service.close()

    async def test_composition_root_builds_only_when_called_and_owns_model(self) -> None:
        class ClosableModel:
            model_id = "test-model"
            version = "test-version"

            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = ClosableModel()
            self.assertFalse((root / "control" / "agent-checkpoints.sqlite3").exists())
            composition = await build_analysis_composition_root(
                data_sources=object(),
                state_root=root,
                model_client_factory=lambda: model,
            )
            self.assertTrue((root / "control" / "agent-checkpoints.sqlite3").exists())
            await composition.close()
            self.assertTrue(model.closed)

    async def test_public_runtime_emits_one_closing_event_and_safe_resolver_error(self) -> None:
        composition = await build_analysis_runtime_from_resolver(
            resolver=TestAnalysisResolver([finish_decision(analysis_plan("pending"))]),
            checkpointer_factory=InMemoryCheckpointerFactory(),
            run_id_factory=lambda: "run-completed",
        )
        try:
            events = [
                event
                async for event in composition.runtime.run(
                    analysis_request(),
                    PRINCIPAL,
                )
            ]
            closing = [event for event in events if event.is_stream_closing]
            self.assertEqual(len(closing), 1)
            self.assertEqual(closing[0].type, AgentEventType.RUN_COMPLETED)
            self.assertEqual([event.sequence for event in events], list(range(len(events))))
            self.assertTrue(closing[0].response.ok)
        finally:
            await composition.close()

        failed = await build_analysis_runtime_from_resolver(
            resolver=FailingResolver(),
            checkpointer_factory=InMemoryCheckpointerFactory(),
            run_id_factory=lambda: "run-resolver-failed",
        )
        try:
            events = [
                event
                async for event in failed.runtime.run(analysis_request(), PRINCIPAL)
            ]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].type, AgentEventType.RUN_STARTED)
            self.assertEqual(events[1].type, AgentEventType.RUN_FAILED)
            self.assertEqual(events[1].response.error.code, ErrorCode.BINDING_STALE)
        finally:
            await failed.close()

    async def test_sqlite_lifecycle_is_inert_and_checkpoint_excludes_runtime_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = SQLiteCheckpointerFactory(root)
            self.assertFalse(factory.database_path.exists())
            resolver = TestAnalysisResolver([clarification_decision()])
            composition = await build_analysis_runtime_from_resolver(
                resolver=resolver,
                checkpointer_factory=factory,
            )
            try:
                events = [
                    event
                    async for event in composition.runtime.run(
                        analysis_request(),
                        PRINCIPAL,
                        run_id="run-checkpoint-safety",
                    )
                ]
                self.assertEqual(events[-1].type, AgentEventType.RUN_WAITING)
                self.assertTrue(factory.database_path.exists())
                self.assertEqual(factory.database_path.stat().st_mode & 0o777, 0o600)
            finally:
                await composition.close()
            checkpoint = factory.database_path.read_bytes()
            self.assertNotIn(resolver.connection_secret.encode(), checkpoint)
            self.assertNotIn(b"runtime-only-marker-", checkpoint)

    def test_checkpoint_serializer_is_non_pickle_and_rejects_callable(self) -> None:
        serializer = checkpoint_serializer()
        self.assertFalse(serializer.pickle_fallback)
        with self.assertRaises(TypeError):
            serializer.dumps_typed(lambda: None)


if __name__ == "__main__":
    unittest.main()
