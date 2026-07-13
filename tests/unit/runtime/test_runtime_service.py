from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from data_agent.execution import (
    Artifact,
    ArtifactKind,
    COMMERCE_EXECUTION_GRAPH,
    ExecutionCheckpoint,
    ExecutionError,
    ExecutionResult,
    ExecutionState,
    ExecutionStatus,
    ExecutionToolTrace,
    ExecutionVersionPins,
    FinalOutput,
)
from data_agent.memory import (
    ConversationWriteBatch,
    EnterpriseMemoryContent,
    EnterpriseMemoryOwner,
    MemoryCandidate,
    NullMemoryManager,
    ProposalStatus,
)
from data_agent.runtime.bundle_store import BundlePaths, BundleStore
from data_agent.runtime.composition import canonical_json, stable_digest
from data_agent.runtime.events import AgentEventType
from data_agent.runtime.models import AgentMode, AgentRequest, AgentResponse, PrincipalContext
from data_agent.skills import BUILTIN_SKILL_REGISTRY, logical_plan_from_eval_case
from data_agent.tools.providers import AnswerRenderOutput, QueryData
from data_agent.tools.schemas import QueryRow


class _Registry:
    version = "1.0.0"

    @staticmethod
    def specs():
        return tuple(
            SimpleNamespace(name=name, version="1.0.0")
            for name in (
                "semantic.search",
                "data.inspect",
                "query.compile",
                "query.execute",
                "result.profile",
                "answer.render",
            )
        )


class _ModelClient:
    model_id = "planner"
    version = "model-v1"

    async def complete(self, prompt: str, **kwargs) -> str:
        return "{}"


class _Planner:
    async def build_plan(self, **kwargs):
        raise AssertionError("the graph executor owns planning")


class _Resolver:
    def __init__(self) -> None:
        self.bound: list[tuple[str, str]] = []
        self.unbound: list[str] = []

    async def bind_run(self, *, run_id: str, conversation_id: str, **kwargs) -> None:
        self.bound.append((run_id, conversation_id))

    async def unbind_run(self, run_id: str) -> None:
        self.unbound.append(run_id)


class _RecordingMemory(NullMemoryManager):
    def __init__(self, *, fail_save: bool = False) -> None:
        super().__init__()
        self.fail_save = fail_save
        self.saved: list[ConversationWriteBatch] = []

    async def save_turn(self, batch: ConversationWriteBatch) -> None:
        self.saved.append(batch)
        if self.fail_save:
            raise RuntimeError("database detail must not escape")
        await super().save_turn(batch)


class _ProposalFactory:
    def __init__(self, candidates=()) -> None:
        self.candidates = tuple(candidates)

    async def build(self, **kwargs):
        return self.candidates


