from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from engine.models import QueryIntent


class InputState(TypedDict):
    question: str
    tenant_id: str
    execute: bool


class OutputState(TypedDict):
    ok: bool
    question: str
    tenant_id: str
    intent: dict[str, Any]
    sql: str
    rows: list[dict[str, Any]]
    answer: str
    error: str
    trace: list[dict[str, Any]]


class GraphState(InputState):
    intent: NotRequired[QueryIntent]
    metrics_result: NotRequired[dict[str, Any]]
    schema_result: NotRequired[dict[str, Any]]
    table_names: NotRequired[list[str]]
    allowed_tables: NotRequired[list[str]]
    candidate_sql: NotRequired[str]
    validated_sql: NotRequired[str]
    validation_result: NotRequired[dict[str, Any]]
    validation_attempts: NotRequired[int]
    retry_feedback: NotRequired[str]
    execution_result: NotRequired[dict[str, Any]]
    rows: NotRequired[list[dict[str, Any]]]
    answer: NotRequired[str]
    error: NotRequired[str]
    trace: NotRequired[list[dict[str, Any]]]
    ok: NotRequired[bool]
    sql: NotRequired[str]
