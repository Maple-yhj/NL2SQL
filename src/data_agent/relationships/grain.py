"""Fail-closed aggregation safety for graph routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import ActivatedRelationshipGraph, RelationshipGraphDraft
from .router import ResolvedJoinGraph


class GraphFanoutError(ValueError):
    code = "GRAPH_UNSAFE_FANOUT"


@dataclass(frozen=True, slots=True)
class CardinalityPropagation:
    """Cardinality observed from the route root at every introduced role.

    ``many`` is sticky: a later many-to-one edge cannot prove that rows which
    already expanded have become unique again.  This intentionally makes the
    guard conservative for aggregate measures.
    """

    root_node_id: str
    node_cardinality: tuple[tuple[str, Literal["one", "many", "unknown"]], ...]
    expansion_edge_ids: tuple[str, ...]
    unknown_edge_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MeasureNativeGrain:
    """The graph-declared native grain for one measure-bearing role."""

    node_id: str
    grain_column_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreAggregationContract:
    """Auditable contract a compiler must satisfy before crossing fan-out.

    This is deliberately a data contract rather than an implicit SQL rewrite:
    a future pre-aggregation implementation must aggregate each measure role
    at these graph-declared native grains before any listed expanding edge is
    joined.  Until a compiler consumes it, ``require_safe`` continues to fail
    closed.
    """

    measure_native_grains: tuple[MeasureNativeGrain, ...]
    expanding_edge_ids: tuple[str, ...]
    aggregation_boundary_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FanoutDecision:
    safe: bool
    expands_measure: bool
    reason: str
    preaggregation_required: bool = False
    cardinality: CardinalityPropagation | None = None
    measure_native_grains: tuple[MeasureNativeGrain, ...] = ()
    preaggregation_contract: PreAggregationContract | None = None


class FanoutGuard:
    """Prevent aggregate measures from silently crossing an expanding edge."""

    def assess(
        self,
        *,
        graph: RelationshipGraphDraft | ActivatedRelationshipGraph,
        route: ResolvedJoinGraph,
        measure_node_ids: tuple[str, ...],
        analysis_type: str,
    ) -> FanoutDecision:
        propagation = self.propagate_cardinality(graph=graph, route=route)
        native_grains = self.measure_native_grains(graph=graph, measure_node_ids=measure_node_ids)
        if analysis_type == "detail":
            return FanoutDecision(
                True,
                False,
                "detail queries may expand rows under the result budget",
                cardinality=propagation,
                measure_native_grains=native_grains,
            )
        if propagation.unknown_edge_ids:
            edges = {edge.edge_id: edge for edge in graph.edges}
            kinds = ", ".join(
                sorted({edges[edge_id].cardinality for edge_id in propagation.unknown_edge_ids})
            )
            return FanoutDecision(
                False,
                True,
                f"{kinds} relationship requires explicit grain rules",
                preaggregation_required=True,
                cardinality=propagation,
                measure_native_grains=native_grains,
                preaggregation_contract=self._preaggregation_contract(
                    native_grains, propagation, route
                ),
            )
        expanded_from = {
            step.existing_node_id
            for step in route.steps
            if step.edge_id in propagation.expansion_edge_ids
        }
        unsafe = set(measure_node_ids) & expanded_from
        if unsafe:
            return FanoutDecision(
                False,
                True,
                "aggregate measure crosses a one-to-many expansion",
                preaggregation_required=True,
                cardinality=propagation,
                measure_native_grains=native_grains,
                preaggregation_contract=self._preaggregation_contract(
                    native_grains, propagation, route
                ),
            )
        return FanoutDecision(
            True,
            False,
            "aggregate route preserves measure grain",
            cardinality=propagation,
            measure_native_grains=native_grains,
        )

    def propagate_cardinality(
        self,
        *,
        graph: RelationshipGraphDraft | ActivatedRelationshipGraph,
        route: ResolvedJoinGraph,
    ) -> CardinalityPropagation:
        """Propagate route-root cardinality in deterministic route order."""

        edges = {edge.edge_id: edge for edge in graph.edges}
        cardinality: dict[str, Literal["one", "many", "unknown"]] = {
            route.root_node_id: "one"
        }
        expansion_edges: list[str] = []
        unknown_edges: list[str] = []
        for step in route.steps:
            edge = edges[step.edge_id]
            existing = cardinality[step.existing_node_id]
            if edge.cardinality in {"many_to_many", "unknown"}:
                introduced: Literal["one", "many", "unknown"] = "unknown"
                unknown_edges.append(edge.edge_id)
            elif existing == "unknown":
                introduced = "unknown"
            elif self._expands(edge.cardinality, step.traversal):
                introduced = "many"
                expansion_edges.append(edge.edge_id)
            else:
                introduced = existing
            cardinality[step.introduced_node_id] = introduced
        return CardinalityPropagation(
            root_node_id=route.root_node_id,
            node_cardinality=tuple(cardinality.items()),
            expansion_edge_ids=tuple(expansion_edges),
            unknown_edge_ids=tuple(unknown_edges),
        )

    @staticmethod
    def measure_native_grains(
        *,
        graph: RelationshipGraphDraft | ActivatedRelationshipGraph,
        measure_node_ids: tuple[str, ...],
    ) -> tuple[MeasureNativeGrain, ...]:
        """Return each measure role's declared component grain, never infer it."""

        component_grains = {
            component.anchor_node_id: component.grain_column_ids
            for component in graph.components
        }
        return tuple(
            MeasureNativeGrain(node_id=node_id, grain_column_ids=component_grains.get(node_id, ()))
            for node_id in dict.fromkeys(measure_node_ids)
        )

    @staticmethod
    def _expands(cardinality: str, traversal: str) -> bool:
        return (cardinality == "one_to_many" and traversal == "forward") or (
            cardinality == "many_to_one" and traversal == "reverse"
        )

    @staticmethod
    def _preaggregation_contract(
        native_grains: tuple[MeasureNativeGrain, ...],
        propagation: CardinalityPropagation,
        route: ResolvedJoinGraph,
    ) -> PreAggregationContract:
        expanding = set(propagation.expansion_edge_ids) | set(propagation.unknown_edge_ids)
        boundaries = tuple(
            step.existing_node_id for step in route.steps if step.edge_id in expanding
        )
        return PreAggregationContract(
            measure_native_grains=native_grains,
            expanding_edge_ids=tuple(sorted(expanding)),
            aggregation_boundary_node_ids=boundaries,
        )

    def require_safe(self, **kwargs: object) -> FanoutDecision:
        decision = self.assess(**kwargs)  # type: ignore[arg-type]
        if not decision.safe:
            raise GraphFanoutError(decision.reason)
        return decision


__all__ = [
    "CardinalityPropagation",
    "FanoutDecision",
    "FanoutGuard",
    "GraphFanoutError",
    "MeasureNativeGrain",
    "PreAggregationContract",
]
