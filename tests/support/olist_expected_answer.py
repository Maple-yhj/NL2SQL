from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable


def _format_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        text = value.isoformat()
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_expected_answer(
    columns: Iterable[str],
    rows: Iterable[Iterable[Any]],
) -> str:
    """Render the complete public answer oracle without production providers."""

    column_values = tuple(columns)
    row_values = tuple(tuple(row) for row in rows)
    if not row_values:
        return "验证后的查询未返回结果。"
    answer = f"已基于验证后的查询证据返回 {len(row_values)} 行结果。"
    first_row = ", ".join(
        f"{column}={_format_cell(value)}"
        for column, value in zip(
            column_values,
            row_values[0],
            strict=True,
        )
    )
    return f"{answer} First verified row: {first_row}."
