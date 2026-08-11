"""Deterministic SQLGlot compiler for governed dataset logical plans."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from sqlglot import exp

from data_agent.datasources import (
    SemanticBindingRecord,
    SemanticFieldMapping,
    SemanticGraphBindingRecord,
    SemanticRelationship,
)
from data_agent.relationships.compiler import bind_join_plan
from data_agent.relationships.grain import FanoutGuard
from data_agent.relationships.router import (
    GraphRouteError,
    GraphRouteRequest,
    GraphRouteResolver,
)
from data_agent.dataset_query.contracts import (
    AnalysisType,
    LogicalQueryPlan,
    PreparedQuery,
    QueryParameter,
    RelationshipRouteEvidence,
    ResultShape,
)
from data_agent.tools.schemas import CatalogSnapshot

from .models import (
    DatasetFilterOperator,
    DatasetPlanStatus,
    DatasetQueryPlan,
    Scalar,
)
from .program import DatasetQueryProgram


class DatasetQueryCompiler:
    def compile(
        self,
        *,
        plan: DatasetQueryPlan | DatasetQueryProgram,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        dialect: Literal["postgres", "sqlite", "duckdb"],
        schema_fingerprint: str,
        bundle_digest: str,
        catalog: CatalogSnapshot | None = None,
        _relation_table_overrides: dict[str, str] | None = None,
        _join_conditions_overrides: dict[
            str,
            tuple[tuple[str, str], ...],
        ]
        | None = None,
        _alias_overrides: dict[str, str] | None = None,
    ) -> PreparedQuery:
        if isinstance(plan, DatasetQueryProgram):
            if catalog is None:
                raise ValueError("dataset query program compilation requires a catalog snapshot")
            from .program_compiler import DatasetQueryProgramCompiler

            return DatasetQueryProgramCompiler().compile(
                program=plan,
                binding=binding,
                catalog=catalog,
                dialect=dialect,
                schema_fingerprint=schema_fingerprint,
                bundle_digest=bundle_digest,
            )
        if isinstance(binding, SemanticGraphBindingRecord):
            if catalog is None:
                raise ValueError("graph binding compilation requires a catalog snapshot")
            return self._compile_graph(
                plan=plan,
                binding=binding,
                catalog=catalog,
                dialect=dialect,
                schema_fingerprint=schema_fingerprint,
                bundle_digest=bundle_digest,
            )
        if plan.status != DatasetPlanStatus.READY:
            raise ValueError("non-ready dataset query plans cannot be compiled")
        mappings = {item.logical_ref: item for item in binding.mappings}
        referenced = {
            *plan.select,
            *plan.group_by,
            *(item.ref for item in plan.aggregations),
            *(item.ref for item in plan.filters),
            *(item.ref for item in plan.order_by),
        }
        aggregation_aliases = {item.alias for item in plan.aggregations}
        unknown = referenced - set(mappings) - aggregation_aliases
        if unknown:
            raise ValueError(
                "dataset query references unknown logical fields: "
                + ", ".join(sorted(unknown))
            )
        physical_refs = referenced - aggregation_aliases
        relations = {mappings[ref].physical_relation for ref in physical_refs}
        primary_relation = binding.primary_relation or binding.mappings[0].physical_relation
        relationship_by_right = {
            item.right_relation: item for item in binding.relationships
        }
        required_relationship_ids: set[str] = set()
        for relation in relations:
            current = relation
            visited: set[str] = set()
            while current != primary_relation:
                if current in visited:
                    raise ValueError("dataset relationship graph contains a cycle")
                visited.add(current)
                relationship = relationship_by_right.get(current)
                if relationship is None:
                    raise ValueError(
                        "dataset query relations are not connected to the primary relation"
                    )
                required_relationship_ids.add(relationship.relationship_id)
                current = relationship.left_relation
        required_relationships = tuple(
            item
            for item in binding.relationships
            if item.relationship_id in required_relationship_ids
        )
        joined_relations = (
            primary_relation,
            *(item.right_relation for item in required_relationships),
        )
        aliases = _alias_overrides or {
            relation: f"dataset_{index}"
            for index, relation in enumerate(joined_relations, start=1)
        }

        def table_expression(relation: str) -> exp.Table:
            physical_relation = (_relation_table_overrides or {}).get(relation, relation)
            if physical_relation.count(".") != 1:
                raise ValueError("physical relation must be schema-qualified")
            schema, table = physical_relation.split(".", 1)
            return exp.Table(
                this=exp.to_identifier(table, quoted=True),
                db=exp.to_identifier(schema, quoted=True),
                alias=exp.TableAlias(
                    this=exp.to_identifier(aliases[relation], quoted=True)
                ),
            )

        source = table_expression(primary_relation)
        parameters: list[QueryParameter] = []

        def column(ref: str) -> exp.Column:
            mapping = mappings[ref]
            return exp.Column(
                this=exp.to_identifier(mapping.physical_column, quoted=True),
                table=exp.to_identifier(
                    aliases[mapping.physical_relation],
                    quoted=True,
                ),
            )

        def parameter(
            value: Scalar,
            purpose: Literal["filter", "limit"],
        ) -> exp.Parameter:
            position = len(parameters) + 1
            parameters.append(
                QueryParameter(position=position, value=value, purpose=purpose)
            )
            return exp.Parameter(this=exp.Var(this=str(position)))

        selections: list[exp.Expression] = []
        selected_refs = plan.select if plan.analysis_type == "detail" else plan.group_by
        used_aliases: set[str] = set()
        for ref in selected_refs:
            alias = self._column_alias(ref, used_aliases)
            selections.append(exp.alias_(column(ref), alias, quoted=True))
        for aggregation in plan.aggregations:
            argument = column(aggregation.ref)
            if aggregation.operation == "count_distinct":
                expression = exp.Count(this=exp.Distinct(expressions=[argument]))
            else:
                expression = {
                    "count": exp.Count,
                    "sum": exp.Sum,
                    "avg": exp.Avg,
                    "min": exp.Min,
                    "max": exp.Max,
                }[aggregation.operation](this=argument)
            selections.append(exp.alias_(expression, aggregation.alias, quoted=True))
        query = exp.select(*selections).from_(source)
        joins: list[exp.Join] = []
        for relationship in required_relationships:
            conditions = (_join_conditions_overrides or {}).get(
                relationship.relationship_id,
                ((relationship.left_column, relationship.right_column),),
            )
            predicates = [
                exp.EQ(
                    this=exp.Column(
                        this=exp.to_identifier(left_column, quoted=True),
                        table=exp.to_identifier(
                            aliases[relationship.left_relation],
                            quoted=True,
                        ),
                    ),
                    expression=exp.Column(
                        this=exp.to_identifier(right_column, quoted=True),
                        table=exp.to_identifier(
                            aliases[relationship.right_relation],
                            quoted=True,
                        ),
                    ),
                )
                for left_column, right_column in conditions
            ]
            on = predicates[0]
            for predicate in predicates[1:]:
                on = exp.and_(on, predicate)
            joins.append(
                exp.Join(
                    this=table_expression(relationship.right_relation),
                    on=on,
                    kind=relationship.join_type.value.upper(),
                )
            )
        if joins:
            query.set("joins", joins)

        predicates: list[exp.Expression] = []
        for item in plan.filters:
            left = column(item.ref)
            if item.operator == DatasetFilterOperator.IS_NULL:
                predicates.append(exp.Is(this=left, expression=exp.Null()))
            elif item.operator == DatasetFilterOperator.IS_NOT_NULL:
                predicates.append(exp.Not(this=exp.Is(this=left, expression=exp.Null())))
            elif item.operator == DatasetFilterOperator.IN:
                assert isinstance(item.value, tuple)
                predicates.append(
                    exp.In(
                        this=left,
                        expressions=[parameter(value, "filter") for value in item.value],
                    )
                )
            elif item.operator == DatasetFilterOperator.CONTAINS:
                assert isinstance(item.value, (str, int, float, bool))
                predicates.append(
                    exp.Like(
                        this=left,
                        expression=parameter(f"%{item.value}%", "filter"),
                    )
                )
            else:
                assert isinstance(item.value, (str, int, float, bool))
                right = parameter(item.value, "filter")
                predicate_type = {
                    DatasetFilterOperator.EQ: exp.EQ,
                    DatasetFilterOperator.NEQ: exp.NEQ,
                    DatasetFilterOperator.GT: exp.GT,
                    DatasetFilterOperator.GTE: exp.GTE,
                    DatasetFilterOperator.LT: exp.LT,
                    DatasetFilterOperator.LTE: exp.LTE,
                }[item.operator]
                predicates.append(predicate_type(this=left, expression=right))
        if predicates:
            combined = predicates[0]
            for predicate in predicates[1:]:
                combined = exp.and_(combined, predicate)
            query = query.where(combined)
        if plan.group_by:
            query.set("group", exp.Group(expressions=[column(ref) for ref in plan.group_by]))
        if plan.order_by:
            ordered: list[exp.Ordered] = []
            for item in plan.order_by:
                if item.ref in aggregation_aliases:
                    expression = exp.Column(this=exp.to_identifier(item.ref, quoted=True))
                else:
                    expression = column(item.ref)
                ordered.append(
                    exp.Ordered(this=expression, desc=item.direction == "desc")
                )
            query.set("order", exp.Order(expressions=ordered))
        query.set("limit", exp.Limit(expression=parameter(plan.limit, "limit")))
        sql = query.sql(dialect=dialect, pretty=False)
        sql_hash = hashlib.sha256(sql.encode()).hexdigest()
        logical_plan = LogicalQueryPlan(
            analysis_type=(
                AnalysisType.DETAIL
                if plan.analysis_type == "detail"
                else AnalysisType.METRIC
            ),
            assumptions=("compiled from an activated user-dataset semantic binding",),
            limit=plan.limit,
            result_shape=ResultShape.TABLE,
        )
        return PreparedQuery(
            dialect=dialect,
            logical_plan=logical_plan,
            logical_plan_hash=logical_plan.stable_hash(),
            sql_ast_hash=sql_hash,
            logical_sql=sql,
            executable_sql=sql,
            parameters=tuple(parameters),
            allowed_relations=tuple(
                dict.fromkeys(
                    (_relation_table_overrides or {}).get(relation, relation)
                    for relation in joined_relations
                )
            ),
            policy_decision_id=hashlib.sha256(
                f"{binding.binding_id}:{binding.version}".encode()
            ).hexdigest(),
            estimated_cost=0,
            max_rows=plan.limit,
            bundle_digest=bundle_digest,
            schema_fingerprint=schema_fingerprint,
        )

    def _compile_graph(
        self,
        *,
        plan: DatasetQueryPlan,
        binding: SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
        dialect: Literal["postgres", "sqlite", "duckdb"],
        schema_fingerprint: str,
        bundle_digest: str,
    ) -> PreparedQuery:
        mappings = {mapping.logical_ref: mapping for mapping in binding.mappings}
        referenced = {
            *plan.select,
            *plan.group_by,
            *(item.ref for item in plan.aggregations),
            *(item.ref for item in plan.filters),
            *(item.ref for item in plan.order_by),
        }
        aggregation_aliases = {item.alias for item in plan.aggregations}
        unknown = referenced - set(mappings) - aggregation_aliases
        if unknown:
            raise ValueError(
                "dataset query references unknown logical fields: "
                + ", ".join(sorted(unknown))
            )
        ordered_refs = tuple(
            dict.fromkeys(
                (
                    *plan.select,
                    *plan.group_by,
                    *(item.ref for item in plan.aggregations),
                    *(item.ref for item in plan.filters),
                    *(item.ref for item in plan.order_by),
                )
            )
        )
        required_node_ids = tuple(
            dict.fromkeys(
                mappings[ref].node_id
                for ref in ordered_refs
                if ref not in aggregation_aliases
            )
        )
        try:
            route = GraphRouteResolver().resolve(
                binding.graph,
                GraphRouteRequest(required_node_ids=required_node_ids),
            )
        except GraphRouteError as exc:
            raise ValueError(f"{exc.code}: {exc}") from exc
        measure_nodes = tuple(
            mappings[item.ref].node_id
            for item in plan.aggregations
            if item.ref in mappings
        )
        try:
            fanout = FanoutGuard().require_safe(
                graph=binding.graph,
                route=route,
                measure_node_ids=measure_nodes,
                analysis_type=plan.analysis_type or "detail",
            )
        except ValueError as exc:
            raise ValueError(f"GRAPH_UNSAFE_FANOUT: {exc}") from exc
        bound = bind_join_plan(graph=binding.graph, catalog=catalog, route=route)
        relations = {relation.relation_id: relation for relation in catalog.relations}
        columns = {
            column.column_id: column.name
            for relation in catalog.relations
            for column in relation.columns
        }
        node_by_id = {node.node_id: node for node in binding.graph.nodes}
        route_node_ids = tuple(
            dict.fromkeys(
                (
                    route.root_node_id,
                    *(step.introduced_node_id for step in route.steps),
                )
            )
        )
        synthetic_mappings = [
            SemanticFieldMapping(
                logical_ref=mapping.logical_ref,
                physical_relation=mapping.node_id,
                physical_column=columns[mapping.column_id],
            )
            for mapping in binding.mappings
            if mapping.node_id in route_node_ids
        ]
        mapped_nodes = {mapping.physical_relation for mapping in synthetic_mappings}
        for node_id in route_node_ids:
            if node_id not in mapped_nodes:
                relation = relations[node_by_id[node_id].relation_id]
                synthetic_mappings.append(
                    SemanticFieldMapping(
                        logical_ref=f"__graph_internal_{node_id}",
                        physical_relation=node_id,
                        physical_column=relation.columns[0].name,
                    )
                )
        by_step = {step.edge_id: step for step in bound.steps}
        synthetic_relationships = tuple(
            SemanticRelationship(
                relationship_id=step.edge_id,
                left_relation=route.steps[index].existing_node_id,
                left_column=step.conditions[0].left_column,
                right_relation=route.steps[index].introduced_node_id,
                right_column=step.conditions[0].right_column,
                join_type="left" if step.join_type == "LEFT" else "inner",
            )
            for index, step in enumerate(bound.steps)
        )
        synthetic = SemanticBindingRecord(
            binding_id=binding.binding_id,
            tenant_id=binding.tenant_id,
            source_id=binding.source_id,
            source_snapshot_version=binding.source_snapshot_version,
            domain_id=binding.domain_id,
            version=binding.version,
            status=binding.status,
            mappings=tuple(synthetic_mappings),
            primary_relation=route.root_node_id,
            relationships=synthetic_relationships,
        )
        prepared = self.compile(
            plan=plan,
            binding=synthetic,
            dialect=dialect,
            schema_fingerprint=schema_fingerprint,
            bundle_digest=bundle_digest,
            _relation_table_overrides={
                node_id: relations[node_by_id[node_id].relation_id].relation
                for node_id in route_node_ids
            },
            _alias_overrides=bound.aliases,
            _join_conditions_overrides={
                edge_id: tuple(
                    (condition.left_column, condition.right_column)
                    for condition in step.conditions
                )
                for edge_id, step in by_step.items()
            },
        )
        logical_plan = prepared.logical_plan.model_copy(
            update={
                "assumptions": (
                    *prepared.logical_plan.assumptions,
                    f"relationship route digest: {route.route_digest}",
                    "relationship nodes: " + ", ".join(route.included_node_ids),
                    "relationship edges: "
                    + ", ".join(step.edge_id for step in route.steps),
                    f"fan-out decision: {fanout.reason}",
                ),
                "relationship_evidence": RelationshipRouteEvidence(
                    route_digest=route.route_digest,
                    logical_node_ids=route.included_node_ids,
                    edge_ids=tuple(step.edge_id for step in route.steps),
                    cardinality_by_node=fanout.cardinality.node_cardinality,
                    fanout_decision=fanout.reason,
                    preaggregation_required=fanout.preaggregation_required,
                ),
            }
        )
        return prepared.model_copy(
            update={
                "logical_plan": logical_plan,
                "logical_plan_hash": logical_plan.stable_hash(),
            }
        )

    @staticmethod
    def _column_alias(ref: str, used: set[str]) -> str:
        raw = re.sub(r"[^a-zA-Z0-9_]+", "_", ref.rsplit(".", 1)[-1]).lower()
        raw = raw.strip("_") or "field"
        if raw[0].isdigit():
            raw = "field_" + raw
        base = raw[:55]
        alias = base
        suffix = 2
        while alias in used:
            marker = f"_{suffix}"
            alias = base[: 63 - len(marker)] + marker
            suffix += 1
        used.add(alias)
        return alias


__all__ = ["DatasetQueryCompiler"]
