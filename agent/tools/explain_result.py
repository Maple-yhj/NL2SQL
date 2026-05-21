from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_PREVIEW_ROWS = 5
DEFAULT_PREVIEW_COLUMNS = 6


def _require_non_empty(value: str, arg_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"Tool[explain_result]: arg '{arg_name}' is empty")


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("Tool[explain_result]: arg 'rows' must be a list")

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Tool[explain_result]: every row must be a mapping")
        normalized_rows.append(dict(row))

    return normalized_rows


def _extract_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []

    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)

    return columns


def _extract_metric_names(metrics_result: dict[str, Any]) -> list[str]:
    metric_names: list[str] = []

    for metric in metrics_result.get("metrics", []) if metrics_result else []:
        if not isinstance(metric, Mapping):
            continue

        metric_name = metric.get("display_name") or metric.get("metric_name")
        if metric_name and metric_name not in metric_names:
            metric_names.append(str(metric_name))

    return metric_names


def _format_value(value: Any) -> str:
    if value is None:
        return "NULL"
    return str(value)


def _format_row(row: dict[str, Any], columns: list[str]) -> str:
    visible_columns = columns[:DEFAULT_PREVIEW_COLUMNS]
    return ", ".join(
        f"{column}={_format_value(row.get(column))}"
        for column in visible_columns
        if column in row
    )


def _build_explanation(
    *,
    row_count: int,
    columns: list[str],
    preview_rows: list[dict[str, Any]],
    metric_names: list[str],
) -> str:
    if row_count == 0:
        return "查询执行成功，但没有返回数据。可以检查筛选条件、时间范围或关联条件是否过窄。"

    parts = [f"查询返回 {row_count} 行。"]

    if metric_names:
        parts.append(f"涉及指标：{', '.join(metric_names)}。")

    if columns:
        parts.append(f"字段包括：{', '.join(columns)}。")

    preview_text = "; ".join(
        _format_row(row, columns)
        for row in preview_rows
    )
    if preview_text:
        parts.append(f"前 {len(preview_rows)} 行示例：{preview_text}。")

    return "".join(parts)


async def explain_result(
    *,
    question: str,
    sql: str,
    rows: list[dict[str, Any]],
    metrics_result: dict[str, Any],
    llm: Any = None,
    max_preview_rows: int = DEFAULT_PREVIEW_ROWS,
) -> dict[str, Any]:
    _require_non_empty(question, "question")
    _require_non_empty(sql, "sql")
    if max_preview_rows <= 0:
        raise ValueError("Tool[explain_result]: arg 'max_preview_rows' must be positive")

    normalized_rows = _normalize_rows(rows)
    columns = _extract_columns(normalized_rows)
    preview_rows = normalized_rows[:max_preview_rows]
    metric_names = _extract_metric_names(metrics_result)

    explanation = _build_explanation(
        row_count=len(normalized_rows),
        columns=columns,
        preview_rows=preview_rows,
        metric_names=metric_names,
    )

    return {
        "ok": True,
        "question": question,
        "sql": sql,
        "row_count": len(normalized_rows),
        "columns": columns,
        "preview_rows": preview_rows,
        "explanation": explanation,
        "message": "success",
    }
