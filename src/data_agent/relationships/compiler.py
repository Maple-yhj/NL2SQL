"""Bind a resolved graph route to aliased physical relations without model input."""

from __future__ import annotations

from dataclasses import dataclass
from sqlglot import exp

from data_agent.tools.schemas import CatalogSnapshot

from .models import ActivatedRelationshipGraph, RelationshipGraphDraft
from .router import ResolvedJoinGraph


@dataclass(frozen=True, slots=True)
class BoundJoinCondition:
    left_alias: str
    left_column: str
    right_alias: str
    right_column: str


@dataclass(frozen=True, slots=True)
class BoundJoinStep:
    edge_id: str
    relation: str
    alias: str
    join_type: str
    conditions: tuple[BoundJoinCondition, ...]


@dataclass(frozen=True, slots=True)
class BoundJoinPlan:
    root_node_id: str
    root_relation: str
    aliases: dict[str, str]
    steps: tuple[BoundJoinStep, ...]
    route_digest: str


def bind_join_plan(*, graph: RelationshipGraphDraft | ActivatedRelationshipGraph, catalog: CatalogSnapshot, route: ResolvedJoinGraph) -> BoundJoinPlan:
    nodes = {node.node_id: node for node in graph.nodes}
    relations = {relation.relation_id: relation for relation in catalog.relations}
    columns = {column.column_id: column.name for relation in catalog.relations for column in relation.columns}
    edges = {edge.edge_id: edge for edge in graph.edges}
    aliases = {route.root_node_id: _alias(nodes[route.root_node_id].role_name, 1)}
    root_relation = relations[nodes[route.root_node_id].relation_id].relation
    steps: list[BoundJoinStep] = []
    for index, step in enumerate(route.steps, start=2):
        edge = edges[step.edge_id]
        introduced = nodes[step.introduced_node_id]
        aliases[step.introduced_node_id] = _alias(introduced.role_name, index)
        forward = step.traversal == "forward"
        conditions = tuple(
            BoundJoinCondition(
                left_alias=aliases[step.existing_node_id],
                left_column=columns[condition.from_column_id if forward else condition.to_column_id],
                right_alias=aliases[step.introduced_node_id],
                right_column=columns[condition.to_column_id if forward else condition.from_column_id],
            )
            for condition in edge.conditions
        )
        steps.append(BoundJoinStep(edge.edge_id, relations[introduced.relation_id].relation, aliases[step.introduced_node_id], edge.join_semantics.upper(), conditions))
    return BoundJoinPlan(route.root_node_id, root_relation, aliases, tuple(steps), route.route_digest)


def _alias(role: str, index: int) -> str:
    safe = "".join(char.lower() if char.isalnum() else "_" for char in role).strip("_") or "node"
    return f"node_{safe[:48]}_{index}"


def sqlglot_from_bound_plan(plan: BoundJoinPlan) -> tuple[exp.Table, tuple[exp.Join, ...]]:
    """Render only the physical join skeleton; projections/filters remain logical-plan work."""
    def table(relation: str, alias: str) -> exp.Table:
        schema, name = relation.split(".", 1)
        return exp.Table(this=exp.to_identifier(name, quoted=True), db=exp.to_identifier(schema, quoted=True), alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
    root = table(plan.root_relation, plan.aliases[plan.root_node_id])
    joins: list[exp.Join] = []
    for step in plan.steps:
        predicates = [
            exp.EQ(
                this=exp.Column(this=exp.to_identifier(condition.left_column, quoted=True), table=exp.to_identifier(condition.left_alias, quoted=True)),
                expression=exp.Column(this=exp.to_identifier(condition.right_column, quoted=True), table=exp.to_identifier(condition.right_alias, quoted=True)),
            ) for condition in step.conditions
        ]
        on = predicates[0]
        for predicate in predicates[1:]:
            on = exp.and_(on, predicate)
        joins.append(exp.Join(this=table(step.relation, step.alias), on=on, kind=step.join_type))
    return root, tuple(joins)


__all__ = ["BoundJoinCondition", "BoundJoinPlan", "BoundJoinStep", "bind_join_plan", "sqlglot_from_bound_plan"]
