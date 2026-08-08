"""Small state-plus-runtime nodes for the native LangGraph analysis loop."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from langgraph.errors import GraphInterrupt

from data_agent.public_contracts import AgentError, ErrorCode
from data_agent.runtime.events import (
    AnswerSynthesizingPayload,
    ContextResolvedPayload,
    ObservationRecordedPayload,
    PlanUpdatedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunResumedPayload,
    RunStartedPayload,
    StepStartedPayload,
    ToolCompletedPayload,
    ToolStartedPayload,
)
from data_agent.runtime.models import (
    AgentArtifactSummary,
    AgentResponse,
    AnalysisStepSummary,
    DatasetRuntimeVersionPins,
    EvidenceSummary,
    PrincipalContext,
)
from data_agent.tools.models import (
    ToolBudget,
    ToolCall,
    ToolErrorCode,
    ToolInvocationContext,
    ToolSpec,
)
from data_agent.tools.invoker import ToolInvoker
from data_agent.tools.registry import ToolRegistry

from .evaluator import AnalysisEvaluator
from .guard import (
    AgentGuardError,
    consume_budget,
    ensure_node_entry,
    guard_planner_decision,
    tool_budget_counters,
)
from .models import (
    AgentAnswerDraft,
    AgentArtifactRef,
    AgentBudgetState,
    AgentContextSnapshot,
    AgentInputRequest,
    AgentObservation,
    AgentRunBudget,
    AgentStatus,
    AnalysisGoal,
    AnalysisPlan,
    AnalysisStep,
    DatasetAuthority,
    EvaluationDecision,
    FindingDraft,
    PlannerDecision,
    stable_digest,
)
from .planner import AnalysisPlanner
from .routing import AgentRoute
from .state import AnalysisAgentState
from .synthesizer import AnalysisSynthesizer


class PlannerPort(Protocol):
    async def decide(self, **kwargs: object) -> PlannerDecision: ...


class EvaluatorPort(Protocol):
    async def evaluate(self, **kwargs: object) -> EvaluationDecision: ...


class SynthesizerPort(Protocol):
    async def synthesize(self, **kwargs: object) -> AgentAnswerDraft: ...


class AgentToolExecutor(Protocol):
    async def invoke(
        self,
        *,
        call_id: str,
        action: object,
        state: AnalysisAgentState,
    ) -> AgentObservation: ...


@dataclass(frozen=True, slots=True)
class DatasetAgentToolInvoker:
    registry: ToolRegistry
    invoker: ToolInvoker
    principal: PrincipalContext
    runtime_resources: object
    skill_id: str = "dataset.analytics"
    skill_version: str = "1.0.0"
    max_rows: int = 1000
    statement_timeout_ms: int = 15_000

    async def invoke(
        self,
        *,
        call_id: str,
        action: object,
        state: AnalysisAgentState,
    ) -> AgentObservation:
        action_id = str(getattr(action, "action_id"))
        tool_name = str(getattr(action, "tool_name"))
        arguments = getattr(action, "arguments")
        spec = self.registry.get(tool_name)
        if spec is None:
            raise AgentGuardError(
                ErrorCode.AGENT_ACTION_NOT_ALLOWED,
                "guarded dataset tool is no longer registered",
            )
        typed_input = spec.input_schema.model_validate(arguments)
        invocation = ToolInvocationContext(
            principal=self.principal,
            skill_id=self.skill_id,
            skill_version=self.skill_version,
            allowed_tools=state["context"].allowed_tool_names,
            budget=ToolBudget(max_calls=1),
            authority=state["authority"],
            mode=state["authority"].mode,
            runtime_resources=self.runtime_resources,
            max_rows=self.max_rows,
            statement_timeout_ms=self.statement_timeout_ms,
            run_id=state["run_id"],
        )
        result = await self.invoker.invoke(
            ToolCall(
                call_id=call_id,
                tool_name=tool_name,
                tool_version=spec.version,
                input_data=typed_input,
                idempotency_key=(call_id if spec.idempotency == "required" else None),
            ),
            invocation,
        )
        observation_id = "observation-" + stable_digest(
            {"run_id": state["run_id"], "call_id": call_id}
        )
        if result.status == "error":
            assert result.structured_error is not None
            return AgentObservation(
                observation_id=observation_id,
                action_id=action_id,
                tool_name=tool_name,
                status="failed",
                summary="Governed tool invocation failed.",
                error=AgentError(
                    code=_public_tool_error(result.structured_error.code),
                    message=result.structured_error.message,
                    retryable=result.structured_error.retryable,
                ),
            )
        typed = result.typed_data
        assert typed is not None
        artifact = getattr(typed, "artifact", None)
        evidence = getattr(typed, "evidence", None)
        artifacts = (artifact,) if artifact is not None else ()
        evidence_refs = (evidence,) if evidence is not None else ()
        return AgentObservation(
            observation_id=observation_id,
            action_id=action_id,
            tool_name=tool_name,
            status="succeeded",
            summary=str(getattr(typed, "summary", "Governed tool completed.")),
            artifact_refs=artifacts,
            evidence_refs=evidence_refs,
            safe_preview=_observation_preview(getattr(typed, "safe_preview", None)),
        )


ContextLoader = Callable[
    [AnalysisAgentState],
    AgentContextSnapshot | Awaitable[AgentContextSnapshot],
]
PersistenceHook = Callable[[AnalysisAgentState], None | Awaitable[None]]
ResponseBuilder = Callable[
    [AnalysisAgentState, bool],
    AgentResponse | Awaitable[AgentResponse],
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _not_cancelled() -> bool:
    return False


async def _no_persistence(state: AnalysisAgentState) -> None:
    del state


@dataclass(frozen=True, slots=True)
class AnalysisGraphContext:
    planner: PlannerPort
    evaluator: EvaluatorPort
    synthesizer: SynthesizerPort
    tool_executor: AgentToolExecutor
    context_loader: ContextLoader
    tool_specs: tuple[ToolSpec, ...]
    version_pins: DatasetRuntimeVersionPins
    budget_limits: AgentRunBudget = field(default_factory=AgentRunBudget)
    clock: Callable[[], datetime] = _utc_now
    cancelled: Callable[[], bool] = _not_cancelled
    persist_turn: PersistenceHook = _no_persistence
    response_builder: ResponseBuilder | None = None

    def specs_by_name(self) -> Mapping[str, ToolSpec]:
        return {spec.name: spec for spec in self.tool_specs}


def _context(runtime: Runtime[AnalysisGraphContext]) -> AnalysisGraphContext:
    if runtime.context is None:
        raise RuntimeError("analysis graph runtime context is required")
    return runtime.context


def _emit(runtime: Runtime[AnalysisGraphContext], payload: object) -> None:
    runtime.stream_writer(payload)


def _entry_guard(
    state: AnalysisAgentState,
    context: AnalysisGraphContext,
) -> None:
    ensure_node_entry(
        status=state["status"],
        budget=state["budget"],
        now=context.clock(),
        cancelled=context.cancelled,
    )


def _failure(exc: BaseException) -> dict[str, object]:
    if isinstance(exc, AgentGuardError):
        error = AgentError(
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
        )
    else:
        error = AgentError(
            code=ErrorCode.INTERNAL_ERROR,
            message="analysis graph node failed safely",
            retryable=False,
        )
    return {"error": error, "next_route": AgentRoute.FAIL.value}


async def initialize_run(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        request = state["request"]
        authority = DatasetAuthority.model_validate(state["authority"])
        if request.mode != authority.mode:
            raise AgentGuardError(
                ErrorCode.ACCESS_DENIED,
                "request mode does not match dataset authority",
            )
        if request.source_id is not None and (
            request.source_id != authority.source_id
            or request.source_version != authority.source_version
            or request.binding_id != authority.binding_id
            or request.binding_version != authority.binding_version
        ):
            raise AgentGuardError(
                ErrorCode.ACCESS_DENIED,
                "request pins do not match dataset authority",
            )
        now = context.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("analysis graph clock must be timezone-aware")
        if context.cancelled():
            raise AgentGuardError(ErrorCode.CANCELLED, "analysis run was cancelled")
        budget = AgentBudgetState(
            started_at=now,
            deadline_at=now
            + timedelta(seconds=context.budget_limits.max_duration_seconds),
        )
        goal = AnalysisGoal(
            original_question=request.question,
            contextualized_question=request.question,
            requested_output=request.requested_output,
            success_criteria=("Answer the current question with validated evidence",),
            constraints=("Use only the pinned dataset authority",),
        )
        _emit(
            runtime,
            RunStartedPayload(
                mode=request.mode,
                enterprise_id=request.enterprise_id,
                domain_id=request.domain_id,
            ),
        )
        return {
            "authority": authority,
            "goal": goal,
            "budget": budget,
            "observations": [],
            "artifact_refs": [],
            "evidence_refs": [],
            "pending_action": None,
            "pending_observation": None,
            "planner_decision": None,
            "evaluation_decision": None,
            "waiting_request": None,
            "answer_draft": None,
            "final_response": None,
            "error": None,
            "plan_revision_count": 0,
            "replan_requested": False,
            "action_step_ids": {},
            "status": AgentStatus.RUNNING,
            "next_route": "load_context",
        }
    except Exception as exc:
        return _failure(exc)


async def load_context(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        loaded = context.context_loader(state)
        if inspect.isawaitable(loaded):
            loaded = await loaded
        snapshot = AgentContextSnapshot.model_validate(loaded)
        goal = state["goal"]
        if snapshot.conversation_summary:
            goal = AnalysisPlanner.rebuild_follow_up_goal(
                question=state["request"].question,
                context=snapshot,
                requested_output=state["request"].requested_output,
            )
        authority = state["authority"]
        _emit(
            runtime,
            ContextResolvedPayload(
                source_id=authority.source_id,
                source_version=authority.source_version,
                binding_id=authority.binding_id,
                binding_version=authority.binding_version,
                schema_fingerprint=authority.schema_fingerprint,
            ),
        )
        return {
            "context": snapshot,
            "goal": goal,
            "next_route": AgentRoute.PLAN_OR_REPLAN.value,
        }
    except Exception as exc:
        return _failure(exc)


async def plan_or_replan(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        counters = ["agent_steps", "model_calls"]
        if state.get("replan_requested"):
            counters.append("replans")
        budget = consume_budget(state["budget"], context.budget_limits, *counters)
        allowed_names = set(state["context"].allowed_tool_names)
        allowed_specs = tuple(
            spec for spec in context.tool_specs if spec.name in allowed_names
        )
        decision = await context.planner.decide(
            goal=state["goal"],
            context=state["context"],
            current_plan=state.get("plan"),
            observations=tuple(state.get("observations", ())),
            budget_remaining=_budget_remaining(budget, context.budget_limits),
            allowed_tools=allowed_specs,
            max_observation_cells=context.budget_limits.max_observation_cells_for_model,
        )
        plan = decision.plan
        action_step_ids = dict(state.get("action_step_ids", {}))
        step_started: AnalysisStep | None = None
        if decision.decision == "act":
            completed = {
                step.step_id
                for step in plan.steps
                if step.status in {"completed", "skipped"}
            }
            step_started = next(
                (
                    step
                    for step in plan.steps
                    if step.status == "pending" and set(step.depends_on).issubset(completed)
                ),
                None,
            )
            if step_started is None or decision.next_action is None:
                raise AgentGuardError(
                    ErrorCode.AGENT_DECISION_INVALID,
                    "planner action has no runnable plan step",
                )
            running_step = step_started.model_copy(update={"status": "running"})
            plan = plan.model_copy(
                update={
                    "steps": tuple(
                        running_step if item.step_id == running_step.step_id else item
                        for item in plan.steps
                    )
                }
            )
            decision = decision.model_copy(update={"plan": plan})
            action_step_ids[decision.next_action.action_id] = running_step.step_id
        current = state.get("plan")
        revision_count = state.get("plan_revision_count", 0)
        if current is None or plan.revision != current.revision:
            revision_count += 1
        _emit(runtime, PlanUpdatedPayload(plan=plan))
        if step_started is not None:
            _emit(
                runtime,
                StepStartedPayload(
                    step_id=step_started.step_id,
                    objective=step_started.objective,
                ),
            )
        return {
            "budget": budget,
            "plan": plan,
            "planner_decision": decision,
            "pending_action": decision.next_action,
            "waiting_request": decision.clarification,
            "action_step_ids": action_step_ids,
            "plan_revision_count": revision_count,
            "replan_requested": False,
            "status": (
                AgentStatus.WAITING_INPUT
                if decision.decision == "clarify"
                else AgentStatus.RUNNING
            ),
            "next_route": None,
        }
    except Exception as exc:
        return _failure(exc)


async def guard_decision(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    if state.get("error") is not None:
        return {"next_route": AgentRoute.FAIL.value}
    try:
        _entry_guard(state, context)
        decision = state.get("planner_decision")
        if decision is None:
            raise AgentGuardError(
                ErrorCode.AGENT_DECISION_INVALID,
                "planner decision is unavailable",
            )
        route = guard_planner_decision(
            decision,
            mode=state["authority"].mode,
            allowed_tool_names=state["context"].allowed_tool_names,
            specs=context.specs_by_name(),
        )
        if route == AgentRoute.FAIL:
            return {
                "error": AgentError(
                    code=ErrorCode.AGENT_DECISION_INVALID,
                    message="planner selected the terminal failure route",
                ),
                "next_route": route.value,
            }
        return {"next_route": route.value}
    except Exception as exc:
        return _failure(exc)


async def execute_tool(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        action = state.get("pending_action")
        if action is None:
            raise AgentGuardError(
                ErrorCode.AGENT_ACTION_NOT_ALLOWED,
                "tool node has no guarded action",
            )
        budget = consume_budget(
            state["budget"],
            context.budget_limits,
            *tool_budget_counters(action.tool_name),
        )
        call_id = stable_call_id(state["run_id"], action.action_id, action.arguments)
        _emit(
            runtime,
            ToolStartedPayload(
                call_id=call_id,
                action_id=action.action_id,
                tool_name=action.tool_name,
                display_name=action.tool_name,
                safe_arguments_digest=stable_digest(action.arguments),
            ),
        )
        observation = await context.tool_executor.invoke(
            call_id=call_id,
            action=action,
            state=state,
        )
        observation = AgentObservation.model_validate(observation)
        if (
            observation.action_id != action.action_id
            or observation.tool_name != action.tool_name
        ):
            raise AgentGuardError(
                ErrorCode.AGENT_ACTION_NOT_ALLOWED,
                "tool observation does not match the guarded action",
            )
        _emit(
            runtime,
            ToolCompletedPayload(
                call_id=call_id,
                action_id=action.action_id,
                tool_name=action.tool_name,
                status=observation.status,
                artifacts=tuple(_artifact_summary(item) for item in observation.artifact_refs),
                evidence=tuple(_evidence_summary(item) for item in observation.evidence_refs),
                error_code=(observation.error.code if observation.error else None),
            ),
        )
        return {
            "budget": budget,
            "pending_observation": observation,
            "next_route": "observe_result",
        }
    except Exception as exc:
        return _failure(exc)


async def observe_result(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        observation = state.get("pending_observation")
        if observation is None:
            raise AgentGuardError(
                ErrorCode.INTERNAL_ERROR,
                "tool node did not produce an observation",
            )
        plan = state["plan"]
        step_id = state.get("action_step_ids", {}).get(observation.action_id)
        if step_id is None:
            raise AgentGuardError(
                ErrorCode.AGENT_DECISION_INVALID,
                "tool action is not assigned to a plan step",
            )
        target_status = "completed" if observation.status == "succeeded" else "blocked"
        plan = plan.model_copy(
            update={
                "steps": tuple(
                    item.model_copy(update={"status": target_status})
                    if item.step_id == step_id
                    else item
                    for item in plan.steps
                )
            }
        )
        _emit(
            runtime,
            ObservationRecordedPayload(
                observation_id=observation.observation_id,
                action_id=observation.action_id,
                summary=observation.summary,
                artifact_ids=tuple(item.artifact_id for item in observation.artifact_refs),
                evidence_ids=tuple(item.evidence_id for item in observation.evidence_refs),
            ),
        )
        return {
            "plan": plan,
            "observations": [observation],
            "artifact_refs": list(observation.artifact_refs),
            "evidence_refs": list(observation.evidence_refs),
            "pending_observation": None,
            "next_route": "evaluate_progress",
        }
    except Exception as exc:
        return _failure(exc)


async def evaluate_progress(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        budget = consume_budget(state["budget"], context.budget_limits, "model_calls")
        required = tuple(
            dict.fromkeys(
                expectation
                for step in state["plan"].steps
                for expectation in step.expected_evidence
            )
        )
        decision = await context.evaluator.evaluate(
            run_id=state["run_id"],
            plan=state["plan"],
            authority=state["authority"],
            observations=tuple(state.get("observations", ())),
            artifacts=tuple(state.get("artifact_refs", ())),
            evidence=tuple(state.get("evidence_refs", ())),
            required_evidence_keys=required,
            deterministic_contradictions=(),
            budget_exhausted=False,
            max_observation_cells=context.budget_limits.max_observation_cells_for_model,
        )
        route = {
            "continue": AgentRoute.PLAN_OR_REPLAN,
            "replan": AgentRoute.PLAN_OR_REPLAN,
            "clarify": AgentRoute.REQUEST_INPUT,
            "finish": AgentRoute.SYNTHESIZE_ANSWER,
            "fail": AgentRoute.FAIL,
        }[decision.decision]
        update: dict[str, object] = {
            "budget": budget,
            "evaluation_decision": decision,
            "waiting_request": decision.clarification,
            "replan_requested": decision.decision == "replan",
            "status": (
                AgentStatus.WAITING_INPUT
                if decision.decision == "clarify"
                else AgentStatus.RUNNING
            ),
            "next_route": route.value,
        }
        if route == AgentRoute.FAIL:
            update["error"] = AgentError(
                code=ErrorCode.AGENT_EVIDENCE_INSUFFICIENT,
                message="analysis evaluation selected the terminal failure route",
            )
        return update
    except Exception as exc:
        return _failure(exc)


async def request_input(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        request = state.get("waiting_request")
        if request is None:
            raise AgentGuardError(
                ErrorCode.AGENT_DECISION_INVALID,
                "input route has no typed input request",
            )
        response = interrupt(request.model_dump(mode="json"))
        message = _resume_message(response)
        goal = state["goal"].model_copy(
            update={
                "contextualized_question": (
                    state["goal"].contextualized_question
                    + "\nClarification response: "
                    + message
                )
            }
        )
        _emit(runtime, RunResumedPayload(interrupt_id=request.interrupt_id))
        return {
            "goal": goal,
            "waiting_request": None,
            "status": AgentStatus.RUNNING,
            "replan_requested": True,
            "next_route": AgentRoute.PLAN_OR_REPLAN.value,
        }
    except GraphInterrupt:
        raise
    except Exception as exc:
        return _failure(exc)


async def synthesize_answer(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        evidence = tuple(state.get("evidence_refs", ()))
        _emit(
            runtime,
            AnswerSynthesizingPayload(
                evidence_ids=tuple(item.evidence_id for item in evidence)
            ),
        )
        budget = state["budget"]
        if state["authority"].mode.value == "plan" and not evidence:
            completion = (
                state.get("planner_decision").completion_summary
                if state.get("planner_decision") is not None
                else None
            )
            draft = AgentAnswerDraft(
                answer=completion or "The governed analysis plan is ready; no data was executed.",
                key_findings=(),
                recommended_chart_artifact_id=None,
                limitations=("Plan mode does not execute or preview datasource rows.",),
                evidence_ids=(),
            )
        else:
            budget = consume_budget(budget, context.budget_limits, "model_calls")
            draft = await context.synthesizer.synthesize(
                run_id=state["run_id"],
                goal=state["goal"],
                mode=state["authority"].mode,
                authority=state["authority"],
                observations=tuple(state.get("observations", ())),
                artifacts=tuple(state.get("artifact_refs", ())),
                evidence=evidence,
                max_observation_cells=context.budget_limits.max_observation_cells_for_model,
            )
        return {
            "budget": budget,
            "answer_draft": draft,
            "next_route": "validate_answer",
        }
    except Exception as exc:
        return _failure(exc)


async def validate_answer(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        draft = state.get("answer_draft")
        if draft is None:
            raise AgentGuardError(
                ErrorCode.AGENT_RESPONSE_UNGROUNDED,
                "answer draft is unavailable",
            )
        draft = AgentAnswerDraft.model_validate(draft)
        available = {item.evidence_id for item in state.get("evidence_refs", ())}
        if not set(draft.evidence_ids).issubset(available):
            raise AgentGuardError(
                ErrorCode.AGENT_RESPONSE_UNGROUNDED,
                "answer references evidence outside the current run",
            )
        return {"answer_draft": draft, "next_route": "persist_turn"}
    except Exception as exc:
        return _failure(exc)


async def persist_turn(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        persisted = context.persist_turn(state)
        if inspect.isawaitable(persisted):
            await persisted
        return {"next_route": "finalize_run"}
    except Exception as exc:
        return _failure(exc)


async def finalize_run(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    try:
        _entry_guard(state, context)
        completed_state = dict(state)
        completed_state["status"] = AgentStatus.COMPLETED
        response = await _response(context, completed_state, ok=True)
        _emit(runtime, RunCompletedPayload())
        return {
            "status": AgentStatus.COMPLETED,
            "final_response": response,
            "next_route": None,
        }
    except Exception as exc:
        return _failure(exc)


async def fail(
    state: AnalysisAgentState,
    runtime: Runtime[AnalysisGraphContext],
) -> dict[str, object]:
    context = _context(runtime)
    error = state.get("error") or AgentError(
        code=ErrorCode.INTERNAL_ERROR,
        message="analysis run failed safely",
    )
    failed_state = dict(state)
    failed_state["error"] = error
    failed_state["status"] = (
        AgentStatus.CANCELLED
        if error.code == ErrorCode.CANCELLED
        else AgentStatus.FAILED
    )
    response = await _response(context, failed_state, ok=False)
    _emit(runtime, RunFailedPayload(error_code=error.code))
    return {
        "error": error,
        "status": failed_state["status"],
        "final_response": response,
        "next_route": None,
    }


async def _response(
    context: AnalysisGraphContext,
    state: AnalysisAgentState,
    *,
    ok: bool,
) -> AgentResponse:
    if context.response_builder is not None:
        value = context.response_builder(state, ok)
        if inspect.isawaitable(value):
            value = await value
        return AgentResponse.model_validate(value)
    request = state["request"]
    draft = state.get("answer_draft")
    artifacts = tuple(state.get("artifact_refs", ()))
    evidence = tuple(state.get("evidence_refs", ()))
    error = None if ok else state.get("error")
    return AgentResponse(
        ok=ok,
        question=request.question,
        contextualized_question=(
            state.get("goal").contextualized_question if state.get("goal") else None
        ),
        conversation_id=request.conversation_id,
        tenant_id=state["authority"].tenant_id,
        message_type="analysis" if ok else "error",
        answer=draft.answer if ok and draft is not None else None,
        error=error,
        version_pins=context.version_pins,
        analysis_plan=state.get("plan"),
        analysis_steps=_step_summaries(state),
        artifacts=tuple(_artifact_summary(item) for item in artifacts),
        evidence=tuple(_evidence_summary(item) for item in evidence),
        limitations=draft.limitations if draft is not None else (),
    )


def _step_summaries(state: AnalysisAgentState) -> tuple[AnalysisStepSummary, ...]:
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


def _artifact_summary(item: AgentArtifactRef) -> AgentArtifactSummary:
    return AgentArtifactSummary(
        artifact_id=item.artifact_id,
        kind=item.kind,
        digest=item.digest,
        row_count=item.row_count,
        sensitivity=item.sensitivity,
        created_at=item.created_at,
    )


def _evidence_summary(item: object) -> EvidenceSummary:
    return EvidenceSummary(
        evidence_id=item.evidence_id,
        claim_key=item.claim_key,
        artifact_id=item.artifact_id,
        field_refs=item.field_refs,
    )


def _budget_remaining(
    budget: AgentBudgetState,
    limits: AgentRunBudget,
) -> dict[str, int]:
    return {
        "agent_steps": limits.max_agent_steps - budget.agent_steps,
        "model_calls": limits.max_model_calls - budget.model_calls,
        "tool_calls": limits.max_tool_calls - budget.tool_calls,
        "query_compiles": limits.max_query_compiles - budget.query_compiles,
        "query_previews": limits.max_query_previews - budget.query_previews,
        "query_executes": limits.max_query_executes - budget.query_executes,
        "replans": limits.max_replans - budget.replans,
    }


def stable_call_id(run_id: str, action_id: str, arguments: object) -> str:
    return "call-" + stable_digest(
        {"run_id": run_id, "action_id": action_id, "arguments": arguments}
    )


def _resume_message(value: object) -> str:
    if isinstance(value, str):
        message = value.strip()
    elif isinstance(value, dict):
        forbidden = {
            "authority",
            "tenant_id",
            "user_id",
            "source_id",
            "source_version",
            "binding_id",
            "binding_version",
        }
        if forbidden.intersection(value):
            raise AgentGuardError(
                ErrorCode.AGENT_RESUME_CONFLICT,
                "resume payload cannot modify dataset authority",
            )
        message = str(value.get("message", "")).strip()
    else:
        message = ""
    if not message or len(message) > 4000:
        raise AgentGuardError(
            ErrorCode.AGENT_INTERRUPT_STALE,
            "resume response is missing or exceeds its size limit",
        )
    return message


def _observation_preview(value: object) -> tuple[dict[str, object], ...]:
    if value is None:
        return ()
    if isinstance(value, dict):
        columns = value.get("columns")
        rows = value.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            output: list[dict[str, object]] = []
            for row in rows[:20]:
                values = row.get("values") if isinstance(row, dict) else None
                if isinstance(values, list):
                    output.append(
                        {
                            str(column): cell
                            for column, cell in zip(columns[:20], values[:20])
                        }
                    )
            return tuple(output)
        return ({str(key): nested for key, nested in list(value.items())[:20]},)
    if isinstance(value, list):
        return tuple(
            {str(key): nested for key, nested in list(row.items())[:20]}
            for row in value[:20]
            if isinstance(row, dict)
        )
    return ({"value": str(value)[:240]},)


def _public_tool_error(code: ToolErrorCode) -> ErrorCode:
    mapping = {
        ToolErrorCode.INPUT_INVALID: ErrorCode.AGENT_DECISION_INVALID,
        ToolErrorCode.TOOL_NOT_ALLOWED: ErrorCode.AGENT_ACTION_NOT_ALLOWED,
        ToolErrorCode.BUDGET_EXCEEDED: ErrorCode.AGENT_BUDGET_EXCEEDED,
        ToolErrorCode.LOGICAL_PLAN_INVALID: ErrorCode.LOGICAL_PLAN_INVALID,
        ToolErrorCode.BINDING_STALE: ErrorCode.BINDING_STALE,
        ToolErrorCode.SQL_COMPILE_ERROR: ErrorCode.SQL_COMPILE_ERROR,
        ToolErrorCode.ACCESS_DENIED: ErrorCode.ACCESS_DENIED,
        ToolErrorCode.GRANT_INVALID: ErrorCode.ACCESS_DENIED,
        ToolErrorCode.GRANT_EXPIRED: ErrorCode.ACCESS_DENIED,
        ToolErrorCode.RELATION_NOT_ALLOWED: ErrorCode.ACCESS_DENIED,
        ToolErrorCode.POLICY_VIOLATION: ErrorCode.SQL_POLICY_VIOLATION,
        ToolErrorCode.ROW_LIMIT_EXCEEDED: ErrorCode.COST_EXCEEDED,
        ToolErrorCode.TIMEOUT: ErrorCode.DEADLINE_EXCEEDED,
        ToolErrorCode.CANCELLED: ErrorCode.CANCELLED,
    }
    return mapping.get(code, ErrorCode.INTERNAL_ERROR)


__all__ = [
    "AgentToolExecutor",
    "AnalysisGraphContext",
    "DatasetAgentToolInvoker",
    "evaluate_progress",
    "execute_tool",
    "fail",
    "finalize_run",
    "guard_decision",
    "initialize_run",
    "load_context",
    "observe_result",
    "persist_turn",
    "plan_or_replan",
    "request_input",
    "stable_call_id",
    "synthesize_answer",
    "validate_answer",
]
