from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent.sql_generator import generate_sql
from agent.tools.descriptions import get_tool_description
from agent.tools.execute_sql import execute_sql
from agent.tools.explain_result import explain_result
from agent.tools.sql_store import search_metrics, search_schema
from agent.tools.validate_sql import validate_sql
from engine.models import QueryIntent


ToolExecutor = Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolContext:
    question: str
    tenant_id: str
    table_names: list[str] | None = None
    allowed_tables: list[str] | None = None
    intent: QueryIntent | None = None
    metrics_result: dict[str, Any] | None = None
    schema_result: dict[str, Any] | None = None
    retry_feedback: str | None = None
    candidate_sql: str | None = None
    validated_sql: str | None = None
    execution_rows: list[dict[str, Any]] | None = None
    execute_enabled: bool = False
    dsn: str | None = None
    timeout_ms: int = 10_000
    max_limit: int = 1000
    llm: Any = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    executor: ToolExecutor
    max_calls: int = 1


async def _execute_search_metrics(
    args: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    return await search_metrics(
        query=args.get("query") or context.question,
        tenant_id=context.tenant_id,
        top_k=args.get("top_k", 3),
        min_score=args.get("min_score"),
    )


async def _execute_search_schema(
    args: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    return await search_schema(
        query=args.get("query") or context.question,
        tenant_id=context.tenant_id,
        top_k=args.get("top_k", 8),
        min_score=args.get("min_score"),
        table_names=context.table_names,
    )


async def _execute_generate_sql(
    args: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    sql = await generate_sql(
        question=context.question,
        intent=context.intent,
        metrics_result=context.metrics_result or {},
        schema_result=context.schema_result or {},
        retry_feedback=context.retry_feedback,
        llm=context.llm,
    )
    return {"ok": True, "sql": sql, "message": "success"}


async def _execute_validate_sql(
    args: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    if not context.candidate_sql:
        raise ValueError("Tool[validate_sql] requires context.candidate_sql")

    return await validate_sql(
        sql=context.candidate_sql,
        tenant_id=context.tenant_id,
        allowed_tables=context.allowed_tables,
        max_limit=context.max_limit,
    )


async def _execute_execute_sql(
    args: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    if not context.execute_enabled:
        raise PermissionError("Tool[execute_sql] is disabled for this request")
    if not context.validated_sql:
        raise ValueError("Tool[execute_sql] requires context.validated_sql")

    return await execute_sql(
        sql=context.validated_sql,
        tenant_id=context.tenant_id,
        dsn=context.dsn,
        timeout_ms=context.timeout_ms,
        max_limit=context.max_limit,
        allowed_tables=context.allowed_tables,
    )


async def _execute_explain_result(
    args: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    if not context.validated_sql:
        raise ValueError("Tool[explain_result] requires context.validated_sql")
    if context.execution_rows is None:
        raise ValueError("Tool[explain_result] requires context.execution_rows")

    return await explain_result(
        question=context.question,
        sql=context.validated_sql,
        rows=context.execution_rows,
        metrics_result=context.metrics_result or {},
        llm=context.llm,
        max_preview_rows=args.get("max_preview_rows", 5),
    )


SEARCH_METRICS_SPEC = ToolSpec(
    name="search_metrics",
    description=get_tool_description("search_metrics"),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "用于检索指标定义的查询。"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
            "min_score": {"type": ["number", "null"]},
        },
        "required": [],
        "additionalProperties": False,
    },
    executor=_execute_search_metrics,
)


SEARCH_SCHEMA_SPEC = ToolSpec(
    name="search_schema",
    description=get_tool_description("search_schema"),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "用于检索表和字段的查询。"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            "min_score": {"type": ["number", "null"]},
        },
        "required": [],
        "additionalProperties": False,
    },
    executor=_execute_search_schema,
    max_calls=2,
)


GENERATE_SQL_SPEC = ToolSpec(
    name="generate_sql",
    description=get_tool_description("generate_sql"),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    executor=_execute_generate_sql,
    max_calls=2,
)


VALIDATE_SQL_SPEC = ToolSpec(
    name="validate_sql",
    description=get_tool_description("validate_sql"),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    executor=_execute_validate_sql,
    max_calls=2,
)


EXECUTE_SQL_SPEC = ToolSpec(
    name="execute_sql",
    description=get_tool_description("execute_sql"),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    executor=_execute_execute_sql,
)


EXPLAIN_RESULT_SPEC = ToolSpec(
    name="explain_result",
    description=get_tool_description("explain_result"),
    input_schema={
        "type": "object",
        "properties": {
            "max_preview_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    executor=_execute_explain_result,
)


TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        SEARCH_METRICS_SPEC,
        SEARCH_SCHEMA_SPEC,
        GENERATE_SQL_SPEC,
        VALIDATE_SQL_SPEC,
        EXECUTE_SQL_SPEC,
        EXPLAIN_RESULT_SPEC,
    )
}


def get_tool_spec(name: str) -> ToolSpec:
    try:
        return TOOL_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown tool: {name}") from exc


def list_tool_specs() -> list[ToolSpec]:
    return list(TOOL_REGISTRY.values())


def _validate_action_arguments(spec: ToolSpec, args: Mapping[str, Any]) -> dict[str, Any]:
    properties = spec.input_schema.get("properties", {})
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ValueError(
            f"Tool[{spec.name}]: unexpected arguments: {', '.join(unknown)}"
        )

    missing = [
        name
        for name in spec.input_schema.get("required", [])
        if name not in args
    ]
    if missing:
        raise ValueError(
            f"Tool[{spec.name}]: missing required arguments: {', '.join(missing)}"
        )

    return dict(args)


async def call_tool(
    name: str,
    args: Mapping[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    spec = get_tool_spec(name)
    validated_args = _validate_action_arguments(spec, args)
    return await spec.executor(validated_args, context)
