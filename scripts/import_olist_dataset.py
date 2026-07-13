from __future__ import annotations

import argparse
import io
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

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
    generated_columns: tuple[ColumnSpec, ...] = ()
    primary_key: tuple[str, ...] = ()


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
        primary_key=("customer_id",),
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
        generated_columns=(
            ColumnSpec("geolocation_row_id", "BIGSERIAL"),
        ),
        primary_key=("geolocation_row_id",),
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
        primary_key=("order_id", "order_item_id"),
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
        primary_key=("order_id", "payment_sequential"),
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
        primary_key=("review_id",),
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
        primary_key=("order_id",),
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
        primary_key=("product_id",),
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
        primary_key=("seller_id",),
    ),
    "product_category_name_translation": TableSpec(
        name="product_category_name_translation",
        csv_name="product_category_name_translation.csv",
        columns=(
            ColumnSpec("product_category_name", "TEXT"),
            ColumnSpec("product_category_name_english", "TEXT"),
        ),
        primary_key=("product_category_name",),
    ),
}


def build_create_table_sql(spec: TableSpec) -> str:
    definitions = [
        f"{column.name} {column.data_type}"
        for column in (*spec.generated_columns, *spec.columns)
    ]
    if spec.primary_key:
        definitions.append(f"PRIMARY KEY ({', '.join(spec.primary_key)})")
    return (
        f"CREATE TABLE IF NOT EXISTS {spec.name} (\n    "
        + ",\n    ".join(definitions)
        + "\n)"
    )


def build_copy_sql(spec: TableSpec) -> str:
    columns = ", ".join(column.name for column in spec.columns)
    return (
        f"COPY {spec.name} ({columns}) "
        "FROM STDIN WITH (FORMAT CSV, HEADER TRUE)"
    )


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


def prepare_schema(conn, *, reset: bool = False) -> None:
    with conn.cursor() as cursor:
        if reset:
            table_names = ", ".join(spec.name for spec in iter_table_specs())
            cursor.execute(f"DROP TABLE IF EXISTS {table_names} CASCADE")
        for spec in iter_table_specs():
            cursor.execute(build_create_table_sql(spec))
        for sql in create_indexes_sql():
            cursor.execute(sql)


def import_zip(zip_path: Path, dsn: str, *, reset: bool = False) -> None:
    import psycopg2

    with psycopg2.connect(dsn) as conn:
        prepare_schema(conn, reset=reset)

        with zipfile.ZipFile(zip_path) as archive:
            for spec in iter_table_specs():
                with archive.open(spec.csv_name) as raw_file:
                    text_file = io.TextIOWrapper(raw_file, encoding="utf-8", newline="")
                    with conn.cursor() as cursor:
                        cursor.copy_expert(build_copy_sql(spec), text_file)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Kaggle OList CSVs into PostgreSQL.")
    parser.add_argument("--zip-path", default="data/olist/brazilian-ecommerce.zip")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--schema-only", action="store_true")
    return parser.parse_args(argv)


def main() -> None:
    load_dotenv()
    args = parse_args()
    dsn = args.dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise ValueError("Missing DATABASE_URL.")
    if args.schema_only:
        import psycopg2

        with psycopg2.connect(dsn) as conn:
            prepare_schema(conn, reset=args.reset)
        return

    import_zip(Path(args.zip_path), dsn, reset=args.reset)


if __name__ == "__main__":
    main()
