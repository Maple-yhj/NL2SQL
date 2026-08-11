"""Immutable, arbitrary relationship-graph domain contracts.

The graph stores business roles rather than physical tables.  That distinction
is what permits self joins, parallel edges, cycles, and multiple roles for one
physical relation while keeping all identifiers deterministic and auditable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, StringConstraints, model_validator

from data_agent.tools.models import ToolModel
from data_agent.tools.schemas import CatalogSnapshot, NonBlankText, StableCatalogId
from typing import Annotated


StableIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$",
    ),
]


class RelationshipModel(ToolModel):
    """Base class kept separate to make the graph boundary explicit."""


RelationshipCardinality = Literal[
    "one_to_one", "one_to_many", "many_to_one", "many_to_many", "unknown"
]


class RelationshipProvenance(RelationshipModel):
    source: Literal["database_constraint", "llm", "user", "migration"]
    run_id: StableIdentifier | None = None
    model_id: NonBlankText | None = None
    prompt_version: NonBlankText | None = None
    explanation: NonBlankText | None = None
    user_edited: bool = False
    rejected: bool = False

    @model_validator(mode="after")
    def validate_source_metadata(self) -> "RelationshipProvenance":
        if self.source == "llm" and self.run_id is None:
            raise ValueError("LLM provenance requires a recommendation run ID")
        if self.rejected and not self.user_edited:
            raise ValueError("rejected recommendations must be user-edited")
        return self


class RelationshipEdgeQuality(RelationshipModel):
    evidence_level: Literal["high", "recommended", "low", "blocked"]
    match_rate: float | None = Field(default=None, ge=0, le=1)
    left_orphan_rate: float | None = Field(default=None, ge=0, le=1)
    right_orphan_rate: float | None = Field(default=None, ge=0, le=1)
    left_unique: bool | None = None
    right_unique: bool | None = None
    average_fanout: float | None = Field(default=None, ge=0)
    maximum_fanout: int | None = Field(default=None, ge=0)
    expansion_ratio: float | None = Field(default=None, ge=0)


class RelationshipGraphNode(RelationshipModel):
    node_id: StableIdentifier
    relation_id: StableCatalogId
    role_name: NonBlankText
    logical_entity: NonBlankText
    enabled: bool = True


class RelationshipCondition(RelationshipModel):
    from_column_id: StableCatalogId
    operator: Literal["eq"] = "eq"
    to_column_id: StableCatalogId


class RelationshipEdge(RelationshipModel):
    edge_id: StableIdentifier
    from_node_id: StableIdentifier
    to_node_id: StableIdentifier
    conditions: tuple[RelationshipCondition, ...] = Field(min_length=1)
    cardinality: RelationshipCardinality = "unknown"
    join_semantics: Literal["inner", "left"] = "inner"
    preserve_node_id: StableIdentifier | None = None
    route_priority: int = Field(default=100, ge=0, le=1_000_000)
    enabled: bool = True
    provenance: RelationshipProvenance
    quality: RelationshipEdgeQuality | None = None

    @model_validator(mode="after")
    def validate_edge_shape(self) -> "RelationshipEdge":
        pairs = tuple(
            (condition.from_column_id, condition.operator, condition.to_column_id)
            for condition in self.conditions
        )
        if len(pairs) != len(set(pairs)):
            raise ValueError("relationship edge cannot repeat a field condition")
        if self.join_semantics == "left":
            if self.preserve_node_id not in {self.from_node_id, self.to_node_id}:
                raise ValueError("LEFT relationship must preserve one edge endpoint")
        elif self.preserve_node_id is not None:
            raise ValueError("INNER relationship cannot declare a preserve side")
        return self


class RelationshipComponent(RelationshipModel):
    component_id: StableIdentifier
    anchor_node_id: StableIdentifier
    grain_column_ids: tuple[StableCatalogId, ...]
    anchor_scoped: bool = False


class RelationshipRouteRule(RelationshipModel):
    rule_id: StableIdentifier
    terminal_node_ids: tuple[StableIdentifier, ...] = Field(min_length=1)
    ordered_edge_ids: tuple[StableIdentifier, ...] = Field(min_length=1)


class RelationshipGraphDraft(RelationshipModel):
    graph_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int = Field(ge=1)
    schema_fingerprint: NonBlankText
    revision: int = Field(ge=1)
    status: Literal["discovering", "draft", "validating", "ready", "failed"]
    nodes: tuple[RelationshipGraphNode, ...]
    edges: tuple[RelationshipEdge, ...]
    components: tuple[RelationshipComponent, ...]
    route_rules: tuple[RelationshipRouteRule, ...] = ()

    @model_validator(mode="after")
    def validate_graph_structure(self) -> "RelationshipGraphDraft":
        _unique("node", (node.node_id for node in self.nodes))
        _unique("edge", (edge.edge_id for edge in self.edges))
        _unique("component", (component.component_id for component in self.components))
        _unique("route rule", (rule.rule_id for rule in self.route_rules))
        node_ids = {node.node_id for node in self.nodes}
        role_keys = {(node.relation_id, node.role_name.casefold()) for node in self.nodes}
        if len(role_keys) != len(self.nodes):
            raise ValueError("role names must be unique for each physical relation")
        logical_entities = {node.logical_entity.casefold() for node in self.nodes}
        if len(logical_entities) != len(self.nodes):
            raise ValueError("logical entities must be unique across graph nodes")
        for edge in self.edges:
            if not {edge.from_node_id, edge.to_node_id}.issubset(node_ids):
                raise ValueError(f"edge {edge.edge_id} references an unknown node")
        for component in self.components:
            if component.anchor_node_id not in node_ids:
                raise ValueError(f"component {component.component_id} has an unknown anchor")
        edges = {edge.edge_id: edge for edge in self.edges}
        for rule in self.route_rules:
            if not set(rule.terminal_node_ids).issubset(node_ids):
                raise ValueError(f"route rule {rule.rule_id} has an unknown terminal")
            if not set(rule.ordered_edge_ids).issubset(edges):
                raise ValueError(f"route rule {rule.rule_id} has an unknown edge")
            _validate_route_rule(rule, edges)
        return self


class ActivatedRelationshipGraph(RelationshipModel):
    """Immutable graph payload pinned inside a v2 semantic binding."""

    graph_id: StableIdentifier
    revision: int = Field(ge=1)
    nodes: tuple[RelationshipGraphNode, ...]
    edges: tuple[RelationshipEdge, ...]
    components: tuple[RelationshipComponent, ...]
    route_rules: tuple[RelationshipRouteRule, ...] = ()


class RelationshipFinding(RelationshipModel):
    code: NonBlankText
    severity: Literal["info", "warning", "error"]
    edge_id: StableIdentifier | None = None
    node_id: StableIdentifier | None = None
    message: NonBlankText


class RelationshipRouteAmbiguity(RelationshipModel):
    required_node_ids: tuple[StableIdentifier, ...]
    candidate_edge_ids: tuple[StableIdentifier, ...]


class RelationshipValidationReport(RelationshipModel):
    graph_id: StableIdentifier
    graph_revision: int = Field(ge=1)
    schema_fingerprint: NonBlankText
    findings: tuple[RelationshipFinding, ...]
    edge_quality: tuple[tuple[StableIdentifier, RelationshipEdgeQuality], ...] = ()
    route_ambiguities: tuple[RelationshipRouteAmbiguity, ...] = ()
    activation_allowed: bool
    report_digest: NonBlankText = "pending"

    @model_validator(mode="after")
    def set_report_digest(self) -> "RelationshipValidationReport":
        payload = self.model_dump(mode="json", exclude={"report_digest"})
        digest = "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.report_digest != "pending" and self.report_digest != digest:
            raise ValueError("relationship validation report digest does not match")
        object.__setattr__(self, "report_digest", digest)
        return self


class RelationshipRecommendationRun(RelationshipModel):
    """Auditable asynchronous discovery attempt; raw prompts and rows stay out."""

    run_id: StableIdentifier
    tenant_id: StableIdentifier
    source_id: StableIdentifier
    source_snapshot_version: int = Field(ge=1)
    graph_id: StableIdentifier
    status: Literal["queued", "running", "succeeded", "failed", "retryable_failed"]
    schema_fingerprint: NonBlankText
    model_id: NonBlankText | None = None
    prompt_version: NonBlankText | None = None
    profiler_version: NonBlankText | None = None
    error_code: NonBlankText | None = None
    error_message: NonBlankText | None = None


def validate_graph_catalog(graph: RelationshipGraphDraft, catalog: CatalogSnapshot) -> None:
    """Ensure graph IDs point to the exact physical objects in its snapshot."""

    relations = {relation.relation_id: relation for relation in catalog.relations}
    columns = {
        column.column_id: relation.relation_id
        for relation in catalog.relations
        for column in relation.columns
    }
    nodes = {node.node_id: node for node in graph.nodes}
    for node in graph.nodes:
        if node.relation_id not in relations:
            raise ValueError(f"node {node.node_id} references an unknown relation")
    for edge in graph.edges:
        left = nodes[edge.from_node_id].relation_id
        right = nodes[edge.to_node_id].relation_id
        for condition in edge.conditions:
            if columns.get(condition.from_column_id) != left:
                raise ValueError(f"edge {edge.edge_id} condition has an invalid from column")
            if columns.get(condition.to_column_id) != right:
                raise ValueError(f"edge {edge.edge_id} condition has an invalid to column")
    for component in graph.components:
        anchor_relation = nodes[component.anchor_node_id].relation_id
        if any(columns.get(column_id) != anchor_relation for column_id in component.grain_column_ids):
            raise ValueError(f"component {component.component_id} has an invalid grain column")


def _unique(name: str, values: object) -> None:
    materialized = tuple(values)  # type: ignore[arg-type]
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{name} IDs must be unique")


def _validate_route_rule(
    rule: RelationshipRouteRule,
    edges: dict[str, RelationshipEdge],
) -> None:
    """Rules are executable trees: each later edge introduces one new node."""

    included: set[str] = set()
    for index, edge_id in enumerate(rule.ordered_edge_ids):
        edge = edges[edge_id]
        endpoints = {edge.from_node_id, edge.to_node_id}
        if index == 0:
            included.update(endpoints)
            continue
        overlap = endpoints & included
        if len(overlap) != 1 or len(endpoints - included) != 1:
            raise ValueError(
                f"route rule {rule.rule_id} edge {edge_id} does not extend a simple join tree"
            )
        included.update(endpoints)
    if not set(rule.terminal_node_ids).issubset(included):
        raise ValueError(f"route rule {rule.rule_id} terminals are not reached")


__all__ = [
    "ActivatedRelationshipGraph",
    "RelationshipCardinality",
    "RelationshipComponent",
    "RelationshipCondition",
    "RelationshipEdge",
    "RelationshipEdgeQuality",
    "RelationshipFinding",
    "RelationshipGraphDraft",
    "RelationshipGraphNode",
    "RelationshipModel",
    "RelationshipProvenance",
    "RelationshipRouteAmbiguity",
    "RelationshipRouteRule",
    "RelationshipRecommendationRun",
    "RelationshipValidationReport",
    "validate_graph_catalog",
]
