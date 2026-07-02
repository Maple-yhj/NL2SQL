from __future__ import annotations

from typing import Any, TypedDict

from engine.models import QueryIntent


class RequiredInputState(TypedDict):
    question: str
    tenant_id: str
    execute: bool


class OptionalInputState(TypedDict, total=False):
    conversation_id: str
    user_id: str


class InputState(RequiredInputState, OptionalInputState):
    pass


class OutputState(TypedDict):
    ok: bool
    question: str
    contextualized_question: str
    conversation_id: str
    user_id: str
    tenant_id: str
    intent: dict[str, Any]
    sql: str
    message_type: str
    rows: list[dict[str, Any]]
    answer: str
    error: str
    trace: list[dict[str, Any]]


class GraphOptionalState(TypedDict, total=False):
    contextualized_question: str
    conversation_history: list[dict[str, Any]]
    user_memories: list[dict[str, Any]]
    intent: QueryIntent
    plan: dict[str, Any]
    execution_graph: dict[str, Any]
    planned_intent: QueryIntent
    plan_context: str
    metrics_result: dict[str, Any]
    schema_result: dict[str, Any]
    table_names: list[str]
    allowed_tables: list[str]
    candidate_sql: str
    validated_sql: str
    validation_result: dict[str, Any]
    validation_attempts: int
    retry_feedback: str
    execution_result: dict[str, Any]
    rows: list[dict[str, Any]]
    answer: str
    error: str
    trace: list[dict[str, Any]]
    ok: bool
    sql: str
    message_type: str


class GraphState(InputState, GraphOptionalState):
    pass
