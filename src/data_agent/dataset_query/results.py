"""Pure rendering helpers for deterministic tabular query results."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal

from data_agent.runtime.models import AgentRow, ChartSpec
from data_agent.tools.schemas import TabularResult

from .models import DatasetQueryPlan


def json_value(value: object):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def tabular_rows(result: TabularResult) -> tuple[AgentRow, ...]:
    return tuple(
        AgentRow(
            root={
                column: json_value(value)
                for column, value in zip(result.columns, item.values, strict=True)
            }
        )
        for item in result.rows
    )


def chart_for_result(
    result: TabularResult,
    *,
    plan: DatasetQueryPlan,
    title: str,
) -> ChartSpec | None:
    if (
        plan.analysis_type != "aggregate"
        or not plan.group_by
        or len(result.columns) < 2
        or not result.rows
    ):
        return None
    x_field = result.columns[0]
    for index, y_field in enumerate(result.columns[1:], start=1):
        if any(
            isinstance(row.values[index], (int, float, Decimal))
            and not isinstance(row.values[index], bool)
            for row in result.rows
        ):
            return ChartSpec(title=title, x_field=x_field, y_field=y_field)
    return None


def answer_for_result(
    result: TabularResult,
    *,
    chart: ChartSpec | None,
) -> str:
    if chart is None or not result.rows:
        return f"查询完成，共返回 {len(result.rows)} 行。"
    x_index = result.columns.index(chart.x_field)
    y_index = result.columns.index(chart.y_field)
    numeric = [
        (row.values[x_index], row.values[y_index])
        for row in result.rows
        if isinstance(row.values[y_index], (int, float, Decimal))
        and not isinstance(row.values[y_index], bool)
    ]
    if not numeric:
        return f"查询完成，共返回 {len(result.rows)} 行。"
    label, value = max(numeric, key=lambda item: float(item[1]))
    return (
        f"在本次返回结果中，{chart.x_field}={label} 的 "
        f"{chart.y_field} 最高，为 {value}；共返回 {len(result.rows)} 行。"
    )


__all__ = ["answer_for_result", "chart_for_result", "json_value", "tabular_rows"]
