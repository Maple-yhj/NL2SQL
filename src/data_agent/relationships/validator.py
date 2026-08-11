"""Deterministic calibration for relationship suggestions and graph activation."""

from __future__ import annotations

from .models import (
    RelationshipEdgeQuality,
    RelationshipFinding,
    RelationshipGraphDraft,
    RelationshipValidationReport,
)


def validate_graph(graph: RelationshipGraphDraft) -> RelationshipValidationReport:
    """Merge deterministic, statistical, model, and user evidence fail-closed."""

    findings: list[RelationshipFinding] = []
    qualities: list[tuple[str, RelationshipEdgeQuality]] = []
    enabled_edges = tuple(edge for edge in graph.edges if edge.enabled)
    grained_nodes = {
        component.anchor_node_id
        for component in graph.components
        if component.grain_column_ids
    }
    for edge in graph.edges:
        quality = _merged_quality(edge)
        qualities.append((edge.edge_id, quality))
        if not edge.enabled:
            continue
        severity: str | None = None
        code = ""
        message = ""
        if edge.provenance.source == "llm" and not edge.provenance.user_edited:
            severity, code, message = (
                "error",
                "RELATIONSHIP_REVIEW_REQUIRED",
                "AI-recommended relationships must be accepted or rejected before activation.",
            )
        elif quality.evidence_level == "blocked":
            severity, code, message = "error", "RELATIONSHIP_BLOCKED", "Relationship evidence is structurally or statistically invalid."
        elif edge.cardinality == "many_to_many":
            if not {edge.from_node_id, edge.to_node_id}.issubset(grained_nodes):
                severity, code, message = "error", "RELATIONSHIP_MANY_TO_MANY", "Many-to-many relationships require explicit grain declarations before activation."
            else:
                severity, code, message = "warning", "RELATIONSHIP_MANY_TO_MANY", "Many-to-many relationship use is restricted to declared grains."
        elif edge.cardinality == "unknown":
            severity, code, message = "error", "RELATIONSHIP_UNKNOWN_CARDINALITY", "Set the relationship cardinality before activation; aggregate queries cannot use an unknown cardinality."
        elif quality.match_rate is not None and quality.match_rate < 0.5:
            severity, code, message = "warning", "RELATIONSHIP_LOW_MATCH_RATE", "Relationship has a low observed match rate."
        if quality.expansion_ratio is not None and quality.expansion_ratio > 10:
            severity, code, message = "error", "RELATIONSHIP_HIGH_FANOUT", "Relationship expansion is too high for safe activation."
        if severity:
            findings.append(RelationshipFinding(code=code, severity=severity, edge_id=edge.edge_id, message=message))

    enabled_node_ids = {node.node_id for node in graph.nodes if node.enabled}
    connected_node_ids = {
        node_id
        for edge in enabled_edges
        for node_id in (edge.from_node_id, edge.to_node_id)
    }
    for node_id in sorted(enabled_node_ids - connected_node_ids):
        findings.append(
            RelationshipFinding(
                code="RELATIONSHIP_ISOLATED_ROLE",
                severity="warning",
                node_id=node_id,
                message=f"Enabled role {node_id} is isolated and can only be queried as a single table.",
            )
        )
    if _has_cycle(enabled_edges) and not graph.route_rules:
        findings.append(
            RelationshipFinding(
                code="RELATIONSHIP_CYCLE_UNRESOLVED",
                severity="error",
                message="Relationship cycles require a preferred route rule or removal of redundant edges before activation.",
            )
        )
    return RelationshipValidationReport(
        graph_id=graph.graph_id,
        graph_revision=graph.revision,
        schema_fingerprint=graph.schema_fingerprint,
        findings=tuple(findings),
        edge_quality=tuple(qualities),
        activation_allowed=not any(item.severity == "error" for item in findings),
    )


def _merged_quality(edge: object) -> RelationshipEdgeQuality:
    """Calibrate evidence without allowing an LLM claim to overrule observations."""

    # Kept local to this module so the persisted graph schema stays compact;
    # the edge already contains immutable provenance and the profiling summary.
    from .models import RelationshipEdge

    assert isinstance(edge, RelationshipEdge)
    quality = edge.quality or RelationshipEdgeQuality(evidence_level="low")
    if edge.provenance.rejected:
        return quality.model_copy(update={"evidence_level": "blocked"})
    if quality.match_rate is not None and quality.match_rate < 0.2:
        return quality.model_copy(update={"evidence_level": "blocked"})
    if edge.provenance.source == "database_constraint":
        return quality.model_copy(update={"evidence_level": "high"})
    if edge.provenance.source == "llm" and quality.evidence_level == "high":
        return quality.model_copy(update={"evidence_level": "recommended"})
    return quality


def _has_cycle(edges: tuple[object, ...]) -> bool:
    from .models import RelationshipEdge

    parent: dict[str, str] = {}

    def root(node_id: str) -> str:
        parent.setdefault(node_id, node_id)
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    for raw_edge in edges:
        assert isinstance(raw_edge, RelationshipEdge)
        left = root(raw_edge.from_node_id)
        right = root(raw_edge.to_node_id)
        if left == right:
            return True
        parent[right] = left
    return False


__all__ = ["validate_graph"]
