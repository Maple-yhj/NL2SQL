from __future__ import annotations

import json
from typing import Any

from core.llm import LLMProtocol


EXPLAIN_SYSTEM = """
You are a BI analyst explaining an executed PostgreSQL query result.
Answer the user's question directly and concisely.
Use only the supplied rows and metric context.
Do not invent causes, trends, or values that are not present in the data.
If rows are empty, state that the query returned no data.
""".strip()


async def explain_result(
    *,
    question: str,
    sql: str,
    rows: list[dict[str, Any]],
    metrics_result: dict[str, Any],
    llm: LLMProtocol,
    max_preview_rows: int = 10,
) -> dict[str, Any]:
    if not question.strip():
        raise ValueError("Tool[explain_result]: question is empty")
    if not sql.strip():
        raise ValueError("Tool[explain_result]: sql is empty")
    if max_preview_rows <= 0:
        raise ValueError("Tool[explain_result]: max_preview_rows must be positive")

    preview = rows[:max_preview_rows]
    prompt = json.dumps(
        {
            "question": question,
            "sql": sql,
            "row_count": len(rows),
            "rows": preview,
            "metrics": metrics_result.get("metrics", []),
        },
        ensure_ascii=False,
        default=str,
        indent=2,
    )
    explanation = await llm.complete(
        prompt=prompt,
        system=EXPLAIN_SYSTEM,
        max_output_tokens=1024,
    )
    if not explanation:
        return {
            "ok": False,
            "explanation": "",
            "message": "The model returned an empty explanation.",
        }
    return {
        "ok": True,
        "explanation": explanation,
        "row_count": len(rows),
        "preview_rows": preview,
        "message": "success",
    }
