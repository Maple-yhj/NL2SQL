from __future__ import annotations

import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from data_agent.execution import (
    ArtifactKind,
    BudgetLimits,
    COMMERCE_EXECUTION_GRAPH,
    EdgeCondition,
    EdgeSpec,
    ErrorCode,
    ExecutionGraphCompiler,
    GraphCompileError,
    GraphFragment,
    NodeKind,
    NodeSpec,
)


def _node(
    node_id: str,
    *,
    inputs: tuple[ArtifactKind, ...] = (),
    outputs: tuple[ArtifactKind, ...] = (),
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        kind=NodeKind.PURE,
        inputs=inputs,
        outputs=outputs,
    )


def _compile(*fragments: GraphFragment, limits: BudgetLimits | None = None):
    return ExecutionGraphCompiler().compile(
        graph_id="test.graph",
        version="1.0.0",
        entry_node="start",
        terminal_nodes=("finish",),
        fragments=fragments,
        limits=limits or BudgetLimits(),
    )


class GraphModelsAndCompilerTests(unittest.TestCase):
    def test_builtin_graph_is_one_frozen_versioned_spec(self) -> None:
        graph = COMMERCE_EXECUTION_GRAPH

        self.assertEqual(graph.graph_id, "commerce.execution")
        self.assertEqual(graph.version, "1.0.0")
        self.assertEqual(len(graph.digest), 64)
        self.assertEqual(len(graph.nodes), len({node.id for node in graph.nodes}))
        self.assertEqual(
            {node.tool_ref for node in graph.nodes if node.tool_ref is not None},
            {
                "semantic.search",
                "data.inspect",
                "query.compile",
                "query.execute",
                "result.profile",
                "answer.render",
            },
        )
        self.assertEqual(
            graph.limits,
            BudgetLimits(
                max_correction_rounds=2,
                max_sql_compile_attempts=3,
                max_tool_calls=24,
                max_duration_seconds=120,
                max_result_rows=1000,
            ),
        )
        self.assertEqual(
            {
                route.code
                for node in graph.nodes
                for route in node.on_error
            },
            {
                ErrorCode.LOGICAL_PLAN_INVALID,
                ErrorCode.BINDING_STALE,
                ErrorCode.SQL_COMPILE_ERROR,
                ErrorCode.SQL_POLICY_VIOLATION,
                ErrorCode.COST_EXCEEDED,
                ErrorCode.EMPTY_RESULT,
                ErrorCode.JOIN_EXPLOSION,
                ErrorCode.ACCESS_DENIED,
                ErrorCode.RESULT_SEMANTIC_MISMATCH,
            },
        )
        plan_edges = [
            edge
            for edge in graph.edges
            if edge.condition == EdgeCondition.MODE_PLAN
        ]
        self.assertEqual(
            [(edge.source, edge.target) for edge in plan_edges],
            [("validate_query", "finalize")],
        )
        self.assertTrue(
            all(
                route.max_attempts is not None or route.terminal
                for node in graph.nodes
                for route in node.on_error
            )
        )
        with self.assertRaises(ValidationError):
            graph.nodes[0].id = "mutated"

    def test_compiler_rejects_duplicate_node_ids(self) -> None:
        first = GraphFragment(
            fragment_id="first",
            nodes=(
                _node("start", outputs=(ArtifactKind.RESOLVED_CONTEXT,)),
                _node(
                    "finish",
                    inputs=(ArtifactKind.RESOLVED_CONTEXT,),
                ),
            ),
            edges=(
                EdgeSpec(
                    source="start",
                    target="finish",
                    artifact=ArtifactKind.RESOLVED_CONTEXT,
                ),
            ),
        )
        duplicate = GraphFragment(
            fragment_id="second",
            nodes=(_node("start"),),
        )

        with self.assertRaisesRegex(GraphCompileError, "duplicate node"):
            _compile(first, duplicate)

    def test_compiler_rejects_incompatible_edge_schema(self) -> None:
        fragment = GraphFragment(
            fragment_id="bad-schema",
            nodes=(
                _node("start", outputs=(ArtifactKind.RESOLVED_CONTEXT,)),
                _node("finish", inputs=(ArtifactKind.SEMANTIC_MATCHES,)),
            ),
            edges=(
                EdgeSpec(
                    source="start",
                    target="finish",
                    artifact=ArtifactKind.RESOLVED_CONTEXT,
                ),
            ),
        )

        with self.assertRaisesRegex(GraphCompileError, "schema"):
            _compile(fragment)

    def test_compiler_rejects_unreachable_nodes(self) -> None:
        fragment = GraphFragment(
            fragment_id="unreachable",
            nodes=(
                _node("start", outputs=(ArtifactKind.RESOLVED_CONTEXT,)),
                _node("finish", inputs=(ArtifactKind.RESOLVED_CONTEXT,)),
                _node("orphan"),
            ),
            edges=(
                EdgeSpec(
                    source="start",
                    target="finish",
                    artifact=ArtifactKind.RESOLVED_CONTEXT,
                ),
            ),
        )

        with self.assertRaisesRegex(GraphCompileError, "unreachable"):
            _compile(fragment)

    def test_compiler_rejects_unbounded_normal_edge_cycle(self) -> None:
        fragment = GraphFragment(
            fragment_id="cycle",
            nodes=(
                _node("start"),
                _node("finish"),
            ),
            edges=(
                EdgeSpec(source="start", target="finish"),
                EdgeSpec(source="finish", target="start"),
            ),
        )

        with self.assertRaisesRegex(GraphCompileError, "unbounded cycle"):
            _compile(fragment)

    def test_compiler_rejects_limits_above_platform_ceiling(self) -> None:
        fragment = GraphFragment(
            fragment_id="limits",
            nodes=(_node("start"), _node("finish")),
            edges=(EdgeSpec(source="start", target="finish"),),
        )

        with self.assertRaisesRegex(GraphCompileError, "budget ceiling"):
            _compile(fragment, limits=BudgetLimits(max_tool_calls=25))

    def test_compiler_rejects_ambiguous_conditions_and_unknown_dependencies(self) -> None:
        ambiguous = GraphFragment(
            fragment_id="ambiguous",
            nodes=(
                _node("start"),
                _node("middle"),
                _node("finish"),
            ),
            edges=(
                EdgeSpec(source="start", target="middle"),
                EdgeSpec(source="start", target="finish"),
                EdgeSpec(source="middle", target="finish"),
            ),
        )
        with self.assertRaisesRegex(GraphCompileError, "ambiguous condition"):
            _compile(ambiguous)

        dependency = GraphFragment(
            fragment_id="dependency",
            nodes=(
                NodeSpec(
                    id="start",
                    kind=NodeKind.PURE,
                    dependencies=("missing",),
                ),
                _node("finish"),
            ),
            edges=(EdgeSpec(source="start", target="finish"),),
        )
        with self.assertRaisesRegex(GraphCompileError, "dependency"):
            _compile(dependency)

    def test_graph_digest_is_verified_when_deserializing(self) -> None:
        raw = COMMERCE_EXECUTION_GRAPH.model_dump(mode="json")
        raw["version"] = "1.0.1"

        with self.assertRaisesRegex(ValidationError, "digest"):
            type(COMMERCE_EXECUTION_GRAPH).model_validate(raw)

    def test_compiler_rejects_overlapping_and_incomplete_mode_conditions(self) -> None:
        overlapping = GraphFragment(
            fragment_id="overlapping",
            nodes=(
                _node("start"),
                _node("middle"),
                _node("finish"),
            ),
            edges=(
                EdgeSpec(source="start", target="middle"),
                EdgeSpec(
                    source="start",
                    target="finish",
                    condition=EdgeCondition.MODE_PLAN,
                ),
                EdgeSpec(source="middle", target="finish"),
            ),
        )
        with self.assertRaisesRegex(GraphCompileError, "mutually exclusive"):
            _compile(overlapping)

        incomplete = GraphFragment(
            fragment_id="incomplete",
            nodes=(_node("start"), _node("finish")),
            edges=(
                EdgeSpec(
                    source="start",
                    target="finish",
                    condition=EdgeCondition.MODE_PLAN,
                ),
            ),
        )
        with self.assertRaisesRegex(GraphCompileError, "complete"):
            _compile(incomplete)


if __name__ == "__main__":
    unittest.main()