class _Closeable:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _Executor:
    graph = COMMERCE_EXECUTION_GRAPH

    def __init__(self, plan, *, status=ExecutionStatus.SUCCEEDED, error=None) -> None:
        self.plan = plan
        self.status = status
        self.error = error
        self.contexts = []
        self.checkpoint_nodes = []
        self.resume_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def execute(self, context):
        raise AssertionError("runtime must use the graph checkpoint/resume path")

    async def create_checkpoint(self, context, *, after_node: str):
        self.contexts.append(context)
        self.checkpoint_nodes.append(after_node)
        self.started.set()
        await self.release.wait()
        plan_artifact = Artifact.create(
            kind=ArtifactKind.LOGICAL_PLAN,
            producing_node="build_logical_plan",
            payload=self.plan,
        )
        state = ExecutionState(
            run_id=context.run_id,
            mode=context.mode,
            status=ExecutionStatus.PAUSED,
            current_node=after_node,
            next_node="compile_query",
            artifacts=(plan_artifact,),
            node_trace=(
                "resolve_context",
                "semantic_search",
                "build_logical_plan",
                "validate_logical_plan",
            ),
        )
        return ExecutionCheckpoint.capture(
            pins=ExecutionVersionPins.for_run(context, self.graph),
            state=state,
        )

    async def resume(self, checkpoint, context):
        self.resume_calls += 1
        if self.status == ExecutionStatus.SUCCEEDED:
            return self._success(context)
        return self._failure(context)

    def _success(self, context):
        plan_artifact = Artifact.create(
            kind=ArtifactKind.LOGICAL_PLAN,
            producing_node="build_logical_plan",
            payload=self.plan,
        )
        artifacts = [plan_artifact]
        row_count = 0
        answer = None
        query_hash = self.plan.stable_hash()
        if context.mode != AgentMode.PLAN:
            data = QueryData(
                logical_plan_hash=self.plan.stable_hash(),
                query_hash=query_hash,
                policy_decision_id="policy-1",
                verification_token="verified",
                columns=("seller", "gmv"),
                rows=(
                    QueryRow(values=("seller-1", 42.0)),
                    QueryRow(values=("seller-2", 21.0)),
                ),
            )
            data_kind = (
                ArtifactKind.QUERY_RESULT
                if context.mode == AgentMode.EXECUTE
                else ArtifactKind.QUERY_PREVIEW
            )
            artifacts.append(
                Artifact.create(
                    kind=data_kind,
                    producing_node="execute_query",
                    payload=data,
                )
            )
            rendered = AnswerRenderOutput(
                answer="seller-1 leads",
                evidence_query_hash=query_hash,
                policy_decision_id="policy-1",
            )
            artifacts.append(
                Artifact.create(
                    kind=ArtifactKind.ANSWER,
                    producing_node="render_answer",
                    payload=rendered,
                )
            )
            row_count = 2
            answer = rendered.answer
        final_payload = FinalOutput(
            status=ExecutionStatus.SUCCEEDED,
            mode=context.mode,
            logical_plan_hash=self.plan.stable_hash(),
            query_hash=query_hash,
            policy_decision_id="policy-1",
            row_count=row_count,
            answer=answer,
            artifact_digests=tuple(item.digest for item in artifacts),
        )
        final = Artifact.create(
            kind=ArtifactKind.FINAL,
            producing_node="finalize",
            payload=final_payload,
        )
        artifacts.append(final)
        state = ExecutionState(
            run_id=context.run_id,
            mode=context.mode,
            status=ExecutionStatus.SUCCEEDED,
            current_node="finalize",
            artifacts=tuple(artifacts),
            node_trace=(
                "resolve_context",
                "semantic_search",
                "build_logical_plan",
                "validate_logical_plan",
                "compile_query",
                "validate_query",
                "finalize",
            ),
            tool_trace=(
                ExecutionToolTrace(
                    call_id=f"{context.run_id}:1:semantic_search",
                    tool_name="semantic.search",
                    tool_version="1.0.0",
                    status="success",
                    attempts=1,
                ),
            ),
            tool_calls=1,
            result_rows=row_count,
        )
        return ExecutionResult(state=state, final_artifact=final)

    def _failure(self, context):
        error = self.error or ExecutionError(
            code="BINDING_STALE",
            message="postgresql://secret-internal",
            node_id="compile_query",
            retryable=True,
        )
        final = Artifact.create(
            kind=ArtifactKind.FINAL,
            producing_node="compile_query",
            payload=FinalOutput(
                status=self.status,
                mode=context.mode,
                error_code=error.code,
            ),
        )
        state = ExecutionState(
            run_id=context.run_id,
            mode=context.mode,
            status=self.status,
            current_node="compile_query",
            artifacts=(final,),
            node_trace=("resolve_context", "compile_query"),
            error=error,
        )
        return ExecutionResult(state=state, final_artifact=final)


class RuntimeServiceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = BundlePaths(
            domain_root=ROOT / "packs" / "domains" / "commerce",
            enterprise_root=ROOT / "packs" / "enterprises" / "olist",
            deployment_profile=ROOT / "packs" / "deployments" / "olist-local.yaml",
            pack_lock=ROOT / "packs" / "enterprises" / "olist" / "pack.lock",
            schema_catalog=ROOT / "schema_catalog.json",
            bundle_manifest=ROOT / "generated" / "bundles" / "olist-local.json",
        )
        cls.alternate_manifest = (
            ROOT / "generated" / "bundles" / ".task8-runtime-service-bundle.json"
        )
        cls.store = BundleStore()
        cls.snapshot = cls.store.load_and_activate(cls.paths)
        case = next(
            item
            for item in cls.snapshot.domain_pack.spec.evals
            if item.id == "commerce.metric_002"
        )
        cls.plan = logical_plan_from_eval_case(case, cls.snapshot.domain_pack)
        cls.principal = PrincipalContext(
            tenant_id="tenant-1",
            user_id="user-1",
            roles=("analyst",),
        )

    async def asyncSetUp(self) -> None:
        # Each test owns its store because hot activation is intentionally mutable.
        if self.alternate_manifest.exists():
            self.alternate_manifest.unlink()
        self.store = BundleStore()
        self.store.load_and_activate(self.paths)
        self.memory = _RecordingMemory()
        conversation = await self.memory.create_conversation(
            tenant_id=self.principal.tenant_id,
            user_id=self.principal.user_id,
            domain_id="commerce",
        )
        self.conversation_id = conversation.conversation_id
        self.resolver = _Resolver()
        self.executor = _Executor(self.plan)
        self.run_number = 0

    async def asyncTearDown(self) -> None:
        if self.alternate_manifest.exists():
            self.alternate_manifest.unlink()

    def _write_alternate_manifest(self) -> Path:
        document = json.loads(self.paths.bundle_manifest.read_text(encoding="utf-8"))
        bundle = document["bundle"]
        bundle.pop("digest")
        bundle["runtime_version"] = "1.0.1"
        bundle["digest"] = stable_digest(bundle)
        document["bundleDigest"] = bundle["digest"]
        self.alternate_manifest.write_text(
            canonical_json(document) + "\n",
            encoding="utf-8",
        )
        return self.alternate_manifest

    def _dependencies(self, **overrides):
        from data_agent.runtime.dependencies import RuntimeDependencies

        values = {
            "bundle_store": self.store,
            "skill_registry": BUILTIN_SKILL_REGISTRY,
            "tool_registry": _Registry(),
            "graph": COMMERCE_EXECUTION_GRAPH,
            "executor": self.executor,
            "memory": self.memory,
            "context_resolver": self.resolver,
            "planner": _Planner(),
            "model_client": _ModelClient(),
            "proposal_factory": _ProposalFactory(),
            "run_id_factory": self._run_id,
        }
        values.update(overrides)
        return RuntimeDependencies(**values)

    def _run_id(self) -> str:
        self.run_number += 1
        return f"run-{self.run_number}"

    def _request(self, mode=AgentMode.EXECUTE, *, include_trace=False):
        return AgentRequest(
            question="Show GMV by seller",
            mode=mode,
            conversation_id=self.conversation_id,
            include_trace=include_trace,
        )

    async def _events(self, runtime, request):
        return [event async for event in runtime.run(request, self.principal)]

    async def test_three_modes_share_graph_and_emit_contiguous_typed_events(self) -> None:
        from data_agent.runtime.service import DefaultDataAgentRuntime

        runtime = DefaultDataAgentRuntime(self._dependencies())
        for mode in AgentMode:
            with self.subTest(mode=mode):
                events = await self._events(runtime, self._request(mode))
                self.assertEqual(
                    [event.sequence for event in events],
                    list(range(len(events))),
                )
                self.assertEqual(len({event.run_id for event in events}), 1)
                self.assertEqual(events[0].type, AgentEventType.RUN_STARTED)
                self.assertEqual(events[-1].type, AgentEventType.RUN_COMPLETED)
                self.assertIn("version_pins", AgentResponse.model_fields)
                self.assertEqual(
                    events[-1].response.version_pins,
                    events[1].data.pins,
                )
                self.assertEqual(
                    sum(
                        event.type
                        in {AgentEventType.RUN_COMPLETED, AgentEventType.RUN_FAILED}
                        for event in events
                    ),
                    1,
                )
                self.assertIsInstance(events[-1].response, AgentResponse)
                self.assertTrue(events[-1].response.ok)
                self.assertEqual(events[-1].response.logical_plan, self.plan)
                self.assertIs(self.executor.contexts[-1].bundle, self.store.snapshot().bundle)
                self.assertIs(self.executor.graph, COMMERCE_EXECUTION_GRAPH)
                self.assertEqual(self.executor.checkpoint_nodes[-1], "validate_logical_plan")
                self.assertIsNotNone(self.memory.saved[-1].checkpoint)
                pins = events[1].data.pins
                self.assertEqual(pins.bundle_digest, self.store.snapshot().bundle.digest)
                self.assertEqual(pins.graph_digest, COMMERCE_EXECUTION_GRAPH.digest)
                self.assertEqual(
                    tuple((pin.component, pin.version) for pin in pins.model_versions),
                    (("planner", "model-v1"),),
                )
                if mode == AgentMode.PLAN:
                    self.assertEqual(events[-1].response.rows, ())
                else:
                    self.assertEqual(len(events[-1].response.rows), 2)
                    self.assertEqual(events[-1].response.answer, "seller-1 leads")

    async def test_hot_activation_does_not_change_running_version_pins(self) -> None:
        from data_agent.runtime.service import DefaultDataAgentRuntime

        self.executor.release.clear()
        runtime = DefaultDataAgentRuntime(self._dependencies())
        first_task = asyncio.create_task(self._events(runtime, self._request()))
        await self.executor.started.wait()
        old_digest = self.executor.contexts[0].bundle.digest

        candidate = self.store.stage(
            replace(
                self.paths,
                bundle_manifest=self._write_alternate_manifest(),
            )
        )
        self.store.activate(candidate)
        self.executor.release.set()
        first = await first_task
        second = await self._events(runtime, self._request())

        self.assertEqual(first[1].data.pins.bundle_digest, old_digest)
        self.assertEqual(self.executor.contexts[0].bundle.digest, old_digest)
        self.assertEqual(
            self.executor.contexts[1].bundle.digest,
            self.store.snapshot().bundle.digest,
        )
        self.assertEqual(
            second[1].data.pins.bundle_digest,
            self.store.snapshot().bundle.digest,
        )

    async def test_trace_is_hidden_from_response_but_saved_to_audit(self) -> None:
        from data_agent.runtime.service import DefaultDataAgentRuntime

        runtime = DefaultDataAgentRuntime(self._dependencies())
        hidden = await self._events(runtime, self._request(include_trace=False))
        self.assertEqual(hidden[-1].response.trace, ())
        self.assertGreater(len(self.memory.saved[-1].assistant_message.payload.trace), 0)

        visible = await self._events(runtime, self._request(include_trace=True))
        self.assertGreater(len(visible[-1].response.trace), 0)
        self.assertEqual(
            len(visible[-1].response.trace),
            len(self.memory.saved[-1].assistant_message.payload.trace),
        )

    async def test_execution_failure_is_safe_and_uses_stable_error_code(self) -> None:
        from data_agent.runtime.errors import ErrorCode
        from data_agent.runtime.service import DefaultDataAgentRuntime

        self.executor.status = ExecutionStatus.FAILED
        runtime = DefaultDataAgentRuntime(self._dependencies())
        events = await self._events(runtime, self._request())

        self.assertEqual(events[-1].type, AgentEventType.RUN_FAILED)
        self.assertEqual(events[-1].response.error.code, ErrorCode.BINDING_STALE)
        self.assertNotIn("postgresql", events[-1].response.error.message)
        self.assertEqual(len(self.memory.saved), 1)

    async def test_timeout_and_cancellation_have_unique_safe_terminals(self) -> None:
        from data_agent.runtime.errors import ErrorCode
        from data_agent.runtime.service import DefaultDataAgentRuntime

        self.executor.release.clear()
        timeout_runtime = DefaultDataAgentRuntime(
            self._dependencies(deadline_seconds=0.01)
        )
        timed_out = await self._events(timeout_runtime, self._request())
        self.assertEqual(timed_out[-1].type, AgentEventType.RUN_FAILED)
        self.assertEqual(timed_out[-1].response.error.code, ErrorCode.DEADLINE_EXCEEDED)

        self.executor = _Executor(self.plan)
        self.executor.release.clear()
        cancel_runtime = DefaultDataAgentRuntime(self._dependencies())
        stream = cancel_runtime.run(self._request(), self.principal)
        first = await anext(stream)
        pinned = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await self.executor.started.wait()
        pending.cancel()
        cancelled = await pending
        self.assertEqual(first.sequence, 0)
        self.assertEqual(pinned.sequence, 1)
        self.assertEqual(cancelled.sequence, 2)
        self.assertEqual(cancelled.type, AgentEventType.RUN_FAILED)
        self.assertEqual(cancelled.response.error.code, ErrorCode.CANCELLED)

    async def test_persistence_failure_never_emits_a_completed_terminal(self) -> None:
        from data_agent.runtime.errors import ErrorCode
        from data_agent.runtime.service import DefaultDataAgentRuntime

        self.memory = _RecordingMemory(fail_save=True)
        conversation = await self.memory.create_conversation(
            tenant_id=self.principal.tenant_id,
            user_id=self.principal.user_id,
            domain_id="commerce",
        )
        self.conversation_id = conversation.conversation_id
        runtime = DefaultDataAgentRuntime(self._dependencies())
        events = await self._events(runtime, self._request())

        self.assertEqual(events[-1].type, AgentEventType.RUN_FAILED)
        self.assertEqual(events[-1].response.error.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn(AgentEventType.RUN_COMPLETED, [event.type for event in events])
        messages = await self.memory.list_messages(
            tenant_id=self.principal.tenant_id,
            user_id=self.principal.user_id,
            domain_id="commerce",
            conversation_id=self.conversation_id,
            limit=10,
        )
        self.assertEqual(messages, ())

    async def test_enterprise_memory_is_proposed_but_never_autoapproved(self) -> None:
        from data_agent.runtime.service import DefaultDataAgentRuntime

        candidate = MemoryCandidate(
            owner=EnterpriseMemoryOwner(
                tenant_id=self.principal.tenant_id,
                domain_id="commerce",
            ),
            content=EnterpriseMemoryContent(
                category="metric_note",
                statement="Use governed GMV semantics",
            ),
            source="runtime.finalize",
        )
        runtime = DefaultDataAgentRuntime(
            self._dependencies(proposal_factory=_ProposalFactory((candidate,)))
        )
        events = await self._events(runtime, self._request())

        self.assertEqual(len(events[-1].response.pending_memory_updates), 1)
        self.assertEqual(self.memory.proposal_count, 1)
        self.assertEqual(self.memory.records, ())
        proposal = next(iter(self.memory._proposals.values()))
        self.assertEqual(proposal.status, ProposalStatus.PENDING_APPROVAL)

    async def test_close_is_idempotent_and_deduplicates_resources(self) -> None:
        from data_agent.runtime.service import DefaultDataAgentRuntime

        first = _Closeable()
        second = _Closeable()
        runtime = DefaultDataAgentRuntime(
            self._dependencies(resources=(first, first, second))
        )

        await runtime.close()
        await runtime.close()

        self.assertEqual(first.close_calls, 1)
        self.assertEqual(second.close_calls, 1)

    async def test_conversation_crud_is_exposed_only_through_public_runtime_facade(self) -> None:
        from data_agent.runtime.service import DefaultDataAgentRuntime

        runtime = DefaultDataAgentRuntime(self._dependencies())
        self.assertTrue(hasattr(runtime, "create_conversation"))
        created = await runtime.create_conversation(
            principal=self.principal,
            domain_id="commerce",
            title="Quarterly GMV",
        )

        self.assertEqual(created.tenant_id, self.principal.tenant_id)
        self.assertEqual(created.user_id, self.principal.user_id)
        self.assertEqual(created.domain_id, "commerce")
        self.assertFalse(created.archived)

        active = await runtime.list_conversations(
            principal=self.principal,
            domain_id="commerce",
            limit=20,
            include_archived=False,
        )
        self.assertIn(created.conversation_id, {item.conversation_id for item in active})
        fetched = await runtime.get_conversation(
            principal=self.principal,
            domain_id="commerce",
            conversation_id=created.conversation_id,
        )
        self.assertEqual(fetched, created)

        archived = await runtime.update_conversation(
            principal=self.principal,
            domain_id="commerce",
            conversation_id=created.conversation_id,
            title="Final GMV",
            archived=True,
        )
        self.assertEqual(archived.title, "Final GMV")
        self.assertTrue(archived.archived)
        active = await runtime.list_conversations(
            principal=self.principal,
            domain_id="commerce",
            limit=20,
            include_archived=False,
        )
        self.assertNotIn(created.conversation_id, {item.conversation_id for item in active})
        all_items = await runtime.list_conversations(
            principal=self.principal,
            domain_id="commerce",
            limit=20,
            include_archived=True,
        )
        self.assertIn(created.conversation_id, {item.conversation_id for item in all_items})
        self.assertEqual(
            await runtime.list_conversation_messages(
                principal=self.principal,
                domain_id="commerce",
                conversation_id=created.conversation_id,
                limit=50,
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
