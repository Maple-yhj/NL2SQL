# schema_catalog.py
import asyncio, json, asyncpg

TARGET_TABLES = ["orders", "products", "users", "refunds"]

async def extract_schema_catalog(dsn: str) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    catalog = []

    for table_name in TARGET_TABLES:
        # 获取表级注释
        table_comment = await conn.fetchval("""
            SELECT obj_description(oid, 'pg_class')
            FROM pg_class
            WHERE relname = $1 AND relnamespace = 'public'::regnamespace
        """, table_name)

        # 获取字段信息 + 字段注释
        columns_raw = await conn.fetch("""
            SELECT
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default,
                pgd.description AS comment
            FROM information_schema.columns c
            LEFT JOIN pg_catalog.pg_description pgd
                ON pgd.objoid = (
                    SELECT oid FROM pg_class
                    WHERE relname = c.table_name
                      AND relnamespace = 'public'::regnamespace
                )
                AND pgd.objsubid = c.ordinal_position
            WHERE c.table_schema = 'public'
              AND c.table_name = $1
            ORDER BY c.ordinal_position
        """, table_name)

        catalog.append({
            "table": table_name,
            "comment": table_comment or "",
            "columns": [
                {
                    "name":     row["column_name"],
                    "type":     row["data_type"],
                    "nullable": row["is_nullable"] == "YES",
                    "default":  row["column_default"],
                    "comment":  row["comment"] or ""
                }
                for row in columns_raw
            ]
        })

    await conn.close()
    return catalog


async def main():
    DSN = "postgresql://postgres:m13066@localhost:5432/nl2sql_dev"
    catalog = await extract_schema_catalog(DSN)
    
    # 保存为 JSON 文件
    with open("schema_catalog.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 抽取完成，共 {len(catalog)} 张表")
    for t in catalog:
        print(f"  {t['table']}: {len(t['columns'])} 个字段")

asyncio.run(main())