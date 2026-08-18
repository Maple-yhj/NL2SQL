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
    DatasetPlanStatus,
    DatasetQueryCompiler,
    DatasetQueryExecutor,
    DatasetQueryPlan,
    DatasetQueryProgram,
    DatasetQueryProgramPlanner,
    DatasetQueryStage,
    DatasetUnionStage,
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
from data_agent.semantic_metrics import (
    DomainPackRegistry,
    EffectiveMetricCatalog,
    LegacyMetricAdapter,
    MetricCatalogEntry,
    MetricCatalogOrigin,
    MetricProposal,
    SemanticMetricServiceError,
)

from .checkpoints import (
    CheckpointerFactory,
    CheckpointerResource,
    InMemoryCheckpointerFactory,
    SQLiteCheckpointerFactory,
)
from .evaluator import AnalysisEvaluator
from .graph import build_analysis_agent_graph, build_dataset_version_pins
from .guard import AgentGuardError
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

    async def resolve_metric_context(self, **kwargs: object) -> object: ...

    async def discover_metric_proposal(self, **kwargs: object) -> object: ...


ConversationSummaryLoader = Callable[
    [AgentRequest, PrincipalContext],
    str | None | Awaitable[str | None],
]
MetricProposalDiscovery = Callable[[str], Awaitable[MetricProposal]]


async def _no_conversation_summary(
    request: AgentRequest,
    principal: PrincipalContext,
) -> str | None:
    del request, principal
    return None


