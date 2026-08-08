"""Deterministic, fail-closed routing over activated relationship graphs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from .models import (
    ActivatedRelationshipGraph,
    RelationshipEdge,
    RelationshipFinding,
    RelationshipGraphDraft,
    NonBlankText,
    StableIdentifier,
)


class GraphRouteError(ValueError):
    def __init__(self, code: Literal["GRAPH_NO_PATH", "GRAPH_AMBIGUOUS_PATH"], message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GraphRouteRequest:
    required_node_ids: tuple[StableIdentifier, ...]
    required_logical_refs: tuple[NonBlankText, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedJoinStep:
    edge_id: StableIdentifier
    existing_node_id: StableIdentifier
    introduced_node_id: StableIdentifier
    traversal: Literal["forward", "reverse"]


@dataclass(frozen=True, slots=True)
class ResolvedJoinGraph:
    root_node_id: StableIdentifier
    required_node_ids: tuple[StableIdentifier, ...]
    included_node_ids: tuple[StableIdentifier, ...]
    steps: tuple[ResolvedJoinStep, ...]
    route_rule_id: StableIdentifier | None
    route_digest: str


Graph = RelationshipGraphDraft | ActivatedRelationshipGraph


class GraphRouteResolver:
    """Resolve only safe executable trees; it never invents a physical JOIN."""

    def resolve(
        self,
        graph: Graph,
        request: GraphRouteRequest,
        *,
        findings: tuple[RelationshipFinding, ...] = (),
    ) -> ResolvedJoinGraph:
        required = tuple(dict.fromkeys(request.required_node_ids))
        nodes = {node.node_id: node for node in graph.nodes}
        if not required:
            raise GraphRouteError("GRAPH_NO_PATH", "a graph route requires at least one node")
        if any(node_id not in nodes or not nodes[node_id].enabled for node_id in required):
            raise GraphRouteError("GRAPH_NO_PATH", "a required graph node is missing or disabled")
        root = self._root(graph, required)
        if len(required) == 1:
            return self._result(root, required, (), None)
        edge_errors = {finding.edge_id for finding in findings if finding.severity == "error" and finding.edge_id}
        edges = tuple(edge for edge in graph.edges if edge.enabled and edge.edge_id not in edge_errors)
        rule = next(
            (
                candidate
                for candidate in graph.route_rules
                if set(required).issubset(candidate.terminal_node_ids)
            ),
            None,
        )
        if rule is not None:
            by_id = {edge.edge_id: edge for edge in edges}
            if not set(rule.ordered_edge_ids).issubset(by_id):
                raise GraphRouteError("GRAPH_NO_PATH", "preferred route includes a disabled or invalid edge")
            steps = self._rule_steps(root, tuple(by_id[item] for item in rule.ordered_edge_ids))
            reached = {root, *(step.introduced_node_id for step in steps)}
            if not set(required).issubset(reached):
                raise GraphRouteError("GRAPH_NO_PATH", "preferred route does not reach required nodes")
            return self._result(root, required, steps, rule.rule_id)

        included = {root}
        steps: list[ResolvedJoinStep] = []
        for target in required:
            if target in included:
                continue
            candidates = self._best_paths(included, target, edges)
            if not candidates:
                raise GraphRouteError("GRAPH_NO_PATH", "required graph nodes are not connected by safe edges")
            if len(candidates) > 1:
                raise GraphRouteError("GRAPH_AMBIGUOUS_PATH", "multiple equally safe relationship paths exist")
            for step in candidates[0]:
                if step.introduced_node_id not in included:
                    included.add(step.introduced_node_id)
                    steps.append(step)
        return self._result(root, required, tuple(steps), None)

    @staticmethod
    def _root(graph: Graph, required: tuple[str, ...]) -> str:
        anchors = {component.anchor_node_id for component in graph.components}
        # The request order is planner-derived and remains observable; we never
        # substitute an arbitrary ID sort as a business-path decision.
        return next((node_id for node_id in required if node_id in anchors), required[0])

    def _best_paths(
        self,
        starts: set[str],
        target: str,
        edges: tuple[RelationshipEdge, ...],
    ) -> tuple[tuple[ResolvedJoinStep, ...], ...]:
        paths: list[tuple[tuple[int, int, int], tuple[ResolvedJoinStep, ...]]] = []
        max_hops = max(1, len(edges) + 1)

        def visit(node: str, seen: set[str], steps: tuple[ResolvedJoinStep, ...], cost: tuple[int, int, int]) -> None:
            if len(steps) > max_hops:
                return
            if node == target:
                paths.append((cost, steps))
                return
            for edge in edges:
                step = self._traverse(edge, node)
                if step is None or step.introduced_node_id in seen:
                    continue
                visit(
                    step.introduced_node_id,
                    seen | {step.introduced_node_id},
                    steps + (step,),
                    (
                        cost[0] + _risk(edge),
                        cost[1] + edge.route_priority,
                        cost[2] + 1,
                    ),
                )

        for start in starts:
            visit(start, set(starts), (), (0, 0, 0))
        if not paths:
            return ()
        best_cost = min(cost for cost, _ in paths)
        unique = {
            tuple((step.edge_id, step.existing_node_id, step.introduced_node_id, step.traversal) for step in steps): steps
            for cost, steps in paths
            if cost == best_cost
        }
        return tuple(unique.values())

    @staticmethod
    def _traverse(edge: RelationshipEdge, existing: str) -> ResolvedJoinStep | None:
        if edge.join_semantics == "left":
            if existing != edge.preserve_node_id:
                return None
            introduced = edge.to_node_id if existing == edge.from_node_id else edge.from_node_id
            return ResolvedJoinStep(
                edge_id=edge.edge_id,
                existing_node_id=existing,
                introduced_node_id=introduced,
                traversal="forward" if existing == edge.from_node_id else "reverse",
            )
        if existing == edge.from_node_id:
            return ResolvedJoinStep(edge.edge_id, existing, edge.to_node_id, "forward")
        if existing == edge.to_node_id:
            return ResolvedJoinStep(edge.edge_id, existing, edge.from_node_id, "reverse")
        return None

    def _rule_steps(self, root: str, edges: tuple[RelationshipEdge, ...]) -> tuple[ResolvedJoinStep, ...]:
        included = {root}
        steps: list[ResolvedJoinStep] = []
        for edge in edges:
            candidates = tuple(
                step for node_id in included if (step := self._traverse(edge, node_id)) is not None
                and step.introduced_node_id not in included
            )
            if len(candidates) != 1:
                raise GraphRouteError("GRAPH_NO_PATH", "preferred route is not an executable join tree")
            steps.append(candidates[0])
            included.add(candidates[0].introduced_node_id)
        return tuple(steps)

    @staticmethod
    def _result(root: str, required: tuple[str, ...], steps: tuple[ResolvedJoinStep, ...], rule_id: str | None) -> ResolvedJoinGraph:
        included = (root, *(step.introduced_node_id for step in steps))
        payload = {"root": root, "required": required, "steps": [asdict(step) for step in steps], "rule": rule_id}
        digest = "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return ResolvedJoinGraph(root, required, tuple(dict.fromkeys(included)), steps, rule_id, digest)


def _risk(edge: RelationshipEdge) -> int:
    evidence = edge.quality.evidence_level if edge.quality else "low"
    return {"high": 0, "recommended": 1, "low": 5, "blocked": 1000}[evidence] + {
        "one_to_one": 0, "many_to_one": 1, "one_to_many": 2, "unknown": 6, "many_to_many": 12
    }[edge.cardinality]


__all__ = ["GraphRouteError", "GraphRouteRequest", "GraphRouteResolver", "ResolvedJoinGraph", "ResolvedJoinStep"]
