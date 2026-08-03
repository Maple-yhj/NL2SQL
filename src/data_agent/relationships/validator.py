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
    for edge in graph.edges:
        quality = _merged_quality(edge)
        severity: str | None = None
        code = ""
        message = ""
        if quality.evidence_level == "blocked":
            severity, code, message = "error", "RELATIONSHIP_BLOCKED", "Relationship evidence is structurally or statistically invalid."
        elif edge.cardinality == "many_to_many":
            severity, code, message = "warning", "RELATIONSHIP_MANY_TO_MANY", "Many-to-many relationships require explicit bridge/grain rules before aggregate use."
        elif edge.cardinality == "unknown":
            severity, code, message = "warning", "RELATIONSHIP_UNKNOWN_CARDINALITY", "Relationship cardinality is unknown and aggregate queries will fail closed."
        elif quality.match_rate is not None and quality.match_rate < 0.5:
            severity, code, message = "warning", "RELATIONSHIP_LOW_MATCH_RATE", "Relationship has a low observed match rate."
        if quality.expansion_ratio is not None and quality.expansion_ratio > 10:
            severity, code, message = "warning", "RELATIONSHIP_HIGH_FANOUT", "Relationship has a high estimated expansion ratio."
        if severity:
            findings.append(RelationshipFinding(code=code, severity=severity, edge_id=edge.edge_id, message=message))
        qualities.append((edge.edge_id, quality))
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


__all__ = ["validate_graph"]
