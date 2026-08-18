"""Deterministic SQLGlot lowering for versioned dataset query programs."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlglot import exp

from data_agent.datasources import SemanticBindingRecord, SemanticGraphBindingRecord
from data_agent.relationships.compiler import bind_join_plan, sqlglot_from_bound_plan
from data_agent.relationships.grain import FanoutGuard
from data_agent.relationships.router import (
    GraphRouteError,
    GraphRouteRequest,
    GraphRouteResolver,
)
from data_agent.semantic_metrics import (
    EffectiveMetricCatalog,
    LegacyMetricAdapter,
    MetricCatalogEntry,
    MetricCatalogOrigin,
    SemanticMetricDefinitionV2,
    SemanticMetricSqlCompiler,
)
from data_agent.tools.schemas import CatalogSnapshot

from .contracts import AnalysisType, LogicalQueryPlan, PreparedQuery, QueryParameter, ResultShape
from .models import DatasetPlanStatus, Scalar
from .program import (
    DatasetAggregateExpression,
    DatasetBinaryExpression,
    DatasetFieldExpression,
    DatasetFunctionExpression,
    DatasetJoinSource,
    DatasetLiteralExpression,
    DatasetMetricExpression,
    DatasetOutputExpression,
    DatasetQueryProgram,
    DatasetQueryStage,
    DatasetRootSource,
    DatasetScalarExpression,
    DatasetStageSource,
    DatasetUnaryExpression,
    DatasetUnionStage,
)


Dialect = Literal["postgres", "sqlite", "duckdb"]


@dataclass(frozen=True, slots=True)
class _StageScope:
    source: exp.Expression
    joins: tuple[exp.Join, ...]
    field_columns: dict[str, exp.Column]
    output_aliases: dict[str, str]
    allowed_relations: tuple[str, ...]
    assumptions: tuple[str, ...] = ()


class DatasetQueryProgramCompiler:
    """Compile a finite program DAG into one parameterized read-only SELECT."""

    def compile(
        self,
        *,
        program: DatasetQueryProgram,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        dialect: Dialect,
        schema_fingerprint: str,
        bundle_digest: str,
        catalog: CatalogSnapshot,
        metric_catalog: EffectiveMetricCatalog | None = None,
    ) -> PreparedQuery:
        if program.status != DatasetPlanStatus.READY:
            raise ValueError("non-ready dataset query programs cannot be compiled")

        metric_catalog = metric_catalog or self._legacy_metric_catalog(binding)
        parameters: list[QueryParameter] = []

        def parameter(value: Scalar | None, purpose: str = "filter") -> exp.Expression:
            if value is None:
                return exp.Null()
            position = len(parameters) + 1
            parameters.append(
                QueryParameter(position=position, value=value, purpose=purpose)
            )
            return exp.Parameter(this=exp.Var(this=str(position)))

        stage_queries: dict[str, exp.Expression] = {}
        stage_outputs: dict[str, tuple[str, ...]] = {}
        stage_lineage: dict[str, tuple[str, ...]] = {}
        allowed_relations: list[str] = []
        assumptions: list[str] = ["compiled from a governed dataset query program v2"]

        for stage in program.stages:
            if isinstance(stage, DatasetUnionStage):
                query, outputs = self._compile_union_stage(
                    stage=stage,
                    stage_outputs=stage_outputs,
                )
                lineage = tuple(
                    dict.fromkeys(
                        ref
                        for stage_id in stage.input_stage_ids
                        for ref in stage_lineage[stage_id]
                    )
                )
            else:
                lineage = self._stage_lineage(
                    stage=stage,
                    binding=binding,
                    prior=stage_lineage,
                    metric_catalog=metric_catalog,
                )
                scope = self._stage_scope(
                    stage=stage,
                    binding=binding,
                    catalog=catalog,
                    metric_catalog=metric_catalog,
                )
                query, outputs = self._compile_query_stage(
                    stage=stage,
                    scope=scope,
                    dialect=dialect,
                    parameter=parameter,
                    metric_catalog=metric_catalog,
                )
                allowed_relations.extend(scope.allowed_relations)
                assumptions.extend(scope.assumptions)
            stage_queries[stage.stage_id] = query
            stage_outputs[stage.stage_id] = outputs
            stage_lineage[stage.stage_id] = lineage

        output_names = stage_outputs[program.output_stage_id]  # type: ignore[index]
        output_table = self._cte_table(program.output_stage_id, "program_output")
        final = exp.select(
            *(
                exp.Column(
                    this=exp.to_identifier(name, quoted=True),
                    table=exp.to_identifier("program_output", quoted=True),
                )
                for name in output_names
            )
        ).from_(output_table)
        final.set(
            "limit",
            exp.Limit(expression=parameter(program.limit, "limit")),
        )
        for stage in program.stages:
            final = final.with_(stage.stage_id, as_=stage_queries[stage.stage_id])

        sql = final.sql(dialect=dialect, pretty=False)
        sql_hash = hashlib.sha256(sql.encode()).hexdigest()
        aggregate = any(
            isinstance(stage, DatasetQueryStage)
            and any(
                isinstance(item.expression, DatasetAggregateExpression)
                or isinstance(item.expression, DatasetMetricExpression)
                for item in stage.projections
            )
            for stage in program.stages
        )
        logical_plan = LogicalQueryPlan(
            analysis_type=AnalysisType.DERIVED if aggregate else AnalysisType.DETAIL,
            metrics=tuple(output_names) if aggregate else (),
            fields=tuple(output_names),
            expected_grain=tuple(output_names) if not aggregate else (),
            assumptions=tuple(dict.fromkeys(assumptions)),
            limit=program.limit,
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
            allowed_relations=tuple(dict.fromkeys(allowed_relations)),
            policy_decision_id=hashlib.sha256(
                f"{binding.binding_id}:{binding.version}:program-v2".encode()
            ).hexdigest(),
            estimated_cost=0,
            max_rows=program.limit,
            bundle_digest=bundle_digest,
            schema_fingerprint=schema_fingerprint,
        )

    def _stage_lineage(
        self,
        *,
        stage: DatasetQueryStage,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        prior: dict[str, tuple[str, ...]],
        metric_catalog: EffectiveMetricCatalog,
    ) -> tuple[str, ...]:
        if isinstance(stage.input, DatasetRootSource):
            return tuple(
                dict.fromkeys(
                    (
                        *((stage.input.anchor_ref,) if stage.input.anchor_ref else ()),
                        *self._stage_field_refs(stage),
                        *(
                            field_ref
                            for ref in self._stage_metric_refs(stage)
                            for field_ref in self._metric_definition(
                                metric_catalog, ref
                            ).ast_field_refs
                        ),
                    )
                )
            )
        if isinstance(stage.input, DatasetStageSource):
            return prior[stage.input.stage_id]
        refs = tuple(
            dict.fromkeys(
                (
                    *prior[stage.input.left_stage_id],
                    *prior[stage.input.right_stage_id],
                )
            )
        )
        if isinstance(binding, SemanticGraphBindingRecord):
            mappings = {item.logical_ref: item for item in binding.mappings}
            nodes = tuple(dict.fromkeys(mappings[ref].node_id for ref in refs))
            try:
                GraphRouteResolver().resolve(
                    binding.graph,
                    GraphRouteRequest(
                        required_node_ids=nodes,
                        required_logical_refs=refs,
                    ),
                )
            except GraphRouteError as exc:
                raise GraphRouteError(
                    exc.code,
                    "stage join cannot bypass the active semantic relationship "
                    f"graph: {exc}",
                ) from exc
        return refs

    def _stage_scope(
        self,
        *,
        stage: DatasetQueryStage,
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
        metric_catalog: EffectiveMetricCatalog,
    ) -> _StageScope:
        if isinstance(stage.input, DatasetRootSource):
            refs = list(self._stage_field_refs(stage))
            metric_refs = self._stage_metric_refs(stage)
            refs.extend(
                field_ref
                for ref in metric_refs
                for field_ref in self._metric_definition(
                    metric_catalog, ref
                ).ast_field_refs
            )
            if stage.input.anchor_ref is not None:
                refs.insert(0, stage.input.anchor_ref)
            ordered_refs = tuple(dict.fromkeys(refs))
            if not ordered_refs:
                raise ValueError(
                    f"dataset stage {stage.stage_id} requires a field reference or anchor_ref"
                )
            if isinstance(binding, SemanticGraphBindingRecord):
                return self._graph_scope(
                    stage=stage,
                    refs=ordered_refs,
                    binding=binding,
                    catalog=catalog,
                    metric_catalog=metric_catalog,
                )
            return self._legacy_scope(stage=stage, refs=ordered_refs, binding=binding)
        if isinstance(stage.input, DatasetStageSource):
            alias = f"src_{stage.stage_id}"
            return _StageScope(
                source=self._cte_table(stage.input.stage_id, alias),
                joins=(),
                field_columns={},
                output_aliases={stage.input.stage_id: alias},
                allowed_relations=(),
            )
        assert isinstance(stage.input, DatasetJoinSource)
        left_alias = f"left_{stage.stage_id}"
        right_alias = f"right_{stage.stage_id}"
        source = self._cte_table(stage.input.left_stage_id, left_alias)
        right = self._cte_table(stage.input.right_stage_id, right_alias)
        if stage.input.join_type == "cross":
            join = exp.Join(this=right, kind="CROSS")
        else:
            predicates = [
                exp.EQ(
                    this=self._output_column(left_alias, item.left_name),
                    expression=self._output_column(right_alias, item.right_name),
                )
                for item in stage.input.conditions
            ]
            on = predicates[0]
            for predicate in predicates[1:]:
                on = exp.and_(on, predicate)
            join = exp.Join(
                this=right,
                on=on,
                kind="LEFT" if stage.input.join_type == "left" else "INNER",
            )
        return _StageScope(
            source=source,
            joins=(join,),
            field_columns={},
            output_aliases={
                stage.input.left_stage_id: left_alias,
                stage.input.right_stage_id: right_alias,
            },
            allowed_relations=(),
        )

    def _graph_scope(
        self,
        *,
        stage: DatasetQueryStage,
        refs: tuple[str, ...],
        binding: SemanticGraphBindingRecord,
        catalog: CatalogSnapshot,
        metric_catalog: EffectiveMetricCatalog,
    ) -> _StageScope:
        mappings = {item.logical_ref: item for item in binding.mappings}
        unknown = set(refs) - set(mappings)
        if unknown:
            raise ValueError(
                "query program references unknown logical fields: "
                + ", ".join(sorted(unknown))
            )
        required_nodes = tuple(dict.fromkeys(mappings[ref].node_id for ref in refs))
        route = GraphRouteResolver().resolve(
            binding.graph,
            GraphRouteRequest(
                required_node_ids=required_nodes,
                required_logical_refs=refs,
            ),
        )
        measure_refs = (
            *self._stage_measure_refs(stage),
            *(
                field_ref
                for ref in self._stage_metric_refs(stage)
                for field_ref in self._metric_definition(
                    metric_catalog, ref
                ).ast_field_refs
            ),
        )
        fanout = FanoutGuard().require_safe(
            graph=binding.graph,
            route=route,
            measure_node_ids=tuple(
                dict.fromkeys(
                    mappings[ref].node_id for ref in measure_refs if ref in mappings
                )
            ),
            analysis_type=("aggregate" if measure_refs or stage.group_by else "detail"),
        )
        bound = bind_join_plan(graph=binding.graph, catalog=catalog, route=route)
        source, joins = sqlglot_from_bound_plan(bound)
        physical_columns = {
            column.column_id: column.name
            for relation in catalog.relations
            for column in relation.columns
        }
        relation_by_id = {item.relation_id: item.relation for item in catalog.relations}
        node_by_id = {item.node_id: item for item in binding.graph.nodes}
        field_columns = {
            ref: exp.Column(
                this=exp.to_identifier(physical_columns[mappings[ref].column_id], quoted=True),
                table=exp.to_identifier(bound.aliases[mappings[ref].node_id], quoted=True),
            )
            for ref in refs
        }
        allowed = tuple(
            dict.fromkeys(
                relation_by_id[node_by_id[node_id].relation_id]
                for node_id in route.included_node_ids
            )
        )
        return _StageScope(
            source=source,
            joins=joins,
            field_columns=field_columns,
            output_aliases={},
            allowed_relations=allowed,
            assumptions=(
                f"stage {stage.stage_id} relationship route digest: {route.route_digest}",
                f"stage {stage.stage_id} fan-out decision: {fanout.reason}",
            ),
        )

    def _legacy_scope(
        self,
        *,
        stage: DatasetQueryStage,
        refs: tuple[str, ...],
        binding: SemanticBindingRecord,
    ) -> _StageScope:
        mappings = {item.logical_ref: item for item in binding.mappings}
        unknown = set(refs) - set(mappings)
        if unknown:
            raise ValueError(
                "query program references unknown logical fields: "
                + ", ".join(sorted(unknown))
            )
        required_relations = tuple(
            dict.fromkeys(mappings[ref].physical_relation for ref in refs)
        )
        primary = (
            required_relations[0]
            if len(required_relations) == 1
            else binding.primary_relation or required_relations[0]
        )
        relationship_by_right = {item.right_relation: item for item in binding.relationships}
        required_relationship_ids: set[str] = set()
        for relation in required_relations:
            current = relation
            visited: set[str] = set()
            while current != primary:
                if current in visited:
                    raise ValueError("dataset relationship graph contains a cycle")
                visited.add(current)
                relationship = relationship_by_right.get(current)
                if relationship is None:
                    raise ValueError(
                        "dataset query relations are not connected to the selected root"
                    )
                required_relationship_ids.add(relationship.relationship_id)
                current = relationship.left_relation
        relationships = tuple(
            item
            for item in binding.relationships
            if item.relationship_id in required_relationship_ids
        )
        joined_relations = (primary, *(item.right_relation for item in relationships))
        aliases = {
            relation: f"stage_{stage.stage_id}_{index}"
            for index, relation in enumerate(joined_relations, start=1)
        }

        def table(relation: str) -> exp.Table:
            schema, name = relation.split(".", 1)
            return exp.Table(
                this=exp.to_identifier(name, quoted=True),
                db=exp.to_identifier(schema, quoted=True),
                alias=exp.TableAlias(
                    this=exp.to_identifier(aliases[relation], quoted=True)
                ),
            )

        joins: list[exp.Join] = []
        for relationship in relationships:
            on = exp.EQ(
                this=exp.Column(
                    this=exp.to_identifier(relationship.left_column, quoted=True),
                    table=exp.to_identifier(aliases[relationship.left_relation], quoted=True),
                ),
                expression=exp.Column(
                    this=exp.to_identifier(relationship.right_column, quoted=True),
                    table=exp.to_identifier(aliases[relationship.right_relation], quoted=True),
                ),
            )
            joins.append(
                exp.Join(
                    this=table(relationship.right_relation),
                    on=on,
                    kind=relationship.join_type.value.upper(),
                )
            )
        return _StageScope(
            source=table(primary),
            joins=tuple(joins),
            field_columns={
                ref: exp.Column(
                    this=exp.to_identifier(mappings[ref].physical_column, quoted=True),
                    table=exp.to_identifier(
                        aliases[mappings[ref].physical_relation], quoted=True
                    ),
                )
                for ref in refs
            },
            output_aliases={},
            allowed_relations=joined_relations,
        )

    def _compile_query_stage(
        self,
        *,
        stage: DatasetQueryStage,
        scope: _StageScope,
        dialect: Dialect,
        parameter,
        metric_catalog: EffectiveMetricCatalog,
    ) -> tuple[exp.Select, tuple[str, ...]]:
        scalar = lambda value: self._compile_scalar(
            value,
            scope=scope,
            dialect=dialect,
            parameter=parameter,
        )
        selections: list[exp.Expression] = []
        output_names: list[str] = []
        for projection in stage.projections:
            expression = projection.expression
            compiled = (
                self._compile_metric(
                    expression,
                    metric_catalog=metric_catalog,
                    scalar=scalar,
                    parameter=parameter,
                    dialect=dialect,
                )
                if isinstance(expression, DatasetMetricExpression)
                else self._compile_aggregate(
                    expression,
                    scalar=scalar,
                    dialect=dialect,
                )
                if isinstance(expression, DatasetAggregateExpression)
                else scalar(expression)
            )
            selections.append(exp.alias_(compiled, projection.alias, quoted=True))
            output_names.append(projection.alias)
        query = exp.select(*selections).from_(scope.source)
        if scope.joins:
            query.set("joins", list(scope.joins))
        if stage.filters:
            query = query.where(self._combine_and(scalar(item) for item in stage.filters))
        if stage.group_by:
            query.set(
                "group",
                exp.Group(expressions=[scalar(item) for item in stage.group_by]),
            )
        if stage.order_by:
            query.set(
                "order",
                exp.Order(
                    expressions=[
                        exp.Ordered(
                            this=exp.Column(
                                this=exp.to_identifier(item.name, quoted=True)
                            ),
                            desc=item.direction == "desc",
                        )
                        for item in stage.order_by
                    ]
                ),
            )
        if stage.limit is not None:
            query.set("limit", exp.Limit(expression=parameter(stage.limit, "limit")))
        return query, tuple(output_names)

    @staticmethod
    def _compile_metric(
        expression: DatasetMetricExpression,
        *,
        metric_catalog: EffectiveMetricCatalog,
        scalar,
        parameter,
        dialect: Dialect,
    ) -> exp.Expression:
        definition = DatasetQueryProgramCompiler._metric_definition(
            metric_catalog, expression.ref
        )
        return SemanticMetricSqlCompiler().compile(
            definition,
            field=lambda ref: scalar(DatasetFieldExpression(ref=ref)),
            parameter=parameter,
            dialect=dialect,
        )

    @staticmethod
    def _metric_definition(
        metric_catalog: EffectiveMetricCatalog,
        ref: str,
    ) -> SemanticMetricDefinitionV2:
        try:
            return metric_catalog.require(ref).definition
        except Exception as exc:
            raise ValueError(f"semantic metric {ref} is unavailable") from exc

    @staticmethod
    def _legacy_metric_catalog(
        binding: SemanticBindingRecord | SemanticGraphBindingRecord,
    ) -> EffectiveMetricCatalog:
        authority = f"embedded-v1:{binding.binding_id}:{binding.version}"
        return EffectiveMetricCatalog.build(
            legacy=tuple(
                MetricCatalogEntry.create(
                    definition=LegacyMetricAdapter.to_v2(metric),
                    origin=MetricCatalogOrigin.LEGACY,
                    authority_ref=authority,
                )
                for metric in binding.metrics
            )
        )

    @staticmethod
    def _compile_union_stage(
        *,
        stage: DatasetUnionStage,
        stage_outputs: dict[str, tuple[str, ...]],
    ) -> tuple[exp.Expression, tuple[str, ...]]:
        expected = stage_outputs[stage.input_stage_ids[0]]
        if not expected:
            raise ValueError("union inputs require visible outputs")
        for stage_id in stage.input_stage_ids[1:]:
            if stage_outputs[stage_id] != expected:
                raise ValueError("union inputs must expose identical ordered outputs")

        def branch(stage_id: str) -> exp.Select:
            alias = f"union_{stage.stage_id}_{stage_id}"
            return exp.select(
                *(
                    exp.Column(
                        this=exp.to_identifier(name, quoted=True),
                        table=exp.to_identifier(alias, quoted=True),
                    )
                    for name in expected
                )
            ).from_(DatasetQueryProgramCompiler._cte_table(stage_id, alias))

        query: exp.Expression = branch(stage.input_stage_ids[0])
        for stage_id in stage.input_stage_ids[1:]:
            query = exp.union(query, branch(stage_id), distinct=False)
        return query, expected

    def _compile_scalar(
        self,
        expression: DatasetScalarExpression,
        *,
        scope: _StageScope,
        dialect: Dialect,
        parameter,
    ) -> exp.Expression:
        if isinstance(expression, DatasetFieldExpression):
            try:
                return scope.field_columns[expression.ref].copy()
            except KeyError as exc:
                raise ValueError(
                    f"logical field {expression.ref} is unavailable in this stage"
                ) from exc
        if isinstance(expression, DatasetOutputExpression):
            try:
                alias = scope.output_aliases[expression.stage_id]
            except KeyError as exc:
                raise ValueError(
                    f"stage output {expression.stage_id}.{expression.name} is unavailable"
                ) from exc
            return self._output_column(alias, expression.name)
        if isinstance(expression, DatasetLiteralExpression):
            return parameter(expression.value)
        if isinstance(expression, DatasetUnaryExpression):
            operand = self._compile_scalar(
                expression.operand,
                scope=scope,
                dialect=dialect,
                parameter=parameter,
            )
            if isinstance(expression.operand, DatasetBinaryExpression):
                operand = exp.Paren(this=operand)
            return {
                "is_null": lambda: exp.Is(this=operand, expression=exp.Null()),
                "is_not_null": lambda: exp.Not(
                    this=exp.Is(this=operand, expression=exp.Null())
                ),
                "not": lambda: exp.Not(this=operand),
                "negate": lambda: exp.Neg(this=operand),
            }[expression.operation]()
        if isinstance(expression, DatasetBinaryExpression):
            left = self._compile_scalar(
                expression.left,
                scope=scope,
                dialect=dialect,
                parameter=parameter,
            )
            right = self._compile_scalar(
                expression.right,
                scope=scope,
                dialect=dialect,
                parameter=parameter,
            )
            if isinstance(expression.left, DatasetBinaryExpression):
                left = exp.Paren(this=left)
            if isinstance(expression.right, DatasetBinaryExpression):
                right = exp.Paren(this=right)
            if expression.operation == "divide":
                right = exp.Nullif(this=right, expression=exp.Literal.number(0))
            operation = {
                "add": exp.Add,
                "subtract": exp.Sub,
                "multiply": exp.Mul,
                "divide": exp.Div,
                "eq": exp.EQ,
                "neq": exp.NEQ,
                "gt": exp.GT,
                "gte": exp.GTE,
                "lt": exp.LT,
                "lte": exp.LTE,
                "and": exp.And,
                "or": exp.Or,
            }[expression.operation]
            return operation(this=left, expression=right)
        assert isinstance(expression, DatasetFunctionExpression)
        arguments = [
            self._compile_scalar(
                item,
                scope=scope,
                dialect=dialect,
                parameter=parameter,
            )
            for item in expression.arguments
        ]
        if expression.operation == "coalesce":
            return exp.Coalesce(this=arguments[0], expressions=arguments[1:])
        if expression.operation == "nullif":
            return exp.Nullif(this=arguments[0], expression=arguments[1])
        if expression.operation == "cast_float":
            return exp.cast(arguments[0], "DOUBLE")
        if expression.operation == "time_bucket":
            assert expression.time_grain is not None
            return self._time_bucket(arguments[0], expression.time_grain, dialect)
        if expression.operation == "date_part":
            assert expression.date_part is not None
            return self._date_part(arguments[0], expression.date_part, dialect)
        if expression.operation == "lower":
            return exp.Lower(this=arguments[0])
        if expression.operation == "contains_ci":
            function = "INSTR" if dialect == "sqlite" else "STRPOS"
            return exp.GT(
                this=exp.Anonymous(
                    this=function,
                    expressions=[
                        exp.Lower(this=arguments[0]),
                        exp.Lower(this=arguments[1]),
                    ],
                ),
                expression=exp.Literal.number(0),
            )
        if expression.operation == "power":
            return exp.Pow(this=arguments[0], expression=arguments[1])
        if expression.operation in {"abs", "sqrt", "sin", "cos", "asin", "radians"}:
            return exp.Anonymous(
                this=expression.operation.upper(),
                expressions=arguments,
            )
        if expression.operation == "date_diff_days":
            return self._date_diff_days(arguments[0], arguments[1], dialect)
        assert expression.operation == "date_diff_months"
        return self._date_diff_months(arguments[0], arguments[1], dialect)

    @staticmethod
    def _compile_aggregate(
        expression: DatasetAggregateExpression,
        *,
        scalar,
        dialect: Dialect,
    ) -> exp.Expression:
        operand = scalar(expression.operand) if expression.operand is not None else None
        condition = scalar(expression.filter) if expression.filter is not None else None
        if expression.operation == "count" and condition is None:
            return exp.Count(this=operand or exp.Star())
        if expression.operation == "count_distinct" and condition is None:
            assert operand is not None
            return exp.Count(this=exp.Distinct(expressions=[operand]))
        if condition is not None:
            if expression.operation == "count" and operand is None:
                return exp.Sum(
                    this=exp.Case(
                        ifs=[exp.If(this=condition, true=exp.Literal.number(1))],
                        default=exp.Literal.number(0),
                    )
                )
            filtered = exp.Case(
                ifs=[exp.If(this=condition, true=operand or exp.Literal.number(1))],
                default=exp.Null(),
            )
            if expression.operation == "count":
                return exp.Count(this=filtered)
            if expression.operation == "count_distinct":
                return exp.Count(this=exp.Distinct(expressions=[filtered]))
            operand = filtered
        assert operand is not None
        if expression.operation == "median":
            return DatasetQueryProgramCompiler._median(operand, dialect)
        return {
            "sum": exp.Sum,
            "avg": exp.Avg,
            "min": exp.Min,
            "max": exp.Max,
        }[expression.operation](this=operand)

    @staticmethod
    def _median(operand: exp.Expression, dialect: Dialect) -> exp.Expression:
        if dialect == "sqlite":
            return exp.Anonymous(this="MEDIAN", expressions=[operand])
        return exp.Median(this=operand)

    @staticmethod
    def _time_bucket(
        value: exp.Expression,
        grain: str,
        dialect: Dialect,
    ) -> exp.Expression:
        if dialect != "sqlite":
            return exp.DateTrunc(
                unit=exp.Var(this=grain),
                this=exp.cast(value, "TIMESTAMP"),
            )
        if grain == "quarter":
            year = exp.cast(
                exp.Anonymous(
                    this="STRFTIME",
                    expressions=[exp.Literal.string("%Y"), value.copy()],
                ),
                "INTEGER",
            )
            month = exp.cast(
                exp.Anonymous(
                    this="STRFTIME",
                    expressions=[exp.Literal.string("%m"), value.copy()],
                ),
                "INTEGER",
            )
            quarter = exp.Div(
                this=exp.Add(this=month, expression=exp.Literal.number(2)),
                expression=exp.Literal.number(3),
            )
            return exp.Anonymous(
                this="PRINTF",
                expressions=[exp.Literal.string("%04d-Q%d"), year, quarter],
            )
        pattern = {
            "day": "%Y-%m-%d",
            "week": "%Y-%W",
            "month": "%Y-%m",
            "year": "%Y",
        }[grain]
        return exp.Anonymous(
            this="STRFTIME",
            expressions=[exp.Literal.string(pattern), value],
        )

    @staticmethod
    def _date_diff_days(
        start: exp.Expression,
        end: exp.Expression,
        dialect: Dialect,
    ) -> exp.Expression:
        if dialect == "sqlite":
            return exp.Sub(
                this=exp.Anonymous(this="JULIANDAY", expressions=[end]),
                expression=exp.Anonymous(this="JULIANDAY", expressions=[start]),
            )
        seconds = exp.Extract(
            this=exp.Var(this="EPOCH"),
            expression=exp.Sub(
                this=exp.cast(end, "TIMESTAMP"),
                expression=exp.cast(start, "TIMESTAMP"),
            ),
        )
        return exp.Div(this=seconds, expression=exp.Literal.number(86400))

    @classmethod
    def _date_diff_months(
        cls,
        start: exp.Expression,
        end: exp.Expression,
        dialect: Dialect,
    ) -> exp.Expression:
        start_year = cls._date_part(start.copy(), "year", dialect)
        end_year = cls._date_part(end.copy(), "year", dialect)
        start_month = cls._date_part(start.copy(), "month", dialect)
        end_month = cls._date_part(end.copy(), "month", dialect)
        return exp.Add(
            this=exp.Mul(
                this=exp.Paren(
                    this=exp.Sub(this=end_year, expression=start_year)
                ),
                expression=exp.Literal.number(12),
            ),
            expression=exp.Sub(this=end_month, expression=start_month),
        )

    @staticmethod
    def _date_part(
        value: exp.Expression,
        part: str,
        dialect: Dialect,
    ) -> exp.Expression:
        if dialect != "sqlite":
            return exp.Extract(
                this=exp.Var(this="DOW" if part == "weekday" else part.upper()),
                expression=exp.cast(value, "TIMESTAMP"),
            )
        pattern = {
            "year": "%Y",
            "month": "%m",
            "day": "%d",
            "hour": "%H",
            "weekday": "%w",
        }[part]
        return exp.cast(
            exp.Anonymous(
                this="STRFTIME",
                expressions=[exp.Literal.string(pattern), value],
            ),
            "INTEGER",
        )

    @classmethod
    def _stage_field_refs(cls, stage: DatasetQueryStage) -> tuple[str, ...]:
        refs: list[str] = []
        for projection in stage.projections:
            expression = projection.expression
            if isinstance(expression, DatasetAggregateExpression):
                if expression.operand is not None:
                    refs.extend(cls._scalar_field_refs(expression.operand))
                if expression.filter is not None:
                    refs.extend(cls._scalar_field_refs(expression.filter))
            elif isinstance(expression, DatasetMetricExpression):
                continue
            else:
                refs.extend(cls._scalar_field_refs(expression))
        for expression in (*stage.filters, *stage.group_by):
            refs.extend(cls._scalar_field_refs(expression))
        return tuple(dict.fromkeys(refs))

    @classmethod
    def _stage_measure_refs(cls, stage: DatasetQueryStage) -> tuple[str, ...]:
        refs: list[str] = []
        for projection in stage.projections:
            if (
                isinstance(projection.expression, DatasetAggregateExpression)
                and projection.expression.operand is not None
            ):
                refs.extend(cls._scalar_field_refs(projection.expression.operand))
        return tuple(dict.fromkeys(refs))

    @staticmethod
    def _stage_metric_refs(stage: DatasetQueryStage) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.expression.ref
                for item in stage.projections
                if isinstance(item.expression, DatasetMetricExpression)
            )
        )

    @classmethod
    def _scalar_field_refs(cls, expression: DatasetScalarExpression) -> tuple[str, ...]:
        if isinstance(expression, DatasetFieldExpression):
            return (expression.ref,)
        if isinstance(expression, (DatasetOutputExpression, DatasetLiteralExpression)):
            return ()
        if isinstance(expression, DatasetUnaryExpression):
            return cls._scalar_field_refs(expression.operand)
        if isinstance(expression, DatasetBinaryExpression):
            return tuple(
                dict.fromkeys(
                    (*cls._scalar_field_refs(expression.left), *cls._scalar_field_refs(expression.right))
                )
            )
        assert isinstance(expression, DatasetFunctionExpression)
        return tuple(
            dict.fromkeys(
                ref
                for argument in expression.arguments
                for ref in cls._scalar_field_refs(argument)
            )
        )

    @staticmethod
    def _combine_and(expressions: Iterable[exp.Expression]) -> exp.Expression:
        values = list(expressions)
        if not values:
            raise ValueError("at least one predicate is required")
        combined = values[0]
        for value in values[1:]:
            combined = exp.and_(combined, value)
        return combined

    @staticmethod
    def _cte_table(stage_id: str, alias: str) -> exp.Table:
        return exp.Table(
            this=exp.to_identifier(stage_id, quoted=True),
            alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)),
        )

    @staticmethod
    def _output_column(alias: str, name: str) -> exp.Column:
        return exp.Column(
            this=exp.to_identifier(name, quoted=True),
            table=exp.to_identifier(alias, quoted=True),
        )


__all__ = ["DatasetQueryProgramCompiler"]
