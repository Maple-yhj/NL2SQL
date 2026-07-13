"""Validation and deterministic compilation for execution graph fragments."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from data_agent.runtime.composition import stable_digest

from .models import (
    BudgetLimits,
    ErrorBudget,
    GraphFragment,
    GraphSpec,
    NodeSpec,
    PLATFORM_BUDGET_CEILING,
    edge_condition_matches,
)
from data_agent.runtime.models import AgentMode


class GraphCompileError(ValueError):
    """Raised when a graph cannot be proven safe at compile time."""


class ExecutionGraphCompiler:
    """Merge fragments and enforce the public graph-IR invariants."""

    def compile(
        self,
        *,
        graph_id: str,
        version: str,
        entry_node: str,
        terminal_nodes: tuple[str, ...],
        fragments: Sequence[GraphFragment],
        limits: BudgetLimits,
    ) -> GraphSpec:
        nodes: list[NodeSpec] = []
        edges = []
        by_id: dict[str, NodeSpec] = {}
        for fragment in fragments:
            for node in fragment.nodes:
                if node.id in by_id:
                    raise GraphCompileError(f"duplicate node id: {node.id}")
                by_id[node.id] = node
                nodes.append(node)
            edges.extend(fragment.edges)

        if entry_node not in by_id:
            raise GraphCompileError("entry node is not declared")
        if not terminal_nodes or any(item not in by_id for item in terminal_nodes):
            raise GraphCompileError("terminal node is not declared")
        if len(terminal_nodes) != len(set(terminal_nodes)):
            raise GraphCompileError("terminal nodes must be unique")

        self._validate_limits(limits)
        self._validate_edges(by_id, edges)
        self._validate_dependencies(by_id)
        self._validate_error_routes(by_id)
        self._validate_normal_edges_are_acyclic(by_id, edges)
        self._validate_condition_partition(
            entry_node,
            terminal_nodes,
            by_id,
            edges,
        )
        self._validate_reachability(entry_node, by_id, edges)

        payload = {
            "graph_id": graph_id,
            "version": version,
            "entry_node": entry_node,
            "terminal_nodes": terminal_nodes,
            "nodes": nodes,
            "edges": edges,
            "limits": limits,
        }
        return GraphSpec(**payload, digest=stable_digest(payload))

    @staticmethod
    def _validate_limits(limits: BudgetLimits) -> None:
        ceiling = PLATFORM_BUDGET_CEILING
        for field_name in type(ceiling).model_fields:
            if getattr(limits, field_name) > getattr(ceiling, field_name):
                raise GraphCompileError(
                    f"{field_name} exceeds the platform budget ceiling"
                )

    @staticmethod
    def _validate_edges(by_id, edges) -> None:
        identities: set[tuple[object, ...]] = set()
        conditions: set[tuple[object, ...]] = set()
        for edge in edges:
            if edge.source not in by_id or edge.target not in by_id:
                raise GraphCompileError("edge references an unknown node")
            identity = (edge.source, edge.target, edge.condition, edge.artifact)
            if identity in identities:
                raise GraphCompileError("duplicate edge")
            identities.add(identity)
            condition_identity = (edge.source, edge.condition)
            if condition_identity in conditions:
                raise GraphCompileError(
                    f"ambiguous condition after node {edge.source}"
                )
            conditions.add(condition_identity)
            if edge.artifact is None:
                continue
            source = by_id[edge.source]
            target = by_id[edge.target]
            if edge.artifact not in source.outputs or edge.artifact not in target.inputs:
                raise GraphCompileError(
                    f"edge schema is incompatible: {edge.source}->{edge.target}"
                )

    @staticmethod
    def _validate_dependencies(by_id: dict[str, NodeSpec]) -> None:
        for node in by_id.values():
            if len(node.dependencies) != len(set(node.dependencies)):
                raise GraphCompileError("node dependencies must be unique")
            missing = set(node.dependencies) - set(by_id)
            if missing:
                raise GraphCompileError(
                    f"node dependency is not declared: {sorted(missing)[0]}"
                )
            if node.id in node.dependencies:
                raise GraphCompileError("node cannot depend on itself")

    @staticmethod
    def _validate_error_routes(by_id: dict[str, NodeSpec]) -> None:
        for node in by_id.values():
            for route in node.on_error:
                if route.terminal:
                    continue
                if route.target not in by_id:
                    raise GraphCompileError("error route references an unknown node")
                if route.max_attempts is None:
                    raise GraphCompileError("error route loop must have a static bound")

    @staticmethod
    def _validate_condition_partition(
        entry_node: str,
        terminal_nodes: tuple[str, ...],
        by_id: dict[str, NodeSpec],
        edges,
    ) -> None:
        """Prove exactly one success edge for every mode reachable at a node."""

        outgoing: dict[str, list[object]] = {node_id: [] for node_id in by_id}
        for edge in edges:
            outgoing[edge.source].append(edge)

        reachable_modes: dict[str, set[AgentMode]] = {
            node_id: set() for node_id in by_id
        }
        reachable_modes[entry_node].update(AgentMode)
        changed = True
        while changed:
            changed = False
            for node_id, modes in tuple(reachable_modes.items()):
                if not modes:
                    continue
                for edge in outgoing[node_id]:
                    matched = {
                        mode
                        for mode in modes
                        if edge_condition_matches(edge.condition, mode)
                    }
                    before = len(reachable_modes[edge.target])
                    reachable_modes[edge.target].update(matched)
                    changed |= len(reachable_modes[edge.target]) != before
                for route in by_id[node_id].on_error:
                    if route.target is None:
                        continue
                    matched = modes.intersection(route.allowed_modes)
                    before = len(reachable_modes[route.target])
                    reachable_modes[route.target].update(matched)
                    changed |= len(reachable_modes[route.target]) != before

        terminal = set(terminal_nodes)
        for node_id, modes in reachable_modes.items():
            if not modes:
                continue
            node_edges = outgoing[node_id]
            if node_id in terminal:
                if node_edges:
                    raise GraphCompileError("terminal node cannot define success edges")
                continue
            for mode in modes:
                matches = sum(
                    edge_condition_matches(edge.condition, mode)
                    for edge in node_edges
                )
                if matches > 1:
                    raise GraphCompileError(
                        f"conditions after {node_id} are not mutually exclusive "
                        f"for mode {mode.value}"
                    )
                if matches == 0:
                    raise GraphCompileError(
                        f"conditions after {node_id} are not complete "
                        f"for mode {mode.value}"
                    )

    @staticmethod
    def _validate_reachability(entry_node, by_id, edges) -> None:
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in by_id}
        for edge in edges:
            adjacency[edge.source].append(edge.target)
        for node in by_id.values():
            adjacency[node.id].extend(
                route.target
                for route in node.on_error
                if route.target is not None
            )
        reachable = {entry_node}
        queue = deque([entry_node])
        while queue:
            source = queue.popleft()
            for target in adjacency[source]:
                if target not in reachable:
                    reachable.add(target)
                    queue.append(target)
        missing = sorted(set(by_id) - reachable)
        if missing:
            raise GraphCompileError("unreachable nodes: " + ", ".join(missing))

    @staticmethod
    def _validate_normal_edges_are_acyclic(by_id, edges) -> None:
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in by_id}
        for edge in edges:
            adjacency[edge.source].append(edge.target)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise GraphCompileError("unbounded cycle in normal graph edges")
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in adjacency[node_id]:
                visit(target)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)


def graph_static_max_steps(graph: GraphSpec) -> int:
    """Return a conservative finite node-step bound for a compiled graph.

    Normal edges are a DAG. Error routes re-enter that DAG, but every route is
    constrained either by the shared correction/compile budgets or by its own
    local bound. Tool calls are included as an additional fail-closed guard so
    a backend recursion ceiling can never become tighter than graph policy.
    """

    adjacency: dict[str, tuple[str, ...]] = {
        node.id: tuple(
            edge.target for edge in graph.edges if edge.source == node.id
        )
        for node in graph.nodes
    }
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def longest_from(node_id: str) -> int:
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            raise GraphCompileError("cannot bound a normal-edge cycle")
        visiting.add(node_id)
        targets = adjacency[node_id]
        length = 1 + max((longest_from(target) for target in targets), default=0)
        visiting.remove(node_id)
        memo[node_id] = length
        return length

    longest_normal_path = max(longest_from(node.id) for node in graph.nodes)
    routes = tuple(route for node in graph.nodes for route in node.on_error)
    correction_local = sum(
        route.max_attempts or 0
        for route in routes
        if route.budget in {ErrorBudget.CORRECTION, ErrorBudget.DIAGNOSTIC}
    )
    compile_local = sum(
        route.max_attempts or 0
        for route in routes
        if route.budget == ErrorBudget.SQL_COMPILE
    )
    local_only = sum(
        route.max_attempts or 0
        for route in routes
        if not route.terminal and route.budget is None
    )
    route_reentries = (
        min(graph.limits.max_correction_rounds, correction_local)
        + min(graph.limits.max_sql_compile_attempts, compile_local)
        + local_only
    )
    return (
        longest_normal_path * (1 + route_reentries)
        + graph.limits.max_tool_calls
    )