class _DatasetNextActionResolver:
    """Translate a high-level Agent plan into schema-valid dataset tool actions."""

    def __init__(
        self,
        *,
        model_client: object,
        binding: object,
        catalog: object,
        metric_catalog: EffectiveMetricCatalog | None = None,
        domain_id: str = "dataset",
        domain_packs: DomainPackRegistry | None = None,
        metric_proposal_discovery: MetricProposalDiscovery | None = None,
    ) -> None:
        self._logical_planner = DatasetQueryProgramPlanner(model_client)  # type: ignore[arg-type]
        self._binding = binding
        self._catalog = catalog
        self._metric_catalog = metric_catalog or (
            _legacy_metric_catalog(binding)
            if hasattr(binding, "metrics")
            else EffectiveMetricCatalog.build()
        )
        self._domain_id = domain_id
        self._domain_packs = domain_packs or DomainPackRegistry()
        self._metric_proposal_discovery = metric_proposal_discovery
        self._metric_proposal: MetricProposal | None = None
        self._query_plan: DatasetQueryPlan | DatasetQueryProgram | None = None

    def initial_decision(
        self,
        *,
        state: object,
        allowed_tools: Sequence[ToolSpec],
    ) -> PlannerDecision:
        if not isinstance(state, dict):
            raise ValueError("dataset initial state must be a mapping")
        allowed = {spec.name for spec in allowed_tools}
        if "catalog.inspect" not in allowed:
            plan = AnalysisPlan(
                plan_id="dataset-" + stable_digest({"run_id": state["run_id"]})[:20],
                revision=1,
                steps=(
                    AnalysisStep(
                        step_id="inspect_catalog",
                        objective="Inspect the pinned dataset catalog",
                        status="pending",
                        expected_evidence=("catalog artifact",),
                    ),
                ),
                completion_criteria=("Answer with validated governed evidence",),
            )
            return self._fail_missing_runtime_tool(plan, "catalog inspection")
        objective, expected_evidence = _canonical_tool_step("catalog.inspect")
        step = AnalysisStep(
            step_id="inspect_catalog",
            objective=objective,
            status="pending",
            expected_evidence=expected_evidence,
        )
        plan = AnalysisPlan(
            plan_id="dataset-" + stable_digest({"run_id": state["run_id"]})[:20],
            revision=1,
            steps=(step,),
            completion_criteria=("Answer with validated governed evidence",),
        )
        return self._act(plan, step, "catalog.inspect", {})

    def requires_model_call(self, *, state: object) -> bool:
        if not isinstance(state, dict):
            return False
        turns = tuple(state.get("clarification_turns", ()))
        if (
            state.get("replan_requested")
            and turns
            and turns[-1].origin == "dataset_query"
        ):
            return True
        observations = tuple(state.get("observations", ()))
        if not observations or observations[-1].status != "succeeded":
            return False
        goal = AnalysisGoal.model_validate(state["goal"])
        if _needs_semantic_evidence(goal.original_question):
            semantic_artifact_ids = {
                artifact.artifact_id
                for observation in observations
                if observation.status == "succeeded"
                and observation.tool_name == "semantic.inspect"
                for artifact in observation.artifact_refs
            }
            evidenced_artifact_ids = {
                item.artifact_id for item in state.get("evidence_refs", ())
            }
            if (
                not semantic_artifact_ids
                or not semantic_artifact_ids.issubset(evidenced_artifact_ids)
                or _is_semantic_only_question(goal.original_question)
            ):
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
        turns = tuple(state.get("clarification_turns", ()))
        dataset_query_resume = bool(
            state.get("replan_requested")
            and turns
            and turns[-1].origin == "dataset_query"
        )
        if dataset_query_resume:
            self._query_plan = None

        observations = tuple(state.get("observations", ()))
        if (
            (not observations or observations[-1].status != "succeeded")
            and not dataset_query_resume
        ):
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
        semantic_artifact = next(
            (
                artifact
                for observation in reversed(observations)
                if observation.status == "succeeded"
                and observation.tool_name == "semantic.inspect"
                for artifact in observation.artifact_refs
            ),
            None,
        )
        semantic_evidence_needed = _needs_semantic_evidence(goal.original_question)
        semantic_only = _is_semantic_only_question(goal.original_question)
        evidenced_artifact_ids = {
            item.artifact_id for item in state.get("evidence_refs", ())
        }

        if prepared is None and semantic_evidence_needed and semantic_artifact is None:
            if "semantic.inspect" not in allowed:
                return self._fail_missing_runtime_tool(plan, "semantic inspection")
            if step is None:
                plan, step = self._append_tool_step(plan, "semantic.inspect")
            return self._act(plan, step, "semantic.inspect", {})

        if (
            prepared is None
            and semantic_evidence_needed
            and semantic_artifact is not None
            and semantic_artifact.artifact_id not in evidenced_artifact_ids
        ):
            if "evidence.collect" not in allowed:
                return self._fail_missing_runtime_tool(plan, "metadata evidence collection")
            if step is None:
                plan, step = self._append_tool_step(plan, "evidence.collect")
            return self._act(
                plan,
                step,
                "evidence.collect",
                {
                    "artifact_id": semantic_artifact.artifact_id,
                    "claim_key": "semantic_definition",
                    "field_refs": list(
                        _semantic_field_refs_for_question(
                            goal.original_question,
                            self._binding,
                        )
                    ),
                },
            )

        if (
            prepared is None
            and semantic_only
            and semantic_artifact is not None
            and semantic_artifact.artifact_id in evidenced_artifact_ids
        ):
            return self._finish_evidence_mode(plan, goal=goal)

        if prepared is None:
            query_plan = await self._plan_query(
                goal,
                clarification_turns=turns,
            )
            if query_plan.status != DatasetPlanStatus.READY:
                prompt = query_plan.clarification_question or (
                    "Please clarify the requested dataset calculation."
                )
                if query_plan.status == DatasetPlanStatus.UNSUPPORTED:
                    raise AgentGuardError(
                        ErrorCode.QUERY_UNSUPPORTED,
                        prompt,
                    )
                proposal = await self._discover_unresolved_metric(
                    goal.original_question
                )
                if proposal is not None:
                    candidate_lines = "\n".join(
                        f"- {item.label}: {item.rationale}"
                        for item in proposal.candidates
                    )
                    decisions = tuple(
                        dict.fromkeys(
                            decision
                            for item in proposal.candidates
                            for decision in item.required_decisions
                        )
                    )
                    prompt = (
                        f"已自动创建指标口径草案 {proposal.proposal_id}，但不会在本次"
                        "运行中静默激活。请在数据源的指标治理面板确认候选、完成验证"
                        "并由管理员发布，然后重新发起查询。\n"
                        f"候选口径：\n{candidate_lines}\n"
                        f"待确认：{'、'.join(decisions) or '无'}"
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
                        origin="dataset_query",
                        prompt=prompt,
                        allow_free_text=True,
                    ),
                    rationale_summary="The logical query needs explicit user clarification.",
                )
            # query.compile is the single source of truth for per-stage relationship
            # validation. A separate, global relationship route can both disagree
            # with the compiler and incorrectly combine independent stage roots.
            if "query.compile" in allowed:
                if step is None:
                    plan, step = self._append_tool_step(plan, "query.compile")
                return self._act(
                    plan,
                    step,
                    "query.compile",
                    {"plan": query_plan.model_dump(mode="json")},
                )
            return self._fail_missing_runtime_tool(plan, "query compilation")

        if prepared is not None and result is None:
            run_tool = (
                "query.execute"
                if "query.execute" in allowed
                else "query.preview"
                if "query.preview" in allowed
                else None
            )
            if run_tool is not None:
                if step is None:
                    plan, step = self._append_tool_step(plan, run_tool)
                return self._act(
                    plan,
                    step,
                    run_tool,
                    {"artifact_id": prepared.artifact_id, "preview_rows": 100},
                )
            if authority.mode.value == "plan":
                return self._finish_plan_mode(plan, goal=goal)
            return self._fail_missing_runtime_tool(plan, "query execution")

        if result is not None and result.artifact_id not in evidenced_artifact_ids:
            if "evidence.collect" in allowed:
                if step is None:
                    plan, step = self._append_tool_step(plan, "evidence.collect")
                refs = _dataset_output_refs(self._query_plan)
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
        if result is not None and result.artifact_id in evidenced_artifact_ids:
            return self._finish_evidence_mode(plan, goal=goal)
        return None

    async def _discover_unresolved_metric(
        self,
        question: str,
    ) -> MetricProposal | None:
        """Ground a same-domain unknown term into a draft, never an activation."""

        if self._metric_proposal is not None:
            return self._metric_proposal
        active_refs = {
            entry.definition.metric_ref for entry in self._metric_catalog.entries
        }
        unresolved = next(
            (
                match
                for match in self._domain_packs.detect_templates(
                    question,
                    domain_id=self._domain_id,
                )
                if match[1].metric_ref not in active_refs
            ),
            None,
        )
        if unresolved is None or self._metric_proposal_discovery is None:
            return None
        _, _, matched_term = unresolved
        try:
            self._metric_proposal = await self._metric_proposal_discovery(matched_term)
        except SemanticMetricServiceError:
            # Discovery is an enhancement to fail-closed clarification. A pack
            # that cannot ground to this schema must not turn clarification
            # into a runtime failure or substitute another metric.
            return None
        return self._metric_proposal

    async def _plan_query(
        self,
        goal: AnalysisGoal,
        *,
        clarification_turns=(),
    ) -> DatasetQueryPlan | DatasetQueryProgram:
        if self._query_plan is None:
            result = await self._logical_planner.build_program(
                # Query compilation should be driven by the current request.
                # Conversation summaries can contain large prior answers or
                # diagnostics and are neither schema evidence nor an explicit
                # clarification. Dataset-query clarification turns are passed
                # separately below, so this remains generic across datasets.
                question=goal.original_question,
                policy_question=goal.original_question,
                binding=self._binding,  # type: ignore[arg-type]
                catalog=self._catalog,  # type: ignore[arg-type]
                clarification_history=tuple(
                    {
                        "prompt": item.prompt,
                        "response": item.response,
                    }
                    for item in clarification_turns
                    if item.origin == "dataset_query"
                ),
                metric_catalog=self._metric_catalog,
                domain_packs=self._domain_packs,
            )
            self._query_plan = result.program
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
        objective, expected_evidence = _canonical_tool_step(tool_name)
        if (
            step.objective != objective
            or step.expected_evidence != expected_evidence
        ):
            step = step.model_copy(
                update={
                    "objective": objective,
                    "expected_evidence": expected_evidence,
                }
            )
            plan = plan.model_copy(
                update={
                    "revision": plan.revision + 1,
                    "steps": tuple(
                        step if item.step_id == step.step_id else item
                        for item in plan.steps
                    ),
                }
            )
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
    def _finish_evidence_mode(
        plan: AnalysisPlan,
        *,
        goal: AnalysisGoal,
    ) -> PlannerDecision:
        completed = plan.model_copy(
            update={
                "revision": plan.revision + 1,
                "steps": tuple(
                    step.model_copy(update={"status": "skipped"})
                    if step.status in {"pending", "blocked"}
                    else step
                    for step in plan.steps
                ),
            }
        )
        chinese = bool(re.search(r"[\u3400-\u9fff]", goal.original_question))
        return PlannerDecision(
            plan=completed,
            decision="finish",
            completion_summary=(
                "所需受治理证据已收集完成。"
                if chinese
                else "The required governed evidence has been collected."
            ),
            rationale_summary=(
                "结果工件已绑定为可验证证据，剩余描述性步骤无需再次执行。"
                if chinese
                else (
                    "The result artifact is bound to verifiable evidence; remaining "
                    "descriptive steps do not require duplicate execution."
                )
            ),
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
        return _DatasetNextActionResolver._append_tool_step(
            plan,
            "evidence.collect",
        )

    @staticmethod
    def _append_tool_step(
        plan: AnalysisPlan,
        tool_name: str,
    ) -> tuple[AnalysisPlan, AnalysisStep]:
        step_ids = {step.step_id for step in plan.steps}
        base_id = {
            "semantic.inspect": "inspect_semantic",
            "query.compile": "compile_query",
            "query.execute": "run_query",
            "query.preview": "preview_query",
            "evidence.collect": "bind_evidence",
        }.get(tool_name, tool_name.replace(".", "_"))
        step_id = base_id
        suffix = 2
        while step_id in step_ids:
            step_id = f"{base_id}_{suffix}"
            suffix += 1
        dependencies = tuple(
            step.step_id
            for step in plan.steps
            if step.status in {"completed", "skipped"}
        )
        objective, expected_evidence = _canonical_tool_step(tool_name)
        evidence_step = AnalysisStep(
            step_id=step_id,
            objective=objective,
            status="pending",
            depends_on=dependencies,
            expected_evidence=expected_evidence,
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


def _dataset_plan_refs(
    plan: DatasetQueryPlan | DatasetQueryProgram | None,
) -> tuple[str, ...]:
    if plan is None or plan.status != DatasetPlanStatus.READY:
        return ()
    if isinstance(plan, DatasetQueryProgram):
        refs: list[str] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if value.get("kind") == "field" and isinstance(value.get("ref"), str):
                    refs.append(value["ref"])
                anchor = value.get("anchor_ref")
                if isinstance(anchor, str):
                    refs.append(anchor)
                for item in value.values():
                    visit(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)

        visit(plan.model_dump(mode="python"))
        return tuple(dict.fromkeys(refs))
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


def _dataset_claim_key(
    plan: DatasetQueryPlan | DatasetQueryProgram | None,
) -> str:
    outputs = _dataset_output_refs(plan)
    return outputs[0] if len(outputs) == 1 else "analysis_result"


def _dataset_output_refs(
    plan: DatasetQueryPlan | DatasetQueryProgram | None,
) -> tuple[str, ...]:
    if plan is None or plan.status != DatasetPlanStatus.READY:
        return ()
    if isinstance(plan, DatasetQueryProgram) and plan.output_stage_id is not None:
        stages = {item.stage_id: item for item in plan.stages}
        output = stages.get(plan.output_stage_id)
        if isinstance(output, DatasetUnionStage) and output.input_stage_ids:
            output = stages.get(output.input_stage_ids[0])
        if isinstance(output, DatasetQueryStage):
            return tuple(item.alias for item in output.projections)
        return ()
    if isinstance(plan, DatasetQueryPlan):
        return tuple(
            dict.fromkeys(
                (
                    *plan.select,
                    *plan.group_by,
                    *(item.alias for item in plan.aggregations),
                )
            )
        )
    return ()


def _dataset_routing_refs(
    plan: DatasetQueryPlan | DatasetQueryProgram | None,
    binding: object,
) -> tuple[str, ...]:
    refs = list(_dataset_plan_refs(plan))
    if not isinstance(plan, DatasetQueryProgram):
        return tuple(refs)
    metric_refs: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("kind") == "metric" and isinstance(value.get("ref"), str):
                metric_refs.append(value["ref"])
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(plan.model_dump(mode="python"))
    metrics = {item.metric_ref: item for item in getattr(binding, "metrics", ())}
    refs.extend(
        metrics[ref].field_ref
        for ref in metric_refs
        if ref in metrics and metrics[ref].field_ref is not None
    )
    return tuple(dict.fromkeys(refs))


def _is_semantic_metadata_question(question: str) -> bool:
    return bool(
        re.search(
            r"(?:字段|列|指标|状态|取值).{0,12}(?:含义|定义|口径|语义|是什么|表示)"
            r"|(?:含义|定义|口径|语义).{0,12}(?:字段|列|指标|状态|取值)"
            r"|\b(?:meaning|definition|semantics?|metadata|describe)\b",
            question,
            flags=re.IGNORECASE,
        )
    )


def _is_semantic_capability_question(question: str) -> bool:
    return bool(
        re.search(
            r"(?:能否|能不能|是否能|可以|可否).{0,12}(?:回答|计算|得到|判断)"
            r"|(?:回答|计算|得到).{0,12}(?:吗|么|？|\?)"
            r"|\bcan\b.{0,24}\b(?:answer|calculate|determine|derive)\b",
            question,
            flags=re.IGNORECASE,
        )
    )


def _is_field_lifecycle_question(question: str) -> bool:
    return bool(
        re.search(
            r"(?:数据泄漏|特征泄漏|目标泄漏|下单时|预测时|当时可用|可用字段|生命周期)"
            r"|(?:字段|特征).{0,16}(?:泄漏|可用|不可用|预测)"
            r"|\b(?:data|target|feature) leakage\b"
            r"|\b(?:available|known) at (?:prediction|scoring|order) time\b"
            r"|\bfield lifecycle\b",
            question,
            flags=re.IGNORECASE,
        )
    )


def _is_semantic_only_question(question: str) -> bool:
    if _is_semantic_capability_question(question) or _is_field_lifecycle_question(question):
        return True
    return _is_semantic_metadata_question(question) and not _is_quantitative_request(question)


def _needs_semantic_evidence(question: str) -> bool:
    return bool(
        _is_semantic_metadata_question(question)
        or _is_semantic_capability_question(question)
        or _is_field_lifecycle_question(question)
    )


def _is_quantitative_request(question: str) -> bool:
    return bool(
        re.search(
            r"(?:多少|几(?:个|条|行)|数量|计数|统计|占比|比例|平均|均值|总数|最大|最小|唯一值)"
            r"|\b(?:how many|count|average|mean|total|ratio|rate|maximum|minimum|distinct)\b",
            question,
            flags=re.IGNORECASE,
        )
    )


def _canonical_tool_step(tool_name: str) -> tuple[str, tuple[str, ...]]:
    return {
        "catalog.inspect": (
            "Inspect the pinned dataset catalog",
            ("catalog artifact",),
        ),
        "semantic.inspect": (
            "Inspect the pinned semantic binding",
            ("semantic definition artifact",),
        ),
        "query.compile": (
            "Compile the governed query program",
            ("prepared query artifact",),
        ),
        "query.execute": (
            "Execute the compiled governed query",
            ("governed query result",),
        ),
        "query.preview": (
            "Preview the compiled governed query",
            ("governed query preview",),
        ),
        "evidence.collect": (
            "Bind the governed result artifact to verifiable evidence",
            ("validated result evidence",),
        ),
    }.get(tool_name, (f"Run governed tool {tool_name}", ("governed tool output",)))


def _semantic_field_refs_for_question(question: str, binding: object) -> tuple[str, ...]:
    lowered = question.casefold()
    matched: list[str] = []
    mappings = tuple(getattr(binding, "mappings", ()))
    for mapping in mappings:
        candidates = (
            mapping.logical_ref,
            mapping.logical_ref.rsplit(".", 1)[-1],
            mapping.display_name,
            *mapping.synonyms,
        )
        if any(
            candidate and str(candidate).casefold() in lowered
            for candidate in candidates
        ):
            matched.append(mapping.logical_ref)
    return tuple(matched or [item.logical_ref for item in mappings[:50]])


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
            resolve_metrics = getattr(
                self._data_sources, "resolve_metric_context", None
            )
            metric_execution = (
                await resolve_metrics(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    conversation_id=request.conversation_id,
                    run_id=run_id,
                    source_id=request.source_id,
                    domain_id=request.domain_id,
                    binding=execution.binding,
                )
                if callable(resolve_metrics)
                else None
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
        metric_catalog = (
            metric_execution.catalog
            if metric_execution is not None
            else _legacy_metric_catalog(binding)
        )
        metric_context = (
            metric_execution.context if metric_execution is not None else None
        )
        relations = tuple(item.relation for item in catalog.relations)
        authority = DatasetAuthority(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            source_id=request.source_id,
            source_version=request.source_version,
            binding_id=request.binding_id,
            binding_version=request.binding_version,
            metric_set_id=(
                metric_context.metric_set.metric_set_id
                if metric_context is not None and metric_context.metric_set is not None
                else None
            ),
            metric_set_version=(
                metric_context.metric_set.version
                if metric_context is not None and metric_context.metric_set is not None
                else None
            ),
            metric_set_digest=(
                metric_context.metric_set.digest.removeprefix("sha256:")
                if metric_context is not None and metric_context.metric_set is not None
                else None
            ),
            metric_overlay_id=(
                metric_context.overlay_id if metric_context is not None else None
            ),
            metric_overlay_digest=(
                metric_context.overlay_digest.removeprefix("sha256:")
                if metric_context is not None
                and metric_context.overlay_digest is not None
                else None
            ),
            schema_fingerprint=snapshot.fingerprint,
            allowed_relation_ids=relations,
            mode=request.mode,
        )
        bundle_digest = hashlib.sha256(
            (
                f"{authority.source_id}:{authority.source_version}:"
                f"{authority.binding_id}:{authority.binding_version}"
                f":{metric_catalog.digest}"
            ).encode("utf-8")
        ).hexdigest()
        tool_runtime = DatasetToolRuntime(
            authority=authority,
            catalog=catalog,
            binding=binding,
            metric_catalog=metric_catalog,
            connector=execution.connector,
            connection_ref=execution.connection_ref,
            bundle_digest=bundle_digest,
            artifacts=self._artifacts,
            compiler=DatasetQueryCompiler(),
            executor=DatasetQueryExecutor(),
        )
        registry = build_dataset_tool_registry()
        domain_pack_identity = (
            metric_execution.domain_pack if metric_execution is not None else None
        )
        installed_domain_packs = DomainPackRegistry()
        domain_manifest = (
            installed_domain_packs.get(
                domain_pack_identity.pack_id,
                domain_pack_identity.version,
            )
            if domain_pack_identity is not None
            else None
        )
        if (
            domain_pack_identity is not None
            and (
                domain_manifest is None
                or domain_manifest.digest != domain_pack_identity.digest
            )
        ):
            raise AnalysisRuntimeError(
                AgentError(
                    code=ErrorCode.BINDING_STALE,
                    message="assigned domain pack is unavailable or stale",
                    retryable=True,
                )
            )
        selected_domain_packs = DomainPackRegistry(
            (domain_manifest,) if domain_manifest is not None else ()
        )
        invoker = ToolInvoker(
            registry,
            credential_broker=DatasetCredentialBroker(tool_runtime),
        )
        tool_executor = DatasetAgentToolInvoker(
            registry=registry,
            invoker=invoker,
            principal=principal,
            runtime_resources=tool_runtime,
            skill_id=(
                domain_manifest.analysis_skill_id
                if domain_manifest is not None
                else "dataset.analytics"
            ),
            skill_version=(
                domain_manifest.analysis_skill_version
                if domain_manifest is not None
                else "1.0.0"
            ),
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
            metric_catalog_digest=metric_catalog.digest.removeprefix("sha256:"),
            catalog_summary=_catalog_summary(catalog),
            semantic_summary=_semantic_summary(binding, metric_catalog),
            conversation_summary=conversation,
            allowed_tool_names=allowed_names,
        )

        async def load_context(_state):
            return snapshot_context

        async def discover_metric_proposal(requested_term: str) -> MetricProposal:
            discover = getattr(
                self._data_sources,
                "discover_metric_proposal",
                None,
            )
            if not callable(discover):
                raise RuntimeError("metric proposal discovery is unavailable")
            proposal = await discover(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                roles=principal.roles,
                source_id=request.source_id,
                domain_id=request.domain_id,
                requested_term=requested_term,
            )
            return MetricProposal.model_validate(proposal)

        semantic_features = getattr(
            self._data_sources,
            "semantic_metric_features",
            None,
        )
        discovery_callback = (
            discover_metric_proposal
            if semantic_features is None
            or bool(getattr(semantic_features, "domain_pack_discovery", False))
            or bool(getattr(semantic_features, "web_discovery", False))
            else None
        )

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
            analysis_skill_id=(
                domain_manifest.analysis_skill_id
                if domain_manifest is not None
                else "dataset.analytics"
            ),
            analysis_skill_version=(
                domain_manifest.analysis_skill_version
                if domain_manifest is not None
                else "1.0.0"
            ),
            domain_pack_id=(
                domain_manifest.pack_id if domain_manifest is not None else None
            ),
            domain_pack_version=(
                domain_manifest.version if domain_manifest is not None else None
            ),
            domain_pack_digest=(
                domain_manifest.digest.removeprefix("sha256:")
                if domain_manifest is not None
                else None
            ),
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
                metric_catalog=metric_catalog,
                domain_id=request.domain_id,
                domain_packs=selected_domain_packs,
                metric_proposal_discovery=discovery_callback,
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


def _semantic_summary(
    binding,
    metric_catalog: EffectiveMetricCatalog,
) -> dict[str, object]:
    return {
        "bindingId": binding.binding_id,
        "bindingVersion": binding.version,
        "logicalFields": [
            {
                "ref": mapping.logical_ref,
                "displayName": mapping.display_name,
                "description": mapping.description,
                "semanticRole": mapping.semantic_role,
                "entity": mapping.entity,
                "grain": mapping.grain,
                "unit": mapping.unit,
                "lifecycleStage": mapping.lifecycle_stage,
                "synonyms": list(mapping.synonyms),
            }
            for mapping in binding.mappings[:400]
        ],
        "metrics": [
            {
                "ref": metric.metric_ref,
                "displayName": metric.display_name,
                "description": metric.description,
                "formula": metric.formula.model_dump(mode="json"),
                "defaultFilter": (
                    metric.default_filter.model_dump(mode="json")
                    if metric.default_filter is not None
                    else None
                ),
                "defaultTimeRef": metric.default_time_ref,
                "unit": metric.unit,
                "grain": metric.grain,
                "currency": metric.currency,
                "synonyms": list(metric.synonyms),
            }
            for metric in tuple(
                item.definition for item in metric_catalog.entries
            )[:200]
        ],
        "relationshipCount": (
            len(binding.graph.edges)
            if isinstance(binding, SemanticGraphBindingRecord)
            else len(binding.relationships)
        ),
    }


def _legacy_metric_catalog(binding) -> EffectiveMetricCatalog:
    return EffectiveMetricCatalog.build(
        legacy=tuple(
            MetricCatalogEntry.create(
                definition=LegacyMetricAdapter.to_v2(metric),
                origin=MetricCatalogOrigin.LEGACY,
                authority_ref=f"embedded-v1:{binding.binding_id}:{binding.version}",
            )
            for metric in binding.metrics
        )
    )


async def _build_dataset_response(
    *,
    artifacts: SQLiteArtifactStore,
    state,
    pins,
    ok: bool,
) -> AgentResponse:
    prepared: PreparedQuery | None = None
    dataset_plan: DatasetQueryPlan | DatasetQueryProgram | None = None
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
