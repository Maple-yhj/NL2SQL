"""Lifecycle-owned composition for the native dataset analysis runtime."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from data_agent.analysis_agent.artifacts import SQLiteArtifactStore
from data_agent.dataset_query import (
    DatasetQueryCompiler,
    DatasetQueryExecutor,
    DatasetQueryPlan,
    chart_for_result,
    tabular_rows,
)
from data_agent.datasources import DataSourceRegistryError, SemanticGraphBindingRecord
from data_agent.public_contracts import AgentError, ErrorCode
from data_agent.dataset_query.contracts import PreparedQuery
from data_agent.runtime.models import (
    AgentArtifactSummary,
    AgentRequest,
    AgentResponse,
    AnalysisStepSummary,
    ComponentVersionPin,
    EvidenceSummary,
    PrincipalContext,
)
from data_agent.tools import ToolInvoker
from data_agent.tools.providers.dataset import (
    DatasetCredentialBroker,
    DatasetToolRuntime,
    build_dataset_tool_registry,
)
from data_agent.tools.providers.dataset.contracts import (
    PreparedQueryArtifactPayload,
)
from data_agent.tools.schemas import TabularResult

from .checkpoints import (
    CheckpointerFactory,
    CheckpointerResource,
    InMemoryCheckpointerFactory,
    SQLiteCheckpointerFactory,
)
from .evaluator import AnalysisEvaluator
from .graph import build_analysis_agent_graph, build_dataset_version_pins
from .models import (
    AgentContextSnapshot,
    AgentRunBudget,
    AgentStatus,
    DatasetAuthority,
    stable_digest,
)
from .nodes import AnalysisGraphContext, DatasetAgentToolInvoker
from .planner import AnalysisPlanner
from .runtime import (
    AnalysisRunResolver,
    AnalysisRuntimeError,
    DataAnalysisAgentRuntime,
    ResolvedAnalysisRun,
)
from .synthesizer import AnalysisSynthesizer


class DataSourceAuthorityService(Protocol):
    async def resolve_active_binding(self, **kwargs: object) -> object: ...

    async def pin_conversation(self, **kwargs: object) -> object: ...


ConversationSummaryLoader = Callable[
    [AgentRequest, PrincipalContext],
    str | None | Awaitable[str | None],
]


async def _no_conversation_summary(
    request: AgentRequest,
    principal: PrincipalContext,
) -> str | None:
    del request, principal
    return None


class DatasetAnalysisRunResolver:
    def __init__(
        self,
        *,
        data_sources: DataSourceAuthorityService,
        model_client: object,
        artifacts: SQLiteArtifactStore,
        budget_limits: AgentRunBudget | None = None,
        conversation_summary_loader: ConversationSummaryLoader = _no_conversation_summary,
        persist_turn=None,
        response_builder=None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._data_sources = data_sources
        self._model_client = model_client
        self._artifacts = artifacts
        self._budget_limits = budget_limits or AgentRunBudget()
        self._conversation_summary_loader = conversation_summary_loader
        self._persist_turn = persist_turn
        self._response_builder = response_builder
        self._cancelled = cancelled

    async def resolve(
        self,
        *,
        request: AgentRequest,
        principal: PrincipalContext,
        run_id: str,
    ) -> ResolvedAnalysisRun:
        del run_id
        if (
            request.source_id is None
            or request.source_version is None
            or request.binding_id is None
            or request.binding_version is None
        ):
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.INVALID_REQUEST,
                    message="dataset analysis requires complete datasource pins",
                )
            )
        try:
            execution = await self._data_sources.resolve_active_binding(
                tenant_id=principal.tenant_id,
                source_id=request.source_id,
                source_version=request.source_version,
                binding_id=request.binding_id,
                binding_version=request.binding_version,
                domain_id=request.domain_id,
            )
            if request.conversation_id is not None:
                await self._data_sources.pin_conversation(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    conversation_id=request.conversation_id,
                    binding=execution.binding,
                )
        except DataSourceRegistryError as exc:
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.BINDING_STALE,
                    message="selected datasource binding is inactive or stale",
                    retryable=True,
                )
            ) from exc
        snapshot = execution.snapshot
        binding = execution.binding
        catalog = snapshot.catalog
        relations = tuple(item.relation for item in catalog.relations)
        authority = DatasetAuthority(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            source_id=request.source_id,
            source_version=request.source_version,
            binding_id=request.binding_id,
            binding_version=request.binding_version,
            schema_fingerprint=snapshot.fingerprint,
            allowed_relation_ids=relations,
            mode=request.mode,
        )
        bundle_digest = hashlib.sha256(
            (
                f"{authority.source_id}:{authority.source_version}:"
                f"{authority.binding_id}:{authority.binding_version}"
            ).encode("utf-8")
        ).hexdigest()
        tool_runtime = DatasetToolRuntime(
            authority=authority,
            catalog=catalog,
            binding=binding,
            connector=execution.connector,
            connection_ref=execution.connection_ref,
            bundle_digest=bundle_digest,
            artifacts=self._artifacts,
            compiler=DatasetQueryCompiler(),
            executor=DatasetQueryExecutor(),
        )
        registry = build_dataset_tool_registry()
        invoker = ToolInvoker(
            registry,
            credential_broker=DatasetCredentialBroker(tool_runtime),
        )
        tool_executor = DatasetAgentToolInvoker(
            registry=registry,
            invoker=invoker,
            principal=principal,
            runtime_resources=tool_runtime,
            max_rows=self._budget_limits.max_result_rows,
        )
        conversation = self._conversation_summary_loader(request, principal)
        if inspect.isawaitable(conversation):
            conversation = await conversation
        relationship_digest = (
            stable_digest(binding.graph.model_dump(mode="json"))
            if isinstance(binding, SemanticGraphBindingRecord)
            else None
        )
        catalog_digest = stable_digest(catalog.model_dump(mode="json"))
        binding_digest = stable_digest(binding.model_dump(mode="json"))
        allowed_names = tuple(
            spec.name
            for spec in registry.specs()
            if "dataset" in spec.authority_kinds and request.mode in spec.allowed_modes
        )
        snapshot_context = AgentContextSnapshot(
            catalog_digest=catalog_digest,
            binding_digest=binding_digest,
            relationship_graph_digest=relationship_digest,
            catalog_summary=_catalog_summary(catalog),
            semantic_summary=_semantic_summary(binding),
            conversation_summary=conversation,
            allowed_tool_names=allowed_names,
        )

        async def load_context(_state):
            return snapshot_context

        model_id = str(getattr(self._model_client, "model_id", "unknown-model"))
        model_version = str(getattr(self._model_client, "version", "unknown-version"))
        pins = build_dataset_version_pins(
            authority=authority,
            tool_registry_version=registry.version,
            model_versions=tuple(
                ComponentVersionPin(component=component, version=f"{model_id}@{model_version}")
                for component in ("planner", "evaluator", "synthesizer")
            ),
            relationship_graph_digest=relationship_digest,
        )
        context_values: dict[str, Any] = {
            "planner": AnalysisPlanner(self._model_client),
            "evaluator": AnalysisEvaluator(self._model_client),
            "synthesizer": AnalysisSynthesizer(self._model_client),
            "tool_executor": tool_executor,
            "context_loader": load_context,
            "tool_specs": registry.specs(),
            "version_pins": pins,
            "budget_limits": self._budget_limits,
        }
        if self._response_builder is not None:
            response_builder = self._response_builder
        else:
            async def response_builder(state, ok):
                return await _build_dataset_response(
                    artifacts=self._artifacts,
                    state=state,
                    pins=pins,
                    ok=ok,
                )

        context_values["response_builder"] = response_builder
        if self._persist_turn is not None:
            async def persist_turn(state):
                completed_state = dict(state)
                completed_state["status"] = AgentStatus.COMPLETED
                response = response_builder(completed_state, True)
                if inspect.isawaitable(response):
                    response = await response
                persisted = self._persist_turn(completed_state, response)
                if inspect.isawaitable(persisted):
                    await persisted

            context_values["persist_turn"] = persist_turn
        if self._cancelled is not None:
            context_values["cancelled"] = self._cancelled
        return ResolvedAnalysisRun(
            authority=authority,
            graph_context=AnalysisGraphContext(**context_values),
        )


@dataclass(slots=True)
class AnalysisRuntimeComposition:
    runtime: DataAnalysisAgentRuntime
    checkpointer: CheckpointerResource
    resources: tuple[object, ...] = ()
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.runtime.close()
        await self.checkpointer.close()
        for resource in reversed(self.resources):
            close = getattr(resource, "close", None) or getattr(resource, "aclose", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result


async def build_analysis_runtime_from_resolver(
    *,
    resolver: AnalysisRunResolver,
    checkpointer_factory: CheckpointerFactory,
    budget_limits: AgentRunBudget | None = None,
    run_id_factory=None,
    resources: tuple[object, ...] = (),
) -> AnalysisRuntimeComposition:
    checkpointer = await checkpointer_factory.open()
    try:
        graph = build_analysis_agent_graph(
            checkpointer=checkpointer.checkpointer,
            budget_limits=budget_limits,
        )
        kwargs = {"graph": graph, "resolver": resolver}
        if run_id_factory is not None:
            kwargs["run_id_factory"] = run_id_factory
        runtime = DataAnalysisAgentRuntime(**kwargs)
        return AnalysisRuntimeComposition(
            runtime=runtime,
            checkpointer=checkpointer,
            resources=resources,
        )
    except BaseException:
        await checkpointer.close()
        raise


async def build_analysis_agent_runtime(
    *,
    data_sources: DataSourceAuthorityService,
    model_client: object,
    state_root: str | Path,
    checkpointer_factory: CheckpointerFactory | None = None,
    budget_limits: AgentRunBudget | None = None,
    conversation_summary_loader: ConversationSummaryLoader = _no_conversation_summary,
    persist_turn=None,
    response_builder=None,
    run_id_factory=None,
    resources: tuple[object, ...] = (),
) -> AnalysisRuntimeComposition:
    root = Path(state_root).expanduser().resolve()
    artifacts = SQLiteArtifactStore(root)
    resolver = DatasetAnalysisRunResolver(
        data_sources=data_sources,
        model_client=model_client,
        artifacts=artifacts,
        budget_limits=budget_limits,
        conversation_summary_loader=conversation_summary_loader,
        persist_turn=persist_turn,
        response_builder=response_builder,
    )
    return await build_analysis_runtime_from_resolver(
        resolver=resolver,
        checkpointer_factory=(
            checkpointer_factory or SQLiteCheckpointerFactory(root)
        ),
        budget_limits=budget_limits,
        run_id_factory=run_id_factory,
        resources=resources,
    )


def _catalog_summary(catalog) -> dict[str, object]:
    return {
        "relationCount": len(catalog.relations),
        "relations": [
            {
                "relationId": relation.relation_id,
                "name": relation.relation,
                "estimatedRows": relation.estimated_rows,
                "columns": [
                    {
                        "columnId": column.column_id,
                        "name": column.name,
                        "type": column.data_type,
                        "nullable": column.nullable,
                    }
                    for column in relation.columns[:80]
                ],
            }
            for relation in catalog.relations[:40]
        ],
    }


def _semantic_summary(binding) -> dict[str, object]:
    return {
        "bindingId": binding.binding_id,
        "bindingVersion": binding.version,
        "logicalFields": [mapping.logical_ref for mapping in binding.mappings[:400]],
        "relationshipCount": (
            len(binding.graph.edges)
            if isinstance(binding, SemanticGraphBindingRecord)
            else len(binding.relationships)
        ),
    }


async def _build_dataset_response(
    *,
    artifacts: SQLiteArtifactStore,
    state,
    pins,
    ok: bool,
) -> AgentResponse:
    prepared: PreparedQuery | None = None
    dataset_plan: DatasetQueryPlan | None = None
    result: TabularResult | None = None
    for reference in tuple(state.get("artifact_refs", ())):
        try:
            document = await artifacts.get_json(
                tenant_id=state["authority"].tenant_id,
                user_id=state["authority"].user_id,
                run_id=state["run_id"],
                artifact_id=reference.artifact_id,
            )
            if reference.kind.value == "prepared_query":
                payload = PreparedQueryArtifactPayload.model_validate(document)
                prepared = payload.prepared
                dataset_plan = payload.plan
            elif reference.kind.value in {"query_preview", "query_result"}:
                result = TabularResult.model_validate(document)
        except (KeyError, TypeError, ValueError):
            continue
    rows = tabular_rows(result) if result is not None else ()
    chart = (
        chart_for_result(
            result,
            plan=dataset_plan,
            title=state["request"].question,
        )
        if result is not None and dataset_plan is not None
        else None
    )
    draft = state.get("answer_draft")
    error = None if ok else state.get("error")
    if not ok and error is None:
        error = AgentError(
            code=ErrorCode.INTERNAL_ERROR,
            message="analysis run failed safely",
        )
    artifact_refs = tuple(state.get("artifact_refs", ()))
    evidence_refs = tuple(state.get("evidence_refs", ()))
    return AgentResponse(
        ok=ok,
        question=state["request"].question,
        contextualized_question=(
            state.get("goal").contextualized_question
            if state.get("goal") is not None
            else state["request"].question
        ),
        conversation_id=state["request"].conversation_id,
        tenant_id=state["authority"].tenant_id,
        logical_plan=prepared.logical_plan if prepared is not None else None,
        dataset_query_plan=(
            dataset_plan.model_dump(mode="json")
            if dataset_plan is not None
            else None
        ),
        sql=prepared.logical_sql if prepared is not None else None,
        message_type="analysis" if ok else "error",
        rows=rows,
        chart=chart,
        answer=draft.answer if ok and draft is not None else None,
        error=error,
        version_pins=pins,
        analysis_plan=state.get("plan"),
        analysis_steps=_analysis_step_summaries(state),
        artifacts=tuple(
            AgentArtifactSummary(
                artifact_id=item.artifact_id,
                kind=item.kind,
                digest=item.digest,
                row_count=item.row_count,
                sensitivity=item.sensitivity,
                created_at=item.created_at,
            )
            for item in artifact_refs
        ),
        evidence=tuple(
            EvidenceSummary(
                evidence_id=item.evidence_id,
                claim_key=item.claim_key,
                artifact_id=item.artifact_id,
                field_refs=item.field_refs,
            )
            for item in evidence_refs
        ),
        limitations=draft.limitations if draft is not None else (),
    )


def _analysis_step_summaries(state) -> tuple[AnalysisStepSummary, ...]:
    plan = state.get("plan")
    if plan is None:
        return ()
    action_steps = state.get("action_step_ids", {})
    observations = tuple(state.get("observations", ()))
    return tuple(
        AnalysisStepSummary(
            step_id=step.step_id,
            objective=step.objective,
            status=step.status,
            tool_names=tuple(
                dict.fromkeys(
                    item.tool_name
                    for item in observations
                    if action_steps.get(item.action_id) == step.step_id
                )
            ),
            evidence_ids=tuple(
                dict.fromkeys(
                    evidence.evidence_id
                    for item in observations
                    if action_steps.get(item.action_id) == step.step_id
                    for evidence in item.evidence_refs
                )
            ),
        )
        for step in plan.steps
    )


__all__ = [
    "AnalysisRuntimeComposition",
    "DataSourceAuthorityService",
    "DatasetAnalysisRunResolver",
    "build_analysis_agent_runtime",
    "build_analysis_runtime_from_resolver",
]
