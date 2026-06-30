from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_TABLES = (
    "olist_customers_dataset",
    "olist_geolocation_dataset",
    "olist_order_items_dataset",
    "olist_order_payments_dataset",
    "olist_order_reviews_dataset",
    "olist_orders_dataset",
    "olist_products_dataset",
    "olist_sellers_dataset",
    "product_category_name_translation",
)


async def extract_schema_catalog(dsn: str, table_names=DEFAULT_TABLES) -> list[dict]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        catalog = []
        for table_name in table_names:
            table_comment = await conn.fetchval(
                """
                SELECT obj_description(oid, 'pg_class')
                FROM pg_class
                WHERE relname = $1 AND relnamespace = 'public'::regnamespace
                """,
                table_name,
            )
            columns = await conn.fetch(
                """
                SELECT c.column_name, c.data_type, c.is_nullable,
                       c.column_default, pgd.description AS comment
                FROM information_schema.columns c
                LEFT JOIN pg_catalog.pg_description pgd
                  ON pgd.objoid = (
                    SELECT oid FROM pg_class
                    WHERE relname = c.table_name
                      AND relnamespace = 'public'::regnamespace
                  )
                 AND pgd.objsubid = c.ordinal_position
                WHERE c.table_schema = 'public' AND c.table_name = $1
                ORDER BY c.ordinal_position
                """,
                table_name,
            )
            catalog.append(
                {
                    "table": table_name,
                    "comment": table_comment or "",
                    "columns": [
                        {
                            "name": row["column_name"],
                            "type": row["data_type"],
                            "nullable": row["is_nullable"] == "YES",
                            "default": row["column_default"],
                            "comment": row["comment"] or "",
                        }
                        for row in columns
                    ],
                }
            )
        return catalog
    finally:
        await conn.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Extract PostgreSQL schema catalog")
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN"))
    parser.add_argument("--output", default="schema_catalog.json")
    parser.add_argument("--tables", nargs="*", default=list(DEFAULT_TABLES))
    args = parser.parse_args()
    if not args.dsn:
        raise ValueError("Missing DATABASE_URL or POSTGRES_DSN.")
    catalog = asyncio.run(extract_schema_catalog(args.dsn, table_names=args.tables))
    Path(args.output).write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
