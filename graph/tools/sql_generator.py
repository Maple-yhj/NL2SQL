# agent/sql_generator.py

from __future__ import annotations
import re
from typing import Any

from engine.models import QueryIntent



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
    llm,
    max_output_tokens: int = 2048,
) -> str:
    if not intent:
        raise ValueError("parse intent error, intent is null")

    prompt = build_sql_prompt(question = question, intent = intent, metrics_result = metrics_result, schema_result = schema_result, retry_feedback = retry_feedback)
    raw = await llm.complete(prompt=prompt, system=SQL_SYSTEM, max_output_tokens = max_output_tokens)
    sql = _extract_sql(raw)
    return sql


def build_sql_prompt(
    *,
    question: str,
    intent: QueryIntent,
    metrics_result: dict[str, Any],
    schema_result: dict[str, Any],
    retry_feedback: str | None
) -> str:
    """
    把自然语言问题、意图、指标检索结果、schema 检索结果拼成 prompt。
    """
    
    metrics_content = []
    schemas_content = []

    metrics = metrics_result.get("metrics")
    schemas= schema_result.get("schema")

    if isinstance(metrics, (list)):
       for metric in metrics:
           metric_content = format_metrics_context(metric)
           metrics_content.append(metric_content)

    if isinstance(schemas, (list)):
       for schema in schemas:
           schema_content = format_schema_context(schema)
           schemas_content.append(schema_content)

    metrics_content_str = "\n\n".join(metrics_content)
    schemas_content_str = "\n\n".join(schemas_content)

    prompt = f"""
Question:
{question}

Parsed intent:
metrics: {intent.metrics}
time_range: {intent.time_range}
dimensions: {intent.dimensions}
filters: {intent.filters}

[METRIC CONTEXT]
{metrics_content_str}

[SCHEMA CONTEXT]
{schemas_content_str}
"""
    
    if retry_feedback:
        prompt = prompt + retry_feedback

    return prompt


def format_metrics_context(metrics_result: dict[str, Any]) -> str:
    content = f"""
Metric: {metrics_result.get("metric_name")}
Display name: {metrics_result.get("display_name")}
Business definition: {metrics_result.get("business_def")}
SQL expression: {metrics_result.get("sql_expr")}
Base table: {metrics_result.get("base_table")}
Time column: {metrics_result.get("time_column")}
Default filters: {metrics_result.get("filters")}
Supported dimensions: {metrics_result.get("dimensions")}
Join tables: {metrics_result.get("join_tables")}
Forbidden: {metrics_result.get("forbidden")}
Synonyms: {metrics_result.get("synonyms")}
Retrieval score: {metrics_result.get("score")}
    """
    return content


def format_schema_context(schema_result: dict[str, Any]) -> str:
    columns = schema_result.get("columns")
    columns_str = ""
    if columns:
        for column in columns:
            nullable = "nullable"  if column.get("nullable") else "not null"
            column_line = f"-{column.get("column_name")}|{column.get("data_type")}|{nullable}|{column.get("comment")}|{column.get("sample_values")}\n"
            columns_str = columns_str + column_line


    content = f"""
Table:{schema_result.get("table_name")}
Table Comment: {schema_result.get("table_comment")}
Allowed columns:
{columns_str}
"""
    return content


def _extract_sql(model_text: str) -> str:
    """
    处理模型可能返回的 fenced sql：
    ```sql
    select ...
    ```
    同时去掉首尾空白和分号。
    """
    value = model_text.strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1)
    value = value.strip().rstrip(";").strip()
    if not value:
        raise ValueError("Model returned empty SQL.")
    return re.sub(r"\s+", " ", value)