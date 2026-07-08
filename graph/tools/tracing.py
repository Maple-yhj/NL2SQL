from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import sqlglot


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def summarize_tool_payload(tool_name: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    summary: dict[str, Any] = {
        "tool": tool_name,
        "keys": sorted(str(key) for key in payload),
    }
    sql = _first_sql_value(payload)
    if sql:
        summary["sql"] = _summarize_sql(sql)
    rows = payload.get("rows")
    if isinstance(rows, list):
        summary["row_count"] = len(rows)
    return summary


def build_tool_trace_event(
    *,
    tool_name: str,
    canonical_name: str,
    started_at: str,
    duration_ms: float,
    ok: bool,
    error_code: str = "",
    input_summary: dict[str, Any] | None = None,
    output_summary: dict[str, Any] | None = None,
    retry_count: int = 0,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "canonical_name": canonical_name,
        "started_at": started_at,
        "duration_ms": round(duration_ms, 3),
        "ok": ok,
        "error_code": error_code,
        "input_summary": input_summary or {},
        "output_summary": output_summary or {},
        "retry_count": retry_count,
    }


def _first_sql_value(payload: dict[str, Any]) -> str:
    for key in ("sql", "candidate_sql", "validated_sql", "normalized_sql", "executable_sql"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _summarize_sql(sql: str) -> dict[str, Any]:
    stripped = sql.strip()
    statement_type = "UNKNOWN"
    tables: list[str] = []
    try:
        expression = sqlglot.parse_one(stripped.rstrip(";"), read="postgres")
        statement_type = expression.key.upper()
        tables = sorted({table.name for table in expression.find_all(sqlglot.exp.Table)})
    except Exception:
        pass
    return {
        "statement_type": statement_type,
        "tables": tables,
        "char_count": len(sql),
        "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16],
    }
