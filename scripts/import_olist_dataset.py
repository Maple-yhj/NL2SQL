from __future__ import annotations

import argparse
import io
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    data_type: str


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    csv_name: str
    columns: tuple[ColumnSpec, ...]


OLIST_TABLES: dict[str, TableSpec] = {
    "olist_customers_dataset": TableSpec(
        name="olist_customers_dataset",
        csv_name="olist_customers_dataset.csv",
        columns=(
            ColumnSpec("customer_id", "TEXT"),
            ColumnSpec("customer_unique_id", "TEXT"),
            ColumnSpec("customer_zip_code_prefix", "INTEGER"),
            ColumnSpec("customer_city", "TEXT"),
            ColumnSpec("customer_state", "TEXT"),
        ),
    ),
    "olist_geolocation_dataset": TableSpec(
        name="olist_geolocation_dataset",
        csv_name="olist_geolocation_dataset.csv",
        columns=(
            ColumnSpec("geolocation_zip_code_prefix", "INTEGER"),
            ColumnSpec("geolocation_lat", "DOUBLE PRECISION"),
            ColumnSpec("geolocation_lng", "DOUBLE PRECISION"),
            ColumnSpec("geolocation_city", "TEXT"),
            ColumnSpec("geolocation_state", "TEXT"),
        ),
    ),
    "olist_order_items_dataset": TableSpec(
        name="olist_order_items_dataset",
        csv_name="olist_order_items_dataset.csv",
        columns=(
            ColumnSpec("order_id", "TEXT"),
            ColumnSpec("order_item_id", "INTEGER"),
            ColumnSpec("product_id", "TEXT"),
            ColumnSpec("seller_id", "TEXT"),
            ColumnSpec("shipping_limit_date", "TIMESTAMP"),
            ColumnSpec("price", "NUMERIC"),
            ColumnSpec("freight_value", "NUMERIC"),
        ),
    ),
    "olist_order_payments_dataset": TableSpec(
        name="olist_order_payments_dataset",
        csv_name="olist_order_payments_dataset.csv",
        columns=(
            ColumnSpec("order_id", "TEXT"),
            ColumnSpec("payment_sequential", "INTEGER"),
            ColumnSpec("payment_type", "TEXT"),
            ColumnSpec("payment_installments", "INTEGER"),
            ColumnSpec("payment_value", "NUMERIC"),
        ),
    ),
    "olist_order_reviews_dataset": TableSpec(
        name="olist_order_reviews_dataset",
        csv_name="olist_order_reviews_dataset.csv",
        columns=(
            ColumnSpec("review_id", "TEXT"),
            ColumnSpec("order_id", "TEXT"),
            ColumnSpec("review_score", "INTEGER"),
            ColumnSpec("review_comment_title", "TEXT"),
            ColumnSpec("review_comment_message", "TEXT"),
            ColumnSpec("review_creation_date", "TIMESTAMP"),
            ColumnSpec("review_answer_timestamp", "TIMESTAMP"),
        ),
    ),
    "olist_orders_dataset": TableSpec(
        name="olist_orders_dataset",
        csv_name="olist_orders_dataset.csv",
        columns=(
            ColumnSpec("order_id", "TEXT"),
            ColumnSpec("customer_id", "TEXT"),
            ColumnSpec("order_status", "TEXT"),
            ColumnSpec("order_purchase_timestamp", "TIMESTAMP"),
            ColumnSpec("order_approved_at", "TIMESTAMP"),
            ColumnSpec("order_delivered_carrier_date", "TIMESTAMP"),
            ColumnSpec("order_delivered_customer_date", "TIMESTAMP"),
            ColumnSpec("order_estimated_delivery_date", "TIMESTAMP"),
        ),
    ),
    "olist_products_dataset": TableSpec(
        name="olist_products_dataset",
        csv_name="olist_products_dataset.csv",
        columns=(
            ColumnSpec("product_id", "TEXT"),
            ColumnSpec("product_category_name", "TEXT"),
            ColumnSpec("product_name_lenght", "INTEGER"),
            ColumnSpec("product_description_lenght", "INTEGER"),
            ColumnSpec("product_photos_qty", "INTEGER"),
            ColumnSpec("product_weight_g", "INTEGER"),
            ColumnSpec("product_length_cm", "INTEGER"),
            ColumnSpec("product_height_cm", "INTEGER"),
            ColumnSpec("product_width_cm", "INTEGER"),
        ),
    ),
    "olist_sellers_dataset": TableSpec(
        name="olist_sellers_dataset",
        csv_name="olist_sellers_dataset.csv",
        columns=(
            ColumnSpec("seller_id", "TEXT"),
            ColumnSpec("seller_zip_code_prefix", "INTEGER"),
            ColumnSpec("seller_city", "TEXT"),
            ColumnSpec("seller_state", "TEXT"),
        ),
    ),
    "product_category_name_translation": TableSpec(
        name="product_category_name_translation",
        csv_name="product_category_name_translation.csv",
        columns=(
            ColumnSpec("product_category_name", "TEXT"),
            ColumnSpec("product_category_name_english", "TEXT"),
        ),
    ),
}


