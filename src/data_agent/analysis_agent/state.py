"""Checkpoint-safe state shape and replay-idempotent reducers."""

from __future__ import annotations

from typing import Annotated, TypedDict, TypeVar

from data_agent.runtime.errors import AgentError
from data_agent.runtime.models import AgentRequest, AgentResponse

from .models import (
    AgentAction,
    AgentAnswerDraft,
    AgentArtifactRef,
    AgentBudgetState,
    AgentContextSnapshot,
    AgentInputRequest,
    AgentObservation,
    AgentStatus,
    AnalysisGoal,
    AnalysisPlan,
    DatasetAuthority,
    EvaluationDecision,
    EvidenceRef,
    PlannerDecision,
)


ModelT = TypeVar("ModelT", AgentObservation, AgentArtifactRef, EvidenceRef)


def _append_unique(
    current: list[ModelT] | None,
    updates: list[ModelT] | None,
    *,
    model_type: type[ModelT],
    id_field: str,
) -> list[ModelT]:
    result = [model_type.model_validate(item) for item in (current or [])]
    positions = {getattr(item, id_field): index for index, item in enumerate(result)}
    for raw_item in updates or []:
        item = model_type.model_validate(raw_item)
        item_id = getattr(item, id_field)
        existing_index = positions.get(item_id)
        if existing_index is None:
            positions[item_id] = len(result)
            result.append(item)
            continue
        if result[existing_index] != item:
            raise ValueError(f"conflicting append-only value for {id_field}={item_id}")
    return result


def append_observations(
    current: list[AgentObservation] | None,
    updates: list[AgentObservation] | None,
) -> list[AgentObservation]:
    return _append_unique(
        current,
        updates,
        model_type=AgentObservation,
        id_field="observation_id",
    )


def append_unique_artifacts(
    current: list[AgentArtifactRef] | None,
    updates: list[AgentArtifactRef] | None,
) -> list[AgentArtifactRef]:
    return _append_unique(
        current,
        updates,
        model_type=AgentArtifactRef,
        id_field="artifact_id",
    )


def append_unique_evidence(
    current: list[EvidenceRef] | None,
    updates: list[EvidenceRef] | None,
) -> list[EvidenceRef]:
    return _append_unique(
        current,
        updates,
        model_type=EvidenceRef,
        id_field="evidence_id",
    )


_STATUS_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.INITIALIZING: frozenset(
        {AgentStatus.RUNNING, AgentStatus.FAILED, AgentStatus.CANCELLED}
    ),
    AgentStatus.RUNNING: frozenset(
        {
            AgentStatus.WAITING_INPUT,
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
        }
    ),
    AgentStatus.WAITING_INPUT: frozenset(
        {AgentStatus.RUNNING, AgentStatus.FAILED, AgentStatus.CANCELLED}
    ),
    AgentStatus.COMPLETED: frozenset(),
    AgentStatus.FAILED: frozenset(),
    AgentStatus.CANCELLED: frozenset(),
}


def transition_agent_status(
    current: AgentStatus | str,
    target: AgentStatus | str,
) -> AgentStatus:
    current_status = AgentStatus(current)
    target_status = AgentStatus(target)
    if current_status == target_status:
        return target_status
    if target_status not in _STATUS_TRANSITIONS[current_status]:
        raise ValueError(
            "invalid agent status transition: "
            f"{current_status.value} -> {target_status.value}"
        )
    return target_status


class AnalysisAgentState(TypedDict, total=False):
    run_id: str
    conversation_id: str | None
    request: AgentRequest
    authority: DatasetAuthority
    goal: AnalysisGoal
    context: AgentContextSnapshot
    plan: AnalysisPlan
    pending_action: AgentAction | None
    pending_observation: AgentObservation | None
    planner_decision: PlannerDecision | None
    evaluation_decision: EvaluationDecision | None
    next_route: str | None
    replan_requested: bool
    action_step_ids: dict[str, str]
    observations: Annotated[list[AgentObservation], append_observations]
    artifact_refs: Annotated[list[AgentArtifactRef], append_unique_artifacts]
    evidence_refs: Annotated[list[EvidenceRef], append_unique_evidence]
    budget: AgentBudgetState
    plan_revision_count: int
    status: AgentStatus
    waiting_request: AgentInputRequest | None
    answer_draft: AgentAnswerDraft | None
    final_response: AgentResponse | None
    error: AgentError | None


__all__ = [
    "AgentStatus",
    "AnalysisAgentState",
    "append_observations",
    "append_unique_artifacts",
    "append_unique_evidence",
    "transition_agent_status",
]
