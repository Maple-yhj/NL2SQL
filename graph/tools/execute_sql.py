from typing import Any

from dotenv import load_dotenv
from graph.tools.validate_sql import validate_sql


async def _connect(dsn: str):
    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise RuntimeError("asyncpg is not installed. Install it with: pip install asyncpg") from exc

    return await asyncpg.connect(dsn)


async def execute_sql(
    sql: str,
    tenant_id: str,
    *,
    dsn: str | None = None,
    timeout_ms: int = 10_000,
    max_limit: int = 1000,
    allowed_tables: list[str] | None = None,
) -> dict[str, Any]:
    if timeout_ms <= 0:
        raise ValueError("Tool[execute_sql]: arg 'timeout_ms' must be positive")

    validation = await validate_sql(
        sql=sql,
        tenant_id=tenant_id,
        allowed_tables=allowed_tables,
        max_limit=max_limit,
    )
    executable_sql = validation.get("normalized_sql", "")

    if not validation["ok"]:
        return {
            "ok": False,
            "sql": sql,
            "normalized_sql": executable_sql,
            "tenant_id": tenant_id,
            "rows": [],
            "row_count": 0,
            "validation": validation,
            "violations": validation["violations"],
            "message": validation["message"],
        }

    if dsn is None:
        import os

        load_dotenv()
        dsn = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")

    if not dsn:
        return {
            "ok": False,
            "sql": sql,
            "normalized_sql": executable_sql,
            "tenant_id": tenant_id,
            "rows": [],
            "row_count": 0,
            "validation": validation,
            "message": "Missing DATABASE_URL or POSTGRES_DSN for SQL execution.",
        }

    try:
        conn = await _connect(dsn)
        try:
            await conn.execute(f"set statement_timeout = {int(timeout_ms)}")
            rows = [dict(row) for row in await conn.fetch(executable_sql)]
        finally:
            await conn.close()
    except Exception as exc:
        return {
            "ok": False,
            "sql": sql,
            "normalized_sql": executable_sql,
            "tenant_id": tenant_id,
            "rows": [],
            "row_count": 0,
            "validation": validation,
            "message": f"SQL execution failed: {exc}",
        }

    return {
        "ok": True,
        "sql": sql,
        "normalized_sql": executable_sql,
        "tenant_id": tenant_id,
        "rows": rows,
        "row_count": len(rows),
        "validation": validation,
        "message": "success",
    }