def build_create_table_sql(spec: TableSpec) -> str:
    columns = ",\n    ".join(
        f"{column.name} {column.data_type}" for column in spec.columns
    )
    return f"CREATE TABLE IF NOT EXISTS {spec.name} (\n    {columns}\n)"


def build_copy_sql(spec: TableSpec) -> str:
    columns = ", ".join(column.name for column in spec.columns)
    return (
        f"COPY {spec.name} ({columns}) "
        "FROM STDIN WITH (FORMAT CSV, HEADER TRUE)"
    )


def support_table_sql(embedding_dim: int) -> list[str]:
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""
CREATE TABLE IF NOT EXISTS semantic_index (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_key TEXT NOT NULL,
    source_table TEXT,
    source_id BIGINT,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    embedding vector({embedding_dim}) NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, object_type, object_key)
)
""".strip(),
        """
CREATE INDEX IF NOT EXISTS idx_semantic_index_lookup
    ON semantic_index (tenant_id, object_type, is_active)
""".strip(),
        """
CREATE TABLE IF NOT EXISTS metrics_registry (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    business_def TEXT NOT NULL,
    sql_expr TEXT NOT NULL,
    base_table TEXT NOT NULL,
    time_column TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    dimensions TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    join_tables TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    filters TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    forbidden TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    synonyms TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, metric_name)
)
""".strip(),
    ]


def olist_metric_sql() -> str:
    return """
INSERT INTO metrics_registry (
    tenant_id, metric_name, display_name, business_def, sql_expr,
    base_table, time_column, dimensions, join_tables, filters, forbidden, synonyms
)
VALUES
(
    'admin', 'gmv', 'GMV',
    'Gross merchandise value from OList order items: item price plus freight value.',
    'SUM(olist_order_items_dataset.price + olist_order_items_dataset.freight_value)',
    'olist_order_items_dataset', 'shipping_limit_date',
    ARRAY['seller_id', 'product_id', 'order_id', 'shipping_limit_date']::TEXT[],
    ARRAY[]::TEXT[], ARRAY[]::TEXT[], ARRAY[]::TEXT[],
    ARRAY['sales', 'revenue', 'gross merchandise value']::TEXT[]
),
(
    'admin', 'orders', 'Orders',
    'Distinct OList orders associated with order items.',
    'COUNT(DISTINCT olist_order_items_dataset.order_id)',
    'olist_order_items_dataset', 'shipping_limit_date',
    ARRAY['seller_id', 'product_id', 'shipping_limit_date']::TEXT[],
    ARRAY[]::TEXT[], ARRAY[]::TEXT[], ARRAY[]::TEXT[],
    ARRAY['order count', 'paid orders']::TEXT[]
),
(
    'admin', 'avg_item_price', 'Average Item Price',
    'Average item price from OList order items.',
    'AVG(olist_order_items_dataset.price)',
    'olist_order_items_dataset', 'shipping_limit_date',
    ARRAY['seller_id', 'product_id', 'shipping_limit_date']::TEXT[],
    ARRAY[]::TEXT[], ARRAY[]::TEXT[], ARRAY[]::TEXT[],
    ARRAY['average price', 'item price']::TEXT[]
),
(
    'admin', 'avg_review_score', 'Average Review Score',
    'Average customer review score for orders connected to OList sellers.',
    'AVG(olist_order_reviews_dataset.review_score)',
    'olist_order_reviews_dataset',
    'review_creation_date',
    ARRAY['review_creation_date', 'seller_id']::TEXT[],
    ARRAY['JOIN olist_order_items_dataset oi ON oi.order_id = olist_order_reviews_dataset.order_id']::TEXT[],
    ARRAY[]::TEXT[], ARRAY[]::TEXT[],
    ARRAY['review score', 'rating']::TEXT[]
)
ON CONFLICT (tenant_id, metric_name)
DO UPDATE SET
    display_name = EXCLUDED.display_name,
    business_def = EXCLUDED.business_def,
    sql_expr = EXCLUDED.sql_expr,
    base_table = EXCLUDED.base_table,
    time_column = EXCLUDED.time_column,
    dimensions = EXCLUDED.dimensions,
    join_tables = EXCLUDED.join_tables,
    filters = EXCLUDED.filters,
    forbidden = EXCLUDED.forbidden,
    synonyms = EXCLUDED.synonyms,
    is_active = true,
    updated_at = now()
