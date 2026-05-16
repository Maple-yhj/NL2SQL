from __future__ import annotations

from typing import Any

from catalog.loader import load_schema_catalog
from core.stream_chat import GeminiLLM
from engine.executor import execute_readonly_sql
from engine.intent_parser import parse_intent
from engine.metrics import MetricRegistry
from engine.sql_generator import generate_sql

from agent.tools.sql_store import search_metrics, search_schema
from agent.tools.validate_sql import validate_sql


def _build_schema_query(question: str, metrics_result: dict[str, Any]) -> str:
    parts = [question]

    for metric in metrics_result.get("metrics", []):
        parts.extend(
            [
                str(metric.get("metric_name", "")),
                str(metric.get("display_name", "")),
                str(metric.get("business_def", "")),
                str(metric.get("base_table", "")),
                " ".join(metric.get("dimensions", []) or []),
                " ".join(metric.get("filters", []) or []),
                " ".join(metric.get("join_tables", []) or []),
            ]
        )

    return " ".join(part for part in parts if part).strip()


def _extract_allowed_tables(schema_result: dict[str, Any]) -> list[str]:
    tables: list[str] = []

    for table in schema_result.get("schema", []):
        table_name = table.get("table_name")
        if table_name and table_name not in tables:
            tables.append(table_name)

    return tables


def _failed_result(
    *,
    question: str,
    tenant_id: str,
    metrics_result: dict[str, Any] | None = None,
    schema_result: dict[str, Any] | None = None,
    sql: str = "",
    validation: dict[str, Any] | None = None,
    message: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "question": question,
        "tenant_id": tenant_id,
        "metrics": metrics_result or {},
        "schema": schema_result or {},
        "sql": sql,
        "validation": validation or {},
        "executed_sql": "",
        "rows": [],
        "message": message,
    }


async def run_agent_nl2sql(
    question: str,
    tenant_id: str = "demo",
    *,
    execute: bool = False,
    catalog_path: str = "schema_catalog.json",
    llm=None,
    dsn: str | None = None,
    max_limit: int = 1000,
) -> dict[str, Any]:
    if not question or not question.strip():
        raise ValueError("question is empty")
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id is empty")

    llm = llm or GeminiLLM()

    # 获取涉及的业务指标
    metrics_result = await search_metrics(
        query=question,
        tenant_id=tenant_id,
        top_k=3,
    )

    #获取涉及的表格和字段
    schema_query = _build_schema_query(question, metrics_result)

    schema_result = await search_schema(
        query=schema_query,
        tenant_id=tenant_id,
        top_k=8,
    )

    allowed_tables = _extract_allowed_tables(schema_result)

    if not allowed_tables:
        return _failed_result(
            question=question,
            tenant_id=tenant_id,
            metrics_result=metrics_result,
            schema_result=schema_result,
            message="No allowed tables were found from schema retrieval.",
        )

    catalog = load_schema_catalog(catalog_path)
    metric_registry = MetricRegistry.default()
    intent = await parse_intent(question, llm=llm)

    sql = await generate_sql(
        question=question,
        intent=intent,
        catalog=catalog,
        metrics=metric_registry,
        llm=llm,
    )

    validation = await validate_sql(
        sql=sql,
        tenant_id=tenant_id,
        allowed_tables=allowed_tables,
        max_limit=max_limit,
    )

    if not validation["ok"]:
        return _failed_result(
            question=question,
            tenant_id=tenant_id,
            metrics_result=metrics_result,
            schema_result=schema_result,
            sql=sql,
            validation=validation,
            message="SQL validation failed.",
        )

    executed_sql = validation["normalized_sql"]

    rows = await execute_readonly_sql(executed_sql, dsn=dsn) if execute else []

    return {
        "ok": True,
        "question": question,
        "tenant_id": tenant_id,
        "metrics": metrics_result,
        "schema": schema_result,
        "intent": intent,
        "sql": sql,
        "validation": validation,
        "executed_sql": executed_sql,
        "rows": rows,
        "message": "success",
    }
