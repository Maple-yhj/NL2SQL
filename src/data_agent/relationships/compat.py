"""Read-only adapters from historical v1 bindings to the graph domain."""

from __future__ import annotations

from data_agent.datasources.models import SemanticBindingRecord
from data_agent.tools.schemas import CatalogSnapshot

from .models import (
    ActivatedRelationshipGraph, RelationshipComponent, RelationshipCondition,
    RelationshipEdge, RelationshipGraphNode, RelationshipProvenance,
)


def normalize_binding_graph(binding: SemanticBindingRecord, catalog: CatalogSnapshot) -> ActivatedRelationshipGraph:
    """Project a v1 tree into role nodes without mutating its stored representation."""
    relations = {relation.relation: relation for relation in catalog.relations}
    mapped = tuple(dict.fromkeys(item.physical_relation for item in binding.mappings))
    nodes = tuple(
        RelationshipGraphNode(node_id=f"v1-node-{index}", relation_id=relations[relation].relation_id,
            role_name=relation.rsplit(".", 1)[-1], logical_entity=relation.replace(".", "_"))
        for index, relation in enumerate(mapped, start=1)
    )
    by_relation = {relation: node for relation, node in zip(mapped, nodes, strict=True)}
    edges = tuple(
        RelationshipEdge(edge_id=f"v1-edge-{relationship.relationship_id}",
            from_node_id=by_relation[relationship.left_relation].node_id,
            to_node_id=by_relation[relationship.right_relation].node_id,
            conditions=(RelationshipCondition(
                from_column_id=_column_id(relations[relationship.left_relation], relationship.left_column),
                to_column_id=_column_id(relations[relationship.right_relation], relationship.right_column),
            ),), cardinality="unknown", join_semantics=relationship.join_type.value,
            preserve_node_id=by_relation[relationship.left_relation].node_id if relationship.join_type.value == "left" else None,
            provenance=RelationshipProvenance(source="migration"),
        ) for relationship in binding.relationships
    )
    anchor = by_relation[binding.primary_relation or mapped[0]].node_id
    return ActivatedRelationshipGraph(graph_id=f"v1-{binding.binding_id}", revision=binding.version, nodes=nodes, edges=edges,
        components=(RelationshipComponent(component_id="v1-component", anchor_node_id=anchor, grain_column_ids=()),))


def _column_id(relation: object, name: str) -> str:
    return next(column.column_id for column in relation.columns if column.name == name)  # type: ignore[attr-defined]


__all__ = ["normalize_binding_graph"]
