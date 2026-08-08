from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlglot import exp, parse_one

from data_agent.runtime.models import AgentResponse


MUTATING_SQL = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter)


def assert_trajectory_invariants(
    testcase: Any,
    state: dict[str, object],
    *,
    allowed_tools: Collection[str],
    minimum_tool_calls: int,
    maximum_tool_calls: int,
) -> None:
    observations = tuple(state.get("observations", ()))
    testcase.assertGreaterEqual(len(observations), minimum_tool_calls)
    testcase.assertLessEqual(len(observations), maximum_tool_calls)
    testcase.assertTrue(
        all(item.tool_name in allowed_tools for item in observations),
        [item.tool_name for item in observations],
    )
    budget = state["budget"]
    testcase.assertEqual(budget.tool_calls, len(observations))
    authority = state.get("authority")
    if authority is not None:
        for item in tuple(state.get("evidence_refs", ())):
            testcase.assertEqual(item.source_id, authority.source_id)
            testcase.assertEqual(item.source_version, authority.source_version)
            testcase.assertEqual(item.binding_id, authority.binding_id)
            testcase.assertEqual(item.binding_version, authority.binding_version)
            testcase.assertEqual(item.schema_fingerprint, authority.schema_fingerprint)


def assert_evidence_and_pins(testcase: Any, response: AgentResponse) -> None:
    testcase.assertTrue(response.ok, response.error)
    testcase.assertIsNotNone(response.version_pins)
    artifact_ids = {item.artifact_id for item in response.artifacts}
    testcase.assertTrue(artifact_ids)
    testcase.assertTrue(response.evidence)
    testcase.assertTrue(
        all(item.artifact_id in artifact_ids for item in response.evidence)
    )
    testcase.assertTrue(
        all(
            set(step.evidence_ids).issubset(
                {item.evidence_id for item in response.evidence}
            )
            for step in response.analysis_steps
        )
    )


def assert_read_only_sql(
    testcase: Any,
    sql: str,
    *,
    allowed_relations: Collection[str],
) -> str:
    tree = parse_one(sql, read="duckdb")
    testcase.assertFalse(any(tree.find_all(MUTATING_SQL)))
    relations = {
        ".".join(filter(None, (table.db, table.name)))
        for table in tree.find_all(exp.Table)
    }
    testcase.assertTrue(relations)
    testcase.assertLessEqual(relations, set(allowed_relations))
    return tree.sql(dialect="duckdb")


__all__ = [
    "assert_evidence_and_pins",
    "assert_read_only_sql",
    "assert_trajectory_invariants",
]
