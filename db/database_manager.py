import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Any

# 数据库连接配置
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "nl2sql_dev",
    "user": "postgres",
    "password": "m13066",
}


def execute_query(
    sql: str,
    params: tuple[Any, ...] | None = None,
    fetchone: bool = False,
    as_dict: bool = True,
) -> list[dict] | dict | None:
    """
    执行 SQL 查询并返回结果。

    Args:
        sql:      SQL 查询语句，参数占位符使用 %s
        params:   SQL 参数元组，默认 None
        fetchone: True 只返回第一行,False 返回所有行，默认 False
        as_dict:  True 返回字典格式,False 返回元组格式，默认 True

    Returns:
        fetchone=True  → 单行字典/元组 或 None
        fetchone=False → 字典/元组列表

    Raises:
        psycopg2.Error: 数据库操作异常
    """
    cursor_factory = RealDictCursor if as_dict else None

    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor(cursor_factory=cursor_factory) as cur:
                cur.execute(sql, params)
                result = cur.fetchone() if fetchone else cur.fetchall()
                return dict(result) if fetchone and as_dict and result else result

    except psycopg2.Error as e:
        print(f"[DB Error] {e}")
        raise