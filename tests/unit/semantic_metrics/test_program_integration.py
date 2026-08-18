from __future__ import annotations

from datetime import datetime

import duckdb

from data_agent.dataset_query import (
    DatasetFieldExpression,
    DatasetFunctionExpression,
    DatasetMetricExpression,
    DatasetProjection,
    DatasetQueryProgram,
    DatasetQueryProgramCompiler,
    DatasetQueryStage,
    DatasetRootSource,
)
from data_agent.datasources import SemanticBindingRecord, SemanticFieldMapping
from data_agent.semantic_metrics import (
    EffectiveMetricCatalog,
    MetricAggregateFormula,
    MetricBinaryExpression,
    MetricCatalogEntry,
    MetricCatalogOrigin,
    MetricFieldExpression,
    MetricSetPredicate,
    SemanticMetricDefinitionV2,
)
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot


def test_v2_gmv_catalog_compiles_inside_quarterly_dataset_program() -> None:
    binding = SemanticBindingRecord(
        binding_id="olist-binding",
        tenant_id="tenant",
        source_id="olist",
        source_snapshot_version=1,
        domain_id="commerce",
        version=1,
        status="active",
        mappings=(
            SemanticFieldMapping(
                logical_ref="commerce.order.purchased_at",
                physical_relation="main.order_items",
                physical_column="purchased_at",
                semantic_role="time",
            ),
            SemanticFieldMapping(
                logical_ref="commerce.order.status",
                physical_relation="main.order_items",
                physical_column="status",
                semantic_role="status",
            ),
            SemanticFieldMapping(
                logical_ref="commerce.item.price",
                physical_relation="main.order_items",
                physical_column="price",
                semantic_role="measure",
            ),
            SemanticFieldMapping(
                logical_ref="commerce.item.freight",
                physical_relation="main.order_items",
                physical_column="freight",
                semantic_role="measure",
            ),
        ),
    )
    definition = SemanticMetricDefinitionV2(
        metric_ref="commerce.gmv",
        display_name="GMV",
        description="Delivered item price plus freight",
        synonyms=("成交总额",),
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricBinaryExpression(
                operation="add",
                left=MetricFieldExpression(ref="commerce.item.price"),
                right=MetricFieldExpression(ref="commerce.item.freight"),
            ),
        ),
        default_filter=MetricSetPredicate(
            operation="in",
            operand=MetricFieldExpression(ref="commerce.order.status"),
            values=("delivered",),
        ),
        default_time_ref="commerce.order.purchased_at",
        allowed_time_refs=("commerce.order.purchased_at",),
        unit="currency",
        currency="BRL",
    )
    metrics = EffectiveMetricCatalog.build(
        governed=(
            MetricCatalogEntry.create(
                definition=definition,
                origin=MetricCatalogOrigin.GOVERNED,
                authority_ref="metric-set:commerce:1",
            ),
        )
    )
    catalog = CatalogSnapshot(
        schema_fingerprint="sha256:olist",
        relations=(
            CatalogRelation(
                relation="main.order_items",
                columns=(
                    CatalogColumn(
                        name="purchased_at", data_type="TIMESTAMP", nullable=False
                    ),
                    CatalogColumn(name="status", data_type="VARCHAR", nullable=False),
                    CatalogColumn(name="price", data_type="DOUBLE", nullable=False),
                    CatalogColumn(name="freight", data_type="DOUBLE", nullable=False),
                ),
            ),
        ),
    )
    quarter = DatasetFunctionExpression(
        operation="time_bucket",
        time_grain="quarter",
        arguments=(DatasetFieldExpression(ref="commerce.order.purchased_at"),),
    )
    program = DatasetQueryProgram(
        stages=(
            DatasetQueryStage(
                stage_id="quarterly_gmv",
                input=DatasetRootSource(),
                projections=(
                    DatasetProjection(alias="quarter", expression=quarter),
                    DatasetProjection(
                        alias="gmv",
                        expression=DatasetMetricExpression(ref="成交总额"),
                    ),
                ),
                group_by=(quarter,),
                order_by=(),
            ),
        ),
        output_stage_id="quarterly_gmv",
    )

    prepared = DatasetQueryProgramCompiler().compile(
        program=program,
        binding=binding,
        dialect="duckdb",
        schema_fingerprint="sha256:olist",
        bundle_digest="bundle",
        catalog=catalog,
        metric_catalog=metrics,
    )
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE order_items"
            "(purchased_at TIMESTAMP, status VARCHAR, price DOUBLE, freight DOUBLE)"
        )
        connection.executemany(
            "INSERT INTO order_items VALUES (?, ?, ?, ?)",
            (
                ("2025-01-10", "delivered", 100, 10),
                ("2025-03-10", "cancelled", 200, 20),
                ("2025-04-10", "delivered", 50, 5),
            ),
        )
        rows = connection.execute(
            prepared.executable_sql,
            [parameter.value for parameter in prepared.parameters],
        ).fetchall()
    finally:
        connection.close()

    assert sorted(rows) == [
        (datetime(2025, 1, 1), 110.0),
        (datetime(2025, 4, 1), 55.0),
    ]
    assert prepared.allowed_relations == ("main.order_items",)
    assert "delivered" not in prepared.executable_sql
