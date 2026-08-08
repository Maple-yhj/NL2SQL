"""Composition of the dataset-only Tool Registry snapshot."""

from __future__ import annotations

from data_agent.tools.registry import ToolRegistry

from .catalog import CATALOG_INSPECT_SPEC, CatalogInspectProvider
from .chart import CHART_RENDER_SPEC, ChartRenderProvider
from .compute import ANALYSIS_COMPUTE_SPEC, AnalysisComputeProvider
from .evidence import EVIDENCE_COLLECT_SPEC, EvidenceCollectProvider
from .profile import (
    DATA_PROFILE_SPEC,
    RESULT_PROFILE_SPEC,
    DataProfileProvider,
    ResultProfileProvider,
)
from .query import (
    QUERY_COMPILE_SPEC,
    QUERY_EXECUTE_SPEC,
    QUERY_EXPLAIN_SPEC,
    QUERY_PREVIEW_SPEC,
    QueryCompileProvider,
    QueryExecuteProvider,
    QueryExplainProvider,
    QueryPreviewProvider,
)
from .relationship import RELATIONSHIP_ROUTE_SPEC, RelationshipRouteProvider
from .semantic import SEMANTIC_INSPECT_SPEC, SemanticInspectProvider


DATASET_TOOL_REGISTRY_VERSION = "dataset-1.0.0"


def build_dataset_tool_registry() -> ToolRegistry:
    registry = ToolRegistry(version=DATASET_TOOL_REGISTRY_VERSION)
    entries = (
        (CATALOG_INSPECT_SPEC, CatalogInspectProvider()),
        (SEMANTIC_INSPECT_SPEC, SemanticInspectProvider()),
        (RELATIONSHIP_ROUTE_SPEC, RelationshipRouteProvider()),
        (QUERY_COMPILE_SPEC, QueryCompileProvider()),
        (QUERY_EXPLAIN_SPEC, QueryExplainProvider()),
        (QUERY_PREVIEW_SPEC, QueryPreviewProvider()),
        (QUERY_EXECUTE_SPEC, QueryExecuteProvider()),
        (DATA_PROFILE_SPEC, DataProfileProvider()),
        (RESULT_PROFILE_SPEC, ResultProfileProvider()),
        (ANALYSIS_COMPUTE_SPEC, AnalysisComputeProvider()),
        (CHART_RENDER_SPEC, ChartRenderProvider()),
        (EVIDENCE_COLLECT_SPEC, EvidenceCollectProvider()),
    )
    for spec, provider in entries:
        registry.register(spec, provider)
    registry.freeze()
    return registry


__all__ = ["DATASET_TOOL_REGISTRY_VERSION", "build_dataset_tool_registry"]
