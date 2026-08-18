from __future__ import annotations

from data_agent.datasources import (
    SemanticBindingRecord,
    SemanticFieldMapping,
    SemanticGraphBindingRecord,
    SemanticGraphFieldMapping,
)
from data_agent.relationships.models import (
    ActivatedRelationshipGraph,
    RelationshipComponent,
    RelationshipCondition,
    RelationshipEdge,
    RelationshipGraphNode,
    RelationshipProvenance,
)
from data_agent.semantic_metrics import (
    MetricAggregateFormula,
    MetricComparisonPredicate,
    MetricFieldExpression,
    MetricLiteralExpression,
    MetricNullPolicy,
    SemanticMetricDefinitionV2,
    SemanticMetricStaticValidator,
)
from data_agent.tools.schemas import CatalogColumn, CatalogRelation, CatalogSnapshot


def _catalog(amount_type: str = "DOUBLE") -> CatalogSnapshot:
    return CatalogSnapshot(
        schema_fingerprint="sha256:validator",
        relations=(
            CatalogRelation(
                relation="main.activity",
                columns=(
                    CatalogColumn(name="amount", data_type=amount_type, nullable=True),
                    CatalogColumn(name="status", data_type="VARCHAR", nullable=False),
                    CatalogColumn(
                        name="occurred_at", data_type="TIMESTAMP", nullable=False
                    ),
                ),
            ),
        ),
    )


def _binding(*, time_role: str = "time") -> SemanticBindingRecord:
    return SemanticBindingRecord(
        binding_id="binding",
        tenant_id="tenant",
        source_id="source",
        source_snapshot_version=1,
        domain_id="generic",
        version=1,
        status="active",
        mappings=(
            SemanticFieldMapping(
                logical_ref="activity.amount",
                physical_relation="main.activity",
                physical_column="amount",
                semantic_role="measure",
            ),
            SemanticFieldMapping(
                logical_ref="activity.status",
                physical_relation="main.activity",
                physical_column="status",
                semantic_role="status",
            ),
            SemanticFieldMapping(
                logical_ref="activity.occurred_at",
                physical_relation="main.activity",
                physical_column="occurred_at",
                semantic_role=time_role,
            ),
        ),
    )


def _definition(**updates) -> SemanticMetricDefinitionV2:
    values = {
        "metric_ref": "activity.value",
        "display_name": "Activity value",
        "description": "Governed activity amount",
        "formula": MetricAggregateFormula(
            operation="sum",
            operand=MetricFieldExpression(ref="activity.amount"),
        ),
        "default_time_ref": "activity.occurred_at",
        "allowed_time_refs": ("activity.occurred_at",),
    }
    values.update(updates)
    return SemanticMetricDefinitionV2(**values)


def test_valid_grounded_metric_has_no_static_issues() -> None:
    issues = SemanticMetricStaticValidator().validate(
        _definition(), binding=_binding(), catalog=_catalog()
    )

    assert issues == ()


def test_unknown_field_and_non_numeric_operand_fail_closed() -> None:
    unknown = _definition(
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricFieldExpression(ref="activity.missing"),
        )
    )
    validator = SemanticMetricStaticValidator()

    unknown_issues = validator.validate(
        unknown, binding=_binding(), catalog=_catalog()
    )
    type_issues = validator.validate(
        _definition(), binding=_binding(), catalog=_catalog("VARCHAR")
    )

    assert [item.code for item in unknown_issues] == ["METRIC_UNKNOWN_FIELD"]
    assert "METRIC_NON_NUMERIC_OPERAND" in {item.code for item in type_issues}


def test_time_role_and_error_null_policy_are_explicit_errors() -> None:
    issues = SemanticMetricStaticValidator().validate(
        _definition(null_policy=MetricNullPolicy.ERROR),
        binding=_binding(time_role="dimension"),
        catalog=_catalog(),
    )

    assert {item.code for item in issues} == {
        "METRIC_INVALID_TIME_ROLE",
        "METRIC_NULL_ERROR_UNSUPPORTED",
    }


def test_graph_fanout_is_rejected_for_measure_on_expanding_side() -> None:
    catalog = CatalogSnapshot(
        schema_fingerprint="sha256:graph",
        relations=(
            CatalogRelation(
                relation_id="relation:orders",
                relation="main.orders",
                columns=(
                    CatalogColumn(
                        column_id="column:order_id",
                        name="order_id",
                        data_type="VARCHAR",
                        nullable=False,
                    ),
                    CatalogColumn(
                        column_id="column:order_amount",
                        name="amount",
                        data_type="DOUBLE",
                        nullable=False,
                    ),
                ),
            ),
            CatalogRelation(
                relation_id="relation:items",
                relation="main.items",
                columns=(
                    CatalogColumn(
                        column_id="column:item_order_id",
                        name="order_id",
                        data_type="VARCHAR",
                        nullable=False,
                    ),
                    CatalogColumn(
                        column_id="column:item_status",
                        name="status",
                        data_type="VARCHAR",
                        nullable=False,
                    ),
                ),
            ),
        ),
    )
    graph = ActivatedRelationshipGraph(
        graph_id="graph",
        revision=1,
        nodes=(
            RelationshipGraphNode(
                node_id="orders",
                relation_id="relation:orders",
                role_name="orders",
                logical_entity="order",
            ),
            RelationshipGraphNode(
                node_id="items",
                relation_id="relation:items",
                role_name="items",
                logical_entity="item",
            ),
        ),
        edges=(
            RelationshipEdge(
                edge_id="orders-items",
                from_node_id="orders",
                to_node_id="items",
                conditions=(
                    RelationshipCondition(
                        from_column_id="column:order_id",
                        to_column_id="column:item_order_id",
                    ),
                ),
                cardinality="one_to_many",
                provenance=RelationshipProvenance(source="user", user_edited=True),
            ),
        ),
        components=(
            RelationshipComponent(
                component_id="commerce",
                anchor_node_id="orders",
                grain_column_ids=("column:order_id",),
            ),
        ),
    )
    binding = SemanticGraphBindingRecord(
        binding_id="binding",
        tenant_id="tenant",
        source_id="source",
        source_snapshot_version=1,
        schema_fingerprint="sha256:graph",
        domain_id="commerce",
        version=1,
        status="active",
        graph=graph,
        mappings=(
            SemanticGraphFieldMapping(
                logical_ref="order.amount",
                node_id="orders",
                column_id="column:order_amount",
                semantic_role="measure",
            ),
            SemanticGraphFieldMapping(
                logical_ref="item.status",
                node_id="items",
                column_id="column:item_status",
                semantic_role="status",
            ),
        ),
        validation_report_digest="sha256:graph-report",
    )
    definition = SemanticMetricDefinitionV2(
        metric_ref="order.value",
        display_name="Order value",
        description="Order value scoped by item status",
        formula=MetricAggregateFormula(
            operation="sum",
            operand=MetricFieldExpression(ref="order.amount"),
        ),
        default_filter=MetricComparisonPredicate(
            operation="eq",
            left=MetricFieldExpression(ref="item.status"),
            right=MetricLiteralExpression(value="accepted"),
        ),
    )

    issues = SemanticMetricStaticValidator().validate(
        definition, binding=binding, catalog=catalog
    )

    assert "GRAPH_UNSAFE_FANOUT" in {item.code for item in issues}
