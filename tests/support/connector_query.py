"""Current-contract query fixture for connector integration tests."""

from __future__ import annotations

import hashlib

from data_agent.dataset_query import (
    AnalysisType,
    LogicalQueryPlan,
    PreparedQuery,
    QueryParameter,
    ResultShape,
)


ALLOWED_RELATIONS = (
    "public.olist_order_items_dataset",
    "public.olist_sellers_dataset",
)
BINDING_DIGEST = "sha256:test-dataset-binding"
CONNECTION_REF = "secret://test/dataset"
SCHEMA_FINGERPRINT = "sha256:test-connector-schema"
SOURCE = "sales"
TENANT_ID = "seller-42"


def governed_sales_query(dialect: str) -> PreparedQuery:
    """Build a deterministic read-only aggregate using public query contracts."""

    plan = LogicalQueryPlan(
        analysis_type=AnalysisType.RANKING,
        metrics=("dataset.gmv",),
        entities=("dataset.OrderItem", "dataset.Seller"),
        dimensions=("dataset.Seller.seller_id",),
        limit=10,
        result_shape=ResultShape.RANKING,
    )
    if dialect == "sqlite":
        executable_sql = (
            'SELECT "t1"."seller_id" AS "seller_id", '
            'SUM(CAST("t0"."price" AS REAL)) + '
            'SUM(CAST("t0"."freight_value" AS REAL)) AS "gmv" '
            'FROM "public"."olist_order_items_dataset" AS "t0" '
            'INNER JOIN "public"."olist_sellers_dataset" AS "t1" '
            'ON "t0"."seller_id" = "t1"."seller_id" '
            'WHERE (DATETIME("t0"."shipping_limit_date") >= @1 '
            'AND DATETIME("t0"."shipping_limit_date") < @2) '
            'AND "t1"."seller_id" = @3 '
            'GROUP BY "t1"."seller_id" ORDER BY "gmv" DESC LIMIT @4'
        )
    else:
        null_ordering = " NULLS LAST" if dialect == "postgres" else ""
        executable_sql = (
            'SELECT "t1"."seller_id" AS "seller_id", '
            'SUM(CAST("t0"."price" AS DECIMAL)) + '
            'SUM(CAST("t0"."freight_value" AS DECIMAL)) AS "gmv" '
            'FROM "public"."olist_order_items_dataset" AS "t0" '
            'INNER JOIN "public"."olist_sellers_dataset" AS "t1" '
            'ON "t0"."seller_id" = "t1"."seller_id" '
            'WHERE (CAST("t0"."shipping_limit_date" AS TIMESTAMP) >= $1 '
            'AND CAST("t0"."shipping_limit_date" AS TIMESTAMP) < $2) '
            'AND "t1"."seller_id" = $3 '
            'GROUP BY "t1"."seller_id" '
            f'ORDER BY "gmv" DESC{null_ordering} LIMIT $4'
        )
    parameters = (
        QueryParameter(
            position=1,
            value="2017-01-01",
            logical_type="datetime",
            purpose="time_start",
        ),
        QueryParameter(
            position=2,
            value="2018-01-01",
            logical_type="datetime",
            purpose="time_end",
        ),
        QueryParameter(
            position=3,
            value=TENANT_ID,
            logical_type="string",
            purpose="tenant_scope",
        ),
        QueryParameter(
            position=4,
            value=10,
            logical_type="integer",
            purpose="limit",
        ),
    )
    return PreparedQuery(
        dialect=dialect,
        logical_plan=plan,
        logical_plan_hash=plan.stable_hash(),
        sql_ast_hash=hashlib.sha256(executable_sql.encode("utf-8")).hexdigest(),
        logical_sql=executable_sql,
        executable_sql=executable_sql,
        parameters=parameters,
        allowed_relations=ALLOWED_RELATIONS,
        policy_decision_id="policy:test-connector-query",
        estimated_cost=0,
        max_rows=10,
        bundle_digest=BINDING_DIGEST,
        schema_fingerprint=SCHEMA_FINGERPRINT,
    )
