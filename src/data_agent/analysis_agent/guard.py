"""Server-owned decision, deadline, cancellation and budget guards."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from data_agent.public_contracts import ErrorCode
from data_agent.runtime.models import AgentMode
from data_agent.tools.models import ToolSpec

from .models import (
    AgentBudgetState,
    AgentRunBudget,
    AgentStatus,
    PlannerDecision,
)
from .routing import AgentRoute


class AgentGuardError(RuntimeError):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def ensure_node_entry(
    *,
    status: AgentStatus,
    budget: AgentBudgetState,
    now: datetime,
    cancelled: Callable[[], bool],
) -> None:
    if status in {AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED}:
        raise AgentGuardError(
            ErrorCode.AGENT_ACTION_NOT_ALLOWED,
            "terminal analysis run cannot execute another node",
        )
    if cancelled():
        raise AgentGuardError(ErrorCode.CANCELLED, "analysis run was cancelled")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("analysis graph clock must be timezone-aware")
    if now >= budget.deadline_at:
        raise AgentGuardError(
            ErrorCode.DEADLINE_EXCEEDED,
            "analysis run exceeded its deadline",
        )


def consume_budget(
    budget: AgentBudgetState,
    limits: AgentRunBudget,
    *counters: str,
) -> AgentBudgetState:
    updates: dict[str, int] = {}
    limit_fields = {
        "agent_steps": "max_agent_steps",
        "model_calls": "max_model_calls",
        "tool_calls": "max_tool_calls",
        "query_compiles": "max_query_compiles",
        "query_previews": "max_query_previews",
        "query_executes": "max_query_executes",
        "replans": "max_replans",
    }
    for counter in counters:
        limit_name = limit_fields.get(counter)
        if limit_name is None:
            raise ValueError(f"unknown Agent budget counter: {counter}")
        current = updates.get(counter, getattr(budget, counter))
        limit = getattr(limits, limit_name)
        if current >= limit:
            code = (
                ErrorCode.AGENT_MAX_STEPS_EXCEEDED
                if counter == "agent_steps"
                else ErrorCode.AGENT_BUDGET_EXCEEDED
            )
            raise AgentGuardError(code, f"analysis {counter} budget is exhausted")
        updates[counter] = current + 1
    return budget.model_copy(update=updates)


def tool_budget_counters(tool_name: str) -> tuple[str, ...]:
    counters = ["tool_calls"]
    if tool_name == "query.compile":
        counters.append("query_compiles")
    elif tool_name == "query.preview":
        counters.append("query_previews")
    elif tool_name == "query.execute":
        counters.append("query_executes")
    return tuple(counters)


def guard_planner_decision(
    decision: PlannerDecision,
    *,
    mode: AgentMode,
    allowed_tool_names: tuple[str, ...],
    specs: Mapping[str, ToolSpec],
) -> AgentRoute:
    routes = {
        "clarify": AgentRoute.REQUEST_INPUT,
        "finish": AgentRoute.SYNTHESIZE_ANSWER,
        "fail": AgentRoute.FAIL,
    }
    if decision.decision != "act":
        return routes[decision.decision]
    action = decision.next_action
    if action is None:
        raise AgentGuardError(
            ErrorCode.AGENT_DECISION_INVALID,
            "planner act decision omitted its action",
        )
    spec = specs.get(action.tool_name)
    if spec is None or action.tool_name not in allowed_tool_names:
        raise AgentGuardError(
            ErrorCode.AGENT_ACTION_NOT_ALLOWED,
            "planner selected a tool outside the allowed registry view",
        )
    if "dataset" not in spec.authority_kinds or mode not in spec.allowed_modes:
        raise AgentGuardError(
            ErrorCode.AGENT_ACTION_NOT_ALLOWED,
            "planner selected a tool unavailable in the current mode",
        )
    try:
        spec.input_schema.model_validate(action.arguments)
    except (TypeError, ValueError) as exc:
        raise AgentGuardError(
            ErrorCode.AGENT_DECISION_INVALID,
            "planner action failed its strict tool input schema",
        ) from exc
    return AgentRoute.EXECUTE_TOOL


__all__ = [
    "AgentGuardError",
    "consume_budget",
    "ensure_node_entry",
    "guard_planner_decision",
    "tool_budget_counters",
]
