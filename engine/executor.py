import re
from typing import Any

from core.google_client import load_env_file


FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|truncate|create|grant|revoke|copy|call|execute)\b",
    re.IGNORECASE,
)


def assert_readonly_sql(sql: str) -> None:
    normalized = sql.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", normalized, flags=re.IGNORECASE):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    if FORBIDDEN_SQL.search(normalized):
        raise ValueError("Only read-only SQL is allowed.")
    if ";" in normalized:
        raise ValueError("Multiple SQL statements are not allowed.")


def ensure_limit(sql: str, limit: int = 100) -> str:
    normalized = sql.strip().rstrip(";")
    if re.search(r"\blimit\s+\d+\b", normalized, flags=re.IGNORECASE):
        return normalized
    return f"{normalized} limit {limit}"


async def execute_readonly_sql(sql: str, dsn: str | None = None, timeout_ms: int = 10_000) -> list[dict[str, Any]]:
    load_env_file()
    if dsn is None:
        import os

        dsn = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
    if not dsn:
        raise ValueError("Missing DATABASE_URL or POSTGRES_DSN for SQL execution.")

    assert_readonly_sql(sql)

    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise RuntimeError("asyncpg is not installed. Install it with: pip install asyncpg") from exc

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"set statement_timeout = {int(timeout_ms)}")
        rows = await conn.fetch(sql)
        return [dict(row) for row in rows]
    finally:
        await conn.close()
