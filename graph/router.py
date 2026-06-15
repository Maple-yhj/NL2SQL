from __future__ import annotations

from graph.state import GraphState


def route_after_schema(state: GraphState) -> str:
    if state.get("error") or not state.get("allowed_tables"):
        return "finalize"
    return "generate_sql"


def route_after_validate(state: GraphState, *, max_attempts: int) -> str:
    result = state.get("validation_result") or {}
    if result.get("ok") is True:
        return "execute_sql" if state.get("execute", False) else "finalize"
    if state.get("validation_attempts", 0) < max_attempts:
        return "generate_sql"
    return "finalize"


def route_after_execute(state: GraphState) -> str:
    result = state.get("execution_result") or {}
    if result.get("ok") is True:
        return "explain"
    return "finalize"