""".strip()


def create_indexes_sql() -> list[str]:
    return [
        "CREATE INDEX IF NOT EXISTS idx_olist_items_seller ON olist_order_items_dataset (seller_id)",
        "CREATE INDEX IF NOT EXISTS idx_olist_items_order ON olist_order_items_dataset (order_id)",
        "CREATE INDEX IF NOT EXISTS idx_olist_items_product ON olist_order_items_dataset (product_id)",
        "CREATE INDEX IF NOT EXISTS idx_olist_orders_customer ON olist_orders_dataset (customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_olist_payments_order ON olist_order_payments_dataset (order_id)",
        "CREATE INDEX IF NOT EXISTS idx_olist_reviews_order ON olist_order_reviews_dataset (order_id)",
        "CREATE INDEX IF NOT EXISTS idx_olist_products_product ON olist_products_dataset (product_id)",
        "CREATE INDEX IF NOT EXISTS idx_olist_sellers_seller ON olist_sellers_dataset (seller_id)",
    ]


def iter_table_specs() -> Iterable[TableSpec]:
    return OLIST_TABLES.values()


def prepare_schema(conn, *, reset: bool = False, embedding_dim: int = 768) -> None:
    with conn.cursor() as cursor:
        if reset:
            table_names = ", ".join(spec.name for spec in iter_table_specs())
            cursor.execute(f"DROP TABLE IF EXISTS {table_names} CASCADE")
        for sql in support_table_sql(embedding_dim):
            cursor.execute(sql)
        for spec in iter_table_specs():
            cursor.execute(build_create_table_sql(spec))
        for sql in create_indexes_sql():
            cursor.execute(sql)
        cursor.execute(olist_metric_sql())


def import_zip(zip_path: Path, dsn: str, *, reset: bool = False, embedding_dim: int = 768) -> None:
    import psycopg2

    with psycopg2.connect(dsn) as conn:
        prepare_schema(conn, reset=reset, embedding_dim=embedding_dim)

        with zipfile.ZipFile(zip_path) as archive:
            for spec in iter_table_specs():
                with archive.open(spec.csv_name) as raw_file:
                    text_file = io.TextIOWrapper(raw_file, encoding="utf-8", newline="")
                    with conn.cursor() as cursor:
                        cursor.copy_expert(build_copy_sql(spec), text_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Kaggle OList CSVs into PostgreSQL.")
    parser.add_argument("--zip-path", default="data/olist/brazilian-ecommerce.zip")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--schema-only", action="store_true")
    parser.add_argument("--embedding-dim", type=int, default=768)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    dsn = args.dsn or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
    if not dsn:
        raise ValueError("Missing DATABASE_URL or POSTGRES_DSN.")
    if args.schema_only:
        import psycopg2

        with psycopg2.connect(dsn) as conn:
            prepare_schema(conn, reset=args.reset, embedding_dim=args.embedding_dim)
        return

    import_zip(Path(args.zip_path), dsn, reset=args.reset, embedding_dim=args.embedding_dim)


if __name__ == "__main__":
    main()
