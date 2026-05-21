from __future__ import annotations

from typing import Any
import re
from core.stream_chat import GeminiLLM
from agent.sql_generator import generate_sql
from engine.intent_parser import parse_intent

from agent.tools.sql_store import search_metrics, search_schema
from agent.tools.execute_sql import execute_sql
from agent.tools.explain_result import explain_result
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
    execution: dict[str, Any] | None = None,
    explanation_result: dict[str, Any] | None = None,
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
        "execution": execution or {},
        "explanation_result": explanation_result or {},
        "explanation": "",
        "executed_sql": "",
        "rows": [],
        "message": message,
    }


def _build_retry_feedback(sql: str, validation: dict[str, Any]) -> str:
    violations = validation.get("violations", [])
    warnings = validation.get("warnings", [])

    violation_lines = [
        f"- {item.get('code')}: {item.get('message')}"
        for item in violations
    ]

    warning_lines = [f"- {warning}" for warning in warnings]

    return f"""
Previous SQL:
{sql}

Validation message:
{validation.get("message", "")}

Violations:
{chr(10).join(violation_lines) or "- none"}

Warnings:
{chr(10).join(warning_lines) or "- none"}
""".strip()


def _extract_base_table(base_table: str) -> str | None:
    if not base_table:
        return None

    first_token = base_table.strip().split()[0]
    return first_token.split(".")[-1]

def _extract_join_tables(join_tables) -> list[str]:
    JOIN_TABLE_RE = re.compile(
    r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_\.]*)",
    re.IGNORECASE,
    )

    if not join_tables:
        return []

    result = []

    for join_clause in join_tables:
        for match in JOIN_TABLE_RE.finditer(join_clause):
            table_name = match.group(1).split(".")[-1]
            result.append(table_name)

    return result

def _extract_metric_table_names(metrics_result: dict) -> list[str]:
    table_names = []

    for metric in metrics_result.get("metrics", []):
        base_table = _extract_base_table(metric.get("base_table"))
        if base_table:
            table_names.append(base_table)

        table_names.extend(_extract_join_tables(metric.get("join_tables")))

    return list(dict.fromkeys(table_names))



async def run_agent_nl2sql(
    question: str,
    tenant_id: str = "demo",
    *,
    execute: bool = False,
    llm = None,
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

    table_names = _extract_metric_table_names(metrics_result)

    #获取涉及的表格和字段
    schema_query = _build_schema_query(question, metrics_result)

    schema_result = await search_schema(
        query = schema_query,
        tenant_id = tenant_id,
        table_names = table_names,
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

    intent = await parse_intent(question, llm=llm)

    sql = ""
    validation = {}
    retry_feedback = None

    for attempt in range(2):
        sql = await generate_sql(
            question=question,
            intent=intent,
            metrics_result=metrics_result,
            schema_result=schema_result,
            llm=llm,
            retry_feedback=retry_feedback,
        )

        validation = await validate_sql(
            sql=sql,
            tenant_id=tenant_id,
            allowed_tables=allowed_tables,
            max_limit=max_limit,
        )

        if validation["ok"]:
            break

        retry_feedback = _build_retry_feedback(sql, validation)
    

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

    execution_result: dict[str, Any] = {}
    explanation_result: dict[str, Any] = {}
    explanation = ""
    rows = []

    if execute:
        execution_result = await execute_sql(
            sql=executed_sql,
            tenant_id=tenant_id,
            dsn=dsn,
            max_limit=max_limit,
            allowed_tables=allowed_tables,
        )
        if not execution_result["ok"]:
            return _failed_result(
                question=question,
                tenant_id=tenant_id,
                metrics_result=metrics_result,
                schema_result=schema_result,
                sql=sql,
                validation=validation,
                execution=execution_result,
                message=execution_result.get("message", "SQL execution failed."),
            )
        rows = execution_result["rows"]
        explanation_result = await explain_result(
            question=question,
            sql=executed_sql,
            rows=rows,
            metrics_result=metrics_result,
            llm=llm,
        )
        if not explanation_result["ok"]:
            return _failed_result(
                question=question,
                tenant_id=tenant_id,
                metrics_result=metrics_result,
                schema_result=schema_result,
                sql=sql,
                validation=validation,
                execution=execution_result,
                explanation_result=explanation_result,
                message=explanation_result.get("message", "SQL result explanation failed."),
            )
        explanation = explanation_result["explanation"]

    return {
        "ok": True,
        "question": question,
        "tenant_id": tenant_id,
        "metrics": metrics_result,
        "schema": schema_result,
        "intent": intent,
        "sql": sql,
        "validation": validation,
        "execution": execution_result,
        "explanation_result": explanation_result,
        "explanation": explanation,
        "executed_sql": executed_sql,
        "rows": rows,
        "message": "success",
    }
