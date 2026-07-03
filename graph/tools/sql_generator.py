from __future__ import annotations

import re
from typing import Any

from core.llm import LLMProtocol
from engine.models import QueryIntent
from graph.data_memory import format_data_memories
from graph.tools.tenant_scope import format_tenant_scope_context


SQL_SYSTEM = """
You generate safe PostgreSQL SELECT SQL for a BI NL2SQL engine.
Rules:
- Return only one SQL statement.
- Use only tables and columns shown in the schema context.
- Generate read-only SELECT or WITH ... SELECT statements only.
- Prefer metric definitions from METRIC CONTEXT.
- Include GROUP BY for every non-aggregate selected dimension.
- Do not include markdown, explanations, DDL, DML, comments, or semicolon.
""".strip()


async def generate_sql(
    *,
    question: str,
    intent: QueryIntent | None,
    metrics_result: dict[str, Any],
    schema_result: dict[str, Any],
    retry_feedback: str | None,
    tenant_id: str = "admin",
    conversation_history: list[dict[str, Any]] | None = None,
    user_memories: list[dict[str, Any]] | None = None,
    data_memories: list[dict[str, Any]] | None = None,
    plan_context: str | None = None,
    llm: LLMProtocol,
    max_output_tokens: int = 2048,
) -> str:
    if intent is None:
        raise ValueError("Tool[generate_sql]: intent is required")

    prompt = build_sql_prompt(
        question=question,
        intent=intent,
        metrics_result=metrics_result,
        schema_result=schema_result,
        retry_feedback=retry_feedback,
        tenant_id=tenant_id,
        conversation_history=conversation_history or [],
        user_memories=user_memories or [],
        data_memories=data_memories or [],
        plan_context=plan_context,
    )
    raw = await llm.complete(
        prompt=prompt,
        system=SQL_SYSTEM,
        max_output_tokens=max_output_tokens,
    )
    return _extract_sql(raw)


def build_sql_prompt(
    *,
    question: str,
    intent: QueryIntent,
    metrics_result: dict[str, Any],
    schema_result: dict[str, Any],
    retry_feedback: str | None,
    tenant_id: str = "admin",
    conversation_history: list[dict[str, Any]] | None = None,
    user_memories: list[dict[str, Any]] | None = None,
    data_memories: list[dict[str, Any]] | None = None,
    plan_context: str | None = None,
) -> str:
    metrics_content = "\n\n".join(
        format_metrics_context(metric)
        for metric in metrics_result.get("metrics", []) or []
    )
    schema_content = "\n\n".join(
        format_schema_context(schema)
        for schema in schema_result.get("schema", []) or []
    )
    conversation_content = format_conversation_context(
        conversation_history or [],
        user_memories or [],
    )
    data_memory_content = format_data_memories(data_memories or [])
    tenant_scope_content = format_tenant_scope_context(tenant_id)

    prompt = f"""
Question:
{question}

Parsed intent:
metrics: {intent.metrics}
time_range: {intent.time_range}
dimensions: {intent.dimensions}
filters: {intent.filters}

[METRIC CONTEXT]
{metrics_content}

[SCHEMA CONTEXT]
{schema_content}
""".strip()
    if plan_context:
        prompt += f"\n\n[PLAN DSL]\n{plan_context}"
    if tenant_scope_content:
        prompt += f"\n\n[TENANT SCOPE]\n{tenant_scope_content}"
    if data_memory_content:
        prompt += f"\n\n[DATA AGENT MEMORY]\n{data_memory_content}"
    if conversation_content:
        prompt += f"\n\n[CONVERSATION CONTEXT]\n{conversation_content}"
    if retry_feedback:
        prompt += f"\n\n[VALIDATION FEEDBACK]\n{retry_feedback}"
    return prompt


def format_conversation_context(
    conversation_history: list[dict[str, Any]],
    user_memories: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if conversation_history:
        parts.append("Recent turns:")
        for item in conversation_history:
            role = str(item.get("role") or "unknown")
            content = str(item.get("content") or "").strip()
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            sql = str(metadata.get("sql") or "").strip()
            suffix = f" | SQL: {sql}" if sql else ""
            parts.append(f"- {role}: {content}{suffix}")
    if user_memories:
        parts.append("User memories:")
        for item in user_memories:
            key = str(item.get("memory_key") or "").strip()
            value = str(item.get("memory_value") or "").strip()
            if key or value:
                parts.append(f"- {key}: {value}" if key else f"- {value}")
    return "\n".join(parts)


def format_metrics_context(metric: dict[str, Any]) -> str:
    return f"""
Metric: {metric.get('metric_name')}
Display name: {metric.get('display_name')}
Business definition: {metric.get('business_def')}
SQL expression: {metric.get('sql_expr')}
Base table: {metric.get('base_table')}
Time column: {metric.get('time_column')}
Default filters: {metric.get('filters')}
Supported dimensions: {metric.get('dimensions')}
Join tables: {metric.get('join_tables')}
Forbidden: {metric.get('forbidden')}
Synonyms: {metric.get('synonyms')}
Retrieval score: {metric.get('score')}
""".strip()


def format_schema_context(schema: dict[str, Any]) -> str:
    columns = []
    for column in schema.get("columns", []) or []:
        nullable = "nullable" if column.get("nullable") else "not null"
        columns.append(
            "- {name} | {data_type} | {nullable} | {comment} | samples={samples}".format(
                name=column.get("column_name"),
                data_type=column.get("data_type"),
                nullable=nullable,
                comment=column.get("comment"),
                samples=column.get("sample_values"),
            )
        )
    return (
        f"Table: {schema.get('table_name')}\n"
        f"Table comment: {schema.get('table_comment')}\n"
        f"Allowed columns:\n{chr(10).join(columns)}"
    )


def _extract_sql(model_text: str) -> str:
    value = model_text.strip()
    fenced = re.search(
        r"```(?:sql)?\s*(.*?)\s*```",
        value,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        value = fenced.group(1)
    value = value.strip().rstrip(";").strip()
    if not value:
        raise ValueError("Model returned empty SQL.")
    return re.sub(r"\s+", " ", value)
