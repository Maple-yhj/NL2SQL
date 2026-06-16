from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from langgraph.runtime import Runtime

from engine.intent_parser import parse_intent
from graph.context import GraphContext
from graph.state import GraphState, OutputState
from graph.tools.execute_sql import execute_sql
from graph.tools.explain_result import explain_result
from graph.tools.sql_generator import generate_sql
from graph.tools.sql_store import search_metrics, search_schema
from graph.tools.validate_sql import validate_sql


_JOIN_TABLE_RE = re.compile(r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.IGNORECASE)


def _trace(state: GraphState, node_name: str, *, ok: bool, message: str = "") -> list[dict[str, Any]]:
    return [
        *state.get("trace", []),
        {"node": node_name, "ok": ok, "message": message},
    ]


def _error(state: GraphState, node_name: str, exc: Exception | str) -> GraphState:
    message = str(exc)
    return {
        "error": message,
        "trace": _trace(state, node_name, ok=False, message=message),
    } # type: ignore


def _metric_table_names(metrics_result: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for metric in metrics_result.get("metrics", []) or []:
        base_table = str(metric.get("base_table") or "").strip()
        if base_table:
            name = base_table.split()[0].split(".")[-1]
            if name not in names:
                names.append(name)
        for join_clause in metric.get("join_tables", []) or []:
            for match in _JOIN_TABLE_RE.finditer(str(join_clause)):
                name = match.group(1).split(".")[-1]
                if name not in names:
                    names.append(name)
    return names


def _retry_feedback(sql: str, validation: dict[str, Any]) -> str:
    violations = "\n".join(
        f"- {item.get('code')}: {item.get('message')}"
        for item in validation.get("violations", []) or []
    ) or "- none"
    warnings = "\n".join(
        f"- {item}" for item in validation.get("warnings", []) or []
    ) or "- none"
    return (
        f"Previous SQL:\n{sql}\n\n"
        f"Validation message:\n{validation.get('message', '')}\n\n"
        f"Violations:\n{violations}\n\nWarnings:\n{warnings}"
    )


async def initialize_node(state: GraphState) -> GraphState:
    question = state.get("question", "").strip()
    tenant_id = state.get("tenant_id", "").strip()
    if not question:
        return _error(state, "initialize", "question is empty")
    if not tenant_id:
        return _error(state, "initialize", "tenant_id is empty")
    return {
        "question": question,
        "tenant_id": tenant_id,
        "validation_attempts": 0,
        "rows": [],
        "answer": "",
        "error": "",
        "trace": [{"node": "initialize", "ok": True, "message": "success"}],
    } # type: ignore


async def parse_intent_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        intent = await parse_intent(state["question"], llm=runtime.context.llm)
        return {
            "intent": intent,
            "trace": _trace(state, "parse_intent", ok=True, message="success"),
        } # pyright: ignore[reportReturnType]
    except Exception as exc:
        return _error(state, "parse_intent", exc)


async def search_metrics_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await search_metrics(
            query=state["question"],
            tenant_id=state["tenant_id"],
            embedding_client=runtime.context.embeddings,
        )
        return {
            "metrics_result": result,
            "table_names": _metric_table_names(result),
            "trace": _trace(
                state,
                "search_metrics",
                ok=bool(result.get("ok")),
                message=str(result.get("message", "")),
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "search_metrics", exc)


async def search_schema_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await search_schema(
            query=state["question"],
            tenant_id=state["tenant_id"],
            embedding_client=runtime.context.embeddings,
            table_names=state.get("table_names") or None,
        )
        allowed_tables = [
            str(item["table_name"])
            for item in result.get("schema", []) or []
            if item.get("table_name")
        ]
        update: GraphState = {
            "schema_result": result,
            "allowed_tables": allowed_tables,
            "trace": _trace(
                state,
                "search_schema",
                ok=bool(result.get("ok") and allowed_tables),
                message=str(result.get("message", "")),
            ),
        } # type: ignore
        if not allowed_tables:
            update["error"] = "No authorized schema tables were retrieved."
        return update
    except Exception as exc:
        return _error(state, "search_schema", exc)


async def generate_sql_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        sql = await generate_sql(
            question=state["question"],
            intent=state.get("intent"),
            metrics_result=state.get("metrics_result", {}),
            schema_result=state.get("schema_result", {}),
            retry_feedback=state.get("retry_feedback"),
            llm=runtime.context.llm,
        )
        return {
            "candidate_sql": sql,
            "validated_sql": "",
            "validation_result": {},
            "execution_result": {},
            "rows": [],
            "answer": "",
            "error": "",
            "trace": _trace(state, "generate_sql", ok=True, message="success"),
        } # type: ignore
    except Exception as exc:
        return _error(state, "generate_sql", exc)


async def validate_sql_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    allowed_tables = state.get("allowed_tables") or []
    if not allowed_tables:
        return _error(state, "validate_sql", "allowed_tables is empty")
    try:
        result = await validate_sql(
            sql=state.get("candidate_sql", ""),
            tenant_id=state["tenant_id"],
            allowed_tables=allowed_tables,
            max_limit=runtime.context.max_limit,
        )
        attempts = state.get("validation_attempts", 0) + 1
        ok = result.get("ok") is True
        return {
            "validation_result": result,
            "validation_attempts": attempts,
            "validated_sql": str(result.get("normalized_sql", "")) if ok else "",
            "retry_feedback": "" if ok else _retry_feedback(state.get("candidate_sql", ""), result),
            "execution_result": {},
            "rows": [],
            "answer": "",
            "error": "" if ok else str(result.get("message", "SQL validation failed.")),
            "trace": _trace(
                state,
                "validate_sql",
                ok=ok,
                message=str(result.get("message", "")),
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "validate_sql", exc)


async def execute_sql_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await execute_sql(
            sql=state.get("validated_sql", ""),
            tenant_id=state["tenant_id"],
            dsn=runtime.context.dsn,
            timeout_ms=runtime.context.timeout_ms,
            max_limit=runtime.context.max_limit,
            allowed_tables=state.get("allowed_tables") or [],
        )
        ok = result.get("ok") is True
        return {
            "execution_result": result,
            "rows": result.get("rows", []) if ok else [],
            "error": "" if ok else str(result.get("message", "SQL execution failed.")),
            "trace": _trace(
                state,
                "execute_sql",
                ok=ok,
                message=str(result.get("message", "")),
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "execute_sql", exc)


async def explain_node(
    state: GraphState,
    runtime: Runtime[GraphContext],
) -> GraphState:
    try:
        result = await explain_result(
            question=state["question"],
            sql=state.get("validated_sql", ""),
            rows=state.get("rows", []),
            metrics_result=state.get("metrics_result", {}),
            llm=runtime.context.llm,
        )
        ok = result.get("ok") is True
        return {
            "answer": str(result.get("explanation", "")) if ok else "",
            "error": "" if ok else str(result.get("message", "Explanation failed.")),
            "trace": _trace(
                state,
                "explain",
                ok=ok,
                message=str(result.get("message", "")),
            ),
        } # type: ignore
    except Exception as exc:
        return _error(state, "explain", exc)


async def finalize_node(state: GraphState) -> OutputState:
    intent = state.get("intent")
    sql = state.get("validated_sql", "")
    error = state.get("error", "")
    ok = bool(sql) and not error
    if state.get("execute", False):
        ok = ok and (state.get("execution_result") or {}).get("ok") is True
    return {
        "ok": ok,
        "question": state["question"],
        "tenant_id": state["tenant_id"],
        "intent": asdict(intent) if intent is not None else {},
        "sql": sql,
        "rows": state.get("rows", []),
        "answer": state.get("answer", ""),
        "error": error,
        "trace": state.get("trace", []),
    }
