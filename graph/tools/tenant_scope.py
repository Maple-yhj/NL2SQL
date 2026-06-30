from __future__ import annotations

import sqlglot
from sqlglot import exp


ADMIN_TENANT_ID = "admin"
CATALOG_TENANT_ID = ADMIN_TENANT_ID

_ORDER_ITEMS_TABLE = "olist_order_items_dataset"

_TENANT_SCOPE_CONDITIONS = {
    "olist_order_items_dataset": "seller_id = {tenant}",
    "olist_sellers_dataset": "seller_id = {tenant}",
    "olist_orders_dataset": (
        "order_id IN ("
        "SELECT order_id FROM olist_order_items_dataset WHERE seller_id = {tenant}"
        ")"
    ),
    "olist_order_payments_dataset": (
        "order_id IN ("
        "SELECT order_id FROM olist_order_items_dataset WHERE seller_id = {tenant}"
        ")"
    ),
    "olist_order_reviews_dataset": (
        "order_id IN ("
        "SELECT order_id FROM olist_order_items_dataset WHERE seller_id = {tenant}"
        ")"
    ),
    "olist_products_dataset": (
        "product_id IN ("
        "SELECT product_id FROM olist_order_items_dataset WHERE seller_id = {tenant}"
        ")"
    ),
    "olist_customers_dataset": (
        "customer_id IN ("
        "SELECT o.customer_id "
        "FROM olist_orders_dataset o "
        "JOIN olist_order_items_dataset oi ON oi.order_id = o.order_id "
        "WHERE oi.seller_id = {tenant}"
        ")"
    ),
    "olist_geolocation_dataset": (
        "geolocation_zip_code_prefix IN ("
        "SELECT seller_zip_code_prefix "
        "FROM olist_sellers_dataset "
        "WHERE seller_id = {tenant} "
        "UNION "
        "SELECT c.customer_zip_code_prefix "
        "FROM olist_customers_dataset c "
        "JOIN olist_orders_dataset o ON o.customer_id = c.customer_id "
        "JOIN olist_order_items_dataset oi ON oi.order_id = o.order_id "
        "WHERE oi.seller_id = {tenant}"
        ")"
    ),
}


def catalog_tenant_id(_: str) -> str:
    return CATALOG_TENANT_ID


def is_admin_tenant(tenant_id: str) -> bool:
    return tenant_id.strip().lower() == ADMIN_TENANT_ID


def apply_tenant_scope(sql: str, tenant_id: str, *, dialect: str = "postgres") -> str:
    if is_admin_tenant(tenant_id):
        return sql

    expression = sqlglot.parse_one(sql, read=dialect)
    tenant_literal = _sql_literal(tenant_id)

    for table in list(expression.find_all(exp.Table)):
        table_name = table.name
        condition_template = _TENANT_SCOPE_CONDITIONS.get(table_name.lower())
        if condition_template is None:
            continue

        alias = table.alias_or_name
        condition = condition_template.format(tenant=tenant_literal)
        scoped_select = sqlglot.parse_one(
            f"SELECT * FROM {table_name} WHERE {condition}",
            read=dialect,
        )
        table.replace(scoped_select.subquery(alias))

    return expression.sql(dialect=dialect)


def format_tenant_scope_context(tenant_id: str) -> str:
    if is_admin_tenant(tenant_id):
        return ""
    tenant_literal = _sql_literal(tenant_id)
    return (
        "The current tenant maps to OList seller_id. "
        f"Restrict business data to seller_id = {tenant_literal}. "
        "For tables without seller_id, filter through olist_order_items_dataset "
        "using order_id or product_id as appropriate."
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
