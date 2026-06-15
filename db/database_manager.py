from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv


def execute_query(
    sql: str,
    params: tuple[Any, ...] | None = None,
    *,
    fetchone: bool = False,
    as_dict: bool = True,
    dsn: str | None = None,
):
    load_dotenv()
    dsn = dsn or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
    if not dsn:
        raise ValueError("Missing DATABASE_URL or POSTGRES_DSN.")

    import psycopg2
    from psycopg2.extras import RealDictCursor

    cursor_factory = RealDictCursor if as_dict else None
    with psycopg2.connect(dsn) as conn:
        with conn.cursor(cursor_factory=cursor_factory) as cursor:
            cursor.execute(sql, params)
            result = cursor.fetchone() if fetchone else cursor.fetchall()
            return dict(result) if fetchone and as_dict and result else result
