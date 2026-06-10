from typing import Any, TypedDict

from engine.models import QueryIntent


class InputState(TypedDict):
    """Values required to start one graph invocation."""

    question: str
    tenant_id: str
    execute: bool


class OutputState(TypedDict):
    """Public result returned after the graph reaches a terminal node."""

    ok: bool
    sql: str
    rows: list[dict[str, Any]]
    answer: str
    error: str


class GraphState(InputState, total=False):
    """Minimal shared state updated incrementally by graph nodes."""

    intent: QueryIntent
    metrics: list[dict[str, Any]]
    schema: list[dict[str, Any]]

    candidate_sql: str
    validated_sql: str
    validation_error: str
    validation_attempts: int

    rows: list[dict[str, Any]]
    answer: str
    error: str
