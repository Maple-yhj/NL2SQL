from __future__ import annotations

import json
from typing import Any

from core.llm import LLMProtocol
from graph.tools.explanation_sanitizer import sanitize_explanation


EXPLAIN_TABLE_SYSTEM = """
You are a BI analyst explaining detailed SQL result rows.
The frontend table paginates all returned rows, so do not list records row by row.
Do not create a Markdown table and do not repeat complete field values that are already in the table.
If your answer would only restate table cell values, return an empty string.
Do not say only a subset is available, do not say only preview/sample rows are shown, and do not tell users to query the database for the complete records.
row_count is the complete number of rows returned to the frontend for this answer.
preview_rows are not a display limit; they are only the rows included in this prompt for your analysis.
Provide 1-3 concise paragraphs of data insights only.
Focus on trends, ranges, concentration, outliers, time span, and notable patterns supported by the rows.
If the rows are insufficient for trend analysis, say that the current records are insufficient to identify a trend.
Do not invent causes, trends, or values that are not present in the data.
If rows are empty, state that the query returned no data.
""".strip()


async def explain_table_result(
    *,
    question: str,
    sql: str,
    rows: list[dict[str, Any]],
    metrics_result: dict[str, Any],
    llm: LLMProtocol,
    max_preview_rows: int = 20,
) -> dict[str, Any]:
    if not question.strip():
        raise ValueError("Tool[explain_table_result]: question is empty")
    if not sql.strip():
        raise ValueError("Tool[explain_table_result]: sql is empty")
    if max_preview_rows <= 0:
        raise ValueError("Tool[explain_table_result]: max_preview_rows must be positive")

    preview = rows[:max_preview_rows]
    prompt = json.dumps(
        {
            "question": question,
            "sql": sql,
            "row_count": len(rows),
            "preview_rows": preview,
            "metrics": metrics_result.get("metrics", []),
            "instruction": (
                "Analyze the detailed rows for trends and notable patterns. "
                "Do not repeat rows one by one because the frontend table displays all returned rows with pagination. "
                "Return an empty string when there is no insight beyond the table values."
            ),
        },
        ensure_ascii=False,
        default=str,
        indent=2,
    )
    explanation = await llm.complete(
        prompt=prompt,
        system=EXPLAIN_TABLE_SYSTEM,
        max_output_tokens=1024,
    )
    explanation = sanitize_explanation(explanation, row_count=len(rows), rows=rows)
    return {
        "ok": True,
        "explanation": explanation,
        "row_count": len(rows),
        "preview_rows": preview,
        "message": "success",
    }
