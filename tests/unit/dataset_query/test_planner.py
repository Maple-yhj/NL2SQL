from __future__ import annotations

import asyncio
import json

from data_agent.dataset_query import DatasetLogicalPlanner
from data_agent.datasources import (
    SemanticBindingStatus,
    SemanticGraphBindingRecord,
    SemanticGraphFieldMapping,
)
from data_agent.relationships.models import (
    ActivatedRelationshipGraph,
    RelationshipCondition,
    RelationshipEdge,
    RelationshipGraphNode,
    RelationshipProvenance,
)
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot


class _SequenceModel:
    model_id = "planner-test"
    version = "1"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = [json.dumps(item) for item in responses]
        self.calls: list[str] = []

    async def complete(self, prompt: str, **_: object) -> str:
        self.calls.append(prompt)
        return self._responses.pop(0)


def _catalog_and_binding() -> tuple[CatalogSnapshot, SemanticGraphBindingRecord]:
    catalog = CatalogSnapshot(
        schema_fingerprint="sha256:planner-route",
        relations=(
            CatalogRelation(
                relation="main.items",
                columns=(
                    CatalogColumn(name="product_id", data_type="TEXT", nullable=False),
                    CatalogColumn(name="price", data_type="DOUBLE", nullable=False),
                ),
            ),
            CatalogRelation(
                relation="main.products",
                columns=(
                    CatalogColumn(name="product_id", data_type="TEXT", nullable=False),
                    CatalogColumn(name="category", data_type="TEXT", nullable=True),
                ),
            ),
            CatalogRelation(
                relation="main.translation",
                columns=(
                    CatalogColumn(name="category_en", data_type="TEXT", nullable=True),
                ),
            ),
        ),
    )
    items, products, translation = catalog.relations
    item_columns = {item.name: item.column_id for item in items.columns}
    product_columns = {item.name: item.column_id for item in products.columns}
    translation_columns = {item.name: item.column_id for item in translation.columns}
    nodes = (
        RelationshipGraphNode(
            node_id="items",
            relation_id=items.relation_id,
            role_name="items",
            logical_entity="Items",
        ),
        RelationshipGraphNode(
            node_id="products",
            relation_id=products.relation_id,
            role_name="products",
            logical_entity="Products",
        ),
        RelationshipGraphNode(
            node_id="translation",
            relation_id=translation.relation_id,
            role_name="translation",
            logical_entity="Translation",
        ),
    )
    graph = ActivatedRelationshipGraph(
        graph_id="planner-route",
        revision=1,
        nodes=nodes,
        edges=(
            RelationshipEdge(
                edge_id="items-products",
                from_node_id="items",
                to_node_id="products",
                conditions=(
                    RelationshipCondition(
                        from_column_id=item_columns["product_id"],
                        to_column_id=product_columns["product_id"],
                    ),
                ),
                cardinality="many_to_one",
                provenance=RelationshipProvenance(source="user"),
            ),
        ),
        components=(),
    )
    binding = SemanticGraphBindingRecord(
        binding_id="planner-route-binding",
        tenant_id="tenant",
        source_id="source",
        source_snapshot_version=1,
        schema_fingerprint=catalog.schema_fingerprint,
        domain_id="commerce",
        version=1,
        status=SemanticBindingStatus.ACTIVE,
        graph=graph,
        mappings=(
            SemanticGraphFieldMapping(
                logical_ref="commerce.Items.price",
                node_id="items",
                column_id=item_columns["price"],
            ),
            SemanticGraphFieldMapping(
                logical_ref="commerce.Products.category",
                node_id="products",
                column_id=product_columns["category"],
            ),
            SemanticGraphFieldMapping(
                logical_ref="commerce.Translation.category_en",
                node_id="translation",
                column_id=translation_columns["category_en"],
            ),
        ),
        validation_report_digest="sha256:planner-route-report",
    )
    return catalog, binding


def _aggregate(group_ref: str, *, measure_ref: str = "commerce.Items.price") -> dict[str, object]:
    return {
        "status": "ready",
        "analysis_type": "aggregate",
        "select": [],
        "aggregations": [
            {"ref": measure_ref, "operation": "sum", "alias": "total_amount"}
        ],
        "group_by": [group_ref],
        "filters": [],
        "order_by": [{"ref": "total_amount", "direction": "desc"}],
        "limit": 100,
    }


def test_planner_repairs_unknown_and_disconnected_optional_fields() -> None:
    catalog, binding = _catalog_and_binding()
    model = _SequenceModel(
        [
            _aggregate("commerce.Products.category", measure_ref="invented.amount"),
            _aggregate("commerce.Translation.category_en"),
            _aggregate("commerce.Products.category"),
        ]
    )

    result = asyncio.run(
        DatasetLogicalPlanner(model).build_plan(
            question="按商品类别汇总金额",
            binding=binding,
            catalog=catalog,
        )
    )

    assert result.plan.group_by == ("commerce.Products.category",)
    assert len(model.calls) == 3
    assert '"routeComponent":"component-1"' in model.calls[0]
    assert '"routeComponent":"component-2"' in model.calls[0]
    assert "outside logicalCatalog" in model.calls[1]
    assert "do not share a safe active relationship route" in model.calls[2]
