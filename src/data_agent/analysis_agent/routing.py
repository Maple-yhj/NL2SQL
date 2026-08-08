"""Closed routing enums for the native analysis graph."""

from __future__ import annotations

from enum import StrEnum

from .state import AnalysisAgentState


class AgentRoute(StrEnum):
    EXECUTE_TOOL = "execute_tool"
    REQUEST_INPUT = "request_input"
    SYNTHESIZE_ANSWER = "synthesize_answer"
    PLAN_OR_REPLAN = "plan_or_replan"
    FAIL = "fail"


def route_after_guard(state: AnalysisAgentState) -> str:
    return _closed_route(
        state,
        allowed={
            AgentRoute.EXECUTE_TOOL,
            AgentRoute.REQUEST_INPUT,
            AgentRoute.SYNTHESIZE_ANSWER,
            AgentRoute.FAIL,
        },
    )


def route_after_evaluation(state: AnalysisAgentState) -> str:
    return _closed_route(
        state,
        allowed={
            AgentRoute.PLAN_OR_REPLAN,
            AgentRoute.REQUEST_INPUT,
            AgentRoute.SYNTHESIZE_ANSWER,
            AgentRoute.FAIL,
        },
    )


def _closed_route(
    state: AnalysisAgentState,
    *,
    allowed: set[AgentRoute],
) -> str:
    raw = state.get("next_route")
    try:
        route = AgentRoute(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("analysis graph state has no valid server route") from exc
    if route not in allowed:
        raise ValueError("analysis graph route is not valid at this boundary")
    return route.value


__all__ = ["AgentRoute", "route_after_evaluation", "route_after_guard"]
