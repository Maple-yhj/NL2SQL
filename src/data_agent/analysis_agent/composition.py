"""Lifecycle-owned composition for the native dataset analysis runtime."""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from data_agent.analysis_agent.artifacts import SQLiteArtifactStore
from data_agent.dataset_query import (
    DatasetLogicalPlanner,
    DatasetPlanStatus,
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
from data_agent.tools.models import ToolSpec
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
    AgentAction,
    AgentContextSnapshot,
    AgentInputReason,
    AgentInputRequest,
    AgentRunBudget,
    AgentStatus,
    AnalysisGoal,
    AnalysisPlan,
    AnalysisStep,
    DatasetAuthority,
    PlannerDecision,
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


class _DatasetNextActionResolver:
    """Translate a high-level Agent plan into schema-valid dataset tool actions."""

    def __init__(self, *, model_client: object, binding: object, catalog: object) -> None:
        self._logical_planner = DatasetLogicalPlanner(model_client)  # type: ignore[arg-type]
        self._binding = binding
        self._catalog = catalog
        self._query_plan: DatasetQueryPlan | None = None

    def requires_model_call(self, *, state: object) -> bool:
        if not isinstance(state, dict):
            return False
        if state.get("replan_requested"):
            plan_value = state.get("plan")
            goal_value = state.get("goal")
            if plan_value is not None and goal_value is not None:
                plan = AnalysisPlan.model_validate(plan_value)
                goal = AnalysisGoal.model_validate(goal_value)
                if (
                    plan.plan_id.startswith("metric-clarification-")
                    and "Clarification response:" in goal.contextualized_question
                ):
                    return False
        observations = tuple(state.get("observations", ()))
        if not observations or observations[-1].status != "succeeded":
            return False
        plan = AnalysisPlan.model_validate(state["plan"])
        completed = {
            step.step_id
            for step in plan.steps
            if step.status in {"completed", "skipped"}
        }
        step = next(
            (
                item
                for item in plan.steps
                if item.status == "pending"
                and set(item.depends_on).issubset(completed)
            ),
            None,
        )
        if step is not None:
            objective = step.objective.casefold()
            successful_tools = {
                item.tool_name for item in observations if item.status == "succeeded"
            }
            if "catalog" in objective and "catalog.inspect" not in successful_tools:
                return False
            if (
                any(token in objective for token in ("semantic", "binding", "logical field"))
                and "semantic.inspect" not in successful_tools
            ):
                return False
        has_prepared = any(
            item.kind.value == "prepared_query"
            for item in state.get("artifact_refs", ())
        )
        return not has_prepared and self._query_plan is None

    async def __call__(
        self,
        *,
        state,
        allowed_tools: Sequence[ToolSpec],
    ) -> PlannerDecision | None:
        plan = AnalysisPlan.model_validate(state["plan"])
        goal = AnalysisGoal.model_validate(state["goal"])
        authority = DatasetAuthority.model_validate(state["authority"])
        allowed = {spec.name for spec in allowed_tools}
        if (
            state.get("replan_requested")
            and plan.plan_id.startswith("metric-clarification-")
            and "Clarification response:" in goal.contextualized_question
        ):
            resumed = self._clarified_metric_plan(
                prior=plan,
                allowed=allowed,
                run_id=state["run_id"],
            )
            return self._act(
                resumed,
                resumed.steps[0],
                "semantic.inspect",
                {},
            )

        observations = tuple(state.get("observations", ()))
        if not observations or observations[-1].status != "succeeded":
            return None
        completed = {
            step.step_id
            for step in plan.steps
            if step.status in {"completed", "skipped"}
        }
        step = next(
            (
                item
                for item in plan.steps
                if item.status == "pending"
                and set(item.depends_on).issubset(completed)
            ),
            None,
        )
        successful_tools = {
            item.tool_name for item in observations if item.status == "succeeded"
        }
        objective = step.objective.casefold() if step is not None else ""

        if (
            step is not None
            and "catalog" in objective
            and "catalog.inspect" in allowed
            and "catalog.inspect" not in successful_tools
        ):
            return self._act(plan, step, "catalog.inspect", {})
        if (
            step is not None
            and any(token in objective for token in ("semantic", "binding", "logical field"))
            and "semantic.inspect" in allowed
            and "semantic.inspect" not in successful_tools
        ):
            return self._act(plan, step, "semantic.inspect", {})

        prepared = next(
            (
                item
                for item in state.get("artifact_refs", ())
                if item.kind.value == "prepared_query"
            ),
            None,
        )
        result = next(
            (
                item
                for item in reversed(tuple(state.get("artifact_refs", ())))
                if item.kind.value in {"query_preview", "query_result"}
            ),
            None,
        )

        if prepared is None:
            query_plan = await self._plan_query(goal)
            if query_plan.status != DatasetPlanStatus.READY:
                prompt = query_plan.clarification_question or (
                    "Please clarify the requested dataset calculation."
                )
                return PlannerDecision(
                    plan=plan,
                    decision="clarify",
                    clarification=AgentInputRequest(
                        interrupt_id=(
                            "query-clarification-"
                            + stable_digest({"run_id": state["run_id"], "prompt": prompt})[:16]
                        ),
                        reason=AgentInputReason.CLARIFICATION,
                        prompt=prompt,
                        allow_free_text=True,
                    ),
                    rationale_summary="The logical query needs explicit user clarification.",
                )
            refs = _dataset_plan_refs(query_plan)
            if (
                step is not None
                and any(token in objective for token in ("relationship", "route", "join path"))
                and "relationship.route" in allowed
                and "relationship.route" not in successful_tools
            ):
                return self._act(
                    plan,
                    step,
                    "relationship.route",
                    {"logical_refs": list(refs or ("analysis_result",))},
                )
            if "query.compile" in allowed and step is not None:
                return self._act(
                    plan,
                    step,
                    "query.compile",
                    {"plan": query_plan.model_dump(mode="json")},
                )

        if prepared is not None and result is None:
            run_tool = (
                "query.execute"
                if "query.execute" in allowed
                else "query.preview"
                if "query.preview" in allowed
                else None
            )
            if run_tool is not None and step is not None:
                return self._act(
                    plan,
                    step,
                    run_tool,
                    {"artifact_id": prepared.artifact_id, "preview_rows": 100},
                )
            if authority.mode.value == "plan":
                return self._finish_plan_mode(plan, goal=goal)
            return self._fail_missing_runtime_tool(plan, "query execution")

        if result is not None and not state.get("evidence_refs"):
            if "evidence.collect" in allowed:
                if step is None:
                    plan, step = self._append_evidence_step(plan)
                refs = _dataset_plan_refs(self._query_plan) if self._query_plan else ()
                if not refs and observations[-1].safe_preview:
                    refs = tuple(observations[-1].safe_preview[0])
                return self._act(
                    plan,
                    step,
                    "evidence.collect",
                    {
                        "artifact_id": result.artifact_id,
                        "claim_key": _dataset_claim_key(self._query_plan),
                        "field_refs": list(refs or ("analysis_result",)),
                    },
                )
            return self._fail_missing_runtime_tool(plan, "evidence collection")
        return None

    async def _plan_query(self, goal: AnalysisGoal) -> DatasetQueryPlan:
        if self._query_plan is None:
            result = await self._logical_planner.build_plan(
                question=goal.contextualized_question,
                binding=self._binding,  # type: ignore[arg-type]
                catalog=self._catalog,  # type: ignore[arg-type]
            )
            self._query_plan = result.plan
        return self._query_plan

    @staticmethod
    def _clarified_metric_plan(
        *,
        prior: AnalysisPlan,
        allowed: set[str],
        run_id: str,
    ) -> AnalysisPlan:
        steps = [
            AnalysisStep(
                step_id="inspect_semantic",
                objective="Inspect the active semantic binding for the clarified metric",
                status="pending",
                expected_evidence=("clarified semantic fields",),
            ),
            AnalysisStep(
                step_id="compile_query",
                objective="Compile a governed query for the clarified metric",
                status="pending",
                depends_on=("inspect_semantic",),
                expected_evidence=("prepared query artifact",),
            ),
        ]
        run_tool = (
            "query.execute"
            if "query.execute" in allowed
            else "query.preview"
            if "query.preview" in allowed
            else None
        )
        if run_tool is not None:
            steps.extend(
                (
                    AnalysisStep(
                        step_id="run_query",
                        objective=(
                            "Execute the clarified governed query"
                            if run_tool == "query.execute"
                            else "Preview the clarified governed query"
                        ),
                        status="pending",
                        depends_on=("compile_query",),
                        expected_evidence=("governed query result",),
                    ),
                    AnalysisStep(
                        step_id="bind_evidence",
                        objective="Bind the clarified result to verifiable evidence",
                        status="pending",
                        depends_on=("run_query",),
                        expected_evidence=("verified result evidence",),
                    ),
                )
            )
        return AnalysisPlan(
            plan_id="clarified-" + stable_digest({"run_id": run_id})[:20],
            revision=prior.revision + 1,
            steps=tuple(steps),
            completion_criteria=(
                "Answer the clarified question with governed evidence",
            ),
        )

    @staticmethod
    def _act(
        plan: AnalysisPlan,
        step,
        tool_name: str,
        arguments: dict[str, object],
    ) -> PlannerDecision:
        action_id = "action-" + stable_digest(
            {
                "plan_id": plan.plan_id,
                "revision": plan.revision,
                "step_id": step.step_id,
                "tool": tool_name,
                "arguments": arguments,
            }
        )[:24]
        return PlannerDecision(
            plan=plan,
            decision="act",
            next_action=AgentAction(
                action_id=action_id,
                tool_name=tool_name,
                arguments=arguments,
                purpose=step.objective,
                expected_evidence=step.expected_evidence,
            ),
            rationale_summary="Selected a schema-valid governed dataset action.",
        )

    @staticmethod
    def _finish_plan_mode(
        plan: AnalysisPlan,
        *,
        goal: AnalysisGoal,
    ) -> PlannerDecision:
        completed = plan.model_copy(
            update={
                "revision": plan.revision + 1,
                "steps": tuple(
                    step.model_copy(update={"status": "skipped"})
                    if step.status == "pending"
                    else step
                    for step in plan.steps
                ),
            }
        )
        chinese = bool(re.search(r"[\u3400-\u9fff]", goal.contextualized_question))
        return PlannerDecision(
            plan=completed,
            decision="finish",
            completion_summary=(
                "受治理查询计划已就绪；规划模式未执行或预览任何数据。"
                if chinese
                else (
                    "The governed query plan is ready; plan mode did not execute "
                    "or preview any data."
                )
            ),
            rationale_summary=(
                "规划模式在受治理查询编译完成后停止，未读取数据。"
                if chinese
                else "Plan mode stops after compiling the governed query without reading data."
            ),
        )

    @staticmethod
    def _append_evidence_step(
        plan: AnalysisPlan,
    ) -> tuple[AnalysisPlan, AnalysisStep]:
        step_ids = {step.step_id for step in plan.steps}
        base_id = "bind_evidence"
        step_id = base_id
        suffix = 2
        while step_id in step_ids:
            step_id = f"{base_id}_{suffix}"
            suffix += 1
        dependencies = tuple(
            step.step_id for step in plan.steps if step.status != "skipped"
        )
        evidence_step = AnalysisStep(
            step_id=step_id,
            objective="Bind the governed query result to verifiable evidence",
            status="pending",
            depends_on=dependencies,
            expected_evidence=("validated result evidence",),
        )
        revised = plan.model_copy(
            update={
                "revision": plan.revision + 1,
                "steps": (*plan.steps, evidence_step),
                "completion_criteria": tuple(
                    dict.fromkeys(
                        (*plan.completion_criteria, "Every result claim has validated evidence")
                    )
                ),
            }
        )
        return revised, evidence_step

    @staticmethod
    def _fail_missing_runtime_tool(
        plan: AnalysisPlan,
        capability: str,
    ) -> PlannerDecision:
        return PlannerDecision(
            plan=plan,
            decision="fail",
            rationale_summary=f"The active runtime does not allow required {capability}.",
        )


def _dataset_plan_refs(plan: DatasetQueryPlan | None) -> tuple[str, ...]:
    if plan is None or plan.status != DatasetPlanStatus.READY:
        return ()
    aliases = {item.alias for item in plan.aggregations}
    refs = dict.fromkeys(
        (
            *plan.select,
            *(item.ref for item in plan.aggregations),
            *plan.group_by,
            *(item.ref for item in plan.filters),
            *(item.ref for item in plan.order_by),
        )
    )
    return tuple(ref for ref in refs if ref not in aliases)


def _dataset_claim_key(plan: DatasetQueryPlan | None) -> str:
    if plan is not None and plan.aggregations:
        return plan.aggregations[0].alias
    return "analysis_result"


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
            "next_action_resolver": _DatasetNextActionResolver(
                model_client=self._model_client,
                binding=binding,
                catalog=catalog,
            ),
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
                terminal_state = dict(state)
                error = terminal_state.get("error")
                try:
                    status = AgentStatus(terminal_state.get("status", AgentStatus.RUNNING))
                except (TypeError, ValueError):
                    status = AgentStatus.FAILED if error is not None else AgentStatus.COMPLETED
                ok = error is None and status not in {
                    AgentStatus.FAILED,
                    AgentStatus.CANCELLED,
                }
                if ok:
                    status = AgentStatus.COMPLETED
                elif getattr(error, "code", None) == ErrorCode.CANCELLED:
                    status = AgentStatus.CANCELLED
                else:
                    status = AgentStatus.FAILED
                terminal_state["status"] = status
                response = response_builder(terminal_state, ok)
                if inspect.isawaitable(response):
                    response = await response
                persisted = self._persist_turn(terminal_state, response)
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
    message_type = (
        "error"
        if not ok
        else "chart"
        if chart is not None and rows
        else "table"
        if rows
        else "analysis"
    )
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
        message_type=message_type,
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
